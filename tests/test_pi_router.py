import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from conductor import pi_router
from conductor.swarm import SwarmBroker


class PiRouterTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)/'swarm'
        self.root.mkdir()
        self.monitor=Mock()
        self.monitor.bridges.return_value={'pi-a':{'instance_id':'instance-a'}}
        self.monitor.control_path.return_value='/fixture/observer.sock'
        self.monitor.command.return_value={'status':'accepted','delivery':'follow_up'}
        self.patcher=patch('conductor.pi_router.monitor',self.monitor);self.patcher.start()
        self.args={'request_id':'request-a','session_id':'pi-a','instance_id':'instance-a',
                   'operation':'environment','task':'Check the assigned environment through its supported API',
                   'ownership':['environment fixture only'],'acceptance':['Return evidence for the exact environment']}

    def tearDown(self):
        self.patcher.stop();self.tmp.cleanup()

    def test_dispatch_result_relay_and_review_are_separate(self):
        first=pi_router.dispatch(self.root,self.args)
        self.assertEqual(pi_router.dispatch(self.root,self.args),first)
        self.monitor.command.assert_called_once()
        self.assertEqual(self.monitor.command.call_args.args[3],'follow_up')
        self.assertFalse(first['complete'])
        broker=SwarmBroker(self.root,self.monitor)
        broker.call('pi-a','swarm_task',{'action':'update','task_id':first['task_id'],'status':'completed',
                                       'result':'Verified environment fixture; evidence fixture/report-1','request_id':'report-1'})
        broker.close()
        self.assertFalse(pi_router.result(self.root,'request-a')['complete'])
        thread='12345678-1234-4234-8234-123456789abc'
        (self.root/'codex-target.json').write_text(json.dumps({'thread_id':thread,'binary':sys.executable}))
        success=subprocess.CompletedProcess([],0,f'Queued message 12345678-1234-4234-8234-123456789abd for thread {thread}','')
        with patch('conductor.codex_inbox.subprocess.run',return_value=success) as queue:
            pi_router.relay_results(self.root);pi_router.relay_results(self.root)
            queue.assert_called_once()
        reviewed=pi_router.review(self.root,{'request_id':'request-a','accepted':True,'evidence':['fixture/report-1']})
        self.assertTrue(reviewed['complete'])

    def test_unknown_delivery_reconciles_same_instance_and_stale_instance_fails_closed(self):
        self.monitor.command.return_value={'status':'unknown'}
        self.assertEqual(pi_router.dispatch(self.root,self.args)['delivery']['status'],'unknown')
        first_call=self.monitor.command.call_args
        self.monitor.command.return_value={'status':'accepted'}
        self.assertEqual(pi_router.dispatch(self.root,self.args)['delivery']['status'],'unknown')
        self.assertEqual(self.monitor.command.call_args,first_call)
        self.assertEqual(self.monitor.command.call_count,1)
        with self.assertRaisesRegex(ValueError,'different Pi assignment'):
            pi_router.dispatch(self.root,self.args|{'task':'different'})
        with self.assertRaisesRegex(ValueError,'no local fallback'):
            pi_router.dispatch(self.root,self.args|{'request_id':'new','instance_id':'stale'})
        self.assertEqual(self.monitor.command.call_count,1)

    def test_interrupted_before_delivery_recovers_and_oversized_payload_leaves_no_task(self):
        with patch.object(self.monitor,'command',side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                pi_router.dispatch(self.root,self.args)
        self.assertEqual(pi_router.dispatch(self.root,self.args)['delivery']['status'],'unknown')
        self.monitor.command.assert_not_called()
        oversized=self.args|{'request_id':'large','ownership':['a'*300]*20,'acceptance':['b'*300]*20}
        with self.assertRaisesRegex(ValueError,'size limit'):
            pi_router.dispatch(self.root,oversized)
        with self.assertRaisesRegex(ValueError,'Unknown Pi assignment'):
            pi_router.result(self.root,'large')

    def test_corrected_result_is_relayed_after_review(self):
        first=pi_router.dispatch(self.root,self.args)
        broker=SwarmBroker(self.root,self.monitor)
        def report(text, request):
            broker.call('pi-a','swarm_task',{'action':'update','task_id':first['task_id'],'status':'completed',
                                           'result':text,'request_id':request})
        report('original evidence','r1')
        pi_router.review(self.root,{'request_id':'request-a','accepted':False,'evidence':['missing proof']})
        report('corrected evidence','r2')
        with patch('conductor.codex_inbox.call',return_value={'status':'queued'}) as send:
            pi_router.relay_results(self.root)
            self.assertIn('corrected evidence',send.call_args.args[3]['message'])
        pi_router.review(self.root,{'request_id':'request-a','accepted':True,'evidence':['corrected proof']})
        report('changed after acceptance','r3')
        self.assertFalse(pi_router.result(self.root,'request-a')['complete'])
        with patch('conductor.codex_inbox.call',return_value={'status':'queued'}) as send:
            pi_router.relay_results(self.root)
            self.assertIn('changed after acceptance',send.call_args.args[3]['message'])
        broker.close()

    def test_disabled_execution_and_invalid_scope_do_not_send(self):
        (self.root.parent/'ui-settings.json').write_text(json.dumps({'computer_access':False}))
        with self.assertRaisesRegex(ValueError,'disabled'):
            pi_router.dispatch(self.root,self.args)
        (self.root.parent/'ui-settings.json').unlink()
        with self.assertRaises(ValueError):
            pi_router.dispatch(self.root,self.args|{'ownership':[]})
        self.monitor.command.assert_not_called()

    def test_control_preserves_owner_task_contract_and_is_idempotent(self):
        original = pi_router.dispatch(self.root, self.args)
        args = {'request_id': 'request-a', 'control_id': 'control-1', 'action': 'steer', 'message': 'Check the earlier evidence'}
        receipt = pi_router.control(self.root, args)
        self.assertEqual(pi_router.control(self.root, args), receipt)
        self.assertEqual(self.monitor.command.call_count, 2)
        call = self.monitor.command.call_args.args
        self.assertEqual(call[:2], ('pi-a', 'instance-a'))
        self.assertEqual(call[3], 'steer')
        self.assertIn(original['task_id'], call[4])
        broker = SwarmBroker(self.root, self.monitor)
        self.assertEqual(broker.db.execute('SELECT count(*) FROM tasks').fetchone()[0], 1)
        broker.close()
        self.assertEqual(pi_router.result(self.root, 'request-a')['task'], original['task'])
        with self.assertRaisesRegex(ValueError, 'different content'):
            pi_router.control(self.root, args | {'message': 'different'})

    def test_control_stale_instance_and_terminal_report_fail_closed(self):
        original = pi_router.dispatch(self.root, self.args)
        args = {'request_id': 'request-a', 'control_id': 'control-1', 'action': 'follow_up', 'message': 'Continue'}
        self.monitor.bridges.return_value = {'pi-a': {'instance_id': 'replacement'}}
        with self.assertRaisesRegex(ValueError, 'stale instance'):
            pi_router.control(self.root, args)
        self.monitor.bridges.return_value = {'pi-a': {'instance_id': 'instance-a'}}
        broker = SwarmBroker(self.root, self.monitor)
        broker.call('pi-a', 'swarm_task', {'action':'update', 'task_id':original['task_id'], 'status':'completed',
                                         'result':'done', 'request_id':'done'})
        broker.close()
        with self.assertRaisesRegex(ValueError, 'new assignment'):
            pi_router.control(self.root, args)
        self.assertEqual(self.monitor.command.call_count, 1)

    def test_unknown_control_is_not_replayed_even_after_instance_change(self):
        pi_router.dispatch(self.root, self.args)
        args = {'request_id': 'request-a', 'control_id': 'control-1', 'action': 'follow_up', 'message': 'Continue'}
        self.monitor.command.side_effect = OSError('connection interrupted')
        with self.assertRaises(OSError):
            pi_router.control(self.root, args)
        self.monitor.command.side_effect = None
        self.monitor.bridges.return_value = {'pi-a': {'instance_id':'replacement'}}
        receipt = pi_router.control(self.root, args)
        self.assertEqual(receipt['delivery']['status'], 'unknown')
        self.assertIn('not be replayed', receipt['reconciliation'])
        self.assertEqual(self.monitor.command.call_count, 2)

    def test_control_rejects_unknown_assignment_delivery(self):
        self.monitor.command.return_value = {'status':'unknown'}
        pi_router.dispatch(self.root, self.args)
        with self.assertRaisesRegex(ValueError, 'reconcile'):
            pi_router.control(self.root, {'request_id':'request-a', 'control_id':'control-1',
                                        'action':'steer', 'message':'Continue'})
        self.assertEqual(self.monitor.command.call_count, 1)

    def test_control_rejects_changed_task_owner(self):
        original = pi_router.dispatch(self.root, self.args)
        broker = SwarmBroker(self.root, self.monitor)
        with broker.db:
            broker.db.execute('UPDATE tasks SET owner=? WHERE task_id=?', ('pi-b', original['task_id']))
        broker.close()
        with self.assertRaisesRegex(ValueError, 'ownership changed'):
            pi_router.control(self.root, {'request_id':'request-a', 'control_id':'control-1',
                                         'action':'steer', 'message':'Continue'})
        self.assertEqual(self.monitor.command.call_count, 1)
