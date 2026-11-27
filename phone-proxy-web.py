#!/usr/bin/env python3
"""Web UI for phone-proxy.sh: the same menu as the TUI, in an app window of its own.

Run:  phone-proxy.sh web [port]      (default port 8090, localhost only)
      python3 phone-proxy-web.py [port] [--replace | --keep] [--headless]

The page opens in Macotron.app, a small WebKit window (Safari's engine) with its own Dock icon and
no browser bars. This script builds it from macotron-app.swift into ~/.phone-proxy on first use
(under a minute) and again whenever that file changes; when it cannot (no swiftc), the page opens
in the default browser instead. --headless serves the page and opens nothing. Closing the window
leaves the server running. Opened from the Dock with nothing on the port, the app starts this
script itself (--headless --keep), the way the last run from a terminal noted in window.json.

Remote desktops & drive shares offers a saved Continuous monitoring toggle: every 30 seconds,
available targets get their missing tunnels opened and Samba shares mounted. Working connections
are left alone; unavailable targets are retried, never forcibly unmounted. Save Samba credentials
with Drive login first. Turning monitoring off leaves connections open; All off stops monitoring
before disconnecting. Automatic recovery output is available from the card's Recovery log button.
If recovery is already running, All off stops future checks immediately and asks you to retry
after that job finishes, so a mount cannot race an unmount.

The optional Wireless ADB card manages a paired, explicitly selected phone without root. Install
Android platform tools and scrcpy on the Mac, pair using the phone's pairing address/code, then
connect using its main Wireless debugging address. Codes are never saved. On a hotspot, empty
address fields default to the phone's gateway IP; enter each debugging port. Android VPN ownership
is reported separately from app-process status and does not prove Tailscale peer reachability.
Wireless debugging may need reconnecting after reboot/network changes. Root-only NAT and raw
disk operations remain disabled on non-rooted devices; SSH/proxy behavior is unchanged.
Restarting Macotron closes any scrcpy window it started; it can be opened again from the card.

Started while an earlier copy of this page holds the port, it replaces that copy: after asking
when run from a terminal (default yes), without asking when run without one, never with --keep,
which leaves that copy running and only opens the window on it.
A port held by any other program is refused. Settings > Restart Macotron runs the script again in
place (the same process, terminal and arguments, plus --headless --replace) and reloads the page;
it is refused while a job runs or when the script on disk does not compile, and open terminals end.

Every button runs the matching phone-proxy.sh command, so the TUI and this page
always agree. Only 127.0.0.1 is served, the Host header is checked, and every
request needs the token in ~/.phone-proxy/web.token (the opened URL carries it).
"""
import base64, fcntl, hashlib, json, os, plistlib, re, shutil, signal, shlex, struct, termios, secrets, socket, subprocess, sys, tempfile, threading, time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import devices   # the companion-device profiles (Raven, a spare phone): ~/.phone-proxy/devices.json
from adb_phone import PhoneADB
SCRIPT = os.path.join(HERE, "phone-proxy.sh")
HOME = os.path.expanduser("~")
STATE = os.path.join(HOME, ".phone-proxy")
WINDOW_APP = os.path.join(STATE, "Macotron.app")       # the page's own app window, built from WINDOW_SRC
WINDOW_SRC = os.path.join(HERE, "macotron-app.swift")
WINDOW_RUN = os.path.join(STATE, "window.json")         # how to start this server, for the app's Dock launches
CONF = os.path.join(HOME, ".phone-proxy-targets.conf")
NAT_DOMAIN = "tab.local"   # suffix of the fixed name nat-up.sh gives every nat=N target; nat-mdns.py answers it on the hotspot
ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
FLAGS = {a for a in sys.argv[1:] if a.startswith("--")}
HEADLESS = "--headless" in FLAGS   # serve only: no app window, no browser
PORT = int(ARGS[0]) if ARGS else 8090
APP_PORT, PROXY_PORT, SSH_PORT = 5678, 8080, 8022
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

os.makedirs(STATE, exist_ok=True)
TOKEN_FILE = os.path.join(STATE, "web.token")
if os.path.exists(TOKEN_FILE):
    TOKEN = open(TOKEN_FILE).read().strip()
else:
    TOKEN = secrets.token_urlsafe(24)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(TOKEN)
BOOT = secrets.token_hex(6)   # new with every start, a restart in place too: the page sees when it is back

# >>> bedrock-usage glue (patch_macotron.py) >>>
try:
    import bedrock_glue
except Exception:
    bedrock_glue = None
# <<< bedrock-usage glue <<<
# >>> reader glue (Reader tab, a web port of Milad.Mind.Mapper) >>>
try:
    import reader_glue
except Exception:
    reader_glue = None
# <<< reader glue <<<
# >>> aws auto-login glue >>>
try:
    import aws_autologin
except Exception:
    aws_autologin = None
# <<< aws auto-login glue <<<
# >>> offload glue (Offload tab: move data off the laptop to OneDrive + aponno, verified) >>>
try:
    import offload_glue
except Exception:
    offload_glue = None
# <<< offload glue <<<


# ---- status ---------------------------------------------------------------
def port_open(host, port, timeout=1.0):
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


def ping_ok(ip):
    try:
        return subprocess.run(["ping", "-c", "1", "-W", "2000", ip], capture_output=True, timeout=5).returncode == 0
    except Exception:
        return False


def sh(cmd, timeout=8):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def read_targets():
    rows = []
    try:
        for line in open(CONF):
            p = line.split()
            if not p or p[0].startswith("#"):
                continue
            p += [""] * (6 - len(p))
            rows.append({"name": p[0], "lport": int(p[1]) if p[1].isdigit() else 0, "rip": p[2], "rport": p[3],
                         "fwd": p[1].isdigit(),                      # "-" = nothing forwarded, NAT only
                         "tunnel": (p[4] or "yes") == "yes" and p[1].isdigit(),
                         "serve": p[2] == "usb:auto" or p[2].startswith("/"),
                         "nat": p[5][4:] if p[5].startswith("nat=") else ""})
    except FileNotFoundError:
        pass
    return rows


def share_listening(port):
    """Is the drive share answering behind the tunnel on localhost:port?

    The tunnel accepts the connection locally; if nothing listens on the tablet's end (the SSD
    is still locked after a reboot) ssh drops it at once, whereas a live Samba stays silent
    until the client speaks."""
    try:
        with socket.create_connection(("127.0.0.1", port), 1.0) as c:
            c.settimeout(0.4)
            try:
                return c.recv(1) != b""
            except socket.timeout:
                return True
    except OSError:
        return False


def toolbox_port(pid):
    for line in sh(["lsof", "-nP", "-a", "-p", pid, "-iTCP", "-sTCP:LISTEN", "-Fn"]).splitlines():
        if line.startswith("n") and ":" in line:
            return int(line.rsplit(":", 1)[1])
    return 0


def pid_alive(pidfile):
    try:
        pid = int(open(pidfile).read().strip())
        os.kill(pid, 0)
        return pid
    except Exception:
        return None


BEDROCK_CACHE_TTL = 60   # aws sts get-caller-identity is a network call; cache so it never gates a status poll
_bedrock_cache = {"ts": 0.0, "ok": False, "detail": "not checked yet"}


def check_bedrock():
    """Whether the AWS CLI's current session (what Bedrock login signs into) is still valid, per the same
    `aws sts get-caller-identity` check bedrock_login() itself runs before offering to log in again."""
    if time.time() - _bedrock_cache["ts"] < BEDROCK_CACHE_TTL:
        return _bedrock_cache["ok"], _bedrock_cache["detail"]
    out = sh(["aws", "sts", "get-caller-identity", "--cli-connect-timeout", "5", "--cli-read-timeout", "10",
              "--query", "[Account,Arn]", "--output", "text"], timeout=16).strip()
    ok, detail = bool(out), (out if out else "not signed in, or the session expired")
    _bedrock_cache.update(ts=time.time(), ok=ok, detail=detail)
    return ok, detail


def status():
    info = dict(kv.split("=", 1) for kv in sh(["bash", SCRIPT, "ip"]).split() if "=" in kv)
    gw, mac = info.get("phone", ""), info.get("mac", "")
    targets = read_targets()
    mounts = sh(["mount"])
    with ThreadPoolExecutor(8) as ex:
        sshd = ex.submit(port_open, gw, SSH_PORT) if gw else None
        prox = ex.submit(port_open, gw, PROXY_PORT) if gw else None
        tun = [ex.submit(port_open, "127.0.0.1", t["lport"]) if t["fwd"] else None for t in targets]
        shr = [ex.submit(share_listening, t["lport"]) if t["serve"] else None for t in targets]
        for t in targets:   # NAT: the address N in the hotspot /24, only while on the hotspot
            t["vip"] = "%s.%s" % (gw.rsplit(".", 1)[0], t["nat"]) if t["nat"] and info.get("mode") == "hotspot" and "." in gw else ""
            t["mname"] = "%s.%s" % (t["name"], NAT_DOMAIN) if t["nat"] else ""   # fixed name, same on every hotspot subnet
        nat = [(ex.submit(port_open, t["vip"], int(t["rport"])) if t["rport"].isdigit() else ex.submit(ping_ok, t["vip"])) if t["vip"] else None for t in targets]
        app = ex.submit(port_open, "127.0.0.1", APP_PORT)
        tb = ex.submit(lambda: sh(["pgrep", "-f", "[t]ool_manager\\.py"]).strip())
        bedrock = ex.submit(check_bedrock)
        for t, f, g, h in zip(targets, tun, shr, nat):
            t["up"] = f.result() if f else False
            t["natup"] = h.result() if h else None
            t["share"] = g.result() if g and t["up"] else None   # None = cannot tell (tunnel down)
            t["mounted"] = ""
            if t["serve"]:
                m = re.search(r"localhost:%d/%s on (\S+)" % (t["lport"], re.escape(t["name"])), mounts)
                t["mounted"] = m.group(1) if m else ""
        return {
            "iface": info.get("interface", ""), "mac": mac, "phone": gw,
            "net": bool(gw and mac), "mode": info.get("mode", ""),
            "tburl": sh(["bash", SCRIPT, "toolbox", "url"]).strip() if tb.result() else "",
            "sshd": bool(sshd and sshd.result()), "proxy": bool(prox and prox.result()),
            "app": app.result(), "toolbox": bool(tb.result()),
            "tbport": toolbox_port(tb.result().split()[0]) if tb.result() else 0,
            "watch": bool(pid_alive(os.path.join(STATE, "watch.pid"))),
            "bedrock": bedrock.result()[0], "bedrock_detail": bedrock.result()[1],
            "awsauto": aws_autologin.snapshot() if aws_autologin else None,
            "targets": targets, "ts": time.strftime("%H:%M:%S"),
        }


# ---- jobs -----------------------------------------------------------------
JOBS = {}
JOBS_LOCK = threading.Lock()
TARGETS_LOCK = threading.RLock()   # serialize automatic dispatch with manual connection actions


def run_job(title, argv, stdin_text=None):
    try:
        p = subprocess.Popen(argv, stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             start_new_session=True, bufsize=1)
    except OSError as e:
        mon_log("Could not start job %s: %s" % (title, e))
        raise ValueError("Could not start %s: %s" % (title, e)) from e
    jid = secrets.token_hex(4)
    job = {"id": jid, "title": title, "out": [], "done": False, "rc": None}
    with JOBS_LOCK:
        JOBS[jid] = job
        for old in [key for key, value in JOBS.items() if value["done"]][:-30]:
            JOBS.pop(old, None)

    def reader():
        for line in p.stdout:
            job["out"].append(ANSI.sub("", line.rstrip("\n")))

    def waiter():
        t = threading.Thread(target=reader, daemon=True)
        t.start()
        if stdin_text is not None:
            try:
                p.stdin.write(stdin_text)
                p.stdin.close()
            except OSError:
                pass
        job["rc"] = p.wait()
        t.join(2)        # a daemon the script started may keep the pipe open; do not wait for it
        job["done"] = True

    threading.Thread(target=waiter, daemon=True).start()
    return jid


def sh_cmd(*args):
    return ["bash", SCRIPT, *args]


def target_on_steps(targets, nat=True):
    q = lambda *a: " ".join(shlex.quote(x) for x in sh_cmd(*a))
    steps = ['echo "== open the %s tunnel"; %s || rc=1' % (t["name"], q("tunnel", t["name"]))
             for t in targets if t["tunnel"] and not t["up"]]
    steps += ['echo "== mount the %s drive share"; %s || rc=1' % (t["name"], q("mount", t["name"]))
              for t in targets if t["serve"] and not t["mounted"]]
    if nat and any(t["vip"] and not t["natup"] for t in targets):
        steps.append('echo "== bring the NAT addresses up"; %s || rc=1' % q("nat", "on"))
    return steps


def all_onoff(on):
    """One job for the targets card's All on / All off, touching only what needs it.

    phone-proxy.sh tunnel restarts tunnels that are already up, which would drop a live remote
    desktop or pull a mounted drive share from under Finder, so each target is handled by name.
    All off leaves the NAT addresses alone: both watchdogs would put them straight back."""
    q = lambda *a: " ".join(shlex.quote(x) for x in sh_cmd(*a))
    steps = []
    targets = status()["targets"]
    if on:
        steps = target_on_steps(targets)
    else:
        for t in targets:
            if t["serve"] and t["mounted"]:
                mark = shlex.quote("localhost:%d/%s on " % (t["lport"], t["name"]))
                steps.append('echo "== unmount the %s drive share"; %s\n'
                             'if mount | grep -qF %s; then echo "%s is still mounted (in use?), so its tunnel stays open."; rc=1\n'
                             'else echo "== close the %s tunnel"; %s || rc=1; fi'
                             % (t["name"], q("unmount", t["name"]), mark, t["name"], t["name"], q("untunnel", t["name"])))
            elif t["tunnel"] and t["up"]:
                steps.append('echo "== close the %s tunnel"; %s || rc=1' % (t["name"], q("untunnel", t["name"])))
    title = "All on" if on else "All off"
    if not steps:
        return title, ["echo", "Everything is already on: every tunnel is open, the drive share is mounted and the NAT "
                       "addresses answer. Nothing was touched." if on else "Everything is already off. Nothing was touched."], None
    return "%s (%d %s)" % (title, len(steps), "step" if len(steps) == 1 else "steps"), \
        ["bash", "-c", "rc=0\n" + "\n".join(steps) + "\nexit $rc\n"], None


def build(action, a):
    """Map a whitelisted action to (title, argv, stdin). Raises ValueError on bad input."""
    name = a.get("name", "")
    simple = {
        "all": ("Do everything", sh_cmd("all")),
        "start": ("Start proxy on phone", sh_cmd("start")),
        "stop": ("Stop proxy and tunnels", sh_cmd("stop")),
        "bedrock": ("Bedrock login", sh_cmd("bedrock-login")),
        "phonelog": ("Phone log", sh_cmd("log")),
        "exitip": ("Exit IP check", sh_cmd("exitip")),
        "drivelogin": ("Drive login", sh_cmd("drive-login")),
        "tabctl": ("Tablet control page", sh_cmd("tabctl")),
    }
    if action in simple:
        return simple[action] + (None,)
    if action == "edge":
        if a.get("mode") not in ("isolated", "default", "plain"):
            raise ValueError("bad edge mode")
        return "Edge (%s)" % a["mode"], sh_cmd("edge", a["mode"]), None
    if action in ("tunnel", "untunnel", "mount", "unmount"):
        if name and not NAME_RE.match(name):
            raise ValueError("bad name")
        return "%s %s" % (action, name), sh_cmd(action, name) if name else sh_cmd(action), None
    if action in ("allon", "alloff"):
        return all_onoff(action == "allon")
    if action == "nat":
        if a.get("op") not in ("on", "off", "status"):
            raise ValueError("bad op")
        return "NAT on the tablet: " + a["op"], sh_cmd("nat", a["op"]), None
    if action == "watch":
        if a.get("state") not in ("on", "off"):
            raise ValueError("bad state")
        return "Watchdog " + a["state"], sh_cmd("watch", a["state"]), None
    if action in ("server", "toolbox"):
        if a.get("op") not in ("start", "stop", "restart"):
            raise ValueError("bad op")
        label = "MiladSync server" if action == "server" else "Toolbox manager"
        return "%s %s" % (label, a["op"]), sh_cmd(action, a["op"]), None
    if action == "speedtest":
        mb = int(a.get("mb", 64))
        if not 8 <= mb <= 1024:
            raise ValueError("size 8-1024 MB")
        return "USB speed test (%d MB)" % mb, sh_cmd("speedtest", str(mb)), None
    if action == "phone":
        v = str(a.get("addr", "")).strip()
        if v in ("auto", "scan"):
            # scan may list several phones and ask which; take the first
            return "Phone address: " + v, sh_cmd("phone", v), "1\n"
        if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", v) or any(int(o) > 255 for o in v.split(".")):
            raise ValueError("not an IPv4 address (or use auto / scan)")
        return "Phone address: " + v, sh_cmd("phone", v), None
    if action == "add":
        ip, rport, lport, nat = a.get("ip", ""), str(a.get("rport") or ""), str(a.get("lport") or ""), str(a.get("nat") or "")
        if not NAME_RE.match(name):
            raise ValueError("name: letters, digits, - or _")
        if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip) or any(int(o) > 255 for o in ip.split(".")):
            raise ValueError("not an IPv4 address")
        for p in (rport, lport):
            if p and p != "-" and not (p.isdigit() and 1 <= int(p) <= 65535):
                raise ValueError("bad port (or - for a NAT-only target)")
        if rport == "-" and not nat:
            raise ValueError("a NAT-only target needs a NAT host number")
        if nat and not (nat.isdigit() and 1 <= int(nat) <= 254):
            raise ValueError("NAT host number: 1-254")
        return "Add " + name, sh_cmd("add"), "\n".join([name, ip, rport, lport, nat]) + "\n"
    if action == "remove":
        names = [t["name"] for t in read_targets()]
        if name not in names:
            raise ValueError("no such target")
        return "Remove " + name, ["bash", "-c", 'printf "%s\\n" "$1" | exec bash "$2" _remove', "_", str(names.index(name) + 1), SCRIPT], None
    raise ValueError("unknown action")


# ---- terminals ------------------------------------------------------------
# Each terminal is the user's login shell on its own pty, driven key by key from an
# xterm.js terminal in the page (vendor/xterm, served from this Mac). Output is kept as
# raw bytes and fetched with a running offset; the fetch waits (long poll) for new bytes,
# so typing feels live without websockets. A terminal can also open on one of the TERM_RUN
# commands instead of the shell: the ones that ask questions, which a job (no input) cannot
# answer, and a shell on the tablet (ssh tab, the alias ssh_tab uses too). When such a command
# exits 0 the page closes its terminal by itself (termDone); one that failed stays open on its
# error.
TERMS = {}
TERMS_LOCK = threading.Lock()
TERM_MAX = 8
TERM_KEEP = 1_000_000        # bytes of output kept per terminal (what a reload replays)
TERM_RUN = {"bedrock": ("Bedrock login", sh_cmd("bedrock-login")),
            "bedrock-auto": ("Bedrock login (auto)", sh_cmd("bedrock-login", "auto")),
            "tablet": ("Tablet", ["ssh", "tab"])}
VENDOR = os.path.join(HERE, "vendor", "xterm")
VENDOR_FILES = {"xterm.js": "application/javascript", "addon-fit.js": "application/javascript",
                "xterm.css": "text/css"}
TERM_BOOT = ("import fcntl,os,signal,sys,termios;fcntl.ioctl(0,termios.TIOCSCTTY,0);"
             "[signal.signal(x,signal.SIG_DFL) for x in (signal.SIGINT,signal.SIGQUIT,signal.SIGTSTP,"
             "signal.SIGTTIN,signal.SIGTTOU,signal.SIGHUP)];"
             "os.execvp(sys.argv[1],sys.argv[1:])")


def term_size(fd, cols, rows):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def term_new(cols=120, rows=32, run=""):
    if run and run not in TERM_RUN:
        raise ValueError("unknown command for a terminal: %r" % run)
    with TERMS_LOCK:
        if len([t for t in TERMS.values() if t["alive"]]) >= TERM_MAX:
            raise ValueError("at most %d open terminals" % TERM_MAX)
        tid = secrets.token_hex(4)
        n = 1 + max([t["num"] for t in TERMS.values()] or [0])
    master, slave = os.openpty()
    term_size(slave, cols, rows)
    shell = os.environ.get("SHELL") or "/bin/zsh"
    title, argv = TERM_RUN[run] if run else ("", [shell, "-il"])
    env = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor",
               LANG=os.environ.get("LANG") or "en_US.UTF-8")
    for k in ("TERM_PROGRAM", "TERM_SESSION_ID"):
        env.pop(k, None)
    proc = subprocess.Popen([sys.executable, "-I", "-c", TERM_BOOT, *argv], stdin=slave, stdout=slave,
                            stderr=slave, cwd=HOME, env=env, start_new_session=True, close_fds=True)
    os.close(slave)
    t = {"id": tid, "num": n, "title": title, "run": run, "proc": proc, "fd": master, "buf": bytearray(),
         "base": 0, "alive": True, "code": None, "cond": threading.Condition()}
    with TERMS_LOCK:
        TERMS[tid] = t

    def reader():
        while True:
            try:
                data = os.read(master, 65536)
            except OSError:
                break
            if not data:
                break
            with t["cond"]:
                t["buf"] += data
                if len(t["buf"]) > TERM_KEEP:
                    cut = len(t["buf"]) - TERM_KEEP
                    del t["buf"][:cut]
                    t["base"] += cut
                t["cond"].notify_all()
        proc.wait()
        if run in ("bedrock", "bedrock-auto"):
            _bedrock_cache["ts"] = 0.0   # the status shows the new session at its next poll, not up to a minute late
        with t["cond"]:
            t["buf"] += ("\r\n[%s exited with code %s]\r\n" % (title or "shell", proc.returncode)).encode()
            t["code"] = proc.returncode
            t["alive"] = False
            t["cond"].notify_all()

    threading.Thread(target=reader, daemon=True).start()
    return tid


def term_get(tid):
    t = TERMS.get(tid)
    if not t:
        raise ValueError("no such terminal")
    return t


def term_out(tid, since, wait=20.0):
    t = term_get(tid)
    with t["cond"]:
        if since >= t["base"] + len(t["buf"]) and t["alive"]:
            t["cond"].wait(wait)
        end = t["base"] + len(t["buf"])
        start = max(since, t["base"])
        data = bytes(t["buf"][start - t["base"]:])
        return {"data": base64.b64encode(data).decode(), "n": end, "reset": since < t["base"],
                "alive": t["alive"], "code": t["code"]}


def term_in(tid, data):
    t = term_get(tid)
    if not t["alive"]:
        raise ValueError("terminal has exited")
    os.write(t["fd"], data.encode()[:65536])


def term_resize(tid, cols, rows):
    t = term_get(tid)
    if t["alive"] and 2 <= cols <= 500 and 2 <= rows <= 300:
        term_size(t["fd"], cols, rows)


def term_close(tid):
    t = term_get(tid)
    if t["alive"]:
        try:
            os.killpg(t["proc"].pid, signal.SIGHUP)
        except OSError:
            pass
        try:
            t["proc"].wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(t["proc"].pid, signal.SIGKILL)
            except OSError:
                pass
    try:
        os.close(t["fd"])
    except OSError:
        pass
    with TERMS_LOCK:
        TERMS.pop(tid, None)


def term_list():
    with TERMS_LOCK:
        return [{"id": t["id"], "num": t["num"], "title": t["title"], "run": t["run"], "alive": t["alive"]}
                for t in TERMS.values()]


def term_api(method, name, q, body):
    if method == "GET" and name == "list":
        return {"terms": term_list()}
    if method == "GET" and name == "out":
        return term_out((q.get("id") or [""])[0], int((q.get("since") or ["0"])[0]))
    if method == "POST" and name == "new":
        return {"id": term_new(int(body.get("cols") or 120), int(body.get("rows") or 32), str(body.get("run") or ""))}
    if method == "POST" and name == "in":
        term_in(str(body.get("id", "")), str(body.get("data", "")))
        return {"ok": True}
    if method == "POST" and name == "resize":
        term_resize(str(body.get("id", "")), int(body.get("cols") or 0), int(body.get("rows") or 0))
        return {"ok": True}
    if method == "POST" and name == "close":
        term_close(str(body.get("id", "")))
        return {"ok": True}
    raise ValueError("unknown terminal call")


# ---- http -----------------------------------------------------------------
# ---- connections monitor (Macotron) ---------------------------------------
# Every 5 s Macotron checks this Mac's side: is the Wi-Fi on the tablet's hotspot (macOS hides
# the network name from programs without Location access, so the router is identified as the
# tablet by its SSH host key or by the tab.local names it answers), does the hotspot carry
# internet, does the tablet answer. Every 15 s it reads Tablotron's own watch
# (~/.config/tabctl/watch.json on the tablet); every 30-60 s it tries the tab.local names, the
# NAT addresses and the URLs in ~/.phone-proxy/monitor-urls.conf. A change becomes a macOS
# notification, and the Connections card shows everything. Which changes notify is the user's:
# Settings > Alarms turns each check's alarm (and warnings, back-to-normal, sounds) on or off,
# Mute on a row is that same switch, and Ignore on a row counts the check as fine for some hours.
HOTSPOT_SSID = ""    # both below are set from the active device profile by apply_device()
TABLET_TS = ""
DEVICE_NAME, DEVICE_LABEL = "raven", "Raven"
DEVICE_HOTSPOT, DEVICE_ROOT, DEVICE_LAN = True, True, ""   # hotspot=no: the portal shares the Mac's Wi-Fi instead
MON_TICK = 5
MON_FILE = os.path.join(STATE, "monitor.json")          # hotspot MACs already proven, mute, alarm settings
MON_LOG = os.path.join(STATE, "monitor.log")
URLS_FILE = os.path.join(STATE, "monitor-urls.conf")
DEFAULT_URLS = ("# Macotron tries each URL every minute: label | URL | HTTP codes that mean it works.\n"
                "# No tokens here: a 401 (sign-in needed) already proves the server answers that name.\n"
                "# Example service | http://example.tab.local:8790/ | 200 401\n")
MON_INTERNET = (("1.1.1.1", 443), ("8.8.8.8", 443))
IP_BOUND_IF = 25            # macOS: send this socket out of one interface only
RENOTIFY = 1800


def mon_log(msg):
    try:
        if os.path.exists(MON_LOG) and os.path.getsize(MON_LOG) > 512 * 1024:
            os.replace(MON_LOG, MON_LOG + ".1")
        with open(MON_LOG, "a") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg.rstrip() + "\n")
    except OSError:
        pass


def wifi_dev():
    m = re.search(r"Hardware Port: Wi-Fi\nDevice: (\S+)", sh(["networksetup", "-listallhardwareports"], 10))
    return m.group(1) if m else "en0"


def bound_tcp(ifname, host, port, timeout=2.0):
    """Milliseconds to connect through `ifname` only (never the USB LAN or the VPN), or None."""
    t0 = time.monotonic()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.IPPROTO_IP, IP_BOUND_IF, socket.if_nametoindex(ifname))
        s.settimeout(timeout)
        s.connect((host, int(port)))
        return int((time.monotonic() - t0) * 1000)
    except (OSError, ValueError):
        return None
    finally:
        s.close()


def resolve(name, timeout=3.0):
    box = []

    def go():
        try:
            box.append(socket.getaddrinfo(name, None, socket.AF_INET)[0][4][0])
        except (OSError, IndexError):
            box.append("")
    th = threading.Thread(target=go, daemon=True)
    th.start()
    th.join(timeout)
    return box[0] if box else ""


def arp_mac(ip):
    m = re.search(r" at ([0-9a-f:]+) on ", sh(["arp", "-n", ip], 5))
    return m.group(1) if m else ""


def ssh_tab(cmd, host=None, timeout=15):
    """Run cmd on the tablet. With host, go straight there: the host key proves it is the tablet."""
    argv = ["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]
    if host:
        argv += ["-o", "ProxyCommand=none", "-o", "HostName=" + host]
    try:
        r = subprocess.run(argv + ["tab", cmd], capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timed out after %d s" % timeout
    except OSError as e:
        return 127, "", str(e)


def tabctl_page():
    """The tablet's control page for the Tablet tab: {"url": ...} or {"error": ...}. phone-proxy.sh
    opens (or rebuilds) its SSH tunnel; the URL, which holds the page's token, comes from the
    tablet over ssh_tab and goes only into this reply, never into a log or a job's output. It is on
    localhost, like Macotron, so tabctl's SameSite cookie is sent inside the frame."""
    with tempfile.TemporaryFile("w+") as out:   # a file, not a pipe: the tunnel's ssh -f keeps a pipe open
        try:
            rc = subprocess.run(sh_cmd("tabctl", "tunnel"), stdin=subprocess.DEVNULL, stdout=out,
                                stderr=subprocess.STDOUT, timeout=40, start_new_session=True).returncode
        except subprocess.TimeoutExpired:
            rc = 124
        out.seek(0)
        said = ANSI.sub("", out.read()).strip()
    if rc:
        return {"error": said or "Could not open the SSH tunnel to the tablet's control page."}
    rc, out, err = ssh_tab("python ~/tabctl/tabctl.py url", timeout=20)
    url = next((w for w in out.split() if w.startswith("http://127.0.0.1:")), "")
    if not url:
        return {"error": "The control page does not answer on the tablet (%s). Start it there with: "
                         "sh ~/tabctl/start.sh" % (err or "no URL")}
    return {"url": "http://localhost:" + url[len("http://127.0.0.1:"):]}


# ---- clipboard list (Clipboard tab): clipkeeper saves the clipboard on Ctrl+Option+<key>; one JSON file per clip ----
CLIP_DIR = os.path.join(STATE, "clips")
CLIP_BIN = os.path.join(HERE, "clipkeeper")
CLIP_SRC = os.path.join(HERE, "clipkeeper.swift")
CLIP_CFG = os.path.join(STATE, "clipkeeper.json")
CLIP_STATUS = os.path.join(STATE, "clipkeeper.status.json")
CLIP_KEEP = 300                                   # unpinned clips kept; older ones are removed when a new one is listed


def clip_cfg():
    cfg = {"on": True, "keycode": 24}                 # 24 = the = key; 10 = section sign (ISO), 50 = grave accent
    try:
        with open(CLIP_CFG) as f:
            cfg.update({k: v for k, v in json.load(f).items() if k in cfg})
    except (OSError, ValueError):
        pass
    return cfg


def clip_items():
    out = []
    try:
        names = sorted(n for n in os.listdir(CLIP_DIR) if n.endswith(".json"))
    except OSError:
        return out
    for n in names:
        try:
            with open(os.path.join(CLIP_DIR, n)) as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if not str(d.get("id", "")).isdigit():
            continue
        out.append(d)
    return out


def clip_list():
    items = clip_items()
    unpinned = [d for d in items if not d.get("pinned")]
    for d in unpinned[:-CLIP_KEEP] if len(unpinned) > CLIP_KEEP else []:
        clip_delete(d["id"])
    rows = []
    for d in reversed(items):
        r = {"id": d["id"], "t": d.get("t", 0), "kind": d.get("kind", "text"), "pinned": bool(d.get("pinned"))}
        if r["kind"] == "image":
            r["bytes"] = d.get("bytes", 0)
        else:
            t = d.get("text", "")
            r["text"] = t[:6000]
            r["len"] = d.get("len", len(t))
        rows.append(r)
    cfg = clip_cfg()
    st = {}
    try:
        with open(CLIP_STATUS) as f:
            st = json.load(f)
    except (OSError, ValueError):
        pass
    running = False
    try:
        running = "clipkeeper" in sh(["ps", "-p", str(int(st.get("pid") or 0)), "-o", "command="])
    except ValueError:
        pass
    return {"items": rows, "cfg": cfg, "keeper": {"running": running, "registered": bool(st.get("registered")) and running,
                                                  "code": st.get("code", 0), "built": os.path.exists(CLIP_BIN)}}


def clip_path(cid, ext="json"):
    if not str(cid).isdigit():
        raise ValueError("bad clip id")
    return os.path.join(CLIP_DIR, "%s.%s" % (cid, ext))


def clip_delete(cid):
    for ext in ("json", "png"):
        try:
            os.unlink(clip_path(cid, ext))
        except OSError:
            pass


def clip_op(body):
    op = str(body.get("op", "list"))
    if op == "list":
        return clip_list()
    if op == "set":
        cfg = clip_cfg()
        if "on" in body:
            cfg["on"] = bool(body["on"])
        if "keycode" in body:
            k = int(body["keycode"])
            if k not in (24, 10, 50):
                raise ValueError("choose the = key, the section sign key or the grave accent key")
            cfg["keycode"] = k
        os.makedirs(STATE, exist_ok=True)
        with open(CLIP_CFG, "w") as f:
            json.dump(cfg, f)
        sh(["pkill", "-f", "clipkeeper --dir"])           # clipkeeper_loop starts it again with the new settings
        return clip_list()
    if op == "save":                                       # the page's button: the Mac's clipboard text now
        text = subprocess.run(["pbpaste"], capture_output=True, timeout=5).stdout.decode("utf-8", "replace")
        if not text:
            raise ValueError("The clipboard has no text. (Images are saved with the shortcut.)")
        items = clip_items()
        if items and items[-1].get("text") == text:
            raise ValueError("That is already the newest clip.")
        ms = int(time.time() * 1000)
        os.makedirs(CLIP_DIR, exist_ok=True)
        with open(clip_path(ms), "w") as f:
            json.dump({"id": str(ms), "t": ms / 1000, "kind": "text", "text": text[:1000000], "len": len(text), "pinned": False}, f)
        return clip_list()
    cid = str(body.get("id", ""))
    if op == "delete":
        clip_delete(cid)
        return clip_list()
    if op == "clear":
        for d in clip_items():
            if not d.get("pinned"):
                clip_delete(d["id"])
        return clip_list()
    if op == "pin":
        p = clip_path(cid)
        with open(p) as f:
            d = json.load(f)
        d["pinned"] = bool(body.get("on", not d.get("pinned")))
        with open(p, "w") as f:
            json.dump(d, f)
        return clip_list()
    if op == "copy":
        with open(clip_path(cid)) as f:
            d = json.load(f)
        if d.get("kind") == "image":
            png = clip_path(cid, "png")
            subprocess.run(["osascript", "-e", 'set the clipboard to (read (POSIX file "%s") as «class PNGf»)' % png],
                           capture_output=True, timeout=10, check=True)
        else:
            subprocess.run(["pbcopy"], input=d.get("text", "").encode("utf-8"), timeout=5, check=True)
        return {"ok": True}
    raise ValueError("unknown op")


def clipkeeper_loop():
    """Keeps clipkeeper (the shortcut helper) running while it is switched on in the Clipboard tab."""
    while True:
        try:
            cfg = clip_cfg()
            seen = sh(["pgrep", "-fl", "clipkeeper --dir"])
            running = bool(seen.split())
            if running and cfg["on"] and "--keycode %d" % cfg["keycode"] not in seen:
                sh(["pkill", "-f", "clipkeeper --dir"])       # it runs with another key than the setting: start it again
                time.sleep(1)
                running = False
            if cfg["on"] and not running and os.path.exists(CLIP_SRC):
                if not os.path.exists(CLIP_BIN) or os.path.getmtime(CLIP_BIN) < os.path.getmtime(CLIP_SRC):
                    subprocess.run(["swiftc", "-O", "-swift-version", "5", "-o", CLIP_BIN, CLIP_SRC],
                                   capture_output=True, timeout=240)
                if os.path.exists(CLIP_BIN):
                    os.makedirs(CLIP_DIR, exist_ok=True)
                    subprocess.Popen([CLIP_BIN, "--dir", CLIP_DIR, "--keycode", str(cfg["keycode"])],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     start_new_session=True)
            elif not cfg["on"] and running:
                sh(["pkill", "-f", "clipkeeper --dir"])
        except Exception:                              # noqa: BLE001 - the loop must not die
            pass
        time.sleep(6)


if "--no-monitor" not in sys.argv:                     # a test copy must not also manage the shortcut helper
    threading.Thread(target=clipkeeper_loop, daemon=True).start()


# ---- tablet control: the Mac's keyboard and mouse on the tablet (Settings > Tablet control) -------
# tabinput (Swift, tabinput.swift) swallows the Mac's events once the cursor crosses a screen edge and
# sends them through an SSH tunnel to inputd.py on the tablet (a root uinput daemon).
TABIN_LPORT, TABIN_RPORT = 18096, 8096
TABIN_BIN = os.path.join(HERE, "tabinput")
TABIN_SRC = os.path.join(HERE, "tabinput.swift")
TABIN_CFG = os.path.join(STATE, "tabinput.json")
TABIN_STATUS = os.path.join(STATE, "tabinput.status.json")
TABIN_TOKEN = os.path.join(STATE, "tabinput.token")
TABIN_INPUTD = os.path.join(HERE, "tablet-bin", "tabctl", "inputd.py")
TABIN_DEFAULTS = {"trigger_screen": 0, "shortcut_key": 17, "trigger": "edge", "corner_size": 16, "return_trigger": "edge", "return_size": 500, "edge": "right", "tw": 2800, "th": 1752, "speed": 1.0, "delay": 1.0, "dim": 0.22,
                 "meeting_on_control": True, "meeting_idle_min": 15, "cmd_as_ctrl": True, "invert_scroll": False, "paste_typing": True, "auto_size": True, "esc_returns": True}
TABIN_LOCK = threading.Lock()
_tabin_meeting = {"issued": 0.0, "until": 0.0, "error": ""}   # when the Meeting profile was last asked for, and when the tablet ends it


def apply_device():
    """Read the active companion device and point the hotspot name, Tailscale address and screen size at it."""
    global HOTSPOT_SSID, TABLET_TS, DEVICE_NAME, DEVICE_LABEL, DEVICE_HOTSPOT, DEVICE_ROOT, DEVICE_LAN
    name, dev = devices.active()
    HOTSPOT_SSID, TABLET_TS = dev.get("ssid", ""), dev["ts"]
    DEVICE_HOTSPOT, DEVICE_ROOT, DEVICE_LAN = dev.get("hotspot", True), dev.get("root", True), dev.get("lan", "")
    DEVICE_NAME, DEVICE_LABEL = name, dev.get("label", name)
    TABIN_DEFAULTS["tw"], TABIN_DEFAULTS["th"] = dev["screen"]
    return name


def device_state():
    d = devices.load()
    return {"active": d["active"], "label": DEVICE_LABEL,
            "devices": {n: {k: v.get(k) for k in ("label", "user", "ssid", "ts", "screen", "hotspot", "root", "lan")} for n, v in d["devices"].items()},
            "check": devices.validate(d["active"], d["devices"][d["active"]])}


def device_op(body):
    op = str(body.get("op", "status"))
    if op == "status":
        return device_state()
    if op == "use":
        name = str(body.get("name", ""))
        try:
            devices.use(name)
        except SystemExit as e:
            raise ValueError(str(e))
        apply_device()
        return dict(device_state(), restart=True)
    raise ValueError("unknown op")


apply_device()


def tabin_cfg():
    cfg = dict(TABIN_DEFAULTS)
    try:
        with open(TABIN_CFG) as f:
            cfg.update({k: v for k, v in json.load(f).items() if k in TABIN_DEFAULTS})
    except (OSError, ValueError):
        pass
    return cfg


def tabin_set(body):
    cfg = tabin_cfg()
    if "trigger" in body:
        if body["trigger"] not in ("edge", "shortcut", "top-right", "top-left", "bottom-right", "bottom-left"):
            raise ValueError("the trigger must be edge, shortcut, top-right, top-left, bottom-right or bottom-left")
        cfg["trigger"] = body["trigger"]
    if "trigger_screen" in body:
        cfg["trigger_screen"] = int(body["trigger_screen"])
    if "shortcut_key" in body:
        if int(body["shortcut_key"]) not in (17, 122, 120, 96):
            raise ValueError("the shortcut key must be T, F1, F2 or F5")
        cfg["shortcut_key"] = int(body["shortcut_key"])
    if "return_trigger" in body:
        if body["return_trigger"] not in ("edge", "top-left", "top-right", "bottom-left", "bottom-right"):
            raise ValueError("the way back must be edge, top-left, top-right, bottom-left or bottom-right")
        cfg["return_trigger"] = body["return_trigger"]
    if "return_size" in body:
        v = int(body["return_size"])
        if not 50 <= v <= 3000:
            raise ValueError("the tablet corner size must be between 50 and 3000 pixels")
        cfg["return_size"] = v
    if "corner_size" in body:
        v = int(body["corner_size"])
        if not 4 <= v <= 300:
            raise ValueError("the corner size must be between 4 and 300")
        cfg["corner_size"] = v
    if "edge" in body:
        if body["edge"] not in ("right", "left", "top", "bottom"):
            raise ValueError("edge must be right, left, top or bottom")
        cfg["edge"] = body["edge"]
    for k, lo, hi in (("tw", 200, 20000), ("th", 200, 20000)):
        if k in body:
            v = int(body[k])
            if not lo <= v <= hi:
                raise ValueError("%s must be between %d and %d" % (k, lo, hi))
            cfg[k] = v
    if "speed" in body:
        v = float(body["speed"])
        if not 0.2 <= v <= 5:
            raise ValueError("speed must be between 0.2 and 5")
        cfg["speed"] = round(v, 2)
    if "delay" in body:
        v = float(body["delay"])
        if not 0 <= v <= 10:
            raise ValueError("the delay must be between 0 and 10 seconds")
        cfg["delay"] = round(v, 2)
    if "dim" in body:
        v = float(body["dim"])
        if not 0 <= v <= 0.9:
            raise ValueError("the dimming must be between 0 and 0.9")
        cfg["dim"] = round(v, 2)
    if "meeting_idle_min" in body:
        v = int(body["meeting_idle_min"])
        if not 15 <= v <= 1440:
            raise ValueError("the Meeting profile's idle time must be 15 to 1440 minutes")
        cfg["meeting_idle_min"] = ((v + 14) // 15) * 15      # the tablet's Meeting level counts in 15 min steps
    for k in ("cmd_as_ctrl", "invert_scroll", "meeting_on_control", "paste_typing", "auto_size", "esc_returns"):
        if k in body:
            cfg[k] = bool(body[k])
    os.makedirs(STATE, exist_ok=True)
    with open(TABIN_CFG, "w") as f:
        json.dump(cfg, f)
    return cfg


def tabin_pids():
    """The running helper's pid (from its status file, checked against ps) and the tunnel's (pgrep)."""
    helper = 0
    try:
        with open(TABIN_STATUS) as f:
            p = int(json.load(f).get("pid") or 0)
        if p and "tabinput" in sh(["ps", "-p", str(p), "-o", "command="]):
            helper = p
    except (OSError, ValueError):
        pass
    tun = sh(["pgrep", "-f", "ssh -N .*127.0.0.1:%d:127.0.0.1:%d" % (TABIN_LPORT, TABIN_RPORT)]).split()
    return helper, [int(x) for x in tun if x.isdigit()]


def tabin_state():
    cfg = tabin_cfg()
    helper, tun = tabin_pids()
    st = {}
    try:
        with open(TABIN_STATUS) as f:
            st = json.load(f)
    except (OSError, ValueError):
        pass
    mt = dict(_tabin_meeting)
    return {"cfg": cfg, "built": os.path.exists(TABIN_BIN), "running": bool(helper), "tunnel": bool(tun),
            "meeting_until": mt["until"] if mt["until"] > time.time() else 0, "meeting_error": mt["error"],
            "state": st.get("state", "stopped") if helper else "stopped", "msg": st.get("msg", "") if helper else "",
            "connected": bool(st.get("connected")) if helper else False,
            "screens": st.get("screens", []) if helper else [], "shortcut_ok": bool(st.get("shortcut_ok", True)),
            "shortcut_presses": st.get("shortcut_presses", 0) if helper else 0}


def tabin_build():
    if os.path.exists(TABIN_BIN) and os.path.getmtime(TABIN_BIN) >= os.path.getmtime(TABIN_SRC):
        return ""
    r = subprocess.run(["swiftc", "-O", "-swift-version", "5", "-o", TABIN_BIN, TABIN_SRC],
                       capture_output=True, text=True, timeout=240)
    return "" if r.returncode == 0 and os.path.exists(TABIN_BIN) else "Building tabinput failed: " + r.stderr[-400:]


def tabin_stop(daemon=False):
    helper, tun = tabin_pids()
    for p in ([helper] if helper else []) + tun:
        try:
            os.kill(p, signal.SIGTERM)
        except OSError:
            pass
    time.sleep(0.5)
    if daemon:
        ssh_tab("sudo python3 ~/tabctl/inputd.py stop", timeout=15)
    return tabin_state()


def tabin_start():
    """Daemon on the tablet, token, SSH tunnel, then the Mac helper. Raises ValueError with what went wrong."""
    if not DEVICE_ROOT:
        raise ValueError("The portal device %s is not rooted: the Mac keyboard and mouse sharing and screen window need root on it." % DEVICE_NAME)
    with TABIN_LOCK:
        tabin_stop()
        err = tabin_build()
        if err:
            raise ValueError(err)
        rc, out, e = ssh_tab("test -f ~/tabctl/inputd.py && echo yes", timeout=15)
        if "yes" not in out:
            if rc not in (0, 1):
                raise ValueError("The tablet does not answer over ssh: %s" % (e or rc))
            if subprocess.run(["scp", "-q", "-o", "BatchMode=yes", TABIN_INPUTD, "tab:tabctl/inputd.py"],
                              capture_output=True, timeout=30).returncode:
                raise ValueError("Could not copy inputd.py to the tablet.")
        rc, out, e = ssh_tab("sudo python3 ~/tabctl/inputd.py start", timeout=25)
        if rc or "inputd" not in out:
            raise ValueError("The input daemon on the tablet did not start: %s" % (e or out.strip() or rc))
        rc, out, e = ssh_tab("sudo python3 ~/tabctl/inputd.py token", timeout=15)
        tok = out.strip()
        if rc or not tok:
            raise ValueError("Could not read the daemon's token: %s" % e)
        os.makedirs(STATE, exist_ok=True)
        fd = os.open(TABIN_TOKEN, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(tok + "\n")
        subprocess.Popen(["ssh", "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
                          "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3",
                          "-L", "127.0.0.1:%d:127.0.0.1:%d" % (TABIN_LPORT, TABIN_RPORT), "tab"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        for _ in range(40):
            if port_open("127.0.0.1", TABIN_LPORT, 0.3):
                break
            time.sleep(0.25)
        else:
            tabin_stop()
            raise ValueError("The SSH tunnel to the tablet's input daemon did not come up.")
        cfg = tabin_cfg()
        try:
            os.unlink(TABIN_STATUS)
        except OSError:
            pass
        p = subprocess.Popen([TABIN_BIN, "run", "--port", str(TABIN_LPORT), "--token-file", TABIN_TOKEN,
                              "--status", TABIN_STATUS, "--edge", cfg["edge"], "--trigger", cfg["trigger"], "--trigger-screen", str(cfg["trigger_screen"]), "--shortcut-key", str(cfg["shortcut_key"]), "--return-trigger", cfg["return_trigger"], "--return-size", str(cfg["return_size"]),
                              "--corner-size", str(cfg["corner_size"]), "--tw", str(cfg["tw"]),
                              "--th", str(cfg["th"]), "--speed", str(cfg["speed"]), "--delay", str(cfg["delay"]), "--dim", str(cfg["dim"]),
                              "--config", TABIN_CFG,
                              "--cmd-as-ctrl", "1" if cfg["cmd_as_ctrl"] else "0",
                              "--invert-scroll", "1" if cfg["invert_scroll"] else "0",
                              "--paste-typing", "1" if cfg["paste_typing"] else "0",
                              "--esc-returns", "1" if cfg["esc_returns"] else "0"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        time.sleep(1.5)
        if p.poll() is not None:
            tabin_stop()
            raise ValueError("The Mac helper stopped at once (code %s): start tabinput from this repository "
                             "by hand (tabinput run ...) to see why." % p.returncode)
        return tabin_state()


def tabin_meeting_loop():
    """While the tablet has the Mac's keyboard and mouse the tablet runs its Meeting profile: it is asked
    for at the first activity and again (at most once a minute) while there is more activity, each time
    for the configured idle minutes, so it ends that long after the last activity. The tablet ends it by
    itself, so a Mac that disappears cannot leave the tablet in Meeting. Its earlier settings come back then."""
    last_size = 0.0
    while True:
        time.sleep(4)
        if not DEVICE_ROOT:
            continue                                     # no root on the portal: no input daemon, no Meeting profile
        try:
            cfg = tabin_cfg()
            if cfg["auto_size"] and time.time() - last_size > 12 and tabin_pids()[0]:
                last_size = time.time()                  # the tablet's real screen: it turns between portrait and landscape
                rc, out, err = ssh_tab("sudo dumpsys window displays | grep -o 'cur=[0-9]*x[0-9]*' | head -1", timeout=20)
                m = re.search(r"cur=(\d+)x(\d+)", out or "")
                if rc == 0 and m and (int(m.group(1)), int(m.group(2))) != (cfg["tw"], cfg["th"]):
                    tabin_set({"tw": int(m.group(1)), "th": int(m.group(2))})
            if not cfg["meeting_on_control"]:
                continue
            try:
                with open(TABIN_STATUS) as f:
                    st = json.load(f)
            except (OSError, ValueError):
                continue
            act = float(st.get("last_activity") or 0)
            helper, _ = tabin_pids()
            if not helper or act <= _tabin_meeting["issued"] or time.time() - _tabin_meeting["issued"] < 60:
                continue
            mins = int(cfg["meeting_idle_min"])
            rc, out, err = ssh_tab("sudo sh ~/tabctl/root.sh power-meeting %d" % mins, timeout=60)
            now = time.time()
            if rc == 0:
                _tabin_meeting.update(issued=now, until=now + mins * 60, error="")
            else:
                _tabin_meeting.update(issued=now - 30, error=(err or out or "rc %s" % rc).strip()[:200])   # try again in 30 s
        except Exception as e:                       # noqa: BLE001 - the loop must not die
            _tabin_meeting["error"] = str(e)[:200]


if "--no-monitor" not in sys.argv:                   # a test copy must not also drive the tablet's profile
    threading.Thread(target=tabin_meeting_loop, daemon=True).start()


# ---- the tablet's screen in a window of its own (Settings > Tablet screen window) -------------------
# screenrecord on the tablet streams H.264 over ssh into ffmpeg, which makes a motion-JPEG stream that Macotron serves to a
# small Chrome app window (/tabscreen). While you are in that window the Mac's mouse and keyboard drive the tablet: a click
# is a tap, a drag a drag, the wheel a swipe, right-click is Back, the keys are real keys. They reach the tablet through
# inputd.py's virtual touchscreen and keyboard (its own ssh tunnel, port 18097).
TABSCR_CFG = os.path.join(STATE, "tabscreen.json")
TABSCR_DEFAULTS = {"scale": 0.5, "bitrate": 6}
TABSCR_PROFILE = os.path.join(STATE, "viewer-profile")
TABSCR_LPORT = 18097
TABSCR_CHROME = next((p for p in (os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                                  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome") if os.path.exists(p)), "")
_tabscr = {"size": None, "rot": 90, "started": 0.0, "error": ""}
_frame = {"jpg": b"", "seq": 0}
_frame_cv = threading.Condition()
_bridge = {"sock": None}
_bridge_lock = threading.Lock()


def tabscr_cfg():
    cfg = dict(TABSCR_DEFAULTS)
    try:
        with open(TABSCR_CFG) as f:
            cfg.update({k: v for k, v in json.load(f).items() if k in cfg})
    except (OSError, ValueError):
        pass
    return cfg


def tabscr_set(body):
    cfg = tabscr_cfg()
    if "scale" in body:
        v = float(body["scale"])
        if not 0.2 <= v <= 1.0:
            raise ValueError("the scale must be between 0.2 and 1")
        cfg["scale"] = round(v, 2)
    if "bitrate" in body:
        v = int(body["bitrate"])
        if not 1 <= v <= 40:
            raise ValueError("the bit rate must be 1 to 40 Mbit/s")
        cfg["bitrate"] = v
    os.makedirs(STATE, exist_ok=True)
    with open(TABSCR_CFG, "w") as f:
        json.dump(cfg, f)
    return cfg


def tabscr_running():
    return bool(sh(["pgrep", "-f", "ffmpeg .*title=macotron-tabscreen"]).split())


def tabscr_window_open():
    return bool(sh(["pgrep", "-f", "user-data-dir=%s" % TABSCR_PROFILE]).split())


def tabscr_state():
    return {"running": tabscr_running(), "cfg": tabscr_cfg(), "size": _tabscr["size"], "error": _tabscr["error"]}


def tabscr_probe():
    """The tablet's screen size and rotation: (w, h, rotation in degrees)."""
    rc, out, err = ssh_tab("sudo dumpsys window displays | grep -oE 'cur=[0-9]+x[0-9]+|mDisplayRotation=ROTATION_[0-9]+' | head -2", timeout=20)
    m = re.search(r"cur=(\d+)x(\d+)", out or "")
    r = re.search(r"ROTATION_(\d+)", out or "")
    if rc == 0 and m:
        return int(m.group(1)), int(m.group(2)), int(r.group(1)) if r else 0
    c = tabin_cfg()
    return int(c["tw"]), int(c["th"]), _tabscr["rot"]


def tabscr_raw(fx, fy):
    """A point as fractions of the screen as it is held now -> the touchscreen's own frame (the natural, portrait one), 0..32767."""
    fx, fy = max(0.0, min(1.0, fx)), max(0.0, min(1.0, fy))
    rot = _tabscr["rot"]
    rx, ry = {0: (fx, fy), 90: (1 - fy, fx), 180: (1 - fx, 1 - fy), 270: (fy, 1 - fx)}.get(rot, (fx, fy))
    return int(rx * 32767), int(ry * 32767)


def tabscr_drain(sock):
    try:
        while sock.recv(4096):
            pass
    except OSError:
        pass
    with _bridge_lock:
        if _bridge["sock"] is sock:
            _bridge["sock"] = None


def tabscr_send(lines):
    """Lines for inputd.py through the viewer's own tunnel; (re)connects when needed."""
    with _bridge_lock:
        sock = _bridge["sock"]
        if sock is None:
            try:
                with open(TABIN_TOKEN) as f:
                    tok = f.read().strip()
                sock = socket.create_connection(("127.0.0.1", TABSCR_LPORT), 3)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.sendall(("HELLO %s\n" % tok).encode())
                sock.settimeout(3)
                if not sock.recv(64).startswith(b"OK"):
                    raise OSError("the input daemon refused")
                sock.settimeout(None)
            except OSError:
                return False
            _bridge["sock"] = sock
            threading.Thread(target=tabscr_drain, args=(sock,), daemon=True).start()
        try:
            sock.sendall(("\n".join(lines) + "\n").encode())
            return True
        except OSError:
            _bridge["sock"] = None
            return False


def tabscr_input(events):
    """Events from the window: {t:'d'|'m'|'u', fx, fy}, {t:'w', fx, fy, dx, dy}, {t:'k', c, v}."""
    lines = []
    for e in events[:200]:
        t = e.get("t")
        if t in ("d", "m"):
            x, y = tabscr_raw(float(e["fx"]), float(e["fy"]))
            lines.append("T %s %d %d" % (t, x, y))
        elif t == "u":
            lines.append("T u")
        elif t == "w":
            fx, fy, dx, dy = float(e["fx"]), float(e["fy"]), float(e.get("dx", 0)), float(e.get("dy", 0))
            x0, y0 = tabscr_raw(fx, fy)
            x1, y1 = tabscr_raw(fx + dx, fy + dy)
            lines.append("SW %d %d %d %d" % (x0, y0, x1 - x0, y1 - y0))
        elif t == "k":
            c, v = int(e["c"]), int(e["v"])
            if 1 <= c <= 248 and v in (0, 1, 2):
                lines.append("K %d %d" % (c, v))
    if lines and not tabscr_send(lines):
        raise ValueError("The tablet's input daemon does not answer.")
    return {"ok": True}


def tabscr_consume(proc):
    """Cuts ffmpeg's motion-JPEG output into frames and keeps the newest for the window."""
    buf = bytearray()
    try:
        while True:
            chunk = proc.stdout.read1(65536)
            if not chunk:
                break
            buf += chunk
            while True:
                a = buf.find(b"\xff\xd8")
                if a < 0:
                    buf.clear()
                    break
                b = buf.find(b"\xff\xd9", a + 2)
                if b < 0:
                    if a > 0:
                        del buf[:a]
                    break
                with _frame_cv:
                    _frame["jpg"] = bytes(buf[a:b + 2])
                    _frame["seq"] += 1
                    _frame_cv.notify_all()
                del buf[:b + 2]
    finally:
        with _frame_cv:
            _frame["seq"] += 1
            _frame_cv.notify_all()


def tabscr_stream(h):
    """GET /api/tabscreen.mjpg: the newest frames as multipart/x-mixed-replace, which an <img> shows live."""
    h.send_response(200)
    h.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
    h.send_header("Cache-Control", "no-store")
    h.end_headers()
    last = 0
    try:
        while True:
            with _frame_cv:
                _frame_cv.wait_for(lambda: _frame["seq"] != last, timeout=4)
                jpg, last = _frame["jpg"], _frame["seq"]
            if not jpg:
                if not tabscr_running():
                    break
                continue
            h.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpg) + jpg + b"\r\n")
            h.wfile.flush()
    except OSError:
        pass


def tabscr_stop():
    sh(["pkill", "-f", "user-data-dir=%s" % TABSCR_PROFILE])
    sh(["pkill", "-f", "ffmpeg .*title=macotron-tabscreen"])
    sh(["pkill", "-f", "screenrecord --output-format=h264"])
    sh(["pkill", "-f", "127.0.0.1:%d:127.0.0.1:%d" % (TABSCR_LPORT, TABIN_RPORT)])
    with _bridge_lock:
        if _bridge["sock"] is not None:
            try:
                _bridge["sock"].close()
            except OSError:
                pass
            _bridge["sock"] = None
    ssh_tab("sudo pkill -f 'screenrecord --output-forma[t]'", timeout=15)
    _tabscr["size"] = None
    with _frame_cv:
        _frame["jpg"] = b""
        _frame["seq"] += 1
        _frame_cv.notify_all()
    return tabscr_state()


def tabscr_start(port):
    if not DEVICE_ROOT:
        raise ValueError("The portal device %s is not rooted: the Mac keyboard and mouse sharing and screen window need root on it." % DEVICE_NAME)
    ffmpeg = next((p for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", shutil.which("ffmpeg") or "") if p and os.path.exists(p)), "")
    if not ffmpeg:
        raise ValueError("ffmpeg is not installed on this Mac (brew install ffmpeg).")
    if not os.path.exists(TABSCR_CHROME):
        raise ValueError("Google Chrome is not installed (the window is a Chrome app window).")
    tabscr_stop()
    cfg = tabscr_cfg()
    # the input daemon on the tablet (with the touchscreen) and its token
    rc, out, err = ssh_tab("grep -c 'Macotron touch' ~/tabctl/inputd.py", timeout=20)
    if out.strip() in ("", "0"):
        if subprocess.run(["scp", "-q", "-o", "BatchMode=yes", TABIN_INPUTD, "tab:tabctl/inputd.py"], capture_output=True, timeout=30).returncode:
            raise ValueError("Could not copy inputd.py to the tablet (is it reachable?).")
        ssh_tab("sudo python3 ~/tabctl/inputd.py stop", timeout=20)
    rc, out, err = ssh_tab("sudo python3 ~/tabctl/inputd.py start", timeout=25)
    if rc or "inputd" not in out:
        raise ValueError("The input daemon on the tablet did not start: %s" % (err or out.strip() or rc))
    rc, out, err = ssh_tab("sudo python3 ~/tabctl/inputd.py token", timeout=15)
    if rc or not out.strip():
        raise ValueError("Could not read the input daemon's token: %s" % err)
    os.makedirs(STATE, exist_ok=True)
    fd = os.open(TABIN_TOKEN, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(out.strip() + "\n")
    tw, th, rot = tabscr_probe()
    _tabscr.update(rot=rot)
    w, h = max(160, int(tw * cfg["scale"]) // 16 * 16), max(160, int(th * cfg["scale"]) // 16 * 16)   # the AVC encoder likes multiples of 16
    rec = "sudo screenrecord --output-format=h264 --size %dx%d --bit-rate %dM --time-limit 0 -" % (w, h, cfg["bitrate"])
    ok = False
    for attempt in range(3):          # the ssh route to the tablet sometimes gives up: check the stream really connected
        log = open(os.path.join(STATE, "tabscreen.log"), "w")
        for pat in ("127.0.0.1:%d:127.0.0.1:%d" % (TABSCR_LPORT, TABIN_RPORT),):
            sh(["pkill", "-f", pat])
        tun = subprocess.Popen(["ssh", "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=10",
                                "-L", "127.0.0.1:%d:127.0.0.1:%d" % (TABSCR_LPORT, TABIN_RPORT), "tab"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log, start_new_session=True)
        for _ in range(24):               # the tunnel first and alone: once the stream runs the tablet answers new connections slowly
            if port_open("127.0.0.1", TABSCR_LPORT, 0.3) or tun.poll() is not None:
                break
            time.sleep(0.5)
        if tun.poll() is not None or not port_open("127.0.0.1", TABSCR_LPORT, 0.3):
            log.close()
            tabscr_stop()
            time.sleep(1.5)
            continue
        src = subprocess.Popen(["ssh", "-n", "-T", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3", "tab", rec],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=log, start_new_session=True)
        dec = subprocess.Popen([ffmpeg, "-loglevel", "error", "-flags", "low_delay", "-probesize", "32768",
                                "-analyzeduration", "0", "-f", "h264", "-i", "pipe:0", "-an", "-pix_fmt", "yuvj420p", "-c:v", "mjpeg", "-q:v", "5", "-metadata", "title=macotron-tabscreen", "-f", "image2pipe", "pipe:1"],
                               stdin=src.stdout, stdout=subprocess.PIPE, stderr=log, start_new_session=True)
        src.stdout.close()
        log.close()
        threading.Thread(target=tabscr_consume, args=(dec,), daemon=True).start()
        for _ in range(16):
            time.sleep(0.5)
            if _frame["jpg"] and src.poll() is None and tun.poll() is None:
                ok = True
                break
            if src.poll() is not None or tun.poll() is not None:
                break
        if ok:
            break
        tabscr_stop()
        time.sleep(1.5)
    if not ok:
        _tabscr["error"] = "The tablet did not answer over ssh or sent no picture (it can be slow for a moment): switch it on again."
        tabscr_stop()
        raise ValueError(_tabscr["error"])
    _tabscr.update(size=(tw, th), started=time.time(), error="")
    shw = min(w, 1200)
    shh = int(shw * h / w)
    subprocess.Popen([TABSCR_CHROME, "--user-data-dir=%s" % TABSCR_PROFILE, "--app=http://localhost:%d/tabscreen?t=%s" % (port, TOKEN),
                      "--window-size=%d,%d" % (shw, shh + 30), "--no-first-run", "--no-default-browser-check",
                      "--disable-features=Translate"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return tabscr_state()


def tabscr_op(body, port=8090):
    op = str(body.get("op", "status"))
    if op == "status":
        return tabscr_state()
    if op == "set":
        tabscr_set(body)
        if body.get("restart") and tabscr_running():
            return tabscr_start(port)
        return tabscr_state()
    if op == "start":
        if any(k in body for k in TABSCR_DEFAULTS):
            tabscr_set(body)
        return tabscr_start(port)
    if op == "stop":
        return tabscr_stop()
    raise ValueError("unknown op")


def tabscr_loop():
    """Stops the stream when its window is closed, and restarts it when the tablet turns (the video size is fixed when it starts)."""
    gone = 0
    while True:
        time.sleep(5)
        try:
            if not tabscr_running():
                gone = 0
                continue
            if not _tabscr["size"]:                        # a stream left over from before Macotron restarted: nobody reads it
                tabscr_stop()
                continue
            if time.time() - _tabscr["started"] < 12:
                continue
            gone = gone + 1 if not tabscr_window_open() else 0
            if gone >= 2:
                tabscr_stop()
                gone = 0
                continue
            tw, th, rot = tabscr_probe()
            _tabscr["rot"] = rot
            if (tw, th) != _tabscr["size"]:
                tabscr_start(PORT_NOW[0])
        except Exception:                                  # noqa: BLE001 - the loop must not die
            pass


PORT_NOW = [8090]                                          # the port Macotron serves on (set when the server starts)

if "--no-monitor" not in sys.argv:
    threading.Thread(target=tabscr_loop, daemon=True).start()


TABSCR_PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>Tablet</title>
<style>html,body{margin:0;height:100%;background:#05080d;overflow:hidden;color:#8aa0b8;font:13px -apple-system,sans-serif}
#v{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;user-select:none;-webkit-user-drag:none;touch-action:none;cursor:crosshair}
#hint{position:absolute;left:8px;bottom:6px;opacity:.55;pointer-events:none}</style></head><body tabindex="0">
<img id="v" alt="">
<div id="hint">Click = tap, drag, wheel = swipe, right-click = Back. Keys go to the tablet while this window is active.</div>
<script>
const T=new URLSearchParams(location.search).get('t')||'';
const v=document.getElementById('v');
v.src='/api/tabscreen.mjpg?t='+encodeURIComponent(T);
const KEYS={KeyA:30,KeyB:48,KeyC:46,KeyD:32,KeyE:18,KeyF:33,KeyG:34,KeyH:35,KeyI:23,KeyJ:36,KeyK:37,KeyL:38,KeyM:50,KeyN:49,KeyO:24,KeyP:25,KeyQ:16,KeyR:19,KeyS:31,KeyT:20,KeyU:22,KeyV:47,KeyW:17,KeyX:45,KeyY:21,KeyZ:44,
 Digit1:2,Digit2:3,Digit3:4,Digit4:5,Digit5:6,Digit6:7,Digit7:8,Digit8:9,Digit9:10,Digit0:11,Minus:12,Equal:13,BracketLeft:26,BracketRight:27,Semicolon:39,Quote:40,Backquote:41,Backslash:43,Comma:51,Period:52,Slash:53,IntlBackslash:86,
 Space:57,Enter:28,NumpadEnter:96,Tab:15,Backspace:14,Escape:1,Delete:111,ArrowUp:103,ArrowDown:108,ArrowLeft:105,ArrowRight:106,Home:102,End:107,PageUp:104,PageDown:109,
 F1:59,F2:60,F3:61,F4:62,F5:63,F6:64,F7:65,F8:66,F9:67,F10:68,F11:87,F12:88,ShiftLeft:42,ShiftRight:54,ControlLeft:29,ControlRight:97,AltLeft:56,AltRight:100,MetaLeft:29,MetaRight:97,CapsLock:58};
let q=[],moveLast=null,timer=0;
function send(e){if(e.t=='m'){moveLast=e}else{if(moveLast){q.push(moveLast);moveLast=null}q.push(e)}if(!timer)timer=setTimeout(flush,16)}
function flush(){timer=0;if(moveLast){q.push(moveLast);moveLast=null}if(!q.length)return;const ev=q;q=[];
 fetch('/api/tabscreen-input?t='+encodeURIComponent(T),{method:'POST',body:JSON.stringify({events:ev}),keepalive:true,headers:{'Content-Type':'application/json'}}).catch(()=>{})}
function frac(e){const r=v.getBoundingClientRect(),nw=v.naturalWidth||16,nh=v.naturalHeight||9,s=Math.min(r.width/nw,r.height/nh),w=nw*s,h=nh*s,x0=r.left+(r.width-w)/2,y0=r.top+(r.height-h)/2;
 return {fx:Math.max(0,Math.min(1,(e.clientX-x0)/w)),fy:Math.max(0,Math.min(1,(e.clientY-y0)/h)),w,h}}
let down=false;
v.addEventListener('pointerdown',e=>{document.body.focus();if(e.button==2){send({t:'k',c:158,v:1});send({t:'k',c:158,v:0});return}if(e.button!=0)return;down=true;v.setPointerCapture(e.pointerId);const f=frac(e);send({t:'d',fx:f.fx,fy:f.fy})});
v.addEventListener('pointermove',e=>{if(!down)return;const f=frac(e);send({t:'m',fx:f.fx,fy:f.fy})});
v.addEventListener('pointerup',e=>{if(!down)return;down=false;const f=frac(e);send({t:'m',fx:f.fx,fy:f.fy});send({t:'u'})});
v.addEventListener('pointercancel',()=>{if(down){down=false;send({t:'u'})}});
v.addEventListener('contextmenu',e=>e.preventDefault());
let wacc={x:0,y:0},wt=0;
v.addEventListener('wheel',e=>{e.preventDefault();const f=frac(e);wacc.x+=e.deltaX;wacc.y+=e.deltaY;if(wt)return;wt=setTimeout(()=>{wt=0;
 const dyf=Math.max(-.35,Math.min(.35,-wacc.y/f.h*0.9)),dxf=Math.max(-.35,Math.min(.35,-wacc.x/f.w*0.9));wacc={x:0,y:0};
 if(Math.abs(dyf)>.004||Math.abs(dxf)>.004)send({t:'w',fx:f.fx,fy:f.fy,dx:dxf,dy:dyf})},60)},{passive:false});
function key(e,val){const c=KEYS[e.code];if(!c)return;e.preventDefault();send({t:'k',c,v:val==1&&e.repeat?2:val})}
window.addEventListener('keydown',e=>key(e,1));window.addEventListener('keyup',e=>key(e,0));
window.addEventListener('blur',()=>{send({t:'u'});down=false});
</script></body></html>"""


# ---- every port through the tablet: a SOCKS5 proxy on localhost (Settings > All ports through the tablet) ------
# The localhost tunnels of the target list carry one fixed remote port each (aponno 8033 -> 3389). A SOCKS5 proxy
# (ssh -D through the tablet, which reaches the Tailscale peers) carries any port of any peer for every program
# that can use a SOCKS proxy: one local port, no admin rights.
SOCKS_CFG = os.path.join(STATE, "socksproxy.json")


def socks_cfg():
    cfg = {"on": False, "port": 1080}
    try:
        with open(SOCKS_CFG) as f:
            cfg.update({k: v for k, v in json.load(f).items() if k in cfg})
    except (OSError, ValueError):
        pass
    return cfg


def socks_running(port=None):
    port = port or socks_cfg()["port"]
    return bool(sh(["pgrep", "-f", "ssh -N .*-D 127.0.0.1:%d " % port]).split())


def socks_state():
    cfg = socks_cfg()
    peers = [{"name": t["name"], "ip": t["rip"]} for t in read_targets() if re.match(r"^\d+\.\d+\.\d+\.\d+$", t.get("rip", ""))]
    return {"cfg": cfg, "running": socks_running(cfg["port"]), "peers": peers}


def socks_stop():
    sh(["pkill", "-f", "ssh -N .*-D 127.0.0.1:%d " % socks_cfg()["port"]])
    time.sleep(0.4)


def socks_start():
    cfg = socks_cfg()
    if socks_running(cfg["port"]):
        return
    if port_open("127.0.0.1", cfg["port"], 0.3):
        raise ValueError("Port %d on this Mac is in use by another program: choose another port." % cfg["port"])
    for attempt in range(4):             # the ssh route to the tablet sometimes gives up at once: ask again
        p = subprocess.Popen(["ssh", "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=10",
                              "-o", "ServerAliveCountMax=3", "-D", "127.0.0.1:%d" % cfg["port"], "tab"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        for _ in range(24):
            if port_open("127.0.0.1", cfg["port"], 0.3):
                return
            if p.poll() is not None:     # ssh gave up: no need to wait out the 12 s
                break
            time.sleep(0.5)
        socks_stop()
        time.sleep(1.5)
    raise ValueError("The tablet did not answer over ssh (it can be slow for a moment): try again.")


def socks_op(body):
    op = str(body.get("op", "status"))
    cfg = socks_cfg()
    if op == "status":
        return socks_state()
    if "port" in body:
        v = int(body["port"])
        if not 1024 <= v <= 65535:
            raise ValueError("the port must be 1024 to 65535")
        if v != cfg["port"]:
            socks_stop()
            cfg["port"] = v
    if op in ("on", "off"):
        cfg["on"] = op == "on"
    os.makedirs(STATE, exist_ok=True)
    with open(SOCKS_CFG, "w") as f:
        json.dump(cfg, f)
    if op == "off":
        socks_stop()
    elif cfg["on"]:
        socks_start()
    return socks_state()


def socks_loop():
    """Keeps the proxy up while it is switched on (the ssh link drops when the Wi-Fi changes)."""
    while True:
        time.sleep(8)
        try:
            cfg = socks_cfg()
            if cfg["on"] and not socks_running(cfg["port"]):
                socks_start()
        except Exception:                              # noqa: BLE001 - the loop must not die
            pass


if "--no-monitor" not in sys.argv:
    threading.Thread(target=socks_loop, daemon=True).start()


def tabin_op(body):
    op = str(body.get("op", "status"))
    if op == "status":
        return tabin_state()
    if op == "set":
        tabin_set(body)
        return tabin_state()
    if op == "start":
        if any(k in body for k in TABIN_DEFAULTS):
            tabin_set(body)
        return tabin_start()
    if op == "stop":
        return tabin_stop(daemon=bool(body.get("daemon")))
    raise ValueError("unknown op")


def mac_notify(title, subtitle, text, sound=None):
    q = lambda s: '"%s"' % str(s).replace("\\", "\\\\").replace('"', '\\"')
    script = "display notification %s with title %s subtitle %s" % (q(text[:230]), q(title), q(subtitle))
    if sound:
        script += " sound name %s" % q(sound)
    try:
        subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def read_urls():
    if not os.path.exists(URLS_FILE):
        try:
            with open(URLS_FILE, "w") as f:
                f.write(DEFAULT_URLS)
        except OSError:
            pass
    out = []
    try:
        for line in open(URLS_FILE):
            p = [x.strip() for x in line.split("|")]
            if len(p) >= 2 and p[0] and not p[0].startswith("#"):
                codes = {int(c) for c in (p[2] if len(p) > 2 else "200").split() if c.isdigit()}
                out.append((p[0], p[1], codes or {200}))
    except OSError:
        pass
    return out


def http_probe(url, timeout=6):
    import urllib.request, urllib.error
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))    # never through a proxy
    try:
        with opener.open(urllib.request.Request(url, method="GET"), timeout=timeout) as r:
            return r.status, ""
    except urllib.error.HTTPError as e:
        try:
            body = e.read(600).decode(errors="replace")
        except Exception:
            body = ""
        return e.code, body
    except Exception as e:
        return None, str(getattr(e, "reason", e))


class MonCheck:
    def __init__(self, cid, label, group, debounce=2):
        self.id, self.label, self.group, self.debounce = cid, label, group, debounce
        self.status, self.msg, self.action, self.quiet = "unknown", "", "", False
        self.since, self.bad = None, 0
        self.noted, self.noted_at = None, 0.0
        self.told = False             # a notification about this bad spell went out
        self.attempts, self.last_fix, self.fixes = 0, 0.0, []

    def set(self, status, msg, action="", quiet=False):
        if status in ("ok", "skip"):
            self.bad, self.since, self.attempts = 0, None, 0
        else:
            self.bad += 1
            self.since = self.since or time.time()
        if status != self.status:
            mon_log("%s: %s -> %s: %s%s" % (self.id, self.status, status, msg, (" | " + action) if action else ""))
        self.status, self.msg, self.action, self.quiet = status, msg, action, quiet

    def may_fix(self, cooldown, most=3):
        return self.attempts < most and time.time() - self.last_fix >= cooldown

    def did(self, what, rc, out=""):
        self.attempts += 1
        self.last_fix = time.time()
        self.fixes = (self.fixes + ["%s %s (%s)" % (time.strftime("%H:%M:%S"), what, "ok" if rc == 0 else "rc %s" % rc)])[-5:]
        mon_log("%s: repair: %s -> rc %s %s" % (self.id, what, rc, str(out)[:300].replace("\n", " ")))

    def as_dict(self):
        return {"id": self.id, "label": self.label, "group": self.group, "status": self.status, "msg": self.msg,
                "action": self.action, "quiet": self.quiet, "since": self.since, "fixes": self.fixes}


class Monitor(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="monitor")
        self.dev = wifi_dev()
        self.checks = {}
        self.lock = threading.Lock()
        try:
            self.saved = json.load(open(MON_FILE))
        except (OSError, ValueError):
            self.saved = {}
        self.saved.setdefault("macs", [])
        self.saved.setdefault("muted_until", 0)
        self.saved.setdefault("off", [])        # checks whose alarm is off: Settings, or Mute on the row
        self.saved.setdefault("ignore", {})     # check id -> until when it counts as fine: Ignore on the row
        self.saved.setdefault("notify", {})     # warn, back, sound: false turns that kind off
        self.saved.setdefault("targets_on", False)
        self.targets_t = 0.0
        self.target_job = None
        self.target_reported = None
        self.target_retry = {}
        self.target_msg = "Waiting for the next check."
        self.verified = {}                # (router, mac) -> (is the tablet, when)
        self.tablet, self.tablet_t, self.tablet_err = {}, 0.0, ""
        self.fetch_t = self.names_t = self.urls_t = 0.0
        self.last_tick = time.time()
        self.grace_until = time.time() + 15
        self.info = {}
        self.wake = threading.Event()

    def chk(self, cid, label, group, debounce=2):
        with self.lock:
            if cid not in self.checks:
                self.checks[cid] = MonCheck(cid, label, group, debounce)
            self.checks[cid].label = label
            return self.checks[cid]

    def save(self):   # the page's requests and the checks both save: one at a time, whole files only
        with self.lock:
            data = json.dumps(self.saved)
            try:
                with open(MON_FILE + ".tmp", "w") as f:
                    f.write(data)
                os.replace(MON_FILE + ".tmp", MON_FILE)
            except OSError:
                pass

    def muted(self):
        mu = self.saved.get("muted_until", 0)
        return mu == -1 or mu > time.time()

    def ignored(self, cid):
        return self.saved["ignore"].get(cid, 0) > time.time()

    def silenced(self, cid):   # all muted, its alarm off, or ignored
        return self.muted() or cid in self.saved["off"] or self.ignored(cid)

    def may_notify(self, k, kind):   # kind: fail, warn or back (to normal)
        return not self.silenced(k.id) and (kind == "fail" or self.saved["notify"].get(kind, True))

    def control(self, op, body=None):
        body = body or {}
        if op == "targets":
            self.set_targets(body.get("on"))
            return
        cid, hours = str(body.get("id", "")), body.get("hours")
        if op == "mute1h":
            op, hours = "mute_for", 1
        with self.lock:
            if op in ("mute_for", "ignore"):
                try:
                    hours = float(hours)
                except (TypeError, ValueError):
                    hours = 0
                if not 0 < hours <= 720:
                    raise ValueError("Give the hours as a number above 0 and at most 720 (30 days).")
            if op in ("alarm", "ignore", "unignore") and cid not in self.checks:
                raise ValueError("unknown check %r" % cid)
            if op == "mute_for":
                self.saved["muted_until"] = time.time() + hours * 3600
            elif op == "mute":
                self.saved["muted_until"] = -1
            elif op == "unmute":
                self.saved["muted_until"] = 0
            elif op == "alarm":
                off = [c for c in self.saved["off"] if c != cid]
                self.saved["off"] = off if body.get("on") else off + [cid]
            elif op == "ignore":
                self.saved["ignore"][cid] = time.time() + hours * 3600
            elif op == "unignore":
                self.saved["ignore"].pop(cid, None)
            elif op == "notify":
                if cid not in ("warn", "back", "sound"):
                    raise ValueError("unknown kind of notification %r" % cid)
                self.saved["notify"][cid] = bool(body.get("on"))
            elif op == "recheck":
                self.fetch_t = self.names_t = self.urls_t = 0.0
                for k in list(self.checks.values()):
                    k.attempts = 0
            else:
                raise ValueError("unknown monitor op %r" % op)
        self.save()
        how = "%g h" % hours if op in ("mute_for", "ignore") else ("on" if body.get("on") else "off") if op in ("alarm", "notify") else ""
        mon_log("control: " + " ".join(s for s in (op, cid, how) if s))
        self.wake.set()

    def set_targets(self, on):
        if not isinstance(on, bool):
            raise ValueError("Continuous monitoring needs on: true or false.")
        with TARGETS_LOCK, self.lock:
            saved = dict(self.saved, targets_on=on)
            try:
                with open(MON_FILE + ".tmp", "w") as f:
                    json.dump(saved, f)
                os.replace(MON_FILE + ".tmp", MON_FILE)
            except OSError as e:
                mon_log("cannot save continuous monitoring: %s" % e)
                raise ValueError("Could not save continuous monitoring: %s" % e) from e
            self.saved = saved
            self.targets_t = 0.0
            if on:
                self.target_retry.clear()
            self.target_msg = "Waiting for the next check." if on else "Off; existing connections are left open."
        mon_log("continuous target monitoring: " + ("on" if on else "off"))
        self.wake.set()

    def check_targets(self):
        if self.target_job:
            job = JOBS.get(self.target_job)
            if job and not job["done"]:
                return
            if job and self.target_reported != self.target_job:
                self.target_reported = self.target_job
                self.target_msg = ("Recovery finished; checking again." if job["rc"] == 0 else
                                   "Recovery failed (exit %s). See Recovery log; retrying automatically." % job["rc"])
                if not self.saved["targets_on"]:
                    self.target_msg += " Monitoring is off."
                mon_log("continuous recovery finished: rc %s" % job["rc"])
        if not self.saved["targets_on"] or time.time() - self.targets_t < 30:
            return
        with JOBS_LOCK:
            if any(not j["done"] for j in JOBS.values()):
                return
        self.targets_t = time.time()
        s = status()
        if not s["sshd"]:
            self.target_msg = "Tablet SSH unavailable; retrying every 30 s."
            return
        ready, waiting, cooling = [], [], []
        for tg in s["targets"]:
            if not target_on_steps([tg], nat=DEVICE_ROOT):
                continue
            if time.time() < self.target_retry.get(tg["name"], 0):
                cooling.append(tg["name"])
                continue
            host, port = ("127.0.0.1", str(tg["lport"])) if tg["serve"] else (tg["rip"], tg["rport"])
            if port.isdigit():
                probe = "timeout 3 bash -c %s" % shlex.quote(
                    "(echo > %s) 2>/dev/null" % shlex.quote("/dev/tcp/%s/%s" % (host, port)))
            else:
                probe = "ping -c 1 -W 2 %s >/dev/null 2>&1" % shlex.quote(host)
            rc, out, err = ssh_tab(probe, timeout=10)
            if rc == 0:
                ready.append(tg)
            else:
                waiting.append(tg["name"])
                if rc not in (1, 124):
                    mon_log("availability probe for %s failed: rc %s %s" % (tg["name"], rc, err or out))
        steps = target_on_steps(ready, nat=DEVICE_ROOT)
        with TARGETS_LOCK:
            if not self.saved["targets_on"]:
                return
            with JOBS_LOCK:
                if any(not j["done"] for j in JOBS.values()):
                    return
            if not steps:
                self.target_msg = ("Waiting to retry %s (Samba attempts are limited to once every 5 minutes); "
                                   "see Recovery log." % ", ".join(cooling) if cooling else
                                   "Waiting for %s; retrying every 30 s." % ", ".join(waiting) if waiting else
                                   "Available connections are enabled; checking every 30 s.")
                return
            self.target_job = run_job("Continuous recovery", ["env", "MOUNT_WAIT=15", "bash", "-c",
                                     "rc=0\n" + "\n".join(steps) + "\nexit $rc\n"])
            for tg in ready:
                # Avoid repeated Finder authentication dialogs when credentials are not saved.
                self.target_retry[tg["name"]] = time.time() + (300 if tg["serve"] else 30)
            self.target_msg = "Enabling %s; see Recovery log." % ", ".join(tg["name"] for tg in ready)
            mon_log(self.target_msg)

    # ---- is the router the tablet? ----
    def is_tablet(self, gw, mac):
        if mac and mac in self.saved["macs"]:
            return True
        ok, when = self.verified.get((gw, mac), (None, 0))
        if ok is not None and time.time() - when < 60:
            return ok
        ok = ssh_tab("true", host=gw, timeout=10)[0] == 0
        if not ok:      # sshd may be down: the root name responder only answers on the hotspot
            prefix = gw.rsplit(".", 1)[0] + "."
            ok = any(resolve("%s.%s" % (t["name"], NAT_DOMAIN)).startswith(prefix) for t in read_targets() if t["nat"])
        self.verified[(gw, mac)] = (ok, time.time())
        if ok and mac and mac not in self.saved["macs"]:
            self.saved["macs"] = (self.saved["macs"] + [mac])[-20:]
            self.save()
            mon_log("learned hotspot router %s (%s)" % (gw, mac))
        return ok

    def run(self):
        while True:
            t0 = time.monotonic()
            try:
                self.tick()
            except Exception:
                import traceback
                mon_log("tick failed: " + traceback.format_exc().replace("\n", " | ")[-800:])
            self.wake.wait(max(0.5, MON_TICK - (time.monotonic() - t0)))
            self.wake.clear()

    def tick(self):
        t = time.time()
        if t - self.last_tick > 3 * MON_TICK:          # the Mac slept: give the network time to come back
            self.grace_until = t + 20
            mon_log("woke after %d s; 20 s grace" % (t - self.last_tick))
        self.last_tick = t
        dev = self.dev
        ip = sh(["ipconfig", "getifaddr", dev], 4).strip()
        gw = sh(["ipconfig", "getoption", dev, "router"], 4).strip()
        wifi = self.chk("wifi", ("Wi-Fi on %s" % HOTSPOT_SSID) if DEVICE_HOTSPOT else "Mac and portal on the same Wi-Fi", "mac")
        on, tcp_gw = False, None
        if not DEVICE_HOTSPOT:
            # no hotspot (no SIM): the portal answers on the Mac's own Wi-Fi at its LAN address
            if not ip:
                wifi.set("fail", "The Mac's Wi-Fi is off or not connected.", "Join the same Wi-Fi as the portal (%s)." % DEVICE_LABEL)
            elif not DEVICE_LAN:
                wifi.set("warn", "No LAN address saved for %s." % DEVICE_LABEL,
                         "Run: phone-proxy.sh device set %s lan=<its Wi-Fi address>" % DEVICE_NAME)
            else:
                tcp_gw = bound_tcp(dev, DEVICE_LAN, SSH_PORT, 2)
                on = tcp_gw is not None
                if on:
                    gw = DEVICE_LAN
                    wifi.set("ok", "same Wi-Fi: Mac %s, portal %s" % (ip, DEVICE_LAN))
                else:
                    wifi.set("fail", "%s does not answer at %s on this Wi-Fi." % (DEVICE_LABEL, DEVICE_LAN),
                             "Put the Mac and the portal on the same Wi-Fi, and run sshd in Termux on the portal.")
        elif not ip:
            if "On" not in sh(["networksetup", "-getairportpower", dev], 5):
                wifi.set("fail", "The Mac's Wi-Fi is off.", "Turn Wi-Fi on and join %s." % HOTSPOT_SSID)
            else:
                wifi.set("fail", "The Mac's Wi-Fi is not connected to any network.", "Join %s from the Wi-Fi menu." % HOTSPOT_SSID)
        else:
            tcp_gw = bound_tcp(dev, gw, SSH_PORT, 2) if gw else None
            on = bool(gw) and self.is_tablet(gw, arp_mac(gw))
            if on:
                wifi.set("ok", "on %s: Mac %s, tablet %s" % (HOTSPOT_SSID, ip, gw))
            else:
                wifi.set("fail", "The Mac's Wi-Fi left %s: it is on another network (router %s)." % (HOTSPOT_SSID, gw or "none"),
                         "Pick %s in the Wi-Fi menu." % HOTSPOT_SSID)
        # rejoin only while its alarm is heard: Mute or Ignore means the user left the hotspot on purpose
        if DEVICE_HOTSPOT and wifi.status == "fail" and "Wi-Fi is off" not in wifi.msg and not self.silenced(wifi.id) and wifi.bad >= 2 and wifi.may_fix(60 if wifi.attempts < 3 else 300, most=10 ** 6):
            try:
                r = subprocess.run(["networksetup", "-setairportnetwork", dev, HOTSPOT_SSID], capture_output=True, text=True, timeout=40)
                out = (r.stdout + r.stderr).strip()
                rc = 1 if (r.returncode or "rror" in out or "ould not" in out) else 0
            except (OSError, subprocess.SubprocessError) as e:
                out, rc = str(e), 1
            wifi.did("rejoin %s" % HOTSPOT_SSID, rc, out)
            self.wake.set()
        if DEVICE_HOTSPOT and wifi.status == "fail" and wifi.attempts >= 3:
            wifi.action = ("Macotron tried to rejoin %s %d times and keeps trying every 5 minutes. Is the tablet's hotspot on? "
                           "Pick it in the Wi-Fi menu, or press Mute or Ignore on this row if you left it on purpose." % (HOTSPOT_SSID, wifi.attempts))

        # the tablet itself (kernel answer on 8022: the hotspot path, else Tailscale)
        tab = self.chk("tablet", "Tablet reachable", "mac")
        if on:
            tab.set("ok", "answers on the hotspot") if tcp_gw is not None else tab.set(
                "fail", "The tablet does not answer on the hotspot (port 8022).",
                "Tablotron's watch repairs Termux by itself within a minute; if this lasts, open Termux on the tablet and run termux-wake-lock.")
        else:
            ts = bound_tcp_any(TABLET_TS, SSH_PORT)
            tab.set("ok", "answers over Tailscale only") if ts is not None else tab.set("skip", "not on the hotspot and not on Tailscale: not checked")

        # internet through the hotspot
        net = self.chk("internet", "Internet through the hotspot", "mac")
        tw = {c["id"]: c for c in (self.tablet.get("checks") or [])} if t - self.tablet_t < 40 else {}
        if on:
            ms = None
            for h, p in MON_INTERNET:
                ms = bound_tcp(dev, h, p, 2)
                if ms is not None:
                    break
            if ms is not None:
                net.set("ok", "reachable (%d ms)" % ms)
            else:
                why = next((tw[k]["msg"] for k in ("wifi", "internet") if k in tw and tw[k]["status"] == "fail"), "")
                net.set("fail", "No internet through the hotspot." + (" Tablet says: " + why if why else ""),
                        (tw.get("wifi") or tw.get("internet") or {}).get("action", "") or "See the tablet checks below.",
                        quiet=bool(why))
        else:
            net.set("skip", "not on the hotspot: not checked")

        # Tablotron's watch, every 15 s
        if DEVICE_ROOT and t - self.fetch_t >= 15 and (on or tab.status == "ok"):
            self.fetch_t = t
            rc, out, err = ssh_tab("cat ~/.config/tabctl/watch.json", timeout=15)
            try:
                st = json.loads(out) if rc == 0 else None
            except ValueError:
                st = None
            if st:
                self.tablet, self.tablet_t, self.tablet_err = st, t, ""
            else:
                self.tablet_err = (err.splitlines() or ["ssh rc %d" % rc])[-1][:200]
        tw_chk = self.chk("tabwatch", "Tablotron watch", "mac")
        age = t - self.tablet.get("t", 0) if self.tablet else None
        if not DEVICE_ROOT:
            tw_chk.set("skip", "%s is not rooted: no Tablotron watch" % DEVICE_NAME)
        elif not (on or tab.status == "ok"):
            tw_chk.set("skip", "tablet not reachable from here: not checked")
        elif self.tablet_err and t - self.tablet_t > 40:
            tw_chk.set("fail", "Cannot read Tablotron's watch: %s" % self.tablet_err,
                       "Open Termux on the tablet and run termux-wake-lock.", quiet=tab.status == "fail")
        elif age is None or age > 45 or self.tablet.get("stopped"):
            tw_chk.set("fail", "Tablotron's watch is not running (last pass %s)." % ("never" if age is None else "%d s ago" % age),
                       "Macotron starts it; if that fails, run on the tablet: sudo python3 ~/tabctl/watch.py start")
            if tw_chk.may_fix(120) and not self.ignored(tw_chk.id):
                rc, out, err = ssh_tab("sudo python3 ~/tabctl/watch.py start", timeout=30)
                tw_chk.did("start Tablotron's watch", rc, out + err)
                self.fetch_t = 0
        else:
            tw_chk.set("ok", "running, last pass %d s ago" % max(0, age))

        # relay the tablet's own checks while its state is fresh (peers: the NAT rows say it here)
        fresh = bool(self.tablet) and t - self.tablet_t < 40 and age is not None and age <= 45
        if fresh:
            for c in self.tablet.get("checks") or []:
                mc = self.chk("t:" + c["id"], c["label"], "tablet", debounce=1)
                st = c["status"] if c["status"] in ("ok", "warn", "fail") else "skip"
                mc.set(st, c["msg"], c.get("action", ""), quiet=c.get("quiet", False) or c["id"] == "peers")
                mc.fixes = c.get("fixes") or []
        else:
            for k in list(self.checks.values()):
                if k.group == "tablet":
                    k.set("skip", "no fresh report from Tablotron's watch")

        # names and NAT addresses, every 30 s
        if on and DEVICE_ROOT and t - self.names_t >= 30:
            self.names_t = t
            peers = (self.tablet.get("info") or {}).get("peers") or {}
            prefix = gw.rsplit(".", 1)[0]
            for tg in read_targets():
                if not tg["nat"]:
                    continue
                name = "%s.%s" % (tg["name"], NAT_DOMAIN)
                vip = "%s.%s" % (prefix, tg["nat"])
                k = self.chk("nat:" + tg["name"], name, "mac")
                got = resolve(name)
                if got != vip:
                    k.set("fail", "%s %s (expected %s)." % (name, "resolves to " + got if got else "does not resolve", vip),
                          "Press NAT refresh in Macotron (the tablet's name responder may be down).")
                    continue
                port = int(tg["rport"]) if tg["rport"].isdigit() else 0
                ms = bound_tcp(dev, vip, port, 3) if port else (0 if ping_ok(vip) else None)
                if ms is not None:
                    k.set("ok", "%s -> %s, port %s answers (%d ms)" % (name, vip, port or "ping", ms))
                elif tg["name"] in peers and peers[tg["name"]] is None:
                    k.set("warn", "%s does not answer; the tablet cannot reach %s over Tailscale either (switched off?)." % (name, tg["name"]),
                          "Check that %s is on and connected to Tailscale." % tg["name"])
                else:
                    k.set("fail", "%s (%s) does not answer on port %s, though the tablet reaches %s." % (name, vip, port, tg["name"]),
                          "Press NAT refresh in Macotron.")
                    if k.may_fix(300, most=2) and not self.ignored(k.id):
                        rc, out, err = ssh_tab("sudo ~/bin/nat-up.sh", timeout=60)
                        k.did("re-apply NAT on the tablet (nat-up.sh)", rc, out + err)
        elif not on or not DEVICE_ROOT:
            for k in self.checks.values():
                if k.id.startswith("nat:"):
                    k.set("skip", "not on the hotspot: not checked" if not on else "%s is not rooted: no NAT names" % DEVICE_NAME)

        # URLs, every 60 s
        if t - self.urls_t >= 60:
            self.urls_t = t
            for label, url, codes in read_urls():
                k = self.chk("url:" + label, label, "mac")
                if ".%s" % NAT_DOMAIN in url and (not on or not DEVICE_ROOT):
                    k.set("skip", "not on the hotspot: not checked" if not on else "%s is not rooted: no NAT names" % DEVICE_NAME)
                    continue
                code, body = http_probe(url)
                if code in codes:
                    k.set("ok", "%s answers (HTTP %d)" % (url, code))
                elif code == 403 and "Not served here" in body:
                    host = urlparse(url).hostname
                    k.set("fail", "The server answers but refuses the name %s (its Host allowlist)." % host,
                          "On that machine set TOOLBOX_HOSTS=%s for the server and restart it." % host)
                elif code:
                    k.set("fail", "%s answered HTTP %d (expected %s)." % (url, code, " or ".join(map(str, sorted(codes)))), "")
                else:
                    k.set("fail", "%s does not answer: %s" % (url, body), "")

        self.last_done = time.time()
        du = shutil.disk_usage("/")
        self.info = {"dev": dev, "ip": ip, "router": gw, "on_hotspot": on, "tablet_age": age,
                     "tablet_fetch_age": (t - self.tablet_t) if self.tablet_t else None, "tablet_err": self.tablet_err,
                     "mac_disk_total": du.total, "mac_disk_used": du.used, "mac_disk_used_pct": round(du.used * 100 / du.total, 1)}
        self.check_targets()
        self.notify_changes()

    def notify_changes(self):
        with self.lock:   # an Ignore that ran out: a check still failing says so at once
            over = [c for c, until in self.saved["ignore"].items() if until <= time.time()]
            for c in over:
                del self.saved["ignore"][c]
                if c in self.checks:
                    self.checks[c].noted = None
        if over:
            self.save()
            mon_log("ignore over: %s" % ", ".join(over))
        if time.time() < self.grace_until:
            return
        sound = self.saved["notify"].get("sound", True)
        for k in list(self.checks.values()):
            title = "Macotron" if k.group == "mac" else "Macotron - tablet"
            if k.status == "fail" and not k.quiet and k.bad >= k.debounce:
                if k.noted != "fail" or time.time() - k.noted_at >= RENOTIFY:
                    if self.may_notify(k, "fail"):
                        mac_notify(title, k.label + ": problem", (k.msg + " " + k.action).strip(), "Basso" if sound else None)
                        mon_log("notify %s: %s" % (k.id, k.msg))
                        k.told = True
                    k.noted, k.noted_at = "fail", time.time()
            elif k.status == "warn" and not k.quiet and k.noted is None and k.since and time.time() - k.since >= 30:
                if self.may_notify(k, "warn"):
                    mac_notify(title, k.label + ": warning", (k.msg + " " + k.action).strip(), "Purr" if sound else None)
                    mon_log("notify %s (warn): %s" % (k.id, k.msg))
                    k.told = True
                k.noted, k.noted_at = "warn", time.time()
            elif k.status in ("ok", "skip") and k.noted:   # back to normal only after a notification that went out
                if k.status == "ok" and k.told and self.may_notify(k, "back"):
                    mac_notify(title, k.label + ": back to normal", k.msg)
                    mon_log("notify %s: back to normal" % k.id)
                k.noted, k.told = None, False

    def state(self):
        with self.lock:
            order = {"fail": 0, "warn": 1, "unknown": 2, "ok": 3, "skip": 4}
            checks = [dict(k.as_dict(), alarm=k.id not in self.saved["off"],
                           ignored=self.saved["ignore"][k.id] if self.ignored(k.id) else 0) for k in list(self.checks.values())]
            checks.sort(key=lambda d: (d["group"] != "mac", order.get("ok" if d["ignored"] else d["status"], 5)))
            return {"t": time.time(), "last": getattr(self, "last_done", 0), "tick": MON_TICK, "info": self.info, "checks": checks,
                    "muted_until": self.saved.get("muted_until", 0), "ssid": HOTSPOT_SSID,
                    "notify": {n: self.saved["notify"].get(n, True) for n in ("warn", "back", "sound")},
                    "tablet_time": self.tablet.get("time", ""), "tablet_info": self.tablet.get("info") or {},
                    "targets": {"on": self.saved["targets_on"], "message": self.target_msg, "job": self.target_job},
                    "urls_file": URLS_FILE}


def bound_tcp_any(host, port, timeout=2.0):
    try:
        t0 = time.monotonic()
        with socket.create_connection((host, port), timeout):
            return int((time.monotonic() - t0) * 1000)
    except OSError:
        return None


MONITOR = None
ADB = None
ADB_ERROR = ""


class H(BaseHTTPRequestHandler):
    server_version = "macotron"

    def log_message(self, *a):
        pass

    def _ok(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("localhost", "127.0.0.1"):
            return False
        q = parse_qs(urlparse(self.path).query)
        tok = self.headers.get("X-Token") or (q.get("t") or [""])[0]
        return secrets.compare_digest(tok, TOKEN)

    def _send(self, code, body, ctype="application/json"):
        ctype = ctype.split(";")[0]
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._ok():
            return self._send(403, "forbidden", "text/plain")
        u = urlparse(self.path)
        if u.path == "/":
            return self._send(200, PAGE, "text/html")
        if u.path.startswith("/vendor/") and u.path[8:] in VENDOR_FILES:
            with open(os.path.join(VENDOR, u.path[8:]), "rb") as f:
                return self._send(200, f.read(), VENDOR_FILES[u.path[8:]])
        if u.path.startswith("/api/term/"):
            try:
                return self._send(200, json.dumps(term_api("GET", u.path.rsplit("/", 1)[-1], parse_qs(u.query), {})))
            except (ValueError, KeyError) as e:
                return self._send(400, json.dumps({"error": str(e)}))
        if u.path == "/api/status":
            return self._send(200, json.dumps(status()))
        if u.path == "/api/ping":
            return self._send(200, json.dumps({"boot": BOOT}))
        if u.path == "/api/tabctl":
            return self._send(200, json.dumps(tabctl_page()))
        if u.path == "/api/tabinput":
            return self._send(200, json.dumps(tabin_state()))
        if u.path == "/api/socks":
            return self._send(200, json.dumps(socks_state()))
        if u.path == "/api/tabscreen":
            return self._send(200, json.dumps(tabscr_state()))
        if u.path == "/tabscreen":
            return self._send(200, TABSCR_PAGE, "text/html")
        if u.path == "/api/tabscreen.mjpg":
            return tabscr_stream(self)
        if u.path == "/api/clips":
            return self._send(200, json.dumps(clip_list()))
        if u.path == "/api/clip-image":
            try:
                with open(clip_path((parse_qs(u.query).get("id") or [""])[0], "png"), "rb") as f:
                    return self._send(200, f.read(), "image/png")
            except (OSError, ValueError):
                return self._send(404, "{}")
        if u.path == "/api/job":
            j = JOBS.get((parse_qs(u.query).get("id") or [""])[0])
            if not j:
                return self._send(404, "{}")
            try:
                since = int((parse_qs(u.query).get("since") or ["0"])[0])
                if since < 0:
                    raise ValueError
            except ValueError:
                return self._send(400, json.dumps({"error": "Job offset must be a nonnegative integer."}))
            return self._send(200, json.dumps({"lines": j["out"][since:], "n": len(j["out"]),
                                               "done": j["done"], "rc": j["rc"], "title": j["title"]}))
        if u.path == "/api/device":
            return self._send(200, json.dumps(device_state()))
        if u.path == "/api/adb":
            return self._send(200, json.dumps(ADB.snapshot() if ADB else {"unavailable": True, "message": ADB_ERROR or "ADB backend not started."}))
        if u.path == "/api/monitor":
            return self._send(200, json.dumps(MONITOR.state() if MONITOR else {"off": True}))
        if u.path == "/api/watchlog":
            try:
                lines = open(os.path.join(STATE, "watch.log")).read().splitlines()[-40:]
            except OSError:
                lines = ["No watchdog log yet."]
            return self._send(200, json.dumps({"lines": [ANSI.sub("", l) for l in lines]}))
        # >>> bedrock-usage glue (patch_macotron.py) >>>
        if u.path == "/bedrock":
            if bedrock_glue is None:
                return self._send(404, "bedrock_glue module not available", "text/plain")
            data = bedrock_glue.read_view_html(HERE)
            if data is None:
                return self._send(404, "bedrock_usage_view.html not found", "text/plain")
            return self._send(200, data, "text/html")
        if u.path == "/api/awsauto":
            return self._send(200, json.dumps(aws_autologin.snapshot() if aws_autologin else {"enabled": False, "active": False, "remaining": None, "last_action": "module not available", "lead": 300}))
        if u.path == "/api/bedrock":
            if bedrock_glue is None:
                return self._send(200, json.dumps({"empty": True, "message": "bedrock_glue module not available"}))
            return self._send(200, json.dumps(bedrock_glue.get_snapshot()))
        # <<< bedrock-usage glue <<<
        # >>> reader glue (Reader tab) >>>
        if u.path == "/reader" or u.path.startswith("/api/reader/"):
            if reader_glue is None:
                return self._send(404, "reader_glue module not available", "text/plain")
            return reader_glue.handle_get(self, u)
        # <<< reader glue <<<
        # >>> offload glue (Offload tab) >>>
        if u.path == "/offload" or u.path.startswith("/api/offload/"):
            if offload_glue is None:
                return self._send(404, "offload_glue module not available", "text/plain")
            return offload_glue.handle_get(self, u)
        # <<< offload glue <<<
        self._send(404, "not found", "text/plain")

    def do_POST(self):
        if not self._ok():
            return self._send(403, "forbidden", "text/plain")
        # >>> reader glue (Reader tab): its uploads are raw bytes, so it reads its own body >>>
        if urlparse(self.path).path.startswith("/api/reader/"):
            if reader_glue is None:
                return self._send(404, json.dumps({"ok": False, "error": "reader_glue module not available"}))
            return reader_glue.handle_post(self, urlparse(self.path))
        # <<< reader glue <<<
        # >>> offload glue (Offload tab) >>>
        if urlparse(self.path).path.startswith("/api/offload/"):
            if offload_glue is None:
                return self._send(404, json.dumps({"ok": False, "error": "offload_glue module not available"}))
            return offload_glue.handle_post(self, urlparse(self.path))
        # <<< offload glue <<<
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("Request body must be a JSON object.")
            if urlparse(self.path).path == "/api/restart":
                restart_self()
                return self._send(200, json.dumps({"ok": True, "boot": BOOT}))
            if urlparse(self.path).path == "/api/awsauto":
                if aws_autologin is None:
                    return self._send(200, json.dumps({"ok": False, "detail": "aws_autologin module not available"}))
                return self._send(200, json.dumps(aws_autologin.set_enabled(bool(body.get("enabled")))))
            if urlparse(self.path).path == "/api/device":
                return self._send(200, json.dumps(device_op(body)))
            if urlparse(self.path).path == "/api/adb":
                if not ADB:
                    raise ValueError(ADB_ERROR or "ADB backend not started.")
                try:
                    result = ADB.operation(body)
                except OSError as e:
                    mon_log("ADB operation failed: %s" % e)
                    raise ValueError("ADB operation failed: %s" % e) from e
                return self._send(200, json.dumps(dict(result, state=ADB.snapshot())))
            if urlparse(self.path).path == "/api/tabinput":
                return self._send(200, json.dumps(tabin_op(body)))
            if urlparse(self.path).path == "/api/socks":
                return self._send(200, json.dumps(socks_op(body)))
            if urlparse(self.path).path == "/api/tabscreen":
                PORT_NOW[0] = self.server.server_address[1]
                return self._send(200, json.dumps(tabscr_op(body, PORT_NOW[0])))
            if urlparse(self.path).path == "/api/tabscreen-input":
                return self._send(200, json.dumps(tabscr_input(body.get("events", []))))
            if urlparse(self.path).path == "/api/clips":
                return self._send(200, json.dumps(clip_op(body)))
            if urlparse(self.path).path == "/api/monitor":
                if not MONITOR:
                    raise ValueError("the monitor is not running (started with --no-monitor)")
                MONITOR.control(str(body.get("op", "")), body)
                return self._send(200, json.dumps(MONITOR.state()))
            # >>> bedrock-usage glue (patch_macotron.py) >>>
            if urlparse(self.path).path == "/api/bedrock":
                if bedrock_glue is None:
                    return self._send(200, json.dumps({"ok": False, "detail": "bedrock_glue module not available"}))
                if str(body.get("op", "")) != "push":
                    return self._send(400, json.dumps({"error": "unknown op"}))
                return self._send(200, json.dumps(bedrock_glue.push_now()))
            # <<< bedrock-usage glue <<<
            if urlparse(self.path).path.startswith("/api/term/"):
                return self._send(200, json.dumps(term_api("POST", urlparse(self.path).path.rsplit("/", 1)[-1], {}, body)))
            action = urlparse(self.path).path.rsplit("/", 1)[-1]
            with TARGETS_LOCK:
                if action in ("alloff", "stop") and MONITOR:
                    MONITOR.set_targets(False)
                if action in ("all", "stop", "allon", "alloff", "tunnel", "untunnel", "mount", "unmount",
                              "nat", "add", "remove") and MONITOR and MONITOR.target_job:
                    job = JOBS.get(MONITOR.target_job)
                    if job and not job["done"]:
                        raise ValueError("Automatic recovery is still finishing. Monitoring is %s; retry this action "
                                         "when Recovery log finishes." % ("on" if MONITOR.saved["targets_on"] else "off"))
                title, argv, stdin = build(action, body)
                jid = run_job(title, argv, stdin)
        except (ValueError, KeyError) as e:
            return self._send(400, json.dumps({"error": str(e)}))
        self._send(200, json.dumps({"id": jid}))


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<script>try{document.documentElement.setAttribute("data-theme",localStorage.theme||"dark")}catch(e){}</script>
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Macotron</title>
<link rel="stylesheet" id="xcss">
<style>
:root{--bg:#0e1621;--card:#16212f;--line:#24344a;--fg:#e6edf5;--dim:#8aa0b8;--acc:#38bdf8;--ok:#34d399;--bad:#f87171;--warn:#fbbf24;--hdr1:#0b3b66;--rowline:#1d2b3e;--mute:#44566d;--swoff:#33465e;--btnbg:#1f3149;--btnborder:#2c4463;--btnhover:#27405f;--tabsbg:#0b1019;--inputbg:#0f1a28;--conbg:#080d14}
:root[data-theme="light"]{--bg:#f4f6f9;--card:#fff;--line:#dde3ea;--fg:#17202b;--dim:#5b6878;--acc:#0b72b9;--ok:#16a34a;--bad:#dc2626;--warn:#d97706;--hdr1:#dbe8f6;--rowline:#e3e8ee;--mute:#c7d0da;--swoff:#c7d0da;--btnbg:#eef3f8;--btnborder:#c9d4e0;--btnhover:#dde8f2;--tabsbg:#eaeef3;--inputbg:#eef2f6;--conbg:#f6f8fa}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
header{background:linear-gradient(90deg,var(--hdr1),var(--bg));padding:14px 22px;display:flex;align-items:center;gap:14px;border-bottom:1px solid var(--line)}
header h1{font-size:17px;margin:0;font-weight:650}header .sub{color:var(--dim);font-size:12.5px}header .ts{margin-left:auto;color:var(--dim);font-size:12px}
main{max-width:1150px;margin:0 auto;padding:18px;display:grid;grid-template-columns:1fr 1fr;gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card.wide{grid-column:1/-1}.card h2{margin:0 0 10px;font-size:12px;letter-spacing:.09em;text-transform:uppercase;color:var(--dim);font-weight:600}
.row{display:flex;align-items:center;gap:10px;padding:6px 0;border-top:1px solid var(--rowline)}.row:first-of-type{border-top:0}
.dot{width:10px;height:10px;border-radius:50%;background:var(--mute);flex:none}.dot.on{background:var(--ok);box-shadow:0 0 7px var(--ok)}.dot.off{background:var(--bad)}.dot.warn{background:var(--warn)}
.act{color:var(--warn);font-size:12.5px}.grp{color:var(--dim);font-size:12px;margin:10px 0 2px}.grp:first-child{margin-top:0}
.grow{flex:1;min-width:0}.dim{color:var(--dim)}.mono{font-family:ui-monospace,Menlo,monospace;font-size:12.5px}
button{background:var(--btnbg);color:var(--fg);border:1px solid var(--btnborder);border-radius:7px;padding:6px 12px;font:inherit;cursor:pointer}
button:hover{background:var(--btnhover);border-color:var(--acc)}button.pri{background:#0c6ea3;border-color:#1b8fcb;color:#fff}button.danger:hover{border-color:var(--bad);color:var(--bad)}
button.bedrock{background:#b45309;border-color:#f59e0b;color:#fff}button.bedrock:hover{background:#d97706;border-color:#fbbf24}
button.usb{background:#047857;border-color:#34d399;color:#fff}button.usb:hover{background:#059669;border-color:#6ee7b7}
button:disabled{opacity:.45;cursor:wait}
[hidden]{display:none!important}
.tabs{display:flex;gap:4px;padding:10px 22px 0;border-bottom:1px solid var(--line);background:var(--tabsbg)}
.tabs button{border-radius:8px 8px 0 0;border-bottom:none;background:transparent;color:var(--dim)}
.tabs button.on{background:var(--card);color:var(--fg);border-color:var(--acc)}
#tt{max-width:1150px;margin:0 auto;padding:18px}
.tbar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-bottom:10px}
.tbar .tt{display:flex;align-items:center;gap:2px}.tbar .tt button.on{background:#0c6ea3;border-color:#1b8fcb;color:#fff}
#tmenu{position:fixed;z-index:50;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:4px;box-shadow:0 6px 20px #000a}#tmenu button{display:block;width:100%;text-align:left;border:none;background:transparent;color:var(--bad)}#tmenu button:hover{background:var(--btnhover)}
details.menu{position:relative;margin-left:auto}
details.menu summary{list-style:none;cursor:pointer;display:flex;align-items:center;justify-content:center;width:30px;height:30px;border-radius:7px;background:var(--btnbg);border:1px solid var(--btnborder);font-size:15px}
details.menu summary::-webkit-details-marker{display:none}
details.menu[open] summary,details.menu summary:hover{border-color:var(--acc)}
.menu-pop{position:absolute;right:0;top:calc(100% + 6px);z-index:50;min-width:170px;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px;box-shadow:0 6px 20px #000a}
.menu-pop .btns button.pri{color:#fff}
.amenu{display:flex;flex-wrap:wrap;gap:6px}.amenu details{position:relative}
.amenu summary{list-style:none;cursor:pointer;user-select:none;background:var(--btnbg);border:1px solid var(--btnborder);border-radius:7px;padding:6px 12px}
.amenu summary::-webkit-details-marker{display:none}
.amenu summary::after{content:"";display:inline-block;margin-left:8px;vertical-align:2px;border:4px solid transparent;border-top:5px solid var(--dim);border-bottom:0}
.amenu summary:hover,.amenu details[open] summary{background:var(--btnhover);border-color:var(--acc)}
.amenu .pop{position:absolute;left:0;top:calc(100% + 4px);z-index:40;min-width:210px;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:4px;box-shadow:0 6px 20px #000a}
.amenu .pop.flip{left:auto;right:0}
.amenu .pop button{display:block;width:100%;text-align:left;border:0;border-radius:6px;background:transparent;color:var(--fg);padding:7px 10px}
.amenu .pop button:hover{background:var(--btnhover)}.amenu .pop button.danger{color:var(--bad)}
.amenu .pop button.pri{box-shadow:inset 3px 0 0 #1b8fcb}.amenu .pop button.bedrock{box-shadow:inset 3px 0 0 #f59e0b}.amenu .pop button.usb{box-shadow:inset 3px 0 0 #34d399}
.term{background:#000;border:1px solid var(--line);border-radius:8px;padding:6px;height:68vh;min-height:300px}
.term .xterm-viewport{background:#000!important}
.btns{display:flex;flex-wrap:wrap;gap:8px}
.sw{position:relative;width:38px;height:21px;background:var(--swoff);border-radius:21px;cursor:pointer;flex:none;transition:.15s}
.sw::after{content:"";position:absolute;top:2px;left:2px;width:17px;height:17px;border-radius:50%;background:#fff;transition:.15s}
.sw.on{background:var(--ok)}.sw.on::after{left:19px}
button.mini{padding:2px 8px;font-size:12px}.alarmset{width:310px;max-height:58vh;overflow-y:auto}.alarmset .row{padding:4px 0}
input{background:var(--inputbg);color:var(--fg);border:1px solid var(--line);border-radius:7px;padding:6px 9px;font:inherit;min-width:0}
.add{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px;padding-top:10px;border-top:1px solid var(--rowline)}
.tag{font-size:11px;padding:1px 7px;border-radius:9px;background:var(--btnbg);color:var(--dim)}.tag.ok{color:var(--ok)}.tag.bad{color:var(--bad)}
#con{background:var(--conbg);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:10px 12px;height:260px;overflow:auto;white-space:pre-wrap;font:12.5px/1.4 ui-monospace,Menlo,monospace}
.ch{display:flex;align-items:center;gap:10px;margin-bottom:8px}.ch h2{margin:0}.spin{display:none;color:var(--acc)}
a{color:var(--acc)}@media(max-width:800px){main{grid-template-columns:1fr}}
.gaugegrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(148px,1fr));gap:14px 6px}
.gauge{text-align:center}
.gauge svg{width:100%;overflow:visible;display:block}
.gauge .val{font-size:18px;font-weight:600;margin-top:2px;color:var(--fg)}
.gauge .lbl{font-size:12px;color:var(--dim);line-height:1.25}
.gauge .sub{font-size:11px;color:var(--dim);margin-top:2px}
.gauge .legend{font-size:11px;margin-top:3px;line-height:1.5}
#ck{max-width:1150px;margin:0 auto;padding:18px}
</style></head><body>
<header><div><h1>◆ Macotron</h1><div class="sub">The Mac's control panel · tablet hotspot · Edge · remote desktops · MiladSync · toolbox</div></div><div class="ts" id="ts"></div>
</header>
<div class="tabs"><button id="tab-c" class="on" onclick="tab('c')">Control</button><button id="tab-t" onclick="tab('t')">Terminals</button><button id="tab-k" onclick="tab('k')">Cockpit</button><button id="tab-p" onclick="tab('p')">Tablet</button><button id="tab-b" onclick="tab('b')">Bedrock</button><button id="tab-r" onclick="tab('r')" title="Reader: text to speech with a synced highlight (Milad.Mind.Mapper)">Reader</button><button id="tab-y" onclick="tab('y')">Clipboard</button><button id="tab-o" onclick="tab('o')" title="Offload: move old meetings media and archives to OneDrive and aponno, verified, then free the laptop">Offload</button><button id="tab-s" onclick="tab('s')">Settings</button></div>
<main id="mc">
<section class="card wide"><div class="ch"><h2 style="cursor:pointer" onclick="toggleMon()" title="Click to show every check, including the ones that are fine">Connections</h2><span class="dim" id="mwhen"></span><span class="dim" id="mlast"></span><span class="btns" style="margin-left:auto" id="mbtns"></span></div><div id="mon"><div class="dim">Starting the checks…</div></div></section>
<section class="card"><h2>Network</h2><div id="net"></div>
 <div class="add"><input id="pa" placeholder="phone address (or auto / scan)" size="26"><button onclick="act('phone',{addr:document.getElementById('pa').value})">Set</button><button onclick="act('phone',{addr:'scan'})">Scan network</button><button onclick="act('phone',{addr:'auto'})">Use gateway</button></div></section>
<section class="card"><h2>Services</h2><div id="svc"></div></section>
<section class="card wide"><div class="ch"><h2>Wireless ADB · phone management (no root)</h2><span class="dim" id="adb-label"></span><button style="margin-left:auto" onclick="adbOp('refresh')">Check now</button></div>
 <div class="dim">On the phone: Developer options → Wireless debugging. On its hotspot the gateway IP is filled in automatically; add the port shown on the phone. Pairing and connection use different ports. Device identity is saved per companion profile; SSH and the phone proxy are unchanged.</div>
 <div class="add"><input id="adb-pair" placeholder="pairing IPv4:port" aria-label="ADB pairing address" size="22"><input id="adb-code" type="password" inputmode="numeric" maxlength="6" autocomplete="off" placeholder="6-digit pairing code" aria-label="ADB pairing code" size="18"><button onclick="adbPair()">Pair</button></div>
 <div class="add"><input id="adb-address" placeholder="connection IPv4:port" aria-label="ADB connection address" size="24"><button onclick="adbConnect()">Connect / enable monitoring</button><button onclick="adbOp('off')">Stop ADB monitoring</button><button onclick="adbOp('disconnect')">Disconnect ADB</button></div>
 <div id="adb-status" class="dim" style="margin-top:10px">Loading ADB status...</div><div id="adb-info"></div>
 <div class="btns" id="adb-controls" style="margin-top:10px">
  <button data-adb="tailscale_open" onclick="adbOp('tailscale_open')">Open Tailscale</button><button data-adb="tailscale_on" onclick="adbOp('tailscale_on')">Tailscale on</button><button data-adb="tailscale_off" onclick="if(confirm('Disconnect Tailscale on the phone? Remote desktop/share traffic through it may stop.'))adbOp('tailscale_off')">Tailscale off</button>
  <button data-adb="wake" onclick="adbOp('wake')">Wake screen</button><button data-adb="sleep" onclick="adbOp('sleep')">Sleep screen</button><button data-adb="wifi_settings" onclick="adbOp('wifi_settings')">Wi-Fi settings</button><button data-adb="hotspot_settings" onclick="adbOp('hotspot_settings')">Hotspot settings</button>
  <button data-adb="scrcpy_start" onclick="adbOp('scrcpy_start')">Open scrcpy screen</button><button data-adb="scrcpy_stop" onclick="adbOp('scrcpy_stop')">Close scrcpy</button>
 </div>
 <div class="add"><span class="dim">Manual brightness (0–255; turns auto brightness off)</span><input id="adb-brightness" type="number" min="0" max="255" aria-label="Phone brightness" size="5"><button id="adb-brightness-set" onclick="adbBrightness()">Set brightness</button></div>
 <div id="adb-result" class="mono" role="status" style="margin-top:8px;white-space:pre-wrap"></div>
 <div class="dim" style="margin-top:8px">scrcpy opens its own keyboard/mouse screen window. Protected screens may be black. Root-only NAT, LUKS and raw disk controls remain unavailable without root.</div>
</section>
<section class="card wide"><div class="ch"><h2>Remote desktops &amp; drive shares</h2><span class="btns" style="margin-left:auto"><button class="pri" onclick="act('allon')" title="Open every tunnel that is down, mount the drive share and bring the NAT addresses up. Whatever already works is left alone, so a live remote desktop is not dropped">All on</button><button class="danger" onclick="if(confirm('Stop continuous monitoring, close every tunnel and unmount the drive share? Open remote desktops drop.'))act('alloff')" title="Stop continuous monitoring, unmount shares, then close tunnels. The NAT addresses stay up (the watchdogs keep them)">All off</button><button id="targetmon" onclick="targetMon()" aria-pressed="false" disabled title="Check availability every 30 seconds and enable missing connections. Turning this off leaves connections open">Continuous monitoring: off</button></span></div><div class="row"><span class="grow dim" id="targetmsg"></span><button id="targetlog" hidden onclick="poll(lastMon.targets.job,0)">Recovery log</button></div><div id="tg"></div>
 <div class="add"><input id="an" placeholder="name (e.g. helios)" size="14"><input id="ai" placeholder="Tailscale IP 100.x.y.z" size="18">
 <input id="ar" placeholder="RDP port 3389 (- = NAT only)" size="22"><input id="al" placeholder="local port (auto)" size="14"><input id="ax" placeholder="NAT .N (optional)" size="14" title="Virtual address N (1-254) in the tablet's hotspot subnet: every port of it is NATed to this target, and the tablet answers the fixed name <name>.tab.local for it"><button class="pri" onclick="addT()">Add target</button></div></section>
<section class="card wide"><h2>Actions</h2><nav class="amenu" id="amenu">
 <details><summary>Proxy</summary><div class="pop">
  <button class="pri" onclick="act('all')" title="The proxy on the phone, every remote desktop tunnel, then Edge">Do everything</button>
  <button onclick="act('start')">Start proxy</button><button class="danger" onclick="act('stop')">Stop proxy &amp; tunnels</button>
  <button onclick="act('exitip')">Exit IP check</button></div></details>
 <details><summary>Edge</summary><div class="pop">
  <button onclick="act('edge',{mode:'isolated'})">Separate profile</button><button onclick="act('edge',{mode:'default'})">Normal profile</button><button onclick="act('edge',{mode:'plain'})">No proxy</button></div></details>
 <details><summary>Tablet</summary><div class="pop">
  <button onclick="tab('p')" title="The tablet's control page (SSD unlock, Samba, meridian, power), in the Tablet tab">Control page</button>
  <button onclick="termRun('tablet')" title="A shell on the tablet (ssh tab), in the Terminals tab; exit it to come back here">Terminal</button>
  <button onclick="act('nat',{op:'on'})" title="Rebuild the virtual addresses (nat=N targets) on the tablet">NAT refresh</button><button onclick="act('nat',{op:'off'})" title="Remove the virtual addresses from the tablet">NAT off</button></div></details>
 <details><summary>Drive</summary><div class="pop">
  <button onclick="act('drivelogin')" title="Remount the drive as a named user; Finder asks for the password once and keeps it in the Keychain">Drive login…</button>
  <button class="usb" onclick="speed()" title="The tablet's SSD and microSD card on their own, the SSH link onto each, and the Finder mount">USB speed test…</button></div></details>
 <details><summary>Bedrock</summary><div class="pop">
  <button class="bedrock" onclick="termRun('bedrock')" title="Opens in the Terminals tab, where you answer its questions">Log in</button>
  <button onclick="tab('b')" title="Model consumption, in the Bedrock tab">Usage</button></div></details>
 <details><summary>Logs</summary><div class="pop">
  <button onclick="act('phonelog')">Phone log</button><button onclick="watchLog()">Watchdog log</button></div></details>
</nav></section>
<section class="card wide"><div class="ch"><h2>Output</h2><span class="spin" id="spin">running…</span><span class="dim" id="jt"></span><button style="margin-left:auto" onclick="con.textContent=''">Clear</button></div><div id="con">Ready.</div></section>
</main>
<div id="ck" hidden>
<section class="card wide"><h2>Cockpit</h2><div id="gauges" class="gaugegrid"></div></section>
</div>
<div id="tp" hidden>
<div class="btns" style="margin-bottom:8px;align-items:center"><button onclick="tabletLoad()">Reload</button><button onclick="termRun('tablet')" title="A shell on the companion device (ssh tab), in the Terminals tab; exit it to come back here">Terminal</button><span class="dim" style="margin-left:12px">Portal device</span><select id="devsel" title="The device Macotron and phone-proxy.sh talk to. Only one is active at a time."></select><button onclick="deviceUse()" title="Make the selected device the companion: rewrites the ssh 'tab' entry, then restarts Macotron">Switch</button><span id="tpmsg" class="dim"></span><span id="devnote" class="dim" style="margin-left:8px"></span></div>
<iframe id="tpf" title="The tablet's control page" style="width:100%;border:0;height:calc(100vh - 150px);min-height:520px"></iframe>
</div>
<div id="tt" hidden>
 <div class="tbar" id="tbar"></div>
 <div id="tbody" class="tbody"></div>
 <div class="dim" style="margin-top:10px;font-size:12px">Real terminals: your login shell, every key goes straight to it (Tab completion, arrows, Ctrl+C, vim, top). They keep running if you close the browser tab and come back with their screen when you return. Right-click a terminal's button to close it.</div>
</div>
<div id="bd" hidden>
<div class="btns" style="margin-bottom:8px"><a id="bdopen" href="#" target="_blank" rel="noopener" class="dim">Open in own window</a></div>
<iframe id="bdf" title="Amazon Bedrock model consumption" style="width:100%;border:0;height:calc(100vh - 150px);min-height:520px"></iframe>
</div>
<div id="rd" hidden>
<iframe id="rdf" title="Reader" allow="autoplay" style="width:100%;border:0;display:block;height:calc(100vh - 104px);min-height:520px"></iframe>
</div>
<div id="of" hidden>
<iframe id="off" title="Offload" style="width:100%;border:0;display:block;height:calc(100vh - 104px);min-height:520px"></iframe>
</div>
<div id="cb" hidden>
<section class="card wide"><h2>Clipboard list</h2>
 <div class="row"><div class="dot" id="cb-dot"></div><div class="grow" id="cb-msg">Loading...</div></div>
 <div class="btns" style="margin:8px 0;align-items:center"><button class="pri" onclick="clipOp('save')" title="Saves the text that is on the Mac clipboard now (the shortcut also saves images)">Save the clipboard now</button><button class="danger" onclick="if(confirm('Remove every clip that is not pinned?'))clipOp('clear')">Clear unpinned</button><input id="cb-q" placeholder="search" size="24" oninput="clipRenderLast()" style="margin-left:auto"></div>
 <div class="row"><div class="sw" id="cb-on" onclick="clipSet({on:!document.getElementById('cb-on').classList.contains('on')})"></div><div class="grow">Shortcut Ctrl+Option+ the chosen key saves the clipboard from any app (needs no macOS permission)</div><select id="cb-key" onchange="clipSet({keycode:+this.value})"><option value="24">= key</option><option value="10">section sign key (ISO keyboard)</option><option value="50">grave accent key (US keyboard)</option></select></div>
</section>
<div id="cb-list" style="margin-top:12px"></div>
</div>
<div id="ss" hidden><main style="grid-template-columns:1fr">
<section class="card wide"><h2>Macotron</h2>
<div class="grp" style="margin-top:0">Theme</div>
<div class="btns"><button id="theme-dark" onclick="setTheme('dark')">Dark</button><button id="theme-light" onclick="setTheme('light')">Light</button></div>
<div class="btns"><button onclick="restartApp()" title="Starts Macotron's server again in place, the way it runs now, and reloads this page: picks up a changed phone-proxy-web.py. Open terminals end">Restart Macotron</button></div>
<div class="grp">Alarms (macOS notifications from the Connections checks)</div>
<div class="alarmset" id="alarmset"><div class="dim">Starting the checks...</div></div>
</section>
<section class="card wide"><h2>Tablet control: the Mac's keyboard and mouse on the tablet</h2>
 <div class="dim" style="margin-bottom:8px">Like Input Director: push the Mac's cursor off the chosen screen edge and the Mac's mouse and keyboard drive the tablet until you move its pointer back past the opposite edge, or press Ctrl+Option+Cmd+Esc. Nothing is taken over unless the tablet answers.</div>
 <div class="row"><div class="dot" id="tin-dot"></div><div class="grow" id="tin-msg">Loading...</div></div>
 <div class="btns" style="margin:8px 0"><button class="pri" id="tin-start" onclick="tabinOp('start')">Start</button><button id="tin-stop" onclick="tabinOp('stop')">Stop</button><button onclick="tabinOp('stop',{daemon:true})" title="Also stop the input daemon on the tablet">Stop and shut the tablet side</button></div>
 <div class="row"><div class="grow">What takes control to the tablet: the whole edge below, or just a corner (a right-hand corner puts the tablet on the right, a left-hand one on the left); applies at once</div><select id="tin-trig" onchange="tabinSave()"><option value="edge">The whole edge</option><option value="shortcut">Only a shortcut (no edge or corner)</option><option value="top-right">Top-right corner</option><option value="bottom-right">Bottom-right corner</option><option value="top-left">Top-left corner</option><option value="bottom-left">Bottom-left corner</option></select></div>
 <div class="row"><div class="grow">The shortcut for "Only a shortcut": Ctrl+Option plus this key switches to the tablet and back (F1 and F2 pressed together also bring control back from the tablet; two plain keys cannot start it without a macOS permission)</div><select id="tin-skey" onchange="tabinSave()"><option value="17">T</option><option value="122">F1</option><option value="120">F2</option><option value="96">F5</option></select></div>
 <div class="row"><div class="grow">Which monitor carries the edge or corner trigger: all monitors together (the outer edge of the whole arrangement), or one monitor's own edge or corner; applies at once</div><select id="tin-scr" onchange="tabinSave()"><option value="0">All monitors together</option></select></div>
 <div class="row"><div class="grow">Edge of the Mac's screens that leads to the tablet (used when the whole edge is the trigger)</div><select id="tin-edge" onchange="tabinSave()"><option>right</option><option>left</option><option>top</option><option>bottom</option></select></div>
 <div class="row"><div class="grow">How control comes back to the Mac: the tablet pointer past the far edge, or pushed into a corner of the tablet's screen (a big corner zone forgives drift in where the helper thinks the pointer is); applies at once</div><select id="tin-rtrig" onchange="tabinSave()"><option value="edge">Past the far edge</option><option value="top-left">Tablet top-left corner</option><option value="bottom-left">Tablet bottom-left corner</option><option value="top-right">Tablet top-right corner</option><option value="bottom-right">Tablet bottom-right corner</option></select><input id="tin-rsize" size="5" onchange="tabinSave()" title="tablet pixels" style="margin-left:6px"> px</div>
 <div class="row"><div class="grow">Size of a corner zone (screen points, 4 to 300)</div><input id="tin-csize" size="5" onchange="tabinSave()"></div>
 <div class="row"><div class="sw" id="tin-auto" onclick="tabinToggle('auto_size')"></div><div class="grow">Follow the tablet's screen size and rotation (portrait or landscape) by itself</div></div>
 <div class="row"><div class="grow">Tablet screen as it is held now (pixels, wide x high): used to know when the pointer has left it; set by itself when the switch above is on</div><input id="tin-tw" size="6" onchange="tabinSave()"> x <input id="tin-th" size="6" onchange="tabinSave()"></div>
 <div class="row"><div class="grow">Pointer speed on the tablet (1 = same as on the Mac); applies at once</div><input id="tin-speed" type="range" min="0.2" max="5" step="0.1" style="width:200px" oninput="tabinSpeedShow();tabinSaveSoon()"><span class="mono" id="tin-speedv" style="width:36px;text-align:right"></span></div>
 <div class="row"><div class="grow">Delay before control moves to the tablet, and back to the Mac (seconds, 0 = none); applies at once</div><input id="tin-delay" size="5" onchange="tabinSave()"></div>
 <div class="row"><div class="grow">Dim the Mac screen while the tablet has control (0 = not at all); applies at once</div><input id="tin-dim" type="range" min="0" max="0.6" step="0.02" style="width:200px" oninput="tabinDimShow();tabinSaveSoon()"><span class="mono" id="tin-dimv" style="width:44px;text-align:right"></span></div>
 <div class="row"><div class="sw" id="tin-mtg" onclick="tabinToggle('meeting_on_control')"></div><div class="grow">Switch the tablet to its Meeting profile while it has control, and back this many minutes after the last activity:</div><input id="tin-idle" size="4" onchange="tabinSave()" title="15 to 1440, in steps of 15"> min</div>
 <div class="dim" id="tin-mtgnote" style="margin:0 0 4px 46px;font-size:12px"></div>
 <div class="row"><div class="sw" id="tin-cmd" onclick="tabinToggle('cmd_as_ctrl')"></div><div class="grow">Cmd works as Ctrl on the tablet (Cmd+C copies)</div></div>
 <div class="row"><div class="sw" id="tin-paste" onclick="tabinToggle('paste_typing')"></div><div class="grow">Cmd+V types the Mac's clipboard text on the tablet (the tablet's own clipboard cannot be reached; US keyboard characters only)</div></div>
 <div class="row"><div class="sw" id="tin-esc" onclick="tabinToggle('esc_returns')"></div><div class="grow">Esc gives control back to the Mac (then Esc no longer reaches the tablet; Ctrl+Option+Cmd+Esc always works)</div></div>
 <div class="row"><div class="sw" id="tin-inv" onclick="tabinToggle('invert_scroll')"></div><div class="grow">Reverse the scroll direction</div></div>
 <div class="dim" style="margin-top:8px;font-size:12px">Needs no macOS permission: while the tablet has control, a faint blue tint covers that Mac screen and the helper takes the keyboard and mouse as its own. macOS-level shortcuts (Cmd+Tab, Cmd+Space) stay on the Mac. Speed, delay, size, Cmd and scroll apply at once; a different edge applies the next time you press Start.</div>
</section>

<section class="card wide"><h2>Tablet screen window</h2>
 <div class="dim" style="margin-bottom:8px">Shows the tablet's screen live in a window of its own. While you are in that window, the Mac's mouse and keyboard drive the tablet: a click is a tap, a drag is a drag, the wheel swipes, right-click is Back, and the keys are real keys (Cmd works as Ctrl). Closing the window turns the stream off. The picture can go black on screens Android protects, such as some password screens.</div>
 <div class="row"><div class="sw" id="tsc-on" onclick="tscToggle()"></div><div class="grow" id="tsc-msg">Off.</div></div>
 <div class="row"><div class="grow">Size of the picture compared with the tablet's real screen; a change restarts the window if it is open</div><select id="tsc-scale" onchange="tscSave()"><option value="0.3">30 %</option><option value="0.4">40 %</option><option value="0.5">50 %</option><option value="0.65">65 %</option><option value="0.8">80 %</option><option value="1">100 %</option></select></div>
 <div class="row"><div class="grow">Picture quality (bit rate); higher is sharper and uses more Wi-Fi</div><select id="tsc-rate" onchange="tscSave()"><option value="3">Low (3 Mbit/s)</option><option value="6">Normal (6 Mbit/s)</option><option value="12">High (12 Mbit/s)</option><option value="20">Very high (20 Mbit/s)</option></select></div>
</section>

<section class="card wide"><h2>All ports through the tablet (SOCKS proxy)</h2>
 <div class="dim" style="margin-bottom:8px">The localhost ports of the target list (8033, 8035) carry one fixed remote port each. This proxy carries any port of any of your machines: one local port that every program with a SOCKS5 proxy setting can use, on any Wi-Fi, with no admin rights. Put the machine's Tailscale address (below) as the host.</div>
 <div class="row"><div class="sw" id="sk-on" onclick="socksToggle()"></div><div class="grow" id="sk-msg">Off.</div><span class="dim">port</span><input id="sk-port" size="6" onchange="socksPort()"></div>
 <div class="dim" id="sk-peers" style="margin-top:8px;font-size:12.5px"></div>
 <pre class="mono" id="sk-use" style="margin:8px 0 0;font-size:12px;white-space:pre-wrap;color:var(--dim)"></pre>
</section>
</main></div>
<script>
const T=new URLSearchParams(location.search).get('t')||sessionStorage.t||'';sessionStorage.t=T;
// <!-- >>> bedrock-tab glue (patch_macotron_tab.py) >>> -->
let bdLoaded=0;
const con=document.getElementById('con');let busy=0;
const api=(p,o)=>fetch(p,{...o,headers:{'X-Token':T,'Content-Type':'application/json'}}).then(r=>r.json());
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const dot=on=>`<span class="dot ${on?'on':'off'}"></span>`;
let lastADB=null,adbBusy=false,adbGateway='';
function adbAddressDefaults(saved=''){const prefix=adbGateway?adbGateway+':':'';
 for(const [id,value] of [['adb-address',saved||prefix],['adb-pair',prefix]]){const input=document.getElementById(id);
  if(document.activeElement!==input&&!input.dataset.edited)input.value=value}}
function adbHotspotDefaults(s){adbGateway=s.net&&s.mode==='hotspot'?s.phone:'';
 adbAddressDefaults(lastADB&&lastADB.endpoint||'')}
function renderADB(s){lastADB=s;document.getElementById('adb-label').textContent=s.label||'';
 adbAddressDefaults(s.endpoint);
 document.getElementById('adb-status').textContent=(s.message||'')+
  (!s.adb_installed&&!s.unavailable?' Install adb: brew install --cask android-platform-tools.':'')+
  (!s.scrcpy_installed&&!s.unavailable?' For screen control: brew install scrcpy.':'')+
  (s.checked?' Last check: '+new Date(s.checked*1000).toLocaleTimeString():'');
 const b=s.battery,t=s.tailscale,c=s.capabilities||{};
 document.getElementById('adb-info').innerHTML=(b?`<div class="row"><div class="grow"><b>Battery</b> ${esc(b.percent)}% · ${esc(b.status)}${b.temperature_c!=null?' · '+esc(b.temperature_c)+' °C':''}${b.powered!=null?' · external power '+(b.powered?'connected':'disconnected'):''}</div></div>`:'')+
  (t?`<div class="row"><div class="grow"><b>Tailscale</b> ${t.installed===true?'installed':t.installed===false?'not installed':'installation unknown'} · app ${t.process_running===true?'running':t.process_running===false?'not running':'process status unknown'} · VPN ${t.vpn_active===true?'active (Android owner matches Tailscale)':t.vpn_active===false?'not active':'state unknown'}<div class="dim">VPN active does not prove peer reachability. Login or VPN approval may be needed on the phone.</div></div></div>`:'')+
  Object.entries(s.errors||{}).map(([k,v])=>`<div class="row"><span class="tag bad">${esc(k)}</span><span class="dim">${esc(v)}</span></div>`).join('')+
  (s.scrcpy_error?`<div class="row"><span class="tag bad">scrcpy</span><span class="dim">${esc(s.scrcpy_error)}</span></div>`:'');
 for(const button of document.querySelectorAll('#adb-controls button')){const op=button.dataset.adb;button.disabled=adbBusy||!s.connected;
  if(op=='tailscale_on'||op=='tailscale_off')button.disabled=button.disabled||!c[op];
  if(op=='tailscale_open')button.disabled=button.disabled||!(t&&t.installed);
  if(op=='scrcpy_start')button.disabled=button.disabled||!s.scrcpy_installed||s.scrcpy_running;
  if(op=='scrcpy_stop')button.disabled=adbBusy||!s.scrcpy_running}
 document.getElementById('adb-brightness-set').disabled=adbBusy||!s.connected;
 const brightness=document.getElementById('adb-brightness');if(document.activeElement!==brightness&&s.brightness!=null)brightness.value=s.brightness}
async function adbLoad(){if(adbBusy)return;try{const s=await api('/api/adb');renderADB(s)}catch(e){document.getElementById('adb-status').textContent='Macotron not answering; phone state unknown.';document.querySelectorAll('#adb-controls button,#adb-brightness-set').forEach(b=>b.disabled=true)}}
async function adbOp(op,extra={}){if(adbBusy)return;
 if(!lastADB||lastADB.unavailable){document.getElementById('adb-result').textContent=lastADB&&lastADB.message||'Wait for ADB status to load.';return}
 adbBusy=true;renderADB(lastADB);
 try{const r=await api('/api/adb',{method:'POST',body:JSON.stringify({op,device:lastADB.device,...extra})});
  document.getElementById('adb-result').textContent=r.error||r.message||'Done.';
  if(r.state)renderADB(r.state);
  return r;
 }catch(e){document.getElementById('adb-result').textContent='ADB request failed: '+e}
 finally{adbBusy=false;if(lastADB)renderADB(lastADB);setTimeout(adbLoad,1000)}}
async function adbPair(){const code=document.getElementById('adb-code'),value=code.value;code.value='';
 await adbOp('pair',{pair_endpoint:document.getElementById('adb-pair').value,code:value})}
async function adbConnect(){const endpoint=document.getElementById('adb-address').value;
 const r=await adbOp('connect',{endpoint});if(r&&r.replace_required&&confirm(r.message))await adbOp('connect',{endpoint,replace:true})}
function adbBrightness(){const input=document.getElementById('adb-brightness'),value=Number(input.value);
 if(!input.value.trim()||!Number.isInteger(value)||value<0||value>255){document.getElementById('adb-result').textContent='Enter a brightness from 0 to 255.';return}
 return adbOp('brightness',{value})}
for(const id of ['adb-address','adb-pair'])document.getElementById(id).addEventListener('input',e=>{e.target.dataset.edited='1'});
adbLoad();setInterval(adbLoad,5000);
function svc(label,on,detail,btns){return `<div class="row">${dot(on)}<div class="grow"><b>${label}</b> <span class="dim">${detail}</span></div>${btns}</div>`}
async function refresh(){let s;try{s=await api('/api/status')}catch(e){document.getElementById('ts').textContent='disconnected';return}
 document.getElementById('ts').textContent='updated '+s.ts;
 adbHotspotDefaults(s);
 document.getElementById('net').innerHTML=s.net?
  `<div class="row"><div class="grow">Interface <b>${esc(s.iface)}</b></div></div><div class="row"><div class="grow">Mac <b class="mono">${s.mac}</b> → Phone <b class="mono">${s.phone}</b></div></div>
   <div class="row"><div class="grow">Link <b>${s.mode=='hotspot'?'hotspot':'same network'}</b> <span class="dim">${s.mode=='hotspot'?'(phone is the gateway)':'(phone has its own address)'}</span></div></div>
   <div class="row">${dot(s.sshd)}<div class="grow">Phone sshd <span class="dim mono">:8022</span></div></div><div class="row">${dot(s.proxy)}<div class="grow">Proxy <span class="dim mono">:8080</span></div></div>`
  :`<div class="row">${dot(false)}<div class="grow">Phone not reachable. Join its hotspot, or the network it is on, then set its address.</div></div>`;
 const pa=document.getElementById('pa');if(pa&&document.activeElement!==pa&&!pa.value&&s.mode=='lan')pa.value=s.phone;
 document.getElementById('svc').innerHTML=
  svc('MiladSync server',s.app,s.app?`<a href="http://localhost:5678" target="_blank">localhost:5678</a>`:'down',`<button onclick="act('server',{op:'${s.app?'stop':'start'}'})">${s.app?'Stop':'Start'}</button><button onclick="act('server',{op:'restart'})">Restart</button>`)+
  svc('Toolbox manager',s.toolbox,s.toolbox?(s.tburl?`<a href="${esc(s.tburl)}" target="_blank">open (with token)</a>`:'running'):'down',`<button onclick="act('toolbox',{op:'${s.toolbox?'stop':'start'}'})">${s.toolbox?'Stop':'Start'}</button><button onclick="act('toolbox',{op:'restart'})">Restart</button>`)+
  svc('Watchdog',s.watch,s.watch?'restores proxy and tunnels every 15s':'off',`<button onclick="act('watch',{state:'${s.watch?'off':'on'}'})">Turn ${s.watch?'off':'on'}</button>`)+
  svc('Amazon Bedrock',s.bedrock,esc(s.bedrock_detail),s.bedrock?`<button onclick="termRun('bedrock')">Log in again</button>`:`<button class="bedrock" onclick="termRun('bedrock')">Log in</button>`)+
  (s.awsauto?svc('AWS auto-login',s.awsauto.enabled,s.awsauto.enabled?(s.awsauto.active?`on: opens the login pages ${Math.round(s.awsauto.lead/60)} min before expiry (${Math.max(0,Math.round(s.awsauto.remaining/60))} min left)`:'on, but the AWS session is not active, so nothing will open'):'off',`<button onclick="api('/api/awsauto',{method:'POST',body:JSON.stringify({enabled:${!s.awsauto.enabled}})}).then(refresh)">Turn ${s.awsauto.enabled?'off':'on'}</button>`):'');
 document.getElementById('tg').innerHTML=s.targets.map(t=>{
  const dest=t.serve?'drive share (SMB)':t.fwd?`${t.rip}:${t.rport}`:`${t.rip} (NAT only, every port)`;
  const nt=t.nat?(t.vip?`<span class="tag ${t.natup?'ok':'bad'}" title="Virtual address on the tablet: every port of it reaches ${esc(t.rip)} (no tunnel, no Tailscale). The name is answered by the tablet over mDNS and stays the same when the hotspot subnet changes; the colour reflects the address">NAT ${esc(t.mname)} · ${esc(t.vip)}</span>`:`<span class="tag" title="Name ${esc(t.mname)} and address .${esc(t.nat)} in the hotspot subnet: only while this Mac is on the hotspot">NAT ${esc(t.mname)} · .${esc(t.nat)}</span>`):'';
  const sw=t.tunnel?`<div class="sw ${t.up?'on':''}" title="toggle tunnel" onclick="act('${t.up?'untunnel':'tunnel'}',{name:'${t.name}'})"></div>`:t.fwd?`<span class="tag">phone-only</span>`:'';
  const lk=t.serve&&t.up&&t.share===false?`<span class="tag bad" title="The tunnel is up but nothing answers on the tablet: the SSD is locked (it locks on every tablet reboot) and the SD buffer is not standing in. Unlock it on the tablet's control page (Tablet control page button), or in Terminal: ./phone-proxy.sh drive-up">drive locked</span>`:'';
  const mt=lk+(t.serve?(t.mounted?`<span class="tag ok">mounted ${esc(t.mounted)}</span><button onclick="act('unmount',{name:'${t.name}'})">Unmount</button>`:`<button onclick="act('mount',{name:'${t.name}'})">Mount</button>`):'');
  return `<div class="row">${t.tunnel?dot(t.up):'<span class="dot"></span>'}<div class="grow"><b>${esc(t.name)}</b> <span class="mono dim">${t.fwd?`localhost:${t.lport} → `:''}${esc(dest)}</span></div>${nt}${mt}${sw}<button class="danger" onclick="rm('${t.name}')">✕</button></div>`}).join('')||'<div class="dim">No targets.</div>'}
function show(t){document.getElementById('jt').textContent=t}
async function act(a,body={}){const r=await api('/api/'+a,{method:'POST',body:JSON.stringify(body)});
 if(r.error){con.textContent+='\n✗ '+r.error;return}
 con.textContent='';poll(r.id,0)}
async function poll(id,since){busy=1;document.getElementById('spin').style.display='inline';
 const r=await api(`/api/job?id=${id}&since=${since}`);show(r.title||'');
 if(r.lines.length){con.textContent+=r.lines.join('\n')+'\n';con.scrollTop=con.scrollHeight}
 if(r.done){busy=0;document.getElementById('spin').style.display='none';con.textContent+=`— finished (exit ${r.rc})\n`;con.scrollTop=con.scrollHeight;refresh()}
 else setTimeout(()=>poll(id,r.n),600)}
function addT(){const g=i=>document.getElementById(i).value.trim();
 act('add',{name:g('an'),ip:g('ai'),rport:g('ar'),lport:g('al'),nat:g('ax')});['an','ai','ar','al','ax'].forEach(i=>document.getElementById(i).value='')}
function rm(n){if(confirm('Remove target '+n+'?'))act('remove',{name:n})}
function speed(){const mb=prompt('Test size in MB (8-1024)','64');if(mb)act('speedtest',{mb:+mb})}
// Actions menu: one category open at a time (pointing at another one while a menu is open opens
// that one); an item, a click elsewhere or Escape closes it; a menu that would leave the window
// opens to the left of its category instead
const AM=document.getElementById('amenu');
function amClose(keep){for(const d of AM.querySelectorAll('details'))if(d!==keep)d.open=false}
AM.addEventListener('toggle',e=>{const d=e.target;if(!d.open)return;amClose(d);
 const p=d.querySelector('.pop');p.classList.remove('flip');if(p.getBoundingClientRect().right>innerWidth-8)p.classList.add('flip')},true);
AM.addEventListener('click',e=>{if(e.target.closest('.pop button'))amClose()});
// pointing switches only when the pointer itself moved: cards redrawn every 5 s or a scroll moving
// a category under a still pointer must not
let amXY='';document.addEventListener('mousemove',e=>{amXY=e.clientX+','+e.clientY},true);
AM.addEventListener('mouseover',e=>{const xy=e.clientX+','+e.clientY,moved=xy!==amXY;amXY=xy;
 const d=e.target.closest('details');if(moved&&d&&!d.open&&AM.querySelector('details[open]'))d.open=true});
document.addEventListener('click',e=>{if(!AM.contains(e.target))amClose()});
document.addEventListener('keydown',e=>{if(e.key==='Escape')amClose()});
async function watchLog(){const r=await api('/api/watchlog');con.textContent=r.lines.join('\n');show('Watchdog log')}

function tab(w){document.getElementById('mc').hidden=w!='c';document.getElementById('tt').hidden=w!='t';document.getElementById('ck').hidden=w!='k';document.getElementById('tp').hidden=w!='p';document.getElementById('bd').hidden=w!='b';document.getElementById('rd').hidden=w!='r';document.getElementById('ss').hidden=w!='s';document.getElementById('cb').hidden=w!='y';document.getElementById('of').hidden=w!='o';
 document.getElementById('tab-c').className=w=='c'?'on':'';document.getElementById('tab-t').className=w=='t'?'on':'';document.getElementById('tab-k').className=w=='k'?'on':'';document.getElementById('tab-p').className=w=='p'?'on':'';document.getElementById('tab-b').className=w=='b'?'on':'';document.getElementById('tab-r').className=w=='r'?'on':'';document.getElementById('tab-s').className=w=='s'?'on':'';document.getElementById('tab-y').className=w=='y'?'on':'';document.getElementById('tab-o').className=w=='o'?'on':'';
 if(w=='r'){const f=document.getElementById('rdf');if(!f.src)f.src='/reader?t='+encodeURIComponent(T)+'&embed=1';else if(f.contentWindow)f.contentWindow.postMessage({type:'reader-focus'},'*')}
 if(w=='o'){const f=document.getElementById('off');if(!f.src)f.src='/offload?t='+encodeURIComponent(T)+'&embed=1&theme='+(document.documentElement.getAttribute('data-theme')||'dark')}
 if(w=='s'){renderAlarmSet(lastMon);tabinLoad()}if(w=='y')clipLoad();if(w=='t')termInit();if(w=='k'&&lastMon)renderGauges(lastMon);if(w=='p'&&!tpLoaded){tpLoaded=1;tabletLoad()}
 if(w=='b'&&!bdLoaded){bdLoaded=1;const th=document.documentElement.getAttribute('data-theme')||'dark';document.getElementById('bdf').src='/bedrock?t='+encodeURIComponent(T)+'&embed=1&theme='+th;document.getElementById('bdopen').href='/bedrock?t='+encodeURIComponent(T);}}
// Tablet tab: tabctl's own page from the tablet, framed; Macotron fetches its URL (token inside)
let tpLoaded=0;
async function deviceLoad(){let r;try{r=await api('/api/device')}catch(e){return}
 const s=document.getElementById('devsel');if(!s)return;
 s.innerHTML=Object.entries(r.devices).map(([n,v])=>'<option value="'+n+'"'+(n===r.active?' selected':'')+'>'+(v.label||n)+'</option>').join('');
 s.dataset.active=r.active;const a=r.devices[r.active]||{};
 document.getElementById('devnote').textContent=a.root===false?'Not rooted: no keyboard/mouse sharing, screen window, power profile, NAT names or Tablotron watch on this device.':''}
async function deviceUse(){const s=document.getElementById('devsel'),n=s.value;
 if(n===s.dataset.active)return alert('That device is already the companion.');
 if(!confirm('Make "'+s.options[s.selectedIndex].text+'" the portal device? The ssh entry changes and Macotron restarts (open terminals end).'))return;
 let r;try{r=await api('/api/device',{method:'POST',body:JSON.stringify({op:'use',name:n})})}catch(e){return alert('Macotron did not answer: '+e)}
 if(r.error)return alert(r.error);
 restartApp(true)}
async function tabletLoad(){deviceLoad();const m=document.getElementById('tpmsg');m.textContent="Opening the tablet's control page through its SSH tunnel...";
 let r;try{r=await api('/api/tabctl')}catch(e){r={error:'Macotron not answering'}}
 if(r.error){m.textContent=r.error;return}
 m.textContent="The tablet's own page, through the SSH tunnel. If it stays blank, its tabctl predates framing: ./phone-proxy.sh tabctl install";
 document.getElementById('tpf').src=r.url}

// Cockpit: analog gauges. rot(v): -90deg at min (needle left) to +90deg at max (needle right), 0deg (up) at the midpoint.
const num=v=>(v===undefined||v===null||v==='')?null:+v;
const human=k=>{k=+k;if(!k&&k!==0)return null;const g=k/1048576;return(g<800?g.toFixed(1)+' GiB':(k/1073741824).toFixed(2)+' TiB')};
function zoneLegend(o){
 let from=o.min,out=[];
 for(let i=0;i<o.zones.length;i++){const z=o.zones[i],first=i===0,last=i===o.zones.length-1;
  const txt=first?`&lt;${Math.round(z.to)}`:last?`\u2265${Math.round(from)}`:`${Math.round(from)} to ${Math.round(z.to)}`;
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
function renderGauges(m){
 const ti=m.tablet_info||{},mi=m.info||{};
 const g=[
  {min:0,max:100,value:num(mi.mac_disk_used_pct),label:'Mac disk used',unit:'%',
    sub:mi.mac_disk_total?`${(mi.mac_disk_used/1073741824).toFixed(1)} GiB of ${(mi.mac_disk_total/1073741824).toFixed(1)} GiB`:null,
    zones:[{to:80,c:'ok'},{to:92,c:'warn'},{to:100,c:'bad'}]},
  {min:0,max:70,value:num(ti.ssd_temp_c),label:'Tablet SSD temperature',unit:' \u00b0C',zones:[{to:55,c:'ok'},{to:65,c:'warn'},{to:70,c:'bad'}]},
  {min:0,max:100,value:num(ti.ssd_used_pct),label:'Tablet SSD used',unit:'%',
    sub:ti.ssd_size_k?`${human(ti.ssd_used_k)} of ${human(ti.ssd_size_k)}`:null,zones:[{to:80,c:'ok'},{to:92,c:'warn'},{to:100,c:'bad'}]},
  {min:0,max:100,value:num(ti.int_used_pct),label:'Tablet storage used',unit:'%',
    sub:ti.int_size_k?`${human(ti.int_used_k)} of ${human(ti.int_size_k)}`:null,zones:[{to:80,c:'ok'},{to:92,c:'warn'},{to:100,c:'bad'}]},
  {min:0,max:100,value:ti.sd_present?num(ti.sd_used_pct):null,label:'Tablet microSD used',unit:'%',
    sub:ti.sd_present?`${human(ti.sd_used_k)} of ${human(ti.sd_size_k)}`:null,zones:[{to:80,c:'ok'},{to:92,c:'warn'},{to:100,c:'bad'}]},
  {min:0,max:100,value:num(ti.bat_level),label:'Tablet battery',unit:'%',zones:[{to:15,c:'bad'},{to:30,c:'warn'},{to:100,c:'ok'}]},
  {min:0,max:50,value:num(ti.bat_temp_c),label:'Tablet battery temperature',unit:' \u00b0C',zones:[{to:45,c:'ok'},{to:50,c:'bad'}]},
  {min:-90,max:-30,value:num(ti.sta_rssi),label:'Tablet Wi-Fi signal',unit:' dBm',sub:ti.ssid?esc(ti.ssid):null,zones:[{to:-80,c:'bad'},{to:-67,c:'warn'},{to:-30,c:'ok'}]},
  {min:0,max:300,value:num(ti.internet_ms),label:'Internet latency',unit:' ms',zones:[{to:80,c:'ok'},{to:200,c:'warn'},{to:300,c:'bad'}]},
 ];
 document.getElementById('gauges').innerHTML=g.map(gaugeSVG).join('');
}
/* ---- terminals (xterm.js, keystroke level) ---- */
const TM={};let TA=null,tinit=0,xl=0,trun='',tfrom='';
function loadX(){if(xl)return xl;
 document.getElementById('xcss').href='/vendor/xterm.css?t='+encodeURIComponent(T);
 const ld=src=>new Promise((ok,no)=>{const e=document.createElement('script');e.src=src+'?t='+encodeURIComponent(T);e.onload=ok;e.onerror=()=>no(new Error('could not load '+src));document.head.appendChild(e)});
 return xl=ld('/vendor/xterm.js').then(()=>ld('/vendor/addon-fit.js'))}
// a terminal that opens on one of the server's TERM_RUN commands (Bedrock login asks questions);
// it notes the tab its button was on, for termDone to go back to
function termRun(run){trun=run;const o=document.querySelector('.tabs .on');tfrom=o?o.id.slice(4):'';tab('t')}
async function termInit(){
 try{await loadX()}catch(e){document.getElementById('tbody').textContent=String(e);xl=0;trun=tfrom='';return}
 const run=trun,from=tfrom;trun=tfrom='';
 if(tinit){termBar();if(TA)termFit(TA);if(run)await termNew(run,from);return}tinit=1;
 const r=await api('/api/term/list');for(const t of r.terms)termAdd(t.id,t.num,t.alive,t.title,t.run);
 if(run)await termNew(run,from);else if(r.terms.length)termShow(r.terms[0].id);else await termNew();
 window.addEventListener('resize',()=>{if(TA)termFit(TA)})}
function termAdd(id,num,alive,title,run,from){
 const el=document.createElement('div');el.className='term';el.style.display='none';document.getElementById('tbody').appendChild(el);
 const term=new Terminal({cursorBlink:true,fontSize:13,fontFamily:'ui-monospace,Menlo,monospace',scrollback:5000,allowProposedApi:true,theme:{background:'#000000'}});
 const fit=new FitAddon.FitAddon();term.loadAddon(fit);term.open(el);
 const t={id,num,alive,title,run,from,since:0,term,fit,el,q:Promise.resolve()};TM[id]=t;
 term.onData(d=>{t.q=t.q.then(()=>api('/api/term/in',{method:'POST',body:JSON.stringify({id,data:d})})).catch(()=>{})});
 term.onResize(({cols,rows})=>{api('/api/term/resize',{method:'POST',body:JSON.stringify({id,cols,rows})}).catch(()=>{})});
 termStream(t)}
async function termStream(t){while(TM[t.id]){let r;
  try{r=await api(`/api/term/out?id=${t.id}&since=${t.since}`)}catch(e){await new Promise(k=>setTimeout(k,1500));continue}
  if(!TM[t.id])return;if(r.error)return;
  if(r.reset)t.term.reset();
  if(r.data){const b=atob(r.data),u=new Uint8Array(b.length);for(let i=0;i<b.length;i++)u[i]=b.charCodeAt(i);t.term.write(u)}
  t.since=r.n;if(t.alive!==r.alive){t.alive=r.alive;termBar()}
  if(!r.alive){termDone(t,r.code);return}}}
// A TERM_RUN command that ended well (exit 0) closes its terminal 3 s later and, when that terminal is
// still the one on screen, goes back to the tab its button was on. One that failed stays open on its error.
function termDone(t,code){if(!t.run||code!==0)return;
 t.term.write('\r\n\x1b[2mDone: this terminal closes in 3 s.\x1b[0m\r\n');
 setTimeout(async()=>{if(!TM[t.id])return;const back=t.from&&TA==t.id&&!document.getElementById('tt').hidden;
  await termClose(t.id);if(back)tab(t.from);refresh()},3000)}
function termFit(id){const t=TM[id];if(!t)return;try{t.fit.fit()}catch(e){}}
async function termNew(run,from){const r=await api('/api/term/new',{method:'POST',body:JSON.stringify({cols:120,rows:32,run})});
 if(r.error){alert(r.error);return}
 const l=await api('/api/term/list');const x=l.terms.find(q=>q.id==r.id);termAdd(r.id,x?x.num:Object.keys(TM).length+1,true,x?x.title:'',x?x.run:run,from);termShow(r.id)}
function termShow(id){TA=id;for(const t of Object.values(TM))t.el.style.display=t.id==id?'block':'none';
 termBar();termFit(id);const t=TM[id];setTimeout(()=>{termFit(id);t.term.focus()},30)}
async function termClose(id){
 const t=TM[id];delete TM[id];await api('/api/term/close',{method:'POST',body:JSON.stringify({id})});t.term.dispose();t.el.remove();
 if(TA!=id){termBar();return}   // another terminal is on screen: it stays there
 const ids=Object.keys(TM);if(ids.length)termShow(ids[ids.length-1]);else{TA=null;termBar()}}
function termMenu(e,id){e.preventDefault();termMenuOff();
 const m=document.createElement('div');m.id='tmenu';m.style.cssText=`left:${e.clientX}px;top:${e.clientY}px`;
 m.innerHTML='<button>Close this terminal</button>';m.firstChild.onclick=()=>{termMenuOff();termClose(id)};document.body.appendChild(m)}
function termMenuOff(){const m=document.getElementById('tmenu');if(m)m.remove()}
document.addEventListener('click',termMenuOff);document.addEventListener('keydown',e=>{if(e.key==='Escape')termMenuOff()});
function termBar(){document.getElementById('tbar').innerHTML=Object.values(TM).map(t=>`<span class="tt"><button class="${t.id==TA?'on':''}" onclick="termShow('${t.id}')" oncontextmenu="termMenu(event,'${t.id}')" title="right-click for options">${t.alive?'':'✕ '}${esc(t.title||'Terminal '+t.num)}</button></span>`).join('')+`<button class="pri" onclick="termNew()">+ New terminal</button>`}
refresh();setInterval(()=>{if(!busy)refresh()},5000);
const fmtS=n=>n<90?n+' s':n<5400?Math.round(n/60)+' min':Math.round(n/3600)+' h';
const monIgn=c=>c.ignored>Date.now()/1000;   // Ignore on its row: counts as fine until then
function monRow(c){const ig=monIgn(c),d=ig?'':{ok:'on',fail:'off',warn:'warn'}[c.status]||'',bad=!ig&&(c.status=='fail'||c.status=='warn');
 return `<div class="row"><span class="dot ${d}"></span><div class="grow"><b>${esc(c.label)}</b> <span class="dim">${esc(c.msg)}</span>`+
 (bad&&c.since?` <span class="tag bad">${fmtS(Math.max(0,Math.round(Date.now()/1000-c.since)))}</span>`:'')+
 (ig?` <span class="tag">ignored, ${fmtS(Math.round(c.ignored-Date.now()/1000))} left</span>`:'')+(c.alarm?'':` <span class="tag">alarm off</span>`)+
 (bad&&c.action?`<div class="act">→ ${esc(c.action)}</div>`:'')+
 (bad&&c.fixes&&c.fixes.length?`<div class="dim" style="font-size:12px">tried: ${esc(c.fixes[c.fixes.length-1])}</div>`:'')+`</div>`+monCtl(c,bad,ig)+`</div>`}
// a row's alarm buttons: on a problem, and wherever the user changed something
function monCtl(c,bad,ig){const b=(op,x,t)=>`<button class="mini" title="${t}" onclick="monRowOp('${op}',${esc(JSON.stringify(c.id))})">${x}</button>`;
 const h=ig?b('unignore','Stop ignoring','Count it as a problem again, with its alarm'):
  (bad||!c.alarm?(c.alarm?b('alarm-off','Mute','No macOS notification for this check until you unmute it (Settings lists every check)'):
   b('alarm-on','Unmute','Notify about this check again')):'')+
  (bad?b('ignore','Ignore...','Count it as fine for some hours: no notification, no repair, not a problem on this card'):'');
 return h?`<span class="btns" style="flex:none">${h}</span>`:''}
function monRowOp(op,id){const c=(lastMon&&lastMon.checks||[]).find(x=>x.id==id)||{label:id};
 if(op!='ignore')return monOp(op=='unignore'?'unignore':'alarm',{id,on:op=='alarm-on'});
 const h=askHours(`Ignore "${c.label}" for how many hours?\nUntil then it counts as fine: no notification and no repair.`,'ignoreH',2);
 if(h)monOp('ignore',{id,hours:h})}
// a number of hours from the user; the last one given comes up again next time
function askHours(q,key,def){try{def=localStorage[key]||def}catch(e){}const s=prompt(q,def);if(s==null)return 0;
 const h=parseFloat(String(s).replace(',','.'));if(!(h>0&&h<=720)){alert('Give the hours as a number above 0 and at most 720 (30 days).');return 0}
 try{localStorage[key]=String(h)}catch(e){}return h}
let monExpanded=false,lastMon=null;
async function targetMon(){const on=!(lastMon&&lastMon.targets&&lastMon.targets.on);
 try{await monOp('targets',{on})}catch(e){alert('Could not change continuous monitoring: '+e)}}
function renderTargetMon(m){const t=m.targets,b=document.getElementById('targetmon');
 b.disabled=!!m.off;b.textContent='Continuous monitoring: '+(t&&t.on?'on':'off');
 b.className=t&&t.on?'pri':'';b.setAttribute('aria-pressed',String(!!(t&&t.on)));
 document.getElementById('targetmsg').textContent=m.off?'Continuous monitoring unavailable (--no-monitor).':t?t.message:'';
 document.getElementById('targetlog').hidden=!(t&&t.job)}
function monSummary(cs,nb,nw){const ok=g=>cs.filter(c=>c.status=='ok'&&(!g||c.group==g)).length,n=g=>cs.filter(c=>c.group==g).length;
 const ni=cs.filter(monIgn).length,ns=cs.filter(c=>c.status!='ok'&&!monIgn(c)).length-nb-nw,head=nb?nb+(nb>1?' problems':' problem'):nw?nw+(nw>1?' warnings':' warning'):'All '+ok()+' checks OK';
 return `<div class="row" style="cursor:pointer" onclick="toggleMon()" title="Click to show every check"><span class="dot ${nb?'off':nw?'warn':'on'}"></span><div class="grow"><b>${head}</b> <span class="dim">This Mac ${ok('mac')}/${n('mac')} · tablet ${ok('tablet')}/${n('tablet')}${ns?' · '+ns+' not checked':''}${ni?' \u00b7 '+ni+' ignored':''}</span></div></div>`}
// seconds since the last pass, counted live between fetches (the page and Macotron share this Mac's clock)
function monAgo(){const m=lastMon;if(!m||m.off)return;const now=Date.now()/1000,a=m.last?Math.max(0,Math.round(now-m.last)):null;
 const late=a!=null&&a>3*m.tick,txt=a==null?'not yet':a+' s ago'+(late?' (checks stalled)':'');
 const ta=m.info&&m.info.tablet_age!=null?Math.max(0,Math.round(m.info.tablet_age+now-m._at)):null;
 document.querySelectorAll('.tago').forEach(e=>{e.textContent=ta==null?'unknown':ta+' s ago';e.style.color=ta!=null&&ta>45?'var(--bad)':''});
 const w=document.getElementById('mlast');if(w){w.textContent=' · last check '+txt;w.style.color=late?'var(--bad)':''}}
function renderMon(m){
 renderTargetMon(m);
 if(m.off){document.getElementById('mon').innerHTML='<div class="dim">The checks are off (started with --no-monitor).</div>';return}
 const live=m.checks.filter(c=>!monIgn(c)),nb=live.filter(c=>c.status=='fail').length,nw=live.filter(c=>c.status=='warn').length;
 const show=monExpanded?m.checks:live.filter(c=>c.status=='fail'||c.status=='warn');
 const mac=show.filter(c=>c.group=='mac'),tab=show.filter(c=>c.group=='tablet');
 const html=(mac.length?`<div class="grp">This Mac</div>`+mac.map(monRow).join(''):'')+
  (tab.length?`<div class="grp">Tablet · Tablotron's watch · last pass <span class="tago"></span></div>`+tab.map(monRow).join(''):'');
 document.getElementById('mon').innerHTML=(monExpanded?'':monSummary(m.checks,nb,nw))+html;monAgo();
 const mu=m.muted_until,muted=mu==-1||mu>Date.now()/1000;
 document.getElementById('mwhen').textContent=(nb?nb+(nb>1?' problems':' problem'):nw?nw+(nw>1?' warnings':' warning'):'all good')+
  ' · every '+m.tick+' s'+(muted?' · notifications muted'+(mu>0?' for '+fmtS(Math.round(mu-Date.now()/1000)):''):'')+
  (monExpanded?' · showing all, click to collapse':(nb||nw)?' · showing problems only, click to see all':'');
 document.getElementById('mbtns').innerHTML=`<button onclick="monOp('recheck')">Check now</button>`+(muted?`<button onclick="monOp('unmute')">Unmute all</button>`:
  `<button onclick="monMuteFor()" title="No macOS notifications for some hours; this card keeps updating">Mute all for...</button><button onclick="monOp('mute')" title="No macOS notifications until you unmute">Mute all</button>`)}
function toggleMon(){monExpanded=!monExpanded;if(lastMon)renderMon(lastMon)}
async function refreshMon(){let m;try{m=await api('/api/monitor')}catch(e){document.getElementById('mwhen').textContent='Macotron not answering';return}
 monShow(m)}
function monShow(m){m._at=Date.now()/1000;lastMon=m;renderMon(m);if(!document.getElementById('ck').hidden)renderGauges(m);
 if(!document.getElementById('ss').hidden)renderAlarmSet(m)}
async function monOp(op,o){const m=await api('/api/monitor',{method:'POST',body:JSON.stringify({op,...o})});
 if(m.error)return alert(m.error);monShow(m);setTimeout(refreshMon,800)}
function monMuteFor(){const h=askHours("Mute all of Macotron's notifications for how many hours?",'muteH',1);if(h)monOp('mute_for',{hours:h})}
// Settings > Alarms: All alarms is the card's Mute all, a check's switch is the Mute on its row
function renderAlarmSet(m){const el=document.getElementById('alarmset');
 if(!m||m.off){el.innerHTML=`<div class="dim">${m?'The checks are off (started with --no-monitor).':'Starting the checks...'}</div>`;return}
 const sw=(on,f,x,t)=>`<div class="row" title="${t||''}"><div class="sw ${on?'on':''}" onclick="${f}"></div><div class="grow">${x}</div></div>`;
 const mu=m.muted_until,muted=mu==-1||mu>Date.now()/1000,kind=(k,x,t)=>sw(m.notify[k],`monOp('notify',{id:'${k}',on:${!m.notify[k]}})`,x,t);
 const grp=(g,name)=>{const cs=m.checks.filter(c=>c.group==g).sort((a,b)=>a.label.localeCompare(b.label));
  return cs.length?`<div class="grp">${name}</div>`+cs.map(c=>sw(c.alarm,`monOp('alarm',{id:${esc(JSON.stringify(c.id))},on:${!c.alarm}})`,
   esc(c.label)+(monIgn(c)?' <span class="tag">ignored</span>':''))).join(''):''};
 el.innerHTML=sw(!muted,muted?"monOp('unmute')":"monOp('mute')",'All alarms'+(muted&&mu>0?` <span class="tag">muted, ${fmtS(Math.round(mu-Date.now()/1000))} left</span>`:''),
   'Off: no notifications at all until you turn it back on')+
  kind('warn','Warnings','A check that is not quite right yet (sound Purr)')+kind('back','Back to normal','A check that failed works again')+
  kind('sound','Sounds','Basso for a problem, Purr for a warning')+grp('mac','This Mac')+grp('tablet','Tablet (it also posts its own notifications)')}
// Clipboard tab: the saved clips, saved by Ctrl+Option+<key> (clipkeeper) or by the button
let clipTimer=0,clipLast=null,clipSig='';
async function clipLoad(){
 try{clipRender(await api('/api/clips'))}catch(e){document.getElementById('cb-msg').textContent='Macotron did not answer: '+e}
 clearInterval(clipTimer);clipTimer=setInterval(()=>{if(document.getElementById('cb').hidden){clearInterval(clipTimer);return}api('/api/clips').then(clipRender).catch(()=>{})},3000)}
function clipRenderLast(){clipSig='';if(clipLast)clipRender(clipLast)}
function clipRender(r){
 if(!r||r.error){document.getElementById('cb-msg').textContent=(r&&r.error)||'No answer';return}
 clipLast=r;const k=r.keeper,c=r.cfg;
 document.getElementById('cb-on').className='sw'+(c.on?' on':'');document.getElementById('cb-key').value=c.keycode;
 let dot='',msg;
 if(!c.on){msg='The shortcut is off. Clips are saved only by the button.'}
 else if(!k.built&&!k.running){dot='warn';msg='Starting the shortcut helper (it is compiled on first use: a minute)...'}
 else if(!k.running){dot='warn';msg='The shortcut helper is not running; Macotron starts it again within a few seconds.'}
 else if(!k.registered){dot='off';msg='The shortcut could not be registered (another app uses Ctrl+Option+that key). Choose the other key.'}
 else{dot='on';msg='Ready: press Ctrl+Option+'+({24:'=',10:'§',50:'`'}[c.keycode]||'?')+' in any app to save what is on the clipboard.'}
 document.getElementById('cb-dot').className='dot '+dot;document.getElementById('cb-msg').textContent=msg;
 const q=(document.getElementById('cb-q').value||'').toLowerCase();
 const rows=r.items.filter(i=>!q||(i.kind=='text'&&i.text.toLowerCase().includes(q)));
 const sig=JSON.stringify([rows.map(i=>[i.id,i.pinned]),q]);
 if(sig==clipSig)return;clipSig=sig;
 document.getElementById('cb-list').innerHTML=rows.length?rows.map(i=>{
  const when=new Date(i.t*1000).toLocaleString([],{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
  const body=i.kind=='image'?`<img src="/api/clip-image?id=${i.id}&t=${encodeURIComponent(T)}" style="max-width:100%;max-height:220px;border-radius:6px">`
   :`<pre class="mono" style="white-space:pre-wrap;word-break:break-word;margin:6px 0 0;max-height:9.5em;overflow:auto">${esc(i.text)}</pre>`;
  return `<section class="card" style="margin-bottom:10px"><div class="row" style="border:0;padding:0"><div class="grow dim">${when} · ${i.kind=='image'?'image, '+Math.round(i.bytes/1024)+' KB':i.len+' characters'}${i.pinned?' · pinned':''}</div>
   <button onclick="clipOp('copy',{id:'${i.id}'})" title="Put it back on the Mac clipboard">Copy</button><button onclick="clipOp('pin',{id:'${i.id}'})">${i.pinned?'Unpin':'Pin'}</button><button class="danger" onclick="clipOp('delete',{id:'${i.id}'})">Delete</button></div>${body}</section>`}).join(''):'<div class="dim" style="padding:14px">Nothing saved yet'+(q?' that matches the search':'')+'.</div>'}
async function clipOp(op,o){
 try{const r=await api('/api/clips',{method:'POST',body:JSON.stringify({op,...(o||{})})});
  if(r.error)return alert(r.error);
  if(op=='copy'){document.getElementById('cb-msg').textContent='Copied: it is on the Mac clipboard again.';return}
  clipSig='';clipRender(r)}catch(e){alert('Macotron did not answer: '+e)}}
function clipSet(o){clipOp('set',o)}
// Settings > All ports through the tablet: ssh -D, a SOCKS5 proxy on localhost
let skBusy=0;
async function socksLoad(){try{socksRender(await api('/api/socks'))}catch(e){}}
function socksRender(r){
 const on=document.getElementById('sk-on');if(!on||skBusy||!r||!r.cfg)return;
 on.className='sw'+(r.running?' on':(r.cfg.on?' warn':''));
 document.getElementById('sk-msg').textContent=r.running?'On: SOCKS5 proxy at localhost:'+r.cfg.port+'.':(r.cfg.on?'Starting... (Macotron tries again every few seconds).':'Off.');
 const p=document.getElementById('sk-port');if(document.activeElement!==p)p.value=r.cfg.port;
 document.getElementById('sk-peers').textContent=r.peers.length?'Hosts: '+r.peers.map(x=>x.name+' = '+x.ip).join('   '):'';
 const ip=(r.peers[0]||{ip:'100.x.y.z'}).ip,pt=r.cfg.port;
 document.getElementById('sk-use').textContent=r.running?
  'ssh through it:   ssh -o ProxyCommand="nc -X 5 -x localhost:'+pt+' %h %p" user@'+ip+'\n'+
  'curl through it:  curl --socks5-hostname localhost:'+pt+' http://'+ip+':PORT/\n'+
  'a program with its own proxy setting: SOCKS5, host localhost, port '+pt+', then connect to '+ip+' on any port\n'+
  'Chrome:           open -na "Google Chrome" --args --proxy-server="socks5://localhost:'+pt+'" --user-data-dir=/tmp/chrome-socks':'';}
async function socksOp(o){skBusy=1;
 try{const r=await api('/api/socks',{method:'POST',body:JSON.stringify(o)});if(r.error)alert(r.error);skBusy=0;socksRender(r.error?await api('/api/socks'):r)}
 catch(e){alert('Macotron did not answer: '+e)}finally{skBusy=0}}
function socksToggle(){socksOp({op:document.getElementById('sk-on').classList.contains('on')||document.getElementById('sk-on').classList.contains('warn')?'off':'on'})}
function socksPort(){socksOp({op:'set',port:+document.getElementById('sk-port').value})}
// Settings > Tablet screen window: ffplay shows the tablet's screenrecord stream
let tscBusy=0;
async function tscLoad(){try{tscRender(await api('/api/tabscreen'))}catch(e){}}
function tscRender(s){
 if(!s||s.error&&!s.cfg){return}
 const on=document.getElementById('tsc-on');if(!on||tscBusy)return;
 on.className='sw'+(s.running?' on':'');
 document.getElementById('tsc-msg').textContent=s.running?'On: the window shows the tablet'+(s.size?' ('+s.size[0]+' x '+s.size[1]+')':'')+'.':(s.error?'Off. '+s.error:'Off.');
 const set=(id,v)=>{const e=document.getElementById(id);if(e&&document.activeElement!==e)e.value=v};set('tsc-scale',s.cfg.scale);set('tsc-rate',s.cfg.bitrate)}
async function tscOp(op,o){tscBusy=1;const m=document.getElementById('tsc-msg');if(op=='start')m.textContent='Starting the stream and the window...';
 try{const s=await api('/api/tabscreen',{method:'POST',body:JSON.stringify({op,...(o||{})})});if(s.error)alert(s.error);tscBusy=0;tscRender(s.error?await api('/api/tabscreen'):s)}
 catch(e){alert('Macotron did not answer: '+e)}finally{tscBusy=0}}
function tscToggle(){tscOp(document.getElementById('tsc-on').classList.contains('on')?'stop':'start')}
function tscSave(){tscOp('set',{scale:+document.getElementById('tsc-scale').value,bitrate:+document.getElementById('tsc-rate').value,restart:true})}
// Settings > Tablet control: starts the tablet's input daemon, the tunnel and the Mac helper (tabinput)
let tabinTimer=0,tabinBusy=0;
async function tabinLoad(){
 tscLoad();socksLoad();try{tabinRender(await api('/api/tabinput'))}catch(e){document.getElementById('tin-msg').textContent='Macotron did not answer: '+e}
 clearInterval(tabinTimer);tabinTimer=setInterval(()=>{if(document.getElementById('ss').hidden){clearInterval(tabinTimer);return}tscLoad();socksLoad();if(!tabinBusy)api('/api/tabinput').then(tabinRender).catch(()=>{})},2000)}
function tabinRender(s){
 if(!s||s.error){document.getElementById('tin-msg').textContent=(s&&s.error)||'No answer';return}
 const c=s.cfg,set=(id,v)=>{const e=document.getElementById(id);if(e&&document.activeElement!==e)e.value=v};
 set('tin-edge',c.edge);set('tin-trig',c.trigger);tabinScreens(s,c);set('tin-skey',c.shortcut_key);set('tin-rtrig',c.return_trigger);set('tin-rsize',c.return_size);set('tin-csize',c.corner_size);set('tin-tw',c.tw);set('tin-th',c.th);set('tin-speed',c.speed);set('tin-delay',c.delay);set('tin-dim',c.dim);set('tin-idle',c.meeting_idle_min);tabinSpeedShow();tabinDimShow();
 document.getElementById('tin-auto').className='sw'+(c.auto_size?' on':'');
 document.getElementById('tin-esc').className='sw'+(c.esc_returns?' on':'');
 document.getElementById('tin-paste').className='sw'+(c.paste_typing?' on':'');
 document.getElementById('tin-mtg').className='sw'+(c.meeting_on_control?' on':'');
 document.getElementById('tin-mtgnote').textContent=!c.meeting_on_control?'':s.meeting_error?'The Meeting profile could not be set: '+s.meeting_error:s.meeting_until?'The tablet is in its Meeting profile until about '+new Date(s.meeting_until*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})+' (it goes back to its earlier settings by itself).':'Not in Meeting now: it starts at the first activity.';
 document.getElementById('tin-cmd').className='sw'+(c.cmd_as_ctrl?' on':'');document.getElementById('tin-inv').className='sw'+(c.invert_scroll?' on':'');
 let dot='',msg;
 if(!s.built)msg='Not built yet: Start compiles the Mac helper (a minute).';
 else if(!s.running)msg='Stopped.';
 else if(s.state=='active'){dot='on';const eff=(c.trigger=='edge'||c.trigger=='shortcut')?c.edge:(c.trigger.endsWith('right')?'right':'left');msg='The tablet has the keyboard and mouse. Ctrl+Option+Cmd+Esc or the tablet pointer leaving past the '+oppEdge(eff)+' edge gives them back.'}
 else if(s.state=='idle'){dot='on';msg=s.msg||'Ready.'}
 else if(s.state=='noconn'){dot='warn';msg='Running, but the tablet\'s input daemon does not answer'+(s.tunnel?'.':' (the tunnel is down).')+' Press Start again.'}
 else msg=s.msg||s.state;
 document.getElementById('tin-dot').className='dot '+dot;document.getElementById('tin-msg').textContent=msg;
 document.getElementById('tin-start').textContent=s.running?'Restart':'Start';document.getElementById('tin-stop').disabled=!s.running}
function tabinScreens(s,c){
 const el=document.getElementById('tin-scr');if(!el||document.activeElement===el)return;
 const list=(s.screens||[]),key=JSON.stringify(list.map(x=>x.id));
 if(el.dataset.key!==key){el.innerHTML='<option value="0">All monitors together</option>'+list.map((x,i)=>'<option value="'+x.id+'">'+(['Left','Middle','Right'][list.length==3?i:-1]||('Monitor '+(i+1)))+': '+esc(x.name)+' ('+x.w+' x '+x.h+(x.main?', main':'')+')</option>').join('');el.dataset.key=key}
 el.value=String(c.trigger_screen||0)}
function oppEdge(e){return {right:'left',left:'right',top:'bottom',bottom:'top'}[e]||e}
async function tabinOp(op,o){tabinBusy=1;const msg=document.getElementById('tin-msg');if(op=='start')msg.textContent='Starting the tablet side, the tunnel and the helper...';
 try{const s=await api('/api/tabinput',{method:'POST',body:JSON.stringify({op,...(o||{})})});if(s.error)alert(s.error);else tabinRender(s)}
 catch(e){alert('Macotron did not answer: '+e)}finally{tabinBusy=0;tabinLoad()}}
function tabinDimShow(){document.getElementById('tin-dimv').textContent=Math.round(100*(+document.getElementById('tin-dim').value))+' %'}
function tabinSpeedShow(){document.getElementById('tin-speedv').textContent=(+document.getElementById('tin-speed').value).toFixed(1)}
let tabinSaveT=0;function tabinSaveSoon(){clearTimeout(tabinSaveT);tabinSaveT=setTimeout(tabinSave,250)}
function tabinSave(){const g=id=>document.getElementById(id).value;
 api('/api/tabinput',{method:'POST',body:JSON.stringify({op:'set',edge:g('tin-edge'),trigger:g('tin-trig'),trigger_screen:+g('tin-scr'),shortcut_key:+g('tin-skey'),return_trigger:g('tin-rtrig'),return_size:+g('tin-rsize'),corner_size:+g('tin-csize'),tw:+g('tin-tw'),th:+g('tin-th'),speed:+g('tin-speed'),delay:+g('tin-delay'),dim:+g('tin-dim'),meeting_idle_min:+g('tin-idle')})}).then(s=>{if(s.error)alert(s.error);else tabinRender(s)})}
function tabinToggle(k){const on=!document.getElementById({cmd_as_ctrl:'tin-cmd',invert_scroll:'tin-inv',meeting_on_control:'tin-mtg',paste_typing:'tin-paste',auto_size:'tin-auto',esc_returns:'tin-esc'}[k]).classList.contains('on');
 api('/api/tabinput',{method:'POST',body:JSON.stringify({op:'set',[k]:on})}).then(tabinRender)}
// Settings > Restart Macotron: the server runs itself again in place; the page reloads once the new copy answers
async function restartApp(skipAsk){
 if(!skipAsk&&!confirm('Restart Macotron? Its server starts again in place (a few seconds) and this page reloads. Open terminals end.'))return;
 let r;try{r=await api('/api/restart',{method:'POST',body:'{}'})}catch(e){return alert('Macotron did not answer: '+e)}
 if(r.error)return alert(r.error);
 const ts=document.getElementById('ts'),t0=Date.now();
 while(Date.now()-t0<60000){
  ts.textContent='restarting...';
  await new Promise(ok=>setTimeout(ok,800));
  try{const p=await api('/api/ping');if(p.boot&&p.boot!=r.boot){location.reload();return}}catch(e){}
 }
 ts.textContent='not back';
 alert('Macotron did not come back within a minute. Start it again from a terminal: python3 phone-proxy-web.py --replace');
}
function setTheme(t){document.documentElement.setAttribute('data-theme',t);try{localStorage.theme=t}catch(e){}
 document.getElementById('theme-dark').className=t=='dark'?'pri':'';document.getElementById('theme-light').className=t=='light'?'pri':'';
 const bdf=document.getElementById('bdf');if(bdf&&bdf.contentWindow)bdf.contentWindow.postMessage({type:'bedrock-theme',theme:t},'*')}
setTheme(document.documentElement.getAttribute('data-theme')||'dark');
refreshMon();setInterval(refreshMon,3000);setInterval(monAgo,1000);
</script></body></html>"""


# ---- app window -----------------------------------------------------------
def window_app():
    """Path of Macotron.app, built from macotron-app.swift and the Info.plist below when it is
    missing or was built from another version of either (the bundle keeps their hash), or None
    when it cannot be. A server the app starts from the Dock has Macotron as its responsible app,
    so the usage strings word the macOS prompts that server's Documents, LAN and Edge use bring."""
    info = {"CFBundleName": "Macotron", "CFBundleDisplayName": "Macotron",
            "CFBundleIdentifier": "local.phone-proxy.macotron", "CFBundleExecutable": "Macotron",
            "CFBundlePackageType": "APPL", "CFBundleShortVersionString": "1.0",
            "LSMinimumSystemVersion": "14.0", "NSHighResolutionCapable": True,
            "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
            "NSDocumentsFolderUsageDescription":
                "The Macotron server it starts runs phone-proxy-web.py from your Documents folder.",
            "NSLocalNetworkUsageDescription":
                "The Macotron server it starts reaches the phone and the tablet on the local network.",
            "NSAppleEventsUsageDescription":
                "The Macotron server it starts quits and reopens Edge to switch the phone proxy."}
    try:
        with open(WINDOW_SRC, "rb") as f:
            stamp = hashlib.sha256(f.read() + plistlib.dumps(info)).hexdigest()
    except OSError:
        return None

    def built(app):
        try:
            with open(os.path.join(app, "Contents", "Resources", "source.sha256")) as f:
                return f.read().strip() == stamp
        except OSError:
            return False

    if built(WINDOW_APP):
        return WINDOW_APP
    swiftc = shutil.which("swiftc")
    if not swiftc:
        print("No swiftc to build the app window with (xcode-select --install adds it).", file=sys.stderr)
        return None
    tmp = WINDOW_APP + ".building"
    with open(os.path.join(STATE, "window-build.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)   # one build at a time; a copy that waited finds it done
        if built(WINDOW_APP):
            return WINDOW_APP
        print("Building the Macotron app window (first use or a changed macotron-app.swift, under a minute)...",
              file=sys.stderr, flush=True)
        info["CFBundleVersion"] = stamp[:12]
        try:
            shutil.rmtree(tmp, ignore_errors=True)
            os.makedirs(os.path.join(tmp, "Contents", "MacOS"))
            os.makedirs(os.path.join(tmp, "Contents", "Resources"))
            with open(os.path.join(tmp, "Contents", "Info.plist"), "wb") as f:
                plistlib.dump(info, f)
            r = subprocess.run([swiftc, "-O", "-o", os.path.join(tmp, "Contents", "MacOS", "Macotron"), WINDOW_SRC],
                               capture_output=True, text=True, timeout=600)
            if r.returncode:
                raise OSError((r.stderr or r.stdout)[-1500:])
            with open(os.path.join(tmp, "Contents", "Resources", "source.sha256"), "w") as f:
                f.write(stamp + "\n")
            shutil.rmtree(WINDOW_APP, ignore_errors=True)
            os.rename(tmp, WINDOW_APP)
        except (OSError, subprocess.SubprocessError) as e:
            shutil.rmtree(tmp, ignore_errors=True)
            print("Could not build the app window: %s" % e, file=sys.stderr)
            return None
    return WINDOW_APP


def open_window(url):
    """Show the page in the Macotron app window, or in the default browser when there is no app.
    First note how this copy runs (python, script, the terminal's PATH) in WINDOW_RUN: opened from
    the Dock with nothing on the port, the app starts the server from that."""
    try:
        with open(WINDOW_RUN, "w") as f:
            json.dump({"python": sys.executable, "script": os.path.abspath(__file__),
                       "path": os.environ.get("PATH", "")}, f)
    except OSError:
        pass
    app = window_app()
    if app and subprocess.run(["open", "-a", app, url]).returncode == 0:
        return
    print("Opening the page in the default browser instead.", file=sys.stderr)
    subprocess.run(["open", url])


def port_holder(port):
    """(pid, command) of the process listening on port, or (0, "")."""
    try:
        out = subprocess.run(["lsof", "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN", "-t"],
                             capture_output=True, text=True, timeout=5).stdout
        pid = next((int(p) for p in out.split() if p.isdigit()), 0)
        if not pid:
            return 0, ""
        cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        # paths say little; the program and its arguments say what it is
        return pid, " ".join(w.rsplit("/", 1)[-1] if "/" in w else w for w in cmd.split())
    except (OSError, subprocess.SubprocessError):
        return 0, ""


def bind(port):
    try:
        return ThreadingHTTPServer(("127.0.0.1", port), H)
    except OSError:
        return None


def restart_self():
    """Settings > Restart Macotron: run this script again in this very process (exec), so the pid,
    the terminal it runs in (or the Dock app that started it) and its arguments stay, plus
    --headless (the window is open already) and --replace (no question in the terminal). Refused
    while a job runs, and when the script on disk does not compile: a half-edited file must never
    take Macotron down. Open terminals end; the page reloads once the new copy answers."""
    with JOBS_LOCK:
        busy = [j["title"] for j in JOBS.values() if not j["done"]]
    if busy:
        raise ValueError("Not restarted: %s still running. Try again when it ends." % ", ".join(busy))
    script = os.path.abspath(__file__)
    try:
        with open(script, "rb") as f:
            compile(f.read(), script, "exec")
    except (OSError, SyntaxError, ValueError) as e:
        raise ValueError("Not restarted: %s does not compile (%s)." % (os.path.basename(script), e))
    argv = [sys.executable, script] + [a for a in sys.argv[1:] if a != "--keep"]
    argv += [f for f in ("--headless", "--replace") if f not in argv]
    if ADB:
        with ADB.lock:
            ADB.close_scrcpy()

    def go():
        time.sleep(0.5)                       # the answer reaches the page first
        for tid in [t["id"] for t in term_list()]:
            try:
                term_close(tid)               # their shells end now, not as strays of the new copy
            except ValueError:
                pass
        mon_log("restart from the page: " + " ".join(shlex.quote(a) for a in argv[1:]))
        sys.stdout.flush()
        sys.stderr.flush()
        locked = MONITOR is not None and MONITOR.lock.acquire(timeout=10)   # never halfway through a save
        try:
            os.execv(sys.executable, argv)
        except OSError as e:                  # only reached when the exec failed: keep running as before
            mon_log("restart failed: %s" % e)
            if locked:
                MONITOR.lock.release()
    threading.Thread(target=go, daemon=True, name="restart").start()


def start_server():
    """Bind the port. An earlier copy of this page on it is replaced: after a question when there
    is a terminal to ask at (default yes), without one otherwise (a restart is what that means),
    never with --keep; that, and a replace declined, open the window on the running copy (unless
    --headless). Anything else on the port is refused."""
    srv = bind(PORT)
    if srv is not None:
        return srv
    pid, cmd = port_holder(PORT)
    url = "http://localhost:%d/?t=%s" % (PORT, TOKEN)
    if not pid or "phone-proxy-web.py" not in cmd:
        who = "pid %d (%s)" % (pid, cmd[:80]) if pid else "something this user cannot see"
        print("Port %d is taken by %s; pick another port as an argument." % (PORT, who))
        sys.exit(1)
    if "--keep" in FLAGS:
        print("Already running on port %d (pid %d): %s" % (PORT, pid, url))
        if not HEADLESS:
            open_window(url)
        sys.exit(0)
    if "--replace" not in FLAGS and sys.stdin.isatty():
        answer = input("Already running on port %d (pid %d). Replace it with this copy? "
                       "Its page terminals end. [Y/n] " % (PORT, pid)).strip().lower()
        if answer not in ("", "y", "yes"):
            print("Left it running: %s" % url)
            if not HEADLESS:
                open_window(url)
            sys.exit(0)
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        time.sleep(0.1)
        srv = bind(PORT)
        if srv is not None:
            print("Replaced the copy that was running (pid %d)." % pid)
            return srv
    print("Pid %d did not release port %d; stop it yourself (kill %d) and try again." % (pid, PORT, pid))
    sys.exit(1)


if __name__ == "__main__":
    while True:   # children that a copy restarted in place (Settings > Restart Macotron) left behind
        try:
            if os.waitpid(-1, os.WNOHANG)[0] == 0:
                break
        except ChildProcessError:
            break
    srv = start_server()
    try:
        ADB = PhoneADB(STATE, devices.active, mon_log)
        if "--no-monitor" not in FLAGS:
            ADB.start()
    except (ValueError, OSError) as e:
        ADB_ERROR = "Could not load ADB settings: %s" % e
        mon_log(ADB_ERROR)
    if "--no-monitor" not in FLAGS:
        MONITOR = Monitor()
        MONITOR.start()
    # >>> bedrock-usage glue (patch_macotron.py) >>>
    if bedrock_glue is not None:
        try:
            bedrock_glue.configure(ssh_tab=ssh_tab, mon_log=mon_log, here=HERE, monitor_getter=lambda: MONITOR)
            bedrock_glue.BedrockPush().start()
        except Exception:
            pass
    # <<< bedrock-usage glue <<<
    # >>> aws auto-login glue >>>
    if aws_autologin is not None:
        try:
            aws_autologin.configure(STATE, mon_log, lambda: term_new(run="bedrock-auto"),
                                    lambda: bool(sh(["aws", "sts", "get-caller-identity", "--cli-connect-timeout", "5",
                                                     "--cli-read-timeout", "10", "--query", "Account", "--output", "text"], timeout=16).strip()))
            aws_autologin.start()
        except Exception:
            pass
    # <<< aws auto-login glue <<<
    # >>> reader glue (Reader tab) >>>
    if reader_glue is not None:
        try:
            reader_glue.configure(here=HERE, mon_log=mon_log)
        except Exception:
            pass
    # <<< reader glue <<<
    # >>> offload glue (Offload tab) >>>
    if offload_glue is not None:
        try:
            offload_glue.configure(here=HERE, mon_log=mon_log)
        except Exception:
            pass
    # <<< offload glue <<<
    url = "http://localhost:%d/?t=%s" % (PORT, TOKEN)
    print("Macotron: " + url, flush=True)
    if not HEADLESS:   # beside the server, which a first build of the window must not hold up
        threading.Thread(target=open_window, args=(url,), daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
