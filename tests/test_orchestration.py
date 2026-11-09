import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from conductor.cli import initialize
from conductor.orchestration import begin_scope, managed_agents
from conductor.runtime import AgentLoop, Scheduler


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = initialize(self.root/'run', self.root, 'Build a verified result',
                                ui=True, scope='filesystem', allow_exec=True, transport='process')
        begin_scope(self.store)
        self.store.update_agent('coordinator', state='running')
        self.scheduler = Scheduler(self.store, self.root/'unused.sock')
        self.manager = AgentLoop(self.store, None, 'coordinator', self.scheduler)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def dispatch(self, ident, **kw):
        with patch.object(self.scheduler, 'tick'):
            return self.manager.execute('dispatch', {'id':ident,'task':'Produce owned result',
                'ownership':[ident+'.txt'], 'acceptance':['Result passes verification'], **kw})

    def submit(self, ident):
        self.store.update_agent(ident, cwd=str(self.root), state='running')
        AgentLoop(self.store, None, ident).finish({'summary':'Result submitted', 'findings':[], 'outcome':'done'})

    def test_failed_verification_blocks_dependency_and_final(self):
        self.dispatch('first', verification='exit 7')
        self.dispatch('dependent', dependencies=['first'])
        self.submit('first')
        self.assertEqual(self.store.agent('first')['state'], 'review')
        with patch('conductor.runtime.subprocess.Popen') as spawn:
            self.scheduler.tick()
            spawn.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'Workers are still active'):
            self.manager.finish({'summary':'Premature', 'findings':[], 'outcome':'done'})
        result = self.manager.execute('review_task', {'agent':'first', 'accepted':True,
            'summary':'Verify submitted work', 'evidence':['first result']})
        self.assertFalse(result['accepted'])
        self.assertEqual(result['verification']['exit_code'],7)
        self.assertEqual(self.store.agent('dependent')['state'],'blocked')

    def test_successful_review_unlocks_and_corrective_task_supersedes_failure(self):
        self.dispatch('failed')
        self.store.update_agent('failed',state='failed',result='Preserve partial work')
        self.dispatch('repair', supersedes='failed', verification='test -f verified.txt')
        self.submit('repair')
        (self.root/'verified.txt').write_text('verified')
        result = self.manager.execute('review_task', {'agent':'repair','accepted':True,
            'summary':'Artifact exists', 'evidence':['verified.txt']})
        self.assertTrue(result['accepted'])
        self.assertEqual([a['id'] for a in managed_agents(self.store)],['repair'])
        self.manager.finish({'summary':'Verified result', 'findings':[], 'outcome':'done'})
        self.assertEqual(self.store.agent('coordinator')['state'],'done')
        self.assertEqual(self.store.agent('failed')['state'],'failed')

    def test_followup_retains_scope_and_review_unlocks_dependency(self):
        original = self.store.config()['orchestration_scope']
        self.dispatch('first')
        self.dispatch('dependent', dependencies=['first'])
        self.submit('first')
        self.store.event('user', '*', 'instruction', 'Continue and verify the work')
        self.assertEqual(begin_scope(self.store), original)
        with patch.object(self.scheduler, 'tick') as tick:
            self.manager.execute('review_task', {'agent': 'first', 'accepted': True,
                'summary': 'Evidence verified', 'evidence': ['first.txt']})
            tick.assert_called_once()
        self.assertEqual(self.store.agent('first')['state'], 'done')
        self.assertEqual({a['id'] for a in managed_agents(self.store)}, {'first', 'dependent'})
        self.store.update_agent('dependent', state='done')
        self.assertNotEqual(begin_scope(self.store), original)
        self.assertEqual(managed_agents(self.store), [])

    def test_legacy_mismatched_scope_remains_reviewable(self):
        self.dispatch('old')
        self.submit('old')
        self.store.configure(orchestration_scope='legacy-followup')
        self.assertEqual([a['id'] for a in managed_agents(self.store)], ['old'])
        self.manager.execute('review_task', {'agent': 'old', 'accepted': True,
            'summary': 'Verified original work', 'evidence': ['old.txt']})
        self.assertEqual(self.store.agent('old')['state'], 'done')

    def test_desktop_delegation_requires_contract_and_manager_can_verify(self):
        with self.assertRaisesRegex(ValueError,'ownership'):
            self.manager.execute('dispatch',{'id':'unscoped','task':'Do work'})
        self.assertIn('exec',self.manager.allowed)

    def test_cancelled_scheduler_never_launches_pending_work(self):
        self.dispatch('pending')
        self.store.configure(cancel_requested=True)
        with patch('conductor.runtime.subprocess.Popen') as spawn:
            self.scheduler.tick()
            spawn.assert_not_called()
