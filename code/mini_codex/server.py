"""Local JSON-RPC adapter over the same MiniCodex engine.

TCP binds only loopback; stdio serves a single initialized client. Disconnecting
one client drops its connection, not the running task. Explicit cancel addresses
one server run. Server shutdown cancels and joins work owned by this process.
No authentication or production remote-control guarantees are claimed.
"""
from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
import sys
import threading
import uuid


class RPCError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class AppServer:
    def __init__(self, config):
        from .engine import MiniCodex
        self.config = config
        self.agent = MiniCodex(config)
        self.pool = ThreadPoolExecutor(max_workers=1,thread_name_prefix='mini-codex-server-run')
        self.lock = threading.RLock()
        self.runs = {}
        self.active_id = None
        self.closed = False

    def _submit(self, params):
        prompt = params.get('prompt')
        if not isinstance(prompt,str) or not prompt.strip():
            raise RPCError(-32602,'run requires a nonempty prompt')
        replay = params.get('replay_responses')
        if replay is not None and (self.config.mode!='replay' or not isinstance(replay,list)):
            raise RPCError(-32602,'replay_responses require replay mode and an array')
        with self.lock:
            if self.closed:
                raise RPCError(-32000,'server closed')
            if self.active_id is not None:
                raise RPCError(-32001,'a run is already active')
            handle = uuid.uuid4().hex
            record = {'handle':handle,'status':'accepted','result':None,'error':None,'events':[],
                      'run_id':None,'cancel_requested':threading.Event(),'finished':threading.Event(),
                      'previous_run_id':getattr(self.agent,'current_run_id',None)}
            self.runs[handle] = record
            self.active_id = handle
            future = self.pool.submit(self._run,record,prompt,replay)
            record['future'] = future
            return {'handle':handle,'status':'accepted'}

    def _run(self, record, prompt, replay):
        def deliver_cancel():
            while not record['finished'].wait(0.01):
                if record['cancel_requested'].is_set():
                    with self.lock:
                        if self.active_id != record['handle'] or record['finished'].is_set():
                            return
                        if self.agent.cancel():
                            return
        cancel_bridge = threading.Thread(target=deliver_cancel,daemon=True,name='mini-codex-cancel-bridge')
        cancel_bridge.start()
        try:
            with self.lock:
                record['status'] = 'running'
            result = self.agent.run(prompt,replay_responses=replay)
            with self.lock:
                record['result'] = asdict(result)
                record['run_id'] = result.run_id
                record['status'] = result.task_status
        except Exception as exc:
            with self.lock:
                record['status'] = 'failed'
                # Avoid serializing provider exceptions with credential-bearing payloads.
                record['error'] = type(exc).__name__
        finally:
            try:
                current = getattr(self.agent,'current_run_id',None)
                if current != record['previous_run_id']:
                    record['run_id'] = current
                events = [e for e in self.agent.events(after_event=0)
                          if e.get('run_id') == record['run_id']]
            except Exception:
                events = []
            with self.lock:
                record['events'] = events
                record['finished'].set()
                self.active_id = None
            cancel_bridge.join(timeout=0.1)

    def _record(self, params):
        handle = params.get('handle')
        with self.lock:
            if handle not in self.runs:
                raise RPCError(-32602,'unknown run handle')
            return self.runs[handle]

    def request(self, frame, connection):
        if not isinstance(frame,dict) or not isinstance(frame.get('method'),str):
            raise RPCError(-32600,'invalid request')
        method, params = frame['method'],frame.get('params',{})
        if not isinstance(params,dict):
            raise RPCError(-32602,'params must be an object')
        if method=='initialize':
            if connection.get('initialized'):
                raise RPCError(-32600,'already initialized')
            connection['initialized'] = True
            return {'server':'mini-codex','protocol':'local-jsonrpc-v1',
                    'capabilities':{'run':True,'events':True,'cancel':True},'transport_scope':'local-only'}
        if not connection.get('initialized'):
            raise RPCError(-32600,'not initialized')
        if method=='run':
            return self._submit(params)
        if method=='status':
            record = self._record(params)
            with self.lock:
                return {k:record[k] for k in ('handle','run_id','status','result','error')}
        if method=='events':
            record = self._record(params)
            after = params.get('after_event',0)
            if type(after) is not int or after<0:
                raise RPCError(-32602,'after_event must be a nonnegative integer')
            with self.lock:
                finished = record['finished'].is_set()
                if finished:
                    events = list(record['events'])
                elif getattr(self.agent,'current_run_id',None)!=record['previous_run_id']:
                    record['run_id'] = self.agent.current_run_id
                    events = [e for e in self.agent.events(after_event=0)
                              if e.get('run_id') == record['run_id']]
                else:
                    events = []
            # Engine events own their sequence and payload; this adapter creates none.
            selected = [e for e in events if e.get('sequence',0)>after]
            return {'handle':record['handle'],'run_id':record['run_id'],'events':selected,
                    'cursor':max((e.get('sequence',0) for e in events),default=after),'finished':finished}
        if method=='cancel':
            record = self._record(params)
            with self.lock:
                if record['finished'].is_set() or self.active_id != record['handle']:
                    return {'handle':record['handle'],'requested':False,'status':record['status']}
                record['cancel_requested'].set()
                delivered = self.agent.cancel()
                return {'handle':record['handle'],'requested':True,'delivered_to_active_run':delivered}
        raise RPCError(-32601,'method not found')

    def frame(self, frame, connection):
        request_id = frame.get('id') if isinstance(frame,dict) else None
        if type(request_id) not in (int,str):
            return {'jsonrpc':'2.0','id':None,'error':{'code':-32600,'message':'request id required'}}
        try:
            result = self.request(frame,connection)
            return {'jsonrpc':'2.0','id':request_id,'result':result}
        except RPCError as exc:
            return {'jsonrpc':'2.0','id':request_id,'error':{'code':exc.code,'message':str(exc)}}
        except Exception:
            return {'jsonrpc':'2.0','id':request_id,'error':{'code':-32603,'message':'internal error'}}

    def close(self):
        with self.lock:
            self.closed = True
            active = self.runs.get(self.active_id)
            if active:
                active['cancel_requested'].set()
        self.agent.cancel()
        self.pool.shutdown(wait=True,cancel_futures=False)
        if hasattr(self.agent,'close'):
            self.agent.close()
        elif getattr(self.agent,'extensions',None):
            self.agent.extensions.close()


class LoopbackServer:
    def __init__(self, app):
        self.app = app
        self.server = None
        self.writers = set()
        self.tasks = set()

    async def start(self, port=0, host='127.0.0.1'):
        if host!='127.0.0.1':
            raise ValueError('only 127.0.0.1 is supported')
        self.server = await asyncio.start_server(self._client,host,port,limit=1_048_576)
        return self.server.sockets[0].getsockname()[:2]

    async def _client(self, reader, writer):
        connection = {}
        task = asyncio.current_task()
        self.tasks.add(task)
        self.writers.add(writer)
        try:
            while raw := await reader.readline():
                if len(raw)>1_048_576:
                    break
                try:
                    frame = json.loads(raw)
                except (ValueError,UnicodeError):
                    response = {'jsonrpc':'2.0','id':None,'error':{'code':-32700,'message':'invalid JSON'}}
                else:
                    response = await asyncio.to_thread(self.app.frame,frame,connection)
                writer.write(json.dumps(response,ensure_ascii=False).encode()+b'\n')
                await writer.drain()
        except (ConnectionError,OSError,ValueError):
            pass
        finally:
            self.writers.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError,OSError):
                pass
            self.tasks.discard(task)

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for writer in tuple(self.writers):
            writer.close()
        if self.tasks:
            await asyncio.gather(*tuple(self.tasks))
        await asyncio.to_thread(self.app.close)


def serve_stdio(config, input_stream=None, output_stream=None):
    app = AppServer(config)
    source, target = input_stream or sys.stdin,output_stream or sys.stdout
    connection = {}
    try:
        for raw in source:
            if len(raw.encode())>1_048_576:
                break
            try:
                frame = json.loads(raw)
                result = app.frame(frame,connection)
            except ValueError:
                result = {'jsonrpc':'2.0','id':None,'error':{'code':-32700,'message':'invalid JSON'}}
            target.write(json.dumps(result,ensure_ascii=False)+'\n')
            target.flush()
    finally:
        app.close()


async def serve_loopback(config, *, port=8765):
    host = LoopbackServer(AppServer(config))
    address = await host.start(port)
    print(json.dumps({'listening':f'{address[0]}:{address[1]}'}),flush=True)
    try:
        await host.server.serve_forever()
    finally:
        await host.close()


def serve(*, workspace, state_dir, host='127.0.0.1', port=8765, mode='replay'):
    """CLI entry point, using the same configured engine for every RPC run."""
    from .engine import AgentConfig
    if host != '127.0.0.1':
        raise ValueError('only 127.0.0.1 is supported')
    config = AgentConfig(workspace=workspace,state_dir=state_dir,mode=mode)
    try:
        asyncio.run(serve_loopback(config,port=port))
    except KeyboardInterrupt:
        return 0
    return 0


def main():
    from pathlib import Path
    from .engine import AgentConfig
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace',type=Path,required=True)
    parser.add_argument('--state-dir',type=Path,required=True)
    parser.add_argument('--transport',choices=('stdio','tcp'),default='stdio')
    parser.add_argument('--port',type=int,default=8765)
    args = parser.parse_args()
    config = AgentConfig(workspace=args.workspace,state_dir=args.state_dir)
    if args.transport=='stdio':
        serve_stdio(config)
    else:
        asyncio.run(serve_loopback(config,port=args.port))


if __name__=='__main__':
    main()
