#!/usr/bin/env python3
"""devices.py - the companion-device profiles (Raven the tablet, a spare phone, ...) and which one is active.

One source of truth, ~/.phone-proxy/devices.json, read by phone-proxy.sh (device values), phone-proxy-web.py
(Macotron) and the `Host tab` block of ~/.ssh/config, which `use` rewrites. Only one device is the
companion at a time. Standard library only; run with `python3 -I`.

  devices.py list                     every profile, the active one marked
  devices.py show [--json]            the active profile
  devices.py shell                    shell assignments for phone-proxy.sh (eval them)
  devices.py use NAME                 make NAME active: ssh config block, tabinput screen size, stale address
  devices.py add NAME --label TEXT --user U --ssid SSID --ts TAILSCALE_IP --screen WxH [--hostkey ALIAS]
                      [--hotspot yes|no] [--root yes|no] [--lan IP]   (no SIM = no hotspot: --hotspot no --lan <its Wi-Fi address>)
  devices.py set NAME key=value ...   change fields of a profile (user, ssid, ts, screen, label, hostkey)
  devices.py check                    does the active profile look usable (fields set, ssh block in place)
"""
import json
import os
import re
import sys
import tempfile
import time

HOME = os.path.expanduser("~")
STATE = os.path.join(HOME, ".phone-proxy")
DEVICES_FILE = os.path.join(STATE, "devices.json")
SSH_CONFIG = os.path.join(HOME, ".ssh", "config")
TABIN_CFG = os.path.join(STATE, "tabinput.json")
PHONE_HOST_F = os.path.join(STATE, "phone_host")
BEGIN = "# >>> companion device (devices.py) >>>"
END = "# <<< companion device <<<"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
FIELDS = ("label", "user", "ssid", "ts", "screen", "hostkey", "hotspot", "root", "lan")
BOOLS = ("hotspot", "root")

DEFAULT = {
    "active": "raven",
    "devices": {
        "raven": {
            "label": "Companion phone (configure before use)",
            "user": "u0_a000",
            "ssid": "PhoneHotspot",
            "ts": "100.64.0.1",
            "screen": [2800, 1752],
            "hostkey": "termux-phone",
            "root": False,
        }
    },
}


def _atomic_write(path, text, mode=0o600):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".dev-")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def load():
    try:
        with open(DEVICES_FILE) as f:
            d = json.load(f)
        if not isinstance(d.get("devices"), dict) or d.get("active") not in d["devices"]:
            raise ValueError("bad devices.json")
        return d
    except (OSError, ValueError):
        return json.loads(json.dumps(DEFAULT))


def save(d):
    _atomic_write(DEVICES_FILE, json.dumps(d, indent=2) + "\n")


def active(d=None):
    d = d or load()
    return d["active"], d["devices"][d["active"]]


def validate(name, dev):
    """Return a list of problems with one profile (empty = fine)."""
    bad = []
    if not NAME_RE.match(name):
        bad.append("name must be lowercase letters, digits, - or _")
    if not re.match(r"^[A-Za-z0-9_.-]{1,40}$", str(dev.get("user", ""))):
        bad.append("user (the Termux uid; run `whoami` in Termux)")
    if dev.get("hotspot", True) and (not str(dev.get("ssid", "")).strip() or any(c in str(dev["ssid"]) for c in "\n\r\"'\\")):
        bad.append("ssid (the hotspot name; not needed when hotspot=false)")
    if str(dev.get("ssid", "")) and any(c in str(dev["ssid"]) for c in "\n\r\"'\\"):
        bad.append("ssid has a quote or newline")
    if dev.get("lan") and (not IP_RE.match(str(dev["lan"])) or any(int(x) > 255 for x in str(dev["lan"]).split("."))):
        bad.append("lan (the portal's address on the shared Wi-Fi)")
    if not IP_RE.match(str(dev.get("ts", ""))) or any(int(x) > 255 for x in str(dev.get("ts", "0.0.0.0")).split(".")):
        bad.append("ts (the Tailscale IPv4 address)")
    s = dev.get("screen")
    if not (isinstance(s, list) and len(s) == 2 and all(isinstance(v, int) and 200 <= v <= 10000 for v in s)):
        bad.append("screen (WxH in pixels, e.g. 2800x1752)")
    if not re.match(r"^[A-Za-z0-9_.-]{1,40}$", str(dev.get("hostkey", ""))):
        bad.append("hostkey (an ssh HostKeyAlias, one per physical device)")
    return bad


def ssh_block(dev):
    ts = dev["ts"]
    hosts = "%s %s" % (dev["lan"], ts) if dev.get("lan") else ts
    proxy = ("sh -c 'for h in $(ipconfig getoption en0 router 2>/dev/null) %s; do case \"$(nc -G 2 -w 2 \"$h\" 8022 "
             "</dev/null 2>/dev/null | head -c 4)\" in SSH-) exec nc \"$h\" 8022;; esac; done; "
             "echo \"tab: no sshd on the Wi-Fi gateway or at %s (LAN address, Tailscale)\" >&2; exit 1'") % (hosts, hosts)
    return "\n".join([BEGIN, "Host tab", "    Port 8022", "    User %s" % dev["user"],
                      "    HostKeyAlias %s" % dev["hostkey"], "    ProxyCommand " + proxy, END]) + "\n"


def apply_ssh_config(dev):
    """Rewrite the `Host tab` block in ~/.ssh/config: between our markers if present, else replacing the
    old hand-written block, else appended. A timestamped copy is kept next to the file first."""
    try:
        with open(SSH_CONFIG) as f:
            text = f.read()
    except OSError:
        text = ""
    block = ssh_block(dev)
    if BEGIN in text and END in text:
        new = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", lambda m: block, text, count=1, flags=re.S)
    else:
        m = re.search(r"^Host tab\n(?:[ \t]+.*\n?)+", text, flags=re.M)
        new = (text[:m.start()] + block + text[m.end():]) if m else (text + ("" if text.endswith("\n") or not text else "\n") + block)
    if new == text:
        return False
    if text:
        _atomic_write(SSH_CONFIG + ".bak-" + time.strftime("%Y%m%d-%H%M%S"), text)
    _atomic_write(SSH_CONFIG, new)
    return True


def apply_screen(dev):
    """Put the device's screen size into tabinput.json (keeps every other setting)."""
    try:
        with open(TABIN_CFG) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    cfg["tw"], cfg["th"] = dev["screen"]
    _atomic_write(TABIN_CFG, json.dumps(cfg))


def use(name):
    d = load()
    if name not in d["devices"]:
        raise SystemExit("no device named %r (have: %s)" % (name, ", ".join(sorted(d["devices"]))))
    bad = validate(name, d["devices"][name])
    if bad:
        raise SystemExit("device %r is incomplete: %s" % (name, "; ".join(bad)))
    d["active"] = name
    save(d)
    changed = apply_ssh_config(d["devices"][name])
    apply_screen(d["devices"][name])
    try:
        os.remove(PHONE_HOST_F)   # a saved LAN address belongs to the previous device
    except OSError:
        pass
    return d, changed


def parse_screen(v):
    m = re.match(r"^(\d+)[xX](\d+)$", str(v))
    if not m:
        raise SystemExit("screen must look like 2800x1752")
    return [int(m.group(1)), int(m.group(2))]


def _fields(pairs):
    out = {}
    for k, v in pairs.items():
        if k not in FIELDS:
            raise SystemExit("unknown field %r (have: %s)" % (k, ", ".join(FIELDS)))
        out[k] = parse_screen(v) if k == "screen" else (str(v).lower() in ("1", "true", "yes", "on") if k in BOOLS else v)
    return out


def main(argv):
    if not argv:
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    d = load()
    if cmd == "list":
        for n, dev in sorted(d["devices"].items()):
            print("%s %-10s %-26s user=%s ssid=%s ts=%s screen=%dx%d hostkey=%s hotspot=%s root=%s lan=%s" % (
                "*" if n == d["active"] else " ", n, dev.get("label", ""), dev.get("user"), dev.get("ssid"),
                dev.get("ts"), dev["screen"][0], dev["screen"][1], dev.get("hostkey"),
                "yes" if dev.get("hotspot", True) else "no", "yes" if dev.get("root", True) else "no", dev.get("lan", "-")))
    elif cmd == "show":
        n, dev = active(d)
        print(json.dumps(dict(dev, name=n), indent=2) if "--json" in rest else "%s: %s" % (n, json.dumps(dev)))
    elif cmd == "shell":
        n, dev = active(d)
        q = lambda s: "'" + str(s).replace("'", "'\\''") + "'"
        print("DEVICE_NAME=%s DEVICE_LABEL=%s PHONE_USER=%s HOSTKEY_ALIAS=%s DEVICE_SSID=%s DEVICE_TS=%s DEVICE_HOTSPOT=%s DEVICE_ROOT=%s DEVICE_LAN=%s" % (
            q(n), q(dev.get("label", n)), q(dev["user"]), q(dev["hostkey"]), q(dev.get("ssid", "")), q(dev["ts"]),
            q("1" if dev.get("hotspot", True) else "0"), q("1" if dev.get("root", True) else "0"), q(dev.get("lan", ""))))
    elif cmd == "use" and len(rest) == 1:
        d, changed = use(rest[0])
        print("active device: %s (%s); ssh 'tab' block %s; tabinput screen %dx%d" % (
            d["active"], d["devices"][d["active"]].get("label", ""), "rewritten" if changed else "already right",
            *d["devices"][d["active"]]["screen"]))
        print("Restart Macotron (or press Restart in it) so it reads the new device; its tunnels follow the new ssh block.")
    elif cmd == "add" and rest:
        name = rest[0]
        opts, it = {}, iter(rest[1:])
        for a in it:
            if not a.startswith("--"):
                raise SystemExit("unexpected %r" % a)
            opts[a[2:]] = next(it, None)
        opts = {k: v for k, v in opts.items() if v is not None}
        if "label" not in opts:
            opts["label"] = name
        opts.setdefault("hostkey", "termux-" + name)
        dev = _fields(opts)
        bad = validate(name, dev)
        if name in d["devices"]:
            raise SystemExit("device %r exists; use `set`" % name)
        if bad:
            raise SystemExit("incomplete: " + "; ".join(bad))
        d["devices"][name] = dev
        save(d)
        print("added %s; run `devices.py use %s` to switch to it" % (name, name))
    elif cmd == "set" and len(rest) >= 2:
        name = rest[0]
        if name not in d["devices"]:
            raise SystemExit("no device named %r" % name)
        d["devices"][name].update(_fields(dict(a.split("=", 1) for a in rest[1:])))
        bad = validate(name, d["devices"][name])
        if bad:
            raise SystemExit("incomplete: " + "; ".join(bad))
        save(d)
        if name == d["active"]:
            apply_ssh_config(d["devices"][name])
            apply_screen(d["devices"][name])
        print("updated", name)
    elif cmd == "check":
        n, dev = active(d)
        bad = validate(n, dev)
        try:
            ok_ssh = ssh_block(dev) in open(SSH_CONFIG).read()
        except OSError:
            ok_ssh = False
        print("device %s: %s; ssh block %s" % (n, "fields ok" if not bad else "PROBLEMS: " + "; ".join(bad),
                                              "in place" if ok_ssh else "NOT in place (run `use %s`)" % n))
        return 0 if (not bad and ok_ssh) else 1
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
