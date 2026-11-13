"""Actual loopback and stdio tests using the same unmocked MiniCodex."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from mini_codex.engine import AgentConfig
from mini_codex.model import function_call, message
from mini_codex.server import AppServer, LoopbackServer
from mini_codex.components.clients.rpc_client import RPCClient


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root/'workspace'
        self.workspace.mkdir()
        (self.workspace/'tasks.txt').write_text('status=open\n')
        self.app = AppServer(AgentConfig(self.workspace,self.root/'state',timeout=5,max_steps=4))
        self.server = LoopbackServer(self.app)
        self.address = await self.server.start()
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            client.close()
        await self.server.close()
        self.temp.cleanup()

    async def client(self):
        client = await asyncio.to_thread(RPCClient,self.address)
        self.clients.append(client)
        return client

    async def terminal(self,client,handle):
        for _ in range(300):
            status = await asyncio.to_thread(client.status,handle)
            if status['status'] not in ('accepted','running'):
                return status
            await asyncio.sleep(0.01)
        self.fail('server run did not terminate')

    async def test_uninitialized_connection_is_rejected(self):
        reader,writer = await asyncio.open_connection(*self.address)
        try:
            writer.write(json.dumps({'id':1,'method':'status','params':{'handle':'x'}}).encode()+b'\n')
            await writer.drain()
            result = json.loads(await asyncio.wait_for(reader.readline(),2))
            self.assertEqual(result['error']['message'],'not initialized')
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_run_events_and_extension_observations_are_same_engine(self):
        client = await self.client()
        script = [[function_call('plan-1','plan_update',steps=[{'text':'inspect','status':'in_progress'}])],
                  [function_call('worker-1','agent_task',paths=['tasks.txt'])],
                  [message('Inspection finished; no verifier configured')]]
        accepted = await asyncio.to_thread(client.run,'Inspect tasks',replay_responses=script)
        status = await self.terminal(client,accepted['handle'])
        self.assertEqual(status['status'],'needs_review',status)
        self.assertEqual(status['result']['run_id'],self.app.agent.last_result.run_id)
        fetched = await asyncio.to_thread(client.events,accepted['handle'])
        self.assertEqual(fetched['events'],self.app.agent.events())
        observations = [e for e in fetched['events'] if e['kind']=='tool_observation']
        self.assertEqual(len(observations),2,observations)
        report = observations[-1]['payload']['content']['result']['report']
        self.assertEqual(report['executor'],'controlled-reader-thread')
        self.assertEqual(self.app.agent.plan.revision,1)
        again = await asyncio.to_thread(client.events,accepted['handle'],fetched['cursor'])
        self.assertEqual(again['events'],[])

    async def test_disconnect_keeps_run_alive_then_other_client_cancels(self):
        entered,release = threading.Event(),threading.Event()
        original = self.app.agent.extensions._inspect
        def held(paths,budget,cancelled):
            entered.set()
            while not cancelled.is_set() and not release.wait(0.01):
                pass
            return original(paths,budget,cancelled)
        self.app.agent.extensions._inspect = held
        first,second = await self.client(),await self.client()
        script = [[function_call('worker-cancel','agent_task',paths=['tasks.txt'])], [message('done')]]
        accepted = await asyncio.to_thread(first.run,'Read slowly',replay_responses=script)
        self.assertTrue(await asyncio.to_thread(entered.wait,2))
        first.close()
        self.clients.remove(first)
        live = await asyncio.to_thread(second.status,accepted['handle'])
        self.assertEqual(live['status'],'running')
        cancelled = await asyncio.to_thread(second.cancel,accepted['handle'])
        self.assertTrue(cancelled['requested'])
        terminal = await self.terminal(second,accepted['handle'])
        self.assertEqual(terminal['status'],'cancelled',terminal)
        self.assertFalse(self.app.agent.extensions.tree.leases)
        events = await asyncio.to_thread(second.events,accepted['handle'])
        self.assertTrue(any(e['kind']=='run_result' for e in events['events']))


    async def test_two_run_handles_expose_only_their_committed_events(self):
        client = await self.client()
        runs = []
        for prompt in ('first task','second task'):
            accepted = await asyncio.to_thread(client.run,prompt,replay_responses=[[message('Review required')]])
            status = await self.terminal(client,accepted['handle'])
            events = await asyncio.to_thread(client.events,accepted['handle'])
            self.assertTrue(events['events'])
            self.assertEqual({e['run_id'] for e in events['events']},{status['result']['run_id']})
            runs.append((accepted['handle'],events))
        old = await asyncio.to_thread(client.events,runs[0][0])
        self.assertEqual(old,runs[0][1])
        self.assertNotEqual(runs[0][1]['run_id'],runs[1][1]['run_id'])

    async def test_late_cancel_for_old_handle_cannot_cancel_new_run(self):
        entered,release_first,release_second = threading.Event(),threading.Event(),threading.Event()
        original_inspect = self.app.agent.extensions._inspect
        count = 0
        def held(paths,budget,cancelled):
            nonlocal count
            count += 1
            release = release_first if count==1 else release_second
            entered.set()
            while not cancelled.is_set() and not release.wait(0.01):
                pass
            return original_inspect(paths,budget,cancelled)
        self.app.agent.extensions._inspect = held
        a,b = await self.client(),await self.client()
        script = [[function_call('reader','agent_task',paths=['tasks.txt'])],[message('review')]]
        first = await asyncio.to_thread(a.run,'first',replay_responses=script)
        self.assertTrue(await asyncio.to_thread(entered.wait,2))
        captured,allow_cancel = threading.Event(),threading.Event()
        original_record = self.app._record
        def paused_lookup(params):
            record = original_record(params)
            if params.get('handle')==first['handle']:
                captured.set()
                allow_cancel.wait(3)
            return record
        self.app._record = paused_lookup
        late = asyncio.create_task(asyncio.to_thread(a.cancel,first['handle']))
        try:
            self.assertTrue(await asyncio.to_thread(captured.wait,2))
            release_first.set()
            self.assertTrue(await asyncio.to_thread(self.app.runs[first['handle']]['finished'].wait,2))
            entered.clear()
            second = await asyncio.to_thread(b.run,'second',replay_responses=script)
            self.assertTrue(await asyncio.to_thread(entered.wait,2))
            allow_cancel.set()
            answer = await late
            self.assertFalse(answer['requested'])
            self.assertFalse(self.app.runs[second['handle']]['cancel_requested'].is_set())
            self.assertEqual((await asyncio.to_thread(b.status,second['handle']))['status'],'running')
        finally:
            allow_cancel.set()
            self.app._record = original_record
            release_first.set()
            release_second.set()
            await late



class StdioServerTests(unittest.TestCase):
    def test_owned_stdio_process_initializes_and_runs_same_kernel(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root/'workspace'
            workspace.mkdir()
            command = [sys.executable,'-m','mini_codex.server','--workspace',str(workspace),
                       '--state-dir',str(root/'state'),'--transport','stdio']
            process = subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE,text=True)
            def request(number,method,params):
                process.stdin.write(json.dumps({'id':number,'method':method,'params':params})+'\n')
                process.stdin.flush()
                return json.loads(process.stdout.readline())
            try:
                self.assertIn('result',request(1,'initialize',{}))
                accepted = request(2,'run',{'prompt':'No tool run','replay_responses':[[message('Review required')]]})['result']
                for number in range(3,203):
                    status = request(number,'status',{'handle':accepted['handle']})['result']
                    if status['status'] not in ('accepted','running'):
                        break
                    time.sleep(0.01)
                self.assertEqual(status['status'],'needs_review',status)
                self.assertIsNotNone(status['result']['run_id'])
            finally:
                process.stdin.close()
                process.wait(timeout=5)
                stderr = process.stderr.read()
                process.stdout.close()
                process.stderr.close()
                self.assertEqual(process.returncode,0,stderr)


if __name__=='__main__':
    unittest.main()
