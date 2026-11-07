#!/usr/bin/env python3
"""Install Conductor extensions and atomically publish their auto-update manifest."""
from pathlib import Path
import os
import hashlib
import json

sources = Path(__file__).resolve().parents[1] / 'integrations/pi'
for name in ('conductor-observer.ts', 'conductor-swarm.ts'):
    destination = Path.home() / '.pi/agent/extensions' / name
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.exists() and not destination.read_text().startswith(('// Read-only Pi extension:', '// Conductor Pi extension:')):
        raise SystemExit(f'An unrelated {name} already exists; refusing to replace it.')
# Validate both destinations before making changes.
for name in ('conductor-observer.ts', 'conductor-swarm.ts'):
    destination = Path.home() / '.pi/agent/extensions' / name
    temporary = destination.with_suffix('.tmp')
    temporary.write_bytes((sources / name).read_bytes())
    os.chmod(temporary, 0o600)
    temporary.replace(destination)
manifest = Path.home() / '.pi/agent/extensions/conductor-runtime.json'
temporary = manifest.with_suffix('.tmp')
temporary.write_text(json.dumps({'hashes': {name: hashlib.sha256((sources / name).read_bytes()).hexdigest() for name in ('conductor-observer.ts', 'conductor-swarm.ts')}}))
os.chmod(temporary, 0o600)
temporary.replace(manifest)
print('Pi observer and swarm client installed. Sessions with auto-update apply them when idle. Older live sessions need /reload once; new sessions load them automatically.')
