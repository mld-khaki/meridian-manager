// tabinput - the Mac's keyboard and mouse on the tablet, like Input Director's edge crossing,
// WITHOUT any macOS permission (no Accessibility, no Input Monitoring, no admin).
//
// How: the helper is a small background app (no Dock icon). It keeps a 2 px invisible strip along
// the chosen outer edge of the Mac's screens. When the cursor is pushed into the strip, a tinted
// overlay covers that screen, the app becomes the active one and takes the Mac's mouse and keyboard
// as ITS OWN events (which needs no permission); each event goes, one line each, through an SSH
// tunnel (127.0.0.1:PORT) to inputd.py on the tablet. Moving the tablet's pointer back past the
// opposite edge, or pressing Ctrl+Option+Cmd+Esc, gives control back to the Mac.
// Limits of doing it without a permission: macOS-level shortcuts (Cmd+Tab, Cmd+Space, Mission
// Control keys) stay on the Mac, and switching to another app by other means ends the takeover.
// Nothing is taken over unless the tablet answered, and any send error gives control back at once.
//
//   tabinput run   --port N --token-file F --status F [--edge right|left|top|bottom] [--config F]
//                  [--trigger edge|top-right|top-left|bottom-right|bottom-left] [--corner-size 16]
//                  [--tw 2800 --th 1752] [--speed 1.0] [--delay 1.0] [--dim 0.22] [--cmd-as-ctrl 1] [--invert-scroll 0] [--paste-typing 1] [--esc-returns 1]
//                  [--return-trigger edge|top-left|top-right|bottom-left|bottom-right] [--return-size 500]
//   --config names Macotron's settings file; speed, delay, size, Cmd and scroll are re-read from it while running.
//   tabinput send --port N --token-file F "M 10 0"    one test line; prints the tablet's answer
//
// Build: swiftc -O -swift-version 5 -o tabinput tabinput.swift

import Cocoa
import CoreAudio
import Carbon
import Darwin

// MARK: - arguments

var args = Array(CommandLine.arguments.dropFirst())
let cmd = args.isEmpty ? "run" : args.removeFirst()

func opt(_ name: String, _ def: String) -> String {
    if let i = args.firstIndex(of: "--" + name), i + 1 < args.count { return args[i + 1] }
    return def
}

let port = UInt16(opt("port", "18096")) ?? 18096
let tokenFile = opt("token-file", "")
let statusFile = opt("status", "")
var cfgEdge = opt("edge", "right")
var trigger = opt("trigger", "edge")                       // "edge" = the whole edge cfgEdge, or a corner: top-right, top-left, bottom-right, bottom-left
var returnTrigger = opt("return-trigger", "edge")        // how control comes back from the tablet: "edge" (pointer past the far edge) or a tablet corner
var returnSize = Double(opt("return-size", "500")) ?? 500   // side of that tablet corner zone, in tablet pixels (a big zone forgives the estimate's drift)
var exitArmed = false                                      // a corner return only counts after the pointer has been outside the zone once
var shortcutKey = Int(opt("shortcut-key", "17")) ?? 17        // trigger "shortcut": Ctrl+Option+this key switches between the Mac and the tablet (17 = T)
var shortcutRef: EventHotKeyRef?
var shortcutHandlerInstalled = false
var entryCGPoint: CGPoint?                                  // where the Mac cursor was when the tablet took over (a shortcut brings it back there)
var lastFKey: [Int: Date] = [:]                             // F1 / F2 (or brightness down / up) presses, for the F1+F2 chord that gives control back
var cornerSize = Double(opt("corner-size", "16")) ?? 16    // side of the square corner zone, in screen points
var edge = effectiveEdge()                                 // the edge the tablet sits on: a right-hand corner means the right edge, a left-hand one the left
var tabW = Double(opt("tw", "2800")) ?? 2800
var tabH = Double(opt("th", "1752")) ?? 1752
var speed = Double(opt("speed", "1.0")) ?? 1.0
var dim = Double(opt("dim", "0.22")) ?? 0.22              // how dark the Mac screen goes while the tablet has control (0 = not at all)
var delay = Double(opt("delay", "1.0")) ?? 1.0            // seconds the cursor must stay at the edge (either way) before control changes hands
var cmdAsCtrl = opt("cmd-as-ctrl", "1") == "1"
var invertScroll = opt("invert-scroll", "0") == "1"
var triggerScreen = Int(opt("trigger-screen", "0")) ?? 0     // 0 = the outer edge of all monitors together; else the display id of the monitor whose own edge or corner is the trigger
var shortcutStatus: Int32 = 0                               // what RegisterEventHotKey answered (0 = registered)
var shortcutPresses = 0                                     // how many times the shortcut reached this helper
var escReturns = opt("esc-returns", "1") == "1"            // a plain Esc gives control back to the Mac (so it no longer reaches the tablet)
var pasteTyping = opt("paste-typing", "1") == "1"          // Cmd+V while the tablet has control types the Mac's clipboard text there
let configFile = opt("config", "")                         // Macotron's settings file: speed, delay and the rest are re-read while running
var configStamp = Date.distantPast

func effectiveEdge() -> String {
    trigger == "edge" || trigger == "shortcut" ? cfgEdge : (trigger.hasSuffix("right") ? "right" : "left")
}

func reloadConfig() {
    guard !configFile.isEmpty,
          let at = (try? FileManager.default.attributesOfItem(atPath: configFile))?[.modificationDate] as? Date,
          at != configStamp, let data = try? Data(contentsOf: URL(fileURLWithPath: configFile)),
          let d = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
    configStamp = at
    if let v = (d["speed"] as? NSNumber)?.doubleValue, v >= 0.1, v <= 10 { speed = v }
    if let v = (d["delay"] as? NSNumber)?.doubleValue, v >= 0, v <= 30 { delay = v }
    if let v = (d["dim"] as? NSNumber)?.doubleValue, v >= 0, v <= 0.9 { dim = v; applyDim() }
    if let v = (d["tw"] as? NSNumber)?.doubleValue, v >= 100 { tabW = v }
    if let v = (d["th"] as? NSNumber)?.doubleValue, v >= 100 { tabH = v }
    if let v = d["cmd_as_ctrl"] as? Bool { cmdAsCtrl = v }
    if let v = d["invert_scroll"] as? Bool { invertScroll = v }
    if let v = d["paste_typing"] as? Bool { pasteTyping = v }
    if let v = d["esc_returns"] as? Bool { escReturns = v }
    var screenChanged = false
    if let v = (d["trigger_screen"] as? NSNumber)?.intValue, v != triggerScreen { triggerScreen = v; screenChanged = true }
    if let v = d["return_trigger"] as? String, ["edge", "top-left", "top-right", "bottom-left", "bottom-right"].contains(v) { returnTrigger = v }
    if let v = (d["return_size"] as? NSNumber)?.doubleValue, v >= 50, v <= 3000 { returnSize = v }
    var changed = false
    if let v = d["edge"] as? String, ["right", "left", "top", "bottom"].contains(v), v != cfgEdge { cfgEdge = v; changed = true }
    if let v = d["trigger"] as? String, ["edge", "shortcut", "top-right", "top-left", "bottom-right", "bottom-left"].contains(v), v != trigger { trigger = v; changed = true }
    if let v = (d["shortcut_key"] as? NSNumber)?.intValue, [17, 122, 120, 96].contains(v), v != shortcutKey { shortcutKey = v; changed = true }
    if let v = (d["corner_size"] as? NSNumber)?.doubleValue, v >= 4, v <= 300, v != cornerSize { cornerSize = v; changed = true }
    if screenChanged { changed = true }
    if changed && !capturing {                             // a new trigger zone takes effect at once (not in the middle of a takeover)
        edge = effectiveEdge()
        cancelDwell()
        rebuildStrips()
        applyShortcut()
        writeStatus(sock >= 0 ? "idle" : "noconn", readyMsg())
    }
}

// MARK: - the connection to the tablet

func readToken() -> String {
    ((try? String(contentsOfFile: tokenFile, encoding: .utf8)) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
}

var sock: Int32 = -1

func closeSock() {
    if sock >= 0 { close(sock); sock = -1 }
}

func connectTablet() -> Bool {
    closeSock()
    let s = socket(AF_INET, SOCK_STREAM, 0)
    if s < 0 { return false }
    var addr = sockaddr_in()
    addr.sin_family = sa_family_t(AF_INET)
    addr.sin_port = port.bigEndian
    addr.sin_addr.s_addr = inet_addr("127.0.0.1")
    let rc = withUnsafePointer(to: &addr) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { connect(s, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) }
    }
    if rc != 0 { close(s); return false }
    var one: Int32 = 1
    setsockopt(s, IPPROTO_TCP, TCP_NODELAY, &one, socklen_t(MemoryLayout<Int32>.size))
    var tv = timeval(tv_sec: 2, tv_usec: 0)                  // the hello answer must come within 2 s
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
    let hello = "HELLO " + readToken() + "\n"
    if write(s, hello, hello.utf8.count) < 0 { close(s); return false }
    var buf = [UInt8](repeating: 0, count: 64)
    let n = read(s, &buf, 64)
    if n < 2 || String(decoding: buf[0..<n], as: UTF8.self).hasPrefix("OK") == false { close(s); return false }
    var none = timeval(tv_sec: 0, tv_usec: 0)
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &none, socklen_t(MemoryLayout<timeval>.size))
    let fl = fcntl(s, F_GETFL)
    _ = fcntl(s, F_SETFL, fl | O_NONBLOCK)
    sock = s
    return true
}

if cmd == "send" {
    guard connectTablet() else { print("not connected"); exit(1) }
    let line = (args.last ?? "P") + "\n"
    _ = write(sock, line, line.utf8.count)
    usleep(300_000)
    var buf = [UInt8](repeating: 0, count: 256)
    let n = read(sock, &buf, 256)
    print(n > 0 ? String(decoding: buf[0..<n], as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines) : "(no answer)")
    exit(0)
}

guard cmd == "run" else { print("usage: tabinput run|send"); exit(2) }

// MARK: - state

var capturing = false              // true while the tablet has the Mac's keyboard and mouse
var vx = 0.0, vy = 0.0             // where the tablet's pointer is, in tablet pixels
var pendDX = 0.0, pendDY = 0.0     // motion not sent yet
var scrollAccX = 0.0, scrollAccY = 0.0
var heldMods = Set<Int>()          // modifier key codes that are down on the tablet
var lastMsg = ""
var lastStatusAt = Date.distantPast
var entryScreen: NSScreen?
var cooldownUntil = Date.distantPast
var dwellTimer: Timer?                                     // the cursor sits in the edge strip: control moves to the tablet when it fires
var lastActivity = 0.0                                     // epoch seconds of the last event sent to the tablet (Macotron's Meeting profile follows it)
var activityDirty = false
var exitSince: Date?                                       // the tablet pointer sits past the far edge since then

func screenID(_ s: NSScreen) -> Int {
    (s.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.intValue ?? 0
}

func screensInfo() -> [[String: Any]] {                       // the monitors, left to right, for Macotron's "which monitor" choice
    NSScreen.screens.sorted { $0.frame.minX < $1.frame.minX }.map {
        ["id": screenID($0), "name": $0.localizedName, "x": Int($0.frame.minX), "w": Int($0.frame.width), "h": Int($0.frame.height),
         "main": $0 == NSScreen.screens.first]
    }
}

func writeStatus(_ state: String, _ msg: String = "") {
    lastMsg = msg
    guard !statusFile.isEmpty else { return }
    lastStatusAt = Date()
    let d: [String: Any] = ["state": state, "edge": edge, "trigger": trigger, "connected": sock >= 0, "last_activity": lastActivity,
                            "screens": screensInfo(), "shortcut_ok": shortcutStatus == 0, "shortcut_presses": shortcutPresses,
                            "pid": Int(getpid()), "msg": msg, "at": Date().timeIntervalSince1970]
    if let data = try? JSONSerialization.data(withJSONObject: d) {
        try? data.write(to: URL(fileURLWithPath: statusFile), options: .atomic)
    }
}

func readyMsg() -> String {
    if trigger == "shortcut" {
        if shortcutStatus != 0 { return "The shortcut Ctrl+Option+\(shortcutName()) could not be registered (macOS code \(shortcutStatus): another app may hold it). Choose another key." }
        return "Ready: press Ctrl+Option+\(shortcutName()) to hand the keyboard and mouse to the tablet, and the same keys (or Esc) to take them back. Presses seen so far: \(shortcutPresses)."
    }
    return trigger == "edge" ? "Ready: push the cursor off the \(edge) edge of the Mac screens."
                      : "Ready: move the cursor into the \(trigger) corner of the Mac screens."
}

// MARK: - sending

func sendRaw(_ s: String) -> Bool {
    guard sock >= 0 else { return false }
    let line = s + "\n"
    let n = line.withCString { write(sock, $0, strlen($0)) }
    if n < 0 && errno != EAGAIN { closeSock(); return false }
    return true
}

func drain() {                                  // the tablet answers each line with OK; read and drop it
    guard sock >= 0 else { return }
    var buf = [UInt8](repeating: 0, count: 4096)
    while true {
        let n = read(sock, &buf, 4096)
        if n > 0 { continue }
        if n == 0 { closeSock(); return }       // the tablet closed the connection
        if errno != EAGAIN { closeSock() }
        return
    }
}

func flushMove() -> Bool {
    let dx = Int(pendDX.rounded(.towardZero)), dy = Int(pendDY.rounded(.towardZero))
    if dx == 0 && dy == 0 { return true }
    pendDX -= Double(dx); pendDY -= Double(dy)
    return sendRaw("M \(dx) \(dy)")
}

func send(_ s: String) -> Bool {
    if !flushMove() { return false }
    return sendRaw(s)
}

// MARK: - key table: Mac virtual key code -> Linux key code

let keyMap: [Int: Int] = [
    0: 30, 1: 31, 2: 32, 3: 33, 4: 35, 5: 34, 6: 44, 7: 45, 8: 46, 9: 47, 11: 48, 12: 16, 13: 17, 14: 18, 15: 19,
    16: 21, 17: 20, 18: 2, 19: 3, 20: 4, 21: 5, 22: 7, 23: 6, 24: 13, 25: 10, 26: 8, 27: 12, 28: 9, 29: 11, 30: 27,
    31: 24, 32: 22, 33: 26, 34: 23, 35: 25, 36: 28, 37: 38, 38: 36, 39: 40, 40: 37, 41: 39, 42: 43, 43: 51, 44: 53,
    45: 49, 46: 50, 47: 52, 48: 15, 49: 57, 50: 41, 51: 14, 53: 1,
    122: 59, 120: 60, 99: 61, 118: 62, 96: 63, 97: 64, 98: 65, 100: 66, 101: 67, 109: 68, 103: 87, 111: 88,
    117: 111, 115: 102, 119: 107, 116: 104, 121: 109, 123: 105, 124: 106, 125: 108, 126: 103,
    76: 96, 82: 82, 83: 79, 84: 80, 85: 81, 86: 75, 87: 76, 88: 77, 89: 71, 91: 72, 92: 73, 65: 83, 67: 55, 69: 78, 78: 74, 75: 98, 81: 117,
]
// modifiers: Mac code -> (Linux code, device flag mask that says it is down)
let modMap: [Int: (Int, UInt)] = [
    55: (125, 0x08), 54: (126, 0x10),         // left/right command  -> meta (or ctrl, see cmdAsCtrl)
    56: (42, 0x02), 60: (54, 0x04),           // shift
    58: (56, 0x20), 61: (100, 0x40),          // option -> alt
    59: (29, 0x01), 62: (97, 0x2000),         // control
    57: (58, 0x00),                           // caps lock (a tap)
]

func linuxMod(_ mac: Int) -> Int {
    guard let m = modMap[mac] else { return 0 }
    if cmdAsCtrl && (mac == 55 || mac == 54) { return mac == 55 ? 29 : 97 }
    return m.0
}

// MARK: - the overlay and the edge strips

final class Overlay: NSWindow {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }
}

final class Strip: NSWindow {
    override var canBecomeKey: Bool { false }
}

final class StripView: NSView {
    var onEnter: ((NSScreen) -> Void)?
    var onExit: (() -> Void)?
    override func updateTrackingAreas() {
        for t in trackingAreas { removeTrackingArea(t) }
        addTrackingArea(NSTrackingArea(rect: bounds, options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect], owner: self, userInfo: nil))
    }
    override func mouseEntered(with event: NSEvent) {
        if let s = window?.screen { onEnter?(s) }
    }
    override func mouseExited(with event: NSEvent) { onExit?() }
}

var strips: [Strip] = []
var overlay: Overlay?
let banner = NSTextField(labelWithString: "")

func cgBounds(_ s: NSScreen) -> CGRect {
    let id = (s.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value ?? CGMainDisplayID()
    return CGDisplayBounds(CGDirectDisplayID(id))
}

// the screen that owns the chosen corner of the whole arrangement: on the outermost side, the highest (top) or lowest (bottom) one
func cornerScreen(_ all: [NSScreen]) -> NSScreen? {
    let right = trigger.hasSuffix("right"), top = trigger.hasPrefix("top")
    let outerX = right ? all.map { $0.frame.maxX }.max()! : all.map { $0.frame.minX }.min()!
    let outer = all.filter { abs((right ? $0.frame.maxX : $0.frame.minX) - outerX) < 1 }
    return top ? outer.max { $0.frame.maxY < $1.frame.maxY } : outer.min { $0.frame.minY < $1.frame.minY }
}

func cornerRect(_ f: NSRect) -> NSRect {
    let s = CGFloat(cornerSize)
    return NSRect(x: trigger.hasSuffix("right") ? f.maxX - s : f.minX, y: trigger.hasPrefix("top") ? f.maxY - s : f.minY, width: s, height: s)
}

func rebuildStrips() {
    for s in strips { s.orderOut(nil) }
    strips.removeAll()
    let every = NSScreen.screens
    guard !every.isEmpty else { return }
    let picked = every.filter { screenID($0) == triggerScreen }
    let all = (triggerScreen != 0 && !picked.isEmpty) ? picked : every   // one chosen monitor: its own edges, else all monitors together
    let uMinX = all.map { $0.frame.minX }.min()!, uMaxX = all.map { $0.frame.maxX }.max()!
    let uMinY = all.map { $0.frame.minY }.min()!, uMaxY = all.map { $0.frame.maxY }.max()!
    for scr in all {
        let f = scr.frame
        var r: NSRect?
        if trigger != "edge" {
            if trigger != "shortcut", let cs = cornerScreen(all), cs === scr { r = cornerRect(f) }
        } else {
            switch edge {
            case "right": if abs(f.maxX - uMaxX) < 1 { r = NSRect(x: f.maxX - 2, y: f.minY, width: 2, height: f.height) }
            case "left": if abs(f.minX - uMinX) < 1 { r = NSRect(x: f.minX, y: f.minY, width: 2, height: f.height) }
            case "top": if abs(f.maxY - uMaxY) < 1 { r = NSRect(x: f.minX, y: f.maxY - 2, width: f.width, height: 2) }
            default: if abs(f.minY - uMinY) < 1 { r = NSRect(x: f.minX, y: f.minY, width: f.width, height: 2) }
            }
        }
        guard let rect = r else { continue }
        let w = Strip(contentRect: rect, styleMask: .borderless, backing: .buffered, defer: false)
        w.level = .statusBar
        w.isOpaque = false
        w.backgroundColor = NSColor(white: 0, alpha: 0.004)   // nearly invisible, but still takes the mouse
        w.hasShadow = false
        w.ignoresMouseEvents = false
        w.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle, .fullScreenAuxiliary]
        let v = StripView(frame: NSRect(origin: .zero, size: rect.size))
        v.onEnter = { s in beginDwell(on: s) }
        v.onExit = { cancelDwell() }
        w.contentView = v
        w.setFrame(rect, display: true)
        w.orderFrontRegardless()
        strips.append(w)
    }
}

// MARK: - handing control over

func applyDim() {                                  // the tint on the Mac screen while the tablet has control; never fully clear, or the window would not take the mouse
    guard let o = overlay else { return }
    o.backgroundColor = NSColor(calibratedRed: 0.02, green: 0.08, blue: 0.14, alpha: max(0.004, CGFloat(dim)))
    banner.isHidden = dim < 0.03
}

func cancelDwell() {
    dwellTimer?.invalidate()
    dwellTimer = nil
}

func beginDwell(on screen: NSScreen) {
    guard !capturing, sock >= 0, Date() >= cooldownUntil else { return }
    cancelDwell()
    if delay <= 0 { startCapture(on: screen); return }
    dwellTimer = Timer.scheduledTimer(withTimeInterval: delay, repeats: false) { _ in
        dwellTimer = nil
        startCapture(on: screen)
    }
}

// Android keeps its pointer where it was last, but vx, vy assume it appears at the entry edge: push it into that edge
// (Android stops it there), then along the edge to the entry point. Without this the way back fired too early.
func slamTablet() {
    var dx = 0, dy = 0
    switch edge {
    case "right": dx = -1
    case "left": dx = 1
    case "top": dy = 1
    default: dy = -1
    }
    for _ in 0..<3 { _ = sendRaw("M \(dx * 3000) \(dy * 3000)") }
    if edge == "right" || edge == "left" {
        for _ in 0..<2 { _ = sendRaw("M 0 -3000") }
        _ = sendRaw("M 0 \(Int(vy))")
    } else {
        for _ in 0..<2 { _ = sendRaw("M -3000 0") }
        _ = sendRaw("M \(Int(vx)) 0")
    }
}

func startCapture(on screen: NSScreen) {
    guard !capturing, sock >= 0, Date() >= cooldownUntil else { return }
    let f = screen.frame
    let m = NSEvent.mouseLocation
    let frac: Double
    switch edge {
    case "right", "left": frac = max(0, min(1, Double((f.maxY - m.y) / f.height)))
    default: frac = max(0, min(1, Double((m.x - f.minX) / f.width)))
    }
    switch edge {                                  // the tablet's pointer appears at the opposite edge
    case "right": vx = 0; vy = frac * tabH
    case "left": vx = tabW; vy = frac * tabH
    case "top": vx = frac * tabW; vy = tabH
    default: vx = frac * tabW; vy = 0
    }
    pendDX = 0; pendDY = 0; scrollAccX = 0; scrollAccY = 0
    entryScreen = screen
    entryCGPoint = CGPoint(x: m.x, y: NSScreen.screens[0].frame.height - m.y)
    exitSince = nil
    exitArmed = false
    if overlay == nil {
        let o = Overlay(contentRect: f, styleMask: .borderless, backing: .buffered, defer: false)
        o.level = .screenSaver
        o.isOpaque = false
        o.hasShadow = false
        o.acceptsMouseMovedEvents = true
        o.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle, .fullScreenAuxiliary]
        banner.textColor = NSColor(white: 1, alpha: 0.9)
        banner.font = NSFont.systemFont(ofSize: 15, weight: .medium)
        banner.alignment = .center
        o.contentView = NSView(frame: f)
        o.contentView!.addSubview(banner)
        overlay = o
    }
    let o = overlay!
    applyDim()
    o.setFrame(f, display: true)
    banner.stringValue = "The tablet has your keyboard and mouse  -  Ctrl+Option+Cmd+Esc brings them back"
    banner.frame = NSRect(x: 0, y: 24, width: f.width, height: 24)
    o.orderFrontRegardless()
    NSApp.activate(ignoringOtherApps: true)
    o.makeKeyAndOrderFront(nil)
    CGAssociateMouseAndMouseCursorPosition(0)      // the cursor stays where it hit the edge
    NSCursor.hide()
    capturing = true
    noteMacAudio()
    slamTablet()
    lastActivity = Date().timeIntervalSince1970
    writeStatus("active", "The tablet has the keyboard and mouse. Ctrl+Option+Cmd+Esc gives them back.")
}

func endCapture(back: CGPoint?, why: String) {
    guard capturing else { return }
    capturing = false
    restoreMacAudio()
    savedVol = nil; savedMute = nil
    typeQueue = []; typeRestore = []; pasteVHeld = false
    exitSince = nil
    _ = flushMove()
    _ = sendRaw("U")
    heldMods.removeAll()
    CGAssociateMouseAndMouseCursorPosition(1)
    NSCursor.unhide()
    overlay?.orderOut(nil)
    cooldownUntil = Date().addingTimeInterval(0.8)
    let scr = entryScreen ?? NSScreen.main ?? NSScreen.screens[0]
    let b = cgBounds(scr)
    let inset = 80.0                               // well clear of the strip, so it does not trigger at once
    var p: CGPoint
    if let back = back { p = back } else {         // hotkey or error: near the edge it left from
        switch edge {
        case "right": p = CGPoint(x: b.maxX - inset, y: b.minY + max(0, min(1, vy / tabH)) * b.height)
        case "left": p = CGPoint(x: b.minX + inset, y: b.minY + max(0, min(1, vy / tabH)) * b.height)
        case "top": p = CGPoint(x: b.minX + max(0, min(1, vx / tabW)) * b.width, y: b.minY + inset)
        default: p = CGPoint(x: b.minX + max(0, min(1, vx / tabW)) * b.width, y: b.maxY - inset)
        }
    }
    if trigger == "shortcut", back == nil, let e0 = entryCGPoint { p = e0 }
    CGWarpMouseCursorPosition(p)
    writeStatus(sock >= 0 ? "idle" : "noconn", why)
}

func exitReturnPoint() -> CGPoint? {               // where the Mac cursor reappears when the tablet pointer left past the far edge
    if trigger == "shortcut" { return nil }           // in shortcut mode only the shortcut (or the hotkey) gives control back
    let b = cgBounds(entryScreen ?? NSScreen.main ?? NSScreen.screens[0])
    let inset = 80.0
    if returnTrigger != "edge" {                       // back by pushing into a tablet corner
        let left = returnTrigger.hasSuffix("left"), top = returnTrigger.hasPrefix("top")
        let inX = left ? vx <= returnSize : vx >= tabW - returnSize
        let inY = top ? vy <= returnSize : vy >= tabH - returnSize
        if !(inX && inY) { exitArmed = true; return nil }   // outside the zone: from now on a visit to it counts
        guard exitArmed else { return nil }                  // came in through the zone (entry at that corner): ignore until it was left
        let fx = max(0, min(1, vx / tabW)), fy = max(0, min(1, vy / tabH))
        switch edge {
        case "right": return CGPoint(x: b.maxX - inset, y: b.minY + fy * b.height)
        case "left": return CGPoint(x: b.minX + inset, y: b.minY + fy * b.height)
        case "top": return CGPoint(x: b.minX + fx * b.width, y: b.minY + inset)
        default: return CGPoint(x: b.minX + fx * b.width, y: b.maxY - inset)
        }
    }
    switch edge {
    case "right" where vx < 0: return CGPoint(x: b.maxX - inset, y: b.minY + max(0, min(1, vy / tabH)) * b.height)
    case "left" where vx > tabW: return CGPoint(x: b.minX + inset, y: b.minY + max(0, min(1, vy / tabH)) * b.height)
    case "top" where vy > tabH: return CGPoint(x: b.minX + max(0, min(1, vx / tabW)) * b.width, y: b.minY + inset)
    case "bottom" where vy < 0: return CGPoint(x: b.minX + max(0, min(1, vx / tabW)) * b.width, y: b.maxY - inset)
    default: return nil
    }
}

let dropped = "The tablet connection dropped; control is back on the Mac."

func shortcutName() -> String { [17: "T", 122: "F1", 120: "F2", 96: "F5"][shortcutKey] ?? "?" }

func fChord(_ id: Int) -> Bool {                   // F1 and F2 (or the brightness keys on them) pressed within 0.35 s of each other
    let now = Date()
    lastFKey[id] = now
    if let t = lastFKey[id == 1 ? 2 : 1], now.timeIntervalSince(t) < 0.35 { lastFKey = [:]; return true }
    return false
}

func toggleByShortcut() {
    shortcutPresses += 1
    if capturing { endCapture(back: nil, why: "Control is back on the Mac (shortcut)."); return }
    if sock < 0 { _ = connectTablet() }
    guard sock >= 0 else { writeStatus("noconn", "Shortcut pressed, but the tablet's input daemon does not answer."); return }
    let m = NSEvent.mouseLocation
    let scr = NSScreen.screens.first { NSMouseInRect(m, $0.frame, false) } ?? NSScreen.main ?? NSScreen.screens[0]
    startCapture(on: scr)
}

let shortcutCallback: EventHandlerUPP = { _, _, _ in
    toggleByShortcut()
    return noErr
}

func applyShortcut() {                             // Ctrl+Option+key as a system hotkey (needs no macOS permission)
    if let r = shortcutRef { UnregisterEventHotKey(r); shortcutRef = nil }
    shortcutStatus = 0
    guard trigger == "shortcut" else { return }
    if !shortcutHandlerInstalled {
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), shortcutCallback, 1, &spec, nil, nil)
        shortcutHandlerInstalled = true
    }
    let hid = EventHotKeyID(signature: OSType(0x54424C54), id: 1)
    shortcutStatus = RegisterEventHotKey(UInt32(shortcutKey), UInt32(controlKey | optionKey), hid, GetApplicationEventTarget(), 0, &shortcutRef)
}

func checkExit() {                                 // the tablet pointer is past the far edge: back to the Mac after the delay
    guard capturing else { return }
    if let back = exitReturnPoint() {
        if exitSince == nil { exitSince = Date() }
        if Date().timeIntervalSince(exitSince!) >= delay { endCapture(back: back, why: "Control is back on the Mac.") }
    } else {
        exitSince = nil
    }
}

// The Mac's media keys (volume, mute, brightness, play/next/previous) are not key presses: macOS sends them as "system defined"
// events with the key in data1. NX key type -> Linux key code (Android reads these as its own volume, media and brightness keys).
let mediaKeys: [Int: Int] = [0: 115, 1: 114, 7: 113, 2: 225, 3: 224, 16: 164, 17: 163, 18: 165, 19: 163, 20: 165]

// The volume keys still act on the Mac too (macOS cannot be stopped from seeing them without a permission), so the Mac's
// output volume and mute are noted when the tablet takes over and put back right after each key.
var savedVol: Float32?
var savedMute: UInt32?

func outputDevice() -> AudioDeviceID {
    var dev = AudioDeviceID(0)
    var size = UInt32(MemoryLayout<AudioDeviceID>.size)
    var a = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDefaultOutputDevice, mScope: kAudioObjectPropertyScopeGlobal,
                                       mElement: kAudioObjectPropertyElementMain)
    AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &a, 0, nil, &size, &dev)
    return dev
}

func volumeAddress() -> AudioObjectPropertyAddress {      // 'vmvc': the main volume of the output device
    AudioObjectPropertyAddress(mSelector: 0x766D7663, mScope: kAudioObjectPropertyScopeOutput, mElement: kAudioObjectPropertyElementMain)
}

func muteAddress() -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyMute, mScope: kAudioObjectPropertyScopeOutput, mElement: kAudioObjectPropertyElementMain)
}

func readVolume() -> Float32? {
    let dev = outputDevice()
    var a = volumeAddress()
    guard dev != 0, AudioObjectHasProperty(dev, &a) else { return nil }
    var v = Float32(0)
    var size = UInt32(MemoryLayout<Float32>.size)
    return AudioObjectGetPropertyData(dev, &a, 0, nil, &size, &v) == noErr ? v : nil
}

func readMute() -> UInt32? {
    let dev = outputDevice()
    var a = muteAddress()
    guard dev != 0, AudioObjectHasProperty(dev, &a) else { return nil }
    var v = UInt32(0)
    var size = UInt32(MemoryLayout<UInt32>.size)
    return AudioObjectGetPropertyData(dev, &a, 0, nil, &size, &v) == noErr ? v : nil
}

func writeVolume(_ value: Float32) {
    let dev = outputDevice()
    var a = volumeAddress()
    guard dev != 0, AudioObjectHasProperty(dev, &a) else { return }
    var v = value
    AudioObjectSetPropertyData(dev, &a, 0, nil, UInt32(MemoryLayout<Float32>.size), &v)
}

func writeMute(_ value: UInt32) {
    let dev = outputDevice()
    var a = muteAddress()
    guard dev != 0, AudioObjectHasProperty(dev, &a) else { return }
    var v = value
    AudioObjectSetPropertyData(dev, &a, 0, nil, UInt32(MemoryLayout<UInt32>.size), &v)
}

func noteMacAudio() {
    savedVol = readVolume()
    savedMute = readMute()
}

func restoreMacAudio() {
    if let v = savedVol, let now = readVolume(), abs(now - v) > 0.0005 { writeVolume(v) }
    if let m = savedMute, let now = readMute(), now != m { writeMute(m) }
}

// MARK: - Cmd+V: the Mac's clipboard typed on the tablet
// The tablet's clipboard cannot be reached from outside (Android keeps it from background programs), so the text is typed as
// key presses on a US keyboard layout. Characters with no key there (accents, emoji, non-Latin scripts) are skipped.

let charKeys: [Character: (Int, Bool)] = {
    var m: [Character: (Int, Bool)] = [:]
    let rows: [(String, [Int])] = [("qwertyuiop", Array(16...25)), ("asdfghjkl", Array(30...38)), ("zxcvbnm", Array(44...50))]
    for (letters, codes) in rows {
        for (i, c) in letters.enumerated() {
            m[c] = (codes[i], false)
            m[Character(c.uppercased())] = (codes[i], true)
        }
    }
    for (i, c) in "1234567890".enumerated() { m[c] = (i + 2, false) }
    for (i, c) in "!@#$%^&*()".enumerated() { m[c] = (i + 2, true) }
    let plain: [(Character, Int)] = [("-", 12), ("=", 13), ("[", 26), ("]", 27), (";", 39), ("'", 40), ("`", 41), ("\\", 43),
                                     (",", 51), (".", 52), ("/", 53), (" ", 57), ("\n", 28), ("\t", 15)]
    for (c, k) in plain { m[c] = (k, false) }
    let shifted: [(Character, Int)] = [("_", 12), ("+", 13), ("{", 26), ("}", 27), (":", 39), ("\"", 40), ("~", 41), ("|", 43),
                                       ("<", 51), (">", 52), ("?", 53)]
    for (c, k) in shifted { m[c] = (k, true) }
    m["\r\n"] = (28, false)                       // Swift treats CR LF as one character
    m["\r"] = (28, false)
    return m
}()

var typeQueue: [(Int, Bool)] = []                  // (Linux key code, needs shift) still to type
var typeRestore: [Int] = []                        // Cmd keys to press again on the tablet once the text is typed
var pasteVHeld = false                             // the V of a Cmd+V is down: its release is swallowed too

func startPasteTyping() {
    guard let text = NSPasteboard.general.string(forType: .string), !text.isEmpty else {
        writeStatus("active", "Cmd+V: the Mac clipboard has no text.")
        return
    }
    var skipped = 0
    var q: [(Int, Bool)] = []
    for ch in text.prefix(20000) {
        if let k = charKeys[ch] { q.append(k) } else { skipped += 1 }
    }
    if q.isEmpty { writeStatus("active", "Cmd+V: none of the \(skipped) characters can be typed on a US keyboard."); return }
    typeRestore = []
    for m in [55, 54] where heldMods.contains(m) {   // the tablet must not see Ctrl held while the text is typed
        _ = send("K \(linuxMod(m)) 0")
        typeRestore.append(m)
    }
    typeQueue = q
    writeStatus("active", "Cmd+V: typing \(q.count) characters on the tablet" + (skipped > 0 ? " (\(skipped) skipped: not on a US keyboard)." : "."))
}

func pumpTyping() {                                // called every 8 ms: a few characters at a time, so the tablet keeps up
    var n = 0
    while n < 2, !typeQueue.isEmpty {
        let (k, shift) = typeQueue.removeFirst()
        if shift { _ = sendRaw("K 42 1") }
        _ = sendRaw("K \(k) 1")
        _ = sendRaw("K \(k) 0")
        if shift { _ = sendRaw("K 42 0") }
        n += 1
    }
    if typeQueue.isEmpty && !typeRestore.isEmpty {
        for m in typeRestore where heldMods.contains(m) { _ = sendRaw("K \(linuxMod(m)) 1") }
        typeRestore = []
        writeStatus("active", "Cmd+V: done.")
    }
}

// MARK: - the captured events (the app's own events: no permission needed)

final class CaptureApp: NSApplication {
    override func sendEvent(_ e: NSEvent) {
        if capturing && handle(e) { return }
        super.sendEvent(e)
    }
}

func handle(_ e: NSEvent) -> Bool {
    drain()
    lastActivity = Date().timeIntervalSince1970
    activityDirty = true
    if sock < 0 { endCapture(back: nil, why: dropped); return true }
    switch e.type {
    case .mouseMoved, .leftMouseDragged, .rightMouseDragged, .otherMouseDragged:
        let dx = Double(e.deltaX), dy = Double(e.deltaY)
        pendDX += dx * speed; pendDY += dy * speed
        vx += dx * speed; vy += dy * speed
        vx = max(-1, min(tabW + 1, vx)); vy = max(-1, min(tabH + 1, vy))
        checkExit()
        return true

    case .leftMouseDown, .leftMouseUp, .rightMouseDown, .rightMouseUp, .otherMouseDown, .otherMouseUp:
        let down = e.type == .leftMouseDown || e.type == .rightMouseDown || e.type == .otherMouseDown
        let name: String
        switch e.type {
        case .leftMouseDown, .leftMouseUp: name = "left"
        case .rightMouseDown, .rightMouseUp: name = "right"
        default: name = e.buttonNumber == 2 ? "middle" : "left"
        }
        if !send("B \(name) \(down ? 1 : 0)") { endCapture(back: nil, why: dropped) }
        return true

    case .scrollWheel:
        var sy = Double(e.scrollingDeltaY), sx = Double(e.scrollingDeltaX)
        if !e.hasPreciseScrollingDeltas { sy *= 10; sx *= 10 }
        if invertScroll { sy = -sy; sx = -sx }
        scrollAccY += sy; scrollAccX += sx
        let ny = Int((scrollAccY / 14).rounded(.towardZero)), nx = Int((scrollAccX / 14).rounded(.towardZero))
        if ny != 0 || nx != 0 {
            scrollAccY -= Double(ny) * 14; scrollAccX -= Double(nx) * 14
            if !send("W \(nx) \(ny)") { endCapture(back: nil, why: dropped) }
        }
        return true

    case .keyDown, .keyUp:
        let code = Int(e.keyCode)
        let fl = e.modifierFlags
        if e.type == .keyDown && (code == 122 || code == 120) && fChord(code == 122 ? 1 : 2) {
            endCapture(back: nil, why: "Control is back on the Mac (F1+F2).")
            return true
        }
        if e.type == .keyDown && code == 53 && escReturns && fl.intersection([.control, .option, .command, .shift]).isEmpty {
            endCapture(back: nil, why: "Control is back on the Mac (Esc).")
            return true
        }
        if e.type == .keyDown && code == 53 && fl.contains(.control) && fl.contains(.option) && fl.contains(.command) {
            endCapture(back: nil, why: "Control is back on the Mac (hotkey).")
            return true
        }
        if pasteTyping && code == 9 {                       // V
            if e.type == .keyDown && fl.contains(.command) && !fl.contains(.shift) && !fl.contains(.option) && !fl.contains(.control) {
                if !e.isARepeat && typeQueue.isEmpty { pasteVHeld = true; startPasteTyping() }
                return true
            }
            if e.type == .keyUp && pasteVHeld { pasteVHeld = false; return true }
        }
        guard let lk = keyMap[code] else { return true }
        var v = e.type == .keyDown ? 1 : 0
        if v == 1 && e.isARepeat { v = 2 }
        if !send("K \(lk) \(v)") { endCapture(back: nil, why: dropped) }
        return true

    case .systemDefined:
        guard e.subtype.rawValue == 8 else { return false }           // 8 = the media keys
        let d = e.data1
        let key = (d & 0xFFFF0000) >> 16, state = (d & 0xFF00) >> 8, repeating = (d & 0x1) != 0
        guard let lk = mediaKeys[key], state == 0xA || state == 0xB else { return false }
        if state == 0xA && (key == 2 || key == 3) && fChord(key == 3 ? 1 : 2) {      // brightness down + up: the F1+F2 keys in their media mode
            endCapture(back: nil, why: "Control is back on the Mac (F1+F2).")
            return true
        }
        if !send("K \(lk) \(state == 0xB ? 0 : (repeating ? 2 : 1))") { endCapture(back: nil, why: dropped) }
        for ms in [40, 120, 300] { DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(ms)) { if capturing { restoreMacAudio() } } }
        return true

    case .flagsChanged:
        let code = Int(e.keyCode)
        guard let m = modMap[code] else { return true }
        let lk = linuxMod(code)
        if code == 57 {                                         // caps lock: a tap
            if !send("K \(lk) 1") || !send("K \(lk) 0") { endCapture(back: nil, why: dropped) }
            return true
        }
        let down = (e.modifierFlags.rawValue & m.1) != 0
        if down == heldMods.contains(code) { return true }
        if down { heldMods.insert(code) } else { heldMods.remove(code) }
        if !send("K \(lk) \(down ? 1 : 0)") { endCapture(back: nil, why: dropped) }
        return true

    default:
        return false                               // activation and system events still go through
    }
}

// MARK: - run

signal(SIGPIPE, SIG_IGN)
let app = CaptureApp.shared
app.setActivationPolicy(.accessory)

func cleanExit(_ code: Int32) {
    if capturing { endCapture(back: nil, why: "Stopped.") }
    CGAssociateMouseAndMouseCursorPosition(1)
    closeSock()
    writeStatus("stopped", "Stopped.")
    exit(code)
}
signal(SIGTERM, SIG_IGN)
signal(SIGINT, SIG_IGN)
let sigTerm = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
sigTerm.setEventHandler { cleanExit(0) }
sigTerm.resume()
let sigInt = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
sigInt.setEventHandler { cleanExit(0) }
sigInt.resume()

NotificationCenter.default.addObserver(forName: NSApplication.didResignActiveNotification, object: nil, queue: .main) { _ in
    if capturing { endCapture(back: nil, why: "Control is back on the Mac (another app took focus).") }
}
NotificationCenter.default.addObserver(forName: NSApplication.didChangeScreenParametersNotification, object: nil, queue: .main) { _ in
    if capturing { endCapture(back: nil, why: "The screens changed; control is back on the Mac.") }
    rebuildStrips()
}

// every 8 ms: send the motion that has piled up; every second: drain, reconnect when needed, refresh the status
Timer.scheduledTimer(withTimeInterval: 0.008, repeats: true) { _ in
    guard capturing else { return }
    if !flushMove() { endCapture(back: nil, why: dropped); return }
    if !typeQueue.isEmpty || !typeRestore.isEmpty { pumpTyping() }
    if exitSince != nil { checkExit() }            // the pointer may be resting past the edge: no events arrive
    if capturing && activityDirty && Date().timeIntervalSince(lastStatusAt) >= 1 {
        activityDirty = false
        writeStatus("active", lastMsg)
    }
}
Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { _ in reloadConfig() }
var lastTry = Date.distantPast
Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { _ in
    drain()
    if capturing { return }
    if sock < 0 {
        if Date().timeIntervalSince(lastTry) > 2 {
            lastTry = Date()
            if connectTablet() { writeStatus("idle", readyMsg()) } else { writeStatus("noconn", "The tablet's input daemon does not answer.") }
        }
    } else if Date().timeIntervalSince(lastStatusAt) > 3 || lastMsg != readyMsg() {
        lastStatusAt = Date()
        writeStatus("idle", readyMsg())
    }
}
reloadConfig()
applyShortcut()
if connectTablet() { writeStatus("idle", readyMsg()) } else { writeStatus("noconn", "The tablet's input daemon does not answer.") }
rebuildStrips()
app.run()
