#!/data/data/com.termux/files/usr/bin/python
"""Tablotron sensors helper.

Runs continuously as the Termux user (started by the boot script, ~/.termux/boot/10-start.sh,
never by watch.py and never through su). That matters: watch.py's root daemon used to read the
accelerometer itself via "su <uid> -c termux-sensor ...", and every single call that hung left a
permanently-orphaned process behind, because su hands the real work off to magiskd (a separate
daemon), not to a child of the calling process -- so no timeout or kill on the calling side can
ever reach it. Here, termux-sensor and termux-location are ordinary children of this already
correctly-privileged process: a timeout kills them cleanly, every time.

Writes the latest accelerometer and location reading to SENSORS_JSON every INTERVAL seconds.
watch.py only ever reads that file; it never spawns a sensor command itself.
"""
import json
import math
import os
import subprocess
import time

HOME = "/data/data/com.termux/files/home"
TERMUX_BIN = "/data/data/com.termux/files/usr/bin"
CFG = HOME + "/.config/tabctl"
SENSORS_JSON = CFG + "/sensors.json"
LOG = CFG + "/sensors_helper.log"
INTERVAL = 60
ACCEL_TIMEOUT = 15
LOC_TIMEOUT = 25


def log(msg):
    try:
        with open(LOG, "a") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


def run(argv, timeout):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, "timed out after %d s" % timeout
    except OSError as e:
        return 127, str(e)


def accel_sample():
    """Peak-to-peak accelerometer magnitude (m/s^2) over an ~0.8 s burst, or None."""
    run([TERMUX_BIN + "/termux-sensor", "-c"], timeout=8)   # release a stale listener, best-effort
    rc, out = run([TERMUX_BIN + "/termux-sensor", "-s", "LSM6DSO Accelerometer", "-d", "100", "-n", "8"],
                  timeout=ACCEL_TIMEOUT)
    if rc != 0:
        log("accel: rc %d: %s" % (rc, out[:200]))
        return None
    mags = []
    dec = json.JSONDecoder()
    i, n = 0, len(out)
    while i < n:
        while i < n and out[i] in " \t\r\n":
            i += 1
        if i >= n:
            break
        try:
            obj, j = dec.raw_decode(out, i)
        except ValueError:
            break
        i = j
        for v in obj.values():
            vals = v.get("values") if isinstance(v, dict) else None
            if vals and len(vals) >= 3:
                mags.append(math.sqrt(vals[0] ** 2 + vals[1] ** 2 + vals[2] ** 2))
    if len(mags) < 2:
        log("accel: parsed %d readings from %d chars" % (len(mags), len(out)))
        return None
    return max(mags) - min(mags)


def location_sample():
    """Android's last-known fix (not a fresh request: "-r last" returns immediately from
    whatever is already cached, instead of waiting on a GPS lock that can take 30+ s or never
    complete indoors). Returns (lat, lon, accuracy_m, provider), or None.
    """
    rc, out = run([TERMUX_BIN + "/termux-location", "-p", "gps", "-r", "last"], timeout=LOC_TIMEOUT)
    if rc != 0:
        log("location: rc %d: %s" % (rc, out[:200]))
        return None
    try:
        d = json.loads(out)
        return d["latitude"], d["longitude"], d.get("accuracy"), d.get("provider")
    except (ValueError, KeyError, TypeError):
        log("location: unparseable %d-char output: %s" % (len(out), out[:200]))
        return None


def main():
    os.makedirs(CFG, exist_ok=True)
    while True:
        t0 = time.time()
        data = {"t": int(time.time()), "accel_pp": accel_sample()}
        loc = location_sample()
        if loc:
            data["lat"], data["lon"], data["loc_acc_m"], data["loc_provider"] = loc
        tmp = SENSORS_JSON + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, SENSORS_JSON)
        time.sleep(max(1.0, INTERVAL - (time.time() - t0)))


if __name__ == "__main__":
    main()
