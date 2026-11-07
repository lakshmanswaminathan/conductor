import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from conductor.coordinator_mcp import call


class BypassTests(unittest.TestCase):
    def test_platform_and_code_bypass_without_dispatch_or_state_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)/'unused'
            with patch('conductor.coordinator_mcp.conductor_mailbox.enqueue') as enqueue, \
                 patch('conductor.coordinator_mcp.pi_router.dispatch') as dispatch:
                for operation in ('platform', 'code', 'other'):
                    result = call(root, 'conductor_bypass', {'operation':operation})
                    self.assertEqual(result['route'], 'codex_direct')
                    self.assertTrue(result['bypass'])
                    self.assertFalse(result['dispatched'])
                enqueue.assert_not_called()
                dispatch.assert_not_called()
            self.assertFalse(root.exists())

    def test_generation_remains_routed_and_invalid_input_fails(self):
        result = call('/unused', 'conductor_bypass', {'operation':'data_generation'})
        self.assertEqual(result['route'], 'conductor')
        self.assertFalse(result['bypass'])
        for args in ({}, {'operation':'unknown'}, {'operation':True}, {'operation':'platform','global':True}):
            with self.assertRaises(ValueError):
                call('/unused', 'conductor_bypass', args)

    def test_registered_stdio_contract_exposes_policy_and_bypass(self):
        script = Path(__file__).parents[1]/'scripts/conductor-pi-mcp'
        messages = [
            {'jsonrpc':'2.0','id':1,'method':'initialize'},
            {'jsonrpc':'2.0','method':'notifications/initialized'},
            {'jsonrpc':'2.0','id':2,'method':'tools/list'},
            {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{
                'name':'conductor_bypass','arguments':{'operation':'platform'}}}]
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run([sys.executable,str(script),'--root',temporary,
                '--routing-policy','data-generation-only'],
                input='\n'.join(json.dumps(item) for item in messages)+'\n', text=True,
                capture_output=True, check=True, timeout=10)
        replies = [json.loads(line)['result'] for line in result.stdout.splitlines()]
        self.assertIn('v2 platform', replies[0]['instructions'])
        self.assertIn('conductor_bypass', [item['name'] for item in replies[1]['tools']])
        self.assertFalse(replies[2]['isError'])
        self.assertEqual(replies[2]['structuredContent']['route'], 'codex_direct')


if __name__ == '__main__':
    unittest.main()
