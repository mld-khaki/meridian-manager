"""Optional, explicitly selected wireless ADB backend. No root or arbitrary shell API.

Pairing codes go to adb's stdin and are never saved or logged. A connected device's hardware
serial is pinned per companion profile; reconnecting to a different phone requires confirmation.
Snapshots are refreshed off the HTTP thread every 15 seconds. Android VPN status is not a
claim that Tailscale peers are reachable. scrcpy runs in its own window and logs locally.
"""
import ipaddress
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time


PACKAGE = "com.tailscale.ipn"
TICK = 15


def endpoint(value):
    if not isinstance(value, str):
        raise ValueError("Enter the wireless debugging IPv4 address:port.")
    match = re.fullmatch(r"([0-9.]+):([0-9]{1,5})", value.strip())
    if not match:
        raise ValueError("Enter the wireless debugging IPv4 address:port.")
    try:
        ip = ipaddress.IPv4Address(match[1])
    except ipaddress.AddressValueError as e:
        raise ValueError("Invalid phone IPv4 address.") from e
    port = int(match[2])
    if ip.is_unspecified or ip.is_multicast or not 1 <= port <= 65535:
        raise ValueError("Invalid phone address or port.")
    return "%s:%d" % (ip, port)


def battery_status(text):
    def number(key):
        m = re.search(r"^\s*" + re.escape(key) + r":\s*(-?\d+)\s*$", text, re.M)
        return int(m[1]) if m else None

    level, scale, temp = number("level"), number("scale"), number("temperature")
    if level is None or scale is None or scale <= 0 or not 0 <= level <= scale:
        raise ValueError("Android did not return a valid battery level/scale.")
    plugged = re.findall(r"^\s*(?:AC|USB|Wireless|Dock) powered:\s*(true|false)\s*$", text, re.M)
    return {"percent": round(level * 100 / scale, 1), "temperature_c": temp / 10 if temp is not None else None,
            "powered": any(x == "true" for x in plugged) if plugged else None,
            "status": {1: "unknown", 2: "charging", 3: "discharging", 4: "not charging", 5: "full"}.get(number("status"), "unknown")}


def vpn_status(text, uid):
    """Only attribute a VPN to Tailscale when Android supplies its owner UID."""
    if "Current Networks:" not in text and "NetworkAgentInfo" not in text:
        return None
    unknown_owner = False
    for block in re.split(r"(?=NetworkAgentInfo)", text)[1:]:
        if not re.search(r"Transports:\s*VPN\b|TRANSPORT_VPN", block):
            continue
        owner = re.search(r"(?:OwnerUid:\s*|ownerUid[=:]\s*)(\d+)", block)
        if not owner:
            unknown_owner = True
        elif int(owner[1]) == uid:
            return True
    return None if unknown_owner else False


def component(text):
    return next((line.strip() for line in text.splitlines()
                 if re.fullmatch(r"com\.tailscale\.ipn/[A-Za-z0-9_.$]+", line.strip())), None)


class PhoneADB:
    def __init__(self, state_dir, active_device, log):
        self.path = os.path.join(state_dir, "adb.json")
        self.state_dir, self.active_device, self.log = state_dir, active_device, log
        self.lock = threading.RLock()
        self.state_lock = threading.Lock()   # cached HTTP snapshots never wait for phone commands
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.scrcpy = None
        self.scrcpy_device = None
        self.cache = {}
        try:
            with open(self.path) as f:
                self.profiles = json.load(f)
            if not isinstance(self.profiles, dict):
                raise ValueError("ADB settings must be an object.")
            for cfg in self.profiles.values():
                if not isinstance(cfg, dict) or not isinstance(cfg.get("on"), bool):
                    raise ValueError("Invalid ADB profile.")
                endpoint(cfg.get("endpoint"))
                if not isinstance(cfg.get("serial"), str) or not cfg["serial"]:
                    raise ValueError("Invalid ADB device identity.")
        except FileNotFoundError:
            self.profiles = {}

    def save(self, profiles):
        os.makedirs(self.state_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".adb-", dir=self.state_dir)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(profiles, f, indent=2)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        with self.state_lock:
            self.profiles = profiles

    def publish(self, name, state):
        with self.state_lock:
            self.cache[name] = state

    def run(self, args, serial=None, input_text=None, timeout=10):
        adb = shutil.which("adb")
        if not adb:
            raise ValueError("adb is not installed. Install Android platform tools: brew install --cask android-platform-tools")
        argv = [adb] + (["-s", serial] if serial else []) + args
        try:
            r = subprocess.run(argv, input=input_text, stdin=None if input_text is not None else subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            # Never include the subprocess exception's arguments/input in pairing errors.
            raise ValueError("ADB command could not finish (%s)." % type(e).__name__) from e
        text = (r.stdout + "\n" + r.stderr).strip()
        if r.returncode or re.search(r"(?im)^\s*(?:error:|error\b|failure\b|failed\b|permission denial|.*SecurityException)", text):
            if input_text is not None:
                raise ValueError("ADB pairing failed. Check the pairing address/code and try again.")
            raise ValueError(text[-600:] or "ADB command failed (exit %d)." % r.returncode)
        return r.stdout.strip()

    def shell(self, serial, *args):
        # adb joins these arguments into a remote shell command.
        return self.run(["shell", *(shlex.quote(arg) for arg in args)], serial=serial)

    def identity(self, serial):
        if self.run(["get-state"], serial=serial) != "device":
            raise ValueError("The selected phone is not authorized or connected.")
        identity = self.shell(serial, "getprop", "ro.serialno").strip()
        if not identity or identity.lower() == "unknown":
            raise ValueError("Android did not provide a hardware serial; cannot safely pin this phone.")
        return identity

    def require_phone(self, name):
        cfg = self.profiles.get(name)
        if not cfg or not cfg["on"]:
            raise ValueError("Connect wireless ADB for this companion profile first.")
        serial = cfg["endpoint"]
        if self.identity(serial) != cfg["serial"]:
            raise ValueError("This address now belongs to a different phone. Use Connect and confirm replacement.")
        return serial

    def snapshot(self):
        with self.state_lock:
            name, dev = self.active_device()
            cfg = self.profiles.get(name, {})
            state = dict(self.cache.get(name, {"connected": False, "message": "Waiting for the first status check." if cfg.get("on") else "ADB monitoring is off."}))
            state.update(device=name, label=dev.get("label", name), on=cfg.get("on", False),
                         endpoint=cfg.get("endpoint", ""), adb_installed=bool(shutil.which("adb")),
                         scrcpy_installed=bool(shutil.which("scrcpy")),
                         scrcpy_running=bool(self.scrcpy and self.scrcpy.poll() is None and self.scrcpy_device == name),
                         scrcpy_log=os.path.join(self.state_dir, "scrcpy.log"), tick=TICK)
            if self.scrcpy and self.scrcpy_device == name and self.scrcpy.poll() not in (None, 0):
                state["scrcpy_error"] = "scrcpy stopped (exit %s). See ~/.phone-proxy/scrcpy.log." % self.scrcpy.returncode
            return state

    def refresh(self):
        with self.lock:
            name, _ = self.active_device()
            cfg = self.profiles.get(name)
            if not cfg or not cfg["on"]:
                self.publish(name, {"connected": False, "message": "ADB monitoring is off.", "checked": time.time()})
                return
            state = {"connected": False, "checked": time.time(), "errors": {}, "capabilities": {}}
            try:
                if self.run(["get-state"], serial=cfg["endpoint"]) != "device":
                    raise ValueError("Device is not connected.")
            except ValueError:
                try:
                    self.run(["connect", cfg["endpoint"]])
                except ValueError as e:
                    state["message"] = str(e)
                    self.publish(name, state)
                    return
            try:
                serial = self.require_phone(name)
            except ValueError as e:
                state["message"] = str(e)
                self.publish(name, state)
                return
            state.update(connected=True, message="Connected; phone status checked every 15 s.")

            def query(key, *args):
                try:
                    return self.shell(serial, *args)
                except ValueError as e:
                    state["errors"][key] = str(e)
                    return None

            text = query("battery", "dumpsys", "battery")
            if text is not None:
                try:
                    state["battery"] = battery_status(text)
                except ValueError as e:
                    state["errors"]["battery"] = str(e)
            packages = query("tailscale", "cmd", "package", "list", "packages", "-U", PACKAGE)
            match = re.search(r"package:com\.tailscale\.ipn\s+uid:(\d+)", packages or "")
            state["tailscale"] = {"installed": bool(match) if packages is not None else None, "vpn_active": None}
            if match:
                connections = query("vpn", "dumpsys", "connectivity")
                if connections is not None:
                    state["tailscale"]["vpn_active"] = vpn_status(connections, int(match[1]))
                pid = query("tailscale_process", "sh", "-c", "pidof com.tailscale.ipn || true")
                state["tailscale"]["process_running"] = bool(pid) if pid is not None else None
                for op, intent in (("tailscale_on", "CONNECT_VPN"), ("tailscale_off", "DISCONNECT_VPN")):
                    action = PACKAGE + "." + intent
                    activity = component(query(op, "cmd", "package", "resolve-activity", "--brief",
                                               "-a", action, "-p", PACKAGE) or "")
                    receiver = None if activity else component(query(op, "cmd", "package", "query-receivers",
                                                                      "--brief", "--components", "-a", action, "-p", PACKAGE) or "")
                    state["capabilities"][op] = {"kind": "activity" if activity else "receiver",
                                                "component": activity or receiver} if activity or receiver else None
            brightness = query("brightness", "settings", "get", "system", "screen_brightness")
            state["brightness"] = int(brightness) if brightness and brightness.isdigit() else None
            self.publish(name, state)

    def operation(self, body):
        if not isinstance(body, dict):
            raise ValueError("ADB request must be a JSON object.")
        op = body.get("op")
        with self.lock:
            name, _ = self.active_device()
            if body.get("device") != name:
                raise ValueError("Companion profile changed; refresh before using ADB controls.")
            if op == "pair":
                addr = endpoint(body.get("pair_endpoint"))
                code = body.get("code", "")
                if not isinstance(code, str) or not re.fullmatch(r"\d{6}", code):
                    raise ValueError("Enter the six-digit pairing code from the phone.")
                text = self.run(["pair", addr], input_text=code + "\n", timeout=30)
                if "Successfully paired" not in text:
                    raise ValueError("ADB did not confirm pairing. Check the phone and try again.")
                return {"ok": True, "message": "Paired. Now connect using the main Wireless debugging address (a different port)."}
            if op == "connect":
                addr = endpoint(body.get("endpoint"))
                self.run(["connect", addr])
                identity = self.identity(addr)
                previous = self.profiles.get(name)
                if previous and previous["serial"] != identity and body.get("replace") is not True:
                    return {"ok": False, "replace_required": True, "message": "This is a different phone. Replace the device pinned to this profile?"}
                self.save(dict(self.profiles, **{name: {"on": True, "endpoint": addr, "serial": identity}}))
                with self.state_lock:
                    self.cache.pop(name, None)
                self.wake.set()
                self.log("ADB connected for companion profile " + name)
                return {"ok": True, "message": "Connected. Checking phone status."}
            if op in ("off", "disconnect"):
                cfg = self.profiles.get(name)
                if cfg:
                    self.save(dict(self.profiles, **{name: dict(cfg, on=False)}))
                    if op == "disconnect":
                        self.close_scrcpy(name)
                        self.run(["disconnect", cfg["endpoint"]])
                with self.state_lock:
                    self.cache.pop(name, None)
                return {"ok": True, "message": "ADB monitoring off."}
            if op == "refresh":
                self.refresh()
                return {"ok": True}
            if op == "scrcpy_stop":
                self.close_scrcpy(name)
                return {"ok": True}
            serial = self.require_phone(name)
            if op == "scrcpy_start":
                executable = shutil.which("scrcpy")
                if not executable:
                    raise ValueError("scrcpy is not installed. Install it with: brew install scrcpy")
                if self.scrcpy and self.scrcpy.poll() is None:
                    raise ValueError("A scrcpy window is already open. Close it before opening another.")
                with open(os.path.join(self.state_dir, "scrcpy.log"), "w") as log:
                    process = subprocess.Popen([executable, "--serial", serial, "--window-title", "Macotron phone"],
                                               stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
                with self.state_lock:
                    self.scrcpy, self.scrcpy_device = process, name
                try:
                    rc = self.scrcpy.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    return {"ok": True, "message": "scrcpy started; check its window. Errors are in scrcpy.log."}
                raise ValueError("scrcpy stopped immediately (exit %s). See ~/.phone-proxy/scrcpy.log." % rc)
            if op in ("tailscale_on", "tailscale_off"):
                capability = self.cache.get(name, {}).get("capabilities", {}).get(op)
                if not capability:
                    raise ValueError("This Tailscale version exposes no supported automation intent. Open Tailscale instead.")
                action = PACKAGE + (".CONNECT_VPN" if op == "tailscale_on" else ".DISCONNECT_VPN")
                command = ["am", "start" if capability["kind"] == "activity" else "broadcast",
                           "-a", action, "-n", capability["component"]]
            elif op == "tailscale_open":
                resolved = component(self.shell(serial, "cmd", "package", "resolve-activity", "--brief",
                                                "-a", "android.intent.action.MAIN", "-c", "android.intent.category.LAUNCHER", "-p", PACKAGE))
                if not resolved:
                    raise ValueError("Tailscale has no launchable activity on this phone.")
                command = ["am", "start", "-n", resolved]
            elif op in ("wake", "sleep"):
                command = ["input", "keyevent", "224" if op == "wake" else "223"]
            elif op in ("wifi_settings", "hotspot_settings"):
                command = ["am", "start", "-a", "android.settings.WIFI_SETTINGS" if op == "wifi_settings" else "android.settings.TETHER_SETTINGS"]
            elif op == "brightness":
                value = body.get("value")
                if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255:
                    raise ValueError("Brightness must be an integer from 0 to 255.")
                self.shell(serial, "settings", "put", "system", "screen_brightness_mode", "0")
                command = ["settings", "put", "system", "screen_brightness", str(value)]
            else:
                raise ValueError("Unknown ADB operation.")
            output = self.shell(serial, *command)
            if op == "brightness" and self.shell(serial, "settings", "get", "system", "screen_brightness") != str(body["value"]):
                raise ValueError("Android did not apply the requested brightness.")
            self.wake.set()
            self.log("ADB control for %s: %s" % (name, op))
            return {"ok": True, "message": "Command sent. Check phone status; Tailscale login/VPN approval may need confirmation on the phone.",
                    "output": output[-600:]}

    def close_scrcpy(self, name=None):
        if self.scrcpy and (name is None or self.scrcpy_device == name) and self.scrcpy.poll() is None:
            self.scrcpy.terminate()
            try:
                self.scrcpy.wait(timeout=5)
            except subprocess.TimeoutExpired:
                raise ValueError("scrcpy has not closed yet. Close its window and retry.")

    def start(self):
        def loop():
            while not self.stop.is_set():
                try:
                    self.refresh()
                except (ValueError, OSError) as e:
                    self.log("ADB status check failed: %s" % e)
                    with self.lock:
                        name, _ = self.active_device()
                        self.publish(name, {"connected": False, "checked": time.time(), "message": str(e)})
                self.wake.wait(TICK)
                self.wake.clear()
        threading.Thread(target=loop, daemon=True, name="phone-adb").start()
