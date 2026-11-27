"""Exercise leaf shell actions with every external operation stubbed out."""
import re
import subprocess
from pathlib import Path


def function(name):
    text = (Path(__file__).resolve().parent.parent / "phone-proxy.sh").read_text()
    return re.search(r"(?ms)^" + re.escape(name) + r"\(\) \{.*?^\}", text).group()


def test_tunnel_that_remains_listening_reports_failure():
    script = """
want_del() { :; }
kill_tunnel() { :; }
sleep() { :; }
tunnel_up() { return 0; }
T_NAME=(desktop); T_LP=(8033)
""" + function("stop_tunnel") + "\nstop_tunnel 0\n"
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=5)
    assert result.returncode == 1 and "still listening" in result.stdout


def test_unmount_that_fails_reports_failure():
    script = """
serve_index() { echo 0; }
mounted_path() { echo /Volumes/example; }
diskutil() { return 1; }
umount() { return 1; }
""" + function("unmount_usb") + "\nunmount_usb example\n"
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=5)
    assert result.returncode == 1 and "Could not unmount" in result.stdout


def test_already_unmounted_share_remains_a_successful_noop():
    script = """
serve_index() { echo 0; }
mounted_path() { :; }
""" + function("unmount_usb") + "\nunmount_usb example\n"
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0 and "Not mounted" in result.stdout
