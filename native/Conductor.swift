import AppKit
import WebKit
import ScreenCaptureKit
import ApplicationServices
import ImageIO
import UniformTypeIdentifiers

struct NativeFailure: Error { let message: String }
func fail(_ message: String) throws -> Never { throw NativeFailure(message: message) }
func status() -> [String: Any] { ["platform": "macos", "screen_recording": CGPreflightScreenCaptureAccess(), "accessibility": AXIsProcessTrusted(), "bundle_path": Bundle.main.bundleURL.path, "bundle_id": Bundle.main.bundleIdentifier ?? "com.conductor.desktop"] }
func application(_ name: String) throws -> NSRunningApplication {
    let matches = NSWorkspace.shared.runningApplications.filter { $0.bundleIdentifier == name || $0.localizedName?.lowercased() == name.lowercased() }
    guard matches.count == 1, let app = matches.first else { try fail(matches.isEmpty ? "Application is not running: \(name)" : "Application name is ambiguous; use its bundle identifier") }
    return app
}
func requireAX() throws { if !AXIsProcessTrusted() { try fail("Accessibility permission is required. Open Conductor and use Enable permissions, then allow Conductor in System Settings > Privacy & Security > Accessibility.") } }
func number(_ json: [String: Any], _ key: String) throws -> Double {
    guard let n = json[key] as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID(), n.doubleValue.isFinite else { try fail("\(key) must be a finite number") }
    return n.doubleValue
}
func textField(_ json: [String: Any], _ key: String) throws -> String {
    guard let value = json[key] as? String, !value.isEmpty else { try fail("\(key) must be a nonempty string") }; return value
}
func axValue(_ element: AXUIElement, _ attribute: String) -> CFTypeRef? {
    var value: CFTypeRef?; guard AXUIElementCopyAttributeValue(element, attribute as CFString, &value) == .success else { return nil }; return value
}
func axTree(_ element: AXUIElement, depth: Int, remaining: inout Int) -> [String: Any] {
    guard remaining > 0 else { return ["truncated": true] }; remaining -= 1
    var node: [String: Any] = [:]
    for (key, attr) in [("role", kAXRoleAttribute), ("subrole", kAXSubroleAttribute), ("title", kAXTitleAttribute), ("description", kAXDescriptionAttribute)] {
        if let value = axValue(element, attr) as? String { node[key] = String(value.prefix(500)) }
    }
    if let rawPosition = axValue(element, kAXPositionAttribute), CFGetTypeID(rawPosition) == AXValueGetTypeID(),
       let rawSize = axValue(element, kAXSizeAttribute), CFGetTypeID(rawSize) == AXValueGetTypeID() {
        let positionValue = rawPosition as! AXValue, sizeValue = rawSize as! AXValue
        var position = CGPoint.zero, size = CGSize.zero
        if AXValueGetType(positionValue) == .cgPoint, AXValueGetType(sizeValue) == .cgSize,
           AXValueGetValue(positionValue, .cgPoint, &position), AXValueGetValue(sizeValue, .cgSize, &size),
           [position.x, position.y, size.width, size.height].allSatisfy({ $0.isFinite && abs($0) <= 100000 }),
           size.width >= 0, size.height >= 0 {
            node["frame"] = ["x": position.x, "y": position.y, "width": size.width, "height": size.height]
        }
    }
    let secure = (node["subrole"] as? String) == kAXSecureTextFieldSubrole
    if secure { node["value"] = "[redacted]" }
    else if let value = axValue(element, kAXValueAttribute) as? String { node["value"] = String(value.prefix(1000)) }
    if depth > 0, let children = axValue(element, kAXChildrenAttribute) as? [AXUIElement] {
        node["children"] = children.prefix(50).prefix(remaining).map { axTree($0, depth: depth - 1, remaining: &remaining) }
        if children.count > 50 { node["truncated"] = true }
    }
    return node
}
func screenshot(_ input: [String: Any]) throws -> [String: Any] {
    guard CGPreflightScreenCaptureAccess() else { try fail("Screen Recording permission is required. Open Conductor and enable permissions in System Settings, then restart Conductor.") }
    let output = try textField(input, "output")
    guard output.hasPrefix("/"), URL(fileURLWithPath: output).pathExtension.lowercased() == "png" else { try fail("output must be an absolute .png path") }
    let target: pid_t? = try (input["app"] as? String).map { try application($0).processIdentifier }
    var result: Result<[String: Any], Error>?
    SCShareableContent.getExcludingDesktopWindows(true, onScreenWindowsOnly: true) { content, error in
        if let error { result = .failure(error); return }
        guard let content else { result = .failure(NativeFailure(message: "No capture content available")); return }
        let filter: SCContentFilter
        let frame: CGRect
        if let target {
            guard let window = content.windows.filter({ $0.owningApplication?.processID == target && $0.windowLayer == 0 && $0.frame.width > 1 && $0.frame.height > 1 }).max(by: { $0.frame.width * $0.frame.height < $1.frame.width * $1.frame.height }) else { result = .failure(NativeFailure(message: "No visible application window found")); return }
            filter = SCContentFilter(desktopIndependentWindow: window); frame = window.frame
        } else {
            guard let display = content.displays.first(where: { $0.displayID == CGMainDisplayID() }) ?? content.displays.first else { result = .failure(NativeFailure(message: "No display available")); return }
            filter = SCContentFilter(display: display, excludingWindows: []); frame = display.frame
        }
        let configuration = SCStreamConfiguration(); configuration.width = Int(frame.width); configuration.height = Int(frame.height); configuration.showsCursor = true; configuration.ignoreShadowsSingleWindow = true; configuration.captureResolution = .nominal
        SCScreenshotManager.captureImage(contentFilter: filter, configuration: configuration) { image, error in
            if let error { result = .failure(error); return }
            guard let image, let destination = CGImageDestinationCreateWithURL(URL(fileURLWithPath: output) as CFURL, UTType.png.identifier as CFString, 1, nil) else { result = .failure(NativeFailure(message: "Cannot create screenshot output")); return }
            CGImageDestinationAddImage(destination, image, nil)
            guard CGImageDestinationFinalize(destination) else { result = .failure(NativeFailure(message: "Cannot write screenshot")); return }
            result = .success(["path": output, "width": image.width, "height": image.height, "origin_x": frame.origin.x, "origin_y": frame.origin.y, "coordinate_system": "global screen points; top-left origin", "scale": 1])
        }
    }
    let deadline = Date().addingTimeInterval(20)
    while result == nil && Date() < deadline { RunLoop.current.run(until: Date().addingTimeInterval(0.02)) }
    guard let result else { try fail("Screen capture timed out") }; return try result.get()
}
func native(_ input: [String: Any]) throws -> [String: Any] {
    let action = try textField(input, "action")
    switch action {
    case "status": return status()
    case "open_permission_settings":
        let pane = try textField(input, "pane")
        let destination: String
        switch pane {
        case "accessibility":
            destination = "Privacy_Accessibility"
            if !AXIsProcessTrusted() {
                let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
                _ = AXIsProcessTrustedWithOptions(options)
            }
        case "screen_recording":
            destination = "Privacy_ScreenCapture"
            if !CGPreflightScreenCaptureAccess() { _ = CGRequestScreenCaptureAccess() }
        default: try fail("pane must be accessibility or screen_recording")
        }
        guard let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?" + destination),
              NSWorkspace.shared.open(url) else { try fail("Could not open the requested System Settings privacy pane") }
        var result = status(); result["opened_pane"] = pane; return result
    case "request_permissions":
        CGRequestScreenCaptureAccess()
        let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        _ = AXIsProcessTrustedWithOptions(options); return status()
    case "apps": return ["apps": NSWorkspace.shared.runningApplications.filter { $0.activationPolicy == .regular }.map { ["name": $0.localizedName ?? "", "bundle_id": $0.bundleIdentifier ?? "", "pid": $0.processIdentifier] as [String: Any] }]
    case "focus":
        let app = try application(textField(input, "app")); guard app.activate(options: []) else { try fail("Application activation failed") }; return ["ok": true, "pid": app.processIdentifier]
    case "screenshot": return try screenshot(input)
    case "ax":
        try requireAX(); let app = try application(textField(input, "app")); guard input["max_depth"] == nil || (input["max_depth"] is Int && CFGetTypeID(input["max_depth"] as! NSNumber) != CFBooleanGetTypeID()) else { try fail("max_depth must be an integer") }; let depth = (input["max_depth"] as? Int) ?? 4
        guard (0...8).contains(depth) else { try fail("max_depth must be between 0 and 8") }
        var remaining = 500; return ["tree": axTree(AXUIElementCreateApplication(app.processIdentifier), depth: depth, remaining: &remaining), "node_limit": 500]
    case "click":
        try requireAX(); let x = try number(input, "x"), y = try number(input, "y")
        guard abs(x) < 100000, abs(y) < 100000 else { try fail("Coordinates are outside supported range") }
        guard input["button"] == nil || input["button"] is String, input["count"] == nil || (input["count"] is Int && CFGetTypeID(input["count"] as! NSNumber) != CFBooleanGetTypeID()) else { try fail("Invalid button or click count") }
        let button = input["button"] as? String ?? "left", count = input["count"] as? Int ?? 1
        guard ["left", "right"].contains(button), (1...3).contains(count) else { try fail("Invalid button or click count") }
        let point = CGPoint(x: x, y: y), mouse: CGMouseButton = button == "left" ? .left : .right
        for n in 1...count {
            for type: CGEventType in button == "left" ? [.leftMouseDown, .leftMouseUp] : [.rightMouseDown, .rightMouseUp] {
                guard let event = CGEvent(mouseEventSource: nil, mouseType: type, mouseCursorPosition: point, mouseButton: mouse) else { try fail("Cannot create mouse event") }
                event.setIntegerValueField(.mouseEventClickState, value: Int64(n)); event.post(tap: .cghidEventTap)
            }
        }; return ["ok": true]
    case "type":
        try requireAX(); guard let text = input["text"] as? String, text.utf16.count <= 100000 else { try fail("text must be a string of at most 100000 UTF-16 code units") }
        // Emit complete characters in bounded chunks, preserving surrogate pairs.
        for character in text { let units = Array(String(character).utf16)
            for down in [true, false] { guard let event = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: down) else { try fail("Cannot create keyboard event") }; units.withUnsafeBufferPointer { event.keyboardSetUnicodeString(stringLength: units.count, unicodeString: $0.baseAddress) }; event.post(tap: .cghidEventTap) }
        }; return ["ok": true]
    case "key":
        try requireAX(); let key = try textField(input, "key").lowercased()
        let keys: [String: CGKeyCode] = ["a":0,"s":1,"d":2,"f":3,"h":4,"g":5,"z":6,"x":7,"c":8,"v":9,"b":11,"q":12,"w":13,"e":14,"r":15,"y":16,"t":17,"1":18,"2":19,"3":20,"4":21,"6":22,"5":23,"=":24,"9":25,"7":26,"-":27,"8":28,"0":29,"]":30,"o":31,"u":32,"[":33,"i":34,"p":35,"enter":36,"return":36,"l":37,"j":38,"'":39,"k":40,";":41,"\\":42,",":43,"/":44,"n":45,"m":46,".":47,"tab":48,"space":49,"`":50,"backspace":51,"escape":53,"delete":117,"home":115,"end":119,"pageup":116,"pagedown":121,"left":123,"right":124,"down":125,"up":126]
        guard let code = keys[key] else { try fail("Unsupported key") }
        guard input["modifiers"] == nil || input["modifiers"] is [String] else { try fail("modifiers must be an array") }
        var flags = CGEventFlags(); for modifier in input["modifiers"] as? [String] ?? [] {
            switch modifier { case "command": flags.insert(.maskCommand); case "shift": flags.insert(.maskShift); case "option": flags.insert(.maskAlternate); case "control": flags.insert(.maskControl); default: try fail("Unsupported modifier") }
        }
        for down in [true, false] { guard let event = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: down) else { try fail("Cannot create keyboard event") }; event.flags = flags; event.post(tap: .cghidEventTap) }; return ["ok": true]
    case "scroll":
        try requireAX(); let dx = try number(input, "dx"), dy = try number(input, "dy")
        guard abs(dx) <= 10000, abs(dy) <= 10000 else { try fail("Scroll deltas must be within ±10000") }
        guard let event = CGEvent(scrollWheelEvent2Source: nil, units: .pixel, wheelCount: 2, wheel1: Int32(dy), wheel2: Int32(dx), wheel3: 0) else { try fail("Cannot create scroll event") }; event.post(tap: .cghidEventTap); return ["ok": true]
    default: try fail("Unsupported native action: \(action)")
    }
}

final class Host: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    var process: Process?; var window: NSWindow!; var web: WKWebView!; var serverPort: Int?; var startupTimer: Timer?; var startupFinished = false
    func applicationDidFinishLaunching(_ notification: Notification) {
        let menu = NSMenu()
        let appMenu = NSMenu(title: "Conductor")
        appMenu.addItem(withTitle: "Quit Conductor", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        let appItem = NSMenuItem(); appItem.submenu = appMenu; menu.addItem(appItem)
        let editMenu = NSMenu(title: "Edit")
        func editItem(_ title: String, _ action: String, _ key: String, modifiers: NSEvent.ModifierFlags = [.command]) {
            let item = editMenu.addItem(withTitle: title, action: NSSelectorFromString(action), keyEquivalent: key)
            // A nil target routes through the focused WKWebView's responder chain.
            item.target = nil; item.keyEquivalentModifierMask = modifiers
        }
        editItem("Undo", "undo:", "z")
        editItem("Redo", "redo:", "z", modifiers: [.command, .shift])
        editMenu.addItem(.separator())
        editItem("Cut", "cut:", "x")
        editItem("Copy", "copy:", "c")
        editItem("Paste", "paste:", "v")
        editItem("Select All", "selectAll:", "a")
        let editMenuItem = NSMenuItem(title: "Edit", action: nil, keyEquivalent: "")
        editMenuItem.submenu = editMenu; menu.addItem(editMenuItem)
        NSApp.mainMenu = menu
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 860), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "Conductor"; window.minSize = NSSize(width: 900, height: 620); window.center()
        web = WKWebView(frame: window.contentView!.bounds); web.autoresizingMask = [.width, .height]; web.navigationDelegate = self; window.contentView!.addSubview(web)
        window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
        do {
            guard let resource = Bundle.main.url(forResource: "host-config", withExtension: "json"), let config = try JSONSerialization.jsonObject(with: Data(contentsOf: resource)) as? [String: String], let python = config["python"], let resources = Bundle.main.resourceURL else { try fail("Missing host-config.json; rebuild with scripts/build-app.sh") }
            let task = Process(); task.executableURL = URL(fileURLWithPath: python); task.arguments = ["-m", "conductor", "ui-server", "--port", "0", "--native", Bundle.main.executablePath!]; task.currentDirectoryURL = resources
            var env = ProcessInfo.processInfo.environment; env["PYTHONUNBUFFERED"] = "1"; env["PYTHONDONTWRITEBYTECODE"] = "1"; env.removeValue(forKey: "PYTHONPATH"); task.environment = env
            let pipe = Pipe(); task.standardOutput = pipe; task.standardError = FileHandle.standardError; process = task
            startupTimer = Timer.scheduledTimer(withTimeInterval: 15, repeats: false) { [weak self] _ in
                guard let self, !self.startupFinished else { return }
                self.startupFinished = true
                if task.isRunning { task.terminate() }
                self.showError("The local server did not start within 15 seconds. Verify the configured Python installation and launch Conductor from Applications, then try again.")
            }
            try task.run()
            DispatchQueue.global().async { [weak self] in
                var data = Data()
                while data.count < 65536 { let byte = pipe.fileHandleForReading.readData(ofLength: 1); if byte.isEmpty || byte == Data([10]) { break }; data.append(byte) }
                DispatchQueue.main.async {
                    guard let self, !self.startupFinished else { return }
                    self.startupFinished = true; self.startupTimer?.invalidate()
                    guard let message = try? JSONSerialization.jsonObject(with: data) as? [String: String], let rawURL = message["url"], let url = URL(string: rawURL), url.scheme == "http", url.host == "127.0.0.1", let token = message["token"] else { self.showError("Conductor server did not return a valid startup handshake."); return }
                    self.serverPort = url.port; var request = URLRequest(url: url); request.setValue(token, forHTTPHeaderField: "X-Conductor-Token"); self.web.load(request)
                }
                // Drain subsequent output without logging potentially sensitive content.
                while !pipe.fileHandleForReading.readData(ofLength: 4096).isEmpty {}
            }
        } catch { startupFinished = true; startupTimer?.invalidate(); showError((error as? NativeFailure)?.message ?? error.localizedDescription) }
    }
    func showError(_ message: String) { let alert = NSAlert(); alert.messageText = "Conductor could not start"; alert.informativeText = message; alert.runModal() }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func applicationWillTerminate(_ notification: Notification) { startupTimer?.invalidate(); if let process, process.isRunning { process.terminate() } }
    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
        if url.scheme == "http", url.host == "127.0.0.1", url.port == serverPort { decisionHandler(.allow) } else { decisionHandler(.cancel) }
    }
}
if CommandLine.arguments.contains("--native") {
    var result: [String: Any]
    do { guard let line = readLine(), line.utf8.count <= 1048576, let data = line.data(using: .utf8), let input = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { try fail("Expected one JSON object on stdin") }; result = try native(input) }
    catch { result = ["ok": false, "error": (error as? NativeFailure)?.message ?? error.localizedDescription] }
    let data = try! JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]); FileHandle.standardOutput.write(data); FileHandle.standardOutput.write(Data([10]))
} else {
    let app = NSApplication.shared; let host = Host(); app.delegate = host; app.setActivationPolicy(.regular); app.run()
}
