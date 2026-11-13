"""Protocol-only teaching prototype. No model, Scheduler, task or approval policy.

Uses newline JSON over ephemeral loopback TCP, NOT Codex WebSocket transport.
Caller chooses eligible peers; the returned answer is input to the real pending
owner, never proof that a tool ran. Single asyncio event loop owns all maps.
"""
import asyncio
import json
from dataclasses import dataclass, field


@dataclass
class Peer:
    writer: asyncio.StreamWriter
    initialized: bool = False
    closed: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class Question:
    recipients: set[int]
    future: asyncio.Future


class Relay:
    def __init__(self):
        self.peers = {}
        self.pending = {}
        self.next_peer = 0
        self.next_request = 0
        self.tasks = set()
        self.audit = []
        self.server = None

    async def start(self):
        self.server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[:2]

    async def _send(self, peer_id, frame):
        peer = self.peers.get(peer_id)
        if peer is None:
            return False
        try:
            peer.writer.write(json.dumps(frame).encode() + b"\n")
            await peer.writer.drain()
            return True
        except (ConnectionError, OSError):
            self.disconnect(peer_id)
            return False

    async def _accept(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        self.next_peer += 1
        peer_id = self.next_peer
        peer = Peer(writer)
        self.peers[peer_id] = peer
        try:
            while raw := await reader.readline():
                try:
                    frame = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self.audit.append(("invalid_json", peer_id))
                    break
                if not isinstance(frame, dict):
                    self.audit.append(("invalid_frame", peer_id))
                    break
                if "method" in frame:
                    if "id" not in frame:
                        self.audit.append(("notification", peer_id, frame["method"]))
                        continue
                    request_id = frame["id"]
                    if type(request_id) not in (str, int):
                        self.audit.append(("invalid_id", peer_id))
                        break
                    method = frame["method"]
                    error = None
                    if method == "initialize":
                        if peer.initialized:
                            error = (-32600, "Already initialized")
                        else:
                            peer.initialized = True
                            result = {"peerId": peer_id}
                    elif not peer.initialized:
                        error = (-32600, "Not initialized")
                    elif method == "protocol/echo":
                        if not isinstance(frame.get("params"), dict):
                            error = (-32602, "Invalid params")
                        else:
                            result = frame["params"]
                    else:
                        error = (-32601, "Method not found")
                    response = {"id": request_id}
                    if error:
                        response["error"] = {"code": error[0], "message": error[1]}
                    else:
                        response["result"] = result
                    await self._send(peer_id, response)
                elif "id" in frame and ("result" in frame or "error" in frame):
                    self.respond(peer_id, frame)
                else:
                    self.audit.append(("invalid_frame", peer_id))
                    break
        finally:
            self.disconnect(peer_id)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            peer.closed.set()
            self.tasks.discard(task)

    async def ask(self, recipients, params):
        eligible = set(recipients)
        if not eligible or any(i not in self.peers or not self.peers[i].initialized
                               for i in eligible):
            raise ValueError("all recipients must be live and initialized")
        self.next_request += 1
        request_id = f"server:{self.next_request}"
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = Question(eligible, future)
        for peer_id in sorted(eligible):
            await self._send(peer_id, {"id": request_id, "method": "input/ask",
                                       "params": params})
        return request_id, future

    def respond(self, peer_id, frame):
        request_id = frame["id"]
        question = self.pending.get(request_id) if type(request_id) in (str, int) else None
        peer = self.peers.get(peer_id)
        if not peer or not peer.initialized or not question or peer_id not in question.recipients:
            self.audit.append(("rejected_response", peer_id, request_id))
            return False
        # Validate before consuming: malformed input cannot steal another peer's prompt.
        if "error" in frame:
            answer = {"status": "client_error"}
        elif isinstance(frame.get("result"), dict) and isinstance(frame["result"].get("answer"), str):
            answer = {"status": "answered", "answer": frame["result"]["answer"]}
        else:
            self.audit.append(("invalid_answer", peer_id, request_id))
            return False
        self.pending.pop(request_id)
        question.future.set_result(answer)
        self.audit.append(("settled", peer_id, request_id))
        return True

    def disconnect(self, peer_id):
        peer = self.peers.pop(peer_id, None)
        if peer:
            peer.writer.close()
        for request_id, question in list(self.pending.items()):
            question.recipients.discard(peer_id)
            if not question.recipients:
                self.pending.pop(request_id)
                question.future.set_result({"status": "no_recipients"})
                self.audit.append(("orphaned", request_id))

    async def close(self):
        self.server.close()
        await self.server.wait_closed()
        for peer_id in list(self.peers):
            self.disconnect(peer_id)
        await asyncio.gather(*tuple(self.tasks))
