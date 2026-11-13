"""Integration tests exercise extension components through their public service."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

from mini_codex.contracts import CallContext
from mini_codex.extensions import ExtensionServices
from mini_codex.components.environments.ownership_probe import git, owner


MCP_FIXTURE = '''import json,sys,time
from pathlib import Path
for line in sys.stdin:
    m=json.loads(line)
    method=m.get('method')
    if 'id' not in m: continue
    if method=='initialize': result={'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':'fixture','version':'1'}}
    elif method=='tools/list': result={'tools':[{'name':'echo','description':'fixture echo','inputSchema':{'type':'object'}},{'name':'write','inputSchema':{'type':'object'}}]}
    elif method=='tools/call':
        if m['params']['name']=='write':
            Path(sys.argv[1]).write_text('committed')
            time.sleep(2)
        result={'content':[{'type':'text','text':json.dumps(m['params']['arguments'])}]}
    else: result={}
    print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':result}),flush=True)
'''


class ExtensionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root/'workspace'
        self.workspace.mkdir()
        (self.workspace/'tasks.txt').write_text('status=open\n')
        self.services = ExtensionServices(self.workspace,self.root/'state')
        self.ctx = CallContext('run-1','turn-1','sample-1',time.monotonic()+10,'call-1')

    def tearDown(self):
        self.services.close()
        self.temp.cleanup()

    def dispatch(self,name,args,context=None):
        return self.services.dispatch(name,args,context or self.ctx)

    def test_memory_persists_and_context_is_source_data(self):
        self.services.remember('Task records use status.\n',key='field')
        self.assertEqual(self.dispatch('memory_read',{'query':'status'})['outcome'],'succeeded')
        context = json.loads(self.services.context('Inspect status'))
        self.assertEqual(context['kind'],'extension_reference_data')
        self.assertEqual(len(context['memories']),1)
        second = ExtensionServices(self.workspace,self.root/'state')
        try:
            self.assertIn('status',second.context('status'))
        finally:
            second.close()

    def test_skill_load_plan_and_controlled_plan_call_real_components(self):
        package = self.root/'state'/'skills'/'review'
        package.mkdir()
        (package/'SKILL.md').write_text('---\nname: Review\ndescription: Review status\n---\nCheck status.\n')
        loaded = self.dispatch('skill_load',{'locator':'skill://local/review'})
        self.assertEqual(loaded['outcome'],'succeeded')
        self.assertIn('skill://local/review',self.services.context('status'))
        plan = self.dispatch('plan_update',{'steps':[{'text':'read','status':'in_progress'}]})
        self.assertEqual(plan['result']['revision'],1)
        self.assertIsNone(self.services.goal)
        combined = self.dispatch('code_execute',{'calls':[
            {'name':'memory_read','arguments':{'query':''}},
            {'name':'agent_task','arguments':{'paths':['tasks.txt']}}]})
        self.assertEqual(combined['outcome'],'succeeded',combined)
        self.assertEqual(combined['result']['runtime'],'python-controlled-call-plan')
        self.assertIn('last_results',self.services.code_state)
        denied = self.dispatch('code_execute',{'calls':[{'name':'exec','arguments':{'command':'false'}}]})
        self.assertEqual(denied['outcome'],'failed')

    def test_worker_real_thread_budget_and_no_path_escape(self):
        report = self.dispatch('agent_task',{'paths':['tasks.txt'],'max_bytes':128})
        self.assertEqual(report['outcome'],'succeeded',report)
        result = report['result']
        self.assertEqual(result['generation'],1)
        self.assertIn('status=open',result['report']['files'][0]['preview'])
        self.assertEqual(result['report']['executor'],'controlled-reader-thread')
        self.assertEqual(result['board_seq'],1)
        exhausted = self.dispatch('agent_task',{'paths':['tasks.txt'],'max_bytes':2})
        self.assertEqual(exhausted['error'],'worker_byte_budget_exhausted')
        denied = self.dispatch('agent_task',{'paths':['../outside']})
        self.assertEqual(denied['outcome'],'failed')
        hidden = self.dispatch('agent_task',{'paths':['.env']})
        self.assertEqual(hidden['outcome'],'failed')
        self.assertFalse(self.services.tree.leases)

    def test_controlled_plan_works_inside_existing_notebook_event_loop(self):
        async def notebook_cell():
            return self.dispatch('code_execute',{'calls':[
                {'name':'agent_task','arguments':{'paths':['tasks.txt']}}]})
        result = asyncio.run(notebook_cell())
        self.assertEqual(result['outcome'],'succeeded',result)
        self.assertEqual(result['result']['results'][0]['result']['report']['bytes'],12)

    def test_worker_rejects_fifo_without_opening_it(self):
        import os
        if not hasattr(os,'mkfifo'):
            self.skipTest('POSIX FIFO fixture')
        os.mkfifo(self.workspace/'blocked-pipe')
        result = self.dispatch('agent_task',{'paths':['blocked-pipe']})
        self.assertEqual(result['outcome'],'failed')
        self.assertIn('regular files',result['error'])
        self.assertFalse(self.services.tree.leases)

    def test_running_worker_cancellation_releases_lease_after_ack(self):
        entered = threading.Event()
        original = self.services._inspect
        def controlled(paths,budget,cancelled):
            entered.set()
            cancelled.wait(2)
            return original(paths,budget,cancelled)
        self.services._inspect = controlled
        result = []
        thread = threading.Thread(target=lambda:result.append(self.dispatch('agent_task',{'paths':['tasks.txt']})))
        thread.start()
        self.assertTrue(entered.wait(1))
        self.assertEqual(len(self.services.tree.leases),1)
        self.services.cancel(self.ctx.run_id)
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0]['outcome'],'cancelled')
        self.assertFalse(self.services.tree.leases)

    def test_plugin_to_real_stdio_mcp_and_timeout_preserves_unknown_effect(self):
        fixture = self.root/'mcp_fixture.py'
        fixture.write_text(MCP_FIXTURE)
        effect = self.root/'effect.txt'
        command = [sys.executable,'-I',str(fixture),str(effect)]
        unconfigured = self.dispatch('mcp_list',{'server':'fixture'})
        self.assertEqual(unconfigured['outcome'],'failed')
        self.assertFalse(effect.exists())
        self.services.configure_mcp('fixture',command,allowed_tools={'echo','write'},read_only_tools={'echo'})
        listed = self.dispatch('mcp_list',{'server':'fixture'})
        self.assertEqual(listed['outcome'],'succeeded',listed)
        manifest = {'plugin_id':'fixture-plugin','version':'1','environment_id':'local','tools':[
            {'name':'fixture.echo','description':'echo task status','exposure':'Deferred',
             'schema':{'type':'object'},'mcp_server':'fixture','mcp_tool':'echo'}]}
        source = self.root/'plugin.json'
        source.write_text(json.dumps(manifest))
        self.services.install_plugin(source,approved=True,enabled=True)
        found = self.dispatch('plugin_search',{'query':'status'})
        self.assertEqual(found['result'][0]['name'],'fixture.echo')
        called = self.dispatch('plugin_call',{'name':'fixture.echo','arguments':{'status':'open'}})
        self.assertEqual(called['outcome'],'succeeded',called)
        self.assertIn('open',called['result']['content'][0]['text'])
        late = replace(self.ctx,deadline=time.monotonic()+0.15,call_id='write')
        uncertain = self.dispatch('mcp_call',{'server':'fixture','name':'write','arguments':{}},late)
        self.assertEqual(uncertain['outcome'],'unknown',uncertain)
        self.assertEqual(uncertain['side_effect_status'],'unknown')
        self.assertEqual(effect.read_text(),'committed')
        self.assertIsNotNone(self.services.mcp['fixture'].process.poll())

    def test_host_worktree_creation_and_model_inspection_use_real_git(self):
        git(self.workspace,'init','-q')
        git(self.workspace,'add','tasks.txt')
        git(self.workspace,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid',
            '-c','commit.gpgsign=false','commit','-qm','baseline')
        info = self.dispatch('worktree_info',{})
        self.assertEqual(info['outcome'],'succeeded',info)
        created = self.services.create_worktree(owner='test-thread')
        target = Path(created['path'])
        self.assertEqual(owner(target),'test-thread')
        self.assertEqual(created['revision'],info['result']['revision'])
        self.assertEqual((target/'tasks.txt').read_text(),'status=open\n')
        self.assertEqual(git(self.workspace,'status','--porcelain').stdout,'')


if __name__=='__main__':
    unittest.main()
