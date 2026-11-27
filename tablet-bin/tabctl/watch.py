#!/data/data/com.termux/files/usr/bin/python3
"""watch.py - Tablotron's watchdog: the root half that keeps the tablet router working.

Tablotron (tabctl.py) is the control page. It runs as the Termux user, so Android can
freeze it together with everything else Termux started (sshd, the socat forwards). This
file runs as root through Magisk su, which puts it outside Termux's process group: it
keeps watching while Termux is frozen and can bring Termux back.

  su -c 'python3 ~/tabctl/watch.py start'   start in the background (no-op if it runs)
  su -c 'python3 ~/tabctl/watch.py stop'
  su -c 'python3 ~/tabctl/watch.py run'     the loop in the foreground
  su -c 'python3 ~/tabctl/watch.py once'    one pass, print the checks, repair nothing
  python3 ~/tabctl/watch.py status          print the last state (no root needed)

Every 5 s it checks the connections: home Wi-Fi, internet, hotspot, Tailscale, Termux
(wake lock, frozen or not), sshd, the forwards, the NAT addresses and their names. Every
1 minute it also reads Tablotron's full status (root.sh status) for the SSD, Samba,
battery and Android settings, takes a short Termux:API accelerometer burst, and appends
a sample (temperatures, signal, battery, storage, tablet movement) to
~/.config/tabctl/history.db for Tablotron's History page to plot; the same pass logs an
SSD disconnect to that database, with whether Eject was pressed first. The same
minute it runs sdbuffer.py tick, which opens the microSD buffer, points the drive share at
the SSD or the buffer, and starts moving buffered files onto the SSD; a pulled SSD is
seen within 5 s, so the share moves to the buffer at once. A Meeting power level whose
end time passed while the tablet restarted is ended here (root.sh power-meeting-end). It
repairs what is safe to repair, posts an Android notification when a connection drops or
comes back, and says what a person has to do when it cannot repair something. The state
goes to ~/.config/tabctl/watch.json, which Tablotron's page and Macotron on the Mac read.

It never touches the SSD passphrase, never unlocks the SSD and never runs
termux-wake-unlock.
"""
import fcntl
import json
import math
import os
import re
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import traceback

HOME = "/data/data/com.termux/files/home"
TERMUX_BIN = "/data/data/com.termux/files/usr/bin"
CFG = HOME + "/.config/tabctl"
STATE = CFG + "/watch.json"
HISTORY_DB = CFG + "/history.db"
HISTORY_DAYS = 180   # how long samples and SSD events are kept
SENSORS_JSON = CFG + "/sensors.json"   # written by sensors_helper.py, running as the Termux user
SENSORS_STALE = 150   # seconds; older than this, treat the helper's last reading as unavailable
DISMISSED = CFG + "/dismissed.json"   # {check_id: signature}; written by tabctl.py's dismiss_check()
LOG = CFG + "/watch.log"
PIDF = CFG + "/watch.pid"
ROOT_SH = HOME + "/tabctl/root.sh"
SDBUFFER = HOME + "/tabctl/sdbuffer.py"   # the microSD card as the drive share's buffer
NAT_UP = HOME + "/bin/nat-up.sh"
BOOT = HOME + "/.termux/boot/10-start.sh"
TARGETS = HOME + "/meridian-targets.conf"
NAT_STATE = "/data/local/tmp/meridian-nat.state"
TSU = "/data/data/com.termux/files/usr/bin/tsu"
TSU_OLD = 'SU_BINARY_SEARCH=("/system/xbin/su" "/system/bin/su")'
TSU_NEW = 'SU_BINARY_SEARCH=("/system/xbin/su" "/system/bin/su" "/product/bin/su" "/debug_ramdisk/su")'
SYS = "/system/bin/"
STA_IF = "wlan0"
HOTSPOT_SCREEN = "com.android.settings/.Settings$WifiTetherSettingsActivity"

TICK = 5            # connection checks
SLOW = 30           # wake lock, Wi-Fi name, sshd banner, DNS
PEERS_EVERY = 60    # Tailscale peers
FULL_EVERY = 60     # root.sh status; also the history-sample interval
INTERNET = (("1.1.1.1", 443), ("8.8.8.8", 443), ("9.9.9.9", 443))
RENOTIFY = 1800     # repeat a failure notification every 30 min while it lasts

TUSER = os.stat(HOME).st_uid


def now():
    return time.time()


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg.rstrip() + "\n"
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > 512 * 1024:
            os.replace(LOG, LOG + ".1")
        new = not os.path.exists(LOG)
        with open(LOG, "a") as f:
            f.write(line)
        if new:
            own(LOG)
    except OSError:
        pass


def gb(n):
    """bytes as GB (or MB under 1 GB), the way Finder counts them"""
    return "%.1f GB" % (n / 1e9) if n >= 1e9 else "%d MB" % round(n / 1e6)


def own(path):
    try:
        os.chown(path, TUSER, TUSER)
        os.chmod(path, 0o644)
    except OSError:
        pass


def history_db():
    """The 1-minute samples and SSD disconnect log, owned by the Termux user so Tablotron can read it."""
    con = sqlite3.connect(HISTORY_DB, timeout=5)
    con.execute("CREATE TABLE IF NOT EXISTS samples ("
                "t INTEGER PRIMARY KEY, ssid TEXT, sta_freq INTEGER, sta_rssi INTEGER, sta_link INTEGER, "
                "ap_freq INTEGER, ap_bw INTEGER, ap_clients INTEGER, internet_ms INTEGER, "
                "bat_level INTEGER, bat_temp_c REAL, bat_charging INTEGER, "
                "ssd_present INTEGER, ssd_temp_c REAL, ssd_open INTEGER, ssd_mounted INTEGER, ssd_used_pct REAL, "
                "int_used_pct REAL, sd_used_pct REAL, wakelocks INTEGER, termux_frozen INTEGER, power_level TEXT, accel_pp REAL, "
                "lat REAL, lon REAL, loc_acc_m REAL)")
    con.execute("CREATE TABLE IF NOT EXISTS ssd_events (t INTEGER PRIMARY KEY, event TEXT NOT NULL, safe INTEGER)")
    for col, decl in (("accel_pp", "REAL"), ("lat", "REAL"), ("lon", "REAL"), ("loc_acc_m", "REAL")):
        try:
            con.execute("ALTER TABLE samples ADD COLUMN %s %s" % (col, decl))   # upgrades a database made before this column existed
        except sqlite3.OperationalError:
            pass   # already there
    return con


_ZENV = None


def zenv():
    """Android's own tools (am, cmd) want zygote's environment, which su does not give."""
    global _ZENV
    if _ZENV is None:
        env = {}
        try:
            pid = subprocess.run([SYS + "pidof", "zygote64"], capture_output=True, text=True,
                                 timeout=5).stdout.split()[0]
            with open("/proc/%s/environ" % pid, "rb") as f:
                for item in f.read().split(b"\0"):
                    k, sep, v = item.decode(errors="replace").partition("=")
                    if sep:
                        env[k] = v
        except Exception:
            pass
        env.setdefault("PATH", "/system/bin:/system/xbin")
        env.setdefault("ANDROID_DATA", "/data")
        env.setdefault("ANDROID_ROOT", "/system")
        _ZENV = env
    return _ZENV


def run(argv, timeout=20, sysenv=False):
    env = zenv() if sysenv else {k: v for k, v in os.environ.items() if k != "LD_PRELOAD"}
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env,
                           stdin=subprocess.DEVNULL)
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, "timed out after %d s" % timeout
    except OSError as e:
        return 127, str(e)


# -- small probes -------------------------------------------------------------
def read_sensors():
    """The latest accelerometer/location reading from sensors_helper.py (its own long-lived
    process, started by the boot script as the Termux user -- see that file's docstring for why
    this daemon no longer samples Termux:API itself: every "su <uid> -c termux-sensor ..." call
    that hung left a permanently-orphaned process behind, because su hands the work off to
    magiskd, not to a child of this process, so nothing here could ever kill it). Returns a dict
    with "accel_pp" and, when a fix is cached, "lat"/"lon"/"loc_acc_m"/"loc_provider" -- or {} if
    the helper is not running or its last write is too old to trust.
    """
    try:
        with open(SENSORS_JSON) as f:
            d = json.load(f)
        if now() - d.get("t", 0) > SENSORS_STALE:
            return {}
        return d
    except (OSError, ValueError):
        return {}


def read_dismissed():
    """{check_id: signature} that the Tablotron page's Dismiss button has acknowledged -- written
    directly by tabctl.py (CFG is owned by the Termux user, so that needs no su). A warning stays
    dismissed only while its own signature matches; a new occurrence of the same kind of event
    gets a new signature and so un-dismisses itself. See ssd_safety's status logic."""
    try:
        with open(DISMISSED) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def ifaddr(name):
    """The interface's primary IPv4 address, without starting a process."""
    if not name:
        return ""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        r = fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", name[:15].encode()))
        return socket.inet_ntoa(r[20:24])
    except OSError:
        return ""
    finally:
        s.close()


def operstate(name):
    try:
        with open("/sys/class/net/%s/operstate" % name) as f:
            return f.read().strip()
    except OSError:
        return "missing"


def tailscale_if():
    try:
        names = sorted(os.listdir("/sys/class/net"))
    except OSError:
        return "", ""
    for n in names:
        if n.startswith("tun"):
            a = ifaddr(n)
            if a.startswith("100."):
                return n, a
    return "", ""


def listening():
    ports = set()
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path) as f:
                for line in f.readlines()[1:]:
                    p = line.split()
                    if len(p) > 3 and p[3] == "0A":
                        ports.add(int(p[1].rsplit(":", 1)[1], 16))
        except OSError:
            pass
    return ports


def tcp(host, port, timeout=2.0, banner=None):
    """Milliseconds to connect (and to read the banner, if one is wanted), or None."""
    t0 = time.monotonic()
    try:
        with socket.create_connection((host, int(port)), timeout=timeout) as s:
            if banner is not None:
                s.settimeout(timeout)
                if not s.recv(64).startswith(banner):
                    return None
            return int((time.monotonic() - t0) * 1000)
    except (OSError, ValueError):
        return None


def dns_ok(name="connectivitycheck.gstatic.com", timeout=4.0):
    box = []
    t = threading.Thread(target=lambda: box.append(_resolve(name)), daemon=True)
    t.start()
    t.join(timeout)
    return bool(box and box[0])


def _resolve(name):
    try:
        return socket.getaddrinfo(name, 443, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return None


def proc_cmd(pid):
    try:
        with open("/proc/%s/cmdline" % pid, "rb") as f:
            return [c.decode(errors="replace") for c in f.read().split(b"\0") if c]
    except OSError:
        return []


def pids_where(pred):
    res = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            uid = os.stat("/proc/" + d).st_uid
        except OSError:
            continue
        cmd = proc_cmd(d)
        if cmd and pred(cmd, uid):
            res.append(int(d))
    return res


def targets():
    out = []
    try:
        with open(TARGETS) as f:
            for line in f:
                p = line.split()
                if not p or p[0].startswith("#"):
                    continue
                name, lport, rip, rport = (p + ["-"] * 4)[:4]
                nat = next((x[4:] for x in p[5:] if x.startswith("nat=")), "")
                out.append({"name": name, "lport": lport, "rip": rip, "rport": rport, "nat": nat})
    except OSError:
        pass
    return out


def wlan_gateway():
    rc, out = run(["ip", "-4", "route", "show", "table", "all"], timeout=5, sysenv=True)
    m = re.search(r"default via (\S+) dev %s\b" % STA_IF, out)
    return m.group(1) if m else ""


# -- repairs --------------------------------------------------------------------
def termux_service(action, script=None):
    argv = [SYS + "am", "startservice", "--user", "0", "-n", "com.termux/.app.TermuxService",
            "-a", action]
    if script:
        argv += ["-d", "com.termux.file://" + script, "--ez", "com.termux.execute.background", "true"]
    return run(argv, timeout=20, sysenv=True)


def take_wake_lock():
    return termux_service("com.termux.service_wake_lock")


def run_boot_script():
    """Termux's boot script starts whatever of sshd, meridian and Tablotron is missing."""
    return termux_service("com.termux.service_execute", BOOT)


def tailscale_connect():
    return run([SYS + "am", "broadcast", "--user", "0", "-n", "com.tailscale.ipn/.IPNReceiver",
                "-a", "com.tailscale.ipn.CONNECT_VPN"], timeout=20, sysenv=True)


def kill(pids):
    for p in pids:
        try:
            os.kill(p, signal.SIGKILL)
        except OSError:
            pass
    return 0, "killed %s" % (" ".join(map(str, pids)) or "nothing")


def wifi_toggle():
    """Turn the Wi-Fi client off and on. The hotspot stays up (seen on this tablet)."""
    rc, out = run([SYS + "cmd", "wifi", "set-wifi-enabled", "disabled"], timeout=20, sysenv=True)
    time.sleep(4)
    rc2, out2 = run([SYS + "cmd", "wifi", "set-wifi-enabled", "enabled"], timeout=20, sysenv=True)
    return rc or rc2, (out + " " + out2).strip()


def fix_tsu():
    try:
        with open(TSU) as f:
            s = f.read()
    except OSError as e:
        return 1, "cannot read tsu: %s" % e
    if TSU_NEW in s:
        return 0, "already patched"
    if TSU_OLD not in s:
        return 1, "tsu changed; patch it by hand (runbook Phase 5 step 4)"
    with open(TSU, "w") as f:
        f.write(s.replace(TSU_OLD, TSU_NEW, 1))
    return 0, "patched"


def notify(tag, title, text, screen=None):
    argv = [SYS + "cmd", "notification", "post", "-S", "bigtext", "-t", title]
    if screen:
        argv += ["-c", "activity", screen]   # the component is the intent spec's one free argument
    rc, out = run(argv + [tag, text], timeout=15, sysenv=True)
    if rc != 0 and screen:
        rc, out = run([SYS + "cmd", "notification", "post", "-S", "bigtext", "-t", title, tag, text],
                      timeout=15, sysenv=True)
    return rc, out


# -- one check ----------------------------------------------------------------
class Check:
    def __init__(self, cid, label, debounce=2, tablet_note=True, screen=None):
        self.id, self.label = cid, label
        self.status, self.msg, self.action = "unknown", "", ""
        self.since = None
        self.bad = 0
        self.debounce = debounce
        self.tablet_note = tablet_note
        self.screen = screen
        self.quiet = False          # a failure caused by another check: no notification
        self.dismissable = False    # an event that happened and was logged, not a live state --
                                    # the Tablotron page can offer to dismiss it; see dismiss_check()
        self.fixes = []
        self.attempts = 0
        self.last_fix = 0.0
        self.steps = {}
        self.noted = None
        self.noted_at = 0.0

    def set(self, status, msg, action="", quiet=False):
        if status == "ok":
            self.bad, self.since, self.attempts, self.steps = 0, None, 0, {}
        else:
            self.bad += 1
            if self.since is None:
                self.since = now()
        if status != self.status:
            log("%s: %s -> %s: %s%s" % (self.id, self.status, status, msg, (" | " + action) if action else ""))
        self.status, self.msg, self.action, self.quiet = status, msg, action, quiet

    def age(self):
        return 0 if self.since is None else now() - self.since

    def may_fix(self, cooldown, most=3):
        return self.attempts < most and now() - self.last_fix >= cooldown

    def step_due(self, name, after, every):
        """A ladder step: due once the failure is `after` s old, then every `every` s."""
        return self.age() >= after and now() - self.steps.get(name, 0) >= every

    def did(self, what, result, step=None):
        rc, out = result
        self.attempts += 1
        self.last_fix = now()
        if step:
            self.steps[step] = now()
        self.fixes = (self.fixes + ["%s %s (rc %d)" % (time.strftime("%H:%M:%S"), what, rc)])[-5:]
        log("%s: repair: %s -> rc %d %s" % (self.id, what, rc, out[:300].replace("\n", " ")))

    def as_dict(self):
        return {"id": self.id, "label": self.label, "status": self.status, "msg": self.msg,
                "action": self.action, "since": self.since, "fixes": self.fixes,
                "quiet": self.quiet, "dismissable": self.dismissable}


# -- the watcher --------------------------------------------------------------
class Watch:
    def __init__(self, repair=True):
        self.repair = repair
        mk = Check
        self.checks = [
            mk("wifi", "Home Wi-Fi"),
            mk("internet", "Internet"),
            mk("hotspot", "Hotspot", debounce=2, screen=HOTSPOT_SCREEN),
            mk("tailscale", "Tailscale"),
            mk("termux", "Termux"),
            mk("sshd", "sshd (port 8022)"),
            mk("forwards", "Forwards (meridian)"),
            mk("tablotron", "Tablotron page (port 8095)", debounce=3),
            mk("sensors_helper", "Sensors helper (accel, location)", debounce=3),
            mk("nat", "NAT addresses and names"),
            mk("peers", "Tailscale peers", debounce=1, tablet_note=False),
            mk("samba", "SSD and Samba", debounce=1),
            mk("sdbuffer", "SD buffer", debounce=1),
            mk("ssd_temp", "SSD temperature", debounce=1),
            mk("ssd_safety", "SSD removal safety", debounce=1),
            mk("power", "Battery", debounce=1),
            mk("android", "Android settings", debounce=1),
        ]
        self.c = {k.id: k for k in self.checks}
        self.full, self.full_t = {}, 0.0
        self.full_busy = False
        self.slow_t = self.peers_t = 0.0
        self.info = {}
        self.ssid = None
        self.wakelocks = None
        self.banner_ok = None
        self.dns = None
        self.peer_res = {}
        self.termux_pid = None
        self.last_ap_ip = None
        self.nat_t = 0.0
        self.nat_res = (0, "")
        self.toggle_t = 0.0
        self.started = now()
        self.sample_t = 0.0
        self.ssd_present_prev = None
        self.sdbuf_t = 0.0
        self.sdbuf_busy = False
        self.meeting_t, self.meeting_busy = 0.0, False
        self.ssd_ejected_at_present = None
        self.ssd_last_disconnect_t, self.ssd_last_disconnect_safe = self.last_disconnect()

    # ---- data ----
    def refresh_full(self):
        rc, out = run(["sh", ROOT_SH, "status"], timeout=45)
        s = {}
        for line in out.splitlines():
            k, sep, v = line.partition("=")
            if sep and k.isidentifier():
                s[k] = v.strip()
        if rc == 0 and s.get("uid") == "0":
            self.full = s
        else:
            log("root.sh status failed rc %d: %s" % (rc, out[-300:]))
        self.full_t = now()

    def kick_full(self):
        """root.sh status can take up to 45 s: run it beside the 5 s loop, never inside it."""
        if self.full_busy:
            return
        self.full_busy = True

        def go():
            try:
                self.refresh_full()
            finally:
                self.full_busy = False
        threading.Thread(target=go, daemon=True, name="full").start()

    def kick_sdbuf(self):
        """sdbuffer.py tick beside the 5 s loop: open the microSD buffer, point the drive share
        at the SSD or the buffer, start moving buffered files onto the SSD (in its own process)."""
        self.sdbuf_t = now()
        if self.sdbuf_busy or not os.path.exists(SDBUFFER):
            return
        self.sdbuf_busy = True

        def go():
            try:
                # long enough for e2fsck on the whole card after an unclean stop (sdbuffer.py gives it 30 min)
                rc, out = run([sys.executable, SDBUFFER, "tick"], timeout=1900)
                if rc:
                    log("sdbuffer.py tick rc %d: %s" % (rc, out[-300:]))
            finally:
                self.sdbuf_busy = False
        threading.Thread(target=go, daemon=True, name="sdbuf").start()

    def meeting_catchup(self, S, t):
        """End a Meeting power level whose end passed with nothing waiting for it (the tablet
        restarted during the meeting). root.sh's own detached waiter ends a meeting on the
        minute, so this only steps in 2 minutes late, and at most every 5 minutes."""
        left = S.get("meeting_left_s", "")
        if not self.repair or self.meeting_busy or not re.fullmatch(r"-?[0-9]+", left) or int(left) > -120:
            return
        if t - self.meeting_t < 300:
            return
        self.meeting_t, self.meeting_busy = t, True

        def go():
            try:
                rc, out = run(["sh", ROOT_SH, "power-meeting-end"], timeout=60)
                log("meeting ended late (its end passed with nothing waiting), rc %d: %s" % (rc, out[-300:]))
                self.full_t = 0              # read the status again on the next pass
            finally:
                self.meeting_busy = False
        threading.Thread(target=go, daemon=True, name="meeting").start()

    def refresh_slow(self):
        rc, out = run([SYS + "dumpsys", "power"], timeout=15, sysenv=True)
        self.wakelocks = sum(1 for l in out.splitlines() if "PARTIAL_WAKE_LOCK" in l and "termux" in l.lower()) if rc == 0 else None
        rc, out = run([SYS + "cmd", "wifi", "status"], timeout=15, sysenv=True)
        m = re.search(r'Wifi is connected to "(.*)"', out)
        self.ssid = m.group(1) if m else ""
        self.banner_ok = tcp("127.0.0.1", 8022, timeout=3, banner=b"SSH-") is not None
        self.dns = dns_ok()
        self.slow_t = now()

    def refresh_peers(self, tg):
        res = {}
        for t in tg:
            if t["rip"].startswith("100.") and t["rport"].isdigit():
                res[t["name"]] = tcp(t["rip"], t["rport"], timeout=3)
        self.peer_res = res
        self.peers_t = now()

    def termux_state(self):
        """(running, frozen) of the com.termux app process."""
        if not self.termux_pid or proc_cmd(self.termux_pid)[:1] != ["com.termux"]:
            rc, out = run([SYS + "pidof", "com.termux"], timeout=5, sysenv=True)
            self.termux_pid = out.split()[0] if rc == 0 and out.split() else None
        if not self.termux_pid:
            return False, False
        try:
            with open("/proc/%s/cgroup" % self.termux_pid) as f:
                for line in f:
                    if line.startswith("0::"):
                        with open("/sys/fs/cgroup" + line.strip()[3:] + "/cgroup.events") as e:
                            return True, "frozen 1" in e.read()
        except OSError:
            pass
        return True, False

    # ---- history: 1-minute samples and the SSD disconnect log ----
    def last_disconnect(self):
        """(t, safe) of the most recently logged SSD disconnect, read at startup -- or (None, None)."""
        try:
            con = history_db()
            row = con.execute("SELECT t, safe FROM ssd_events WHERE event='disconnect' ORDER BY t DESC LIMIT 1").fetchone()
            con.close()
            if row is None:
                return None, None
            return row[0], (None if row[1] is None else bool(row[1]))
        except sqlite3.Error:
            return None, None

    def log_ssd_event(self, event, safe):
        t = int(now())
        try:
            con = history_db()
            con.execute("INSERT OR IGNORE INTO ssd_events (t, event, safe) VALUES (?, ?, ?)",
                        (t, event, None if safe is None else int(safe)))
            con.commit()
            con.close()
            own(HISTORY_DB)
        except sqlite3.Error as e:
            log("ssd_events insert failed: %s" % e)
        return t

    def ssd_track(self, S):
        """A disconnect is 'present' going false: either the drive vanished while active (unsafe), or it
        was already de-authorized by Eject and then unplugged (safe). Pressing Eject alone never fires
        this -- the drive stays 'present' (de-authorized but still plugged in) until it is actually pulled."""
        present = bool(S.get("ssd_disk")) or S.get("ssd_ejected") == "yes"
        if self.ssd_present_prev is True and present is False:
            safe = self.ssd_ejected_at_present == "yes"
            self.ssd_last_disconnect_t = self.log_ssd_event("disconnect", safe)
            self.ssd_last_disconnect_safe = safe
        elif self.ssd_present_prev is False and present is True:
            self.log_ssd_event("reconnect", None)
        self.ssd_present_prev = present
        if present:
            self.ssd_ejected_at_present = S.get("ssd_ejected")

    def check_sdbuffer(self, S):
        """The microSD buffer, from sdbuffer.py status (the sdbuf_* lines of root.sh status)."""
        k = self.c["sdbuffer"]
        state, target = S.get("sdbuf"), S.get("sdbuf_target")
        size_k, avail_k = int(S.get("sdbuf_size_k") or 0), int(S.get("sdbuf_avail_k") or 0)
        files, held = int(S.get("sdbuf_files") or 0), int(S.get("sdbuf_bytes") or 0)
        room = "%s free of %s" % (gb(avail_k << 10), gb(size_k << 10))
        waits = "%d files (%s) wait for the SSD" % (files, gb(held)) if files else "empty"
        young = k.age() < 180            # a state the next sdbuffer.py tick changes: no notification yet
        ssd_up = S.get("ssd_mounted") == "yes" and bool(S.get("ssd_disk"))
        if not state or state == "none":
            k.set("ok", "not set up" if state else "not installed", quiet=True)
        elif state == "absent":
            k.set("warn", "The buffer's microSD card is not in the tablet.",
                  "Put it back: until then nothing takes new files while the SSD is away.")
        elif state == "error":
            k.set("fail", "The buffer's microSD card stopped answering while it was open.",
                  "Push the card in firmly; the watch closes the buffer and opens it again once the card answers.")
        elif S.get("sdbuf_key") != "yes":
            k.set("fail", "The buffer's key /data/adb/sdbuffer.key is missing, so the card cannot be opened.",
                  "Format the card again: sudo python3 ~/tabctl/sdbuffer.py format --again (what is on it is lost).")
        elif state == "ready":
            k.set("warn", "The buffer's microSD card is in but not open.",
                  "The watch opens it within a minute; if it stays closed, run sudo python3 ~/tabctl/sdbuffer.py open.",
                  quiet=young)
        elif target == "buffer" and avail_k < 1 << 20:
            k.set("fail", "The buffer is nearly full: %s." % room, "Unlock the SSD, so the files can move onto it.")
        elif S.get("sdbuf_ssd_full") == "yes" and files:
            k.set("fail", "The SSD is full: %s." % waits, "Free space on the SSD; the files move as soon as there is room.")
        elif target in ("none", "other") or (target == "ssd" and not ssd_up):
            k.set("warn", "The drive share points at %s." % {"none": "nothing", "ssd": "the SSD, which is away"}.get(target, "an unknown folder"),
                  "The watch points it again within a minute; if it stays, run sudo python3 ~/tabctl/sdbuffer.py point.",
                  quiet=young)
        elif S.get("sdbuf_error"):
            k.set("warn", "The last move left files in the buffer: %s" % S["sdbuf_error"],
                  "Read /data/local/ssd/sdbuffer.log on the tablet.")
        elif target == "buffer" and avail_k < size_k // 10:
            k.set("warn", "The buffer is filling up: %s." % room, "Unlock the SSD, so the files can move onto it.")
        else:
            doing = "takes new files while the SSD is away" if target == "buffer" else (
                "moving files onto the SSD" if S.get("sdbuf_syncing") == "yes" else "standing by, the share is on the SSD")
            k.set("ok", "%s; %s; %s" % (doing, waits, room))

    def sample(self, S):
        def i(v):
            try:
                return int(v)
            except (TypeError, ValueError):
                return None
        sens = read_sensors()   # from sensors_helper.py's own process; see read_sensors()
        row = (int(now()), self.ssid or None, i(S.get("sta_freq")), i(S.get("sta_rssi")), i(S.get("sta_link")),
               i(S.get("ap_freq")), i(S.get("ap_bw")), i(S.get("ap_clients")), i(self.info.get("internet_ms")),
               i(self.info.get("bat_level")), self.info.get("bat_temp_c"),
               int(S.get("bat_ac") == "true" or S.get("bat_usb") == "true"),
               int(bool(S.get("ssd_disk"))), self.info.get("ssd_temp_c"),
               int(S.get("ssd_open") == "yes"), int(S.get("ssd_mounted") == "yes"), self.info.get("ssd_used_pct"),
               self.info.get("int_used_pct"), self.info.get("sd_used_pct"),
               self.wakelocks, int(bool(self.info.get("termux_frozen"))), S.get("power_level"),
               sens.get("accel_pp"), sens.get("lat"), sens.get("lon"), sens.get("loc_acc_m"))
        try:
            con = history_db()
            con.execute("INSERT OR IGNORE INTO samples VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
            cutoff = int(now()) - HISTORY_DAYS * 86400
            con.execute("DELETE FROM samples WHERE t < ?", (cutoff,))
            con.execute("DELETE FROM ssd_events WHERE t < ?", (cutoff,))
            con.commit()
            con.close()
            own(HISTORY_DB)
        except sqlite3.Error as e:
            log("samples insert failed: %s" % e)

    # ---- one pass ----
    def tick(self):
        t = now()
        c = self.c
        tg = targets()
        if not self.full and not self.full_t:
            self.refresh_full()              # the first pass needs it
        elif t - self.full_t >= FULL_EVERY:
            self.kick_full()
        S = self.full
        self.ssd_track(S)
        gone = bool(S.get("ssd_disk")) and not os.path.exists("/sys/block/" + S["ssd_disk"])
        if gone and t - self.full_t > 10:
            self.kick_full()                 # the SSD was just pulled: see it now, not in a minute
        if self.repair and t - self.sdbuf_t >= (10 if gone else FULL_EVERY):
            self.kick_sdbuf()                # ...and let the SD buffer take the share
        self.meeting_catchup(S, t)
        ap_if = S.get("ap_if", "")
        ap_ip = ifaddr(ap_if) if ap_if else ""
        if ap_if and not ap_ip and t - self.full_t > 10:
            self.kick_full()                 # the hotspot may have gone or moved: a later pass sees it
        if not ap_if and t - self.full_t > 20:
            self.kick_full()                 # see a hotspot that came back within 20 s
        if t - self.slow_t >= SLOW or any(c[k].status in ("fail", "unknown") for k in ("termux", "sshd", "wifi")):
            if t - self.slow_t >= 10:
                self.refresh_slow()
        if t - self.peers_t >= PEERS_EVERY:
            self.refresh_peers(tg)

        ports = listening()
        running, frozen = self.termux_state()
        sta_ip = ifaddr(STA_IF)
        sta_up = operstate(STA_IF) == "up" and bool(sta_ip)
        ts_if, ts_ip = tailscale_if()
        net_ms = None
        for host, port in INTERNET:
            net_ms = tcp(host, port, timeout=2)
            if net_ms is not None:
                break
        ap_freq = int(S.get("ap_freq") or 0)
        dfs = 5250 <= ap_freq <= 5730

        # Termux
        if not running:
            c["termux"].set("fail", "Termux is not running.", "Open Termux on the tablet.")
        elif frozen:
            c["termux"].set("fail", "Android froze Termux: sshd, the forwards and Tablotron stop answering.",
                            "Open Termux on the tablet and run termux-wake-lock.")
        elif self.wakelocks == 0:
            c["termux"].set("fail", "Termux holds no wake lock, so Android may freeze it.",
                            "Open Termux on the tablet and run termux-wake-lock.")
        else:
            c["termux"].set("ok", "running, wake lock held")
        termux_bad = c["termux"].status == "fail"

        # sshd
        if 8022 not in ports:
            c["sshd"].set("fail", "Nothing listens on port 8022.", "Open Termux on the tablet and run sshd.", quiet=termux_bad)
        elif self.banner_ok is False:
            c["sshd"].set("fail", "sshd listens but does not answer.", "Open Termux on the tablet and run: pkill -9 -x sshd; sshd", quiet=termux_bad)
        else:
            c["sshd"].set("ok", "answering on 8022")

        # forwards
        want = [int(x["lport"]) for x in tg if x["lport"].isdigit() and not x["rip"].startswith(("usb:", "/"))] + [8080]
        missing = [p for p in want if p not in ports]
        if missing:
            c["forwards"].set("fail", "Not listening: %s." % ", ".join(map(str, missing)),
                              "Press Restart meridian in Tablotron.", quiet=termux_bad)
        else:
            c["forwards"].set("ok", "listening on %s" % ", ".join(map(str, want)))

        # Tablotron page
        if 8095 not in ports:
            c["tablotron"].set("warn", "Tablotron's page is not running.", "Open Termux on the tablet and run the boot script: ~/.termux/boot/10-start.sh", quiet=termux_bad)
        else:
            c["tablotron"].set("ok", "listening on 8095")

        # Sensors helper (accelerometer, location): a separate long-lived process, not spawned by
        # this daemon -- see read_sensors(). Liveness is "it wrote something recently", regardless
        # of whether that reading itself has an accel/location value (a dead sensor permission is
        # not something a restart fixes).
        sh_sens = read_sensors()
        if not sh_sens:
            c["sensors_helper"].set("warn", "No fresh reading from the sensors helper.",
                                     "Open Termux on the tablet and run the boot script: ~/.termux/boot/10-start.sh", quiet=termux_bad)
        else:
            bits = [n for n, ok in (("accel", sh_sens.get("accel_pp") is not None),
                                     ("location", sh_sens.get("lat") is not None)) if ok]
            c["sensors_helper"].set("ok", ("running (%s)" % ", ".join(bits)) if bits else "running, no readings yet")

        # home Wi-Fi
        name = ('"%s"' % self.ssid) if self.ssid else "the home Wi-Fi"
        if sta_up:
            extra = ", %s dBm" % S["sta_rssi"] if S.get("sta_rssi") else ""
            c["wifi"].set("ok", "connected to %s (%s%s)" % (name, sta_ip, extra))
        else:
            act = "Turn Mobile Hotspot off and on on the tablet: that frees the Wi-Fi radio to rejoin the home network. If it still fails, check the home router."
            if dfs:
                act += " The hotspot is on DFS channel %d, which ties the radio to that channel." % ((ap_freq - 5000) // 5)
            c["wifi"].set("fail", "The tablet is not connected to the home Wi-Fi.", act)

        # internet
        if net_ms is not None and self.dns is False:
            c["internet"].set("warn", "Addresses answer but DNS does not resolve.", "Check the home router's DNS.")
        elif net_ms is not None:
            c["internet"].set("ok", "reachable (%d ms)" % net_ms)
        elif not sta_up:
            c["internet"].set("fail", "No internet: the tablet is off the home Wi-Fi.", c["wifi"].action, quiet=True)
        else:
            gw = wlan_gateway()
            gw_ok = bool(gw) and run([SYS + "ping", "-c", "1", "-W", "2", gw], timeout=6, sysenv=True)[0] == 0
            if gw_ok:
                c["internet"].set("fail", "The home Wi-Fi works but has no internet (router %s answers)." % gw,
                                  "Check the home router and its internet connection.")
            else:
                c["internet"].set("fail", "The home router%s does not answer." % ((" " + gw) if gw else ""),
                                  "Check the home router. If it is fine, turn Mobile Hotspot off and on on the tablet.")

        # hotspot
        if not ap_if:
            c["hotspot"].set("fail", "The hotspot is off: the Mac and other clients have no network.",
                             "Turn Mobile Hotspot on (tap this notification).")
        else:
            band = "2.4 GHz" if ap_freq and ap_freq < 3000 else "5 GHz"
            desc = "on, %s channel %s, %s clients, %s" % (band, (ap_freq - 2407) // 5 if ap_freq < 3000 else (ap_freq - 5000) // 5,
                                                         S.get("ap_clients", "?"), ap_ip or "no address")
            if ap_freq and ap_freq < 3000:
                c["hotspot"].set("warn", "The hotspot runs on 2.4 GHz (slower).", "Open hotspot settings and choose 5 GHz, then turn the hotspot off and on.")
            elif S.get("ap_cfg_autooff") == "true":
                c["hotspot"].set("warn", "The hotspot turns itself off when nobody is connected.", "Open hotspot settings and turn that off.")
            else:
                c["hotspot"].set("ok", desc)
        # Tailscale
        if ts_ip:
            c["tailscale"].set("ok", "up, %s on %s" % (ts_ip, ts_if))
        elif net_ms is None:
            c["tailscale"].set("warn", "Tailscale has no address; it waits for internet.", "", quiet=True)
        else:
            c["tailscale"].set("fail", "Tailscale is not connected.", "Open the Tailscale app on the tablet and connect.")

        # Tailscale peers (Macotron tells the user; no tablet notification)
        down = [k for k, v in self.peer_res.items() if v is None]
        if not self.peer_res:
            c["peers"].set("ok", "no Tailscale targets")
        elif not ts_ip or net_ms is None:
            c["peers"].set("warn", "Not checked: Tailscale or internet is down.", "", quiet=True)
        elif len(down) == len(self.peer_res):
            c["peers"].set("warn", "No Tailscale peer answers: %s." % ", ".join(down), "Check that those machines are on and connected to Tailscale.")
        elif down:
            c["peers"].set("warn", "%s does not answer (switched off?)." % ", ".join(down), "Check that it is on and connected to Tailscale.")
        else:
            c["peers"].set("ok", ", ".join("%s %d ms" % (k, v) for k, v in self.peer_res.items()))

        # NAT addresses and names
        if not any(x["nat"] for x in tg):
            c["nat"].set("ok", "no NAT targets")
        elif not ap_if:
            c["nat"].set("warn", "Waits for the hotspot.", "", quiet=True)
        else:
            if t - self.nat_t >= 15 or c["nat"].status != "ok" or ap_ip != self.last_ap_ip:
                self.nat_res = run(["sh", NAT_UP, "status"], timeout=20)
                self.nat_t = t
            rc, out = self.nat_res
            if rc == 0 and "No NAT applied" not in out:
                c["nat"].set("ok", "; ".join(l for l in out.splitlines() if "mDNS" in l) or "applied")
            else:
                c["nat"].set("fail", (out.splitlines() or ["not applied"])[-1][:160], "Run sudo ~/bin/nat-up.sh on the tablet and read its output.")

        if ap_ip and self.last_ap_ip and ap_ip != self.last_ap_ip:
            log("hotspot address changed %s -> %s" % (self.last_ap_ip, ap_ip))
        if ap_ip:
            self.last_ap_ip = ap_ip

        # full-status checks
        if S:
            ssd_up = S.get("ssd_mounted") == "yes" and bool(S.get("ssd_disk"))   # a pulled SSD leaves its mount behind
            buffer_serves = S.get("sdbuf") == "open" and S.get("sdbuf_target") == "buffer"
            meanwhile = " The SD buffer takes new files meanwhile." if buffer_serves else ""
            down = [i for i in ("tunnel", "lan") if S.get("smb_" + i) != "up"]
            if down and (ssd_up or buffer_serves):
                c["samba"].set("fail", "Samba (%s) is down." % ", ".join(down), "Press Restart Samba in Tablotron.")
            elif ssd_up:
                c["samba"].set("ok", "SSD mounted, both Samba servers up")
            elif S.get("ssd_ejected") == "yes":
                c["samba"].set("warn", "The SSD is ejected." + meanwhile, "Unplug it, or press Reconnect in Tablotron's SSD card.")
            elif not S.get("ssd_disk"):
                c["samba"].set("warn", "The SSD is not detected." + meanwhile, "Check its power supply and cable.")
            elif S.get("ssd_open") != "yes":
                c["samba"].set("warn", "The SSD is locked." + meanwhile, "Type the passphrase in Tablotron's SSD card.")
            else:
                c["samba"].set("fail", "The SSD is unlocked but not mounted." + meanwhile, "Press Unlock again in Tablotron (it runs drive-up.sh).")
            self.check_sdbuffer(S)
            if S.get("ssd_temp_c"):
                st = int(S["ssd_temp_c"])
                if st >= 65:
                    c["ssd_temp"].set("fail", "SSD is hot: %d C." % st, "Give the drive air; check its enclosure's fan or airflow.")
                elif st >= 55:
                    c["ssd_temp"].set("warn", "SSD is warm: %d C." % st, "Keep an eye on it; give it air if it keeps climbing.")
                else:
                    c["ssd_temp"].set("ok", "%d C" % st)
            else:
                c["ssd_temp"].set("ok", "not available (SSD not detected, or smartctl failed)", quiet=True)
            dismissed = read_dismissed().get("ssd_safety")
            if self.ssd_last_disconnect_safe is False and dismissed != self.ssd_last_disconnect_t:
                c["ssd_safety"].dismissable = True
                c["ssd_safety"].set("warn", "The SSD was last removed without pressing Eject first.",
                                    "Always press Eject in the SSD card before unplugging it.")
            else:
                c["ssd_safety"].dismissable = False
                c["ssd_safety"].set("ok", "no unsafe disconnects on record", quiet=True)
            powered = S.get("bat_ac") == "true" or S.get("bat_usb") == "true"
            lvl = int(S.get("bat_level") or 0)
            temp = int(S.get("bat_temp") or 0)
            if not powered and lvl < 15:
                c["power"].set("fail", "Battery at %d%% and not charging." % lvl, "Plug the charger in.")
            elif not powered and lvl < 30:
                c["power"].set("warn", "Battery at %d%% and not charging." % lvl, "Plug the charger in.")
            elif temp > 450:
                c["power"].set("warn", "Battery is hot: %.1f C." % (temp / 10), "Give the tablet air; take it out of any case.")
            else:
                c["power"].set("ok", "%d%%, %s, %.1f C" % (lvl, "charging" if powered else "on battery", temp / 10))
            drift = []
            if S.get("phantom") not in ("false", ""):
                drift.append("phantom process monitor on")
            if S.get("idle_termux") != "yes" or S.get("idle_tailscale") != "yes":
                drift.append("battery exemption missing")
            if S.get("tsu_patched") != "yes":
                drift.append("tsu lost its patch")
            if drift:
                c["android"].set("warn", "; ".join(drift) + ".", "Press Re-apply Android settings in Tablotron.")
            else:
                c["android"].set("ok", "phantom monitor off, exemptions set, tsu patched")

        def _num(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        def _pct(used_key, size_key):
            used, size = _num(S.get(used_key)), _num(S.get(size_key))
            return round(used * 100 / size, 1) if size else None
        bat_temp_t = _num(S.get("bat_temp"))
        self.info = {"ssid": self.ssid, "sta_ip": sta_ip, "sta_freq": S.get("sta_freq"), "sta_rssi": S.get("sta_rssi"),
                     "ap_if": ap_if, "ap_ip": ap_ip, "ap_freq": ap_freq, "ap_clients": S.get("ap_clients"),
                     "ts_ip": ts_ip, "internet_ms": net_ms, "dns": self.dns, "termux_frozen": frozen,
                     "wakelocks": self.wakelocks, "peers": self.peer_res, "full_age": int(now() - self.full_t),
                     "bat_level": _num(S.get("bat_level")), "bat_temp_c": bat_temp_t / 10 if bat_temp_t is not None else None,
                     "ssd_temp_c": _num(S.get("ssd_temp_c")), "ssd_used_pct": _pct("ssd_used_k", "ssd_size_k"),
                     "ssd_used_k": _num(S.get("ssd_used_k")), "ssd_size_k": _num(S.get("ssd_size_k")),
                     "int_used_pct": _pct("int_used_k", "int_size_k"),
                     "int_used_k": _num(S.get("int_used_k")), "int_size_k": _num(S.get("int_size_k")),
                     "sd_present": S.get("sd_present") == "yes", "sd_used_pct": _pct("sd_used_k", "sd_size_k"),
                     "sd_used_k": _num(S.get("sd_used_k")), "sd_size_k": _num(S.get("sd_size_k"))}
        if t - self.sample_t >= FULL_EVERY:
            self.sample_t = t   # set before starting: a stuck accelerometer call must never stall this loop
            threading.Thread(target=self.sample, args=(S,), daemon=True, name="sample").start()
        if self.repair:
            self.repairs(frozen, sta_up, net_ms, dfs)
        self.notify_changes()

    # ---- repairs ----
    def repairs(self, frozen, sta_up, net_ms, dfs):
        c = self.c
        tm = c["termux"]
        if tm.status == "fail" and tm.bad >= 1 and tm.may_fix(15, most=4):
            tm.did("take the Termux wake lock (wakes the app)", take_wake_lock())
            self.slow_t = 0                     # look again on the next pass
        termux_ok = tm.status == "ok"

        ss = c["sshd"]
        if termux_ok and ss.status == "fail" and ss.bad >= 2 and ss.may_fix(45):
            if "does not answer" in ss.msg:
                pids = pids_where(lambda cmd, uid: uid == TUSER and os.path.basename(cmd[0]) == "sshd")
                ss.did("kill the stuck sshd", kill(pids))
                time.sleep(1)
            ss.did("run the Termux boot script (starts sshd)", run_boot_script())
            self.slow_t = 0

        fw = c["forwards"]
        if termux_ok and fw.status == "fail" and fw.bad >= 3 and fw.may_fix(120, most=2):
            pids = pids_where(lambda cmd, uid: uid == TUSER and (
                any(a.endswith("meridian.sh") for a in cmd) or
                (os.path.basename(cmd[0]) == "socat" and any("TCP4-LISTEN" in a for a in cmd)) or
                "meridian-proxy" in " ".join(cmd)))
            fw.did("stop meridian", kill(pids))
            time.sleep(1)
            fw.did("run the Termux boot script (starts meridian)", run_boot_script())

        tt = c["tablotron"]
        if termux_ok and tt.status != "ok" and tt.bad >= 3 and tt.may_fix(120, most=2):
            tt.did("run the Termux boot script (starts Tablotron)", run_boot_script())

        sh = c["sensors_helper"]
        if termux_ok and sh.status != "ok" and sh.bad >= 3 and sh.may_fix(120, most=2):
            sh.did("run the Termux boot script (starts the sensors helper)", run_boot_script())

        wf = c["wifi"]
        if wf.status == "fail":
            if wf.step_due("scan", 15, 60):
                wf.did("start a Wi-Fi scan", run([SYS + "cmd", "wifi", "start-scan"], timeout=15, sysenv=True), step="scan")
            if wf.step_due("toggle", 90, 600) and now() - self.toggle_t >= 600:
                self.toggle_t = now()
                wf.did("turn the Wi-Fi client off and on", wifi_toggle(), step="toggle")

        it = c["internet"]
        if it.status == "fail" and sta_up and "does not answer" in it.msg and it.step_due("toggle", 120, 600) and now() - self.toggle_t >= 600:
            self.toggle_t = now()
            it.did("turn the Wi-Fi client off and on", wifi_toggle(), step="toggle")

        ts = c["tailscale"]
        if ts.status == "fail" and ts.bad >= 2 and ts.may_fix(30):
            ts.did("ask Tailscale to connect", tailscale_connect())

        nt = c["nat"]
        if nt.status == "fail" and nt.may_fix(15):
            nt.did("re-apply NAT and names (nat-up.sh)", run(["sh", NAT_UP], timeout=60))

        sb = c["samba"]
        if sb.status == "fail" and "Samba" in sb.msg and sb.may_fix(600, most=2):
            sb.did("restart Samba (root.sh samba-restart)", run(["sh", ROOT_SH, "samba-restart"], timeout=300))
            self.full_t = 0

        an = c["android"]
        if an.status == "warn" and an.may_fix(3600, most=2):
            if "tsu" in an.msg:
                an.did("patch tsu", fix_tsu())
            if "phantom" in an.msg or "exemption" in an.msg:
                an.did("re-apply Android settings (root.sh fix-android)", run(["sh", ROOT_SH, "fix-android"], timeout=60))
            self.full_t = 0

    # ---- notifications ----
    def notify_changes(self):
        for k in self.checks:
            if not k.tablet_note:
                continue
            tag = "tablotron_" + k.id
            if k.status == "fail" and not k.quiet and k.bad >= k.debounce:
                if k.noted != "fail" or now() - k.noted_at >= RENOTIFY:
                    self._post(k, tag, "%s: problem" % k.label, "%s %s" % (k.msg, k.action))
                    k.noted = "fail"
            elif k.status == "warn" and not k.quiet and k.age() >= 60 and k.noted is None:
                self._post(k, tag, "%s: warning" % k.label, "%s %s" % (k.msg, k.action))
                k.noted = "warn"
            elif k.status == "ok" and k.noted:
                self._post(k, tag, "%s: back to normal" % k.label, k.msg)
                k.noted = None

    def _post(self, k, tag, title, text):
        rc, out = notify(tag, "Tablotron - " + title, text.strip(), screen=k.screen if k.status != "ok" else None)
        k.noted_at = now()
        log("notify %s: %s | %s (rc %d)" % (tag, title, text.strip()[:200], rc))

    # ---- state ----
    def state(self):
        return {"version": 1, "t": now(), "time": time.strftime("%Y-%m-%d %H:%M:%S"), "pid": os.getpid(),
                "tick": TICK, "started": self.started, "repair": self.repair,
                "checks": [k.as_dict() for k in self.checks], "info": self.info}

    def write(self, stopped=False):
        st = self.state()
        if stopped:
            st["stopped"] = True
        tmp = STATE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f, indent=1)
        own(tmp)
        os.replace(tmp, STATE)


# -- commands -----------------------------------------------------------------
def running_pid():
    try:
        with open(PIDF) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return None
    cmd = proc_cmd(pid)
    return pid if any(a.endswith("watch.py") for a in cmd) and "run" in cmd else None


def cmd_run():
    os.makedirs(CFG, exist_ok=True)
    if running_pid() not in (None, os.getpid()):
        print("already running (pid %d)" % running_pid())
        return 0
    with open(PIDF, "w") as f:
        f.write(str(os.getpid()))
    own(PIDF)
    w = Watch(repair=True)
    stop = []
    signal.signal(signal.SIGTERM, lambda *a: stop.append(1))
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    log("watch started (pid %d)" % os.getpid())
    while not stop:
        t0 = time.monotonic()
        try:
            w.tick()
            w.write()
        except Exception:
            log("tick failed: " + traceback.format_exc().replace("\n", " | ")[-800:])
        while not stop and time.monotonic() - t0 < TICK:
            time.sleep(0.5)
    w.write(stopped=True)
    log("watch stopped")
    return 0


def cmd_start():
    pid = running_pid()
    if pid:
        print("Tablotron watch is already running (pid %d)." % pid)
        return 0
    os.makedirs(CFG, exist_ok=True)
    try:
        os.chown(CFG, TUSER, TUSER)
    except OSError:
        pass
    p = subprocess.Popen([sys.executable, os.path.abspath(__file__), "run"], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=open(LOG, "a"), start_new_session=True)
    time.sleep(1.5)
    if p.poll() is None:
        print("Tablotron watch started (pid %d)." % p.pid)
        return 0
    print("Tablotron watch failed to start; see %s" % LOG)
    return 1


def cmd_stop():
    pid = running_pid()
    if not pid:
        print("Tablotron watch is not running.")
        return 0
    os.kill(pid, signal.SIGTERM)
    for _ in range(40):
        if not running_pid():
            print("Tablotron watch stopped.")
            return 0
        time.sleep(0.25)
    print("Tablotron watch did not stop (pid %d)." % pid)
    return 1


def show(st):
    age = now() - st.get("t", 0)
    print("Tablotron watch, state from %s (%d s ago)%s" % (st.get("time", "?"), age, "  [stopped]" if st.get("stopped") else ""))
    for k in st.get("checks", []):
        line = "  %-4s %-28s %s" % (k["status"].upper(), k["label"], k["msg"])
        if k["status"] != "ok" and k.get("action"):
            line += "  -> " + k["action"]
        print(line)
        for fx in k.get("fixes") or []:
            print("         repaired: " + fx)


def cmd_once():
    w = Watch(repair=False)
    w.tick()
    show(w.state())
    return 0


def cmd_status():
    try:
        with open(STATE) as f:
            show(json.load(f))
    except (OSError, ValueError) as e:
        print("No state yet (%s)." % e)
        return 1
    st = json.load(open(STATE))
    fresh = not st.get("stopped") and now() - st.get("t", 0) < 4 * TICK
    print("running (pid %s)" % st.get("pid") if fresh else "NOT running (state is stale)")
    return 0 if fresh else 1


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd != "status" and os.geteuid() != 0:
        print("Run me as root: su -c 'python3 %s %s'" % (os.path.abspath(__file__), cmd))
        return 2
    return {"run": cmd_run, "start": cmd_start, "stop": cmd_stop, "once": cmd_once,
            "status": cmd_status}.get(cmd, lambda: print(__doc__) or 2)()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
