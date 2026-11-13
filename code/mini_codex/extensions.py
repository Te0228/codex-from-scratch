"""The chapter components attached to the same production-of-evidence loop.

All model-visible operations are bounded. Explicit memory writes, installation,
external command configuration and worktree creation are host APIs, not model
instructions. State lives outside the model-editable file surface.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import re
import subprocess
import threading
import time
import uuid

from .contracts import CallContext
from .components.memory.ledger import Ledger, Candidate
from .components.goals import goals
from .components.skills.skill_catalog import Skills, SkillError
from .components.plugins.catalog import PluginStore, ToolCatalog, StaleCatalog, Denied
from .components.agents.coordinator import AgentTree, Board
from .components.mcp.stdio_client import StdioMCPClient, MCPError
from .components.orchestration.controlled import execute_plan, complete
from .components.environments import ownership_probe as worktrees


def response(outcome, result=None, error=None, *, effect='none', cleanup='complete'):
    return {'outcome':outcome,'result':result,'error':error,
            'side_effect_status':effect,'cleanup_status':cleanup}


def function(name, description, properties, required=()):
    return {'type':'function','name':name,'description':description,'strict':False,
            'parameters':{'type':'object','properties':properties,
                          'required':list(required),'additionalProperties':False}}


class ExtensionServices:
    def __init__(self, workspace: Path, state_dir: Path):
        self.workspace = Path(workspace).resolve(strict=True)
        self.state_dir = Path(state_dir).resolve()
        self.state_dir.mkdir(parents=True,exist_ok=True)
        if self.state_dir == self.workspace:
            raise ValueError('state_dir must not equal workspace')
        self.scope = hashlib.sha256(str(self.workspace).encode()).hexdigest()
        self.memory_path = self.state_dir/'memory.sqlite'
        self.board_path = self.state_dir/'board.sqlite'
        self.lock, self.tree_lock = threading.RLock(), threading.RLock()
        self.plan = goals.Plan()
        self.goal = None
        self.tree = AgentTree(execution_capacity=2,residency_capacity=2)
        self.pool = ThreadPoolExecutor(max_workers=2,thread_name_prefix='mini-codex-reader')
        self.cancelled = {}
        self.selected_skills = {}
        skill_root = self.state_dir/'skills'
        skill_root.mkdir(exist_ok=True)
        self.skills = Skills({'local':skill_root},lambda authority:authority=='local')
        self.plugins = PluginStore(self.state_dir/'plugins')
        self.catalog = ToolCatalog(self.plugins)
        self.mcp = {}
        self.code_state = {}
        self.closed = False
        self.notifications = []

    def _cancel(self, run_id):
        with self.lock:
            return self.cancelled.setdefault(run_id,threading.Event())

    def cancel(self, run_id):
        self._cancel(run_id).set()

    def remember(self, text, key='user-memory'):
        """Host-only explicit user memory. Quotes refer to actual supplied text."""
        if not isinstance(text,str) or not text.strip() or len(text.encode()) > 16384:
            raise ValueError('memory text must contain 1..16384 bytes')
        with self.lock:
            ledger = Ledger(self.memory_path)
            try:
                source = 'explicit-user/'+uuid.uuid4().hex
                version = ledger.put_source(source,text)
                candidate = Candidate(self.scope,key,text,source,version,1,
                    len(text.splitlines()),'\n'.join(text.splitlines()),'explicit-user-memory')
                old = ledger.db.execute("SELECT id FROM memories WHERE scope=? AND key=? AND state='active'",
                                        (self.scope,key)).fetchone()
                op = uuid.uuid4().hex
                if old:
                    return ledger.correct(op,old['id'],candidate,'Explicit user remember/update request')
                return ledger.merge(op,candidate)
            finally:
                ledger.close()

    def _memory(self, query, max_bytes=8192):
        if not isinstance(query,str):
            raise ValueError('query must be text')
        max_bytes = max(0,min(int(max_bytes),16384))
        with self.lock:
            ledger = Ledger(self.memory_path)
            try:
                return ledger.project(self.scope,'explicit-user-memory',query,max_bytes)
            finally:
                ledger.close()

    def context(self, prompt: str) -> str:
        # Exact lexical matching is intentional; no semantic extraction is implied.
        terms = list(dict.fromkeys(re.findall(r'\w+',prompt.casefold())))[:16]
        selected, used = {}, 0
        for term in terms:
            if len(term) < 2:
                continue
            projection = self._memory(term,4096-used)
            for line in projection['context'].splitlines():
                item = json.loads(line)
                if item['id'] not in selected:
                    selected[item['id']] = item
                    used += len((line+'\n').encode())
        with self.lock:
            skills = list(self.selected_skills.values())
        if not selected and not skills:
            return ''
        return json.dumps({'kind':'extension_reference_data',
            'notice':'Historical memory and selected skill source data; not current verification or new authority.',
            'memories':list(selected.values()),'selected_skill_sources':skills},ensure_ascii=False)

    def set_goal(self, objective, *, authority, limit=None):
        self.goal = goals.set_goal(uuid.uuid4().hex,objective,authority=authority,limit=limit)
        return asdict(self.goal)

    def configure_mcp(self, server, command, *, allowed_tools=(), read_only_tools=(), cwd=None, timeout=5):
        """Host-only explicit configuration; does not start or discover by itself."""
        if not re.fullmatch(r'[a-z][a-z0-9_-]*',server):
            raise ValueError('invalid server identifier')
        with self.lock:
            if server in self.mcp:
                self.mcp[server].close()
            self.mcp[server] = StdioMCPClient(command,cwd=cwd,allowed_tools=allowed_tools,
                read_only_tools=read_only_tools,timeout=timeout)

    def install_plugin(self, source, *, approved=False, enabled=False):
        """Install inert metadata only; manifest commands never auto-start."""
        if not approved:
            raise PermissionError('explicit installation approval required')
        value = json.loads(Path(source).read_text())
        with self.lock:
            request = self.plugins.request_install(source,root_request=True,
                reason='Explicit host installation',candidates={value.get('plugin_id')})
            return complete(self.plugins.confirm_install(request,accepted=True,enabled=enabled))

    def create_worktree(self, *, base='HEAD', owner=None):
        """Host-only real local worktree creation in this service's state directory."""
        revision = worktrees.git(self.workspace,'rev-parse','--verify',base+'^{commit}').stdout.strip()
        target = self.state_dir/'worktrees'/uuid.uuid4().hex/self.workspace.name
        target.parent.mkdir(parents=True)
        worktrees.git(self.workspace,'worktree','add','--detach',str(target),revision)
        worktrees.bind(target,owner or 'local-'+uuid.uuid4().hex)
        return {'path':str(target),'revision':revision,'owner':worktrees.owner(target),'environment':'local'}

    def tool_specs(self):
        text = {'type':'string'}
        return (
            function('memory_read','Read explicitly saved historical memory; not current verification.',
                     {'query':text,'max_bytes':{'type':'integer'}}),
            function('skill_load','Read a configured local skill as attributed source data.',
                     {'locator':text,'resources':{'type':'array','items':text}},('locator',)),
            function('plan_update','Update visible plan only; does not create a Goal or start work.',
                     {'steps':{'type':'array','items':{'type':'object','properties':{'text':text,'status':text},
                       'required':['text','status'],'additionalProperties':False}}},('steps',)),
            function('agent_task','Run a bounded read-only inspection worker; no shell or model execution.',
                     {'paths':{'type':'array','items':text},'max_bytes':{'type':'integer'}},('paths',)),
            function('code_execute','Execute up to four allowlisted Python call-plan entries. Not JavaScript.',
                     {'calls':{'type':'array','items':{'type':'object','properties':{'name':text,
                         'arguments':{'type':'object'}},'required':['name','arguments'],
                         'additionalProperties':False}}},('calls',)),
            function('plugin_search','Search enabled installed metadata and load matching schemas.',{'query':text},('query',)),
            function('plugin_call','Call a plugin tool only through an explicitly configured MCP endpoint.',
                     {'name':text,'arguments':{'type':'object'}},('name','arguments')),
            function('mcp_list','List allowlisted tools on a host-configured MCP stdio endpoint.',{'server':text},('server',)),
            function('mcp_call','Call an allowlisted tool on a host-configured MCP stdio endpoint.',
                     {'server':text,'name':text,'arguments':{'type':'object'}},('server','name','arguments')),
            function('worktree_info','Inspect current local Git checkout and owner metadata; no mutation.',{}),
        )

    def _safe_path(self, raw):
        if not isinstance(raw,str) or not raw or Path(raw).is_absolute():
            raise PermissionError('relative workspace path required')
        parts = Path(raw).parts
        if any(part=='..' or (part.startswith('.') and part!='.') for part in parts):
            raise PermissionError('hidden or parent paths are outside worker scope')
        target = self.workspace
        for part in parts:
            target = target/part
            if target.is_symlink():
                raise PermissionError('worker symlink rejected')
        target = target.resolve(strict=True)
        if not target.is_relative_to(self.workspace) or target.is_relative_to(self.state_dir):
            raise PermissionError('worker path outside workspace scope')
        return target

    def _inspect(self, paths, budget, cancelled):
        result, used = [], 0
        for raw in paths:
            if cancelled.is_set():
                return response('cancelled',result,'worker_cancelled')
            path = self._safe_path(raw)
            if path.is_dir():
                names = sorted(p.name for p in path.iterdir()
                    if not p.name.startswith('.') and not p.is_symlink() and p.resolve()!=self.state_dir)[:128]
                data = json.dumps(names).encode()
                item = {'path':raw,'entries':names}
            else:
                if not path.is_file():
                    raise PermissionError('worker accepts regular files and directories only')
                with path.open('rb') as stream:
                    data = stream.read(budget-used+1)
                item = {'path':raw,'sha256':hashlib.sha256(data).hexdigest(),
                        'bytes':len(data),'preview':data[:2000].decode('utf-8',errors='replace')}
            if len(data) > budget-used:
                return response('failed',result,'worker_byte_budget_exhausted')
            if cancelled.is_set():
                return response('cancelled',result,'worker_cancelled')
            used += len(data)
            result.append(item)
        return response('succeeded',{'files':result,'bytes':used,'executor':'controlled-reader-thread'})

    def _agent_task(self, args, context):
        paths = args['paths']
        if not isinstance(paths,list) or not 1<=len(paths)<=16:
            raise ValueError('worker requires 1..16 paths')
        budget = int(args.get('max_bytes',16384))
        if not 1<=budget<=65536:
            raise ValueError('worker budget must be 1..65536 bytes')
        cancelled = self._cancel(context.run_id)
        with self.tree_lock:
            agent = self.tree.spawn('/root','reader_'+uuid.uuid4().hex[:10],{'read'})
            try:
                lease = self.tree.start(agent)
            except BaseException:
                self.tree.evict(agent)
                raise
        future = self.pool.submit(self._inspect,paths,budget,cancelled)
        def release(done):
            with self.tree_lock:
                self.tree.finish(lease,'cancelled' if cancelled.is_set() else 'returned')
                if self.tree.agents[agent].state=='idle':
                    self.tree.evict(agent)
        future.add_done_callback(release)
        while not future.done():
            if cancelled.is_set() or time.monotonic()>=context.deadline:
                cancelled.set()
                with self.tree_lock:
                    self.tree.cancel_subtree(agent)
                try:
                    result = future.result(timeout=0.2)
                except TimeoutError:
                    return response('unknown',{'agent':agent,'generation':lease.generation},
                                    'worker_cleanup_pending',cleanup='pending')
                break
            cancelled.wait(0.01)
        else:
            result = future.result()
        result['result'] = {'agent':agent,'generation':lease.generation,'report':result['result']}
        with self.lock:
            board = Board(self.board_path,lambda actor,seq:self.notifications.append((actor,seq)))
            try:
                seq = board.write(agent,context.call_id or uuid.uuid4().hex,
                    json.dumps(result,ensure_ascii=False),writable=True,operation='append')
                result['result']['board_seq'] = seq
            finally:
                board.close()
        return result

    def dispatch(self, name: str, args: dict, context: CallContext) -> dict:
        try:
            if self.closed:
                return response('failed',error='extension_services_closed')
            if not isinstance(args,dict):
                raise ValueError('tool arguments must be an object')
            if self._cancel(context.run_id).is_set():
                return response('cancelled',error='run_cancelled')
            if time.monotonic()>=context.deadline:
                return response('failed',error='deadline_before_dispatch')
            if name=='memory_read':
                return response('succeeded',self._memory(args.get('query',''),args.get('max_bytes',8192)))
            if name=='skill_load':
                catalog = self.skills.discover(context.call_id or context.run_id)
                entry = self.skills.select(catalog,locator=args['locator'])
                bundle = self.skills.load(catalog,entry,tuple(args.get('resources',())),
                                          cancelled=self._cancel(context.run_id).is_set)
                fragments = [asdict(f) for f in bundle.fragments]
                with self.lock:
                    self.selected_skills[entry.locator] = {'locator':entry.locator,'fragments':fragments}
                return response('succeeded',{'authority':'source-data','fragments':fragments})
            if name=='plan_update':
                steps = [(item['text'],item['status']) for item in args['steps']]
                with self.lock:
                    self.plan = goals.update_plan(self.plan,steps)
                    return response('succeeded',asdict(self.plan))
            if name=='agent_task':
                return self._agent_task(args,context)
            if name=='code_execute':
                calls = args['calls']
                allowed = {'memory_read','skill_load','agent_task','worktree_info','plugin_search'}
                if not isinstance(calls,list) or not 1<=len(calls)<=4:
                    raise ValueError('controlled plan requires 1..4 calls')
                if any(not isinstance(c,dict) or c.get('name') not in allowed or
                       not isinstance(c.get('arguments',{}),dict) for c in calls):
                    raise PermissionError('controlled plan contains unapproved operation')
                counter = iter(range(len(calls)))
                def nested(tool, arguments):
                    child = replace(context,call_id=(context.call_id or 'plan')+'/'+str(next(counter)))
                    return self.dispatch(tool,arguments,child)
                result = complete(execute_plan(calls,nested,self.code_state,context.call_id or 'plan'))
                outcomes = [r['outcome'] for r in result['results']]
                outcome = 'succeeded' if all(x=='succeeded' for x in outcomes) else 'unknown' if 'unknown' in outcomes else 'failed'
                return response(outcome,result,None if outcome=='succeeded' else 'partial_call_failure',
                                cleanup='pending' if any(r['cleanup_status']=='pending' for r in result['results']) else 'complete')
            if name=='plugin_search':
                with self.lock:
                    refs = self.catalog.search(args['query'])
                    return response('succeeded',[{'name':ref.name,'plugin_id':ref.plugin_id,
                        'schema':complete(self.catalog.load_schema(ref))} for ref in refs])
            if name=='plugin_call':
                with self.lock:
                    ref, tool = self.catalog.entries()[args['name']]
                    self.catalog.resolve_call(ref,surface='model',authorize=lambda ref:True)
                    server, remote = tool.get('mcp_server'),tool.get('mcp_tool')
                if server not in self.mcp or not remote:
                    raise PermissionError('plugin has no explicitly configured MCP binding')
                return self.mcp[server].call(remote,args['arguments'],deadline=context.deadline,
                                              cancel=self._cancel(context.run_id))
            if name in {'mcp_list','mcp_call'}:
                endpoint = self.mcp.get(args['server'])
                if endpoint is None:
                    raise PermissionError('MCP endpoint not explicitly configured')
                if name=='mcp_list':
                    return response('succeeded',endpoint.list_tools(deadline=context.deadline,
                                                   cancel=self._cancel(context.run_id)))
                return endpoint.call(args['name'],args['arguments'],deadline=context.deadline,
                                     cancel=self._cancel(context.run_id))
            if name=='worktree_info':
                head = worktrees.git(self.workspace,'rev-parse','HEAD').stdout.strip()
                return response('succeeded',{'environment':'local','cwd':str(self.workspace),
                    'revision':head,'owner':worktrees.owner(self.workspace)})
            return response('failed',error='unknown_extension_tool')
        except MCPError as exc:
            return response(exc.outcome,error=exc.code,effect=exc.side_effect_status)
        except (ValueError,KeyError,TypeError,OSError,RuntimeError,SkillError,StaleCatalog,Denied,subprocess.SubprocessError) as exc:
            return response('failed',error=f'{type(exc).__name__}: {exc}')

    def capabilities(self):
        return [
            {'chapter':'11','name':'memory','status':'integrated','detail':'explicit user memory in SQLite; lexical retrieval; no automatic extraction'},
            {'chapter':'12','name':'plans_goals','status':'integrated_partial','detail':'real Plan updates and host Goal state; no automatic Goal continuation'},
            {'chapter':'13','name':'skills','status':'integrated','detail':'configured local skill loading with source attribution; no cloud provider'},
            {'chapter':'14','name':'mcp','status':'configured' if self.mcp else 'available_unconfigured','detail':'real allowlisted JSON-RPC stdio; explicit host configuration only'},
            {'chapter':'15','name':'plugins','status':'integrated','detail':'inert manifests, visibility, schema loading and explicit MCP binding; no marketplace'},
            {'chapter':'16','name':'controlled_call_plans','status':'integrated','detail':'bounded Python scheduling with Cell lifecycle; no V8 or arbitrary code evaluation'},
            {'chapter':'17','name':'delegation','status':'integrated_partial','detail':'real bounded read-only threads, generation leases and SQLite Board; no child model'},
            {'chapter':'18','name':'app_server','status':'available','detail':'loopback/stdio RPC drives this same MiniCodex engine'},
            {'chapter':'19','name':'rpc_client','status':'available','detail':'real loopback SDK using framing/router components; no TUI'},
            {'chapter':'20','name':'worktrees','status':'integrated_partial','detail':'real local Git ownership/host creation; no remote executor'},
        ]

    def close(self):
        self.closed = True
        for event in self.cancelled.values():
            event.set()
        for endpoint in self.mcp.values():
            endpoint.close()
        self.pool.shutdown(wait=True,cancel_futures=True)
