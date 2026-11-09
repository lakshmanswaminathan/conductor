import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from conductor.computer import computer_tools
from conductor.models import DEFAULT_MODEL
from conductor.pi_monitor import pi_tools
from conductor.provider import OpenRouter, schema_diagnostic
from conductor.runtime import COMMON_TOOLS, MANAGER_TOOLS
from conductor.swarm_runtime import swarm_tools, codex_tools


class ProviderSchemaTest(unittest.TestCase):
    def test_complete_desktop_tool_catalog_has_typed_enums(self):
        def check(value, path):
            if isinstance(value, dict):
                if 'enum' in value:
                    self.assertIn('type', value, path)
                for key, child in value.items():
                    check(child, path + '.' + key)
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    check(child, f'{path}[{index}]')
        for tool in COMMON_TOOLS + MANAGER_TOOLS + computer_tools() + pi_tools() + swarm_tools() + codex_tools():
            check(tool['function']['parameters'], tool['function']['name'])

    def test_schema_400_exposes_only_safe_diagnostic_and_does_not_retry(self):
        payload = {'error': {'message': 'Provider returned error', 'metadata': {'raw': json.dumps({
            'error': {'message': "Invalid request: tools.function.parameters is not a valid moonshot flavored json schema, details: <At path 'properties.status': type is not defined>",
                      'private_data': 'do-not-display'}})}, 'private_data': 'also-private'}}
        error = urllib.error.HTTPError('https://openrouter.ai/api/v1/chat/completions', 400, 'Bad Request', {}, io.BytesIO(json.dumps(payload).encode()))
        with patch('urllib.request.urlopen', side_effect=error) as call:
            with self.assertRaisesRegex(RuntimeError, 'properties.status is missing its type') as raised:
                OpenRouter(DEFAULT_MODEL, key='fixture').complete([{'role': 'user', 'content': 'Test'}], [])
            call.assert_called_once()
            self.assertNotIn('private', str(raised.exception))
        self.assertIsNone(schema_diagnostic(json.dumps({'error': {'message': 'A private echoed prompt'}})))
        self.assertIsNone(schema_diagnostic('not-json'))


if __name__ == '__main__':
    unittest.main()
