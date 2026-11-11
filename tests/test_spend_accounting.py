import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conductor.provider import OpenRouter, spend_limit, request_summary
from conductor.store import Store


def response(cost=0.1):
    usage = {} if cost is None else {'cost': cost}
    return io.BytesIO(json.dumps({'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}], 'usage': usage}).encode())


class SpendAccountingTest(unittest.TestCase):
    def test_threshold_survives_resume_and_preserves_legacy_totals(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            store.configure(api_calls=20, usage={'cost': 2.0})
            store.close()
            provider = OpenRouter('fixture', key='fixture', state_path=root, max_spend=2.05)
            with patch('urllib.request.urlopen', return_value=response()) as call:
                provider.complete([], [], purpose='compaction', agent='coordinator')
                with self.assertRaisesRegex(RuntimeError, 'threshold reached'):
                    provider.complete([], [])
                call.assert_called_once()
            resumed = OpenRouter('fixture', key='fixture', state_path=root, max_spend=2.05)
            self.assertEqual(resumed.calls, 21)
            self.assertAlmostEqual(resumed.usage['cost'], 2.1)
            record = request_summary(root)['last_request']
            self.assertEqual((record['purpose'], record['agent'], record['status']), ('compaction', 'coordinator', 'completed'))
            self.assertEqual(record['cost'], 0.1)
            self.assertIsNotNone(record['latency_ms'])

    def test_unknown_charge_pauses_threshold_and_timeout_pauses_unlimited(self):
        for error in (False, True):
            with tempfile.TemporaryDirectory() as root:
                provider = OpenRouter('fixture', key='fixture', state_path=root, max_spend=None if error else 1)
                with patch('urllib.request.urlopen', side_effect=TimeoutError() if error else None, return_value=response(None)) as call:
                    if error:
                        with self.assertRaisesRegex(RuntimeError, 'outcome unknown'):
                            provider.complete([], [])
                    else:
                        provider.complete([], [])
                    with self.assertRaisesRegex(RuntimeError, 'unknown'):
                        provider.complete([], [])
                    call.assert_called_once()
                self.assertEqual(request_summary(root)['unknown_charges'], 1)

    def test_zero_spend_blocks_but_zero_call_ceiling_is_unlimited(self):
        with patch('urllib.request.urlopen', return_value=response()) as call:
            with self.assertRaisesRegex(RuntimeError, 'threshold reached'):
                OpenRouter('fixture', key='fixture', max_spend=0).complete([], [])
            call.assert_not_called()
            OpenRouter('fixture', key='fixture', max_calls=0).complete([], [])
            call.assert_called_once()
        for value in (-1, float('nan'), float('inf'), True):
            with self.assertRaises(ValueError):
                spend_limit(value)

    def test_pending_reservation_survives_crash(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, 'requests.json').write_text(json.dumps([{'status': 'pending', 'cost': None, 'cumulative_calls': 3}]))
            provider = OpenRouter('fixture', key='fixture', state_path=root)
            self.assertEqual(provider.calls, 3)
            with patch('urllib.request.urlopen') as call:
                with self.assertRaisesRegex(RuntimeError, 'outcome unknown'):
                    provider.complete([], [])
                call.assert_not_called()
