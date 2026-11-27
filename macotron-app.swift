// Macotron's app window: the phone-proxy-web.py page in a WebKit window of its own (own Dock icon,
// no browser bars). phone-proxy-web.py builds this file into ~/.phone-proxy/Macotron.app on first
// use, and again whenever it changes, then opens it with `open -a Macotron.app <url>`. Started any
// other way (Dock, Finder) it opens the port it showed last, with the token in
// ~/.phone-proxy/web.token, and when nothing listens there it starts the server itself, once, the
// way ~/.phone-proxy/window.json says. Links to other sites open in the default browser, a link
// that opens a window of the page's own gets a second app window, and closing the last window
// quits the app; the server keeps running.
import AppKit
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate {
    let defaults = UserDefaults.standard
    let state = NSHomeDirectory() + "/.phone-proxy"
    var main: NSWindow!
    var web: WKWebView!
    var home: URL?                 // the page; other origins go to the default browser
    var down = false               // the "not answering" page is showing, a retry is due
    var retrying = false           // and that retry is scheduled
    var bare = false               // opened without a URL (Dock, Finder)
    var mayStart = false           // so it may start the server: once, before the page first answers
    var server: Process?           // the server it started, until that is seen to have ended
    var extra = Set<NSWindow>()    // windows the page opened (its "Open in own window" link)

    func applicationWillFinishLaunching(_ note: Notification) {
        NSAppleEventManager.shared().setEventHandler(self, andSelector: #selector(getURL(_:withReplyEvent:)),
            forEventClass: AEEventClass(kInternetEventClass), andEventID: AEEventID(kAEGetURL))
        NSWindow.allowsAutomaticWindowTabbing = false
        NSApp.mainMenu = menu()
        NSApp.applicationIconImage = icon()
        web = makeWebView(WKWebViewConfiguration())
        let z = defaults.double(forKey: "zoom")
        if z > 0 { web.pageZoom = z }
        main = window(web, autosave: "Macotron")
    }

    func applicationDidFinishLaunching(_ note: Notification) {
        if home == nil {   // no URL came with the launch: opened from the Dock or Finder
            let arg = CommandLine.arguments.dropFirst().first { $0.hasPrefix("http") }
            bare = arg == nil
            mayStart = bare
            show(arg.flatMap { URL(string: $0) } ?? localURL())
        }
        main.makeKeyAndOrderFront(nil)
        NSApp.activate()
    }

    func localURL() -> URL? {   // the port shown last (8090 at first) and the token on disk
        let port = defaults.integer(forKey: "port")
        let token = (try? String(contentsOfFile: state + "/web.token", encoding: .utf8))?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return URL(string: "http://localhost:\(port > 0 ? port : 8090)/?t=\(token)")
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { true }
    func applicationSupportsSecureRestorableState(_ app: NSApplication) -> Bool { true }

    @objc func getURL(_ event: NSAppleEventDescriptor, withReplyEvent reply: NSAppleEventDescriptor) {
        bare = false   // sent by a running server's script
        mayStart = false
        show(event.paramDescriptor(forKeyword: AEKeyword(keyDirectObject))?.stringValue.flatMap { URL(string: $0) })
        main.makeKeyAndOrderFront(nil)
    }

    // The same URL again only brings the window forward, so a second run does not reset the page.
    func show(_ url: URL?) {
        guard let url, url != home || down else { return }
        home = url
        if let p = url.port { defaults.set(p, forKey: "port") }
        down = false
        retry()
    }

    func retry() {
        if let home { web.load(URLRequest(url: home)) }
    }

    func makeWebView(_ conf: WKWebViewConfiguration) -> WKWebView {
        let v = WKWebView(frame: .zero, configuration: conf)
        v.navigationDelegate = self
        v.uiDelegate = self
        v.allowsMagnification = true
        v.isInspectable = true     // Safari > Develop > this Mac > Macotron, to debug the page
        return v
    }

    func window(_ v: WKWebView, autosave: String) -> NSWindow {
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1200, height: 860),
                         styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        w.title = "Macotron"
        w.contentView = v
        w.isReleasedWhenClosed = false
        w.delegate = self
        w.center()
        _ = w.setFrameAutosaveName(autosave)   // and back where it was last time
        return w
    }

    func windowWillClose(_ note: Notification) {
        if let w = note.object as? NSWindow { extra.remove(w) }
    }

    // ---- the page -----------------------------------------------------------
    func isHome(_ u: URL) -> Bool {   // same scheme, host and port; localhost and 127.0.0.1 are one
        func host(_ u: URL?) -> String? { u?.host == "127.0.0.1" ? "localhost" : u?.host }
        return home != nil && u.scheme == home?.scheme && u.port == home?.port && host(u) == host(home)
    }

    func webView(_ v: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        // a link in the page itself to another site goes to the default browser; frames (the Tablet
        // tab shows the tablet's own page) keep their links
        if action.navigationType == .linkActivated, action.targetFrame?.isMainFrame == true,
           let u = action.request.url, !isHome(u) {
            NSWorkspace.shared.open(u)
            decisionHandler(.cancel)
        } else {
            decisionHandler(.allow)
        }
    }

    func webView(_ v: WKWebView, didCommit nav: WKNavigation!) {
        if v === web, v.url.map(isHome) == true {   // the server answered; a later stop is someone's choice
            down = false
            mayStart = false
        }
    }

    func webView(_ v: WKWebView, didFinish nav: WKNavigation!) {
        if let t = v.title, !t.isEmpty { v.window?.title = t }
    }

    // Server not answering (not started yet, stopped, being replaced): say so and retry every 2 s.
    // Opened from the Dock and refused (nothing listens on the port), it first starts the server.
    func webView(_ v: WKWebView, didFailProvisionalNavigation nav: WKNavigation!, withError error: Error) {
        let e = error as NSError
        guard v === web, e.domain == NSURLErrorDomain, e.code != NSURLErrorCancelled, home != nil else { return }
        if mayStart && e.code == NSURLErrorCannotConnectToHost {
            showDown(startServer())
        } else if !down {
            showDown("Start it with: phone-proxy.sh web, or python3 phone-proxy-web.py")
        }
        scheduleRetry()
    }

    func showDown(_ note: String) {
        down = true
        let at = "\(home?.host ?? "localhost"):\(home?.port ?? 80)"
        let text = note.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;")
        web.loadHTMLString("""
            <html><body style="margin:0;height:90vh;display:flex;align-items:center;justify-content:center;
            text-align:center;font:15px -apple-system,sans-serif;color:#999;background:#16181d">
            <div><b style="color:#ddd">Macotron is not answering on \(at)</b><br><br>
            \(text)<br>Trying again every 2 seconds.</div>
            </body></html>
            """, baseURL: nil)
    }

    func scheduleRetry() {   // one at a time, however many loads fail meanwhile
        guard !retrying else { return }
        retrying = true
        Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 2_000_000_000)
            self?.retrying = false
            self?.retryIfDown()
        }
    }

    func retryIfDown() {
        guard down else { return }
        if let s = server, !s.isRunning {   // the server it started has ended
            server = nil
            if s.terminationStatus != 0 {
                showDown("The server it started stopped (exit \(s.terminationStatus)); see ~/.phone-proxy/web.log")
                return scheduleRetry()   // a load now would cancel that page before it shows
            }
        }
        if bare, let u = localURL() { home = u }   // a server started just now may have made the token
        retry()
    }

    // Start the server the way phone-proxy-web.py last noted it in window.json: its python, its path
    // and the terminal's PATH (an app gets a bare one, without Homebrew). --keep leaves alone a copy
    // that turned up meanwhile. Macotron becomes that server's responsible app, so macOS may ask once
    // whether Macotron may use the Documents folder, the local network and Edge.
    func startServer() -> String {
        mayStart = false
        guard let data = FileManager.default.contents(atPath: state + "/window.json"),
              let rec = (try? JSONSerialization.jsonObject(with: data)) as? [String: String],
              let python = rec["python"], let script = rec["script"] else {
            return "Nothing noted to start it with yet: run phone-proxy-web.py once from a terminal."
        }
        let fd = Darwin.open(state + "/web.log", O_WRONLY | O_APPEND | O_CREAT, 0o644)
        let log = fd >= 0 ? FileHandle(fileDescriptor: fd, closeOnDealloc: true) : FileHandle.nullDevice
        var env = ProcessInfo.processInfo.environment
        if let path = rec["path"], !path.isEmpty { env["PATH"] = path }
        if env["LANG"] == nil { env["LANG"] = "en_US.UTF-8" }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: python)
        p.arguments = [script, String(home?.port ?? 8090), "--headless", "--keep"]
        p.environment = env
        p.standardInput = FileHandle.nullDevice
        p.standardOutput = log
        p.standardError = log
        do {
            try p.run()
        } catch {
            return "Could not start \(script): \(error.localizedDescription)"
        }
        server = p
        return "Starting it: \((script as NSString).lastPathComponent) --headless, output in ~/.phone-proxy/web.log"
    }

    func webViewWebContentProcessDidTerminate(_ v: WKWebView) { v.reload() }

    func webView(_ v: WKWebView, createWebViewWith conf: WKWebViewConfiguration, for action: WKNavigationAction,
                 windowFeatures: WKWindowFeatures) -> WKWebView? {
        guard let u = action.request.url else { return nil }
        if !isHome(u) {
            NSWorkspace.shared.open(u)
            return nil
        }
        let nv = makeWebView(conf)
        let w = window(nv, autosave: "Macotron.extra")
        extra.insert(w)
        w.makeKeyAndOrderFront(nil)
        return nv
    }

    func webViewDidClose(_ v: WKWebView) { v.window?.close() }

    func webView(_ v: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        ask(v, message, ["OK"]) { _ in completionHandler() }
    }

    func webView(_ v: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        ask(v, message, ["OK", "Cancel"]) { completionHandler($0 == .alertFirstButtonReturn) }
    }

    func webView(_ v: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String, defaultText: String?,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (String?) -> Void) {
        let field = NSTextField(string: defaultText ?? "")
        field.frame = NSRect(x: 0, y: 0, width: 300, height: 24)
        ask(v, prompt, ["OK", "Cancel"], field) { completionHandler($0 == .alertFirstButtonReturn ? field.stringValue : nil) }
    }

    func ask(_ v: WKWebView, _ text: String, _ buttons: [String], _ field: NSTextField? = nil,
             _ done: @escaping (NSApplication.ModalResponse) -> Void) {
        let a = NSAlert()
        a.messageText = "Macotron"
        a.informativeText = text
        buttons.forEach { a.addButton(withTitle: $0) }
        a.accessoryView = field
        guard let w = v.window ?? main else { return done(a.runModal()) }
        a.beginSheetModal(for: w, completionHandler: done)
        if let field { a.window.makeFirstResponder(field) }
    }

    // ---- menus and the Dock icon ---------------------------------------------
    var current: WKWebView { NSApp.keyWindow?.contentView as? WKWebView ?? web }

    @objc func reload() { if current === web && down { retryIfDown() } else { current.reload() } }
    @objc func zoomIn() { zoom(current.pageZoom + 0.1) }
    @objc func zoomOut() { zoom(current.pageZoom - 0.1) }
    @objc func zoomReset() { zoom(1) }

    func zoom(_ z: CGFloat) {
        current.pageZoom = min(3, max(0.5, z))
        if current === web { defaults.set(Double(current.pageZoom), forKey: "zoom") }
    }

    func menu() -> NSMenu {
        let bar = NSMenu()
        func add(_ title: String, _ items: [NSMenuItem]) -> NSMenu {
            let m = NSMenu(title: title)
            items.forEach { m.addItem($0) }
            let top = NSMenuItem(title: title, action: nil, keyEquivalent: "")
            top.submenu = m
            bar.addItem(top)
            return m
        }
        func item(_ title: String, _ action: Selector, _ key: String,
                  _ mods: NSEvent.ModifierFlags = .command, to target: AnyObject? = nil) -> NSMenuItem {
            let i = NSMenuItem(title: title, action: action, keyEquivalent: key)
            i.keyEquivalentModifierMask = mods
            i.target = target
            return i
        }
        _ = add("Macotron", [
            item("Hide Macotron", #selector(NSApplication.hide(_:)), "h"),
            item("Hide Others", #selector(NSApplication.hideOtherApplications(_:)), "h", [.command, .option]),
            item("Show All", #selector(NSApplication.unhideAllApplications(_:)), "", []),
            .separator(),
            item("Quit Macotron", #selector(NSApplication.terminate(_:)), "q")])
        _ = add("Edit", [   // without these, Cmd-C/V/X/A/Z do nothing in a web view
            item("Undo", Selector(("undo:")), "z"),
            item("Redo", Selector(("redo:")), "z", [.command, .shift]),
            .separator(),
            item("Cut", #selector(NSText.cut(_:)), "x"),
            item("Copy", #selector(NSText.copy(_:)), "c"),
            item("Paste", #selector(NSText.paste(_:)), "v"),
            item("Select All", #selector(NSText.selectAll(_:)), "a")])
        _ = add("View", [
            item("Reload Page", #selector(reload), "r", to: self),
            .separator(),
            item("Actual Size", #selector(zoomReset), "0", to: self),
            item("Zoom In", #selector(zoomIn), "+", to: self),
            item("Zoom Out", #selector(zoomOut), "-", to: self)])
        NSApp.windowsMenu = add("Window", [
            item("Minimize", #selector(NSWindow.performMiniaturize(_:)), "m"),
            item("Zoom", #selector(NSWindow.performZoom(_:)), "", []),
            .separator(),
            item("Close", #selector(NSWindow.performClose(_:)), "w")])
        return bar
    }

    func icon() -> NSImage {
        NSImage(size: NSSize(width: 512, height: 512), flipped: false) { r in
            let tile = NSBezierPath(roundedRect: r.insetBy(dx: 50, dy: 50), xRadius: 92, yRadius: 92)
            NSGradient(starting: NSColor(srgbRed: 0.20, green: 0.50, blue: 0.98, alpha: 1),
                       ending: NSColor(srgbRed: 0.07, green: 0.22, blue: 0.60, alpha: 1))?.draw(in: tile, angle: -90)
            let conf = NSImage.SymbolConfiguration(pointSize: 190, weight: .semibold)
                .applying(NSImage.SymbolConfiguration(paletteColors: [.white]))
            if let s = NSImage(systemSymbolName: "antenna.radiowaves.left.and.right", accessibilityDescription: nil)?
                .withSymbolConfiguration(conf) {
                s.draw(in: NSRect(x: r.midX - s.size.width / 2, y: r.midY - s.size.height / 2,
                                  width: s.size.width, height: s.size.height))
            }
            return true
        }
    }
}

MainActor.assumeIsolated {
    let delegate = AppDelegate()
    NSApplication.shared.delegate = delegate
    withExtendedLifetime(delegate) { NSApplication.shared.run() }
}
