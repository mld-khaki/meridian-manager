#!/data/data/com.termux/files/usr/bin/sh
# Install the tabctl files staged in ~/tabctl/new (tabctl.py and root.sh, and watch.py and sdbuffer.py
# when they came along): compile, back up, swap, restart tabctl and the watch, show status.
#   sh ~/tabctl/new/install.sh            install and restart
#   sh ~/tabctl/new/install.sh router     ... then apply the Router level and watch the home Wi-Fi for a minute
set -e
cd ~/tabctl
for f in tabctl.py watch.py sdbuffer.py; do
  [ -f "new/$f" ] || continue
  python -m py_compile "new/$f" || { echo "new/$f does not compile: nothing was installed."; exit 1; }
done
first_sdb=no
[ -f new/sdbuffer.py ] && [ ! -f sdbuffer.py ] && first_sdb=yes
for f in tabctl.py root.sh watch.py sdbuffer.py; do
  [ -f "new/$f" ] || continue
  [ -f "$f" ] && cp -p "$f" "$f.bak"
  cp "new/$f" "$f"
  echo "installed $f"
done
chmod 700 tabctl.py root.sh
pkill -f "[t]abctl.py serve" || true
sleep 1
sh ~/tabctl/start.sh
sleep 4
python -c "import socket; s=socket.socket(); s.settimeout(3); s.connect(('127.0.0.1', 8095))" 2>/dev/null && echo "tabctl listening on 8095" || { echo "tabctl is NOT listening; see ~/tabctl/tabctl.log"; exit 1; }
if [ -f new/watch.py ]; then
  sudo sh ~/tabctl/root.sh watch-stop || true
  sudo sh ~/tabctl/root.sh watch-start
fi
python ~/tabctl/tabctl.py status | grep -E '"(power_level|sta_freq|wakelocks_termux|ssd_mounted|smb_tunnel|smb_lan|sdbuf|sdbuf_target|sdbuf_files)"' || true
[ "$first_sdb" = no ] || echo "The SD buffer is installed but no card is the buffer yet. On the Mac: phone-proxy.sh sdbuffer format (it erases the microSD card, and asks first)."
[ "${1:-}" = router ] || { echo "Installed."; exit 0; }
echo "--- applying the Router level through the page API"
python - <<'PY'
import json, os, urllib.request
tok = open(os.path.expanduser("~/.config/tabctl/token")).read().strip()
req = urllib.request.Request("http://127.0.0.1:8095/api/action", data=json.dumps({"action": "power-router"}).encode(),
                             headers={"Content-Type": "application/json", "X-Tabctl": "1", "Cookie": "tabctl=" + tok})
r = json.load(urllib.request.urlopen(req, timeout=90))
print("rc", r["rc"]); print(r["out"])
PY
echo "--- watching the home Wi-Fi for 60 s"
i=0
while [ $i -lt 4 ]; do
  sleep 15; i=$((i + 1))
  sudo sh -c 'echo "$(date +%T) home Wi-Fi: $(dumpsys wifi | grep -m1 -oE "Frequency: -?[0-9]+")  bluetooth_on=$(settings get global bluetooth_on) location_mode=$(settings get secure location_mode)"'
  python -c 'import socket
s=socket.socket(); s.settimeout(4)
try: s.connect(("1.1.1.1",443)); print("  internet OK")
except Exception as e: print("  internet FAILED:", e)'
done
python ~/tabctl/tabctl.py status | grep -E '"(power_level|sta_freq|ap_freq)"'
echo "If the home Wi-Fi dropped: turn the hotspot off and on from the tablet; to go back, press Full on the page."
