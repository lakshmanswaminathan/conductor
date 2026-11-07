import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from conductor import conductor_mailbox as box
from conductor.coordinator_mcp import call
from conductor.store import Store


class MailboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def deliver(self, request, chat='test-chat'):
        item = next(item for item in box.pending(self.root) if item['request_id'] == request)
        store = Store(self.root/'runs'/chat)
        store.event('user', '*', 'instruction', item['message'])
        store.close()
        box.ack(self.root, request, chat)

    def test_queue_is_idempotent_attributed_and_survives_reopen(self):
        args = {'request_id':'work-1','message':'Manage the already-authorized task'}
        first = call(self.root, 'conductor_send', args)
        self.assertEqual(first['status'], 'queued')
        self.assertEqual(call(self.root, 'conductor_send', args), first)
        self.assertEqual(len(box.pending(self.root)), 1)
        self.assertIn('not new human authorization', box.pending(self.root)[0]['message'])
        self.assertIsNone(first['report'])
        with self.assertRaisesRegex(ValueError, 'different content'):
            box.enqueue(self.root, args | {'message':'other task'})
        with self.assertRaisesRegex(ValueError, 'Unknown Conductor chat'):
            box.enqueue(self.root, args | {'chat_id':'missing'})
        self.deliver('work-1')
        self.assertEqual(box.pending(self.root), [])
        self.assertEqual(call(self.root, 'conductor_result', {'request_id':'work-1'})['status'], 'delivered')
        self.assertEqual(box.enqueue(self.root, args)['chat_id'], 'test-chat')

    def test_report_survives_outage_and_is_not_sent_twice(self):
        box.enqueue(self.root, {'request_id':'work-1','message':'task'})
        self.deliver('work-1')
        with patch('conductor.codex_inbox.call', side_effect=ValueError('offline')):
            box.report(self.root, 'test-chat', 'done', 'Evidence report', 1)
        self.assertEqual(box.status(self.root)['pending_reports'], 1)
        receipt = {'status':'queued','queue_id':'receipt'}
        with patch('conductor.codex_inbox.call', return_value=receipt) as send:
            box.flush(self.root)
            box.report(self.root, 'test-chat', 'done', 'Evidence report', 1)
            box.flush(self.root)
            send.assert_called_once()
            self.assertIn('conductor_send', send.call_args.args[3]['message'])
        self.assertEqual(box.result(self.root, 'work-1')['report']['delivery'], receipt)

    def test_old_result_does_not_complete_new_instruction(self):
        box.enqueue(self.root, {'request_id':'one','message':'first'})
        self.deliver('one')
        box.enqueue(self.root, {'request_id':'two','chat_id':'test-chat','message':'second'})
        self.deliver('two')
        with patch('conductor.codex_inbox.call', return_value={'status':'unknown'}):
            box.report(self.root, 'test-chat', 'done', 'first only', 1)
        self.assertIsNotNone(box.result(self.root, 'one')['report'])
        self.assertIsNone(box.result(self.root, 'two')['report'])
        with patch('conductor.codex_inbox.call') as send:
            box.flush(self.root)
            send.assert_not_called()  # unknown external delivery is not replayed
        with patch('conductor.codex_inbox.call', return_value={'status':'queued'}):
            box.report(self.root, 'test-chat', 'blocked', 'second blocked', 2)
        self.assertEqual(box.result(self.root, 'one')['report']['message'], 'first only')
        self.assertEqual(box.result(self.root, 'two')['report']['message'], 'second blocked')

    def test_restart_exposes_interruption_without_replaying_work(self):
        box.enqueue(self.root, {'request_id':'one','message':'first'})
        self.deliver('one')
        box.enqueue(self.root, {'request_id':'two','message':'still queued'})
        self.assertEqual(box.recover(self.root), 1)
        self.assertEqual(box.result(self.root, 'one')['status'], 'interrupted')
        self.assertEqual([item['request_id'] for item in box.pending(self.root)], ['two'])
        self.assertEqual(box.recover(self.root), 0)
        with patch('conductor.codex_inbox.call', return_value={'status':'queued'}):
            box.report(self.root, 'test-chat', 'done', 'Resumed after inspecting Pi', 1)
        self.assertEqual(box.result(self.root, 'one')['status'], 'reported')
        self.assertEqual(box.recover(self.root), 0)


if __name__ == '__main__':
    unittest.main()
