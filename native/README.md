# Native Conductor host

Build from the repository root with `./scripts/build-app.sh`, then open `native/build/Conductor.app`. Requires macOS 14+, Xcode command-line tools, and Python 3. The app is ad-hoc signed as `com.conductor.desktop`. The build copies the Python package into app Resources, excluding bytecode caches. The backend runs from Resources and no longer depends on the checkout. Python itself is not bundled: its absolute path is selected at build time (`CONDUCTOR_PYTHON` overrides discovery), and moving Python requires rebuilding. Install the bundle in Applications to avoid protected Documents-folder access during startup. A missing server handshake produces a visible error after 15 seconds.

The AppKit host opens a 1280×860 WKWebView and starts `python -m conductor ui-server --port 0 --native <app executable>`. The first stdout line must be `{"url":"http://127.0.0.1:PORT","token":"..."}`. It authenticates the initial request with `X-Conductor-Token`; the server must set an HttpOnly session cookie for subsequent requests. Tokens are never included in argv or logs. Navigation is restricted to the server's loopback origin. Closing the final window terminates the server process.

## Native protocol

Invoke `native/build/Conductor.app/Contents/MacOS/Conductor --native`, write one JSON line to stdin, and read one JSON line from stdout. Errors are `{"ok":false,"error":"..."}`; callers must inspect this field, not just the exit code.

- `{"action":"status"}` → platform, screen_recording and accessibility booleans, plus bundle_path and bundle_id identifying the native app.
- `{"action":"apps"}` → `apps` array with name, bundle_id, pid (regular desktop applications).
- `{"action":"focus","app":"com.apple.TextEdit"}` → activates a uniquely identified running application.
- `{"action":"screenshot","app":"com.apple.TextEdit","output":"/absolute/path.png"}` → captures largest visible normal window; omit app for main display. Output includes path, width, height, origin_x, origin_y, scale. Images use screen-point dimensions (scale 1); translate local image positions by origin to click. Capture omits the window shadow and includes window decoration; verify visual alignment before sensitive actions.
- `{"action":"click","x":100,"y":100,"button":"left","count":1}` → global screen coordinates, top-left origin; left/right and count 1–3.
- `{"action":"type","text":"Hello"}` → Unicode keyboard events to the focused application, without clipboard mutation.
- `{"action":"key","key":"a","modifiers":["command"]}` → US-layout physical key codes, common navigation keys, command/shift/option/control. Use `type` for layout-independent text.
- `{"action":"scroll","dx":0,"dy":-300}` → pixel scroll at current pointer position; deltas bounded to ±10000.
- `{"action":"ax","app":"com.apple.TextEdit","max_depth":4}` → accessibility tree; max depth 8, total 500 nodes, 50 children per node, truncated strings, secure text field values redacted. Nodes with valid AXPosition/AXSize expose `frame: {x, y, width, height}` in global screen points with top-left origin; missing or invalid geometry is omitted, and each component is bounded to ±100000 with nonnegative dimensions. Ordinary visible values can contain sensitive user content.
- `{"action":"open_permission_settings","pane":"screen_recording"}` → requests the selected OS permission if denied, then opens the actual System Settings privacy pane (`screen_recording` or `accessibility`) and returns current status plus `opened_pane`. Opening settings does not grant permission. Expose only through an explicit user UI control, never to the agent tool schema.
- `{"action":"request_permissions"}` → explicit system permission requests and current status. The server must expose this only through a direct user-triggered permissions control, never as an automatic agent action.

The executable does not interpret shell commands. Accessibility is required for input and AX reads; Screen Recording is required for capture. Permission denial is an explicit error. System Settings may attribute CLI requests to the responsible launching app/terminal; grant access to the displayed responsible application and restart Conductor as macOS requests. Event posting confirms delivery to the OS only; callers must inspect a fresh screenshot/AX tree to verify the intended app state.
