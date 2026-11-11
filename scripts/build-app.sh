#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${CONDUCTOR_PYTHON:-$(command -v python3)}"
APP="$ROOT/native/build/Conductor.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
xcrun swiftc -swift-version 5 -O -target "$(uname -m)-apple-macosx14.0" -framework AppKit -framework WebKit -framework ScreenCaptureKit -framework ApplicationServices -framework ImageIO -framework UniformTypeIdentifiers "$ROOT/native/Conductor.swift" -o "$APP/Contents/MacOS/Conductor"
cp "$ROOT/native/Info.plist" "$APP/Contents/Info.plist"
"$PYTHON" - "$ROOT" "$PYTHON" "$APP/Contents/Resources/host-config.json" <<'PY'
import json, pathlib, shutil, sys, subprocess, datetime, hashlib
root = pathlib.Path(sys.argv[1])
config = pathlib.Path(sys.argv[3])
package = config.parent / 'conductor'
if package.exists():
    shutil.rmtree(package)
shutil.copytree(root / 'conductor', package, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo'))
revision = subprocess.check_output(['git', '-C', str(root), 'rev-parse', '--short=12', 'HEAD'], text=True).strip()
dirty = bool(subprocess.check_output(['git', '-C', str(root), 'status', '--porcelain'], text=True).strip())
digest = hashlib.sha256()
for source in sorted(package.rglob('*')):
    if source.is_file():
        digest.update(str(source.relative_to(package)).encode())
        digest.update(source.read_bytes())
(package / 'build-info.json').write_text(json.dumps({'revision': revision + ('-dirty' if dirty else ''), 'source_sha256': digest.hexdigest(), 'built_at': datetime.datetime.now(datetime.timezone.utc).isoformat()}))
config.write_text(json.dumps({'python': str(pathlib.Path(sys.argv[2]).resolve())}))
PY
codesign --force --sign - --identifier com.conductor.desktop "$APP"
printf '%s\n' "$APP"
