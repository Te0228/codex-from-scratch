"""Synchronous loopback client using the notebook's framing and pending router."""
from itertools import count
import json
import socket
import threading

from .protocol_lab import JsonlDecoder, PendingRouter, ProtocolError


EPOCHS = count(1)


class RPCClient:
    def __init__(self, address, *, timeout=5):
        if address[0]!='127.0.0.1':
            raise ValueError('only loopback connections are supported')
        self.socket = socket.create_connection(address,timeout=timeout)
        self.reader = self.socket.makefile('rb')
        self.router = PendingRouter(next(EPOCHS))
        self.decoder = JsonlDecoder()
        self.next_id = 0
        self.lock = threading.Lock()
        self.request('initialize',{})

    def request(self, method, params=None):
        with self.lock:
            self.next_id += 1
            request_id = self.next_id
            future = self.router.register(request_id)
            try:
                packet = {'jsonrpc':'2.0','id':request_id,'method':method,'params':params or {}}
                self.socket.sendall(json.dumps(packet).encode()+b'\n')
                raw = self.reader.readline(1_048_578)
                if not raw:
                    raise ConnectionError('server disconnected; remote status unknown')
                frames = list(self.decoder.feed(raw))
                if len(frames)!=1 or frames[0].get('id')!=request_id:
                    raise ProtocolError('unexpected response identity')
                self.router.response(self.router.epoch,request_id,frames[0])
                response = future.result()
                if 'error' in response:
                    raise ProtocolError(json.dumps(response['error']))
                return response['result']
            except BaseException:
                if not future.done():
                    self.router.disconnect()
                    future.exception()  # Observe local waiter failure; do not retry a remote run.
                raise

    def run(self, prompt, *, replay_responses=None):
        params = {'prompt':prompt}
        if replay_responses is not None:
            params['replay_responses'] = replay_responses
        return self.request('run',params)

    def status(self, handle):
        return self.request('status',{'handle':handle})

    def events(self, handle, after_event=0):
        return self.request('events',{'handle':handle,'after_event':after_event})

    def cancel(self, handle):
        return self.request('cancel',{'handle':handle})

    def close(self):
        self.router.disconnect()
        self.reader.close()
        self.socket.close()

    def __enter__(self):
        return self

    def __exit__(self,*exc):
        self.close()
