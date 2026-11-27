"""devices.py: profiles, validation, and the ssh 'Host tab' rewrite, all against a temporary HOME."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import devices  # noqa: E402


@pytest.fixture()
def home(tmp_path, monkeypatch):
    st = tmp_path / ".phone-proxy"
    st.mkdir()
    (tmp_path / ".ssh").mkdir()
    monkeypatch.setattr(devices, "STATE", str(st))
    monkeypatch.setattr(devices, "DEVICES_FILE", str(st / "devices.json"))
    monkeypatch.setattr(devices, "SSH_CONFIG", str(tmp_path / ".ssh" / "config"))
    monkeypatch.setattr(devices, "TABIN_CFG", str(st / "tabinput.json"))
    monkeypatch.setattr(devices, "PHONE_HOST_F", str(st / "phone_host"))
    (tmp_path / ".ssh" / "config").write_text("Host other\n    Port 22\n\nHost tab\n    Port 8022\n    User old\n\nHost last\n    Port 1\n")
    (st / "tabinput.json").write_text('{"tw": 2800, "th": 1752, "speed": 0.4}')
    (st / "phone_host").write_text("1.2.3.4")
    return tmp_path


def add_phone():
    d = devices.load()
    d["devices"]["pixel"] = {"label": "Spare", "user": "u0_a77", "ssid": "PixelHS", "ts": "100.70.1.2",
                             "screen": [1080, 2400], "hostkey": "termux-pixel"}
    devices.save(d)


def test_default_is_raven(home):
    n, dev = devices.active()
    assert n == "raven" and dev["ssid"] == "PhoneHotspot" and not dev["root"] and not devices.validate(n, dev)


def test_use_rewrites_only_the_tab_block_and_keeps_a_backup(home):
    add_phone()
    devices.use("pixel")
    text = (home / ".ssh" / "config").read_text()
    assert "User u0_a77" in text and "HostKeyAlias termux-pixel" in text and "100.70.1.2" in text
    assert "User old" not in text and "Host other" in text and "Host last" in text
    assert list((home / ".ssh").glob("config.bak-*"))
    assert json.loads((home / ".phone-proxy" / "tabinput.json").read_text()) == {"tw": 1080, "th": 2400, "speed": 0.4}
    assert not (home / ".phone-proxy" / "phone_host").exists()


def test_switching_back_is_idempotent_and_leaves_one_block(home):
    add_phone()
    devices.use("pixel")
    devices.use("raven")
    assert devices.apply_ssh_config(devices.active()[1]) is False
    text = (home / ".ssh" / "config").read_text()
    assert text.count("Host tab") == 1 and devices.DEFAULT["devices"]["raven"]["user"] in text and "Host last" in text


def test_incomplete_device_is_refused(home):
    d = devices.load()
    d["devices"]["bad"] = {"user": "x"}
    devices.save(d)
    with pytest.raises(SystemExit):
        devices.use("bad")
    assert devices.load()["active"] == "raven"


def test_unknown_device_is_refused(home):
    with pytest.raises(SystemExit):
        devices.use("nope")


def test_hostile_values_are_rejected(home):
    dev = dict(devices.DEFAULT["devices"]["raven"], ssid='x"\nHost evil')
    assert devices.validate("raven", dev)
    dev = dict(devices.DEFAULT["devices"]["raven"], user="a; rm -rf ~")
    assert devices.validate("raven", dev)


def test_no_hotspot_device_needs_no_ssid_and_gets_lan_in_the_ssh_path(home):
    d = devices.load()
    d["devices"]["note"] = {"label": "Note", "user": "u0_a9", "ts": "100.70.1.9", "screen": [1440, 3200],
                            "hostkey": "termux-note", "hotspot": False, "root": False, "lan": "10.0.0.55"}
    devices.save(d)
    assert not devices.validate("note", d["devices"]["note"])
    devices.use("note")
    text = (home / ".ssh" / "config").read_text()
    assert "10.0.0.55 100.70.1.9" in text


def test_hotspot_device_still_needs_an_ssid(home):
    dev = dict(devices.DEFAULT["devices"]["raven"])
    dev["ssid"] = ""
    assert devices.validate("raven", dev)
    dev["hotspot"] = False
    assert not devices.validate("raven", dev)


def test_bad_lan_address_is_rejected(home):
    dev = dict(devices.DEFAULT["devices"]["raven"], lan="10.0.0.999")
    assert devices.validate("raven", dev)
