#!/usr/bin/env python3
"""Set Pi 1.5 model-specific native compaction thresholds without inference."""
import argparse
import json
import os
from pathlib import Path
import stat
import tempfile

MAX_SAFE_INTEGER = 2**53 - 1


def token_count(value):
    try:
        if isinstance(value, bool) or isinstance(value, float):
            raise ValueError
        number = int(value)
        if not 0 <= number <= MAX_SAFE_INTEGER:
            raise ValueError
        return number
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError('Token counts must be nonnegative safe integers') from None


def configure(settings, model, context_window, target_tokens=200000, keep_recent=20000, dry_run=False):
    context_window, target_tokens, keep_recent = map(token_count, (context_window, target_tokens, keep_recent))
    if not 0 <= keep_recent < target_tokens < context_window:
        raise ValueError('Require keep-recent < target-tokens < context-window, with a positive target')
    if not isinstance(model, str) or '/' not in model or any(c.isspace() for c in model) or not all(model.split('/', 1)):
        raise ValueError('Model must be the exact Pi provider/model identifier')
    path = Path(settings).expanduser()
    if path.is_symlink():
        path = path.resolve()
    original = path.read_bytes() if path.exists() else None
    data = json.loads(original) if original is not None else {}
    if not isinstance(data, dict):
        raise ValueError('Pi settings must be a JSON object')
    compaction = data.setdefault('compaction', {})
    if not isinstance(compaction, dict):
        raise ValueError('compaction must be an object')
    overrides = compaction.setdefault('modelOverrides', {})
    if not isinstance(overrides, dict):
        raise ValueError('compaction.modelOverrides must be an object')
    entry = overrides.setdefault(model, {})
    if not isinstance(entry, dict):
        raise ValueError('Selected model override must be an object')
    desired = {'reserveTokens': context_window - target_tokens, 'keepRecentTokens': keep_recent}
    changed = any(entry.get(key) != value for key, value in desired.items())
    entry.update(desired)
    if changed and not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
        fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, 'w') as output:
                json.dump(data, output, indent=2, ensure_ascii=False)
                output.write('\n')
                output.flush()
                os.fsync(output.fileno())
            # Refuse to overwrite settings changed by Pi since our initial read.
            if (path.read_bytes() if path.exists() else None) != original:
                raise RuntimeError('Pi settings changed during update; retry against the latest file')
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return {'model': model, 'target_tokens': target_tokens, 'keep_recent_tokens': keep_recent,
            'reserve_tokens': desired['reserveTokens'], 'automatic_enabled': compaction.get('enabled', True),
            'changed': changed, 'dry_run': dry_run}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', type=Path, default=Path.home() / '.pi/agent/settings.json')
    parser.add_argument('--model', required=True)
    parser.add_argument('--context-window', required=True, type=token_count)
    parser.add_argument('--target-tokens', default=200000, type=token_count)
    parser.add_argument('--keep-recent', default=20000, type=token_count)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    try:
        result = configure(args.settings, args.model, args.context_window, args.target_tokens, args.keep_recent, args.dry_run)
    except (ValueError, OSError, RuntimeError, argparse.ArgumentTypeError):
        parser.exit(2, 'Could not configure Pi compaction: check token ordering, model identifier, and settings file structure/access; no settings content displayed.\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
