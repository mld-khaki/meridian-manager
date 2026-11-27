"""ADB behavior without a real phone, user configuration, or processes."""
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import adb_phone


@pytest.fixture()
def phone(tmp_path, monkeypatch):
    monkeypatch.setattr(adb_phone.shutil, "which", lambda name: "/tools/" + name)
    logs, calls = [], []
    ctl = adb_phone.PhoneADB(str(tmp_path), lambda: ("phone", {"label": "Test phone"}), logs.append)
    answers = {
        ("get-state",): "device",
        ("shell", "getprop", "ro.serialno"): "hardware-A",
        ("shell", "dumpsys", "battery"): "level: 50\nscale: 100\ntemperature: 310\nUSB powered: true\nstatus: 2",
        ("shell", "cmd", "package", "list", "packages", "-U", adb_phone.PACKAGE): "package:com.tailscale.ipn uid:10123",
        ("shell", "dumpsys", "connectivity"): "Current Networks:\nNetworkAgentInfo{ Transports: VPN OwnerUid: 10123 }",
        ("shell", "sh", "-c", "'pidof com.tailscale.ipn || true'"): "",
        ("shell", "settings", "get", "system", "screen_brightness"): "100",
    }

    def run(args, serial=None, input_text=None, timeout=10):
        calls.append((args, serial, input_text))
        answer = answers.get(tuple(args), "")
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(ctl, "run", run)
    ctl.test_answers, ctl.test_calls, ctl.test_logs = answers, calls, logs
    return ctl


def connect(phone):
    return phone.operation({"op": "connect", "device": "phone", "endpoint": "192.168.1.20:37000"})


def test_pairing_code_only_goes_to_stdin_and_is_not_saved(tmp_path, monkeypatch):
    calls, logs = [], []
    monkeypatch.setattr(adb_phone.shutil, "which", lambda _: "/tools/adb")

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="Successfully paired to 192.168.1.20:38000", stderr="")

    monkeypatch.setattr(adb_phone.subprocess, "run", run)
    ctl = adb_phone.PhoneADB(str(tmp_path), lambda: ("phone", {}), logs.append)
    r = ctl.operation({"op": "pair", "device": "phone", "pair_endpoint": "192.168.1.20:38000", "code": "123456"})
    assert r["ok"]
    assert calls[0][0] == ["/tools/adb", "pair", "192.168.1.20:38000"]
    assert calls[0][1]["input"] == "123456\n"
    assert not Path(ctl.path).exists()
    assert "123456" not in str(logs) + str(r)


def test_pair_failure_redacts_output_and_timeout_details(tmp_path, monkeypatch):
    monkeypatch.setattr(adb_phone.shutil, "which", lambda _: "/tools/adb")
    ctl = adb_phone.PhoneADB(str(tmp_path), lambda: ("phone", {}), lambda _: None)
    monkeypatch.setattr(adb_phone.subprocess, "run", lambda *a, **k:
                        SimpleNamespace(returncode=1, stdout="wrong code 123456", stderr="failed"))
    with pytest.raises(ValueError) as exc:
        ctl.run(["pair", "192.168.1.20:38000"], input_text="123456\n")
    assert "123456" not in str(exc.value)

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(["adb", "123456"], 30, output="123456")

    monkeypatch.setattr(adb_phone.subprocess, "run", timeout)
    with pytest.raises(ValueError) as exc:
        ctl.run(["pair", "192.168.1.20:38000"], input_text="123456\n")
    assert "123456" not in str(exc.value)


@pytest.mark.parametrize("value", ["192.168.1.20:0", "192.168.1.20:65536", "999.1.1.1:123",
                                  "192.168.1.20:123;touch /tmp/x", "-s another", None])
def test_bad_endpoint_never_reaches_adb(phone, value):
    with pytest.raises(ValueError):
        phone.operation({"op": "connect", "device": "phone", "endpoint": value})
    assert not phone.test_calls


def test_connect_pins_identity_and_requires_confirmation_for_replacement(phone):
    connect(phone)
    saved = json.loads(Path(phone.path).read_text())
    assert saved["phone"]["serial"] == "hardware-A"
    phone.test_answers[("shell", "getprop", "ro.serialno")] = "hardware-B"
    r = connect(phone)
    assert r["replace_required"] and not r["ok"]
    with pytest.raises(ValueError, match="different phone"):
        phone.operation({"op": "wake", "device": "phone"})
    assert not any(c[0] == ["shell", "input", "keyevent", "224"] for c in phone.test_calls)
    phone.operation({"op": "connect", "device": "phone", "endpoint": "192.168.1.20:37000", "replace": True})
    assert phone.profiles["phone"]["serial"] == "hardware-B"


def test_profile_switch_rejects_stale_ui_and_never_selects_default_device(phone, monkeypatch):
    connect(phone)
    monkeypatch.setattr(phone, "active_device", lambda: ("other", {}))
    with pytest.raises(ValueError, match="profile changed"):
        phone.operation({"op": "wake", "device": "phone"})
    phone.refresh()
    assert not phone.snapshot()["connected"]
    assert "other" not in phone.profiles


def test_battery_and_vpn_are_independent_of_app_process(phone):
    connect(phone)
    phone.refresh()
    s = phone.snapshot()
    assert s["battery"] == {"percent": 50.0, "temperature_c": 31.0, "powered": True, "status": "charging"}
    assert s["tailscale"]["vpn_active"] is True
    assert s["tailscale"]["process_running"] is False
    assert not s["capabilities"]["tailscale_on"]
    assert all(serial == "192.168.1.20:37000" for args, serial, _ in phone.test_calls if args[0] == "shell")


def test_connect_intent_is_probed_before_use_and_is_targeted(phone):
    connect(phone)
    action = adb_phone.PACKAGE + ".CONNECT_VPN"
    phone.test_answers[("shell", "cmd", "package", "resolve-activity", "--brief", "-a", action, "-p", adb_phone.PACKAGE)] = "com.tailscale.ipn/.AutomationActivity"
    phone.refresh()
    phone.operation({"op": "tailscale_on", "device": "phone"})
    assert (["shell", "am", "start", "-a", action, "-n", "com.tailscale.ipn/.AutomationActivity"],
            "192.168.1.20:37000", None) in phone.test_calls
    with pytest.raises(ValueError, match="no supported"):
        phone.operation({"op": "tailscale_off", "device": "phone"})


def test_receiver_fallback_uses_explicit_supported_component(phone):
    connect(phone)
    action = adb_phone.PACKAGE + ".DISCONNECT_VPN"
    phone.test_answers[("shell", "cmd", "package", "query-receivers", "--brief", "--components",
                        "-a", action, "-p", adb_phone.PACKAGE)] = "com.tailscale.ipn/.IPNReceiver"
    phone.refresh()
    phone.operation({"op": "tailscale_off", "device": "phone"})
    assert (["shell", "am", "broadcast", "-a", action, "-n", "com.tailscale.ipn/.IPNReceiver"],
            "192.168.1.20:37000", None) in phone.test_calls


def test_disconnection_clears_old_battery_and_vpn_status(phone):
    connect(phone)
    phone.refresh()
    phone.test_answers[("get-state",)] = ValueError("offline")
    phone.test_answers[("connect", "192.168.1.20:37000")] = ValueError("connection refused")
    phone.refresh()
    state = phone.snapshot()
    assert not state["connected"] and "refused" in state["message"]
    assert "battery" not in state and "tailscale" not in state


def test_off_is_persisted_and_does_not_disconnect_or_touch_proxy(phone):
    connect(phone)
    phone.test_calls.clear()
    phone.operation({"op": "off", "device": "phone"})
    phone.refresh()
    assert not phone.test_calls
    assert not phone.snapshot()["on"]
    restored = adb_phone.PhoneADB(phone.state_dir, phone.active_device, phone.log)
    assert not restored.profiles["phone"]["on"]


def test_brightness_verification_and_input_guards(phone):
    connect(phone)
    with pytest.raises(ValueError, match="integer"):
        phone.operation({"op": "brightness", "device": "phone", "value": True})
    with pytest.raises(ValueError, match="did not apply"):
        phone.operation({"op": "brightness", "device": "phone", "value": 50})
    phone.test_answers[("shell", "settings", "get", "system", "screen_brightness")] = "50"
    assert phone.operation({"op": "brightness", "device": "phone", "value": 50})["ok"]


def test_android_permission_failure_is_not_success(tmp_path, monkeypatch):
    monkeypatch.setattr(adb_phone.shutil, "which", lambda _: "/tools/adb")
    monkeypatch.setattr(adb_phone.subprocess, "run", lambda *a, **k:
                        SimpleNamespace(returncode=0, stdout="java.lang.SecurityException: Permission denial", stderr=""))
    ctl = adb_phone.PhoneADB(str(tmp_path), lambda: ("phone", {}), lambda _: None)
    with pytest.raises(ValueError, match="Permission denial"):
        ctl.shell("192.168.1.20:37000", "settings", "put", "secure", "x", "1")


def test_missing_tools_and_malformed_config_are_explicit(tmp_path, monkeypatch):
    monkeypatch.setattr(adb_phone.shutil, "which", lambda _: None)
    ctl = adb_phone.PhoneADB(str(tmp_path), lambda: ("phone", {}), lambda _: None)
    with pytest.raises(ValueError, match="not installed"):
        ctl.run(["version"])
    Path(ctl.path).write_text('{"phone": {"on": true, "endpoint": "wrong"}}')
    with pytest.raises(ValueError):
        adb_phone.PhoneADB(str(tmp_path), lambda: ("phone", {}), lambda _: None)


def test_scrcpy_uses_selected_device_and_only_stops_its_own_process(phone, monkeypatch):
    connect(phone)
    class Process:
        returncode = None
        terminated = False

        def poll(self):
            return self.returncode

        def wait(self, timeout):
            if not self.terminated:
                raise subprocess.TimeoutExpired(["scrcpy"], timeout)
            self.returncode = 0
            return 0

        def terminate(self):
            self.terminated = True

    process, spawned = Process(), []
    monkeypatch.setattr(adb_phone.subprocess, "Popen", lambda args, **kw: spawned.append(args) or process)
    assert phone.operation({"op": "scrcpy_start", "device": "phone"})["ok"]
    assert spawned[0][:3] == ["/tools/scrcpy", "--serial", "192.168.1.20:37000"]
    assert phone.snapshot()["scrcpy_running"]
    phone.operation({"op": "scrcpy_stop", "device": "phone"})
    assert process.terminated and not phone.snapshot()["scrcpy_running"]


@pytest.mark.parametrize("text,uid,result", [
    ("Current Networks:\nNetworkAgentInfo{ Transports: VPN OwnerUid: 10123 }", 10123, True),
    ("Current Networks:\nNetworkAgentInfo{ Transports: VPN OwnerUid: 999 }", 10123, False),
    ("Current Networks:\nNetworkAgentInfo{ Transports: VPN }", 10123, None),
    ("Current Networks:\nNetworkAgentInfo{ Transports: WIFI }", 10123, False),
    ("Permission denied", 10123, None),
])
def test_vpn_owner_attribution(text, uid, result):
    assert adb_phone.vpn_status(text, uid) is result


def test_malformed_battery_is_not_reported_as_zero_percent():
    with pytest.raises(ValueError, match="valid battery"):
        adb_phone.battery_status("temperature: 300\nlevel: 10")
    assert adb_phone.battery_status("level: 1\nscale: 2")["percent"] == 50


def test_remote_shell_arguments_remain_quoted(phone):
    phone.shell("192.168.1.20:37000", "sh", "-c", "pidof com.tailscale.ipn || true")
    assert phone.test_calls[-1][0] == ["shell", "sh", "-c", "'pidof com.tailscale.ipn || true'"]


def test_nonobject_request_is_rejected_without_adb_commands(phone):
    with pytest.raises(ValueError, match="JSON object"):
        phone.operation([])
    assert not phone.test_calls


def test_snapshot_does_not_wait_for_slow_phone_diagnostics(phone, monkeypatch):
    import threading

    phone.operation({"op": "connect", "device": "phone", "endpoint": "127.0.0.1:40000"})
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    original = phone.run

    def slow(args, **kwargs):
        if args == ["shell", "dumpsys", "battery"]:
            entered.set()
            assert release.wait(5)
        return original(args, **kwargs)

    monkeypatch.setattr(phone, "run", slow)
    worker = threading.Thread(target=phone.refresh)
    reader = threading.Thread(target=lambda: (phone.snapshot(), returned.set()))
    worker.start()
    try:
        assert entered.wait(2)
        reader.start()
        assert returned.wait(0.5), "Status requests are blocked by a phone diagnostic subprocess."
        assert phone.snapshot()["endpoint"] == "127.0.0.1:40000"
    finally:
        release.set()
        worker.join(2)
        if reader.ident:
            reader.join(2)


def test_hotspot_gateway_prefills_both_addresses_without_overwriting_choices():
    import ast
    import shutil

    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to exercise the UI address defaults.")
    tree = ast.parse((Path(__file__).resolve().parent.parent / "phone-proxy-web.py").read_text())
    page = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                and any(isinstance(a, ast.Name) and a.id == "PAGE" for a in n.targets))
    helpers = "let lastADB=" + page.split("let lastADB=", 1)[1].split("function renderADB", 1)[0]
    script = """
const assert = require('node:assert/strict');
const inputs = {'adb-address': {value:'',dataset:{}}, 'adb-pair': {value:'',dataset:{}}};
const document = {activeElement:null,getElementById:id=>inputs[id]};
""" + helpers + """
const address=inputs['adb-address'], pair=inputs['adb-pair'];
adbHotspotDefaults({net:true,mode:'hotspot',phone:'10.1.2.1'});
assert.equal(address.value,'10.1.2.1:');
assert.equal(pair.value,'10.1.2.1:');
adbAddressDefaults();
assert.equal(address.value,'10.1.2.1:');
adbHotspotDefaults({net:true,mode:'hotspot',phone:'10.3.4.1'});
assert.equal(address.value,'10.3.4.1:');
assert.equal(pair.value,'10.3.4.1:');
lastADB={endpoint:'192.168.1.20:37000'};
adbHotspotDefaults({net:true,mode:'hotspot',phone:'10.5.6.1'});
assert.equal(address.value,lastADB.endpoint);
assert.equal(pair.value,'10.5.6.1:');
address.value='10.5.6.1:40000';address.dataset.edited='1';
pair.value='10.5.6.1:41000';pair.dataset.edited='1';
adbHotspotDefaults({net:true,mode:'hotspot',phone:'10.7.8.1'});
adbAddressDefaults('192.168.1.20:37000');
assert.equal(address.value,'10.5.6.1:40000');
assert.equal(pair.value,'10.5.6.1:41000');
delete address.dataset.edited;delete pair.dataset.edited;lastADB=null;
document.activeElement=address;
adbHotspotDefaults({net:true,mode:'hotspot',phone:'10.9.10.1'});
assert.equal(address.value,'10.5.6.1:40000');
assert.equal(pair.value,'10.9.10.1:');
document.activeElement=null;
adbHotspotDefaults({net:true,mode:'lan',phone:'192.168.1.1'});
assert.equal(address.value,'');
assert.equal(pair.value,'');
adbHotspotDefaults({net:false,mode:'hotspot',phone:'10.9.10.1'});
assert.equal(address.value,'');
"""
    result = subprocess.run([node], input=script, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
