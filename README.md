# Meridian Manager

Macotron: a macOS control panel for a companion Android phone, phone-proxy connections,
remote desktops and drive shares.

## Run on macOS

Python 3.11 or newer is recommended. The main app uses the Python standard library. The native
Macotron window, clipboard helper and rooted-device input helper are built from Swift source
when needed; install Apple's command-line developer tools for `swiftc`. Without it the web UI
can use a browser.

```sh
brew install --cask android-platform-tools
brew install scrcpy
python3 phone-proxy-web.py
```

The UI binds to localhost, requires a token and opens in Macotron.app. Runtime state stays in
`~/.phone-proxy/`; closing the app window leaves the server running.

## Wireless ADB without root

1. Enable Developer options and **Wireless debugging** on the phone (Android 11+). Put the phone
   and Mac on trusted shared Wi-Fi. Whether wireless debugging works while the phone provides
   the hotspot depends on the device; shared Wi-Fi is the recommended setup.
2. In Macotron's **Wireless ADB** card, enter the address from **Pair device with pairing code**
   and its six-digit code. Press **Pair**. The code is cleared from the form, sent through adb's
   stdin, and never saved or logged by Macotron.
3. Enter the **IP address and port** from the main Wireless debugging screen and press
   **Connect / enable monitoring**. This is a different port from pairing.
4. Battery and Tailscale diagnostics refresh every 15 seconds. Open **scrcpy screen** for a
   separate screen-mirroring window with keyboard/mouse control.

Connection details and hardware identity are saved per companion profile in `~/.phone-proxy/adb.json`.
Every device command explicitly selects that endpoint; Macotron never chooses the first device
attached to adb. A different hardware identity requires confirmation before replacing the
profile's pinned phone. Connection ports may change after reboot/network changes; enter the
new address and reconnect. This backend does not alter SSH, the proxy, or system firewall rules.

### Available controls

- Battery percentage, charging/external-power state and temperature, when Android reports them.
- Tailscale installation and app-process state; Android VPN ownership, when its diagnostics
  expose enough information. **App running does not mean VPN connected**, and VPN active does
  not guarantee peer reachability. Unsupported diagnostic formats show unknown, not connected.
- Tailscale on/off only when the installed app exposes its official automation activity or
  receiver. First-time VPN approval and sign-in still happen on the phone. Disconnecting
  Tailscale can interrupt remote desktops and shares, so the UI asks for confirmation.
- Screen wake/sleep, opening Wi-Fi/hotspot settings, and manual brightness (0-255, disables
  automatic brightness). Android/vendor restrictions surface as errors.
- Optional scrcpy launch/close for the selected phone. Protected screens may appear black.
  Output goes to `~/.phone-proxy/scrcpy.log`; restarting Macotron closes its scrcpy window.

**Stop ADB monitoring** leaves ADB and an existing scrcpy window alone. **Disconnect ADB**
stops monitoring, closes that profile's scrcpy window and disconnects only its saved endpoint.
Macotron never kills the shared adb server or unrelated scrcpy processes.

ADB is Android debugging-shell access, **not root**. Custom NAT/firewall rules, the existing
`/dev/uinput` backend, and raw/LUKS disk controls remain root-only.

Official Tailscale intent reference:
[Android manifest](https://github.com/tailscale/tailscale-android/blob/main/android/src/main/AndroidManifest.xml).
Screen-control tool: [scrcpy](https://github.com/Genymobile/scrcpy).

## Companion profile and existing phone proxy

The bundled `raven` profile is a **non-rooted example**, not a real configured device. Set your
Termux username, SSH host-key alias, hotspot/LAN address and Tailscale address before using SSH
features. For example (replace all values with your phone's):

```sh
python3 devices.py set raven label="My phone" user=u0_a123 ssid=MyHotspot ts=100.64.0.2 root=no
python3 devices.py use raven
```

`use` updates the managed `Host tab` SSH block and stores profiles in `~/.phone-proxy/devices.json`.
Restart Macotron after switching profiles. ADB-only use does not require Termux or SSH.

The phone-proxy features expect the existing meridian/Termux services on the phone. That
installation is separate; ADB pairing does not install or restart it. Remote targets live in
`~/.phone-proxy-targets.conf`, initially empty with commented examples. Configure them through
the UI or edit that file. Samba credentials belong in the macOS Keychain, never in this repo.

**All on** enables missing connections once. **Continuous monitoring** saves its setting,
checks availability through the phone every 30 seconds, opens missing tunnels and mounts
available phone-hosted Samba shares without restarting working connections. Unavailable
targets are retried; failed Samba mount attempts are limited to once every five minutes.
**All off** disables monitoring before disconnecting. If a recovery job is already running,
it asks you to retry after that job finishes to avoid racing mount/unmount operations.
Root-only NAT recovery is skipped for non-rooted profiles.

The legacy rooted-device scripts live in [tablet-bin/tabctl/](./tablet-bin/tabctl/). Their
disk/power controls are for an explicitly configured rooted device, not an ADB replacement.

## Optional integrations

MiladSync, Toolbox Manager, Reader, Offload and Bedrock are separate applications, not copied
into this repository. The existing UI retains their integration points.

- `MERIDIAN_MILADSYNC_DIR`: path to an existing MiladSync checkout.
- `MERIDIAN_MILADSYNC_PYTHON`: its Python executable (defaults to that checkout's
  `venv_miladsync/bin/python`).
- `MERIDIAN_TOOLBOX_DIR`: an existing Toolbox Manager checkout (default `~/milad_one_toolbox`).
- `MERIDIAN_BEDROCK_URL`: your app-launcher URL, if using Bedrock login. No organization URL
  is bundled.

Personal runtime configuration, credentials, generated binaries, backups, logs and unrelated
applications are excluded. The vendored xterm assets retain their [MIT license](./vendor/xterm/LICENSE)
and [source/version record](./vendor/xterm/SOURCE.txt).

## Tests

```sh
python3 -m pip install -r requirements-dev.txt
python3 -m pytest -q
```

Tests use temporary state and fake ADB/process responses; no phone, proxy, real HOME or real
credentials are touched. Real pairing, OEM permissions, installed Tailscale behavior and
scrcpy streaming require a live phone check.
