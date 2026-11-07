import json
from pathlib import Path
import tempfile
import unittest

from conductor.pi_monitor import status_projection
from conductor.pi_sessions import PiSessions, _task_preview


class PiStatusTests(unittest.TestCase):
    def test_delegated_task_previews_hide_transport_envelopes(self):
        task = 'Generate the requested evaluation cases'
        envelope = '[Task delegated by the user’s Codex orchestrator through Conductor]\n' + json.dumps({'task_id':'x','task':task}) + '\nInstructions'
        self.assertEqual(_task_preview(envelope), task)
        self.assertEqual(_task_preview('Check the release status'), 'Check the release status')
    def session(self, status='idle', events=()):
        return {'last_prompt':'Build requested fixtures', 'harness':{'status':status,
            'observed_at':100, 'active_tools':[]}, 'recent_activity':list(events)}

    def test_live_tool_idle_and_lost_connection_are_distinct(self):
        session = self.session('working')
        session['harness']['active_tools'] = [{'name':'bash', 'started_at':80}]
        running = status_projection(session)
        self.assertEqual(running['activity'], 'Running bash')
        self.assertEqual(running['state'], 'working')
        idle = status_projection(self.session())
        self.assertEqual(idle['state'], 'idle')
        self.assertIn('not proof', idle['reason'])
        lost = status_projection(self.session('recorded'), {'status':'working','observed_at':80})
        self.assertEqual(lost['state'], 'attention')
        self.assertEqual(lost['heartbeat_at'], 80)
        self.assertIn('may still be running', lost['reason'])
        self.assertEqual(status_projection(self.session('recorded'), {'status':'closed'})['state'], 'saved')

    def test_errors_are_evidence_not_a_permanent_failed_label(self):
        failed = {'id':'failed', 'role':'toolResult','is_error':True,'text':'Validation failed'}
        session = self.session(events=[failed])
        self.assertEqual(status_projection(session)['state'], 'attention')
        session['harness']['status'] = 'working'
        self.assertEqual(status_projection(session)['state'], 'working')
        self.assertEqual(status_projection(session)['error_event_id'], 'failed')
        session['harness']['status'] = 'idle'
        session['recent_activity'].append({'role':'toolResult','is_error':False,'text':'Retry passed'})
        self.assertEqual(status_projection(session)['latest_error'], '')
        session['recent_activity'] = [failed, {'role':'user','text':'New task'}]
        self.assertEqual(status_projection(session)['state'], 'idle')
        self.assertEqual(status_projection(session)['latest_error'], '')

    def test_summary_uses_active_branch_and_only_public_bounded_content(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            entries = [
                {'type':'session','version':3,'id':'s','cwd':'/fixture'},
                {'type':'message','id':'u','parentId':None,'message':{'role':'user','content':'task'}},
                {'type':'message','id':'good','parentId':'u','message':{'role':'assistant','content':[
                    {'type':'thinking','thinking':'NEVER_PUBLIC'}, {'type':'text','text':'Progress '*200}]}},
                {'type':'message','id':'other','parentId':'u','message':{'role':'toolResult','isError':True,'content':'Different branch failed'}},
            ]
            (root/'test.jsonl').write_text('\n'.join(json.dumps(e) for e in entries)+'\n')
            result = PiSessions(root).list_sessions(leaf_ids={'s':'good'})['sessions'][0]
            self.assertEqual([e['id'] for e in result['recent_activity']], ['u','good'])
            self.assertNotIn('NEVER_PUBLIC', json.dumps(result))
            self.assertLessEqual(len(result['recent_activity'][-1]['text']),400)
            self.assertFalse(result['recent_activity'][-1]['is_error'])
            missing = PiSessions(root).list_sessions(leaf_ids={'s':'not-flushed'})['sessions'][0]
            missing['harness'] = {'status':'idle','observed_at':100}
            status = status_projection(missing)
            self.assertEqual(status['state'], 'idle')
            self.assertEqual(status['latest_error'], '')
            self.assertEqual(status['latest_update'], '')
            self.assertIsNone(status['activity_at'])
            self.assertIn('unavailable', status['task'])


if __name__ == '__main__':
    unittest.main()
