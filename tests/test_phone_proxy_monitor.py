"""Continuous recovery, isolated from the real HOME, helpers and network."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def web(tmp_path, monkeypatch):
    import devices

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(devices, "active", lambda: ("raven", devices.DEFAULT["devices"]["raven"]))
    for name in ("bedrock_glue", "reader_glue", "aws_autologin", "offload_glue"):
        monkeypatch.setitem(sys.modules, name, None)
    path = Path(__file__).resolve().parent.parent / "phone-proxy-web.py"
    monkeypatch.setattr(sys, "argv", [str(path), "--no-monitor"])
    spec = importlib.util.spec_from_file_location("phone_proxy_monitor_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "DEVICE_ROOT", True)
    monkeypatch.setattr(module, "wifi_dev", lambda: "en0")
    clock = [1000.0]
    monkeypatch.setattr(module.time, "time", lambda: clock[0])
    module.test_clock = clock
    module.test_runs = []

    def run_job(title, argv, stdin_text=None):
        jid = str(len(module.test_runs) + 1)
        module.test_runs.append((title, argv))
        module.JOBS[jid] = {"done": False, "rc": None}
        return jid

    monkeypatch.setattr(module, "run_job", run_job)
    monkeypatch.setattr(module, "ssh_tab", lambda *args, **kwargs: (0, "", ""))
    return module


def target(name="desktop", **changes):
    return dict({"name": name, "lport": 8033, "rip": "100.1.2.3", "rport": "3389",
                 "fwd": True, "tunnel": True, "serve": False, "nat": "",
                 "up": False, "mounted": "", "vip": "", "natup": None}, **changes)


def setup_monitor(web, monkeypatch, targets, sshd=True):
    monkeypatch.setattr(web, "status", lambda: {"sshd": sshd, "targets": targets})
    mon = web.Monitor()
    mon.set_targets(True)
    return mon


def test_toggle_is_saved_and_disabled_does_not_probe(web, monkeypatch):
    mon = web.Monitor()
    assert not mon.saved["targets_on"]
    mon.set_targets(True)
    assert web.Monitor().saved["targets_on"]
    mon.set_targets(False)
    monkeypatch.setattr(web, "status", lambda: pytest.fail("disabled monitor checked targets"))
    mon.check_targets()
    assert not web.test_runs
    assert json.loads(Path(web.MON_FILE).read_text())["targets_on"] is False


def test_availability_returns_and_only_missing_connections_are_enabled(web, monkeypatch):
    rows = [target(), target("live", up=True), target("usb", serve=True, rip="usb:auto", lport=8040)]
    mon = setup_monitor(web, monkeypatch, rows)
    probes = []
    available = [False]

    def probe(cmd, **kwargs):
        probes.append(cmd)
        return (0 if available[0] else 1), "", ""

    monkeypatch.setattr(web, "ssh_tab", probe)
    mon.check_targets()
    assert len(probes) == 2 and not web.test_runs
    assert "/dev/tcp/127.0.0.1/8040" in probes[1]
    available[0] = True
    web.test_clock[0] += 30
    mon.check_targets()
    script = web.test_runs[0][1][-1]
    assert "tunnel desktop" in script and "tunnel usb" in script and "mount usb" in script
    assert "live" not in script
    assert "untunnel" not in script and "unmount" not in script
    assert web.test_runs[0][1][:2] == ["env", "MOUNT_WAIT=15"]


def test_active_jobs_do_not_overlap_and_failed_mounts_retry(web, monkeypatch):
    mon = setup_monitor(web, monkeypatch, [target("usb", serve=True, up=True)])
    web.JOBS["manual"] = {"done": False}
    mon.check_targets()
    assert not web.test_runs
    web.JOBS["manual"]["done"] = True
    mon.check_targets()
    assert "tunnel usb" not in web.test_runs[0][1][-1]
    web.test_clock[0] += 30
    mon.check_targets()
    assert len(web.test_runs) == 1
    web.JOBS[mon.target_job].update(done=True, rc=1)
    mon.check_targets()
    assert "Recovery log" in mon.target_msg
    assert len(web.test_runs) == 1
    web.test_clock[0] += 300
    mon.check_targets()
    assert len(web.test_runs) == 2


def test_off_during_probe_prevents_dispatch(web, monkeypatch):
    mon = setup_monitor(web, monkeypatch, [target()])

    def probe(*args, **kwargs):
        mon.set_targets(False)
        return 0, "", ""

    monkeypatch.setattr(web, "ssh_tab", probe)
    mon.check_targets()
    assert not web.test_runs


def test_unreachable_tablet_and_nat_only_target(web, monkeypatch):
    mon = setup_monitor(web, monkeypatch, [target()], sshd=False)
    monkeypatch.setattr(web, "ssh_tab", lambda *a, **k: pytest.fail("tablet is offline"))
    mon.check_targets()
    assert not web.test_runs and "SSH unavailable" in mon.target_msg
    rows = [target("natonly", tunnel=False, fwd=False, rport="-", nat="203",
                   vip="10.1.2.203", natup=False)]
    monkeypatch.setattr(web, "status", lambda: {"sshd": True, "targets": rows})
    probes = []
    monkeypatch.setattr(web, "ssh_tab", lambda cmd, **k: (probes.append(cmd) or 0, "", ""))
    web.test_clock[0] += 30
    mon.check_targets()
    assert probes == ["ping -c 1 -W 2 100.1.2.3 >/dev/null 2>&1"]
    assert "nat on" in web.test_runs[0][1][-1]
    assert "tunnel natonly" not in web.test_runs[0][1][-1]


def test_persistence_failure_is_reported_and_does_not_enable(web, monkeypatch):
    mon = web.Monitor()

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(web.os, "replace", fail)
    with pytest.raises(ValueError, match="Could not save"):
        mon.set_targets(True)
    assert not mon.saved["targets_on"]


def test_nonroot_tablet_recovers_tunnels_without_attempting_nat(web, monkeypatch):
    monkeypatch.setattr(web, "DEVICE_ROOT", False)
    rows = [target(nat="203", vip="10.1.2.203", natup=False),
            target("natonly", tunnel=False, fwd=False, rport="-", nat="204", vip="10.1.2.204", natup=False)]
    mon = setup_monitor(web, monkeypatch, rows)
    probes = []
    monkeypatch.setattr(web, "ssh_tab", lambda cmd, **k: (probes.append(cmd) or 0, "", ""))
    mon.check_targets()
    assert len(probes) == 1
    assert "tunnel desktop" in web.test_runs[0][1][-1]
    assert "nat on" not in web.test_runs[0][1][-1]


def test_alloff_disables_monitoring_before_build_and_waits_for_recovery(web, monkeypatch):
    import io

    mon = setup_monitor(web, monkeypatch, [target()])
    monkeypatch.setattr(web, "MONITOR", mon)
    handler = object.__new__(web.H)
    handler.path = "/api/alloff"
    handler.headers = {}
    handler.rfile = io.BytesIO(b"{}")
    monkeypatch.setattr(handler, "_ok", lambda: True)
    replies = []
    monkeypatch.setattr(handler, "_send", lambda code, body: replies.append((code, json.loads(body))))

    def build(action, body):
        assert action == "alloff" and not mon.saved["targets_on"]
        return "All off", ["echo", "off"], None

    monkeypatch.setattr(web, "build", build)
    handler.do_POST()
    assert replies[-1][0] == 200
    mon.set_targets(True)
    mon.target_job = "pending"
    web.JOBS["pending"] = {"done": False}
    handler.do_POST()
    assert replies[-1][0] == 400 and "still finishing" in replies[-1][1]["error"]
    assert not mon.saved["targets_on"]
