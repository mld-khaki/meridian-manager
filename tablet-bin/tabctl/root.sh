#!/system/bin/sh
# root.sh - the root half of tabctl. tabctl.py runs it through Magisk su:
#   su -c "sh ~/tabctl/root.sh <command>"
# status        print key=value lines (never SSIDs, passwords, BSSIDs or MACs)
# unlock        read the LUKS passphrase from stdin and run drive-up.sh
# up            run drive-up.sh without a passphrase (already unlocked, or key file)
# lock          run drive-down.sh
#               (all three, eject, reboot and shutdown also move the drive share between the SSD and
#               the microSD buffer, sdbuffer.py next to this file, when a card has been made the buffer)
# eject         lock if needed, then detach the SSD from USB (safe to unplug)
# reconnect     attach an ejected SSD again without unplugging it (it comes back locked)
# reboot        lock the SSD if needed, then restart the tablet
# shutdown      lock the SSD if needed, then power the tablet off
# samba-restart stop both smbd instances and start them again through drive-up.sh (SSD locked: for the SD buffer)
# smb-start [restart]  start the smbd instances that are not running, from the configs drive-up.sh wrote
# hotspot-settings  open Android's Wi-Fi hotspot screen
# fix-android   re-apply phantom-process, battery whitelist and Magisk mount namespace settings, and turn off Magisk's su toast for Termux
# open-page     open the URL given on stdin in the tablet's browser
# watch-start|watch-stop  start or stop watch.py, the root watchdog next to this file
# power-full|power-router|power-minimal  apply a power level: Bluetooth, location, background scanning, screen timeout, screensaver
# power-meeting [MINUTES]  the Meeting level for 15 to 1440 minutes (default 30), then back to the settings from before;
#               it also lifts ChatGPT's background limits. power-meeting-end ends a meeting now.
export PATH=/data/data/com.termux/files/usr/bin:/product/bin:/debug_ramdisk:/system/bin:/system/xbin
H=/data/data/com.termux/files/home
STATE=/data/local/ssd
MNT=/mnt/ssd
EXPECT_MODEL=CT4000MX500SSD1
AP_XML=/data/misc_ce/0/apexdata/com.android.wifi/WifiConfigStoreSoftAp.xml
EJECTED=$STATE/ejected   # sysfs path of the USB device that eject de-authorized
SDB=$H/tabctl/sdbuffer.py   # the microSD card as the drive share's buffer
BAT=/sys/class/power_supply/battery   # Samsung's battery driver: current in mA, negative while it drains

kv() { printf '%s=%s\n' "$1" "$2"; }
# Android's own tools (am, sm) need zygote's environment, which su does not give us.
zenv() { env -i $(tr '\0' ' ' < /proc/$(pidof zygote64 | cut -d' ' -f1)/environ) "$@"; }
first() { grep -m1 -oE "$1" | grep -oE -- '-?[0-9]+' | head -1; }
# sdbuffer.py, when it is installed (it does nothing either while no card is the buffer)
buffer() { [ -f "$SDB" ] || return 0; python3 "$SDB" "$@"; }
# start the smbd instances that are not running, with the configs drive-up.sh wrote: with them the
# SD buffer serves the drive share while the SSD is locked or away
smb_start() {
  for inst in tunnel lan; do
    conf=$STATE/$inst/smb.conf
    [ -f "$conf" ] || { echo "No $conf yet: unlock the SSD once, so drive-up.sh writes it."; continue; }
    pgrep -f "[s]mbd -D -s $conf" >/dev/null && continue
    if smbd -D -s "$conf"; then echo "smbd ($inst) started"; else echo "smbd ($inst) did not start"; fi
  done
}
smb_stop() {
  pkill -f "[s]mbd -D -s $STATE/tunnel/smb.conf"
  pkill -f "[s]mbd -D -s $STATE/lan/smb.conf"
  sleep 2
}
# the saved Wi-Fi networks, and switching to one of them (wifiswitch.py does the work, detached: the hotspot can restart meanwhile)
WIFILOG=$STATE/wifi-switch.log
wifi_saved_list() {  # the saved networks' names, one per line (cmd wifi list-networks prints: id, name, security)
  cmd wifi list-networks 2>/dev/null | sed -E '1d; s/^[0-9]+[[:space:]]+//; s/[[:space:]]+[a-z0-9^-]+[[:space:]]*$//' | awk 'NF && !seen[$0]++'
}
wifi_connect() {  # $1 = the name of a saved network
  [ -n "$1" ] || { echo "Choose a saved network."; exit 2; }
  wifi_saved_list | grep -Fxq -- "$1" || { echo "\"$1\" is not one of the tablet's saved networks."; exit 2; }
  : > "$WIFILOG" 2>/dev/null
  setsid sh "$0" _wifi-run "$1" > /dev/null 2>&1 < /dev/null &
  echo "Switching the tablet's Wi-Fi to \"$1\". The hotspot can restart and this page and SSH can drop for up to a minute; the result shows under the network list."
}

# the SSD's block device name (sdX), looking only at USB disks (never the internal UFS)
ssd_disk() {
  for bdev in /sys/block/sd*; do
    case "$(readlink -f "$bdev")" in */usb*) ;; *) continue ;; esac
    m="$(tr -d ' ' < "$bdev/device/vendor" 2>/dev/null)$(tr -d ' ' < "$bdev/device/model" 2>/dev/null)"
    [ "$m" = "$EXPECT_MODEL" ] && { basename "$bdev"; return 0; }
  done
  return 1
}
# the USB device (the sysfs dir with idVendor) that the block device $1 sits on
usb_dev() {
  p=$(readlink -f "/sys/block/$1/device")
  while [ -n "$p" ] && [ "$p" != / ]; do
    [ -f "$p/idVendor" ] && { echo "$p"; return 0; }
    p=$(dirname "$p")
  done
  return 1
}
# the de-authorized USB device eject left behind, if it is still plugged in
ejected_dev() {
  e=$(cat "$EJECTED" 2>/dev/null)
  case "$e" in /sys/devices/*/usb*) ;; *) return 1 ;; esac
  [ "$(cat "$e/authorized" 2>/dev/null)" = 0 ] && echo "$e"
}

eject() {
  disk=$(ssd_disk) || { echo "The SSD is not detected, so there is nothing to eject."; exit 1; }
  usb=$(usb_dev "$disk") || { echo "Cannot find the USB device under /dev/block/$disk."; exit 1; }
  if [ -e /dev/mapper/ssd ] || grep -qs " $MNT " /proc/mounts; then
    buffer away > /dev/null    # stop moving files and put the share on the SD buffer before the SSD goes
    bash "$H/bin/drive-down.sh" || { buffer up > /dev/null; exit 1; }
    buffer tick
  fi
  [ -e /dev/mapper/ssd ] && { echo "The SSD is still unlocked (cryptsetup close failed), so it was not ejected."; exit 1; }
  for h in /sys/block/$disk/holders/* /sys/block/$disk/$disk*/holders/*; do
    [ -e "$h" ] && { echo "Something still holds /dev/block/$disk ($(basename "$h")), so it was not ejected."; exit 1; }
  done
  sync
  # make the kernel send STOP UNIT when it drops the disk, so the drive flushes and parks
  for s in /sys/block/$disk/device/scsi_disk/*/manage_start_stop; do echo 1 > "$s"; done
  echo 1 > "/sys/block/$disk/device/delete" || { echo "Could not detach /dev/block/$disk."; exit 1; }
  echo "$usb" > "$EJECTED"
  # de-authorize the USB device so nothing probes the drive again until Reconnect or a replug
  echo 0 > "$usb/authorized" || echo "Note: could not de-authorize $usb; the drive is detached anyway."
  echo "SSD ejected. Safe to unplug."
  echo "To use it again without unplugging, press Reconnect."
}

reconnect() {
  usb=$(ejected_dev) || { rm -f "$EJECTED"; echo "No ejected SSD is plugged in. If you unplugged it, plug it back in."; exit 1; }
  echo 1 > "$usb/authorized" || { echo "Could not re-authorize $usb."; exit 1; }
  rm -f "$EJECTED"
  i=0
  while [ $i -lt 20 ]; do
    disk=$(ssd_disk) && { echo "SSD reconnected as /dev/block/$disk. It is locked: unlock it in the SSD card."; exit 0; }
    sleep 1; i=$((i + 1))
  done
  echo "The USB device is back, but the SSD did not show up within 20 s. Check its power supply, or unplug and replug it."
  exit 1
}

# the six settings a power level sets, as one string: Bluetooth / location mode / background Wi-Fi
# scanning / background Bluetooth scanning / screen timeout (ms) / screensaver when docked
power_now() {
  printf '%s/%s/%s/%s/%s/%s' "$(settings get global bluetooth_on 2>/dev/null)" "$(settings get secure location_mode 2>/dev/null)" \
    "$(settings get global wifi_scan_always_enabled 2>/dev/null)" "$(settings get global ble_scan_always_enabled 2>/dev/null)" \
    "$(settings get system screen_off_timeout 2>/dev/null)" "$(settings get secure screensaver_enabled 2>/dev/null)"
}
level_settings() {  # $1 = a level: its six settings, as power_now prints them
  case "$1" in
    full)    echo 1/3/1/1/60000/1 ;;
    meeting) echo 1/3/0/0/600000/1 ;;
    router)  echo 0/0/0/0/60000/1 ;;
    minimal) echo 0/0/0/0/15000/0 ;;
    *) return 1 ;;
  esac
}
level_of() {  # $1 = six settings: the level they match, or custom
  for lv in full meeting router minimal; do
    [ "$(level_settings $lv)" = "$1" ] && { echo $lv; return 0; }
  done
  echo custom
}
power_level_now() { level_of "$(power_now)"; }
level_name() { case "$1" in full) echo Full ;; meeting) echo Meeting ;; router) echo Router ;; minimal) echo Minimal ;; *) echo "the settings from before" ;; esac; }
onoff() { case "$1" in 1) echo on ;; 0) echo off ;; *) echo "left as it was" ;; esac; }

# Meeting also lets the ChatGPT app keep working with the screen off (a voice chat through a
# meeting): off Android's battery optimization, the whitelist fix-android puts Termux on, and allowed
# to run in the background. It stays so after the meeting. Samsung's own sleeping-apps lists are its own.
CHATGPT=com.openai.chatgpt
chatgpt_unlimit() {
  [ -n "$(cmd package path "$CHATGPT" 2>/dev/null)" ] || { echo "ChatGPT: the app is not installed, so there is nothing to lift."; return 0; }
  if dumpsys deviceidle whitelist +"$CHATGPT" >/dev/null 2>&1; then echo "ChatGPT: off Android's battery optimization"
  else echo "ChatGPT: could not take it off Android's battery optimization"; fi
  if cmd appops set "$CHATGPT" RUN_ANY_IN_BACKGROUND allow 2>/dev/null; then echo "ChatGPT: allowed to run in the background"
  else echo "ChatGPT: could not allow it to run in the background"; fi
  echo "If ChatGPT still stops with the screen off, take it off Settings > Battery > Background usage limits (Samsung's sleeping apps)."
}

# set six settings given as power_now prints them; a value Android never stored (null) is left as it is
power_set() {
  oifs=$IFS; IFS=/; set -- $1; IFS=$oifs
  case "$1" in
    1) cmd bluetooth_manager enable >/dev/null 2>&1 || zenv /system/bin/svc bluetooth enable; echo "Bluetooth: on" ;;
    0) cmd bluetooth_manager disable >/dev/null 2>&1 || zenv /system/bin/svc bluetooth disable; echo "Bluetooth: off" ;;
  esac
  case "$2" in
    0) cmd location set-location-enabled false 0 >/dev/null 2>&1 || settings put secure location_mode 0; echo "Location: off" ;;
    [1-3]) cmd location set-location-enabled true 0 >/dev/null 2>&1 || settings put secure location_mode "$2"; echo "Location: on" ;;
  esac
  case "$3" in [01]) settings put global wifi_scan_always_enabled "$3" ;; esac
  case "$4" in [01]) settings put global ble_scan_always_enabled "$4" ;; esac
  if [ "$3" = "$4" ]; then echo "Background Wi-Fi and Bluetooth scanning: $(onoff "$3")"
  else echo "Background scanning: Wi-Fi $(onoff "$3"), Bluetooth $(onoff "$4")"; fi
  case "$5" in
    ''|*[!0-9]*) ;;
    *) if [ "$5" -ge 120000 ] && [ $(($5 % 60000)) -eq 0 ]; then t="$(($5 / 60000)) min"; else t="$(($5 / 1000)) s"; fi
       settings put system screen_off_timeout "$5" && echo "Screen timeout: $t" ;;
  esac
  case "$6" in [01]) settings put secure screensaver_enabled "$6" && echo "Screensaver when docked: $(onoff "$6")" ;; esac
}
wifi_check() {  # a power change can drop the home Wi-Fi: say whether it is still connected
  sleep 6
  f=$(dumpsys wifi 2>/dev/null | first 'Frequency: [0-9]+')
  if [ "${f:-0}" -gt 0 ] 2>/dev/null; then echo "Home Wi-Fi: still connected ($f MHz)."
  else echo "WARNING: the home Wi-Fi is not connected. If it is not back within a minute, open hotspot settings and turn the hotspot off and on. If that does not help either, choose Full again."; fi
}
power_level() {  # $1 = full, meeting, router or minimal. Never touches the Termux wake lock or the hotspot.
  v=$(level_settings "$1") || { echo "unknown power level: $1"; exit 2; }
  # choosing another level ends a running meeting (Meeting again only gives it a new end: meeting_start)
  if [ "$1" != meeting ] && [ -f "$MEETING" ]; then rm -f "$MEETING"; echo "The meeting is over: it no longer switches back by itself."; fi
  power_set "$v"
  [ "$1" = meeting ] && chatgpt_unlimit
  echo "Power level: $1. The Termux wake lock is untouched: it is what keeps SSH, meridian and this page alive."
  [ "$1" = meeting ] && meeting_says
  wifi_check
}

# A meeting ends by itself. power-meeting MINUTES writes $MEETING (its end and the six settings to go
# back to) and starts a detached root.sh that waits for the end; watch.py ends a meeting whose end
# passed with nothing waiting (the tablet restarted during it). Choosing another level ends it too.
MEETING=$STATE/meeting
dur_text() {  # $1 = minutes
  if [ $(($1 / 60)) -eq 0 ]; then echo "$1 min"
  elif [ $(($1 % 60)) -eq 0 ]; then echo "$(($1 / 60)) h"
  else echo "$(($1 / 60)) h $(($1 % 60)) min"; fi
}
meeting_read() {  # sets mend and mback: the running meeting's end (epoch s) and the settings to go back to
  [ -f "$MEETING" ] && read -r mend mback < "$MEETING" || return 1
  case "$mend" in ''|*[!0-9]*) return 1 ;; esac
  case "$mback" in */*/*/*/*/*) return 0 ;; esac
  return 1
}
meeting_says() {  # when the running meeting ends and what comes back then
  meeting_read || return 0
  left=$((mend - $(date +%s))); [ "$left" -gt 0 ] || left=0
  at=$(date -d "@$mend" +%H:%M 2>/dev/null)
  back=$(level_name "$(level_of "$mback")")
  if [ -n "$at" ]; then echo "Meeting until $at ($(dur_text $(((left + 59) / 60))) from now), then back to $back."
  else echo "Meeting for $(dur_text $(((left + 59) / 60))) more, then back to $back."; fi
  echo "To end it sooner, press End meeting or choose another level."
}
meeting_start() {  # $1 = minutes, 15 to 1440 (24 h); 30 when not given
  m=${1:-30}
  case "$m" in ''|*[!0-9]*) echo "The meeting length is whole minutes, 15 to 1440 (24 h)."; exit 2 ;; esac
  if [ "$m" -lt 15 ] || [ "$m" -gt 1440 ]; then echo "The meeting length is 15 to 1440 minutes (24 h)."; exit 2; fi
  # back to: what a running meeting saved, else the settings now (Meeting set without an end goes back to Router)
  if meeting_read; then back=$mback
  else back=$(power_now); [ "$(level_of "$back")" = meeting ] && back=$(level_settings router); fi
  end=$(($(date +%s) + m * 60))
  mkdir -p "$STATE" && echo "$end $back" > "$MEETING" || { echo "Could not write $MEETING, so the meeting did not start."; exit 1; }
  setsid sh "$0" _meeting-wait "$end" > "$STATE/meeting.log" 2>&1 < /dev/null &
  echo "Meeting for $(dur_text "$m")."
  power_level meeting
}
meeting_end() {  # back to the saved settings; the file moves away first, so only one caller ends a meeting
  mv "$MEETING" "$MEETING.ending" 2>/dev/null || { echo "No meeting is running."; return 1; }
  read -r mend mback < "$MEETING.ending"; rm -f "$MEETING.ending"
  case "$mback" in */*/*/*/*/*) ;; *) echo "The meeting's saved settings are unreadable, so nothing changed: choose a level."; return 1 ;; esac
  echo "Meeting over: back to $(level_name "$(level_of "$mback")")."
  power_set "$mback"
  wifi_check
}

power() {  # $1 = reboot or shutdown
  if [ -e /dev/mapper/ssd ] || grep -qs " $MNT " /proc/mounts; then
    buffer away > /dev/null
    bash "$H/bin/drive-down.sh" || { buffer up > /dev/null; echo "The SSD could not be locked, so the tablet was not touched."; exit 1; }
  fi
  # the SD buffer too, so its card comes back clean; a buffer still in use never stops the restart
  buffer close > /dev/null || echo "Note: the SD buffer was still in use; ext4's journal mends it on the next start."
  sync
  # a detached root.sh does the power call a few seconds later, so the page still gets this answer
  setsid sh "$0" _power-now "$1" > /dev/null 2>&1 < /dev/null &
  if [ "$1" = reboot ]; then
    echo "Restarting in 3 s. The page, SSH and Tailscale come back in a minute or two; then unlock the SSD here."
  else
    echo "Shutting down in 3 s. Everything stays off until someone presses the tablet's power button."
  fi
}

status() {
  kv uid "$(id -u)"
  kv selinux "$(getenforce 2>/dev/null)"
  kv magisk "$(magisk -v 2>/dev/null)"
  kv mnt_ns "$(magisk --sqlite "SELECT value FROM settings WHERE key='mnt_ns'" 2>/dev/null | sed 's/.*=//')"
  kv uptime_s "$(cut -d' ' -f1 /proc/uptime | cut -d. -f1)"

  b=$(dumpsys battery 2>/dev/null)
  kv bat_level "$(echo "$b" | first 'level: [0-9]+')"
  kv bat_status "$(echo "$b" | first 'status: [0-9]+')"
  kv bat_temp "$(echo "$b" | first 'temperature: [0-9]+')"
  kv bat_ac "$(echo "$b" | grep -m1 -oE 'AC powered: (true|false)' | cut -d' ' -f3)"
  kv bat_usb "$(echo "$b" | grep -m1 -oE 'USB powered: (true|false)' | cut -d' ' -f3)"
  kv bat_wireless "$(echo "$b" | grep -m1 -oE 'Wireless powered: (true|false)' | cut -d' ' -f3)"
  kv bat_max_charge_ua "$(echo "$b" | first 'Max charging current: [0-9]+')"
  # the battery's own current, now and its running average (mA, negative while it drains), and the
  # charger's input voltage (mV); the charger's input current is not measured on this tablet
  kv bat_current_ma "$(cat $BAT/current_now 2>/dev/null)"
  kv bat_current_avg_ma "$(cat $BAT/current_avg 2>/dev/null)"
  kv bat_input_mv "$(cat $BAT/vbyp_voltage 2>/dev/null)"

  w=$(dumpsys wifi 2>/dev/null)
  kv sta_freq "$(echo "$w" | first 'Frequency: [0-9]+')"
  kv sta_rssi "$(echo "$w" | first 'RSSI: -?[0-9]+')"
  kv sta_link "$(echo "$w" | first 'Link speed: [0-9]+')"
  kv ssid "$(echo "$w" | grep -m1 -oE 'mWifiInfo SSID: "[^"]*"' | sed -E 's/.*"(.*)"/\1/')"
  kv wifi_saved "$(wifi_saved_list | tr '\n' '|' | sed 's/|$//')"
  kv wifi_switch "$(tail -n 1 "$WIFILOG" 2>/dev/null)"
  ap=$(echo "$w" | grep -m1 -oE 'mCurrentSoftApInfoMap \{[a-z0-9]+=SoftApInfo\{bandwidth= ?[0-9]+, frequency= ?[0-9]+')
  apif=$(echo "$ap" | sed -nE 's/.*\{([a-z0-9]+)=SoftApInfo.*/\1/p')
  kv ap_if "$apif"
  kv ap_freq "$(echo "$ap" | sed -nE 's/.*frequency= ?([0-9]+).*/\1/p')"
  kv ap_bw "$(echo "$ap" | sed -nE 's/.*bandwidth= ?([0-9]+).*/\1/p')"
  if [ -n "$apif" ]; then
    kv ap_ip "$(ip -4 -o addr show "$apif" 2>/dev/null | awk '{print $4}' | head -1)"
    kv ap_clients "$(ip -4 neigh show dev "$apif" 2>/dev/null | grep -cE 'REACHABLE|STALE|DELAY|PROBE')"
  fi
  if [ -f "$AP_XML" ]; then
    kv ap_cfg_band "$(grep -oE 'name="Band" value="[0-9]+"' "$AP_XML" | grep -oE '[0-9]+')"
    kv ap_cfg_autooff "$(grep -oE 'name="AutoShutdownEnabled" value="[a-z]+"' "$AP_XML" | sed -E 's/.*value="([a-z]+)"/\1/')"
    kv ap_cfg_security "$(grep -oE 'name="SecurityType" value="[0-9]+"' "$AP_XML" | grep -oE '[0-9]+')"
  fi
  kv ts_ip "$(ip -4 -o addr 2>/dev/null | awk '$4 ~ /^100\./ {sub(/\/.*/, "", $4); print $4; exit}')"

  listeners=$(ss -ltnp 2>/dev/null)
  for p in 8022 8033 8035 8080 8040 445 8095; do
    kv "port_$p" "$(echo "$listeners" | grep -E ":$p[[:space:]]" | grep -m1 -oE 'users:\(\("[^"]+"' | cut -d'"' -f2 | sed 's/^smbd.*/smbd/')"
  done

  disk=$(ssd_disk)
  kv ssd_disk "$disk"
  kv ssd_ejected "$(ejected_dev >/dev/null && echo yes || echo no)"
  if [ -n "$disk" ]; then
    kv ssd_gib "$(( $(cat /sys/block/$disk/size) / 2097152 ))"
    kv ssd_type "$(blkid "/dev/block/${disk}1" 2>/dev/null | grep -oE 'TYPE="[^"]+"' | cut -d'"' -f2)"
    # SMART attribute 194 (or 190 on drives that use Airflow_Temperature instead): degrees C.
    # -d sat is the USB-SATA passthrough this bridge needs; without it smartctl sees no SMART data at all.
    kv ssd_temp_c "$(smartctl -A -d sat "/dev/block/$disk" 2>/dev/null | awk '/^19[04] .*[Tt]emperature/ {sub(/\(.*/,""); print $NF; exit}')"
  fi
  kv ssd_open "$([ -e /dev/mapper/ssd ] && echo yes || echo no)"
  # a pulled SSD (no Eject first) leaves its mount behind and every read fails with an I/O error:
  # that is not "mounted". Report it as not mounted, and say so in ssd_stale_mount.
  kv ssd_stale_mount "$(if grep -qs " $MNT " /proc/mounts && [ -z "$disk" ]; then echo yes; else echo no; fi)"
  if grep -qs " $MNT " /proc/mounts && [ -n "$disk" ]; then
    kv ssd_mounted yes
    df -k "$MNT" 2>/dev/null | awk 'NR==2 {print "ssd_size_k=" $2; print "ssd_used_k=" $3; print "ssd_avail_k=" $4}'
  else
    kv ssd_mounted no
  fi
  kv keyfile "$([ -f /data/adb/ssd.key ] && echo yes || echo no)"

  df -k /data 2>/dev/null | awk 'NR==2 {print "int_size_k=" $2; print "int_used_k=" $3; print "int_avail_k=" $4}'

  # a microSD or USB-OTG card shows up as a vold "public" volume; its mount path carries a volume id that changes, so find it by the device column instead
  sdmnt=$(df -k 2>/dev/null | awk '$1 ~ /^\/dev\/block\/vold\/public:/ {print $NF; exit}')
  # once the card is the drive's buffer, Android no longer mounts it: sdbuffer.py does, here
  grep -qs " /mnt/sdbuffer " /proc/mounts && sdmnt=/mnt/sdbuffer
  if [ -n "$sdmnt" ]; then
    kv sd_present yes
    df -k "$sdmnt" 2>/dev/null | awk 'NR==2 {print "sd_size_k=" $2; print "sd_used_k=" $3; print "sd_avail_k=" $4}'
  else
    kv sd_present no
  fi

  for inst in tunnel lan; do
    if pgrep -f "smbd -D -s $STATE/$inst/smb.conf" >/dev/null; then
      kv "smb_${inst}" up
      kv "smb_${inst}_sessions" "$(smbstatus -b -s "$STATE/$inst/smb.conf" 2>/dev/null | grep -cE '^[0-9]+ ')"
    else
      kv "smb_${inst}" down
    fi
  done

  kv phantom "$(settings get global settings_enable_monitor_phantom_procs 2>/dev/null)"
  wl=$(dumpsys deviceidle whitelist 2>/dev/null)
  kv idle_termux "$(echo "$wl" | grep -q ',com.termux,' && echo yes || echo no)"
  kv idle_tailscale "$(echo "$wl" | grep -q ',com.tailscale.ipn,' && echo yes || echo no)"
  kv idle_chatgpt "$(echo "$wl" | grep -q ",$CHATGPT," && echo yes || echo no)"
  kv wakelocks_termux "$(dumpsys power 2>/dev/null | grep -i 'PARTIAL_WAKE_LOCK' | grep -ci termux)"
  kv tsu_patched "$(grep -q '/product/bin/su' /data/data/com.termux/files/usr/bin/tsu 2>/dev/null && echo yes || echo no)"
  pn=$(power_now)
  kv bt_on "$(echo "$pn" | cut -d/ -f1)"; kv loc_mode "$(echo "$pn" | cut -d/ -f2)"
  kv wifi_scan_always "$(echo "$pn" | cut -d/ -f3)"; kv ble_scan_always "$(echo "$pn" | cut -d/ -f4)"
  kv screen_timeout_ms "$(echo "$pn" | cut -d/ -f5)"; kv screensaver "$(echo "$pn" | cut -d/ -f6)"
  kv power_level "$(power_level_now)"
  if meeting_read; then kv meeting_left_s "$((mend - $(date +%s)))"; kv meeting_back "$(level_of "$mback")"; fi
  buffer status 2>/dev/null
}

case "${1:-}" in
  status) status ;;
  unlock|up)
    if [ "$1" = up ]; then bash "$H/bin/drive-up.sh" < /dev/null; else bash "$H/bin/drive-up.sh"; fi
    rc=$?
    [ $rc -eq 0 ] && buffer up    # the share back on the SSD; what the SD buffer took meanwhile moves onto it
    exit $rc ;;
  lock)
    buffer away > /dev/null      # stop moving files and put the share on the SD buffer before the SSD goes
    bash "$H/bin/drive-down.sh"; rc=$?
    if [ $rc -eq 0 ]; then buffer tick; else buffer up > /dev/null; fi
    exit $rc ;;
  eject) eject ;;
  reconnect) reconnect ;;
  reboot|shutdown) power "$1" ;;
  _power-now)  # run detached by power(): Android's normal shutdown, with init's as the fallback
    sleep 3
    case "${2:-}" in
      reboot) zenv /system/bin/svc power reboot || /system/bin/reboot ;;
      shutdown) zenv /system/bin/svc power shutdown || /system/bin/reboot -p ;;
    esac ;;
  samba-restart)
    smb_stop
    if [ -e /dev/mapper/ssd ]; then
      bash "$H/bin/drive-up.sh" < /dev/null; rc=$?
      buffer tick > /dev/null      # drive-up.sh writes the SSD's folder as the share's path: the link again
      exit $rc
    fi
    buffer tick > /dev/null        # opens the SD buffer, points the share at it and starts smbd for it
    buffer status 2>/dev/null | grep -qx 'sdbuf_target=buffer' || { echo "The SSD is locked. Unlock it first."; exit 1; }
    smb_start
    echo "The SSD is locked; Samba serves the SD buffer meanwhile." ;;
  smb-start)
    [ "${2:-}" = restart ] && smb_stop
    smb_start ;;
  hotspot-settings)
    zenv /system/bin/am start -n 'com.android.settings/.Settings$WifiTetherSettingsActivity' ;;
  watch-start) exec /data/data/com.termux/files/usr/bin/python3 "$H/tabctl/watch.py" start ;;
  watch-stop) exec /data/data/com.termux/files/usr/bin/python3 "$H/tabctl/watch.py" stop ;;
  open-page)  # the URL (with token) comes on stdin, so it never sits in su's command line
    read -r url && zenv /system/bin/am start -a android.intent.action.VIEW -d "$url" >/dev/null && echo "Opened in the tablet's browser." ;;
  power-full|power-router|power-minimal) power_level "${1#power-}" ;;
  power-meeting) meeting_start "${2:-}" ;;
  wifi-connect) wifi_connect "${2:-}" ;;
  _wifi-run) exec /data/data/com.termux/files/usr/bin/python3 "$H/tabctl/wifiswitch.py" "${2:-}" ;;
  power-meeting-end) meeting_end ;;
  _meeting-wait)  # run detached by meeting_start: ends the meeting at $2, unless it was ended or replaced first
    while meeting_read && [ "$mend" = "$2" ]; do
      left=$((mend - $(date +%s)))
      if [ "$left" -le 0 ]; then meeting_end; exit $?; fi
      sleep $((left < 60 ? left : 60))
    done ;;
  fix-android)
    settings put global settings_enable_monitor_phantom_procs false && echo "phantom process monitor: off"
    dumpsys deviceidle whitelist +com.termux >/dev/null && echo "battery whitelist: com.termux"
    dumpsys deviceidle whitelist +com.tailscale.ipn >/dev/null && echo "battery whitelist: com.tailscale.ipn"
    magisk --sqlite "REPLACE INTO settings (key,value) VALUES('mnt_ns',0)" && echo "Magisk mount namespace: global"
    # tabctl's status refresh runs sudo several times every 10 s: no "Superuser rights granted" toast for each grant
    magisk --sqlite "UPDATE policies SET notification=0 WHERE uid=$(stat -c %u /data/data/com.termux)" && echo "Magisk su toast for Termux: off" ;;
  *) echo "usage: root.sh status|unlock|up|lock|eject|reconnect|reboot|shutdown|samba-restart|smb-start|hotspot-settings|fix-android|open-page|watch-start|watch-stop|power-full|power-router|power-minimal|power-meeting [MINUTES]|power-meeting-end|wifi-connect SSID" >&2; exit 2 ;;
esac
