import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from conductor.cli import initialize
from conductor.pi_context_policy import maintain_context


class PiContextPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = initialize(root/'run', root, 'Fixture')
        self.store.configure(computer_access=True)
        self.store.add_agent('worker', 'coordinator', 'Owned work')
        self.store.update_agent('worker', state='running')
        self.store.configure(**{'assignment:worker': {'kind':'pi', 'phase':'accepted', 'session_id':'pi-one', 'instance_id':'instance-one'}})
        self.record = {'instance_id':'instance-one', 'status':'idle', 'observed_at':time.time(),
                       'leaf_id':'leaf-one', 'context_usage':{'tokens':300000}, 'pending_messages':False, 'active_tools':[]}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_native_request_is_owned_idle_and_not_replayed_after_unknown(self):
        with patch('conductor.pi_context_policy.monitor') as monitor:
            monitor.records.return_value = {'pi-one':self.record, 'unassigned':self.record}
            monitor.command.side_effect = OSError('lost reply')
            maintain_context(self.store)
            maintain_context(self.store)
            monitor.command.assert_called_once()
            self.assertEqual(monitor.command.call_args.args[0:2], ('pi-one','instance-one'))
            self.assertEqual(monitor.command.call_args.args[-1], 'compact')
            self.assertEqual(self.store.config()['context_compaction:pi-one']['delivery']['status'], 'unknown')

    def test_busy_pending_stale_wrong_instance_and_disabled_policy_do_not_compact(self):
        with patch('conductor.pi_context_policy.monitor') as monitor:
            for change in ({'status':'working'}, {'pending_messages':True}, {'active_tools':[{'name':'bash'}]},
                           {'observed_at':time.time()-60}, {'instance_id':'replacement'},
                           {'compaction':{'state':'running'}}, {'context_usage':{'tokens':None}}):
                monitor.records.return_value = {'pi-one':self.record | change}
                maintain_context(self.store)
            self.store.configure(pi_compaction_tokens=0)
            monitor.records.return_value = {'pi-one':self.record}
            maintain_context(self.store)
            monitor.command.assert_not_called()
