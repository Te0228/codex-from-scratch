"""Deterministic integration tests; FakeWorker never supplies OS isolation evidence."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from mini_codex.contracts import ExecutionObservation
from mini_codex.engine import AgentConfig, CommandVerifier, MiniCodex
from mini_codex.history import History
from mini_codex.model import function_call, message
from mini_codex.loop import Loop
from mini_codex.components.context.projection import ContextProjector, ContextBudgetExceeded
from mini_codex.worker import CHECKPOINT, SandboxUnavailable


class FakeWorker:
    """Controlled real-file fixture, explicitly no OS containment."""
    def __init__(self, workspace, read_only=False):
        self.workspace = Path(workspace).resolve()
        self.read_only, self.clean, self.executions = read_only, True, 0
        self.last_cleanup = {'fixture': True}

    def execute(self, action, limits, cancel=None):
        self.executions += 1
        if cancel and cancel.cancelled:
            return ExecutionObservation(None, '', False, 'cancelled', 'none', 'confirmed', 'cancelled')
        if action.operation == 'read':
            return ExecutionObservation(0, (self.workspace/action.arguments['path']).read_text(), False, 'exited', 'none', 'confirmed')
        if action.operation == 'apply_patch':
            assert not self.read_only
            target = self.workspace/action.arguments['path']
            text = target.read_text()
            if text.count(action.arguments['old']) != 1:
                return ExecutionObservation(1,'mismatch',False,'exited','none','confirmed')
            target.write_text(text.replace(action.arguments['old'],action.arguments['new'],1))
            return ExecutionObservation(0,'patched',False,'exited','confirmed','confirmed')
        if action.operation == 'exec':
            return ExecutionObservation(0,'fixture exit',False,'exited','none','confirmed')
        raise ValueError('unsupported fixture operation')


class FileVerifier:
    def check(self, workspace, deadline):
        return {'passed':(Path(workspace)/'task.py').read_text()=='good','cleanup_status':'confirmed','evidence_kind':'test_fixture_content'}
    def freeze_and_check(self, workspace, destination, deadline):
        import shutil
        shutil.copytree(workspace,destination)
        return self.check(destination,deadline)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.workspace=self.root/'workspace';self.workspace.mkdir()
        (self.workspace/'task.py').write_text('bad');self.state=self.root/'state';self.agents=[]
    def agent(self, **options):
        result=MiniCodex(AgentConfig(self.workspace,self.state,**options));self.agents.append(result);return result
    def tearDown(self):
        for agent in reversed(self.agents):agent.close()

    def test_text_without_verification_never_passes_and_is_committed(self):
        agent=self.agent();result=agent.run('Inspect',[[message('All tests passed!')]])
        self.assertEqual(result.task_status,'needs_review');self.assertEqual(result.stop_reason,'verification_required')
        self.assertTrue((agent.run_dir/'services.json').is_file())
        recovered=History('observer',journal_path=self.state/'session.sqlite')
        try:
            self.assertEqual(recovered.events[-1]['kind'],'scheduler_terminal')
            self.assertIn(result.run_id,{e['run_id'] for e in recovered.events})
        finally:recovered.close()
        self.assertEqual(agent.events(after_event=agent.events()[-1]['sequence']),[])
        self.assertFalse(agent.cancel())

    def test_same_loop_writes_verifies_and_projects_observation(self):
        agent=self.agent();responses=[[function_call('read','read',path='task.py')],
            [function_call('patch','apply_patch',path='task.py',old='bad',new='good')],[message('Changed')]]
        with patch('mini_codex.engine.LinuxWorker',FakeWorker):result=agent.run('Fix file',responses,FileVerifier())
        self.assertEqual(result.task_status,'passed');self.assertEqual((self.workspace/'task.py').read_text(),'good')
        self.assertEqual(agent.last_dispatcher.registry.handler_entries,2);self.assertEqual(agent.history.unresolved_actions(),[])
        request=agent.model.requests[-1]
        self.assertTrue(any(i.get('call_id')=='patch' and i.get('type')=='function_call_output' for i in request.messages))
        kinds=[e['kind'] for e in agent.events()]
        self.assertEqual(kinds.count('context_projection'),3);self.assertEqual(kinds.count('tool_intent'),2)
        self.assertEqual(kinds.count('tool_result'),2);self.assertNotIn('task_list.py',request.instructions)
        self.assertEqual(request.model_options['mode'],'replay');self.assertEqual(request.model_options['provider'],'replay')

    def test_extension_uses_same_dispatch_and_does_not_duplicate_side_effect(self):
        agent=self.agent();call=function_call('plan','plan_update',steps=[{'text':'Read','status':'pending'}])
        result=agent.run('Prepare plan',[[call],[call],[message('Ready')]])
        self.assertEqual(result.task_status,'needs_review');self.assertEqual(agent.plan.revision,1)
        self.assertEqual(agent.last_dispatcher.registry.handler_entries,1)
        observations=[e['payload'] for e in agent.events() if e['kind']=='tool_observation']
        self.assertEqual(observations[1]['error_code'],'duplicate_call')
        self.assertTrue(any(e['kind']=='tool_intent' and e['payload']['action']['operation']=='plan_update' for e in agent.events()))

    def test_schema_failure_never_enters_core_or_extension_handler(self):
        agent=self.agent();responses=[[function_call('bad1','read',path=123)],
            [function_call('bad2','plan_update',steps=[{'text':'oops','status':True}])],[message('Done')]]
        with patch('mini_codex.engine.LinuxWorker',FakeWorker):result=agent.run('Schema',responses)
        self.assertEqual(result.task_status,'needs_review');self.assertEqual(agent.last_dispatcher.registry.handler_entries,0)
        self.assertEqual(agent.plan.revision,0);self.assertEqual(agent.history.unresolved_actions(),[])

    def test_missing_backend_is_blocked_without_fallback(self):
        class Missing(FakeWorker):
            def execute(self,*args,**kwargs):raise SandboxUnavailable('fixture missing backend')
        agent=self.agent()
        with patch('mini_codex.engine.LinuxWorker',Missing):result=agent.run('Read',[[function_call('r','read',path='task.py')]])
        self.assertEqual(result.task_status,'blocked');self.assertEqual(result.stop_reason,'sandbox_unavailable')
        self.assertEqual(agent.history.unresolved_actions(),[]);self.assertEqual((self.workspace/'task.py').read_text(),'bad')

    def test_unknown_write_is_not_repeated_on_recovery(self):
        class Lost(FakeWorker):
            def execute(self,action,limits,cancel=None):
                super().execute(action,limits,cancel);raise ConnectionError('injected after real file write')
        first=self.agent();call=function_call('patch','apply_patch',path='task.py',old='bad',new='good')
        with patch('mini_codex.engine.LinuxWorker',Lost):result=first.run('Patch',[[call]])
        self.assertEqual(result.task_status,'unknown');self.assertEqual((self.workspace/'task.py').read_text(),'good')
        self.assertEqual(len(first.history.unresolved_actions()),1);first.close();recovered=self.agent()
        with patch('mini_codex.engine.ReplayModel') as model:report=recovered.run('Continue',[[call]])
        model.assert_not_called();self.assertEqual(report.task_status,'needs_review')
        self.assertEqual(report.stop_reason,'unresolved_prior_actions');self.assertEqual((self.workspace/'task.py').read_text(),'good')

    def test_budget_failure_prevents_model_call(self):
        agent=self.agent(context_budget=20);result=agent.run('Mandatory input',[[message('Passed')]])
        self.assertEqual(result.task_status,'failed');self.assertEqual(result.stop_reason,'context_budget')
        self.assertEqual(agent.model.requests,[]);self.assertTrue(any(e['kind']=='context_rejected' for e in agent.events()))

    def test_cancel_propagates_real_token_and_commits_one_terminal(self):
        entered=threading.Event()
        class Waiting(FakeWorker):
            def execute(self,action,limits,cancel=None):
                entered.set();until=time.monotonic()+3
                while not cancel.cancelled and time.monotonic()<until:time.sleep(.005)
                assert cancel.cancelled,'cancel token missing'
                return ExecutionObservation(None,'',False,'cancelled','none','confirmed','cancelled')
        agent=self.agent();results=[];errors=[]
        def background():
            try:results.append(agent.run('Wait',[[function_call('r','read',path='task.py')]]))
            except BaseException as error:errors.append(error)
        with patch('mini_codex.engine.LinuxWorker',Waiting):
            thread=threading.Thread(target=background);thread.start();self.assertTrue(entered.wait(2))
            self.assertTrue(agent.cancel());self.assertTrue(agent.events());thread.join(4)
        self.assertFalse(thread.is_alive());self.assertEqual(errors,[]);self.assertEqual(results[0].task_status,'cancelled')
        self.assertEqual(sum(e['kind']=='run_stopped' for e in agent.events()),1)
        self.assertEqual(sum(e['kind']=='scheduler_terminal' for e in agent.events()),1)

    def test_run_directory_cannot_overwrite_previous_evidence(self):
        directory=self.root/'artifacts';directory.mkdir();(directory/'baseline.json').write_text('{}')
        agent=self.agent(run_dir=directory);agent.run('Inspect',[[message('Done')]])
        self.assertEqual(agent.run_dir,directory.resolve())
        with self.assertRaises(FileExistsError):agent.run('Another',[[message('Done')]])

    def test_workspace_state_and_trusted_code_boundaries(self):
        with self.assertRaisesRegex(ValueError,'state_dir'):MiniCodex(AgentConfig(self.workspace,self.workspace/'state'))
        with self.assertRaisesRegex(ValueError,'run_dir'):MiniCodex(AgentConfig(self.workspace,self.state,run_dir=self.workspace/'runs'))
        for workspace in (CHECKPOINT,CHECKPOINT.parent,CHECKPOINT/'mini_codex'):
            with self.assertRaisesRegex(ValueError,'trusted code'):MiniCodex(AgentConfig(workspace,self.state))

    def test_command_verification_does_not_claim_protected_assertions(self):
        with patch('mini_codex.engine.LinuxWorker',FakeWorker):result=CommandVerifier('explicit command').check(self.workspace,time.monotonic()+2)
        self.assertTrue(result['passed']);self.assertEqual(result['evidence_kind'],'user_command_exit')
        self.assertFalse(result['independent_assertions_verified'])

    def test_final_verifier_exception_does_not_publish_second_stop(self):
        class BrokenFinal(FileVerifier):
            def check(self, workspace, deadline):
                return {'passed':True,'cleanup_status':'confirmed'}
            def freeze_and_check(self, workspace, destination, deadline):
                raise RuntimeError('injected final verification failure')
        agent=self.agent()
        result=agent.run('Inspect',[[message('Ready')]],BrokenFinal())
        self.assertEqual(result.task_status,'failed')
        self.assertEqual(sum(e['kind']=='run_stopped' for e in agent.events()),1)
        self.assertEqual(sum(e['kind']=='run_result' for e in agent.events()),1)

    def test_cancel_after_committed_stop_cannot_change_frozen_verification_terminal(self):
        entered,release=threading.Event(),threading.Event()
        class FrozenCheck(FileVerifier):
            def check(self,workspace,deadline):return {'passed':True,'cleanup_status':'confirmed'}
            def freeze_and_check(self,workspace,destination,deadline):
                entered.set()
                if not release.wait(3):raise AssertionError('test barrier did not release')
                return {'passed':True,'cleanup_status':'confirmed'}
        agent=self.agent();results=[];errors=[]
        def run():
            try:results.append(agent.run('Inspect',[[message('Ready')]],FrozenCheck()))
            except BaseException as error:errors.append(error)
        thread=threading.Thread(target=run);thread.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertTrue(any(e['kind']=='run_stopped' for e in agent.events()))
            self.assertFalse(agent.cancel())
        finally:
            release.set();thread.join(4)
        self.assertFalse(thread.is_alive());self.assertEqual(errors,[])
        self.assertEqual(results[0].task_status,'passed')
        terminal=[e for e in agent.events() if e['kind']=='scheduler_terminal']
        self.assertEqual(terminal[0]['payload']['status'],'completed')

    def test_cancel_wins_lock_after_candidate_check_before_stop_commit(self):
        class Ready(FileVerifier):
            def check(self,workspace,deadline):return {'passed':True,'cleanup_status':'confirmed'}
            def freeze_and_check(self,workspace,destination,deadline):
                return {'passed':True,'cleanup_status':'confirmed'}
        agent=self.agent();accepted=[];original_stop=Loop.stop
        def interleave(loop,reason):
            if reason=='candidate_ready':
                # Deterministically insert cancellation in the former check/stop gap.
                accepted.append(agent.cancel())
            return original_stop(loop,reason)
        with patch.object(Loop,'stop',interleave):
            result=agent.run('Inspect',[[message('Ready')]],Ready())
        self.assertEqual(accepted,[True])
        self.assertEqual(result.task_status,'cancelled')
        self.assertEqual(result.stop_reason,'cancelled')
        terminal=[e for e in agent.events() if e['kind']=='scheduler_terminal']
        self.assertEqual(terminal[0]['payload']['status'],'cancelled')
        stops=[e for e in agent.events() if e['kind']=='run_stopped']
        self.assertEqual(len(stops),1);self.assertEqual(stops[0]['payload']['reason'],'cancelled')


class ProjectionTests(unittest.TestCase):
    def test_preserves_task_and_whole_pairs_without_mutating_raw_history(self):
        history=History('run');history.append('task_input',{},[{'role':'user','content':'Keep API unchanged'}])
        history.append('model_items',{},[function_call('x','read',path='task.py')])
        history.append('tool_observation',{},[{'type':'function_call_output','call_id':'x','output':'z'*2000}])
        history.append('model_items',{},[function_call('y','read',path='small.py')])
        history.append('tool_observation',{},[{'type':'function_call_output','call_id':'y','output':'small result'}])
        before=history.snapshot();small=ContextProjector(600).project(before,'system',())
        self.assertEqual(small.raw_items[0],{'role':'user','content':'Keep API unchanged'})
        self.assertEqual([i.get('call_id') for i in small.raw_items[1:]],['y','y'])
        self.assertEqual(history.snapshot(),before)
        self.assertEqual(len(ContextProjector(4000).project(before,'system',()).raw_items),5)

    def test_latest_completed_tool_group_must_fit_or_sampling_stops(self):
        history=History('run');history.append('task_input',{},[{'role':'user','content':'Task'}])
        history.append('model_items',{},[function_call('write','apply_patch',path='x',old='a',new='b')])
        history.append('tool_observation',{},[{'type':'function_call_output','call_id':'write','output':'evidence'*200}])
        before=history.snapshot()
        with self.assertRaisesRegex(ContextBudgetExceeded,'mandatory'):
            ContextProjector(300).project(before,'system',())
        self.assertEqual(history.snapshot(),before)
        self.assertEqual(len(ContextProjector(4000).project(before,'system',()).raw_items),3)
    def test_orphan_rejected_and_pending_call_mandatory(self):
        history=History('run');history.append('tool_observation',{},[{'type':'function_call_output','call_id':'x','output':'x'}])
        with self.assertRaisesRegex(ContextBudgetExceeded,'orphan'):ContextProjector(1000).project(history.snapshot(),'system',())
        pending=History('run');pending.append('model_items',{},[function_call('x','read',path='long'*100)])
        with self.assertRaisesRegex(ContextBudgetExceeded,'mandatory'):ContextProjector(100).project(pending.snapshot(),'system',())

if __name__=='__main__':unittest.main()
