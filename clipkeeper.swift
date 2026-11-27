// clipkeeper - saves the Mac's current clipboard to Macotron's clipboard list when you press Ctrl+Option+<key>.
//
// A small background app (no Dock icon). It registers one system-wide hotkey with the Carbon
// RegisterEventHotKey call, which needs NO macOS permission (no Accessibility, no Input Monitoring).
// On the hotkey it reads the pasteboard (text, or an image) and writes one JSON file per clip into
// the clips folder; Macotron's Clipboard tab lists those files. Macotron starts and restarts it.
//
//   clipkeeper --dir ~/.phone-proxy/clips [--keycode 10]
//
// Key codes: 10 = the key left of 1 on an ISO keyboard (the section sign), 50 = the same key on an
// ANSI keyboard (the grave accent).
//
// Build: swiftc -O -swift-version 5 -o clipkeeper clipkeeper.swift

import Cocoa
import Carbon

var args = Array(CommandLine.arguments.dropFirst())
func opt(_ name: String, _ def: String) -> String {
    if let i = args.firstIndex(of: "--" + name), i + 1 < args.count { return args[i + 1] }
    return def
}
let dir = opt("dir", NSHomeDirectory() + "/.phone-proxy/clips")
let keycode = UInt32(opt("keycode", "10")) ?? 10
let maxText = 1_000_000

try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)

func newestText() -> String? {                       // do not save the same text twice in a row
    guard let names = try? FileManager.default.contentsOfDirectory(atPath: dir) else { return nil }
    let newest = names.filter { $0.hasSuffix(".json") }.sorted().last
    guard let n = newest, let data = FileManager.default.contents(atPath: dir + "/" + n),
          let d = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
    return d["text"] as? String
}

func writeJSON(_ d: [String: Any], id: String) {
    guard let data = try? JSONSerialization.data(withJSONObject: d) else { return }
    try? data.write(to: URL(fileURLWithPath: dir + "/" + id + ".json"), options: .atomic)
}

func saveClip() {
    let pb = NSPasteboard.general
    let ms = Int(Date().timeIntervalSince1970 * 1000)
    let id = String(ms)
    if let s = pb.string(forType: .string), !s.isEmpty {
        if s == newestText() { NSSound(named: NSSound.Name("Funk"))?.play(); return }     // already the newest clip
        let t = s.count > maxText ? String(s.prefix(maxText)) : s
        writeJSON(["id": id, "t": Double(ms) / 1000, "kind": "text", "text": t, "len": s.count, "pinned": false], id: id)
        NSSound(named: NSSound.Name("Pop"))?.play()
        return
    }
    var png: Data? = pb.data(forType: .png)
    if png == nil, let tiff = pb.data(forType: .tiff), let rep = NSBitmapImageRep(data: tiff) {
        png = rep.representation(using: .png, properties: [:])
    }
    if let p = png {
        let file = id + ".png"
        try? p.write(to: URL(fileURLWithPath: dir + "/" + file))
        writeJSON(["id": id, "t": Double(ms) / 1000, "kind": "image", "file": file, "bytes": p.count, "pinned": false], id: id)
        NSSound(named: NSSound.Name("Pop"))?.play()
        return
    }
    NSSound(named: NSSound.Name("Basso"))?.play()      // nothing saveable on the clipboard
}

func writeStatus(_ registered: Bool, _ code: Int32) {
    let d: [String: Any] = ["pid": Int(getpid()), "registered": registered, "keycode": Int(keycode), "code": Int(code),
                            "at": Date().timeIntervalSince1970]
    if let data = try? JSONSerialization.data(withJSONObject: d) {
        try? data.write(to: URL(fileURLWithPath: dir + "/../clipkeeper.status.json"), options: .atomic)
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)

var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
InstallEventHandler(GetApplicationEventTarget(), { _, _, _ in
    saveClip()
    return noErr
}, 1, &spec, nil, nil)

var ref: EventHotKeyRef?
let hkid = EventHotKeyID(signature: OSType(0x434C4B50), id: 1)
let st = RegisterEventHotKey(keycode, UInt32(controlKey | optionKey), hkid, GetApplicationEventTarget(), 0, &ref)
writeStatus(st == noErr, Int32(st))

signal(SIGTERM, SIG_IGN)
let sigTerm = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
sigTerm.setEventHandler {
    if let r = ref { UnregisterEventHotKey(r) }
    exit(0)
}
sigTerm.resume()

app.run()
