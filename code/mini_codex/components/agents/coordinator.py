"""待集成的协调算法；无模型、无新 Loop、无进程恢复承诺。

AgentTree 由一个事件循环串行调用。Board 使用实际 SQLite 事务。
Actor/权限句柄必须由宿主绑定，不能来自模型可编辑的参数。
"""
from dataclasses import dataclass, field
import json
import sqlite3


@dataclass(frozen=True)
class Lease:
    agent: str
    generation: int


@dataclass
class Agent:
    parent: str | None
    grants: frozenset[str]
    generation: int = 0
    state: str = "idle"
    resident: bool = True
    closed: bool = False
    mailbox: list[str] = field(default_factory=list)


class AgentTree:
    def __init__(self, execution_capacity=2, residency_capacity=4):
        self.agents = {"/root": Agent(None, frozenset({"read", "write"}))}
        self.execution_capacity = execution_capacity
        self.residency_capacity = residency_capacity
        self.leases: set[Lease] = set()
        self.events: list[tuple] = []

    def spawn(self, parent, name, allowed):
        owner = self.agents[parent]
        if owner.closed:
            raise RuntimeError("subtree_closed")
        if not name or not all(c.isascii() and (c.isalnum() or c == "_") for c in name):
            raise ValueError("task_name")
        path = parent + "/" + name
        if path in self.agents:
            raise ValueError("duplicate_path")
        if sum(a.resident for k, a in self.agents.items() if k != "/root") >= self.residency_capacity:
            raise RuntimeError("ResidencyCapacity")
        # 教学设计：requested allowlist 只能收紧父授权；真实 grants 留在前章。
        self.agents[path] = Agent(parent, owner.grants & frozenset(allowed))
        self.events.append(("spawn", path))
        return path

    def start(self, path):
        agent = self.agents[path]
        if path == "/root" or agent.closed or not agent.resident or agent.state != "idle":
            raise RuntimeError("not_startable")
        if len(self.leases) >= self.execution_capacity:
            raise RuntimeError("ExecutionCapacity")
        agent.generation += 1
        lease = Lease(path, agent.generation)
        self.leases.add(lease)
        agent.state = "running"
        self.events.append(("start", lease))
        return lease

    def finish(self, lease, outcome):
        # 仅释放这个实例的租约；旧 worker 晚到不能改写新 turn。
        if lease not in self.leases:
            self.events.append(("stale_completion", lease, outcome))
            return False
        self.leases.remove(lease)
        agent = self.agents[lease.agent]
        agent.state = "closed" if agent.closed else "idle"
        if agent.closed:
            agent.resident = False
        self.events.append(("finish", lease, outcome))
        return True

    def send(self, path, text):
        agent = self.agents[path]
        if agent.closed:
            raise RuntimeError("subtree_closed")
        agent.mailbox.append(text)
        self.events.append(("mail", path, text))

    def followup(self, path, text):
        self.send(path, text)  # 输入先接受；admission 拒绝不静默丢输入。
        if self.agents[path].state == "idle":
            return self.start(path)
        return None

    def cancel_subtree(self, path):
        selected = [k for k in self.agents if k == path or k.startswith(path + "/")]
        pending = []
        for key in selected:
            a = self.agents[key]
            a.closed = True  # 先 fencing；不再接受子任务。
            if a.state == "running" or a.state == "cancelling":
                a.state = "cancelling"
                pending.append(Lease(key, a.generation))
            else:
                a.state, a.resident = "closed", False
        self.events.append(("cancel_requested", path))
        return pending  # scheduler 确认停止后调用 finish；这里不提前腾槽。

    def evict(self, path):
        a = self.agents[path]
        if path == "/root" or a.state != "idle":
            raise RuntimeError("busy")
        a.resident = False


class Board:
    """一个本地协作板；通知只是回调尝试，不提供可靠消息队列。

持久幂等键=(actor, request_id)，分页按实际插入 seq；仅提供记录补读。
"""
    def __init__(self, path, notify):
        self.db = sqlite3.connect(path)
        self.notify = notify
        self.failures = []
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS posts(
          seq INTEGER PRIMARY KEY, actor TEXT, request_id TEXT, fingerprint TEXT,
          body TEXT, operation TEXT, UNIQUE(actor,request_id));
        CREATE TABLE IF NOT EXISTS subscriptions(actor TEXT PRIMARY KEY, enabled INTEGER);
        """)

    def close(self):
        self.db.close()

    def subscribe(self, actor, enabled=True):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO subscriptions VALUES(?,?)", (actor, int(enabled)))

    def write(self, actor, request_id, body, *, writable, operation="post", lose_response=False):
        if not writable:
            raise PermissionError("read_only_connection")
        if operation not in {"post", "append"} or not request_id or not body:
            raise ValueError("invalid_write")
        fingerprint = json.dumps((operation, body), ensure_ascii=False)
        recipients = []
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            old = self.db.execute("SELECT seq,fingerprint FROM posts WHERE actor=? AND request_id=?", (actor, request_id)).fetchone()
            if old:
                if old[1] != fingerprint:
                    raise ValueError("request_id_reused")
                return old[0]  # 不重播通知。
            seq = self.db.execute("INSERT INTO posts(actor,request_id,fingerprint,body,operation) VALUES(?,?,?,?,?)", (actor, request_id, fingerprint, body, operation)).lastrowid
            if operation == "post":
                recipients = [r[0] for r in self.db.execute("SELECT actor FROM subscriptions WHERE enabled=1 AND actor<>?", (actor,))]
                self.db.execute("INSERT OR IGNORE INTO subscriptions VALUES(?,1)", (actor,))
        # sqlite commit 已完成。断开的调用者不能据此断言写入未发生。
        for recipient in recipients:
            try:
                self.notify(recipient, seq)
            except Exception as exc:
                self.failures.append((recipient, seq, type(exc).__name__))
        if lose_response:
            raise ConnectionError("response_lost_after_commit")
        return seq

    def capture(self):
        return self.db.execute("SELECT coalesce(max(seq),0) FROM posts").fetchone()[0]

    def read(self, after=0, cutoff=None, limit=100):
        if limit < 1:
            raise ValueError("limit")
        cutoff = self.capture() if cutoff is None else cutoff
        return self.db.execute("SELECT seq,actor,request_id,body,operation FROM posts WHERE seq>? AND seq<=? ORDER BY seq LIMIT ?", (after, cutoff, limit)).fetchall()
