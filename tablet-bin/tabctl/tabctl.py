#!/data/data/com.termux/files/usr/bin/python
"""tabctl - Tablotron, the control page for the Tab S7+ router, served from Termux.

  python ~/tabctl/tabctl.py serve    run the server on port 8095 (Termux:Boot starts it)
  python ~/tabctl/tabctl.py url      print the page addresses, with the access token
  python ~/tabctl/tabctl.py open     open the page in the tablet's browser (logs it in)
  python ~/tabctl/tabctl.py status   print the raw status as JSON

Only 127.0.0.1 and Tailscale addresses (100.64.0.0/10) may connect. The first visit needs
?t=<token>; the page then keeps a cookie. Root work goes through Magisk su to root.sh,
which sits next to this file. The SSD passphrase goes straight to cryptsetup on stdin and
is never stored or logged. The Connections card shows what watch.py (the root watchdog next
to this file) finds every 5 s; Tablotron starts watch.py whenever its state goes stale.

History offers cards, overlays and a Focus overlay of enabled graphs. Graph settings
provide colors and auto/custom ranges for every layout. Save settings writes the layout,
focused metric and graph options atomically to ~/.config/tabctl/history-settings.json;
all browsers share the last saved settings. Unsaved edits stay in the current page.
"""
import http.server
import ipaddress
import json
import math
import os
import re
import secrets
import shlex
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlparse

HOME = os.path.expanduser("~")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT_SH = os.path.join(HERE, "root.sh")
PORT = int(os.environ.get("TABCTL_PORT", "8095"))
TOKEN_FILE = os.path.join(HOME, ".config", "tabctl", "token")
MERIDIAN_LOG = os.path.join(HOME, "meridian.log")
WATCH_STATE = os.path.join(HOME, ".config", "tabctl", "watch.json")
HISTORY_DB = os.path.join(HOME, ".config", "tabctl", "history.db")
HISTORY_MAX_HOURS = 180 * 24  # matches watch.py's HISTORY_DAYS retention
HISTORY_SETTINGS = os.path.join(HOME, ".config", "tabctl", "history-settings.json")
HISTORY_COLORS = {
    "ssd_temp_c": "#dc2626", "bat_temp_c": "#d97706", "bat_level": "#16a34a",
    "sta_rssi": "#0b72b9", "sta_link": "#0369a1", "internet_ms": "#7c3aed",
    "accel_pp": "#be185d",
}
DISMISSED = os.path.join(HOME, ".config", "tabctl", "dismissed.json")
DISMISSABLE = {
    # check_id -> a query returning the signature (sqlite row[0]) of the event it is currently
    # warning about; watch.py clears the warning once this file's value matches that signature,
    # and a new occurrence always gets a new one, so dismissing never hides a future event.
    "ssd_safety": "SELECT t FROM ssd_events WHERE event='disconnect' AND safe=0 ORDER BY t DESC LIMIT 1",
}
WATCH_STALE = 30  # seconds without a new pass before Tablotron restarts watch.py
TSU = "/data/data/com.termux/files/usr/bin/tsu"
TSU_OLD = 'SU_BINARY_SEARCH=("/system/xbin/su" "/system/bin/su")'
TSU_NEW = 'SU_BINARY_SEARCH=("/system/xbin/su" "/system/bin/su" "/product/bin/su" "/debug_ramdisk/su")'
SU_PATHS = ("/product/bin/su", "/debug_ramdisk/su", "/system/bin/su", "/system/xbin/su", "/sbin/su")
ALLOWED_NETS = [ipaddress.ip_network(n) for n in ("127.0.0.0/8", "100.64.0.0/10")]
ROOT_ACTIONS = {"up": 900, "lock": 120, "eject": 180, "reconnect": 60, "reboot": 120, "shutdown": 120,
                "samba-restart": 900, "hotspot-settings": 30, "fix-android": 60,
                "power-full": 60, "power-meeting": 60, "power-meeting-end": 60, "power-router": 60, "power-minimal": 60,
                "watch-start": 30, "watch-stop": 30, "wifi-connect": 30}


def load_token():
    try:
        with open(TOKEN_FILE) as f:
            t = f.read().strip()
        if t:
            return t
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(TOKEN_FILE), mode=0o700, exist_ok=True)
    t = secrets.token_urlsafe(24)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(t + "\n")
    return t


TOKEN = load_token()


def tail(path, n):
    try:
        with open(path, errors="replace") as f:
            return "".join(f.readlines()[-n:])
    except OSError:
        return ""


def run_root(cmd, stdin=None, timeout=120, args=()):
    """Run root.sh <cmd> [args] as root. Magisk's su cannot load Termux's LD_PRELOAD, so drop it."""
    su = next((p for p in SU_PATHS if os.path.exists(p)), None)
    if not su:
        return 127, "No su binary found. Is Magisk root still installed (Phase 3)?"
    env = {k: v for k, v in os.environ.items() if k != "LD_PRELOAD"}
    line = "sh %s %s" % (shlex.quote(ROOT_SH), " ".join(shlex.quote(a) for a in (cmd,) + tuple(args)))
    try:
        r = subprocess.run([su, "-c", line], input=stdin, capture_output=True, text=True,
                           timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return 124, "Timed out after %d s." % timeout
    return r.returncode, (r.stdout + r.stderr).strip()


def run_user(script, timeout=60):
    try:
        r = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 124, "Timed out after %d s." % timeout
    return r.returncode, (r.stdout + r.stderr).strip()


_status = {"t": 0.0, "data": None}
_status_lock = threading.Lock()


def get_status(max_age=3):
    with _status_lock:
        if _status["data"] is not None and time.time() - _status["t"] < max_age:
            return _status["data"]
        rc, out = run_root("status", timeout=40)
        s = {}
        for line in out.splitlines():
            k, sep, v = line.partition("=")
            if sep and k.isidentifier():
                s[k] = v.strip()
        s["root_ok"] = rc == 0 and s.get("uid") == "0"
        if not s["root_ok"]:
            s["root_error"] = out[-600:]
        s["meridian_log"] = tail(MERIDIAN_LOG, 12)
        s["now"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _status.update(t=time.time(), data=s)
        return s


_watch = {"kick": 0.0}


def watch_state():
    """watch.py's last pass, with its age; restart watch.py (as root) when it went quiet."""
    try:
        with open(WATCH_STATE) as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    st["age"] = time.time() - st.get("t", 0) if st.get("t") else None
    stale = st["age"] is None or st["age"] > WATCH_STALE or st.get("stopped")
    if stale and time.time() - _watch["kick"] > 60:
        _watch["kick"] = time.time()
        threading.Thread(target=run_root, args=("watch-start",), kwargs={"timeout": 30}, daemon=True).start()
        st["kicked"] = True
    return st


def get_history(hours):
    """Samples and SSD disconnect events from the last `hours`, for Tablotron's History page."""
    cutoff = int(time.time()) - max(1, min(hours, HISTORY_MAX_HOURS)) * 3600
    try:
        con = sqlite3.connect(HISTORY_DB, timeout=5)
        con.row_factory = sqlite3.Row
        samples = [dict(r) for r in con.execute("SELECT * FROM samples WHERE t >= ? ORDER BY t", (cutoff,))]
        events = [dict(r) for r in con.execute(
            "SELECT * FROM ssd_events WHERE t >= ? ORDER BY t DESC", (cutoff,))]
        total = con.execute("SELECT COUNT(*) FROM ssd_events WHERE event='disconnect'").fetchone()[0]
        unsafe = con.execute("SELECT COUNT(*) FROM ssd_events WHERE event='disconnect' AND safe=0").fetchone()[0]
        con.close()
    except sqlite3.Error:
        samples, events, total, unsafe = [], [], 0, 0
    return {"samples": samples, "events": events, "disconnects_total": total, "disconnects_unsafe": unsafe}


def dismiss_check(check_id):
    """Acknowledge a check's current warning (see DISMISSABLE and watch.py's read_dismissed()).
    Writes directly, no su: CFG is owned by the Termux user, same as history.db and watch.log."""
    query = DISMISSABLE.get(check_id)
    if not query:
        return 2, "Nothing to dismiss for %s." % check_id
    try:
        con = sqlite3.connect(HISTORY_DB, timeout=5)
        row = con.execute(query).fetchone()
        con.close()
    except sqlite3.Error as e:
        return 1, "Could not read history: %s" % e
    if row is None:
        return 0, "Nothing to dismiss."
    try:
        with open(DISMISSED) as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    d[check_id] = row[0]
    tmp = DISMISSED + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.replace(tmp, DISMISSED)
    return 0, "Dismissed."


def restart_meridian():
    run_user('pkill -f "[m]eridian"; pkill -f "[s]ocat TCP4"; true')
    time.sleep(1)
    with open(MERIDIAN_LOG, "w") as log:
        subprocess.Popen(["./meridian.sh"], cwd=HOME, stdin=subprocess.DEVNULL, stdout=log,
                         stderr=subprocess.STDOUT, start_new_session=True)
    time.sleep(4)
    return 0, "meridian restarted.\n" + tail(MERIDIAN_LOG, 10)


def fix_tsu():
    try:
        with open(TSU) as f:
            s = f.read()
    except OSError as e:
        return 1, "Cannot read tsu: %s" % e
    if TSU_NEW in s:
        return 0, "tsu already finds Magisk's su."
    if TSU_OLD not in s:
        return 1, "tsu has changed and the expected line is missing. Check runbook Phase 5 step 4 by hand."
    with open(TSU, "w") as f:
        f.write(s.replace(TSU_OLD, TSU_NEW, 1))
    return 0, "tsu patched: sudo works over SSH again."


_action_lock = threading.Lock()


def default_history_settings():
    return {"view": "cards", "focus": "ssd_temp_c", "metrics": {
        key: {"enabled": key == "ssd_temp_c", "color": color, "min": None, "max": None}
        for key, color in HISTORY_COLORS.items()
    }}


def validate_history_settings(settings):
    if not isinstance(settings, dict) or set(settings) != {"view", "focus", "metrics"}:
        raise ValueError("Expected view, focus and metrics.")
    if settings["view"] not in ("cards", "overlay", "focus"):
        raise ValueError("Unknown history layout.")
    if not isinstance(settings["focus"], str) or settings["focus"] not in HISTORY_COLORS:
        raise ValueError("Unknown focused metric.")
    metrics = settings["metrics"]
    if not isinstance(metrics, dict) or set(metrics) != set(HISTORY_COLORS):
        raise ValueError("Settings must include every history metric.")
    for key, metric in metrics.items():
        if not isinstance(metric, dict) or set(metric) != {"enabled", "color", "min", "max"}:
            raise ValueError("Invalid settings for %s." % key)
        if not isinstance(metric["enabled"], bool):
            raise ValueError("Enabled must be true or false for %s." % key)
        if not isinstance(metric["color"], str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", metric["color"]):
            raise ValueError("Invalid color for %s." % key)
        low, high = metric["min"], metric["max"]
        if low is None and high is None:
            continue
        if not all(type(v) in (int, float) and abs(v) <= 1e12 and math.isfinite(v) for v in (low, high)):
            raise ValueError("Enter finite minimum and maximum values within +/-1e12 for %s." % key)
        if low >= high:
            raise ValueError("Minimum must be less than maximum for %s." % key)
    return settings


def load_history_settings():
    try:
        with open(HISTORY_SETTINGS) as f:
            return validate_history_settings(json.load(f))
    except FileNotFoundError:
        return default_history_settings()


def save_history_settings(settings):
    validate_history_settings(settings)
    directory = os.path.dirname(HISTORY_SETTINGS)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=directory, prefix=".history-settings-", delete=False) as f:
            tmp = f.name
            json.dump(settings, f, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, HISTORY_SETTINGS)
    finally:
        if tmp is not None and os.path.exists(tmp):
            os.unlink(tmp)


def do_action(name, body):
    if name == "unlock":
        pw = body.pop("passphrase", "")
        if not pw:
            return 1, "Type the passphrase first."
        rc, out = run_root("unlock", stdin=pw + "\n", timeout=900)
        pw = None
        if rc != 0 and "No key available" in out:
            out = "Wrong passphrase.\n\n" + out
        return rc, out
    if name == "wifi-connect":
        ssid = body.get("ssid")                       # one of the saved networks; root.sh checks that again
        if not isinstance(ssid, str) or not 1 <= len(ssid) <= 32 or any(ord(c) < 32 for c in ssid):
            return 1, "Choose a saved network."
        return run_root(name, timeout=ROOT_ACTIONS[name], args=(ssid,))
    if name == "power-meeting":
        m = body.get("minutes", 30)       # the page asks: 15 min to 24 h, in 15 min steps
        if isinstance(m, bool) or not isinstance(m, int) or not 15 <= m <= 1440 or m % 15:
            return 1, "Choose a meeting length of 15 min to 24 h, in 15 min steps."
        return run_root(name, timeout=ROOT_ACTIONS[name], args=(str(m),))
    if name in ROOT_ACTIONS:
        return run_root(name, timeout=ROOT_ACTIONS[name])
    if name == "meridian-restart":
        return restart_meridian()
    if name == "sshd-restart":
        return run_user('pkill -x sshd; sleep 1; sshd && echo "sshd restarted. Open SSH sessions stay connected."')
    if name == "wake-lock":
        return run_user('termux-wake-lock && echo "Termux wake lock taken."')
    if name == "fix-tsu":
        return fix_tsu()
    if name.startswith("dismiss-"):
        return dismiss_check(name[len("dismiss-"):])
    return 2, "Unknown action: %s" % name


def tailscale_ip():
    """The address this device uses toward Tailscale's DNS, which is its Tailscale IP."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("100.100.100.100", 53))
            ip = s.getsockname()[0]
        return ip if ipaddress.ip_address(ip) in ALLOWED_NETS[1] else None
    except OSError:
        return None


# Framed only by a page on the viewing machine's own localhost: Macotron's Tablet tab, which reaches
# this page through the Mac's SSH tunnel. frame-ancestors overrides X-Frame-Options, which is gone.
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                               "img-src 'self' data:; frame-ancestors http://localhost:* http://127.0.0.1:*; "
                               "base-uri 'none'; form-action 'self'",
}


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "tabctl"
    sys_version = ""

    def log_message(self, fmt, *args):  # never log the query string: it can hold the token
        sys.stderr.write("%s %s %s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), self.client_address[0],
                                            self.command, urlparse(self.path).path))

    def send(self, code, body, ctype="text/plain; charset=utf-8", extra=None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in {**SECURITY_HEADERS, **(extra or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def allowed(self):
        try:
            ip = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            return False
        return any(ip in n for n in ALLOWED_NETS)

    def authed(self):
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "tabctl" and secrets.compare_digest(v, TOKEN):
                return True
        return False

    def do_GET(self):
        if not self.allowed():
            return self.send(403, "tabctl answers only on the tablet itself and over Tailscale.\n")
        u = urlparse(self.path)
        if u.path == "/manifest.json":
            return self.send(200, MANIFEST, "application/manifest+json")
        if u.path == "/icon.svg":
            return self.send(200, ICON, "image/svg+xml")
        t = parse_qs(u.query).get("t", [""])[0]
        if t:
            if secrets.compare_digest(t, TOKEN):
                cookie = "tabctl=%s; Path=/; Max-Age=31536000; HttpOnly; SameSite=Strict" % TOKEN
                return self.send(302, "", extra={"Location": "/", "Set-Cookie": cookie})
            return self.send(403, "Wrong token.\n")
        if not self.authed():
            return self.send(401, LOGIN_PAGE, "text/html; charset=utf-8")
        if u.path == "/":
            return self.send(200, PAGE, "text/html; charset=utf-8")
        if u.path == "/api/status":
            return self.send(200, json.dumps(get_status()), "application/json")
        if u.path == "/api/watch":
            return self.send(200, json.dumps(watch_state()), "application/json")
        if u.path == "/api/history/settings":
            try:
                settings = load_history_settings()
            except (OSError, ValueError) as e:
                sys.stderr.write("Cannot load history settings: %s\n" % e)
                return self.send(500, json.dumps({"error": "Cannot load history settings: %s" % e}), "application/json")
            return self.send(200, json.dumps(settings), "application/json")
        if u.path == "/api/history":
            try:
                hours = int(parse_qs(u.query).get("hours", ["24"])[0])
            except ValueError:
                hours = 24
            return self.send(200, json.dumps(get_history(hours)), "application/json")
        return self.send(404, "Not found.\n")

    do_HEAD = do_GET

    def do_POST(self):
        if not self.allowed():
            return self.send(403, "Forbidden.\n")
        if not self.authed() or self.headers.get("X-Tabctl") != "1":
            return self.send(401, "Not logged in.\n")
        path = urlparse(self.path).path
        if path not in ("/api/action", "/api/history/settings"):
            return self.send(404, "Not found.\n")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self.send(400, "Bad request.\n")
        if n <= 0 or n > 4096:
            return self.send(400, "Bad request.\n")
        try:
            body = json.loads(self.rfile.read(n))
        except ValueError:
            return self.send(400, "Bad request.\n")
        if path == "/api/history/settings":
            try:
                save_history_settings(body)
            except ValueError as e:
                return self.send(400, json.dumps({"error": str(e)}), "application/json")
            except OSError as e:
                sys.stderr.write("Cannot save history settings: %s\n" % e)
                return self.send(500, json.dumps({"error": "Cannot save history settings: %s" % e}), "application/json")
            return self.send(200, json.dumps(body), "application/json")
        if not isinstance(body, dict):
            return self.send(400, "Bad request.\n")
        name = str(body.get("action", ""))
        if not _action_lock.acquire(blocking=False):
            body.clear()
            return self.send(409, json.dumps({"rc": 409, "out": "Another action is still running."}),
                             "application/json")
        try:
            rc, out = do_action(name, body)
        finally:
            body.clear()
            _action_lock.release()
        _status["t"] = 0.0  # the next status call reads fresh values
        return self.send(200, json.dumps({"rc": rc, "out": out}), "application/json")


MANIFEST = json.dumps({
    "name": "Tablotron", "short_name": "Tablotron", "start_url": "/", "display": "standalone",
    "background_color": "#0f1720", "theme_color": "#0f1720",
    "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}],
})

ICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#0f1720"/>
<path d="M14 30a26 26 0 0 1 36 0M20 37a17 17 0 0 1 24 0M26 44a8 8 0 0 1 12 0" stroke="#38bdf8" stroke-width="4" fill="none" stroke-linecap="round"/>
<circle cx="32" cy="50" r="3.5" fill="#38bdf8"/></svg>"""

LOGIN_PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tablotron</title><body style="font-family:system-ui,sans-serif;max-width:34em;margin:3em auto;padding:0 16px;line-height:1.5">
<h1>Tablotron</h1><p>This page needs its access token once. On the tablet, run this in Termux:</p>
<pre style="background:#8882;padding:8px;border-radius:6px;overflow-x:auto">python ~/tabctl/tabctl.py open</pre>
<p>From the PC: <code>ssh tab python ~/tabctl/tabctl.py url</code> and open the address it prints.</p></body>"""

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tablotron</title>
<link rel="manifest" href="/manifest.json"><link rel="icon" href="/icon.svg">
<meta name="theme-color" content="#0f1720">
<style>
:root{--bg:#f4f6f9;--card:#fff;--fg:#17202b;--dim:#5b6878;--line:#dde3ea;--acc:#0b72b9;--ok:#16a34a;--warn:#d97706;--bad:#dc2626;--btn:#eef3f8}
@media (prefers-color-scheme:dark){:root{--bg:#0f1720;--card:#16212d;--fg:#e6edf3;--dim:#93a1b0;--line:#26364a;--acc:#38bdf8;--ok:#22c55e;--warn:#f59e0b;--bad:#f87171;--btn:#1d2c3d}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--card);border-bottom:1px solid var(--line);padding:10px 16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
header h1{font-size:18px;margin:0}
header .dim{font-size:13px}
main{max-width:1200px;margin:0 auto;padding:16px;display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:14px}
@media (max-width:420px){main{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;min-width:0}
.card h2{font-size:15px;margin:0 0 8px;display:flex;align-items:center;gap:8px}
.wide{grid-column:1/-1}
.row{display:flex;justify-content:space-between;gap:10px;padding:5px 0;border-top:1px solid var(--line)}
.row:first-of-type{border-top:none}
.row .k{color:var(--dim);flex-shrink:0;max-width:55%}
.row .v{text-align:right;overflow-wrap:anywhere}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;background:var(--dim);vertical-align:middle}
.ok{background:var(--ok)}.warn{background:var(--warn)}.bad{background:var(--bad)}
.t-ok{color:var(--ok)}.t-warn{color:var(--warn)}.t-bad{color:var(--bad)}
.dim{color:var(--dim)}
.btns{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
button{font:inherit;background:var(--btn);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:7px 12px;cursor:pointer}
button:hover{border-color:var(--acc)}
button.pri{background:var(--acc);border-color:var(--acc);color:#fff}
button.danger{color:var(--bad)}
button:disabled{opacity:.5;cursor:wait}
input[type=password]{font:inherit;width:100%;padding:8px 10px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--fg)}
.issues{margin:0;padding-left:20px}
.issues li{margin:3px 0}
.bar{height:8px;border-radius:4px;background:var(--line);overflow:hidden;margin:6px 0 2px}
.bar>div{height:100%;background:var(--acc)}
.gaugegrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(148px,1fr));gap:10px 6px;margin-top:4px}
.gauge{text-align:center}
.gauge svg{width:100%;overflow:visible;display:block}
.gauge .val{font-size:18px;font-weight:600;margin-top:2px}
.gauge .lbl{font-size:12px;color:var(--dim);line-height:1.25}
.gauge .sub{font-size:11px;color:var(--dim);margin-top:2px}
.gauge .legend{font-size:11px;margin-top:3px;line-height:1.5}
.histgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,280px),1fr));gap:14px;margin-top:8px}
.histchart{text-align:center;min-width:0;border:1px solid var(--line);border-radius:8px;padding:10px}
.histchart canvas{width:100%;height:110px;display:block}
.histchart .lbl{font-size:12px;color:var(--dim);margin-top:4px}
.histgrid.focus,.histgrid.overlay{grid-template-columns:minmax(0,1fr)}
.histchart.large canvas{height:320px}
.histthumbs{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,130px),1fr));gap:8px}
.histthumbs .histchart{width:100%;padding:8px}
.histthumbs canvas{height:50px}
.histthumbs [aria-pressed=true]{border:2px solid var(--acc);background:var(--bg)}
.histthumbs .histoff{opacity:.55}
.histoptions{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,250px),1fr));gap:10px;margin-top:10px}
.histoption{border:1px solid var(--line);border-radius:8px;padding:10px;min-width:0}
.histoption legend{font-size:13px}
.histoption label{display:inline-flex;align-items:center;gap:5px;font-size:13px;margin:4px}
.histoption input[type=number]{width:95px;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:4px;padding:4px}
.histoption input[type=color]{width:38px;height:28px;padding:2px}
.histoption input:invalid{border-color:var(--bad)}
.histlegend{display:flex;flex-wrap:wrap;gap:8px 16px;margin-top:10px;font-size:12px}
.histkey{display:inline-block;width:18px;height:3px;vertical-align:middle;margin-right:5px}
#histview{display:flex;flex-wrap:wrap;gap:6px}
button:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
.evt{display:flex;justify-content:space-between;gap:10px;padding:4px 0;border-top:1px solid var(--line);font-size:13px}
.evt:first-of-type{border-top:none}
pre{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin:8px 0 0;max-height:320px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;font:12.5px/1.4 ui-monospace,Menlo,Consolas,monospace}
ol,ul{padding-left:20px;margin:6px 0}
li{margin:3px 0}
details summary{cursor:pointer;color:var(--dim);margin-top:8px}
code{font:13px ui-monospace,Menlo,Consolas,monospace}
details.menu{position:relative}
details.menu summary{list-style:none;margin:0;display:flex;align-items:center;gap:6px;color:var(--bad);font-weight:600;background:var(--btn);border:1px solid var(--bad);border-radius:8px;padding:7px 12px}
details.menu summary::-webkit-details-marker{display:none}
details.menu[open] summary,details.menu summary:hover{border-color:var(--acc)}
.menu-pop{position:absolute;right:0;top:calc(100% + 6px);z-index:10;min-width:200px;display:flex;flex-direction:column;gap:6px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px;box-shadow:0 6px 24px #0004}
.menu-pop button{text-align:left}
[hidden]{display:none!important}
#normal{display:contents}
.act{color:var(--dim);font-size:13px;margin-top:2px}
.row .v .act{text-align:right}
.clock{margin-left:auto;display:flex;align-items:baseline;gap:8px;font-variant-numeric:tabular-nums;white-space:nowrap}
.clock b{font-size:20px;font-weight:600}
.clock span{font-size:13px;color:var(--dim)}
button.ico{display:inline-flex;align-items:center;gap:6px}
</style></head>
<body>
<header><h1>Tablotron</h1><span id="health"></span><span class="dim" id="when">loading...</span>
<div class="clock"><b id="clock-t"></b><span id="clock-d"></span></div>
<button onclick="refresh(true)">Refresh</button>
<button id="cockpitbtn" onclick="toggleCockpit()">Cockpit</button>
<button id="historybtn" onclick="toggleHistory()">History</button>
<button class="ico" id="fsbtn" type="button" title="Full screen" hidden><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path id="fsico" d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></svg><span id="fslbl">Full screen</span></button>
<details class="menu" id="powermenu"><summary><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" aria-hidden="true"><path d="M12 3v8M6.3 6.8a8 8 0 1 0 11.4 0"/></svg>Power</summary><div class="menu-pop">
<button data-act="reboot">Restart tablet</button>
<button class="danger" data-act="shutdown">Shut down tablet</button>
</div></details></header>
<main>
<section class="card wide" id="cockpit" hidden><h2>Cockpit</h2><div id="gauges" class="gaugegrid"></div></section>
<section class="card wide" id="history" hidden><h2>History</h2>
<div class="btns"><select id="histrange" aria-label="History time range" onchange="loadHistory()"><option value="1">Last hour</option><option value="3">Last 3 hours</option><option value="6">Last 6 hours</option><option value="24" selected>Last 24 hours</option><option value="168">Last 7 days</option><option value="720">Last 30 days</option><option value="4320">All (180 days)</option></select>
<div id="histview" role="group" aria-label="History plot layout">
<button type="button" data-view="cards" aria-pressed="true" class="pri" onclick="setHistView('cards')">Cards</button>
<button type="button" data-view="overlay" aria-pressed="false" onclick="setHistView('overlay')">Overlay</button>
<button type="button" data-view="focus" aria-pressed="false" onclick="setHistView('focus')">Focus</button>
</div></div>
<p class="dim" id="histhelp" style="font-size:13px">Each metric is shown in its own card.</p>
<details id="histsettings"><summary>Graph settings: enable, scale and color</summary>
<p class="dim" style="font-size:13px">Enable graphs to overlay them in Focus. Colors and scales apply in all layouts. Custom ranges clip values outside the range. Save settings to share them across browsers; the last save wins.</p>
<div id="histoptions" class="histoptions"></div></details>
<div class="btns"><button type="button" id="histsave" onclick="saveHistSettings()" disabled>Save settings</button>
<button type="button" id="histretry" onclick="loadHistory()" hidden>Retry loading</button>
<span id="histmessage" role="status" aria-live="polite"></span></div>
<div id="histcharts" class="histgrid"></div>
<h3 style="margin:16px 0 8px;font-size:14px">SSD disconnects<span class="dim" id="histssdsum"></span></h3>
<div id="histssdevents" class="dim">Loading...</div>
<h3 style="margin:16px 0 8px;font-size:14px">Location</h3>
<div id="histloc" class="dim">Loading...</div></section>
<div id="normal">
<section class="card wide"><h2><span style="cursor:pointer" onclick="toggleWatch()" title="Click to show every check, including the ones that are fine">Connections</span> <span class="dim" id="wwhen" style="font-weight:400;font-size:13px"></span></h2><div id="watch" class="dim">Checking...</div>
<div class="btns" id="wbtns" hidden><button data-act="watch-start">Start the watch</button></div></section>

<section class="card wide"><h2>Needs attention</h2><div id="issues" class="dim">Checking...</div></section>

<section class="card"><h2>Wi-Fi and hotspot</h2><div id="wifi"></div>
<div class="btns" style="margin-top:8px;align-items:center"><label class="dim" for="wsel">Switch the tablet's Wi-Fi to</label><select id="wsel"></select><button id="wgo">Connect</button></div>
<div class="dim" id="wnote" style="font-size:12.5px;margin-top:4px"></div>
<div class="btns"><button data-act="hotspot-settings">Open hotspot settings</button></div>
<p class="dim" style="margin:8px 0 0;font-size:13px">The name and password can only be changed on that screen. Set Speed and compatibility to 5 GHz and turn off "Turn off hotspot automatically".</p></section>

<section class="card"><h2>SSD</h2><div id="ssd"></div>
<form id="unlockf" style="display:none;margin-top:10px" autocomplete="off">
<input type="password" id="pw" placeholder="SSD passphrase" autocomplete="off" aria-label="SSD passphrase">
<div class="btns"><button class="pri" type="submit">Unlock and mount</button></div></form>
<div class="btns" id="ssdbtns"></div></section>

<section class="card"><h2>Samba shares</h2><div id="smb"></div>
<div class="btns"><button data-act="samba-restart">Restart Samba</button></div></section>

<section class="card"><h2>Remote access and meridian</h2><div id="net"></div>
<div class="btns"><button data-act="meridian-restart">Restart meridian</button><button data-act="sshd-restart" data-confirm="Restart sshd? Open SSH sessions stay connected.">Restart sshd</button></div>
<details><summary>meridian log</summary><pre id="mlog"></pre></details></section>

<section class="card"><h2>Root and Android settings</h2><div id="sys"></div>
<div class="btns"><button data-act="fix-android">Re-apply Android settings</button><button data-act="fix-tsu">Fix tsu</button><button data-act="wake-lock">Take wake lock</button></div></section>

<section class="card"><h2>Power saving</h2><div id="psave"></div>
<div class="btns" id="psavebtns"></div>
<div class="btns" id="mtgask" hidden><label style="display:inline-flex;align-items:center;gap:6px">Meeting length, from now <select id="mtgmin"></select></label><button class="pri" id="mtggo">Start meeting</button><button id="mtgno">Cancel</button></div>
<p class="dim" style="margin:8px 0 0;font-size:13px">Full: Bluetooth, location and background scanning on. Meeting: for using the tablet in a meeting, for a length you choose (15 min to 24 h); then it goes back to the settings from before by itself. Bluetooth and location on, background scanning off, a 10 min screen timeout, and ChatGPT off battery optimisation and free to run in the background, so a chat keeps going with the screen off. Router: Bluetooth, location and scanning off, the tablet only routes. Minimal: also a 15 s screen timeout and no screensaver when docked. The Termux wake lock stays on in every level; without it SSH, meridian and this page die. If the home Wi-Fi drops after a change, turn the hotspot off and on.</p></section>

<section class="card"><h2>Tablet</h2><div id="dev"></div>
<div class="btns"><button data-act="reboot">Restart tablet</button><button class="danger" data-act="shutdown">Shut down tablet</button></div></section>

<section class="card wide"><h2>Output <span class="dim" id="busy"></span></h2><pre id="out">Ready.</pre></section>

<section class="card"><h2>After a reboot</h2><ol>
<li>Unlock the SSD here (or let the key file do it, if set up).</li>
<li>Check that the hotspot is on and on 5 GHz.</li>
<li>Check Tailscale shows a 100.x address.</li>
<li>Everything above "Needs attention" should be empty.</li></ol></section>

<section class="card"><h2>After a LineageOS update</h2><ol>
<li>Root is gone: patch the new boot.img with Magisk and flash it (runbook Phase 3).</li>
<li>Then press "Re-apply Android settings" here (mount namespace, battery, phantom processes).</li>
<li>If sudo over SSH says "Are you rooted?", press "Fix tsu".</li></ol></section>

<section class="card"><h2>Never</h2><ul>
<li>Never tap Android's "Format" or "Set up" on the USB drive notification: it erases the encrypted SSD.</li>
<li>Never unplug the SSD before pressing Eject.</li>
<li>Never let the SSD run without its own power supply.</li></ul></section>
</div>
</main>
<script>
const $=id=>document.getElementById(id);
let S={},busy=false;
const CONFIRM={
  reboot:'Restart the tablet? The SSD is locked first, so both Samba shares stop. SSH, Tailscale, the hotspot and this page are gone for a minute or two. Afterwards, unlock the SSD here.',
  shutdown:"Shut the tablet down? The SSD is locked first, so both Samba shares stop. SSH, Tailscale, the hotspot and this page stay off until someone presses the tablet's power button."};
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const row=(k,v,lvl)=>`<div class="row"><span class="k">${lvl?`<span class="dot ${lvl}"></span>`:''}${esc(k)}</span><span class="v">${v}</span></div>`;
const chan=f=>{f=+f;if(!f)return null;if(f>=2412&&f<=2472)return(f-2407)/5;if(f==2484)return 14;if(f>=5160&&f<=5885)return(f-5000)/5;if(f>=5955)return(f-5950)/5;return null};
const band=f=>{f=+f;return f<3000?'2.4 GHz':f<5925?'5 GHz':'6 GHz'};
const dfs=f=>{f=+f;return f>=5250&&f<=5730};
const WIDTH={1:'20 MHz',2:'20 MHz',3:'40 MHz',4:'80 MHz',5:'80+80 MHz',6:'160 MHz',11:'320 MHz'};
const CFGBAND={1:'2.4 GHz',2:'5 GHz only',3:'"5 GHz" (5 GHz preferred, 2.4 GHz fallback)',4:'6 GHz',7:'2.4 / 5 / 6 GHz'};
const SEC={0:'Open',1:'WPA2-Personal',2:'WPA2/WPA3 transition',3:'WPA3-Personal',4:'OWE transition',5:'OWE'};
const gib=k=>(k/1048576).toFixed(k>1048576*100?0:1)+' GiB';
const tib=k=>(k/1073741824).toFixed(2)+' TiB';
const dur=s=>{s=+s;const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);return(d?d+'d ':'')+(h?h+'h ':'')+m+'m'};
const port=(p,want)=>{const v=S['port_'+p];return v?`<span class="t-ok">${esc(v)} on :${p}</span>`:`<span class="t-${want||'bad'}">not listening on :${p}</span>`};

// Cockpit: analog gauges. rot(v): -90deg at min (needle left) to +90deg at max (needle right), 0deg (up) at the midpoint.
const num=v=>(v===undefined||v===null||v==='')?null:+v;
const pctOf=(u,s)=>{u=num(u);s=num(s);return s?Math.round(u*1000/s)/10:null};
const human=k=>{k=+k;if(!k&&k!==0)return null;const g=k/1048576;return(g<800?g.toFixed(1)+' GiB':(k/1073741824).toFixed(2)+' TiB')};
function zoneLegend(o){
  let from=o.min,out=[];
  for(let i=0;i<o.zones.length;i++){const z=o.zones[i],first=i===0,last=i===o.zones.length-1;
    const txt=first?`&lt;${Math.round(z.to)}`:last?`≥${Math.round(from)}`:`${Math.round(from)} to ${Math.round(z.to)}`;
    out.push(`<span style="color:var(--${z.c})">${txt}${o.unit||''}</span>`);from=z.to}
  return out.join(' &nbsp;');
}
function gaugeSVG(o){
  const cx=100,cy=100,r=78,sw=14;
  const frac=v=>Math.max(0,Math.min(1,(v-o.min)/(o.max-o.min)));
  const rot=v=>-90+180*frac(v);
  const pt=(v,rad)=>{const a=(90-rot(v))*Math.PI/180;return[cx+rad*Math.cos(a),cy-rad*Math.sin(a)]};
  let arcs='',from=o.min;
  for(const z of o.zones){const[x0,y0]=pt(from,r),[x1,y1]=pt(z.to,r);
    arcs+=`<path d="M ${x0.toFixed(2)},${y0.toFixed(2)} A ${r},${r} 0 0 1 ${x1.toFixed(2)},${y1.toFixed(2)}" stroke-width="${sw}" fill="none" style="stroke:var(--${z.c})"/>`;from=z.to}
  const has=o.value!=null&&!isNaN(o.value);
  const v=has?Math.max(o.min,Math.min(o.max,o.value)):o.min;
  return `<div class="gauge"><svg viewBox="0 0 200 112">${arcs}
    <line class="needle" x1="${cx}" y1="${cy}" x2="${cx}" y2="${cy-r+12}" style="stroke:var(--fg);stroke-width:3" opacity="${has?1:.25}" transform="rotate(${rot(v)} ${cx} ${cy})"/>
    <circle cx="${cx}" cy="${cy}" r="4" style="fill:var(--fg)"/></svg>
    <div class="val">${has?o.value:'--'}${has?(o.unit||''):''}</div><div class="lbl">${esc(o.label)}</div>
    ${o.sub?`<div class="sub">${o.sub}</div>`:''}
    <div class="legend">${zoneLegend(o)}</div></div>`;
}
function renderGauges(){
  const wet=S.sta_freq?num(S.sta_rssi):null;
  const g=[
    {min:0,max:70,value:num(S.ssd_temp_c),label:'SSD temperature',unit:' \u00b0C',zones:[{to:55,c:'ok'},{to:65,c:'warn'},{to:70,c:'bad'}]},
    {min:0,max:100,value:S.ssd_mounted==='yes'?pctOf(S.ssd_used_k,S.ssd_size_k):null,label:'SSD used',unit:'%',
      sub:S.ssd_mounted==='yes'?`${human(S.ssd_used_k)} of ${human(S.ssd_size_k)}`:null,zones:[{to:80,c:'ok'},{to:92,c:'warn'},{to:100,c:'bad'}]},
    {min:0,max:100,value:pctOf(S.int_used_k,S.int_size_k),label:'Internal storage used',unit:'%',
      sub:S.int_size_k?`${human(S.int_used_k)} of ${human(S.int_size_k)}`:null,zones:[{to:80,c:'ok'},{to:92,c:'warn'},{to:100,c:'bad'}]},
    {min:0,max:100,value:S.sd_present==='yes'?pctOf(S.sd_used_k,S.sd_size_k):null,label:'microSD used',unit:'%',
      sub:S.sd_present==='yes'?`${human(S.sd_used_k)} of ${human(S.sd_size_k)}`:null,zones:[{to:80,c:'ok'},{to:92,c:'warn'},{to:100,c:'bad'}]},
    {min:0,max:100,value:num(S.bat_level),label:'Battery',unit:'%',zones:[{to:15,c:'bad'},{to:30,c:'warn'},{to:100,c:'ok'}]},
    {min:0,max:50,value:S.bat_temp?num(S.bat_temp)/10:null,label:'Battery temperature',unit:' \u00b0C',zones:[{to:45,c:'ok'},{to:50,c:'bad'}]},
    {min:-90,max:-30,value:wet,label:'Wi-Fi signal',unit:' dBm',zones:[{to:-80,c:'bad'},{to:-67,c:'warn'},{to:-30,c:'ok'}]},
    {min:0,max:300,value:(W&&W.info)?num(W.info.internet_ms):null,label:'Internet latency',unit:' ms',zones:[{to:80,c:'ok'},{to:200,c:'warn'},{to:300,c:'bad'}]},
  ];
  $('gauges').innerHTML=g.map(gaugeSVG).join('');
}
let cockpitOn=false;
function toggleCockpit(){cockpitOn=!cockpitOn;$('cockpit').hidden=!cockpitOn;$('normal').hidden=cockpitOn;
  $('cockpitbtn').className=cockpitOn?'pri':'';if(cockpitOn)renderGauges()}

let historyOn=false,histData=null,histView='cards',histFocus='ssd_temp_c';
let histSettingsLoaded=false,histSaving=false,histDirty=false,histRevision=0,histInvalid=new Set();
function toggleHistory(){historyOn=!historyOn;$('history').hidden=!historyOn;$('normal').hidden=historyOn;
  $('historybtn').className=historyOn?'pri':'';if(historyOn)loadHistory()}
async function loadHistory(){
  $('histretry').hidden=true;
  if(!histSaving)$('histmessage').textContent=histDirty?'Unsaved graph settings.':'';
  try{
    if(!histSettingsLoaded){
      const settings=await histRequest('/api/history/settings');
      histOptions=settings.metrics;histFocus=settings.focus;histSettingsLoaded=true;
      renderHistOptions();setHistView(settings.view,false);
    }
    const hours=+$('histrange').value;
    histData=await histRequest('/api/history?hours='+hours);
    renderHistCharts();renderHistEvents();renderHistLocation();
  }catch(e){
    $('histmessage').textContent='Could not load history: '+e.message;
    $('histretry').hidden=false;
  }
}
async function histRequest(url,options){
  const r=await fetch(url,options);
  const text=await r.text();
  if(!r.ok){
    let message=text;
    if((r.headers.get('content-type')||'').includes('application/json'))message=JSON.parse(text).error||text;
    throw new Error(r.status===401?'Session expired; refresh and sign in again.':message||'HTTP '+r.status);
  }
  return JSON.parse(text);
}
const HIST_METRICS=[
  {k:'ssd_temp_c',label:'SSD temperature (°C)',color:'#dc2626'},
  {k:'bat_temp_c',label:'Battery temperature (°C)',color:'#d97706'},
  {k:'bat_level',label:'Battery (%)',color:'#16a34a'},
  {k:'sta_rssi',label:'Wi-Fi signal (dBm)',color:'#0b72b9'},
  {k:'sta_link',label:'Wi-Fi link speed (Mbps)',color:'#0369a1'},
  {k:'internet_ms',label:'Internet latency (ms)',color:'#7c3aed'},
  {k:'accel_pp',label:'Tablet movement (accelerometer, m/s2 peak-to-peak)',color:'#be185d'},
];
let histOptions=Object.fromEntries(HIST_METRICS.map(m=>[m.k,{enabled:m.k===histFocus,color:m.color,min:null,max:null}]));
function updateHistSave(){
  $('histsave').disabled=!histSettingsLoaded||histSaving||histInvalid.size>0||!histDirty;
}
function histChanged(){
  histDirty=true;histRevision++;
  $('histmessage').textContent=histInvalid.size?'Fix invalid ranges before saving.':'Unsaved graph settings.';
  updateHistSave();
}
function renderHistOptions(){
  $('histoptions').innerHTML=HIST_METRICS.map(m=>{
    const o=histOptions[m.k],custom=o.min!==null;
    return `<fieldset class="histoption"><legend>${esc(m.label)}</legend>
      <label><input type="checkbox" id="he_${m.k}" ${o.enabled?'checked':''} onchange="enableHistMetric('${m.k}',this.checked)">Enable in Focus</label>
      <label>Color <input type="color" aria-label="${esc(m.label)} color" value="${o.color}" onchange="colorHistMetric('${m.k}',this.value)"></label>
      <label><input type="checkbox" id="ha_${m.k}" ${custom?'':'checked'} onchange="scaleHistMetric('${m.k}')">Auto range</label>
      <label>Min <input type="number" step="any" min="-1000000000000" max="1000000000000" id="hmin_${m.k}" value="${custom?o.min:''}" ${custom?'':'disabled'} onchange="scaleHistMetric('${m.k}')"></label>
      <label>Max <input type="number" step="any" min="-1000000000000" max="1000000000000" id="hmax_${m.k}" value="${custom?o.max:''}" ${custom?'':'disabled'} onchange="scaleHistMetric('${m.k}')"></label>
      <div class="t-bad" id="herr_${m.k}" role="alert"></div></fieldset>`;
  }).join('');
}
function enableHistMetric(key,enabled){
  histOptions[key].enabled=enabled;
  $('he_'+key).checked=enabled;
  histChanged();renderHistCharts();
}
function colorHistMetric(key,color){
  histOptions[key].color=color;histChanged();renderHistCharts();
}
function scaleHistMetric(key){
  const auto=$('ha_'+key).checked,low=$('hmin_'+key),high=$('hmax_'+key);
  low.disabled=high.disabled=auto;
  const min=low.valueAsNumber,max=high.valueAsNumber;
  const valid=auto||(Number.isFinite(min)&&Number.isFinite(max)&&Math.abs(min)<=1e12&&Math.abs(max)<=1e12&&min<max);
  const message=valid?'':'Enter min < max, both within +/-1e12.';
  low.setCustomValidity(message);high.setCustomValidity(message);$('herr_'+key).textContent=message;
  if(valid){
    histInvalid.delete(key);histOptions[key].min=auto?null:min;histOptions[key].max=auto?null:max;
  }else histInvalid.add(key);
  histChanged();if(valid)renderHistCharts();
}
async function saveHistSettings(){
  if(!histSettingsLoaded||histSaving||histInvalid.size)return;
  const revision=histRevision;
  const settings={view:histView,focus:histFocus,metrics:histOptions};
  histSaving=true;updateHistSave();$('histmessage').textContent='Saving settings...';
  try{
    await histRequest('/api/history/settings',{method:'POST',headers:{'Content-Type':'application/json','X-Tabctl':'1'},body:JSON.stringify(settings)});
    if(revision===histRevision){histDirty=false;$('histmessage').textContent='Settings saved on the tablet.'}
    else $('histmessage').textContent='Earlier settings saved; you have newer unsaved changes.';
  }catch(e){$('histmessage').textContent='Could not save settings: '+e.message}
  finally{histSaving=false;updateHistSave()}
}
function setHistView(view,dirty=true){
  histView=view;
  for(const b of $('histview').querySelectorAll('button')){
    const active=b.dataset.view===view;
    b.setAttribute('aria-pressed',String(active));b.classList.toggle('pri',active);
  }
  $('histhelp').textContent=view==='overlay'?
    'Overlaid trends: each metric is scaled independently from 0 to 100% of its auto or custom range (shown below), not its actual units.':
    view==='focus'?'Enabled graphs are overlaid. Click a mini-plot to enable and highlight it; use Graph settings to disable graphs. Multiple graphs use independent scales.':
    'Each metric is shown in its own card.';
  if(dirty)histChanged();
  renderHistCharts();
}
function focusHistMetric(key){
  histFocus=key;enableHistMetric(key,true);
  $('ht_'+key).focus();
}
function histSeries(metric,samples){
  return {...metric,...histOptions[metric.k],pts:samples.map(r=>[r.t,r[metric.k]])};
}
function seriesRange(pts){
  let min=Infinity,max=-Infinity;
  for(const [,v] of pts)if(Number.isFinite(v)){min=Math.min(min,v);max=Math.max(max,v)}
  return min===Infinity?null:{min,max};
}
function histRangeLabel(series){
  const range=histPlotRange(series);
  const fmt=v=>Number(v.toPrecision(6)).toString();
  const label=range?`${fmt(range.min)} to ${fmt(range.max)} (${series.min===null?'auto':'custom'})`:'no data';
  return range&&!seriesRange(series.pts)?label+'; no data':label;
}
function histPlotRange(series){
  return series.min!==null?{min:series.min,max:series.max}:seriesRange(series.pts);
}
function histLegend(series){
  return '<div class="histlegend">'+series.map(m=>`<span><span class="histkey" style="background:${m.color}"></span>${esc(m.label)}: ${histRangeLabel(m)}</span>`).join('')+'</div>';
}
function renderHistCharts(){
  const S=histData?histData.samples:[];
  const box=$('histcharts');
  box.className='histgrid '+histView;
  if(!S.length){box.innerHTML='<p class="dim">No samples yet. They are taken every minute.</p>';return}
  const series=HIST_METRICS.map(m=>histSeries(m,S));
  const chart=(m,large=false)=>`<div class="histchart${large?' large':''}"><canvas id="hc_${m.k}" role="img" aria-label="${esc(m.label)} history"></canvas><div class="lbl">${esc(m.label)}</div></div>`;
  if(histView==='overlay'){
    box.innerHTML='<div class="histchart large"><canvas id="hc_overlay" role="img" aria-label="Overlaid history trends, independently scaled"></canvas></div>'+
      histLegend(series);
    drawHistSeries($('hc_overlay'),series,true);
  }else if(histView==='focus'){
    const enabled=series.filter(m=>m.enabled),selected=enabled.find(m=>m.k===histFocus);
    box.innerHTML=(enabled.length?'<div class="histchart large"><canvas id="hc_focus" role="img" aria-label="Enabled history graphs"></canvas><div class="lbl">'+
      (selected?'Highlighted: '+esc(selected.label):'Enabled graphs')+'</div></div>'+histLegend(enabled):
      '<p class="dim">No graphs enabled. Select a mini-plot or enable a graph in Graph settings.</p>')+
      '<div class="histthumbs" role="group" aria-label="Choose focused plot">'+
      series.map(m=>`<button type="button" class="histchart${m.enabled?'':' histoff'}" id="ht_${m.k}" aria-pressed="${m.enabled&&m.k===histFocus}" onclick="focusHistMetric('${m.k}')"><canvas id="hm_${m.k}" aria-hidden="true"></canvas><span class="lbl">${esc(m.label)}${m.enabled?'':' (off)'}</span></button>`).join('')+'</div>';
    if(enabled.length)drawHistSeries($('hc_focus'),enabled,enabled.length>1);
    for(const m of series)drawHistSeries($('hm_'+m.k),[m],false,true);
  }else{
    box.innerHTML=series.map(m=>chart(m)).join('');
    for(const m of series)drawHistSeries($('hc_'+m.k),[m]);
  }
}
function drawHistSeries(canvas,series,normalized=false,mini=false){
  const dpr=window.devicePixelRatio||1,w=canvas.clientWidth||280,h=canvas.clientHeight||110;
  canvas.width=w*dpr;canvas.height=h*dpr;
  const ctx=canvas.getContext('2d');ctx.scale(dpr,dpr);
  const dim=getComputedStyle(document.documentElement).getPropertyValue('--dim').trim();
  const valid=series.filter(s=>seriesRange(s.pts)).map(s=>({...s,range:histPlotRange(s)}));
  if(!valid.length){ctx.fillStyle=dim;ctx.font='12px system-ui';ctx.fillText('no data',8,h/2);return}
  const pad=mini?{l:2,r:2,t:4,b:4}:{l:52,r:8,t:8,b:32};
  const pts=series[0].pts,t0=pts[0][0],t1=pts[pts.length-1][0];
  let vMin=normalized?0:valid[0].range.min,vMax=normalized?100:valid[0].range.max;
  if(vMin===vMax){vMin-=1;vMax+=1}
  const x=t=>pad.l+(w-pad.l-pad.r)*(t1>t0?(t-t0)/(t1-t0):0);
  const y=v=>h-pad.b-(h-pad.t-pad.b)*(v-vMin)/(vMax-vMin);
  ctx.strokeStyle=getComputedStyle(document.documentElement).getPropertyValue('--line').trim();
  ctx.lineWidth=1;
  ctx.beginPath();ctx.moveTo(pad.l,pad.t);ctx.lineTo(pad.l,h-pad.b);ctx.lineTo(w-pad.r,h-pad.b);ctx.stroke();
  if(!mini){
    const label=v=>Number(v.toPrecision(6))+(normalized?'%':'');
    ctx.fillStyle=dim;ctx.font='10px system-ui';ctx.textAlign='right';
    ctx.fillText(label(vMax),pad.l-3,pad.t+9);
    ctx.fillText(label(vMin),pad.l-3,h-pad.b);
    const date=t=>new Date(t*1000).toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
    ctx.textAlign='left';ctx.fillText(date(t0),pad.l,h-14);
    ctx.textAlign='right';ctx.fillText(date(t1),w-pad.r,h-2);
  }
  ctx.save();ctx.beginPath();ctx.rect(pad.l,pad.t,w-pad.l-pad.r,h-pad.t-pad.b);ctx.clip();
  for(const s of valid){
    ctx.strokeStyle=s.color;ctx.fillStyle=s.color;ctx.lineWidth=mini?1.5:histView==='focus'&&s.k===histFocus?3:2;ctx.beginPath();
    let connected=false;
    for(const [t,v] of s.pts){
      if(!Number.isFinite(v)){connected=false;continue}
      const value=normalized?(s.range.max===s.range.min?50:100*(v-s.range.min)/(s.range.max-s.range.min)):v;
      const px=x(t),py=y(value);
      if(connected)ctx.lineTo(px,py);else ctx.moveTo(px,py);
      connected=true;
      ctx.fillRect(px-1,py-1,2,2);
    }
    ctx.stroke();
  }
  ctx.restore();
}
let histResizeFrame;
window.addEventListener('resize',()=>{
  cancelAnimationFrame(histResizeFrame);
  histResizeFrame=requestAnimationFrame(()=>{if(historyOn)renderHistCharts()});
});
function renderHistEvents(){
  const ev=histData?histData.events:[],box=$('histssdevents');
  $('histssdsum').textContent=histData?` - ${histData.disconnects_total} total, ${histData.disconnects_unsafe} unsafe`:'';
  if(!ev.length){box.innerHTML='None recorded.';return}
  box.innerHTML=ev.map(e=>{
    const when=new Date(e.t*1000).toLocaleString();
    if(e.event==='reconnect')return `<div class="evt"><span>${when}</span><span class="dim">reconnected</span></div>`;
    const safe=e.safe===1;
    return `<div class="evt"><span>${when}</span><span class="${safe?'t-ok':'t-bad'}">${safe?'disconnected (ejected first)':'disconnected without Eject'}</span></div>`;
  }).join('');
}
function renderHistLocation(){
  const S=histData?histData.samples:[],box=$('histloc');
  const last=[...S].reverse().find(r=>r.lat!=null&&r.lon!=null);
  if(!last){box.innerHTML='No location fix recorded yet.';return}
  const ageMin=Math.round((Date.now()/1000-last.t)/60);
  const acc=last.loc_acc_m!=null?` (+/-${Math.round(last.loc_acc_m)} m)`:'';
  const when=new Date(last.t*1000).toLocaleString();
  box.innerHTML=`<div>${last.lat.toFixed(5)}, ${last.lon.toFixed(5)}${acc}</div>`+
    `<div class="dim" style="margin-top:2px">${when} (${ageMin} min ago)</div>`+
    `<div class="btns" style="margin-top:6px"><a href="https://www.google.com/maps?q=${last.lat},${last.lon}" target="_blank" rel="noopener"><button type="button">Open in Maps</button></a></div>`;
}

function issues(){
  const I=[];const add=(lvl,t)=>I.push([lvl,t]);
  if(!S.root_ok){add('bad','No root: su did not answer as uid 0. Open Magisk, Superuser, and allow Termux. After a LineageOS update, redo runbook Phase 3.');return I}
  if(!S.ap_if)add('bad','The hotspot is off.');
  else if(+S.ap_freq<3000){
    if(dfs(S.sta_freq))add('warn',`The hotspot runs on 2.4 GHz because the home Wi-Fi is on DFS channel ${chan(S.sta_freq)}, where a hotspot may not transmit. Set the home router's 5 GHz to channel 36-48 or 149-161.`);
    else if(S.ap_cfg_band==='1')add('warn','The hotspot is set to 2.4 GHz. Open hotspot settings and choose 5 GHz.');
    else if(+S.sta_freq&&+S.sta_freq<3000)add('warn','The hotspot runs on 2.4 GHz because the tablet itself is on a 2.4 GHz network.');
    else add('warn','The hotspot runs on 2.4 GHz. Turning it off and on again may move it to 5 GHz.');
  }
  if(S.ap_cfg_autooff==='true')add('warn','The hotspot turns itself off when nobody is connected. Open hotspot settings and turn that off.');
  if(!S.ts_ip)add('bad','Tailscale has no 100.x address. Open the Tailscale app and connect.');
  if(!S.port_8022)add('bad','sshd is not running. Press Restart sshd.');
  if(!S.port_8033||!S.port_8035||!S.port_8080)add('warn','meridian is not fully up (8033, 8035, 8080). Press Restart meridian.');
  if(S.ssd_ejected==='yes')add('warn','The SSD is ejected. Unplug it, or press Reconnect in the SSD card.');
  else if(!S.ssd_disk)add('warn',S.ssd_stale_mount==='yes'?'The SSD was unplugged without Eject: its old mount is still there but unreadable'+(S.sdbuf_target==='buffer'?', and the SD buffer takes new files.':'.')+' Plug it back in, or lock it in the SSD card.':'The SSD is not detected. Check its power supply and cable.');
  else if(S.ssd_open!=='yes')add('warn','The SSD is locked. Type the passphrase in the SSD card.');
  else if(S.ssd_mounted!=='yes')add('bad','The SSD is unlocked but not mounted. Press Unlock again (it runs drive-up.sh).');
  if(S.ssd_mounted==='yes'&&(S.smb_tunnel!=='up'||S.smb_lan!=='up'))add('warn','A Samba server is down. Press Restart Samba.');
  if(S.mnt_ns!=='0')add('warn','Magisk mount namespace is not Global: the SSD mount would be invisible to apps. Press Re-apply Android settings.');
  if(S.phantom!=='false')add('warn','Android may kill Termux background processes (phantom process monitor is on). Press Re-apply Android settings.');
  if(S.idle_termux!=='yes'||S.idle_tailscale!=='yes')add('warn','Termux or Tailscale is not exempt from battery optimisation. Press Re-apply Android settings.');
  if(S.tsu_patched!=='yes')add('warn','sudo over SSH will fail (tsu lost its patch, probably after pkg upgrade). Press Fix tsu.');
  if(+S.bat_temp>450)add('warn',`Battery is hot: ${(S.bat_temp/10).toFixed(1)} °C.`);
  return I;
}

// Battery current, from Samsung's battery driver (mA, negative while the battery drains; its running
// average). The tablet does not measure what the charger gives, so while it is plugged in its own
// consumption is unknown; when the battery drains anyway, that drain is a floor for it.
function batRows(){
  const n=v=>{const x=num(v);return Number.isFinite(x)?x:null};
  const ia=n(S.bat_current_avg_ma),ma=v=>Math.abs(Math.round(v))+' mA';
  if(ia===null)return row('Charging current','?')+row('Consumption current','?');
  if(![S.bat_ac,S.bat_usb,S.bat_wireless].includes('true'))
    return row('Charging current','none (unplugged)')+row('Consumption current',ma(ia)+' from the battery','ok');
  if(ia>=0)return row('Charging current',ma(ia)+' into the battery','ok')+row('Consumption current','not measured while plugged in');
  const mv=n(S.bat_input_mv),ua=n(S.bat_max_charge_ua);
  const sup=[mv?(mv/1000).toFixed(1)+' V':'',ua?'up to '+(ua/1e6).toFixed(1)+' A':''].filter(Boolean).join(', ');
  return row('Charging current',`none: plugged in, but the battery drains ${ma(ia)}${sup?' (supply '+sup+')':''}`,'warn')+
    row('Consumption current',`more than ${ma(ia)}: the battery's ${ma(ia)} plus the charger's share, which is not measured`,'warn');
}

function render(){
  const I=issues();const worst=I.some(i=>i[0]==='bad')?'bad':I.length?'warn':'ok';
  $('health').innerHTML=`<span class="dot ${worst}"></span>${worst==='ok'?'All good':I.length+' to check'}`;
  $('when').textContent='Updated '+(S.now||'');
  $('issues').innerHTML=I.length?'<ul class="issues">'+I.map(([l,t])=>`<li class="t-${l}">${esc(t)}</li>`).join('')+'</ul>':'<span class="t-ok">Nothing. Everything checks out.</span>';

  const sf=+S.sta_freq,af=+S.ap_freq;
  let w=row('Home Wi-Fi',sf?`${S.ssid?esc(S.ssid)+', ':''}${band(sf)}, channel ${chan(sf)}${dfs(sf)?' <span class="t-warn">(DFS)</span>':''}`:'<span class="t-bad">not connected</span>',sf?(dfs(sf)?'warn':'ok'):'bad');
  if(sf)w+=row('Signal / link',`${esc(S.sta_rssi)} dBm, ${esc(S.sta_link)} Mbps`);
  w+=row('Hotspot',S.ap_if?`on, ${band(af)} channel ${chan(af)}, ${WIDTH[S.ap_bw]||'?'}`:'<span class="t-bad">off</span>',S.ap_if?(af<3000?'warn':'ok'):'bad');
  if(S.ap_if)w+=row('Hotspot clients',esc(S.ap_clients)+` <span class="dim">(${esc(S.ap_ip||'')})</span>`);
  w+=row('Band setting',esc(CFGBAND[S.ap_cfg_band]||S.ap_cfg_band||'?'));
  w+=row('Security',esc(SEC[S.ap_cfg_security]||S.ap_cfg_security||'?'));
  w+=row('Turn off automatically',S.ap_cfg_autooff==='false'?'off':'<span class="t-warn">on</span>',S.ap_cfg_autooff==='false'?'ok':'warn');
  $('wifi').innerHTML=w;
  { const sel=$('wsel'),saved=(S.wifi_saved||'').split('|').filter(Boolean),key=saved.join('|');   // the saved networks: a list that stays put while you choose
    if(sel.dataset.key!==key&&document.activeElement!==sel){sel.innerHTML=saved.map(n=>`<option>${esc(n)}</option>`).join('');sel.dataset.key=key}
    if(document.activeElement!==sel&&S.ssid&&saved.includes(S.ssid))sel.value=S.ssid;
    $('wnote').textContent=S.wifi_switch||''; }

  let d=row('Drive',S.ssd_disk?`CT4000MX500SSD1, ${esc(S.ssd_gib)} GiB (/dev/block/${esc(S.ssd_disk)})`:S.ssd_ejected==='yes'?'ejected, safe to unplug':'<span class="t-warn">not detected</span>',S.ssd_disk||S.ssd_ejected==='yes'?'ok':'warn');
  if(S.ssd_disk)d+=row('Format',S.ssd_type==='crypt_LUKS'||S.ssd_type==='crypto_LUKS'?'LUKS2':`<span class="t-bad">${esc(S.ssd_type||'unknown')}</span>`,S.ssd_type&&S.ssd_type.includes('LUKS')?'ok':'bad');
  if(S.ssd_disk&&S.ssd_temp_c)d+=row('Temperature',S.ssd_temp_c+' C',+S.ssd_temp_c>=65?'bad':+S.ssd_temp_c>=55?'warn':'ok');
  d+=row('Lock',S.ssd_open==='yes'?'unlocked':'locked',S.ssd_open==='yes'?'ok':'warn');
  d+=row('Mounted at /mnt/ssd',S.ssd_mounted==='yes'?'yes':'no',S.ssd_mounted==='yes'?'ok':(S.ssd_open==='yes'?'bad':''));
  if(S.ssd_mounted==='yes'){const sz=+S.ssd_size_k,us=+S.ssd_used_k,av=+S.ssd_avail_k;d+=`<div class="bar"><div style="width:${Math.max(1,Math.round(us*100/sz))}%"></div></div><div class="dim" style="font-size:13px">${tib(us)} used, ${tib(av)} free of ${tib(sz)}</div>`}
  d+=row('Unlock at boot (key file)',S.keyfile==='yes'?'yes':'no');
  $('ssd').innerHTML=d;
  $('unlockf').style.display=S.ssd_disk&&S.ssd_open!=='yes'?'block':'none';
  const ej=`<button class="danger" data-act="eject" data-confirm="Eject the SSD? It is locked first if needed (both Samba shares stop and anyone copying loses the connection), then detached from USB so it is safe to unplug.">Eject (safe to unplug)</button>`;
  $('ssdbtns').innerHTML=S.ssd_ejected==='yes'?`<button class="pri" data-act="reconnect">Reconnect</button>`:
    S.ssd_open==='yes'?`<button data-act="up">Mount and start Samba</button><button class="danger" data-act="lock" data-confirm="Lock the SSD? Both Samba shares stop and anyone copying loses the connection. It stays attached, so you can unlock it again here.">Lock</button>`+ej:
    S.ssd_disk?ej:'';

  const sm=(n,label,p)=>row(label,S['smb_'+n]==='up'?`up on :${p}, ${esc(S['smb_'+n+'_sessions'])} session(s)`:'<span class="t-warn">down</span>',S['smb_'+n]==='up'?'ok':'warn');
  $('smb').innerHTML=sm('tunnel','Mac (via SSH tunnel)','8040 (127.0.0.1)')+sm('lan','Windows / LAN / Tailscale','445')+
    row('Windows path',S.ts_ip?`<code>\\\\${esc(S.ts_ip)}\\usb</code>`:'-')+row('Mac path','<code>smb://milad@localhost:8040/usb</code>');

  $('net').innerHTML=row('Tailscale',S.ts_ip?esc(S.ts_ip):'<span class="t-bad">no address</span>',S.ts_ip?'ok':'bad')+
    row('sshd',port(8022),S.port_8022?'ok':'bad')+row('aponno RDP',port(8033,'warn'),S.port_8033?'ok':'warn')+
    row('obsidian RDP',port(8035,'warn'),S.port_8035?'ok':'warn')+row('Web proxy',port(8080,'warn'),S.port_8080?'ok':'warn');
  $('mlog').textContent=S.meridian_log||'(empty)';

  $('sys').innerHTML=S.root_ok?
    row('Root',`uid 0, Magisk ${esc(S.magisk)}`,'ok')+row('SELinux',esc(S.selinux))+
    row('Mount namespace',S.mnt_ns==='0'?'Global':`<span class="t-warn">${esc(S.mnt_ns||'?')}</span>`,S.mnt_ns==='0'?'ok':'warn')+
    row('Phantom process monitor',S.phantom==='false'?'off':'<span class="t-warn">on</span>',S.phantom==='false'?'ok':'warn')+
    row('Battery exemption',`Termux ${S.idle_termux==='yes'?'yes':'no'}, Tailscale ${S.idle_tailscale==='yes'?'yes':'no'}`,S.idle_termux==='yes'&&S.idle_tailscale==='yes'?'ok':'warn')+
    row('Termux wake locks',esc(S.wakelocks_termux),+S.wakelocks_termux>0?'ok':'warn')+
    row('tsu patch (sudo over SSH)',S.tsu_patched==='yes'?'yes':'<span class="t-warn">no</span>',S.tsu_patched==='yes'?'ok':'warn')
    :row('Root','<span class="t-bad">not available</span>','bad')+`<pre>${esc(S.root_error||'')}</pre>`;

  const LV={full:'Full',meeting:'Meeting',router:'Router',minimal:'Minimal',custom:'Custom (matches no level)'};
  const onoff=v=>v==='1'?'on':v==='0'?'off':esc(v||'?');
  const tmo=+S.screen_timeout_ms;
  const ml=/^-?\d+$/.test(S.meeting_left_s||'')?+S.meeting_left_s:null;   // a running meeting: seconds to its end
  const back=S.meeting_back&&S.meeting_back!=='custom'?(LV[S.meeting_back]||esc(S.meeting_back)):'the settings from before';
  const hm=ms=>{const d=new Date(ms);return String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0')};
  $('psave').innerHTML=row('Level',esc(LV[S.power_level]||S.power_level||'?'),S.power_level&&S.power_level!=='custom'?'ok':'warn')+
    (ml!==null?row('Meeting ends',ml>0?`in ${mins(Math.ceil(ml/60))} (at ${hm(Date.now()+ml*1000)}), then back to ${back}`:`now: going back to ${back}`,ml>0?'ok':'warn')
     :S.power_level==='meeting'?row('Meeting ends','never: no length was set. Press Meeting and choose one, or choose another level.','warn'):'')+
    row('Bluetooth',onoff(S.bt_on))+row('Location',S.loc_mode==='0'?'off':S.loc_mode?`on (mode ${esc(S.loc_mode)})`:'?')+
    row('Background scanning',`Wi-Fi ${onoff(S.wifi_scan_always)}, Bluetooth ${onoff(S.ble_scan_always)}`)+
    row('Screen timeout',tmo>=120000?Math.round(tmo/60000)+' min':tmo>0?Math.round(tmo/1000)+' s':'?')+
    row('Screensaver when docked',onoff(S.screensaver))+
    row('ChatGPT in the background',S.idle_chatgpt==='yes'?'not limited':S.idle_chatgpt==='no'?'battery optimised (Meeting lifts this)':'?',
      S.power_level==='meeting'?(S.idle_chatgpt==='yes'?'ok':'warn'):'');
  $('psavebtns').innerHTML=['full','meeting','router','minimal'].map(l=>`<button ${l==='meeting'?'data-ask="meeting"':`data-act="power-${l}"`}${S.power_level===l?' class="pri"':''}>${LV[l]}</button>`).join('')+
    (ml!==null?'<button data-act="power-meeting-end">End meeting</button>':'');

  const st={2:'charging',3:'discharging',4:'not charging',5:'full'}[S.bat_status]||'?';
  $('dev').innerHTML=row('Battery',`${esc(S.bat_level)}%, ${st}${S.bat_ac==='true'?' (AC)':S.bat_usb==='true'?' (USB)':''}`,+S.bat_level<20?'warn':'ok')+batRows()+
    row('Battery temperature',S.bat_temp?(S.bat_temp/10).toFixed(1)+' °C':'?',+S.bat_temp>450?'warn':'ok')+row('Up for',S.uptime_s?dur(S.uptime_s):'?');
  if(cockpitOn)renderGauges();
}

async function refresh(force){
  if(busy&&!force)return;
  try{const r=await fetch('/api/status',{cache:'no-store'});if(r.status===401){location.reload();return}S=await r.json();render()}
  catch(e){$('when').textContent='Cannot reach the tablet: '+e.message}
}

async function act(name,extra,label){
  if(busy)return;busy=true;document.querySelectorAll('button').forEach(b=>b.disabled=true);
  $('busy').textContent='running '+(label||name)+'...';$('out').textContent='';
  // a level change takes about 7 s (it ends with a check that the home Wi-Fi survived) and the tablet answers nothing else meanwhile:
  // show the level that was pressed at once, and say what is going on; the real answer replaces this when it comes
  if(/^power-(full|meeting|router|minimal)$/.test(name)){S.power_level=name.slice(6);try{render()}catch(e){}
    $('out').textContent='Switching to '+name.slice(6)+' now. It takes about 7 s: the tablet finishes by checking that the home Wi-Fi survived.'}
  try{
    const r=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json','X-Tabctl':'1'},body:JSON.stringify({action:name,...(extra||{})})});
    const j=await r.json();$('out').textContent=(j.rc===0?'':'Exit code '+j.rc+'\n\n')+(j.out||'(no output)');
  }catch(e){$('out').textContent='Failed: '+e.message}
  busy=false;$('busy').textContent='';document.querySelectorAll('button').forEach(b=>b.disabled=false);refresh(true);
}

document.addEventListener('click',e=>{const pm=$('powermenu');if(pm.open&&!pm.contains(e.target))pm.open=false;
  const b=e.target.closest('button[data-act]');if(!b)return;if(pm.contains(b))pm.open=false;
  const q=b.dataset.confirm||CONFIRM[b.dataset.act];if(q&&!confirm(q))return;act(b.dataset.act,null,b.textContent)});
$('unlockf').addEventListener('submit',e=>{e.preventDefault();const p=$('pw').value;$('pw').value='';
  if(!p){$('out').textContent='Type the passphrase first.';return}act('unlock',{passphrase:p},'unlock (this takes a few seconds)')});
$('wgo').addEventListener('click',()=>{const n=$('wsel').value;if(!n)return;
  if(!confirm('Switch the tablet\'s Wi-Fi to "'+n+'"? The hotspot can restart and this page and SSH can drop for up to a minute while it changes.'))return;
  act('wifi-connect',{ssid:n},'Wi-Fi: '+n)});
// Meeting asks for its length first: 15 min to 24 h in 15 min steps, 30 min chosen to start with
function mins(m){const h=Math.floor(m/60),r=m%60;return h?(r?h+' h '+r+' min':h+' h'):r+' min'}
for(let m=15;m<=1440;m+=15)$('mtgmin').add(new Option(mins(m),m,m===30,m===30));
document.addEventListener('click',e=>{if(!e.target.closest('button[data-ask="meeting"]'))return;
  $('mtgask').hidden=false;$('mtgmin').focus()});
$('mtgno').addEventListener('click',()=>{$('mtgask').hidden=true});
$('mtggo').addEventListener('click',()=>{const m=+$('mtgmin').value;$('mtgask').hidden=true;
  act('power-meeting',{minutes:m},'Meeting for '+mins(m))});
function tick(){const d=new Date();
  $('clock-t').textContent=d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
  $('clock-d').textContent=d.toLocaleDateString([],{weekday:'short',day:'numeric',month:'short'});
  setTimeout(tick,1000-d.getMilliseconds()+5)}
tick();
// full screen: the standard API, or the webkit-prefixed one on older browsers; the button stays hidden without either
const DE=document.documentElement,fsEl=()=>document.fullscreenElement||document.webkitFullscreenElement;
const fsReq=DE.requestFullscreen||DE.webkitRequestFullscreen,fsExit=document.exitFullscreen||document.webkitExitFullscreen;
function fsSync(){const on=!!fsEl();$('fslbl').textContent=on?'Exit full screen':'Full screen';$('fsbtn').title=$('fslbl').textContent;
  $('fsico').setAttribute('d',on?'M9 4v5H4M15 4v5h5M9 20v-5H4M15 20v-5h5':'M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5')}
if(fsReq){$('fsbtn').hidden=false;
  $('fsbtn').addEventListener('click',()=>{const p=fsEl()?fsExit.call(document):fsReq.call(DE,{navigationUI:'hide'});
    if(p&&p.catch)p.catch(e=>{$('out').textContent='Full screen failed: '+e.message})});
  document.addEventListener('fullscreenchange',fsSync);document.addEventListener('webkitfullscreenchange',fsSync);fsSync()}
refresh(true);setInterval(()=>{if(!document.hidden)refresh(false)},10000);
// Connections: watch.py (root) checks every 5 s; this card reads its state every 3 s
let W=null,watchExpanded=false;const WL={ok:'ok',warn:'warn',fail:'bad'},WO={fail:0,warn:1,unknown:2,ok:3};
function toggleWatch(){watchExpanded=!watchExpanded;renderWatch()}
// the header's "last check N s ago" counts on every second between fetches
function watchWhen(){
  const age=W&&W.age!=null?W.age+(W._at?Date.now()/1000-W._at:0):null,stale=age==null||age>30||(W&&W.stopped);
  const cs=W&&W.checks||[],nb=cs.filter(c=>c.status=='fail').length,nw=cs.filter(c=>c.status=='warn').length;
  $('wwhen').textContent=(age==null?'watch has not run yet':stale?`watch NOT running (last pass ${age<120?Math.round(age)+' s':dur(age)} ago)`:`checked every ${W.tick} s · last check ${Math.max(0,Math.round(age))} s ago`)+
    (W&&!stale?(watchExpanded?' · showing all, click to collapse':(nb||nw)?' · showing problems only, click to see all':' · click to see every check'):'');
}
function renderWatch(){
  const stale=!W||W.age==null||W.age>30||W.stopped;
  $('wbtns').hidden=!stale;
  const nb=(W&&W.checks||[]).filter(c=>c.status=='fail').length,nw=(W&&W.checks||[]).filter(c=>c.status=='warn').length;
  watchWhen();
  let h=stale?row('Watch','<span class="t-bad">not running: nobody checks the connections.</span><div class="act">Tablotron is starting it; press Start the watch if this stays.</div>','bad'):'';
  const all=(W&&W.checks||[]).slice().sort((a,b)=>WO[a.status]-WO[b.status]);
  const C=watchExpanded?all:all.filter(k=>k.status=='fail'||k.status=='warn');
  if(!watchExpanded&&!stale&&all.length){const ok=all.filter(k=>k.status=='ok').length,ns=all.length-ok-nb-nw;
    h+=`<div class="row" style="cursor:pointer" onclick="toggleWatch()" title="Click to show every check"><span class="k"><span class="dot ${nb?'bad':nw?'warn':'ok'}"></span>${nb?nb+(nb>1?' problems':' problem'):nw?nw+(nw>1?' warnings':' warning'):'All '+ok+' checks OK'}</span><span class="v dim">${ok} of ${all.length} OK${ns?' · '+ns+' not checked':''}</span></div>`}
  for(const k of C){let v=esc(k.msg);
    if(k.status!=='ok'&&k.action)v+=`<div class="act">${esc(k.action)}</div>`;
    if(k.status!=='ok'&&k.fixes&&k.fixes.length)v+=`<div class="act">tried: ${esc(k.fixes[k.fixes.length-1])}</div>`;
    if(k.status!=='ok'&&k.dismissable)v+=`<div class="act"><button data-act="dismiss-${k.id}" data-confirm="Dismiss this warning? It comes back if the same thing happens again.">Dismiss</button></div>`;
    h+=row(k.label,v,WL[k.status]||'')}
  $('watch').innerHTML=h||(stale?'<span class="dim">No checks yet.</span>':'<span class="dim">All good. Click Connections to show every check.</span>');
}
async function refreshWatch(){
  try{const r=await fetch('/api/watch',{cache:'no-store'});if(r.status===401){location.reload();return}W=await r.json();W._at=Date.now()/1000;renderWatch();if(cockpitOn)renderGauges()}
  catch(e){$('wwhen').textContent='cannot reach the tablet: '+e.message}
}
refreshWatch();setInterval(()=>{if(!document.hidden)refreshWatch()},3000);setInterval(()=>{if(!document.hidden)watchWhen()},1000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh(false)});
</script></body></html>"""


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    local = "http://127.0.0.1:%d/?t=%s" % (PORT, TOKEN)
    if cmd == "url":
        print(local)
        ts = tailscale_ip()
        if ts:
            print("http://%s:%d/?t=%s" % (ts, PORT, TOKEN))
    elif cmd == "open":  # Android blocks a background app from starting the browser, so ask root's am
        rc, out = run_root("open-page", stdin=local + "\n", timeout=30)
        print(out or "Exit code %d" % rc)
    elif cmd == "status":
        print(json.dumps(get_status(), indent=1))
    elif cmd == "serve":
        srv = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
        srv.daemon_threads = True
        sys.stderr.write("tabctl listening on port %d\n" % PORT)
        srv.serve_forever()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
