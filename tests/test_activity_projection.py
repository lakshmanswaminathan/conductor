"""Compact UI projection retains result diagnostics without duplicate prose."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from conductor.cli import initialize
from conductor.ui_server import App
from conductor.runtime import AgentLoop

class ActivityProjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = App(Path(self.tmp.name)/'runs')
        self.store = initialize(self.app.root/'chat-test',Path(self.tmp.name),'Task',scope='filesystem',ui=True)
    def tearDown(self):
        self.store.close()
        self.app.close()
        self.tmp.cleanup()
    def call(self, ident, name='pi_context', args=None):
        self.store.append('coordinator',{'role':'assistant','tool_calls':[{'id':ident,'function':{'name':name,'arguments':json.dumps(args or {})}}]})
    def result(self, ident, output):
        self.store.append('coordinator',{'role':'tool','tool_call_id':ident,'content':json.dumps(output)})
    def messages(self):
        return self.app.snapshot('chat-test')['messages']
    def test_results_update_one_activity_and_redact_details(self):
        self.call('one',args={'session_id':'peer','api_key':'private-value'})
        self.result('one',{'error':'Cannot read','detail':'sk-abcdefghijk'})
        rows = [m for m in self.messages() if m['kind']=='tool']
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['status'],'failed')
        self.assertEqual(rows[0]['tool_call_id'],'one')
        self.assertEqual(rows[0]['label'],'Read Pi context · peer')
        self.assertGreaterEqual(rows[0]['duration_ms'],0)
        self.assertNotIn('private-value',rows[0]['content'])
        self.assertNotIn('sk-abcdefghijk',rows[0]['content'])
        self.assertIn('Cannot read',rows[0]['content'])
        self.assertIn('private-value',json.dumps(self.store.history('coordinator')))
    def test_unresolved_activity_reflects_live_job_only(self):
        self.call('one')
        with patch.object(self.app,'is_running',return_value=True):
            self.assertEqual(self.messages()[-1]['status'],'running')
        self.assertEqual(self.messages()[-1]['status'],'interrupted')
    def test_archived_command_failure_keeps_outcome(self):
        self.call('large', 'exec', {'command':'synthetic-failure'})
        loop = AgentLoop(self.store, None, 'coordinator')
        output = loop.output({'exit_code':1, 'output':'x'*10000})
        self.store.append('coordinator', {'role':'tool','tool_call_id':'large','content':output})
        self.assertEqual(self.messages()[-1]['status'],'failed')
        self.assertIn('artifact',json.loads(output))
    def test_success_and_finish_evidence_are_separate(self):
        self.call('one')
        self.result('one',{'value':'x'*10000})
        self.call('done','finish',{'summary':'One concise summary.'})
        self.result('done',{'summary':'One concise summary.','findings':[{'value':'Repeated detail','evidence':['source:42']}],'evidence':['source:43']})
        rows = self.messages()
        tools = [m for m in rows if m['kind']=='tool']
        self.assertEqual(len(tools),1)
        self.assertEqual(tools[0]['status'],'completed')
        self.assertLessEqual(len(tools[0]['content']),6000)
        self.assertEqual(rows[-1]['content'],'One concise summary.')
        self.assertEqual(rows[-1]['findings'][0]['evidence'],['source:42'])
        self.assertEqual(rows[-1]['evidence'],['source:43'])
    def test_archived_finish_preserves_accepted_findings(self):
        result = {'summary':'Accepted summary', 'findings':[{'value':'x'*3500,'evidence':['source:1']},{'value':'y'*3500,'evidence':['source:2']}]}
        self.call('done','finish',result)
        output = AgentLoop(self.store,None,'coordinator').output(result)
        self.store.append('coordinator',{'role':'tool','tool_call_id':'done','content':output})
        row = self.messages()[-1]
        self.assertEqual(row['kind'],'text')
        self.assertEqual(row['content'],result['summary'])
        self.assertEqual(row['findings'],result['findings'])
