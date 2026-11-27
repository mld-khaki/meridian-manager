#!/bin/bash
# phone-proxy.sh - menu for routing Edge and remote desktops through the phone's hotspot.
# Run with no arguments for the menu.
# Shortcuts: phone [ip|auto|scan] | server [start|stop|restart|status] | toolbox [start|stop|restart|status] | all | up | start | stop | edge [mode] | tunnel [name] | untunnel [name] | add | device [list|use NAME|add NAME ...] | watch [on|off] | web [port] | bedrock-login | mount [name] | unmount [name] | drive-login [user] | drive-up | tabctl [tunnel|install] | sdbuffer [status|format|sync] | nat [on|off|status] | tools | speedtest [MB] | ip
#
# Targets (remote desktops reached through the phone's Tailscale) live in
# $CONF, one per line:   name  local_port  remote_ip  remote_port  tunnel(yes|no)
# The same file is pushed to the phone, where meridian.sh turns each line into a socat forward.
# A remote_ip of "usb:auto" (or an absolute path on the phone) is a drive share instead:
# the phone shares that folder over SMB (Samba) on its loopback, the tunnel brings it to
# localhost:local_port, and "mount" attaches smb://localhost:local_port/<name> in Finder,
# which shows the real capacity of the drive.
# An optional sixth column "nat=N" (N = 1-254) also gives the target a virtual address N in the
# hotspot's /24 on the tablet (e.g. 192.0.2.201 -> 100.64.0.2, every port), built by
# ~/bin/nat-up.sh there: the Mac then reaches it directly, with no tunnel and no Tailscale.
# A line with "-" as the ports forwards nothing and is NAT only (any Tailscale endpoint, not just
# a remote desktop):   example  -  100.64.0.2  -  no  nat=203

# The companion device (Raven, a spare phone, ...) comes from ~/.phone-proxy/devices.json, one active at a time:
# PHONE_USER (the Termux uid), HOSTKEY_ALIAS (one ssh host key per physical device). Switch with: phone-proxy.sh device use NAME
DEVICES_PY="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/devices.py"
if _dev=$(python3 -I "$DEVICES_PY" shell 2>/dev/null) && [ -n "$_dev" ]; then eval "$_dev"; else echo "Cannot load companion device configuration." >&2; exit 1; fi
unset _dev
SSH_PORT=8022
PROXY_PORT=8080
TABCTL_PORT=8095                    # the tablet's control page (tabctl), reached through an SSH tunnel
SMB_USER=${PHONE_SMB_USER:-milad}   # who mounts the drive share: the tablet's tunnel Samba refuses guest
EDGE_APP="Microsoft Edge"
PROFILE_DIR="$HOME/.edge-phone-profile"
CONF="$HOME/.phone-proxy-targets.conf"
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"

# MiladSync server (app.py next to this script): UI on 5678, peer listener on 5679
APP_DIR="$(dirname "$SELF")"
MILADSYNC_DIR="${MERIDIAN_MILADSYNC_DIR:-$APP_DIR}"
APP_PY="${MERIDIAN_MILADSYNC_PYTHON:-$MILADSYNC_DIR/venv_miladsync/bin/python}"
APP_PORT=5678
APP_LOG="$HOME/.phone-proxy/miladsync-server.log"

# Toolbox manager (the milad_one_toolbox launcher), run with its own venv
TB_DIR="${MERIDIAN_TOOLBOX_DIR:-$HOME/milad_one_toolbox}"
TB_PY="$TB_DIR/venvs/toolbox_manager/bin/python"
TB_LOG="$HOME/.phone-proxy/toolbox-manager.log"

# watchdog state: what the user turned on (proxy, tunnel names), the pid, the log
STATE_DIR="$HOME/.phone-proxy"
WANT="$STATE_DIR/wanted"
PIDF="$STATE_DIR/watch.pid"
WLOG="$STATE_DIR/watch.log"
WATCH_EVERY=15
BEDROCK_URL="${MERIDIAN_BEDROCK_URL:-}"
BEDROCK_WAIT=20     # seconds to wait for the launcher page before running aws login

R=$'\e[31m'; G=$'\e[32m'; Y=$'\e[33m'; B=$'\e[1m'; D=$'\e[2m'; N=$'\e[0m'
# menu palette (256-colour)
A=$'\e[38;5;81m'      # accent
H=$'\e[1;38;5;231;48;5;25m'   # title bar
K=$'\e[1;38;5;16;48;5;81m'    # key cap
KB=$'\e[1;38;5;16;48;5;208m'  # key cap: Bedrock (orange)
KU=$'\e[1;38;5;16;48;5;114m'  # key cap: USB speed test (green)
export LC_CTYPE=UTF-8   # so ${#var} counts characters, which the box drawing depends on

mkdir -p "$STATE_DIR"
wants()    { grep -qxF "$1" "$WANT" 2>/dev/null; }
want_add() { wants "$1" || echo "$1" >> "$WANT"; }
want_del() { { grep -vxF "$1" "$WANT" 2>/dev/null; true; } > "$WANT.new"; mv "$WANT.new" "$WANT"; }

# ---- targets --------------------------------------------------------------
init_conf() {
  [ -f "$CONF" ] && return
  cat > "$CONF" <<'EOT'
# name  local_port  remote_ip  remote_port  tunnel(yes|no)  [nat=N: virtual address N in the hotspot /24 on the tablet, every port -> remote_ip]
# Configure your own targets in the UI or uncomment and edit these examples:
# desktop   8033  100.64.0.2    3389   yes
# usb       8040  usb:auto        -   yes
EOT
}

load_targets() {
  T_NAME=(); T_LP=(); T_RIP=(); T_RP=(); T_TUN=(); T_NAT=()
  local n lp rip rp tun extra
  while read -r n lp rip rp tun extra; do
    case "$n" in ""|\#*) continue ;; esac
    [ "$lp" = - ] && tun=no      # no port: nothing is forwarded, the entry is NAT only
    T_NAME+=("$n"); T_LP+=("$lp"); T_RIP+=("$rip"); T_RP+=("$rp"); T_TUN+=("${tun:-yes}")
    extra=${extra%% *}
    case "$extra" in nat=*) T_NAT+=("${extra#nat=}") ;; *) T_NAT+=("") ;; esac
  done < "$CONF"
}

# a drive share (Samba on the phone), not a forwarded RDP host
is_serve() { [[ "$1" == usb:auto || "$1" == /* ]]; }

# index of a target by name, or empty
target_index() {
  local i
  for i in "${!T_NAME[@]}"; do [ "${T_NAME[$i]}" = "$1" ] && { echo "$i"; return 0; }; done
  return 1
}

# ---- detection ------------------------------------------------------------
# Two ways to reach the phone:
#   hotspot : the Mac is on the phone's hotspot, so the phone is the Wi-Fi gateway.
#   same LAN: Mac and phone are both on some other router; the phone has its own address.
# Order tried: PHONE_HOST (env, this run only) first, then the Wi-Fi gateway, then the
# address saved with menu key h / "phone <ip>"; the first one whose sshd answers wins.
# The gateway goes before the saved address so a stale one (say, the phone's address on
# another hotspot) cannot win while the Mac is on the phone's own hotspot. A normal
# router does not answer on $SSH_PORT, so on a shared LAN the saved address still wins.
# If none answers, the explicit address (else the gateway) is kept so the messages name
# something.
PHONE_HOST_F="$STATE_DIR/phone_host"
DRIVE_USER_F="$STATE_DIR/drive_user"
# who the drive share is mounted as: $DRIVE_USER, else the name saved by "drive-login", else $SMB_USER
drive_user() { local u; u=$(head -n 1 "$DRIVE_USER_F" 2>/dev/null); echo "${DRIVE_USER:-${u:-$SMB_USER}}"; }
phone_override() { echo "${PHONE_HOST:-$(cat "$PHONE_HOST_F" 2>/dev/null)}"; }

detect() {
  local want gw cand order wifi ri
  wifi=$(networksetup -listallhardwareports | awk '/Wi-Fi/{getline; print $2; exit}')
  IFACE=${PHONE_IFACE:-$wifi}
  gw=$([ -n "$IFACE" ] && ipconfig getoption "$IFACE" router 2>/dev/null)
  want=$(phone_override)
  GW=""
  if [ -n "$PHONE_HOST" ]; then order="$PHONE_HOST $gw"; else order="$gw $want"; fi
  for cand in $order; do
    [ -n "$cand" ] && port_up "$cand" "$SSH_PORT" && { GW=$cand; break; }
  done
  [ -z "$GW" ] && GW=${want:-$gw}
  if [ -n "$GW" ] && [ "$GW" = "$gw" ]; then PHONE_MODE=hotspot; else PHONE_MODE=lan; fi
  # the interface that actually reaches the phone (Ethernet, a second Wi-Fi, ...)
  if [ -z "$PHONE_IFACE" ] && [ -n "$GW" ]; then
    ri=$(route -n get "$GW" 2>/dev/null | awk '/interface:/{print $2; exit}')
    [ -n "$ri" ] && IFACE=$ri
  fi
  MYIP=$([ -n "$IFACE" ] && ipconfig getifaddr "$IFACE" 2>/dev/null)
  [ -z "$MYIP" ] && [ -n "$wifi" ] && MYIP=$(ipconfig getifaddr "$wifi" 2>/dev/null)
}

port_up() { nc -z -G 1 "$1" "$2" 2>/dev/null; }

SSH_OPTS=(-p "$SSH_PORT" -o BatchMode=yes -o ConnectTimeout=5
          -o StrictHostKeyChecking=accept-new -o HostKeyAlias=$HOSTKEY_ALIAS)

phone_ssh() { ssh -n "${SSH_OPTS[@]}" "$PHONE_USER@$GW" "$@"; }

have_net() { [ -n "$GW" ] && [ -n "$MYIP" ]; }

# Look for a phone running Termux sshd on this Mac's /24.
scan_phone() {
  local base i tmp
  [ -n "$MYIP" ] || { echo "${R}This Mac has no address to scan from.${N}"; return 1; }
  base=${MYIP%.*}; tmp=$(mktemp)
  echo "Scanning $base.0/24 for sshd on :$SSH_PORT ..." >&2
  for i in $(seq 1 254); do
    [ "$base.$i" = "$MYIP" ] && continue
    ( nc -z -G 1 "$base.$i" "$SSH_PORT" 2>/dev/null && echo "$base.$i" >> "$tmp" ) &
  done
  wait; sort -t. -k4 -n "$tmp"; rm -f "$tmp"
}

# set_phone [ip|auto|scan]: remember the phone's address for the same-LAN case
set_phone() {
  local v=$1 hits pick n=0 h
  if [ -z "$v" ]; then
    echo "Current: ${GW:-none} ($PHONE_MODE${GW:+, saved: $(phone_override)})"
    printf "Phone address (blank = keep, auto = use the Wi-Fi gateway, scan = find it on this network): "; read -r v
    [ -z "$v" ] && return 0
  fi
  case "$v" in
    auto) rm -f "$PHONE_HOST_F"; detect; echo "${G}Using the Wi-Fi gateway (phone hotspot).${N}"; return 0 ;;
    scan)
      hits=$(scan_phone)
      [ -z "$hits" ] && { echo "${R}Nothing answered on :$SSH_PORT. Is sshd running in Termux?${N}"; return 1; }
      echo "$hits" | while read -r h; do n=$((n + 1)); echo "  $n  $h"; done
      if [ "$(echo "$hits" | wc -l)" -eq 1 ]; then v=$hits
      else printf "Number to use: "; read -r pick; v=$(echo "$hits" | sed -n "${pick:-0}p"); fi
      [ -z "$v" ] && { echo "Cancelled."; return 1; } ;;
  esac
  valid_ip "$v" || { echo "${R}Not an IPv4 address.${N}"; return 1; }
  echo "$v" > "$PHONE_HOST_F"; detect
  if port_up "$v" "$SSH_PORT"; then echo "${G}Phone set to $v (sshd answers).${N}"
  else echo "${Y}Saved $v, but sshd is not answering there yet.${N}"; fi
}

# ---- phone proxy ----------------------------------------------------------
push_targets() {
  scp -q -P "$SSH_PORT" -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new \
      -o HostKeyAlias=$HOSTKEY_ALIAS "$CONF" "$PHONE_USER@$GW:meridian-targets.conf"
}

start_proxy() {
  have_net || { echo "${R}No network path to the phone (interface: ${IFACE:-none}).${N}"; return 1; }
  if ! port_up "$GW" "$SSH_PORT"; then
    echo "${R}Phone sshd ($GW:$SSH_PORT) not answering.${N} Run 'sshd' in Termux."
    return 1
  fi
  if port_up "$GW" "$PROXY_PORT"; then
    want_add proxy; echo "${G}Proxy already running.${N}"
    if has_nat && [ "$(cat "$NAT_GW_F" 2>/dev/null)" != "$GW" ]; then nat_apply; fi
    return 0
  fi
  push_targets || { echo "${R}Could not copy the target list to the phone.${N}"; return 1; }
  echo "Starting meridian.sh on the phone..."
  phone_ssh "MAC_IP=$MYIP PHONE_IP=$GW nohup bash ~/meridian.sh </dev/null >~/meridian.log 2>&1 &" \
    || { echo "${R}SSH login failed (key not accepted?).${N}"; return 1; }
  for _ in $(seq 1 40); do port_up "$GW" "$PROXY_PORT" && break; sleep 1; done
  if port_up "$GW" "$PROXY_PORT"; then
    want_add proxy; echo "${G}Proxy is up on $GW:$PROXY_PORT.${N}"
    phone_ssh 'grep "no drive found" ~/meridian.log' 2>/dev/null | sed "s/^/${Y}WARNING: /; s/\$/${N}/"
    nat_apply
  else
    echo "${R}Proxy did not come up.${N} Check the log (key l)."; return 1
  fi
}

# kill the phone side only ([x] patterns so the pkill does not match its own ssh session)
phone_kill() { phone_ssh 'pkill -f "[m]eridian-proxy"; pkill -f "[s]ocat"; pkill -f "[r]clone serve"; pkill -f "[s]mbd"; pkill -f "[m]eridian.sh"; true'; }

stop_proxy() {
  have_net || return 1
  rm -f "$WANT"
  stop_all_tunnels >/dev/null
  kill_tabctl_tunnel
  has_nat && nat_off >/dev/null
  phone_kill && echo "${G}Stopped on $GW.${N}" || echo "${R}Could not reach the phone.${N}"
}

# apply a changed target list on the phone (restarts the forwards if the proxy is running)
apply_targets() {
  have_net || return 0
  if port_up "$GW" "$PROXY_PORT"; then
    echo "Applying to the phone..."
    phone_kill; sleep 1; start_proxy
  fi
}

exit_ip() {
  have_net || return 1
  echo "Via phone : $(curl -s -m 10 -x "http://$GW:$PROXY_PORT" https://ifconfig.me || echo failed)"
  echo "Direct    : $(curl -s -m 10 https://ifconfig.me || echo failed)"
}

# ---- tunnels --------------------------------------------------------------
# tunnel i: localhost:LP on the Mac -> phone:LP (socat there -> remote desktop host)
tunnel_up() { nc -z -G 1 127.0.0.1 "${T_LP[$1]}" 2>/dev/null; }

kill_tunnel() { pkill -f "[s]sh .*-L ${T_LP[$1]}:127.0.0.1:${T_LP[$1]}" 2>/dev/null; }

stop_tunnel() {
  want_del "${T_NAME[$1]}"
  kill_tunnel "$1"; sleep 1
  if tunnel_up "$1"; then
    echo "${R}${T_NAME[$1]}: localhost:${T_LP[$1]} still listening (not started by this script?).${N}"
  else
    echo "${G}${T_NAME[$1]}: tunnel closed.${N}"
  fi
}

stop_all_tunnels() {
  local i
  for i in "${!T_NAME[@]}"; do [ "${T_TUN[$i]}" = yes ] && stop_tunnel "$i"; done
}

start_tunnel() {
  local i=$1
  have_net || { echo "${R}No network path to the phone (interface: ${IFACE:-none}).${N}"; return 1; }
  kill_tunnel "$i"; sleep 1     # a tunnel left from another hotspot IP would be stale
  port_up "$GW" "$PROXY_PORT" || start_proxy || return 1
  ssh -f -N -L "${T_LP[$i]}:127.0.0.1:${T_LP[$i]}" "${SSH_OPTS[@]}" \
    -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
    "$PHONE_USER@$GW" || { echo "${R}${T_NAME[$i]}: could not open the tunnel.${N}"; return 1; }
  sleep 1
  if tunnel_up "$i"; then
    want_add "${T_NAME[$i]}"; echo "$GW" > "$STATE_DIR/tgw.${T_NAME[$i]}"
    if is_serve "${T_RIP[$i]}"; then
      echo "${G}${T_NAME[$i]}: drive share at smb://localhost:${T_LP[$i]}/${T_NAME[$i]}  (menu key m mounts it in Finder)${N}"
    else
      echo "${G}${T_NAME[$i]}: connect your remote desktop app to localhost:${T_LP[$i]}${N}"
    fi
  else
    echo "${R}${T_NAME[$i]}: tunnel did not start listening.${N}"; return 1
  fi
}

toggle_tunnel() {
  if tunnel_up "$1"; then stop_tunnel "$1"; else start_tunnel "$1"; fi
}

start_all_tunnels() {
  local i
  for i in "${!T_NAME[@]}"; do [ "${T_TUN[$i]}" = yes ] && start_tunnel "$i"; done
}

# ---- add / remove targets ---------------------------------------------------
valid_ip() {
  local o
  [[ "$1" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || return 1
  for o in ${1//./ }; do [ "$((10#$o))" -le 255 ] || return 1; done
}
valid_port() { [[ "$1" =~ ^[0-9]+$ ]] && [ "$1" -ge 1 ] && [ "$1" -le 65535 ]; }

next_free_port() {
  local p=8033 i used
  while :; do
    used=0
    for i in "${!T_LP[@]}"; do [ "${T_LP[$i]}" = "$p" ] && used=1; done
    [ $used = 0 ] && ! nc -z -G 1 127.0.0.1 "$p" 2>/dev/null && { echo "$p"; return; }
    p=$((p + 1))
  done
}

add_target() {
  local name ip rport lport def i nat
  printf "Name (e.g. helios): "; read -r name
  [[ "$name" =~ ^[A-Za-z0-9_-]+$ ]] || { echo "${R}Use letters, digits, - or _ only.${N}"; return 1; }
  target_index "$name" >/dev/null && { echo "${R}'$name' already exists.${N}"; return 1; }
  printf "Tailscale IP (100.x.y.z): "; read -r ip
  valid_ip "$ip" || { echo "${R}Not an IPv4 address.${N}"; return 1; }
  printf "Remote RDP port [3389] (- = no forward, NAT only): "; read -r rport; rport=${rport:-3389}
  [ "$rport" = - ] || valid_port "$rport" || { echo "${R}Bad port.${N}"; return 1; }
  def=$(next_free_port)
  printf "Local port on the Mac and phone [%s]%s: " "$def" "$([ "$rport" = - ] && echo " (ignored: NAT only)")"; read -r lport
  if [ "$rport" = - ]; then lport=-
  else
    lport=${lport:-$def}
    valid_port "$lport" || { echo "${R}Bad port.${N}"; return 1; }
    for i in "${!T_LP[@]}"; do
      [ "${T_LP[$i]}" = "$lport" ] && { echo "${R}Port $lport is already used by ${T_NAME[$i]}.${N}"; return 1; }
    done
  fi
  printf "NAT address on the tablet's hotspot, host number 1-254 (%s): " "$([ "$lport" = - ] && echo required || echo "blank = none")"; read -r nat
  if [ -n "$nat" ]; then
    [[ "$nat" =~ ^[0-9]+$ ]] && [ "$nat" -ge 1 ] && [ "$nat" -le 254 ] || { echo "${R}NAT host number must be 1-254.${N}"; return 1; }
    for i in "${!T_NAT[@]}"; do [ "${T_NAT[$i]}" = "$nat" ] && { echo "${R}.$nat is already used by ${T_NAME[$i]}.${N}"; return 1; }; done
  fi
  [ "$lport" = - ] && [ -z "$nat" ] && { echo "${R}A NAT-only target needs a NAT host number.${N}"; return 1; }
  printf "%-10s  %-5s  %-15s  %-5s  %s%s\n" "$name" "$lport" "$ip" "$rport" "$([ "$lport" = - ] && echo no || echo yes)" "${nat:+  nat=$nat}" >> "$CONF"
  load_targets
  if [ "$lport" = - ]; then echo "${G}Added $name: NAT only -> $ip (address .$nat in the hotspot subnet)${N}"
  else echo "${G}Added $name: localhost:$lport -> $ip:$rport${N}"; fi
  apply_targets
}

remove_target() {
  load_targets
  local n=0 i names=() k
  echo "Targets:"
  for i in "${!T_NAME[@]}"; do
    n=$((n + 1)); names[$n]=${T_NAME[$i]}
    echo "  $n  ${T_NAME[$i]}  (localhost:${T_LP[$i]})"
  done
  printf "Number to remove (blank = cancel): "; read -r k
  [ -z "$k" ] && return 0
  [[ "$k" =~ ^[0-9]+$ ]] && [ -n "${names[$k]:-}" ] || { echo "${R}No such target.${N}"; return 1; }
  i=$(target_index "${names[$k]}")
  kill_tunnel "$i"; want_del "${names[$k]}"
  awk -v n="${names[$k]}" '$1 != n' "$CONF" > "$CONF.new" && mv "$CONF.new" "$CONF"
  echo "${G}Removed ${names[$k]}.${N}"
  load_targets
  apply_targets
}

# ---- watchdog ---------------------------------------------------------------
wlog()   { printf '%s %s\n' "$(date '+%H:%M:%S')" "$*"; }
notify() { osascript -e "display notification \"$1\" with title \"Phone proxy\"" >/dev/null 2>&1; }

watch_running() { [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; }

# one pass every $WATCH_EVERY s: restore whatever the user turned on and that has died
watch_loop() {
  local i need mt0
  mt0=$(stat -f %m "$SELF")
  wlog "watchdog started (every ${WATCH_EVERY}s)"
  while :; do
    # this script was edited since the watchdog started: restart on the new code
    if [ "$(stat -f %m "$SELF")" != "$mt0" ]; then wlog "script changed, reloading"; exec "$SELF" _watch; fi
    detect; load_targets
    if ! have_net; then
      wlog "no network path to the phone (interface ${IFACE:-none})"
    elif ! port_up "$GW" "$SSH_PORT"; then
      wlog "phone sshd not answering at $GW:$SSH_PORT (Termux asleep or killed?)"
    else
      if wants proxy; then
        need=0
        port_up "$GW" "$PROXY_PORT" || need=1
        for i in "${!T_NAME[@]}"; do
          is_serve "${T_RIP[$i]}" && continue
          wants "${T_NAME[$i]}" && ! port_up "$GW" "${T_LP[$i]}" && need=1
        done
        if [ $need = 1 ]; then
          wlog "phone proxy or a forward is down: restarting it"
          phone_kill; sleep 1
          if start_proxy; then notify "Proxy restarted on the phone"; wlog "restarted"; else wlog "restart failed"; fi
        fi
      fi
      for i in "${!T_NAME[@]}"; do
        wants "${T_NAME[$i]}" || continue
        if ! tunnel_up "$i"; then
          wlog "${T_NAME[$i]}: tunnel down, reopening"
          start_tunnel "$i" && notify "${T_NAME[$i]} tunnel reopened"
        elif [ "$(cat "$STATE_DIR/tgw.${T_NAME[$i]}" 2>/dev/null)" != "$GW" ]; then
          wlog "${T_NAME[$i]}: phone moved to $GW, rebuilding tunnel"
          start_tunnel "$i"
        fi
      done
      nat_check
    fi
    sleep "$WATCH_EVERY"
  done
}

watch_on() {
  if watch_running; then echo "${G}Watchdog already running (pid $(cat "$PIDF")).${N}"; return 0; fi
  [ -f "$WLOG" ] && [ "$(wc -c < "$WLOG")" -gt 200000 ] && : > "$WLOG"
  nohup "$SELF" _watch >> "$WLOG" 2>&1 &
  echo $! > "$PIDF"
  echo "${G}Watchdog started (pid $!). Log: $WLOG${N}"
}

watch_off() {
  if watch_running; then kill "$(cat "$PIDF")" 2>/dev/null; rm -f "$PIDF"; echo "${G}Watchdog stopped.${N}"
  else rm -f "$PIDF"; echo "Watchdog was not running."; fi
}

# ---- MiladSync server ---------------------------------------------------------
# Works on whatever listens on $APP_PORT, so a copy started by hand in a terminal is
# found, stopped and restarted the same way as one started here.
app_pid()     { lsof -nP -iTCP:"$APP_PORT" -sTCP:LISTEN -t 2>/dev/null | head -n 1; }
app_running() { [ -n "$(app_pid)" ]; }

app_start() {
  local _
  if app_running; then echo "${G}MiladSync server already running (pid $(app_pid)) on :$APP_PORT.${N}"; return 0; fi
  [ -x "$APP_PY" ] || { echo "${R}No Python at $APP_PY.${N}"; return 1; }
  [ -f "$MILADSYNC_DIR/app.py" ] || { echo "${R}MiladSync is a separate app. Set MERIDIAN_MILADSYNC_DIR to its folder.${N}"; return 1; }
  ( cd "$MILADSYNC_DIR" && nohup "$APP_PY" app.py </dev/null >>"$APP_LOG" 2>&1 & )
  for _ in $(seq 1 15); do app_running && break; sleep 1; done
  if app_running; then echo "${G}MiladSync server started (pid $(app_pid)): http://localhost:$APP_PORT  Log: $APP_LOG${N}"
  else echo "${R}MiladSync server did not come up.${N} Last log lines:"; tail -n 8 "$APP_LOG" 2>/dev/null; return 1; fi
}

app_stop() {
  local pid _
  pid=$(app_pid)
  [ -z "$pid" ] && { echo "MiladSync server was not running."; return 0; }
  kill "$pid" 2>/dev/null
  for _ in $(seq 1 10); do app_running || break; sleep 1; done
  if app_running; then echo "${R}MiladSync server (pid $pid) did not stop.${N}"; return 1; fi
  echo "${G}MiladSync server stopped.${N}"
}

app_restart() { app_stop && app_start; }

app_status() {
  if app_running; then echo "${G}MiladSync server running${N} (pid $(app_pid)) on :$APP_PORT"; else echo "MiladSync server not running."; fi
}

# ---- Toolbox manager ------------------------------------------------------------
# Same as: cd milad_one_toolbox; python ./.tool-manager/tool_manager.py (venvs/toolbox_manager)
tb_pid()     { pgrep -f "[t]ool_manager\.py" 2>/dev/null | head -n 1; }
tb_running() { [ -n "$(tb_pid)" ]; }

# Web link of the running manager, with its access token (the manager refuses a bare
# address in token mode). Host is this Mac's hotspot address, so the phone can use it too.
tb_url() {
  local pid port tok
  pid=$(tb_pid); [ -z "$pid" ] && return 1
  port=$(lsof -nP -a -p "$pid" -iTCP -sTCP:LISTEN -Fn 2>/dev/null | sed -n 's/^n.*:\([0-9][0-9]*\)$/\1/p' | head -n 1)
  tok=$(cat "$TB_DIR/.tool-manager/web_token.txt" 2>/dev/null)
  [ -n "$port" ] || return 1
  printf 'http://%s:%s/%s\n' "${MYIP:-localhost}" "$port" "${tok:+?token=$tok}"
}

tb_copy_url() {
  local u; u=$(tb_url) || { echo "${R}Toolbox manager is not serving a web page.${N}"; return 1; }
  printf '%s' "$u" | pbcopy; echo "${G}Link with token copied:${N} $u"
}

tb_start() {
  local _
  if tb_running; then echo "${G}Toolbox manager already running (pid $(tb_pid)).${N}"; return 0; fi
  [ -x "$TB_PY" ] || { echo "${R}No Python at $TB_PY.${N}"; return 1; }
  ( cd "$TB_DIR" && nohup "$TB_PY" ./.tool-manager/tool_manager.py </dev/null >>"$TB_LOG" 2>&1 & )
  for _ in $(seq 1 8); do tb_running && break; sleep 1; done
  if tb_running; then echo "${G}Toolbox manager started (pid $(tb_pid)). Log: $TB_LOG${N}"
    sleep 2; tb_url | sed "s/^/  Web (with token): /"
  else echo "${R}Toolbox manager did not start.${N} Last log lines:"; tail -n 8 "$TB_LOG" 2>/dev/null; return 1; fi
}

tb_stop() {
  local pid _
  pid=$(tb_pid)
  [ -z "$pid" ] && { echo "Toolbox manager was not running."; return 0; }
  kill "$pid" 2>/dev/null
  for _ in $(seq 1 10); do tb_running || break; sleep 1; done
  if tb_running; then echo "${R}Toolbox manager (pid $pid) did not stop.${N}"; return 1; fi
  echo "${G}Toolbox manager stopped.${N}"
}

tb_restart() { tb_stop && tb_start; }

tb_status() {
  if tb_running; then echo "${G}Toolbox manager running${N} (pid $(tb_pid))  $(tb_url)"; else echo "Toolbox manager not running."; fi
}

# ---- Edge -----------------------------------------------------------------
# $1 = isolated | default | plain
edge() {
  case "$1" in
    isolated)
      port_up "$GW" "$PROXY_PORT" || start_proxy || return 1
      open -na "$EDGE_APP" --args --user-data-dir="$PROFILE_DIR" --no-first-run \
        --proxy-server="http://$GW:$PROXY_PORT"
      echo "${G}Edge opened with its own profile, through $GW:$PROXY_PORT.${N}"
      ;;
    default)
      port_up "$GW" "$PROXY_PORT" || start_proxy || return 1
      if pgrep -x "$EDGE_APP" >/dev/null; then
        printf "%sEdge is running, so the proxy flag would be ignored. Quit it now? [y/N] %s" "$Y" "$N"
        read -rsn1 a; echo
        [ "$a" = y ] || { echo "Cancelled."; return 1; }
        osascript -e "quit app \"$EDGE_APP\""
        for _ in $(seq 1 10); do pgrep -x "$EDGE_APP" >/dev/null || break; sleep 1; done
      fi
      open -na "$EDGE_APP" --args --proxy-server="http://$GW:$PROXY_PORT"
      echo "${G}Edge opened (your normal profile) through $GW:$PROXY_PORT.${N}"
      ;;
    plain)
      open -na "$EDGE_APP"
      echo "Edge opened without a proxy."
      ;;
  esac
}

# Mount / unmount a drive share in Finder (first drive target, or the one named).
serve_index() {
  local i
  if [ -n "$1" ]; then i=$(target_index "$1") || { echo "${R}No target '$1'.${N}" >&2; return 1; }; echo "$i"; return 0; fi
  for i in "${!T_NAME[@]}"; do is_serve "${T_RIP[$i]}" && { echo "$i"; return 0; }; done
  echo "${R}No drive target in $CONF.${N}" >&2; return 1
}

mounted_path() {   # prints the /Volumes path if the share is mounted
  mount | awk -v h="localhost:${T_LP[$1]}/${T_NAME[$1]} on " 'index($0,h){print $3; exit}'
}

mount_usb() {
  local i mp u wait=${MOUNT_WAIT:-60}
  u=$(drive_user)
  i=$(serve_index "$1") || return 1
  mp=$(mounted_path "$i")
  [ -n "$mp" ] && { echo "${G}Already mounted at $mp${N}"; return 0; }
  have_net || { echo "${R}No network path to the phone (interface: ${IFACE:-none}).${N}"; return 1; }
  if ! phone_ssh "(echo > /dev/tcp/127.0.0.1/${T_LP[$i]}) 2>/dev/null"; then
    echo "${R}Nothing is sharing the drive on the phone (127.0.0.1:${T_LP[$i]}).${N} The SSD is locked and the SD buffer is not standing in (no card made the buffer, or its card is out): unlock the SSD on the tablet's control page ($0 tabctl), or run $0 drive-up"
    return 1
  fi
  tunnel_up "$i" || start_tunnel "$i" || return 1
  echo "Mounting smb://localhost:${T_LP[$i]}/${T_NAME[$i]} in Finder..."
  perl -e "alarm $wait; exec @ARGV" osascript -e "mount volume \"smb://$u@localhost:${T_LP[$i]}/${T_NAME[$i]}\"" >/dev/null 2>&1
  sleep 1; mp=$(mounted_path "$i")
  if [ -n "$mp" ]; then echo "${G}Mounted at $mp${N}"
  else
    echo "${R}Mount failed.${N} Finder did not mount it within $wait s: run this from Terminal on the Mac itself (not over ssh or from a background job)."
    [ "$u" = guest ] || echo "If it was waiting for the Mac-tunnel Samba password for $u, run: $0 drive-login $u  and tick \"Remember this password in my keychain\"."
    echo "Or in Finder: Go, Connect to Server, smb://$u@localhost:${T_LP[$i]}/${T_NAME[$i]}  (the Mac-tunnel Samba password, not the LAN one)"
    return 1
  fi
}

# Switch the drive share from guest to a named user and let Finder keep the password in the
# Keychain (tick "Remember this password in my keychain" in its dialog). Run it once, from
# Terminal on the Mac. The password goes to Finder only, never through this script.
drive_login() {
  local u=${1:-} i mp
  [ -z "$u" ] && { u=$(drive_user); [ "$u" = guest ] && u=milad; }
  [[ "$u" =~ ^[A-Za-z0-9_.-]+$ ]] || { echo "${R}User name: letters, digits, . _ - only.${N}"; return 1; }
  i=$(serve_index "") || return 1
  mp=$(mounted_path "$i")
  if [ -n "$mp" ]; then
    diskutil unmount "$mp" >/dev/null 2>&1 || { echo "${R}Could not unmount $mp (in use?).${N}"; return 1; }
  fi
  echo "Mounting as $u. Finder will ask for the password: tick \"Remember this password in my keychain\"."
  DRIVE_USER=$u MOUNT_WAIT=180 mount_usb "" || return 1
  if security find-internet-password -a "$u" >/dev/null 2>&1; then
    echo "$u" > "$DRIVE_USER_F"
    echo "${G}Saved: the drive now mounts as $u.${N}"
  else
    echo "${Y}Mounted as $u, but the password is not in the Keychain, so a plain mount would wait for it again.${N}"
    echo "Run $0 drive-login again and tick \"Remember this password in my keychain\" (it is unticked by default)."
    return 1
  fi
}

unmount_usb() {
  local i mp
  i=$(serve_index "$1") || return 1
  mp=$(mounted_path "$i")
  [ -z "$mp" ] && { echo "Not mounted."; return 0; }
  diskutil unmount "$mp" >/dev/null 2>&1 || umount "$mp" 2>/dev/null
  [ -z "$(mounted_path "$i")" ] && echo "${G}Unmounted $mp (safe to stop the tunnel).${N}" || echo "${R}Could not unmount $mp (in use?).${N}"
}

# ---- tablet control page (tabctl) and SSD unlock ------------------------------
# tabctl serves http://127.0.0.1:8095/ on the tablet and admits only 127.0.0.1 and Tailscale
# addresses, so the Mac reaches it through an SSH tunnel, which the tablet sees as 127.0.0.1.
# The page URL carries the access token: it goes straight to the browser and is never printed.
tabctl_tunnel_up()  { nc -z -G 1 127.0.0.1 "$TABCTL_PORT" 2>/dev/null; }
kill_tabctl_tunnel() { pkill -f "[s]sh .*-L ${TABCTL_PORT}:127.0.0.1:${TABCTL_PORT}" 2>/dev/null; }

# Open the tunnel, or rebuild one left from another hotspot address (it can hang on to that for
# ~90 s). It prints no URL: Macotron's Tablet tab asks the tablet for the page URL itself.
tabctl_tunnel() {
  have_net || { echo "${R}No network path to the phone (interface: ${IFACE:-none}).${N}"; return 1; }
  if tabctl_tunnel_up && [ "$(cat "$STATE_DIR/tgw.tabctl" 2>/dev/null)" != "$GW" ]; then
    kill_tabctl_tunnel; sleep 1
  fi
  if ! tabctl_tunnel_up; then
    ssh -f -N -L "$TABCTL_PORT:127.0.0.1:$TABCTL_PORT" "${SSH_OPTS[@]}" -o ExitOnForwardFailure=yes \
      -o ServerAliveInterval=30 -o ServerAliveCountMax=3 "$PHONE_USER@$GW" \
      || { echo "${R}tabctl: could not open the tunnel.${N}"; return 1; }
    sleep 1
    echo "$GW" > "$STATE_DIR/tgw.tabctl"
  fi
}

tabctl_open() {
  local url
  tabctl_tunnel || return 1
  url=$(phone_ssh "python ~/tabctl/tabctl.py url" | grep -m1 '^http://127\.0\.0\.1:')   # holds the token: never echo it
  [ -n "$url" ] || { echo "${R}The control page does not answer on the tablet.${N} Start it with: ssh -n tab 'sh ~/tabctl/start.sh'"; return 1; }
  open "http://localhost:${url#http://127.0.0.1:}"
  echo "${G}Tablet control page opened (http://localhost:${TABCTL_PORT}/).${N}"
}

# Install this folder's tablet-bin/tabctl on the tablet through tabctl's own installer (it backs
# up, swaps, compiles and restarts tabctl and the watch). Refused while the tablet runs a file that
# is neither this folder's copy nor any version committed here: a change made on the tablet itself
# is never overwritten. A file the tablet does not have yet (sdbuffer.py at first) is just added.
TABCTL_FILES=(tabctl.py root.sh watch.py sdbuffer.py)
tabctl_install() {
  local f c have known src="$APP_DIR/tablet-bin/tabctl"
  have_net || { echo "${R}No network path to the phone (interface: ${IFACE:-none}).${N}"; return 1; }
  for f in "${TABCTL_FILES[@]}"; do
    have=$(phone_ssh "if [ -e ~/tabctl/$f ]; then sha256sum ~/tabctl/$f; else echo new; fi" | cut -d' ' -f1)
    [ -n "$have" ] || { echo "${R}Cannot read ~/tabctl/$f on the tablet.${N}"; return 1; }
    [ "$have" = new ] && continue
    known=no
    [ "$(shasum -a 256 "$src/$f" | cut -d' ' -f1)" = "$have" ] && known=yes
    for c in $(git -C "$APP_DIR" log --format=%h -- "tablet-bin/tabctl/$f" 2>/dev/null); do
      [ "$known" = yes ] && break
      [ "$(git -C "$APP_DIR" show "$c:tablet-bin/tabctl/$f" | shasum -a 256 | cut -d' ' -f1)" = "$have" ] && known=yes
    done
    [ "$known" = yes ] || { echo "${R}The tablet's ~/tabctl/$f matches no version kept here: it was changed on the tablet.${N} Copy it into tablet-bin/tabctl/ and commit it first."; return 1; }
  done
  phone_ssh "mkdir -p ~/tabctl/new" &&
    scp -q -P "$SSH_PORT" -o BatchMode=yes -o ConnectTimeout=5 -o HostKeyAlias=$HOSTKEY_ALIAS \
      "${TABCTL_FILES[@]/#/$src/}" "$src/install.sh" "$PHONE_USER@$GW:tabctl/new/" &&
    phone_ssh "sh ~/tabctl/new/install.sh"
}

# Unlock and mount the SSD after a tablet reboot. cryptsetup asks for the LUKS passphrase on
# this terminal (-t); it is typed by hand and never passes through this script. It goes through
# root.sh unlock, as Tablotron's Unlock does, so the share moves back from the SD buffer at once.
drive_up() {
  have_net || { echo "${R}No network path to the phone (interface: ${IFACE:-none}).${N}"; return 1; }
  [ -t 0 ] || { echo "${R}drive-up needs a terminal for the passphrase.${N} Use the tablet's control page instead ($0 tabctl): its SSD card has the Unlock box."; return 1; }
  ssh -t "${SSH_OPTS[@]}" "$PHONE_USER@$GW" sudo sh /data/data/com.termux/files/home/tabctl/root.sh unlock
}

# The tablet's microSD card as the drive share's buffer (tablet-bin/tabctl/sdbuffer.py, run as root):
# status prints its state; format erases the card and makes it the buffer, asking on this terminal
# first (type ERASE); sync moves what it holds onto the SSD now instead of within the watch's minute.
# format never opens a terminal on the tablet (ssh -t sessions there came back garbled, plain ones
# do not): the tablet names the card and its token, this terminal asks, and the tablet erases only
# a card with that same path and size.
sdbuffer_cmd() {
  local py=/data/data/com.termux/files/usr/bin/python3 sb=/data/data/com.termux/files/home/tabctl/sdbuffer.py
  local out tok answer
  have_net || { echo "${R}No network path to the phone (interface: ${IFACE:-none}).${N}"; return 1; }
  case "${1:-status}" in
    format)
      [ -t 0 ] || { echo "${R}sdbuffer format asks before it erases the card, so it needs a terminal.${N} Run $0 sdbuffer format in one."; return 1; }
      [ -z "${2:-}" ] || [ "$2" = --again ] || { echo "usage: $0 sdbuffer format [--again]" >&2; return 2; }
      out=$(phone_ssh "sudo $py $sb format --check ${2:-}") || { printf '%s\n' "$out"; return 1; }
      printf '%s\n' "$out" | grep -v '^confirm='
      tok=$(printf '%s\n' "$out" | sed -n 's/^confirm=//p')
      [[ "$tok" =~ ^/dev/block/[A-Za-z0-9]+:[0-9]+$ ]] || { echo "${R}The tablet named no card to erase.${N}"; return 1; }
      printf "This ERASES everything on that card and makes it the drive's encrypted buffer.\nType ERASE to go on: "
      read -r answer
      [ "$answer" = ERASE ] || { echo "Nothing changed."; return 1; }
      phone_ssh "sudo $py $sb format --confirm $tok ${2:-}" ;;
    status|sync) phone_ssh "sudo $py $sb $1" ;;
    *) echo "usage: $0 sdbuffer [status|format [--again]|sync]" >&2; return 2 ;;
  esac
}

# ---- NAT on the tablet (targets with a nat=N column) ----------------------------
# The tablet gives such a target the address N in its hotspot /24 and DNATs every port of it to
# the target's Tailscale address (~/bin/nat-up.sh there, nat-down.sh undoes it). The address is
# only reachable while this Mac is on the hotspot, and moves with the subnet when the hotspot
# restarts; the watchdog rebuilds it when it stops answering.
NAT_GW_F="$STATE_DIR/tgw.nat"
NAT_TS_F="$STATE_DIR/nat.last"
has_nat() { local i; for i in "${!T_NAME[@]}"; do [ -n "${T_NAT[$i]:-}" ] && return 0; done; return 1; }
nat_vip() { [ -n "${T_NAT[$1]:-}" ] && [ "$PHONE_MODE" = hotspot ] && echo "${GW%.*}.${T_NAT[$1]}"; }

nat_apply() {   # push the targets file and (re)build the NAT on the tablet; prints the mapping
  has_nat || return 0
  have_net || { echo "${R}No network path to the phone (interface: ${IFACE:-none}).${N}"; return 1; }
  [ "$PHONE_MODE" = hotspot ] || { echo "${Y}NAT addresses live in the tablet's hotspot subnet and this Mac is not on the hotspot.${N}"; return 1; }
  push_targets || { echo "${R}Could not copy the target list to the phone.${N}"; return 1; }
  date +%s > "$NAT_TS_F"
  if phone_ssh "sudo /data/data/com.termux/files/home/bin/nat-up.sh"; then echo "$GW" > "$NAT_GW_F"
  else echo "${R}nat-up.sh failed on the tablet.${N}"; return 1; fi
}

nat_off() {
  have_net || return 1
  phone_ssh "sudo /data/data/com.termux/files/home/bin/nat-down.sh" && rm -f "$NAT_GW_F"
}

nat_status() {
  local i vip
  has_nat || { echo "No target has a nat=N column in $CONF."; return 0; }
  for i in "${!T_NAME[@]}"; do
    [ -n "${T_NAT[$i]:-}" ] || continue
    vip=$(nat_vip "$i")
    if [ -z "$vip" ]; then echo "${T_NAME[$i]}: NAT address .${T_NAT[$i]} (only while on the hotspot)"; continue; fi
    if valid_port "${T_RP[$i]}" && port_up "$vip" "${T_RP[$i]}"; then echo "${G}${T_NAME[$i]}: $vip -> ${T_RIP[$i]} (all ports); port ${T_RP[$i]} answers${N}"
    elif ping -c 1 -W 2000 "$vip" >/dev/null 2>&1; then
      if valid_port "${T_RP[$i]}"; then echo "${Y}${T_NAME[$i]}: $vip -> ${T_RIP[$i]} answers ping, but port ${T_RP[$i]} does not${N}"
      else echo "${G}${T_NAME[$i]}: $vip -> ${T_RIP[$i]} (all ports); answers ping${N}"; fi
    else echo "${R}${T_NAME[$i]}: $vip is not answering (run: $0 nat on)${N}"; fi
  done
}

# watchdog pass: a virtual address that stops answering ping (tablet rebooted, hotspot restarted)
# gets the NAT rebuilt, at most once a minute
nat_check() {
  local i vip now last
  has_nat && wants proxy && [ "$PHONE_MODE" = hotspot ] || return 0
  now=$(date +%s); last=$(cat "$NAT_TS_F" 2>/dev/null || echo 0)
  [ $((now - last)) -ge 60 ] || return 0
  for i in "${!T_NAME[@]}"; do
    vip=$(nat_vip "$i"); [ -n "$vip" ] || continue
    # every port and protocol of the address is NATed to the peer, so a failed ping may just be the
    # peer asleep: ask the tablet whether it still holds the address before rebuilding anything
    if [ "$(cat "$NAT_GW_F" 2>/dev/null)" = "$GW" ] && { ping -c 1 -W 2000 "$vip" >/dev/null 2>&1 || phone_ssh "sudo /data/data/com.termux/files/home/bin/nat-up.sh status" >/dev/null 2>&1; }; then
      continue
    else
      wlog "NAT: $vip (${T_NAME[$i]}) is gone from the tablet, rebuilding it"
      if nat_apply >/dev/null; then wlog "NAT rebuilt"; else wlog "NAT rebuild failed"; fi
      return
    fi
  done
}

# ---- tools ------------------------------------------------------------------
now()  { perl -MTime::HiRes=time -e 'printf "%.3f", time'; }
rate() { awk -v mb="$1" -v a="$2" -v b="$3" 'BEGIN{ d=b-a; if (d<=0) d=0.001; printf "%6.1f MB/s  (%.2f s)", mb/d, d }'; }

# The USB link of the tablet's SSD, from sysfs: 480 Mbit/s is USB 2 (about 40 MB/s at best),
# 5000 and up USB 3. Empty when the phone has no USB disk.
ssd_link() {
  ssh "${SSH_OPTS[@]}" "$PHONE_USER@$GW" 'sudo sh -s' 2>/dev/null <<'EOF' | head -n 1
for b in /sys/block/sd*; do
  d=$(readlink -f "$b/device")
  while [ -n "$d" ] && [ "$d" != / ] && [ ! -f "$d/speed" ]; do d=${d%/*}; done
  [ -f "$d/speed" ] && echo "USB $(cat "$d/speed") Mbit/s"
done
EOF
}

# dd write and read of $4 MB in folder $2 on the phone, through sudo when $3 is "sudo ". The file
# (.speedtest) stays: the card-to-SSD copy reads it again, and usb_speedtest removes it.
drive_dd() {
  local line
  echo "   $1"
  line=$(phone_ssh "${3}dd if=/dev/zero of='$2/.speedtest' bs=1M count=$4 conv=fsync 2>&1 | tail -n 1")
  echo "     write: $line"
  line=$(phone_ssh "${3}dd if='$2/.speedtest' of=/dev/null bs=1M iflag=direct 2>&1 | tail -n 1")
  case "$line" in *[Ee]rror*|*nvalid*|*"not supported"*)
    line="$(phone_ssh "${3}dd if='$2/.speedtest' of=/dev/null bs=1M 2>&1 | tail -n 1")  (may be cached)" ;; esac
  echo "     read : $line"
}

# $4 MB from the Mac into folder $2 on the phone over SSH, synced there: the link and the drive
# together, about what a share on that drive could reach
ssh_dd() {
  local t0 t1
  t0=$(now)
  dd if=/dev/zero bs=1048576 count="$4" 2>/dev/null |
    ssh "${SSH_OPTS[@]}" "$PHONE_USER@$GW" "${3}dd of='$2/.speedtest-ssh' bs=1M conv=fsync 2>/dev/null"
  t1=$(now)
  echo "   onto the $1: $(rate "$4" "$t0" "$t1")"
  phone_ssh "${3}rm -f '$2/.speedtest-ssh'"
}

# Verify USB mount speed: three links, so the slowest one shows the bottleneck.
#   1. the drives on the phone (dd, no network): the tablet's SSD and microSD card, or a phone's
#      USB stick; with both on the tablet, also what moving a file from the card to the SSD costs
#   2. the SSH link Mac <-> phone (what every tunnel rides on), bare and onto each drive
#   3. the Finder mount (SMB over the tunnel), written, read back uncached and checksummed
usb_speedtest() {
  local mb=${1:-64} i mp ssd link vol vbuf vdev vmnt vfs vlabel vroot vfree t0 t1 line
  have_net || { echo "${R}No network path to the phone (interface: ${IFACE:-none}).${N}"; return 1; }
  i=$(serve_index "") || return 1
  mp=$(mounted_path "$i")
  if [ -z "$mp" ]; then
    echo "The drive is not mounted; mounting it first..."
    mount_usb "" || return 1
    mp=$(mounted_path "$i")
  fi
  echo "${B}USB speed test, ${mb} MB${N}   (mount: $mp)"
  echo

  echo "1. Drives on the phone (no network)"
  # The tablet's SSD is /mnt/ssd and root-only, so it is driven through sudo. Under /mnt/media_rw
  # is the tablet's microSD card, which Termux cannot write (sudo again), or a phone's USB stick,
  # which it can; that path carries a volume id that changes, so it is looked up every time. Once
  # the card is the drive's buffer, it is /mnt/sdbuffer instead (root-only); the test file goes on
  # the card's top folder, beside the buffer's share folder, so nothing moves it onto the SSD.
  ssd=$(phone_ssh "awk '\$2 == \"/mnt/ssd\" {print \$2; exit}' /proc/mounts")
  vol=$(phone_ssh "awk '\$2 == \"/mnt/sdbuffer\" {print \$1, \$2, \$3; exit}' /proc/mounts")
  vbuf=${vol:+yes}
  [ -n "$vol" ] || vol=$(phone_ssh "awk '\$2 ~ \"^/mnt/media_rw/\" {print \$1, \$2, \$3; exit}' /proc/mounts")
  read -r vdev vmnt vfs <<< "$vol"
  case "$vbuf$vdev" in yes*) vlabel="SD buffer" ;; */public:179,*) vlabel="SD card" ;; *) vlabel="USB drive" ;; esac   # block major 179 is MMC
  vroot=""
  if [ -n "$vmnt" ] && ! phone_ssh "test -w '$vmnt'"; then vroot="sudo "; fi
  if [ -z "$ssd" ] && [ -z "$vmnt" ]; then
    echo "   ${R}the phone lists no drive (SSD at /mnt/ssd, or USB under /mnt/media_rw)${N}"
  fi
  if [ -n "$ssd" ]; then
    link=$(ssd_link)
    drive_dd "SSD at $ssd/share${link:+ ($link)}" "$ssd/share" "sudo " "$mb"   # the shared folder, under a name of its own
  fi
  if [ -n "$vmnt" ]; then
    vfree=$(phone_ssh "${vroot}df -k '$vmnt'" | awk 'NR==2 {printf ", %.0f GB free", $4/1048576}')
    drive_dd "$vlabel at $vmnt ($vfs$vfree)" "$vmnt" "$vroot" "$mb"
  fi
  if [ -n "$ssd" ] && [ -n "$vmnt" ]; then
    echo "   $vlabel -> SSD (moving a file from the card onto the SSD)"
    line=$(phone_ssh "sudo dd if='$vmnt/.speedtest' iflag=direct of='$ssd/share/.speedtest-copy' bs=1M conv=fsync 2>&1 | tail -n 1")
    case "$line" in *[Ee]rror*|*nvalid*|*"not supported"*)
      line="$(phone_ssh "sudo dd if='$vmnt/.speedtest' of='$ssd/share/.speedtest-copy' bs=1M conv=fsync 2>&1 | tail -n 1")  (card side may be cached)" ;; esac
    echo "     copy : $line"
  fi
  if [ -n "$ssd" ]; then phone_ssh "sudo rm -f '$ssd/share/.speedtest' '$ssd/share/.speedtest-copy'"; fi
  if [ -n "$vmnt" ]; then phone_ssh "${vroot}rm -f '$vmnt/.speedtest'"; fi
  echo

  echo "2. SSH link Mac <-> phone"
  t0=$(now); dd if=/dev/zero bs=1048576 count="$mb" 2>/dev/null | ssh "${SSH_OPTS[@]}" "$PHONE_USER@$GW" 'cat >/dev/null'; t1=$(now)
  echo "   to phone  : $(rate "$mb" "$t0" "$t1")"
  t0=$(now); phone_ssh "dd if=/dev/zero bs=1048576 count=$mb 2>/dev/null" >/dev/null; t1=$(now)
  echo "   from phone: $(rate "$mb" "$t0" "$t1")"
  # the same stream, written onto each drive and synced there
  if [ -n "$ssd" ]; then ssh_dd "SSD" "$ssd/share" "sudo " "$mb"; fi
  if [ -n "$vmnt" ]; then ssh_dd "$vlabel" "$vmnt" "$vroot" "$mb"; fi
  echo

  echo "3. Finder mount (SMB over the tunnel)"
  if [ -n "$vbuf" ]; then
    case "$(phone_ssh "sudo readlink /data/local/ssd/drive")" in
      /mnt/sdbuffer/*) echo "   the drive share, on the SD buffer now (the SSD is away)" ;;
      *) echo "   the drive share, on the SSD now (the SD buffer stands in while the SSD is away)" ;;
    esac
  elif [ -n "$ssd" ] && [ -n "$vmnt" ]; then echo "   the SSD's share (the $vlabel has no share of its own)"; fi
  python3 -I - "$mp" "$mb" <<'PY'
import fcntl, os, sys, time, zlib
mp, mb = sys.argv[1], int(sys.argv[2])
path = os.path.join(mp, ".speedtest-%d" % os.getpid())
blocks = [os.urandom(1 << 20) for _ in range(mb)]          # unique data, generated before timing
want = 0
for b in blocks:
    want = zlib.crc32(b, want)
def rate(sec): return "%6.1f MB/s  (%.2f s)" % (mb / max(sec, 0.001), sec)
try:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    fcntl.fcntl(fd, fcntl.F_NOCACHE, 1)
    t0 = time.time()
    for b in blocks:
        os.write(fd, b)
    os.fsync(fd)
    w = time.time() - t0
    os.close(fd)
    print("   write: " + rate(w))
    fd = os.open(path, os.O_RDONLY)
    fcntl.fcntl(fd, fcntl.F_NOCACHE, 1)
    got = 0
    t0 = time.time()
    while True:
        d = os.read(fd, 1 << 20)
        if not d:
            break
        got = zlib.crc32(d, got)
    r = time.time() - t0
    os.close(fd)
    print("   read : " + rate(r) + "   (uncached)")
    print("   data : " + ("checksum matches" if got == want else "CHECKSUM MISMATCH"))
finally:
    try: os.unlink(path)
    except OSError: pass
PY
  echo
  echo "${D}The slowest line is the limit. If 1 is fast but 2 and 3 are slow, it is the hotspot/SSH link; if 1 is slow, it is the drive or its power.${N}"
  if [ -n "$vbuf" ]; then
    echo "${D}The SD buffer's own speed counts only while the SSD is away; 'SD buffer -> SSD' in 1 is about how fast what it took moves onto the SSD afterwards.${N}"
  elif [ -n "$ssd" ] && [ -n "$vmnt" ]; then
    echo "${D}The $vlabel pays off as a go-between only if 'onto the $vlabel' in 2 clearly beats 3 and its copy onto the SSD in 1 keeps up; an SSD at USB 480 Mbit/s points at its port or cable first.${N}"
  fi
}

tools_menu() {
  local k mb
  while true; do
    clear
    box_top
    box_title "Tools"
    box_sep
    box_row "$(keycell 1 'Verify drive speed (SSD, SD card, SSH link, Finder mount)' 0 "$KU")"
    box_row "$(keycell b 'Back' 0)"
    box_bottom
    printf "\n %s>%s " "$A" "$N"
    read -rsn1 k; echo
    case "$k" in
      1) printf "Test size in MB [64]: "; read -r mb; mb=${mb:-64}
         if [[ "$mb" =~ ^[0-9]+$ ]] && [ "$mb" -ge 1 ] && [ "$mb" -le 2048 ]; then usb_speedtest "$mb"
         else echo "${R}Enter a whole number from 1 to 2048.${N}"; fi
         pause ;;
      b|B|q|Q|"") return ;;
    esac
  done
}

# Bedrock login: open the AWS app launcher in Edge (your normal profile, direct, so the
# Microsoft sign-in session is used), wait for the page, then run `aws login`. It asks
# questions (and so does aws login), so it needs a terminal: Macotron opens one for it, and
# closes it by itself once this exits 0 (signed in and verified, or the valid session kept).
bedrock_login() {
  [ -n "$BEDROCK_URL" ] || { echo "${R}Set MERIDIAN_BEDROCK_URL to your organization's app launcher URL first.${N}"; return 1; }
  local a ident auto=${1:-}   # "auto": started by the auto-login watcher, so do not ask whether to log in again
  [ -t 0 ] || { echo "${R}Bedrock login asks questions, so it needs a terminal: run phone-proxy.sh bedrock-login in one, or press Bedrock login in Macotron (it opens in the Terminals tab).${N}"; return 1; }
  command -v aws >/dev/null || { echo "${R}aws CLI not found in PATH.${N}"; return 1; }
  if [ "$auto" != auto ] && aws sts get-caller-identity --cli-connect-timeout 5 --cli-read-timeout 10 >/dev/null 2>&1; then
    printf "%sAWS session is still valid.%s Log in again anyway? [y/N] " "$Y" "$N"
    read -rsn1 a; echo
    [ "$a" = y ] || return 0
  fi
  echo "Opening the AWS app launcher in Edge..."
  open -a "$EDGE_APP" "$BEDROCK_URL" || { echo "${R}Could not open Edge.${N}"; return 1; }
  printf "Press any key once the page has loaded (continues by itself after %ss)... " "$BEDROCK_WAIT"
  read -rsn1 -t "$BEDROCK_WAIT"; echo
  aws login || { echo "${R}aws login failed.${N}"; return 1; }
  ident=$(aws sts get-caller-identity --query '[Account,Arn]' --output text 2>/dev/null)
  [ -n "$ident" ] || { echo "${Y}aws login finished but the session could not be verified.${N}"; return 1; }
  echo "${G}Logged in:${N} $ident"
}

# everything: proxy on phone + every RDP tunnel + Edge
do_all() {
  start_proxy || return 1
  start_all_tunnels
  edge isolated
}

pick_iface() {
  echo "Network interfaces:"
  networksetup -listallhardwareports | awk '/Hardware Port/{p=$0; sub(/Hardware Port: /,"",p)} /Device/{print "  " $2 "  (" p ")"}'
  printf "Interface to use [%s] (blank = auto): " "$IFACE"
  read -r v
  if [ -z "$v" ]; then unset PHONE_IFACE; else PHONE_IFACE=$v; fi
  detect
}

show_log() { have_net && phone_ssh 'tail -n 25 ~/meridian.log' || echo "No connection."; }

# ---- UI -------------------------------------------------------------------
W=78   # outer width of the menu box

dot() { if "$@"; then printf "%s●%s" "$G" "$N"; else printf "%s●%s" "$R" "$N"; fi; }

hline() { local l; printf -v l '%*s' "$1" ''; printf '%s' "${l// /$2}"; }

# visible length of a string that may hold colour escapes
vlen() { local p; p=$(printf '%s' "$1" | sed $'s/\\x1b\\[[0-9;]*m//g'); echo "${#p}"; }

box_top()    { printf "%s╭%s╮%s\n" "$A" "$(hline $((W - 2)) ─)" "$N"; }
box_bottom() { printf "%s╰%s╯%s\n" "$A" "$(hline $((W - 2)) ─)" "$N"; }
box_sep()    { printf "%s├%s┤%s\n" "$A" "$(hline $((W - 2)) ─)" "$N"; }
box_row() {
  local pad=$((W - 4 - $(vlen "$1")))
  [ $pad -lt 0 ] && pad=0
  printf "%s│%s %s%*s %s│%s\n" "$A" "$N" "$1" "$pad" "" "$A" "$N"
}
box_title() {   # a section heading that is part of the box
  local t=" $1 "
  printf "%s│%s%s%s%*s%s│%s\n" "$A" "$N" "$H" "$t" $((W - 2 - ${#t})) "" "$N$A" "$N"
}
box_section() { # ├─ Title ────┤
  local t=" $1 "
  printf "%s├─%s%s%s%s%s┤%s\n" "$A" "$N$B" "$t" "$N$A" "$(hline $((W - 3 - $(vlen "$t"))) ─)" "" "$N"
}

# a key cap plus its label, label padded to $3 columns (0 = no padding)
# optional $4 = key cap colour (default $K)
keycell() { printf "%s %s %s %-*s" "${4:-$K}" "$1" "$N" "$3" "$2"; }

draw() {
  local i n=0 dest state
  TIDX=()
  clear
  box_top
  box_title "◆  PHONE PROXY   Edge · remote desktops · MiladSync · toolbox"
  box_section "Network"
  if have_net; then
    box_row "Interface ${B}$IFACE${N}    Mac ${B}$MYIP${N}  ${A}→${N}  Phone ${B}$GW${N}"
    box_row "Link ${B}$([ "$PHONE_MODE" = hotspot ] && echo "hotspot" || echo "same network")${N} ${D}$([ "$PHONE_MODE" = hotspot ] && echo "(phone is the gateway)" || echo "(phone has its own address; h changes it)")${N}"
    box_row "Phone sshd $(dot port_up "$GW" "$SSH_PORT") ${D}:$SSH_PORT${N}      Proxy $(dot port_up "$GW" "$PROXY_PORT") ${D}:$PROXY_PORT${N}"
  else
    box_row "${R}Phone not reachable.${N} Join the hotspot or the phone's network, then set its address (${B}h${N})."
  fi
  box_section "Services"
  if app_running; then state="${G}up${N}   ${D}pid $(app_pid)${N}  ${A}http://localhost:$APP_PORT${N}"; else state="${D}down  (d starts it)${N}"; fi
  box_row "$(dot app_running) ${B}$(printf "%-16s" "MiladSync server")${N} $state"
  if tb_running; then state="${G}up${N}   ${D}pid $(tb_pid)${N}"; else state="${D}down  (g starts it)${N}"; fi
  box_row "$(dot tb_running) ${B}$(printf "%-16s" "Toolbox manager")${N} $state"
  tb_running && tb_url >/dev/null && box_row "  ${A}$(tb_url | sed "s/token=.*/token=…/")${N} ${D}(u copies)${N}"
  if watch_running; then state="${G}on${N}   ${D}restores proxy and tunnels every ${WATCH_EVERY}s${N}"; else state="${D}off  (w turns it on)${N}"; fi
  box_row "$(dot watch_running) ${B}$(printf "%-16s" "Watchdog")${N} $state"
  box_section "Remote desktops   ${D}(press the number to toggle a tunnel)${N}"
  for i in "${!T_NAME[@]}"; do
    if [ "${T_TUN[$i]}" = yes ]; then
      n=$((n + 1)); TIDX[$n]=$i
      if is_serve "${T_RIP[$i]}"; then dest="drive share (SMB) $(mounted_path "$i" | sed 's/^/mounted: /')"
      else dest="${T_RIP[$i]}:${T_RP[$i]}${T_NAT[$i]:+  ${D}NAT${N} $(nat_vip "$i" || echo ".${T_NAT[$i]} (hotspot only)")}"; fi
      box_row "$(printf '%s %s %s' "$K" "$n" "$N") $(printf '%-11s' "${T_NAME[$i]}") $(dot tunnel_up "$i")  ${A}localhost:${T_LP[$i]}${N} ${D}→${N} $dest"
    elif [ "${T_LP[$i]}" = - ]; then
      box_row "${D}  -  $(printf '%-11s' "${T_NAME[$i]}") ○  NAT only${N}   ${D}→${N} ${T_RIP[$i]}  ${D}NAT${N} $(nat_vip "$i" || echo ".${T_NAT[$i]} (hotspot only)")"
    else
      box_row "${D}  -  $(printf '%-11s' "${T_NAME[$i]}") ○  phone-only :${T_LP[$i]} → ${T_RIP[$i]}:${T_RP[$i]}${T_NAT[$i]:+  NAT $(nat_vip "$i" || echo ".${T_NAT[$i]}")}${N}"
    fi
  done
  box_section "Actions"
  box_row "$(keycell a 'Do everything' 26)$(keycell s 'Start proxy on phone' 0)"
  box_row "$(keycell x 'Stop proxy and tunnels' 26)$(keycell + 'Add a remote desktop' 0)"
  box_row "$(keycell e 'Edge, separate profile' 26)$(keycell - 'Remove a remote desktop' 0)"
  box_row "$(keycell n 'Edge, normal profile' 26)$(keycell m 'Mount / unmount USB drive' 0)"
  box_row "$(keycell p 'Edge, no proxy' 26)$(keycell b 'Bedrock login' 0 "$KB")"
  box_row "$(keycell d 'MiladSync server on/off' 26)$(keycell t 'Tools: USB speed test' 0 "$KU")"
  box_row "$(keycell o 'Restart MiladSync server' 26)$(keycell w 'Watchdog on/off' 0)"
  box_row "$(keycell g 'Toolbox manager on/off' 26)$(keycell v 'Watchdog log' 0)"
  box_row "$(keycell G 'Restart toolbox manager' 26)$(keycell l 'Phone log' 0)"
  box_row "$(keycell u 'Copy toolbox link+token' 26)$(keycell c 'Exit IP check' 0)"
  box_row "$(keycell i 'Choose interface' 26)$(keycell h 'Set phone address' 0)"
  box_row "$(keycell T 'Tablet control page' 26)$(keycell U 'Unlock the SSD (passphrase)' 0)"
  box_row "$(keycell N 'NAT on the tablet' 26)$(keycell r 'Refresh' 0)"
  box_row "$(keycell q 'Quit' 26)"
  box_bottom
  printf "\n %s>%s " "$A" "$N"
}

pause() { printf "\n%sPress any key...%s" "$D" "$N"; read -rsn1; }

menu() {
  local k i
  while true; do
    detect; load_targets; draw
    read -rsn1 k; echo
    case "$k" in
      a|A) do_all; pause ;;
      s)   start_proxy; pause ;;
      x)   stop_proxy; pause ;;
      e)   edge isolated; pause ;;
      n)   edge default; pause ;;
      p)   edge plain; pause ;;
      "+") add_target; pause ;;
      "-") remove_target; pause ;;
      m)   i=$(serve_index "") && { if [ -n "$(mounted_path "$i")" ]; then unmount_usb; else mount_usb; fi; }; pause ;;
      t)   tools_menu ;;
      b)   bedrock_login; pause ;;
      d)   if app_running; then app_stop; else app_start; fi; pause ;;
      o)   app_restart; pause ;;
      g)   if tb_running; then tb_stop; else tb_start; fi; pause ;;
      G)   tb_restart; pause ;;
      u)   tb_copy_url; pause ;;
      w)   if watch_running; then watch_off; else watch_on; fi; pause ;;
      v)   tail -n 25 "$WLOG" 2>/dev/null || echo "No watchdog log yet."; pause ;;
      i)   pick_iface ;;
      h)   set_phone; pause ;;
      l)   show_log; pause ;;
      c)   exit_ip; pause ;;
      T)   tabctl_open; pause ;;
      U)   drive_up; pause ;;
      N)   nat_status; printf "Apply or refresh the NAT on the tablet now? [y/N] "; read -rsn1 k; echo; [ "$k" = y ] && nat_apply; pause ;;
      [1-9])
        if [ -n "${TIDX[$k]:-}" ]; then toggle_tunnel "${TIDX[$k]}"; else echo "No target $k."; fi
        pause ;;
      r|"") ;;
      q|Q) clear; exit 0 ;;
    esac
  done
}

# ---- web UI ---------------------------------------------------------------------
# Web version of this menu (phone-proxy-web.py, localhost only, token in the URL), in its own
# Macotron app window. With --keep a copy already serving the port stays and only gets the window;
# otherwise this one starts serving and opens the window itself.
web_ui() {
  local port=$1 py
  py=$(command -v python3) || { echo "${R}python3 not found.${N}"; return 1; }
  [ -d "$STATE_DIR/Macotron.app" ] || echo "First run: building the Macotron app window (under a minute)."
  ( nohup "$py" "$APP_DIR/phone-proxy-web.py" "$port" --keep </dev/null >>"$STATE_DIR/web.log" 2>&1 & )
  for _ in $(seq 1 10); do nc -z 127.0.0.1 "$port" 2>/dev/null && break; sleep 0.5; done
  nc -z 127.0.0.1 "$port" 2>/dev/null || { echo "${R}Web UI did not start.${N} See $STATE_DIR/web.log"; return 1; }
  echo "${G}Web UI:${N} http://localhost:$port/?t=$(cat "$STATE_DIR/web.token")"
}

# ---- entry ----------------------------------------------------------------
# tunnel / untunnel [name]: one target, or all when no name is given
each_tunnel() {
  local fn=$1 name=$2 i
  if [ -n "$name" ]; then
    i=$(target_index "$name") || { echo "No target '$name'." >&2; return 1; }
    "$fn" "$i"
  else
    for i in "${!T_NAME[@]}"; do [ "${T_TUN[$i]}" = yes ] && "$fn" "$i"; done
  fi
}

init_conf
detect
load_targets

case "${1:-menu}" in
  menu)     menu ;;
  all)      do_all ;;
  up)       start_proxy && edge isolated ;;
  start)    start_proxy ;;
  stop)     stop_proxy ;;
  edge)     edge "${2:-isolated}" ;;
  tunnel)   each_tunnel start_tunnel "${2:-}" ;;
  untunnel) each_tunnel stop_tunnel "${2:-}" ;;
  add)      add_target ;;
  server)   case "${2:-status}" in start) app_start ;; stop) app_stop ;; restart) app_restart ;; status) app_status ;; *) echo "usage: $0 server [start|stop|restart|status]" >&2; exit 2 ;; esac ;;
  toolbox)  case "${2:-status}" in start) tb_start ;; stop) tb_stop ;; restart) tb_restart ;; status) tb_status ;; url) tb_url ;; *) echo "usage: $0 toolbox [start|stop|restart|status|url]" >&2; exit 2 ;; esac ;;
  bedrock-login) bedrock_login "${2:-}" ;;
  tools)    tools_menu ;;
  speedtest) usb_speedtest "${2:-64}" ;;
  mount)    mount_usb "${2:-}" ;;
  drive-login) drive_login "${2:-}" ;;
  drive-up) drive_up ;;
  tabctl)   case "${2:-}" in tunnel) tabctl_tunnel ;; install) tabctl_install ;; *) tabctl_open ;; esac ;;
  sdbuffer) sdbuffer_cmd "${2:-}" "${3:-}" ;;
  nat)      case "${2:-status}" in on) nat_apply ;; off) nat_off ;; status) nat_status ;; *) echo "usage: $0 nat [on|off|status]" >&2; exit 2 ;; esac ;;
  unmount)  unmount_usb "${2:-}" ;;
  watch)    case "${2:-}" in on) watch_on ;; off) watch_off ;; *) watch_loop ;; esac ;;
  _watch)   R=; G=; Y=; B=; D=; N=; A=; H=; K=; KB=; KU=; watch_loop ;;
  log)      show_log ;;
  exitip)   exit_ip ;;
  _remove)  remove_target ;;
  web)      web_ui "${2:-8090}" ;;
  phone)    set_phone "${2:-}" ;;
  device)   shift; python3 -I "$DEVICES_PY" "${@:-list}" ;;
  ip)       echo "interface=$IFACE mac=$MYIP phone=$GW mode=$PHONE_MODE" ;;
  *)        echo "usage: $0 [menu|phone [ip|auto|scan]|server [start|stop|restart|status]|toolbox [start|stop|restart|status]|all|up|start|stop|edge [isolated|default|plain]|tunnel [name]|untunnel [name]|add|watch [on|off]|bedrock-login|mount [name]|unmount [name]|drive-login [user]|drive-up|tabctl [tunnel|install]|sdbuffer [status|format|sync]|nat [on|off|status]|tools|speedtest [MB]|web [port]|ip]" >&2; exit 2 ;;
esac
