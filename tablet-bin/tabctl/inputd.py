#!/data/data/com.termux/files/usr/bin/python3
"""inputd.py - the Mac's keyboard and mouse on this tablet (runs as root).

A virtual mouse and a virtual keyboard are made through /dev/uinput, so Android treats them like
a real USB mouse (with its pointer) and keyboard. The Mac side (Macotron's tabinput helper) sends
the Mac's events over a TCP connection to 127.0.0.1:PORT; the Mac reaches that port through an SSH
tunnel, so nothing else on the network sees it. Other apps on the tablet could connect too, which
is why the first line must carry the token in TOKEN_FILE (root-only).

  sudo python3 ~/tabctl/inputd.py start     start detached (nothing to do if it already runs)
  sudo python3 ~/tabctl/inputd.py stop      stop it
  sudo python3 ~/tabctl/inputd.py status    key=value lines: inputd, inputd_clients, inputd_port
  sudo python3 ~/tabctl/inputd.py token     print the token (the Mac asks for it over ssh)
  sudo python3 ~/tabctl/inputd.py serve     run in the foreground

Protocol, one line per event, answered with "OK" or "ERR text":
  HELLO <token>          first line
  M dx dy                relative mouse motion
  B <left|right|middle> 0|1     button up or down
  W dx dy                scroll: dy > 0 scrolls up, dx > 0 scrolls right (in notches)
  K <linux key code> 0|1|2      key up, down, repeat
  U                      release every key and button and lift the touch (the Mac helper sends it when control comes back)
  P                      ping
  T d|m X Y              touch down / move at X,Y (0..32767 of the screen's natural, portrait frame), T u lifts it
  SW X Y DX DY           a quick swipe from X,Y by DX,DY (the scroll wheel of the screen window)

Several controllers may be connected at once (the keyboard-and-mouse helper and the screen window).
"""
import fcntl
import os
import secrets
import select
import signal
import socket
import struct
import sys
import threading
import time

PORT = 8096
STATE_DIR = "/data/local/ssd"
TOKEN_FILE = STATE_DIR + "/inputd.token"
PID_FILE = STATE_DIR + "/inputd.pid"
LOG = STATE_DIR + "/inputd.log"
CLIENTS_FILE = STATE_DIR + "/inputd.clients"

EV_SYN, EV_KEY, EV_REL = 0, 1, 2
REL_X, REL_Y, REL_HWHEEL, REL_WHEEL = 0, 1, 6, 8
BTN = {"left": 0x110, "right": 0x111, "middle": 0x112}

UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_RELBIT = 0x40045566
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502


def log(msg):
    try:
        with open(LOG, "a") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


class Dev:
    """One uinput device."""

    def __init__(self, name, keys=(), rels=()):
        self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
        for k in keys:
            fcntl.ioctl(self.fd, UI_SET_KEYBIT, k)
        if rels:
            fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_REL)
            for r in rels:
                fcntl.ioctl(self.fd, UI_SET_RELBIT, r)
        # struct uinput_user_dev: name[80], input_id (bustype, vendor, product, version), ff_effects_max, 4 x abs[64]
        dev = struct.pack("80sHHHHi", name.encode(), 0x03, 0x1209, 0x0001, 1, 0) + b"\0" * (4 * 64 * 4)
        os.write(self.fd, dev)
        fcntl.ioctl(self.fd, UI_DEV_CREATE)

    def emit(self, typ, code, val):
        os.write(self.fd, struct.pack("llHHi", 0, 0, typ, code, val))

    def sync(self):
        self.emit(EV_SYN, 0, 0)

    def close(self):
        try:
            fcntl.ioctl(self.fd, UI_DEV_DESTROY)
        finally:
            os.close(self.fd)


UI_SET_ABSBIT = 0x40045567
UI_SET_PROPBIT = 0x4004556E
EV_ABS = 3
ABS_X, ABS_Y, ABS_PRESSURE = 0, 1, 0x18
ABS_MT_SLOT, ABS_MT_TOUCH_MAJOR, ABS_MT_POSITION_X, ABS_MT_POSITION_Y, ABS_MT_TRACKING_ID = 0x2F, 0x30, 0x35, 0x36, 0x39
BTN_TOUCH = 0x14A
AMAX = 32767


class Touch:
    """A virtual touchscreen (absolute, one finger): the screen window taps, drags and swipes with it."""

    def __init__(self):
        self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
        fcntl.ioctl(self.fd, UI_SET_KEYBIT, BTN_TOUCH)
        fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_ABS)
        for a in (ABS_X, ABS_Y, ABS_PRESSURE, ABS_MT_SLOT, ABS_MT_TOUCH_MAJOR, ABS_MT_POSITION_X, ABS_MT_POSITION_Y, ABS_MT_TRACKING_ID):
            fcntl.ioctl(self.fd, UI_SET_ABSBIT, a)
        fcntl.ioctl(self.fd, UI_SET_PROPBIT, 1)          # INPUT_PROP_DIRECT: a touchscreen, not a touchpad
        amax = [0] * 64
        for a in (ABS_X, ABS_Y, ABS_MT_POSITION_X, ABS_MT_POSITION_Y):
            amax[a] = AMAX
        amax[ABS_PRESSURE] = 255
        amax[ABS_MT_TOUCH_MAJOR] = 255
        amax[ABS_MT_SLOT] = 9
        amax[ABS_MT_TRACKING_ID] = 65535
        dev = struct.pack("80sHHHHi", b"Macotron touch", 0x03, 0x1209, 0x0002, 1, 0)
        dev += struct.pack("64i", *amax) + struct.pack("64i", *([0] * 64)) + b"\0" * (2 * 64 * 4)
        os.write(self.fd, dev)
        fcntl.ioctl(self.fd, UI_DEV_CREATE)
        self.down = False
        self.ids = 0

    def emit(self, typ, code, val):
        os.write(self.fd, struct.pack("llHHi", 0, 0, typ, code, val))

    def pos(self, x, y):
        x, y = max(0, min(AMAX, int(x))), max(0, min(AMAX, int(y)))
        self.emit(EV_ABS, ABS_MT_POSITION_X, x)
        self.emit(EV_ABS, ABS_MT_POSITION_Y, y)
        self.emit(EV_ABS, ABS_X, x)
        self.emit(EV_ABS, ABS_Y, y)

    def press(self, x, y):
        if self.down:
            self.lift()
        self.ids = (self.ids + 1) % 60000
        self.emit(EV_ABS, ABS_MT_SLOT, 0)
        self.emit(EV_ABS, ABS_MT_TRACKING_ID, self.ids)
        self.pos(x, y)
        self.emit(EV_ABS, ABS_PRESSURE, 120)
        self.emit(EV_ABS, ABS_MT_TOUCH_MAJOR, 60)
        self.emit(EV_KEY, BTN_TOUCH, 1)
        self.emit(EV_SYN, 0, 0)
        self.down = True

    def move(self, x, y):
        if not self.down:
            return
        self.emit(EV_ABS, ABS_MT_SLOT, 0)
        self.pos(x, y)
        self.emit(EV_SYN, 0, 0)

    def lift(self):
        if not self.down:
            return
        self.emit(EV_ABS, ABS_MT_SLOT, 0)
        self.emit(EV_ABS, ABS_MT_TRACKING_ID, -1)
        self.emit(EV_KEY, BTN_TOUCH, 0)
        self.emit(EV_SYN, 0, 0)
        self.down = False

    def close(self):
        try:
            self.lift()
        except OSError:
            pass
        try:
            fcntl.ioctl(self.fd, UI_DEV_DESTROY)
        finally:
            os.close(self.fd)


class Injector:
    def __init__(self):
        self.lock = threading.Lock()
        self.touch = Touch()
        self.mouse = Dev("Macotron mouse", keys=list(BTN.values()), rels=(REL_X, REL_Y, REL_WHEEL, REL_HWHEEL))
        self.kbd = Dev("Macotron keyboard", keys=range(1, 249))
        self.down_keys = set()
        self.down_btns = set()

    def handle(self, line):
        p = line.split()
        if not p:
            return "ERR empty"
        c = p[0]
        if c == "P":
            return "OK"
        if c == "M":
            dx, dy = int(p[1]), int(p[2])
            if dx:
                self.mouse.emit(EV_REL, REL_X, dx)
            if dy:
                self.mouse.emit(EV_REL, REL_Y, dy)
            self.mouse.sync()
            return "OK"
        if c == "B":
            code, v = BTN[p[1]], int(p[2])
            self.mouse.emit(EV_KEY, code, v)
            self.mouse.sync()
            (self.down_btns.add if v else self.down_btns.discard)(code)
            return "OK"
        if c == "W":
            dx, dy = int(p[1]), int(p[2])
            if dy:
                self.mouse.emit(EV_REL, REL_WHEEL, dy)
            if dx:
                self.mouse.emit(EV_REL, REL_HWHEEL, dx)
            self.mouse.sync()
            return "OK"
        if c == "K":
            code, v = int(p[1]), int(p[2])
            if not 1 <= code <= 248:
                return "ERR key code"
            self.kbd.emit(EV_KEY, code, v)
            self.kbd.sync()
            if v == 1:
                self.down_keys.add(code)
            elif v == 0:
                self.down_keys.discard(code)
            return "OK"
        if c == "T":
            if p[1] == "d":
                self.touch.press(float(p[2]), float(p[3]))
            elif p[1] == "m":
                self.touch.move(float(p[2]), float(p[3]))
            else:
                self.touch.lift()
            return "OK"
        if c == "SW":
            threading.Thread(target=self.swipe, args=tuple(float(v) for v in p[1:5]), daemon=True).start()
            return "OK"
        if c == "U":
            self.release_all()
            return "OK"
        return "ERR unknown command"

    def swipe(self, x, y, dx, dy):
        """A quick finger swipe (a scroll): down at x,y, eight steps of dx,dy, up."""
        steps = 8
        try:
            with self.lock:
                self.touch.press(x, y)
            for i in range(1, steps + 1):
                time.sleep(0.012)
                with self.lock:
                    self.touch.move(x + dx * i / steps, y + dy * i / steps)
            time.sleep(0.02)
        finally:
            with self.lock:
                self.touch.lift()

    def release_all(self):
        try:
            self.touch.lift()
        except OSError:
            pass
        for k in list(self.down_keys):
            self.kbd.emit(EV_KEY, k, 0)
        for b in list(self.down_btns):
            self.mouse.emit(EV_KEY, b, 0)
        self.down_keys.clear()
        self.down_btns.clear()
        self.kbd.sync()
        self.mouse.sync()

    def close(self):
        try:
            self.release_all()
        except OSError:
            pass
        self.mouse.close()
        self.kbd.close()
        self.touch.close()


def get_token():
    try:
        with open(TOKEN_FILE) as f:
            t = f.read().strip()
        if t:
            return t
    except OSError:
        pass
    t = secrets.token_hex(16)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(t + "\n")
    return t


def write_clients(n):
    try:
        with open(CLIENTS_FILE, "w") as f:
            f.write(str(n))
    except OSError:
        pass


def serve():
    token = get_token()
    inj = Injector()
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", PORT))
    srv.listen(4)
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))
    log("inputd listening on 127.0.0.1:%d (pid %d)" % (PORT, os.getpid()))
    stop = []
    signal.signal(signal.SIGTERM, lambda *a: stop.append(1))
    signal.signal(signal.SIGINT, lambda *a: stop.append(1))
    clients = {}                                   # socket -> {"buf": bytes, "authed": bool}
    write_clients(0)

    def drop(c, why):
        clients.pop(c, None)
        try:
            c.close()
        except OSError:
            pass
        log(why)
        write_clients(sum(1 for v in clients.values() if v["authed"]))

    try:
        while not stop:
            try:
                r, _, _ = select.select([srv] + list(clients), [], [], 1.0)
            except (InterruptedError, ValueError):
                continue
            for sck in r:
                if sck is srv:
                    c, _ = srv.accept()
                    if len(clients) >= 4:          # the oldest goes: a stale connection must not lock the others out
                        old = next(iter(clients))
                        drop(old, "oldest client dropped (too many)")
                    c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    clients[c] = {"buf": b"", "authed": False}
                    continue
                st = clients.get(sck)
                if st is None:
                    continue
                try:
                    data = sck.recv(65536)
                except OSError:
                    data = b""
                if not data:
                    inj.release_all() if not any(v["authed"] for k, v in clients.items() if k is not sck) else None
                    drop(sck, "client left")
                    continue
                st["buf"] += data
                out = []
                refused = False
                while b"\n" in st["buf"]:
                    line, st["buf"] = st["buf"].split(b"\n", 1)
                    line = line.decode("ascii", "replace").strip()
                    if not st["authed"]:
                        if line == "HELLO " + token:
                            st["authed"] = True
                            write_clients(sum(1 for v in clients.values() if v["authed"]))
                            log("client authenticated")
                            out.append("OK")
                        else:
                            drop(sck, "client refused (bad token)")
                            refused = True
                            break
                        continue
                    try:
                        with inj.lock:
                            out.append(inj.handle(line))
                    except (ValueError, IndexError, KeyError):
                        out.append("ERR bad line")
                if not refused and out:
                    try:
                        sck.sendall(("\n".join(out) + "\n").encode())
                    except OSError:
                        pass
    finally:
        log("inputd stopping")
        inj.close()
        write_clients(0)
        for f in (PID_FILE,):
            try:
                os.unlink(f)
            except OSError:
                pass


def pid():
    try:
        with open(PID_FILE) as f:
            p = int(f.read().strip())
        os.kill(p, 0)
        return p
    except (OSError, ValueError):
        return 0


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "serve":
        serve()
    elif cmd == "start":
        if pid():
            print("inputd already runs (pid %d)" % pid())
            return 0
        get_token()
        if os.fork():
            for _ in range(30):                 # wait for the daemon to write its pid
                time.sleep(0.2)
                if pid():
                    print("inputd started (pid %d)" % pid())
                    return 0
            print("inputd did not start; see %s" % LOG)
            return 1
        os.setsid()
        if os.fork():
            os._exit(0)
        fd = os.open(os.devnull, os.O_RDWR)
        for n in (0, 1, 2):
            os.dup2(fd, n)
        try:
            serve()
        except Exception as e:                  # noqa: BLE001 - last line of the log
            log("inputd failed: %r" % (e,))
        os._exit(0)
    elif cmd == "stop":
        p = pid()
        if not p:
            print("inputd is not running")
            return 0
        os.kill(p, signal.SIGTERM)
        for _ in range(20):
            time.sleep(0.2)
            if not pid():
                break
        print("inputd stopped" if not pid() else "inputd did not stop")
    elif cmd == "token":
        print(get_token())
    elif cmd == "status":
        p = pid()
        print("inputd=%s" % ("up" if p else "down"))
        print("inputd_port=%d" % PORT)
        try:
            with open(CLIENTS_FILE) as f:
                print("inputd_clients=%s" % (f.read().strip() or "0" if p else "0"))
        except OSError:
            print("inputd_clients=0")
        print("inputd_uinput=%s" % ("yes" if os.path.exists("/dev/uinput") else "no"))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
