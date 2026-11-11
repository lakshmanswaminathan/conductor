import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/configure-pi-compaction.py'
SPEC = importlib.util.spec_from_file_location('pi_compaction_settings', SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PiCompactionSettingsTest(unittest.TestCase):
    def test_preserves_other_settings_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            original = {'private': 'fixture-secret', 'compaction': {'enabled': False, 'reserveTokens': 123,
                'modelOverrides': {'other/model': {'reserveTokens': 500}, 'provider/model': {'extra': 'keep'}}}}
            path.write_text(json.dumps(original))
            result = MODULE.configure(path, 'provider/model', 1000000)
            self.assertFalse(result['automatic_enabled'])
            saved = json.loads(path.read_text())
            self.assertEqual(saved['private'], 'fixture-secret')
            self.assertEqual(saved['compaction']['reserveTokens'], 123)
            self.assertEqual(saved['compaction']['modelOverrides']['other/model'], {'reserveTokens': 500})
            self.assertEqual(saved['compaction']['modelOverrides']['provider/model'], {'extra': 'keep', 'reserveTokens': 800000, 'keepRecentTokens': 20000})
            before = path.stat().st_mtime_ns
            self.assertFalse(MODULE.configure(path, 'provider/model', 1000000)['changed'])
            self.assertEqual(path.stat().st_mtime_ns, before)

    def test_invalid_targets_and_dry_run_never_mutate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            path.write_text('{"private":"fixture-secret"}')
            before = path.read_bytes()
            for context, target, keep in ((200000, 200000, 20000), (1000000, 20000, 20000), (0, 0, 0), (2**53, 200000, 20000)):
                with self.assertRaises((ValueError, MODULE.argparse.ArgumentTypeError)):
                    MODULE.configure(path, 'provider/model', context, target, keep)
                self.assertEqual(path.read_bytes(), before)
            result = subprocess.run([sys.executable, str(SCRIPT), '--settings', str(path), '--model', 'provider/model', '--context-window', '1000000', '--dry-run'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('fixture-secret', result.stdout + result.stderr)
            self.assertTrue(json.loads(result.stdout)['dry_run'])
            self.assertEqual(path.read_bytes(), before)
