"""Desktop coordinator owns inference, delegates execution to Pi, and collaborates with Codex."""
import json
from pathlib import Path
import subprocess
import tempfile
import threading
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

from conductor.cli import initialize
from conductor.ui_server import App


class DesktopBridgeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/tmp')
        self.root = Path(self.temp.name)
        self.app = App(self.root / 'runs', provider_factory=Mock(return_value=object()))
        self.app.mailbox_stop.set()
        self.app.mailbox_thread.join(2)
        self.app.key = ''
        self.bridge = ModuleType('conductor.desktop_bridge')
        self.bridge.submit = Mock(return_value={'id': 'chat-fixture', 'delivery': {'status': 'queued'}})
        self.bridge.status = Mock(return_value={'configured': True, 'thread_id': 'pinned-thread'})
        self.modules = patch.dict('sys.modules', {'conductor.desktop_bridge': self.bridge})
        self.modules.start()

    def tearDown(self):
        self.modules.stop()
        self.app.close()
        self.temp.cleanup()

    def test_own_coordinator_retries_and_busy_steering(self):
        self.app.key = 'fixture'
        entered, release = threading.Event(), threading.Event()
        versions = []
        def run(store, provider):
            config = store.config()
            self.assertEqual(config['execution_policy'], 'pi_only')
            self.assertFalse(config['allow_exec'])
            versions.append(len(store.user_context()))
            if len(versions) == 1:
                entered.set()
                release.wait(3)
            store.configure(processed_user_version=versions[-1])
            store.update_agent('coordinator', state='done', result='finished')
        with patch('conductor.ui_server.run_coordinator', side_effect=run), patch('conductor.conductor_mailbox.report'):
            try:
                first = self.app.create({'message':'first','request_id':'first'})
                self.assertTrue(entered.wait(2))
                retry = self.app.create({'message':'first','request_id':'first'})
                self.assertEqual(first['id'], retry['id'])
                result = self.app.submit(first['id'], {'message':'steer','request_id':'steer'})
                self.assertTrue(result['queued'])
                self.app.submit(first['id'], {'message':'steer','request_id':'steer'})
                thread = self.app.jobs[first['id']]
                release.set()
                thread.join(4)
                self.assertFalse(thread.is_alive())
                self.assertEqual(versions, [1,2])
                self.app.create({'message':'first','request_id':'first'})
                self.assertEqual(versions, [1,2])
                self.assertEqual(self.app.snapshot(first['id'])['engine'], 'conductor')
                with self.assertRaisesRegex(ValueError, 'different content'):
                    self.app.submit(first['id'], {'message':'different','request_id':'steer'})
            finally:
                release.set()

    def test_codex_mailbox_waits_for_key_then_persists_and_wakes_once(self):
        from conductor import conductor_mailbox
        conductor_mailbox.enqueue(self.root, {'message':'inspect Pi','request_id':'incoming'})
        self.app.drain_mailbox()
        self.assertIn('OpenRouter key', self.app.mailbox_status['error'])
        self.assertEqual(len(conductor_mailbox.pending(self.root)),1)
        self.app.key = 'fixture'
        with patch.object(self.app,'start') as start:
            self.app.drain_mailbox()
            self.app.drain_mailbox()
            start.assert_called_once()
        receipt = conductor_mailbox.result(self.root,'incoming')
        self.assertEqual(receipt['status'],'delivered')
        self.assertEqual(len(self.app.snapshot(receipt['chat_id'])['messages']),1)

    def test_restart_interrupts_stale_coordinator_without_reexecution(self):
        store = initialize(self.app.root / 'stale', self.root, 'pending work')
        store.update_agent('coordinator', state='running')
        store.close()
        self.app.close()
        self.app = App(self.root / 'runs', provider_factory=Mock(side_effect=AssertionError('Unexpected reexecution')))
        self.app.mailbox_stop.set()
        self.app.mailbox_thread.join(2)
        snapshot = self.app.snapshot('stale')
        self.assertEqual(snapshot['state'], 'interrupted')
        self.assertFalse(snapshot['running'])
        self.app.provider_factory.assert_not_called()

    def test_failed_later_run_reports_only_attempted_new_events(self):
        store = initialize(self.app.root / 'failure', self.root, 'first')
        store.configure(processed_user_version=1)
        second = store.event('user', '*', 'instruction', 'second')
        attempted = store.user_context()
        store.event('user', '*', 'instruction', 'arrived during failed model request')
        store.update_agent('coordinator', state='failed', result='provider failed')
        try:
            with patch('conductor.conductor_mailbox.report') as report:
                self.app._report_outcome(store, 'failure', attempted, 1)
                report.assert_called_once_with(self.app.root.parent, 'failure', 'failed', 'provider failed', second, from_event_id=second)
        finally:
            store.close()

    def test_snapshot_preserves_history_and_exposes_delivery(self):
        store = initialize(self.app.root / 'fixture', self.root, 'question', engine='desktop_bridge')
        store.configure(bridge_pending=True, bridge_delivery={'status': 'unknown'})
        store.append('coordinator', {'role': 'assistant', 'content': 'answer'})
        store.close()
        snapshot = self.app.snapshot('fixture')
        self.assertEqual([m['content'] for m in snapshot['messages']], ['question', 'answer'])
        self.assertTrue(snapshot['bridge_pending'])
        self.assertEqual(snapshot['bridge_delivery']['status'], 'unknown')
        self.assertEqual(snapshot['engine'], 'desktop_bridge')
        self.assertFalse(snapshot['running'])

    def test_real_bridge_round_trip_keeps_one_message_and_reply(self):
        self.modules.stop()
        import sys
        from conductor import desktop_bridge
        thread = '12345678-1234-4234-8234-123456789abc'
        swarm = self.app.root.parent / 'swarm'
        (swarm / 'codex-target.json').write_text(json.dumps({'thread_id': thread, 'binary': sys.executable}))
        result = subprocess.CompletedProcess([], 0, 'Queued message 12345678-1234-4234-8234-123456789abd for thread ' + thread, '')
        with patch('conductor.codex_inbox.subprocess.run', return_value=result) as queue:
            first = desktop_bridge.submit(self.app.root, 'question', 'repeat')
            second = desktop_bridge.submit(self.app.root, 'question', 'repeat')
            self.assertEqual(first['id'], second['id'])
            queue.assert_called_once()
        reply = {'chat_id': first['id'], 'request_id': 'repeat', 'response_id': 'answer-1', 'message': 'answer', 'status': 'done'}
        desktop_bridge.reply(self.app.root, reply)
        desktop_bridge.reply(self.app.root, reply)
        snapshot = self.app.snapshot(first['id'])
        self.assertEqual([m['content'] for m in snapshot['messages']], ['question', 'answer'])
        self.assertEqual(snapshot['state'], 'done')
        self.assertEqual(snapshot['messages'][-1]['agent'], 'Codex')
        self.assertFalse(snapshot['bridge_pending'])

    def test_delivery_notice_and_observer_version_projection(self):
        source = (Path(__file__).parents[1] / 'conductor/web/app.js').read_text()
        functions = '\n'.join(line for line in source.splitlines() if line.startswith(('function notice(', 'function clearRelayNotice(', 'function extensionRuntimeText(')))
        harness = """
const assert=require('node:assert/strict');const node={dataset:{}};const $=()=>node;const state={id:'chat'};
""" + functions + """
notice('Queued','relay');clearRelayNotice({id:'other',state:'done'});assert.equal(node.textContent,'Queued');
clearRelayNotice({id:'chat',state:'done'});assert.equal(node.hidden,true);
notice('Unrelated failure');clearRelayNotice({id:'chat',state:'working'});assert.equal(node.textContent,'Unrelated failure');
assert.match(extensionRuntimeText({instance_id:'live'}),/older observer.*initial/);
assert.match(extensionRuntimeText({extension_runtime:{loaded_hashes:{observer:'hash'},update_state:'waiting_for_idle'}}),/waiting for Pi/);
assert.match(extensionRuntimeText({extension_runtime:{loaded_hashes:{observer:'hash'},update_state:'current'}}),/current/);
"""
        subprocess.run(['node', '-e', harness], check=True, capture_output=True, text=True)

    def test_composer_reuses_request_after_lost_response(self):
        source = (Path(__file__).parents[1] / 'conductor/web/app.js').read_text()
        send = source[source.index('const composerRequests='):source.index('const modelMenus=')]
        harness = """
const assert=require('node:assert/strict');
const nodes={prompt:{value:'hello',style:{},focus(){}},send:{}};
const $=id=>nodes[id];const state={id:null,bootstrap:{key_configured:true}};
const notice=()=>{};let calls=[];let next=0;
const crypto={randomUUID:()=>String(++next)};
async function api(path,body){calls.push({path,body});if(calls.length===1)throw Error('lost response');return {id:'chat-one',delivery:{status:'queued'}};}
async function loadChat(){}async function loadChats(){}
""" + send + """
(async()=>{await send();await send();assert.equal(calls.length,2);assert.equal(calls[0].body.request_id,calls[1].body.request_id);assert.equal(state.id,'chat-one');assert.equal(nodes.prompt.value,'');})().catch(e=>{console.error(e);process.exitCode=1;});
"""
        subprocess.run(['node', '-e', harness], check=True, capture_output=True, text=True)
