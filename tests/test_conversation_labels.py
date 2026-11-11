import tempfile
from pathlib import Path
import unittest

from conductor.cli import initialize
from conductor.conductor_mailbox import _chats
from conductor.conversation_labels import conversation_labels, request_text
from conductor.ui_server import App


class ConversationLabelsTest(unittest.TestCase):
    def test_agent_feedback_chat_keeps_task_title_and_latest_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = initialize(root/'run', root, 'Placeholder')
            store.db.execute("DELETE FROM events WHERE kind='instruction'")
            store.db.commit()
            store.add_feedback('Build a status monitor', 'goal')
            store.add_feedback('Review its blocked-task display', 'review')
            labels = conversation_labels(store, {}, store.agent('coordinator'))
            self.assertEqual(labels, {'title': 'Build a status monitor', 'preview': 'Review its blocked-task display'})
            self.assertEqual(store.user_context(), [])
            store.close()

    def test_old_truncated_transport_title_is_recovered_on_all_read_surfaces(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = '[Codex orchestration request ' + 'long-id-' * 20 + ']\nUser explicitly requests Conductor dispatch to clean up ALL17 tasks.'
            store = initialize(root/'runs'/'chat', root, prompt, title=prompt[:100])
            store.event('user', '*', 'instruction', '[Codex orchestration request followup]\nReview the repaired package.')
            original = store.config()['title']
            app = object.__new__(App)
            app.root, app.jobs, app.stopping = root/'runs', {}, set()
            for result in (app.chats()[0], app.snapshot('chat'), _chats(root)[0]):
                self.assertEqual(result['title'], 'clean up ALL17 tasks.')
                self.assertEqual(result['preview'], 'Review the repaired package.')
            self.assertEqual(store.config()['title'], original)
            self.assertEqual(store.user_context()[0]['body'], prompt)
            store.configure(title='My explicit task name')
            self.assertEqual(conversation_labels(store, store.config(), store.agent('coordinator'))['title'], 'My explicit task name')
            store.close()

    def test_only_known_envelopes_are_unwrapped_and_credentials_redacted(self):
        self.assertEqual(request_text('[Other label]\nKeep this scope'), '[Other label] Keep this scope')
        self.assertEqual(request_text('[Codex orchestration requested]\nBuild the monitor'), 'Build the monitor')
        secret = 'sk-or-v1-' + 'x' * 64
        self.assertNotIn(secret, request_text('Use ' + secret))
