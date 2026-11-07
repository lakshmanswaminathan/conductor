import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from conductor import desktop_bridge
from conductor.store import Store

THREAD = '12345678-1234-4234-8234-123456789abc'


class DesktopBridgeCoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)/'runs'
        (self.root.parent/'swarm').mkdir()
        (self.root.parent/'swarm/codex-target.json').write_text(json.dumps({'thread_id':THREAD,'binary':sys.executable}))
        self.success = subprocess.CompletedProcess([],0,f'Queued message 12345678-1234-4234-8234-123456789abd for thread {THREAD}','')

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_ui_message_is_forwarded_once_with_correct_provenance(self):
        message = 'a'*39999+'z'
        with patch('conductor.codex_inbox.subprocess.run',return_value=self.success) as queue:
            first = desktop_bridge.submit(self.root,message,'request-1')
            self.assertEqual(desktop_bridge.submit(self.root,message,'request-1'),first)
            queue.assert_called_once()
            wire = queue.call_args.args[0][-1]
            self.assertIn(message,wire)
            self.assertIn('User request forwarded',wire)
            self.assertNotIn('not a new human instruction',wire)
        store=Store(self.root/first['id'])
        self.assertEqual(len(store.user_context()),1)
        self.assertFalse(store.config()['allow_exec'])
        store.close()

    def test_timeout_does_not_requeue_after_new_client_and_conflicts_fail(self):
        with patch('conductor.codex_inbox.subprocess.run',side_effect=subprocess.TimeoutExpired('codex',20)) as queue:
            first=desktop_bridge.submit(self.root,'do work','req')
            second=desktop_bridge.submit(self.root,'do work','req',chat_id=first['id'])
            self.assertEqual(second['delivery']['status'],'unknown')
            queue.assert_called_once()
            with self.assertRaisesRegex(ValueError,'different content'):
                desktop_bridge.submit(self.root,'different work','req',chat_id=first['id'])

    def test_old_callback_cannot_finish_newer_pending_request(self):
        with patch('conductor.codex_inbox.subprocess.run',return_value=self.success):
            first=desktop_bridge.submit(self.root,'first','one')
            desktop_bridge.submit(self.root,'second','two',chat_id=first['id'])
        args={'chat_id':first['id'],'request_id':'one','response_id':'reply-one','message':'first answer','status':'done'}
        desktop_bridge.reply(self.root,args)
        self.assertTrue(desktop_bridge.reply(self.root,args)['duplicate'])
        with self.assertRaisesRegex(ValueError,'different content'):
            desktop_bridge.reply(self.root,args|{'message':'changed'})
        with self.assertRaisesRegex(ValueError,'Unknown request'):
            desktop_bridge.reply(self.root,args|{'request_id':'no-such-request','response_id':'other'})
        store=Store(self.root/first['id'])
        self.assertEqual(store.agent('coordinator')['state'],'awaiting_codex')
        self.assertTrue(store.config()['bridge_pending'])
        self.assertEqual(len(store.history('coordinator')),1)
        store.close()
