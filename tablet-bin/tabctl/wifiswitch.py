#!/data/data/com.termux/files/usr/bin/python3
"""wifiswitch.py - switch the tablet's Wi-Fi to one of its saved networks (runs as root, started detached by root.sh).

  sudo python3 ~/tabctl/wifiswitch.py "<ssid>"          switch
  sudo python3 ~/tabctl/wifiswitch.py --check "<ssid>"  only say whether the saved network and its key can be found

Android has no command that picks a saved network, but `cmd wifi connect-network <ssid> wpa2 <key>` joins it. The key is
read from the tablet's own Wi-Fi store at the moment of switching, handed to that one command and nowhere else: it is
never printed, logged or sent anywhere (the log holds network names and results only).
The result goes to /data/local/ssd/wifi-switch.log, whose last line the control page shows.
"""
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

STORE = "/data/misc/apexdata/com.android.wifi/WifiConfigStore.xml"
LOG = "/data/local/ssd/wifi-switch.log"


def log(msg):
    try:
        with open(LOG, "a") as f:
            f.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg))
    except OSError:
        pass


def lookup(ssid):
    """(kind, key) for the saved network: kind 'wpa2' with its passphrase, or 'open'. Raises ValueError with a reason."""
    root = ET.parse(STORE).getroot()
    found = []
    for cfg in root.iter("WifiConfiguration"):
        d = {e.get("name"): (e.text or "") for e in cfg if e.tag == "string"}
        if d.get("SSID") == '"%s"' % ssid:
            found.append(d)
    if not found:
        raise ValueError("\"%s\" is not in the tablet's Wi-Fi store" % ssid)
    for d in found:                                           # a network saved twice (open and WPA): prefer the one with a key
        psk = d.get("PreSharedKey")
        if psk:
            if psk.startswith('"') and psk.endswith('"') and 8 <= len(psk) - 2 <= 63:
                return "wpa2", psk[1:-1]
            raise ValueError("its key is stored as a raw hex key, which this command cannot use")
    for d in found:
        if d.get("ConfigKey", "").endswith("NONE"):
            return "open", ""
    raise ValueError("no usable saved key for \"%s\" (an enterprise or WPA3-only network?)" % ssid)


def connected_to(ssid):
    r = subprocess.run(["cmd", "wifi", "status"], capture_output=True, text=True, timeout=20)
    return 'Wifi is connected to "%s"' % ssid in r.stdout


def main(argv):
    check = "--check" in argv
    names = [a for a in argv[1:] if not a.startswith("--")]
    if len(names) != 1 or not names[0]:
        print("usage: wifiswitch.py [--check] <ssid>")
        return 2
    ssid = names[0]
    try:
        kind, key = lookup(ssid)
    except (ValueError, OSError, ET.ParseError) as e:
        log("cannot switch to %s: %s" % (ssid, e))
        print("cannot switch to %s: %s" % (ssid, e))
        return 1
    if check:
        print("ok: %s network %s, key %s" % (kind, ssid, "found" if key else "not needed"))
        return 0
    log("switching to %s ..." % ssid)
    args = ["cmd", "wifi", "connect-network", ssid, kind] + ([key] if key else [])
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        log("the connect command did not answer in 30 s")
        return 1
    key = None
    out = (r.stdout + r.stderr).strip().replace("\n", " ")[:120]
    for _ in range(30):                                       # up to a minute for the connection
        time.sleep(2)
        try:
            if connected_to(ssid):
                log("connected to %s" % ssid)
                return 0
        except (subprocess.TimeoutExpired, OSError):
            pass
    log("not connected to %s after a minute (%s)" % (ssid, out or "no message"))
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
