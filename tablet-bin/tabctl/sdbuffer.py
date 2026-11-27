#!/data/data/com.termux/files/usr/bin/python3
"""sdbuffer.py - the tablet's microSD card as the drive share's buffer (runs as root).

The Mac mounts one share, [usb] on both smbd instances, whose path is the link LINK. While
the SSD is mounted the link points at the SSD's shared folder; while it is not, at the
buffer: the whole microSD card, LUKS2 with ext4, opened with a random key kept in /data/adb,
so it needs no passphrase and the card on its own is unreadable. The Mac sees one plain
mount either way; while the SSD is away it shows only what was written since. When the SSD
is back, the files move from the buffer into the same folders on the SSD. A different file
already at a path is never overwritten: the incoming one is kept as "name 2.ext", the way
Finder's Keep Both names it, and Finder's own .DS_Store files give way to the SSD's.

  sudo python3 ~/tabctl/sdbuffer.py format   erase the microSD card and make it the buffer (asks)
       format --check            say which card it would erase, and the confirm token for it
       format --confirm TOKEN    erase that card without a terminal: phone-proxy.sh asks on the Mac
  sudo python3 ~/tabctl/sdbuffer.py open     unlock and mount the buffer (nothing to do if it is)
  sudo python3 ~/tabctl/sdbuffer.py close    point the share away from the buffer, unmount, lock
  sudo python3 ~/tabctl/sdbuffer.py point    point the share at the SSD or the buffer, as things are
  sudo python3 ~/tabctl/sdbuffer.py away     the SSD is about to go: stop moving, point at the buffer
                                             and keep it there until the SSD's mount is gone
  sudo python3 ~/tabctl/sdbuffer.py up       the SSD was just unlocked: point at it, start moving
  sudo python3 ~/tabctl/sdbuffer.py sync     move the buffered files onto the SSD
  sudo python3 ~/tabctl/sdbuffer.py tick     open, point and start a sync when due (the watch's minute)
  sudo python3 ~/tabctl/sdbuffer.py status   key=value lines for root.sh status and the watch

root.sh runs away before drive-down.sh and up after drive-up.sh; tick starts the two smbd
instances (root.sh smb-start) when the buffer serves the share without the SSD, and makes
the share's path in both smb.conf files the link, should drive-up.sh have written the SSD's
folder there (while the SSD is mounted the link points at that same folder).

A file moves once no process has it open and it has not changed for SETTLE seconds. The
copy goes to a hidden temporary name beside its target, is flushed, read back from the SSD
and compared, takes its name through a hard link (so it can never replace a file that
appeared meanwhile), and only then leaves the buffer.
"""
import errno
import fcntl
import glob
import hashlib
import json
import os
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import time

TERMUX_BIN = "/data/data/com.termux/files/usr/bin"
PATH = TERMUX_BIN + ":/product/bin:/debug_ramdisk:/system/bin:/system/xbin"   # root.sh's
HOME = "/data/data/com.termux/files/home"
STATE_DIR = "/data/local/ssd"                    # root.sh's STATE: the smbd configs live here
KEY = "/data/adb/sdbuffer.key"                   # root-only, like the SSD's optional key file
INFO = STATE_DIR + "/sdbuffer.json"              # the card's LUKS UUID, written by format
LAST = STATE_DIR + "/sdbuffer-sync.json"         # what the last sync did, written by sync
LOG = STATE_DIR + "/sdbuffer.log"
SYNC_LOCK = STATE_DIR + "/sdbuffer-sync.lock"    # held by the running sync, with its pid
POINT_LOCK = STATE_DIR + "/sdbuffer-point.lock"  # one change of the link at a time
OPEN_LOCK = STATE_DIR + "/sdbuffer-open.lock"    # one opening (and disk check) of the card at a time
LINK = STATE_DIR + "/drive"                      # the drive share's path in both smb.conf files
AWAY = STATE_DIR + "/sdbuffer-away"              # the SSD is being locked: the mount id it had
ROOT_SH = HOME + "/tabctl/root.sh"
MAPPER = "sdbuffer"
DM = "/dev/mapper/" + MAPPER
DM_CONTROL = "/dev/mapper/control"               # libdevmapper's; Android has only /dev/device-mapper
SYS_MISC_DM = "/sys/class/misc/device-mapper/dev"
MNT = "/mnt/sdbuffer"
BUF = MNT + "/share"
SSD_MNT = "/mnt/ssd"
SSD_SHARE = SSD_MNT + "/share"
SMB_INSTANCES = ("tunnel", "lan")
SETTLE = 60                 # seconds a file stays unchanged before it moves
MARGIN = 256 << 20          # bytes the SSD keeps free
CHUNK = 4 << 20
FINDER_FILES = (".DS_Store",)
SYS_BLOCK = "/sys/block"
SYS_DEV = "/sys/dev/block"
PROC = "/proc"


class Stopped(Exception):
    pass


def log(msg):
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > 2 << 20:
            os.replace(LOG, LOG + ".1")
        with open(LOG, "a") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def run(argv, timeout=60, env=None):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env, stdin=subprocess.DEVNULL)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 127, str(e)


def tool(name):
    """Termux's copy of an e2fsprogs tool, else Android's."""
    for p in (TERMUX_BIN + "/" + name, "/system/bin/" + name):
        if os.path.exists(p):
            return p
    return name


def ensure_dm_control():
    """cryptsetup open and close need /dev/mapper/control, which /dev lacks after every boot
    until drive-up.sh makes it; with the SSD still locked, make it here the same way."""
    if os.path.exists(DM_CONTROL):
        return True
    try:
        os.makedirs(os.path.dirname(DM_CONTROL), exist_ok=True)
        with open(SYS_MISC_DM) as f:
            major, minor = (int(x) for x in f.read().strip().split(":"))
        os.mknod(DM_CONTROL, stat.S_IFCHR | 0o600, os.makedev(major, minor))
        return True
    except (OSError, ValueError) as e:
        log("could not make %s: %s" % (DM_CONTROL, e))
        return False


def cryptsetup(args, timeout=60):
    ensure_dm_control()
    return run(["cryptsetup"] + args, timeout=timeout)


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def lock(path, wait=0.0):
    """An exclusive flock on path, or None when another process holds it after `wait` s."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    end = time.time() + wait
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            if time.time() >= end:
                os.close(fd)
                return None
            time.sleep(0.2)


def unlock(fd):
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ---- where things are ---------------------------------------------------------------
def mounts():
    try:
        with open(PROC + "/mounts") as f:
            return [line.split() for line in f]
    except OSError:
        return []


def mounted(path):
    return any(len(m) > 1 and m[1] == path for m in mounts())


def usable(path):
    """The folder is there and answers: a pulled drive leaves a mount that fails with EIO."""
    try:
        os.statvfs(path)
        return os.path.isdir(path)
    except OSError:
        return False


def mount_id(path):
    """The kernel's id of the mount at path (the top one), or None."""
    mid = None
    try:
        with open(PROC + "/self/mountinfo") as f:
            for line in f:
                p = line.split()
                if len(p) > 4 and p[4] == path:
                    mid = p[0]
    except OSError:
        pass
    return mid


def dev_attached(dev):
    """The block device dev, and for dm-crypt the partition under it, is still in sysfs. A
    pulled drive or card leaves its mount and its mapping behind, and the folders the kernel
    has cached keep answering, but its disk leaves sysfs."""
    try:
        rdev = os.stat(dev).st_rdev
    except OSError:
        return False
    if not rdev:
        return False
    base = "%s/%d:%d" % (SYS_DEV, os.major(rdev), os.minor(rdev))
    if not os.path.isdir(base):
        return False
    slaves = glob.glob(base + "/slaves/*")
    if os.path.isdir(base + "/dm") and not slaves:
        return False
    return all(os.path.exists(os.path.realpath(s)) for s in slaves)


def ssd_attached():
    """The disk under the SSD's mount is still plugged in."""
    src = None
    for m in mounts():
        if len(m) > 1 and m[1] == SSD_MNT:
            src = m[0]
    return bool(src) and dev_attached(src)


def card_pulled():
    """The buffer is open, or its mapping is still there, but its card is gone."""
    return (mounted(MNT) and not usable(BUF)) or (os.path.exists(DM) and not dev_attached(DM))


def ssd_going():
    """away ran for the SSD's present mount: keep the share off it until that mount is gone."""
    try:
        with open(AWAY) as f:
            mid = f.read().strip()
    except OSError:
        return False
    try:
        fresh = time.time() - os.path.getmtime(AWAY) < 600     # a lock that never finished lets go
    except OSError:
        fresh = False
    if mid and fresh and mid == mount_id(SSD_MNT):
        return True
    clear_away()
    return False


def clear_away():
    try:
        os.unlink(AWAY)
    except FileNotFoundError:
        pass


def ssd_ready():
    return mounted(SSD_MNT) and ssd_attached() and usable(SSD_SHARE) and not ssd_going()


def buffer_ready():
    return mounted(MNT) and usable(BUF)


def link_target():
    try:
        return os.readlink(LINK)
    except OSError:
        return None


def set_link(target):
    if target is None:
        try:
            os.unlink(LINK)
        except FileNotFoundError:
            pass
        return
    tmp = LINK + ".new"
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    os.symlink(target, tmp)
    os.replace(tmp, LINK)        # never a moment without a link


def sd_cards():
    """MicroSD cards only: the kernel types SD cards "SD" and eMMC "MMC"; the UFS and the
    USB disks are sd* devices and never listed here."""
    out = []
    for d in sorted(glob.glob(SYS_BLOCK + "/mmcblk*")):
        name = os.path.basename(d)
        try:
            with open(d + "/device/type") as f:
                kind = f.read().strip()
            with open(d + "/size") as f:
                size = int(f.read()) * 512
        except (OSError, ValueError):
            continue
        if kind != "SD":
            continue
        parts = sorted(p for p in os.listdir(d) if p.startswith(name + "p"))
        out.append({"disk": "/dev/block/" + name, "parts": ["/dev/block/" + p for p in parts], "size": size})
    return out


def luks_uuid(dev):
    rc, out = run(["cryptsetup", "luksUUID", dev], timeout=20)
    return out.strip() if rc == 0 else ""


def find_card(uuid):
    for card in sd_cards():
        for dev in card["parts"] or [card["disk"]]:
            if uuid and luks_uuid(dev) == uuid:
                return dev
    return None


# ---- Samba -------------------------------------------------------------------------
def smb_conf(inst):
    return "%s/%s/smb.conf" % (STATE_DIR, inst)


def share_names():
    """The drive share's section name in the smb.conf files (usb today)."""
    names = set()
    for inst in SMB_INSTANCES:
        section = None
        try:
            with open(smb_conf(inst)) as f:
                for line in f:
                    s = line.strip()
                    if s.startswith("[") and s.endswith("]"):
                        section = s[1:-1].strip()
                        continue
                    k, sep, v = s.partition("=")
                    if sep and section and k.strip().lower() == "path" and v.strip() in (LINK, SSD_SHARE, BUF):
                        names.add(section)
        except OSError:
            continue
    return sorted(names) or ["usb"]


def adopt_confs():
    """Make the drive share's path the link in both smb.conf files, where drive-up.sh may
    have written the SSD's folder; only that one line changes. smbd reads it again at once."""
    for inst in SMB_INSTANCES:
        conf = smb_conf(inst)
        try:
            st = os.stat(conf)
            with open(conf) as f:
                lines = f.readlines()
        except OSError:
            continue
        changed = False
        for i, line in enumerate(lines):
            k, sep, v = line.partition("=")
            if sep and k.strip().lower() == "path" and v.strip() and os.path.normpath(v.strip()) == SSD_SHARE:
                lines[i] = line[:len(line) - len(line.lstrip())] + "path = " + LINK + "\n"
                changed = True
        if not changed:
            continue
        tmp = conf + ".sdbuffer"
        with open(tmp, "w") as f:
            f.writelines(lines)
            f.flush()
            os.fsync(f.fileno())
        os.chown(tmp, st.st_uid, st.st_gid)
        os.chmod(tmp, stat.S_IMODE(st.st_mode))
        os.replace(tmp, conf)
        rc, out = run(["smbcontrol", "-s", conf, "smbd", "reload-config"], timeout=20)
        log("%s: the drive share's path is now %s (reload: rc %d)" % (conf, LINK, rc))


def reconnect_clients():
    """Close the drive share's connections, so each client reconnects to where the link
    points now (Finder does that by itself)."""
    failed = False
    for inst in SMB_INSTANCES:
        conf = smb_conf(inst)
        if not os.path.exists(conf):
            continue
        for name in share_names():
            rc, out = run(["smbcontrol", "-s", conf, "all", "close-share", name], timeout=20)
            if rc:
                failed = True
                log("smbcontrol close-share %s (%s): rc %d %s" % (name, inst, rc, out[:200]))
    # without smbcontrol, restarting both servers drops every client just the same (from the
    # configs as they are: drive-up.sh is not run, so nothing points the share again)
    if failed:
        rc, out = run(["sh", ROOT_SH, "smb-start", "restart"], timeout=120)
        log("restarted smbd instead: rc %d %s" % (rc, out[-200:].replace("\n", " ")))


# ---- pointing the share --------------------------------------------------------------
def where(target):
    return {SSD_SHARE: "ssd", BUF: "buffer", None: "none"}.get(target, "other")


def point(away=False):
    """Point the share at the SSD when it is mounted, else at the buffer when it is open,
    else at nothing (the share then refuses connections). away: the SSD is about to go, so
    the share stays off it until its mount is gone, whoever points it meanwhile."""
    fd = lock(POINT_LOCK, wait=60)
    if fd is None:
        return "busy"
    try:
        if away:
            stop_sync()
            mid = mount_id(SSD_MNT)
            if mid:
                with open(AWAY, "w") as f:
                    f.write(mid)
        if not away and ssd_ready():
            want = SSD_SHARE
        elif buffer_ready():
            want = BUF
        else:
            want = None
        have = link_target()
        if want == have:
            return where(want)
        set_link(want)
        log("share now points at %s (was %s)" % (where(want), where(have)))
        reconnect_clients()
        return where(want)
    finally:
        unlock(fd)


# ---- opening and closing -----------------------------------------------------------
def fail(msg):
    print(msg)
    log("error: " + msg)
    return 1


def open_buffer():
    uuid = load(INFO).get("uuid")
    if not uuid:
        return fail("The card has not been made the buffer: run sudo python3 ~/tabctl/sdbuffer.py format")
    if not os.path.exists(KEY):
        return fail("The buffer's key %s is missing, so the card cannot be opened. Format it again." % KEY)
    fd = lock(OPEN_LOCK)
    if fd is None:
        return fail("The buffer is being opened by another run (its disk check can take a while).")
    try:
        if os.path.exists(DM) and not dev_attached(DM) and not mounted(MNT):
            rc, out = cryptsetup(["close", MAPPER])     # left by a card pulled while open
            if rc:
                return fail("The buffer's old mapping is still in use (cryptsetup close: %s)." % out[-200:])
        if not os.path.exists(DM):
            dev = find_card(uuid)
            if not dev:
                return fail("The buffer's microSD card is not in the tablet.")
            rc, out = cryptsetup(["open", "--key-file", KEY, dev, MAPPER], timeout=90)
            if rc:
                return fail("cryptsetup open %s: %s" % (dev, out[-300:]))
            log("opened %s" % dev)
        if not mounted(MNT):
            rc, out = run([tool("e2fsck"), "-p", DM], timeout=1800)   # as drive-up.sh does for the SSD
            if rc == 127:
                log("no e2fsck: mounting without a check")
            elif rc >= 4:
                return fail("e2fsck found errors on the buffer it could not fix (exit %d). Run: sudo e2fsck -f %s" % (rc, DM))
            elif rc:
                log("e2fsck -p fixed the buffer (exit %d): %s" % (rc, out[-200:].replace("\n", " ")))
            os.makedirs(MNT, exist_ok=True)
            rc, out = run(["/system/bin/mount", "-t", "ext4", "-o", "noatime,nosuid,nodev", DM, MNT], timeout=90)
            if rc:
                return fail("mount %s: %s" % (MNT, out[-300:]))
            log("mounted %s" % MNT)
        os.makedirs(BUF, exist_ok=True)
        return 0
    finally:
        unlock(fd)


def close_buffer():
    stop_sync()
    fd = lock(POINT_LOCK, wait=60)
    try:
        if link_target() == BUF:
            set_link(SSD_SHARE if ssd_ready() else None)
            log("share now points at %s (closing the buffer)" % where(link_target()))
            reconnect_clients()
        for _ in range(5):
            if not mounted(MNT):
                break
            rc, out = run(["/system/bin/umount", MNT], timeout=60)
            if rc:
                log("umount %s: %s" % (MNT, out[-200:]))
                time.sleep(2)
        if mounted(MNT):
            return fail("The buffer is still in use and was not closed (umount %s failed)." % MNT)
        if os.path.exists(DM):
            rc, out = cryptsetup(["close", MAPPER])
            if rc:
                return fail("cryptsetup close: %s" % out[-300:])
        log("closed")
        return 0
    finally:
        if fd is not None:
            unlock(fd)


def stop_sync(wait=30):
    """Stop a running sync (between two blocks of a file; the half-copied file is dropped)."""
    fd = lock(SYNC_LOCK)
    if fd is not None:
        unlock(fd)
        return True
    try:
        with open(SYNC_LOCK) as f:
            pid = int(f.read().strip() or 0)
        if pid > 1:
            os.kill(pid, signal.SIGTERM)
    except (OSError, ValueError):
        pass
    fd = lock(SYNC_LOCK, wait=wait)
    if fd is None:
        log("the sync did not stop within %d s" % wait)
        return False
    unlock(fd)
    return True


# ---- moving files -------------------------------------------------------------------
def busy_paths(root):
    """Paths under root that some process has open (smbd while a Mac still writes)."""
    out = set()
    for fd in glob.glob(PROC + "/[0-9]*/fd/*"):
        try:
            p = os.readlink(fd)
        except OSError:
            continue
        if p == root or p.startswith(root + "/"):
            out.add(p)
    return out


def free_name(path, is_dir=False):
    base, ext = (path, "") if is_dir else os.path.splitext(path)
    n = 2
    while os.path.lexists("%s %d%s" % (base, n, ext)):
        n += 1
    return "%s %d%s" % (base, n, ext)


def same_content(a, b):
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        with open(a, "rb") as fa, open(b, "rb") as fb:
            while True:
                x, y = fa.read(CHUNK), fb.read(CHUNK)
                if x != y:
                    return False
                if not x:
                    return True
    except OSError:
        return False


def copy_meta(src_st, src, dst):
    if hasattr(os, "listxattr"):      # Finder's tags, comments and resource forks (Samba keeps them as xattrs)
        try:
            for name in os.listxattr(src, follow_symlinks=False):
                if name.startswith("security.selinux"):
                    continue
                try:
                    os.setxattr(dst, name, os.getxattr(src, name, follow_symlinks=False), follow_symlinks=False)
                except OSError:
                    pass
        except OSError:
            pass
    os.chmod(dst, stat.S_IMODE(src_st.st_mode))
    if os.geteuid() == 0:
        os.chown(dst, src_st.st_uid, src_st.st_gid)
    os.utime(dst, ns=(src_st.st_atime_ns, src_st.st_mtime_ns))


def fsync_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def hard_link(src, dst):
    """os.link, which Termux's Python does not have: call libc's link() (it never replaces dst,
    and raises FileExistsError when dst exists, like os.link)."""
    if hasattr(os, "link"):
        return os.link(src, dst)
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.link(os.fsencode(src), os.fsencode(dst)) != 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e), dst)


def place(src, dst, st, stop):
    """Copy src to dst without ever replacing anything at dst. Raises FileExistsError when
    dst appeared meanwhile, Stopped when asked to stop, OSError when the copy reads back wrong."""
    tmp = os.path.join(os.path.dirname(dst), ".sdbuffer-%s.part" % secrets.token_hex(4))
    want = hashlib.sha256()
    try:
        with open(src, "rb") as fi, open(tmp, "xb") as fo:
            while True:
                block = fi.read(CHUNK)
                if not block:
                    break
                if stop():
                    raise Stopped()
                fo.write(block)
                want.update(block)
            fo.flush()
            os.fsync(fo.fileno())
        got = hashlib.sha256()
        with open(tmp, "rb") as f:                 # read back from the SSD itself, not the page cache
            if hasattr(os, "posix_fadvise"):
                os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
            while True:
                block = f.read(CHUNK)
                if not block:
                    break
                got.update(block)
        if got.digest() != want.digest():
            raise OSError(errno.EIO, "the copy on the SSD reads back different")
        copy_meta(st, src, tmp)
        hard_link(tmp, dst)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
    fsync_dir(os.path.dirname(dst))


def ensure_dir(dst_dir, src_dir, dst_root, src_root):
    """Make dst_dir (and the folders above it) like their buffer counterparts."""
    if os.path.isdir(dst_dir) or os.path.normpath(dst_dir) == os.path.normpath(dst_root):
        return
    ensure_dir(os.path.dirname(dst_dir), os.path.dirname(src_dir), dst_root, src_root)
    os.mkdir(dst_dir)
    try:
        st = os.lstat(src_dir)
        os.chmod(dst_dir, stat.S_IMODE(st.st_mode))
        if os.geteuid() == 0:
            os.chown(dst_dir, st.st_uid, st.st_gid)
    except OSError:
        pass


def move_tree(src, dst, settle=SETTLE, t_now=None, busy=frozenset(), stop=lambda: False, room=lambda n: True):
    """One pass: move every settled, closed file under src into the same place under dst.
    Folders a file name already takes on dst get a free name ("Photos 2"); emptied folders
    leave src once they too have settled. Returns what it did."""
    t_now = time.time() if t_now is None else t_now
    res = {"moved": 0, "bytes": 0, "same": 0, "kept_both": [], "errors": [], "waiting": 0, "full": False, "stopped": False}
    dirmap = {src: dst}
    for root, dirs, files in os.walk(src):
        dirs.sort()
        files.sort()
        droot = dirmap.get(root)
        if droot is None:
            dirs[:] = []
            continue
        for d in list(dirs):
            sp, dp = os.path.join(root, d), os.path.join(droot, d)
            if os.path.islink(sp):
                dirs.remove(d)
                res["errors"].append("%s: a link, left in the buffer" % os.path.relpath(sp, src))
                continue
            if os.path.lexists(dp) and (os.path.islink(dp) or not os.path.isdir(dp)):
                dp = free_name(dp, is_dir=True)
                res["kept_both"].append(os.path.relpath(dp, dst))
            dirmap[sp] = dp
        for name in files:
            if stop():
                res["stopped"] = True
                return res
            sp = os.path.join(root, name)
            rel = os.path.relpath(sp, src)
            try:
                st = os.lstat(sp)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                res["errors"].append("%s: not a plain file, left in the buffer" % rel)
                continue
            if sp in busy or max(st.st_mtime, st.st_ctime) > t_now - settle:
                res["waiting"] += 1
                continue
            dp = os.path.join(droot, name)
            try:
                ensure_dir(droot, root, dst, src)
                if os.path.lexists(dp):
                    if os.path.isfile(dp) and not os.path.islink(dp) and same_content(sp, dp):
                        os.unlink(sp)
                        res["same"] += 1
                        continue
                    if name in FINDER_FILES:
                        os.unlink(sp)
                        continue
                    dp = free_name(dp)
                if not room(st.st_size):
                    res["full"] = True
                    res["errors"].append("the SSD has no room for %s (%d MB)" % (rel, st.st_size >> 20))
                    return res
                try:
                    place(sp, dp, st, stop)
                except FileExistsError:            # the Mac wrote that name meanwhile: keep both
                    dp = free_name(dp)
                    place(sp, dp, st, stop)
                if os.path.basename(dp) != name:
                    res["kept_both"].append(os.path.relpath(dp, dst))
                os.unlink(sp)
                res["moved"] += 1
                res["bytes"] += st.st_size
            except Stopped:
                res["stopped"] = True
                return res
            except OSError as e:
                res["errors"].append("%s: %s" % (rel, e.strerror or e))
    for root, dirs, files in os.walk(src, topdown=False):
        if root == src or stop():
            continue
        try:
            if os.listdir(root):
                continue
            st = os.lstat(root)
        except OSError:
            continue
        if root in busy or max(st.st_mtime, st.st_ctime) > t_now - settle:
            res["waiting"] += 1
            continue
        try:
            if root in dirmap:
                ensure_dir(dirmap[root], root, dst, src)   # an empty folder made on purpose appears on the SSD too
            os.rmdir(root)
        except OSError:
            pass
    return res


def ssd_room(n):
    try:
        v = os.statvfs(SSD_SHARE)
        return v.f_bavail * v.f_frsize - MARGIN >= n
    except OSError:
        return False


def waiting():
    """(files, bytes) in the buffer."""
    files = size = 0
    for root, dirs, names in os.walk(BUF):
        for name in names:
            try:
                st = os.lstat(os.path.join(root, name))
            except OSError:
                continue
            files += 1
            size += st.st_size
    return files, size


def has_entries():
    try:
        return bool(os.listdir(BUF))
    except OSError:
        return False


def sync():
    fd = lock(SYNC_LOCK)
    if fd is None:
        print("A sync is already running.")
        return 0
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    stopping = []
    signal.signal(signal.SIGTERM, lambda *a: stopping.append(1))
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    t0 = time.time()
    total = {"started": t0, "moved": 0, "bytes": 0, "same": 0, "kept_both": [], "errors": [], "waiting": 0}
    try:
        seen = {"t": 0.0, "off": False}

        def stop():
            # place asks before every block: look at the SSD (a few /proc reads) once a second at most
            if time.time() - seen["t"] >= 1:
                seen["t"], seen["off"] = time.time(), link_target() != SSD_SHARE or not ssd_ready()
            return bool(stopping) or seen["off"]
        while not stop() and buffer_ready():
            r = move_tree(BUF, SSD_SHARE, settle=SETTLE, busy=busy_paths(MNT), stop=stop, room=ssd_room)
            for k in ("moved", "bytes", "same", "waiting"):
                total[k] = r[k] if k == "waiting" else total[k] + r[k]
            total["kept_both"] = (total["kept_both"] + r["kept_both"])[-20:]
            total["errors"] = (total["errors"] + r["errors"])[-20:]
            total["full"] = r["full"]
            if r["moved"] == 0 or r["full"] or r["stopped"]:
                break
        if not stopping and buffer_ready() and not has_entries() and shutil.which("fstrim"):
            rc, out = run(["fstrim", MNT], timeout=600)     # the card writes faster with its free blocks erased
            if rc:
                log("fstrim: rc %d %s" % (rc, out[:200]))
        total["ended"] = time.time()
        total["stopped"] = bool(stopping)
        did = total["moved"] or total["same"] or total["errors"] or total["kept_both"]
        if did or (buffer_ready() and not has_entries()):   # a pass with nothing to do keeps the last report,
            save(LAST, total)                                # unless the buffer is empty: old errors are over then
        if did:
            log("sync: %d files (%d MB) onto the SSD in %d s, %d identical dropped, kept both %s, errors %s"
                % (total["moved"], total["bytes"] >> 20, total["ended"] - t0, total["same"],
                   total["kept_both"] or "none", total["errors"] or "none"))
        return 0
    finally:
        unlock(fd)


def start_sync():
    """Run sync in its own session, so it outlives the watch's call."""
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "sync"], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def syncing():
    fd = lock(SYNC_LOCK)
    if fd is None:
        return True
    unlock(fd)
    return False


# ---- status, tick ------------------------------------------------------------------
def status():
    kv = []
    info = load(INFO)
    if not info.get("uuid"):
        state = "none"
    elif mounted(MNT):
        state = "open" if usable(BUF) else "error"
    elif os.path.exists(DM) and not dev_attached(DM):
        state = "error"                          # pulled while open; tick closes what is left
    elif os.path.exists(DM) or find_card(info["uuid"]):
        state = "ready"
    else:
        state = "absent"
    kv.append(("sdbuf", state))
    kv.append(("sdbuf_key", "yes" if os.path.exists(KEY) else "no"))
    kv.append(("sdbuf_target", where(link_target())))
    kv.append(("sdbuf_syncing", "yes" if syncing() else "no"))
    if state == "open":
        v = os.statvfs(MNT)
        kv.append(("sdbuf_size_k", v.f_blocks * v.f_frsize // 1024))
        kv.append(("sdbuf_avail_k", v.f_bavail * v.f_frsize // 1024))
        files, size = waiting()
        kv.append(("sdbuf_files", files))
        kv.append(("sdbuf_bytes", size))
    last = load(LAST)
    if last:
        kv.append(("sdbuf_last_sync", int(last.get("ended") or last.get("started") or 0)))
        kv.append(("sdbuf_last_moved", last.get("moved", 0)))
        kv.append(("sdbuf_last_kept", len(last.get("kept_both") or [])))
        if last.get("errors"):
            kv.append(("sdbuf_error", str(last["errors"][-1]).replace("\n", " ")[:200]))
        if last.get("full"):
            kv.append(("sdbuf_ssd_full", "yes"))
    return kv


def tick():
    """The watch's minute: open the buffer when it can, mend a pulled card, point the share,
    start Samba when the buffer serves the share without the SSD, and start a sync when the
    SSD is back and files wait. Returns a line for a person, or ""."""
    info = load(INFO)
    if not info.get("uuid"):
        return ""
    adopt_confs()
    if card_pulled():
        if mounted(MNT):
            log("the buffer's card stopped answering; closing what is left")
        fd = lock(POINT_LOCK, wait=60)
        try:
            if link_target() == BUF:
                set_link(SSD_SHARE if ssd_ready() else None)
                reconnect_clients()
        finally:
            if fd is not None:
                unlock(fd)
        if mounted(MNT):
            run(["/system/bin/umount", "-l", MNT], timeout=60)
        rc, out = cryptsetup(["close", MAPPER])
        if rc:
            log("cryptsetup close (card pulled): %s" % out[-200:])
    if not buffer_ready() and os.path.exists(KEY) and (os.path.exists(DM) or find_card(info["uuid"])):
        open_buffer()
    target = point()
    if target == "busy":
        return ""                                # another change of the share is under way
    if target == "buffer":
        rc, out = run(["sh", ROOT_SH, "smb-start"], timeout=120)     # drive-down.sh may have stopped smbd
        if out:
            log("smb-start: rc %d %s" % (rc, out.replace("\n", "; ")[:300]))
        return "The SSD is away: the SD buffer takes new files."
    if link_target() == SSD_SHARE and buffer_ready() and has_entries():
        if not syncing():
            start_sync()
            log("sync started")
        files, size = waiting()
        if files:
            return "Moving %d file%s (%s) from the SD buffer onto the SSD." % (files, "" if files == 1 else "s", size_text(size))
    return ""


def size_text(n):
    """bytes the way Finder counts them"""
    for unit, d in (("GB", 1e9), ("MB", 1e6)):
        if n >= d:
            return "%.1f %s" % (n / d, unit)
    return "%d KB" % max(1, round(n / 1e3))


# ---- format --------------------------------------------------------------------------
def zygote_env():
    """Android's own tools (sm) need zygote's environment, which su does not give us."""
    rc, out = run(["pidof", "zygote64"], timeout=10)
    try:
        with open(PROC + "/%s/environ" % out.split()[0], "rb") as f:
            raw = f.read().split(b"\0")
        return dict(x.decode(errors="replace").split("=", 1) for x in raw if b"=" in x)
    except (OSError, IndexError):
        return None


def mount_of(dev):
    """Where dev is mounted, by device number: vold mounts the card through a node of its
    own (/dev/block/vold/public:179,N), never by the kernel's name."""
    try:
        rdev = os.stat(dev).st_rdev
    except OSError:
        return None, None
    for m in mounts():
        if len(m) > 2 and m[0].startswith("/dev/"):
            try:
                if os.stat(m[0]).st_rdev == rdev:
                    return m[1], m[2]
            except OSError:
                continue
    return None, None


def release_from_android(dev):
    """Ask vold to unmount the card (its public volume), so nothing has it open."""
    env = zygote_env()
    minor = os.minor(os.stat(dev).st_rdev)
    rc, out = run(["/system/bin/sm", "list-volumes", "all"], timeout=30, env=env)
    for line in out.splitlines():
        f = line.split()
        if f and f[0].startswith("public:179,") and f[0].endswith(",%d" % minor) and "unmounted" not in f[1:2]:
            rc, out2 = run(["/system/bin/sm", "unmount", f[0]], timeout=60, env=env)
            print("Android let go of the card (%s): %s" % (f[0], "ok" if rc == 0 else out2[-200:]))
    for _ in range(15):
        if not mount_of(dev)[0]:
            return True
        time.sleep(1)
    return False


def format_card(again=False, check=False, confirm=None):
    """Erase the one microSD card and make it the buffer. In a terminal it asks for ERASE. Without
    one (a terminal over ssh can garble), --check says which card it would erase and prints its
    token, and --confirm TOKEN erases only that same card (path and size): phone-proxy.sh asks for
    ERASE on the Mac in between."""
    if not check and confirm is None and not sys.stdin.isatty():
        return fail("Run format in a terminal: it erases the card and asks first (or use --check, then --confirm).")
    cards = sd_cards()
    if len(cards) != 1:
        return fail("Expected one microSD card in the tablet, found %d." % len(cards))
    card = cards[0]
    if len(card["parts"]) > 1:
        return fail("The card has %d partitions; format expects one (or none)." % len(card["parts"]))
    dev = card["parts"][0] if card["parts"] else card["disk"]
    info = load(INFO)
    if info.get("uuid") and luks_uuid(dev) == info["uuid"] and not again:
        return fail("This card is the buffer already. To erase it and its waiting files: format --again")
    mp, fs = mount_of(dev)
    token = "%s:%d" % (dev, card["size"])
    if confirm is not None:
        if confirm != token:
            return fail("The card is not the one you confirmed (%s; now %s): nothing was erased." % (confirm, token))
        print("Erasing %s (%.1f GB) and making it the drive's buffer..." % (dev, card["size"] / 1e9))
    else:
        print("MicroSD card %s, %.1f GB." % (dev, card["size"] / 1e9))
        if mp:
            v = os.statvfs(mp)
            used = (v.f_blocks - v.f_bfree) * v.f_frsize
            print("Now %s, mounted at %s: %.1f GB used of %.1f GB." % (fs, mp, used / 1e9, v.f_blocks * v.f_frsize / 1e9))
        elif luks_uuid(dev):
            print("Now an encrypted volume%s." % (" (this buffer)" if luks_uuid(dev) == info.get("uuid") else ""))
        else:
            print("Now not mounted (unknown contents).")
        if check:
            print("confirm=" + token)
            return 0
        print("This ERASES everything on the card and makes it the drive's encrypted buffer.")
        sys.stdout.write("Type ERASE to go on: ")
        sys.stdout.flush()
        if sys.stdin.readline().strip() != "ERASE":
            print("Nothing changed.")
            return 1
    if os.path.exists(DM) or mounted(MNT):
        if close_buffer():
            return 1
    if mp and not release_from_android(dev):
        return fail("Android still has the card mounted at %s; nothing was erased." % mount_of(dev)[0])
    os.makedirs(os.path.dirname(KEY), exist_ok=True)
    tmp = KEY + ".new"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o400)
    try:
        os.write(fd, secrets.token_bytes(64))
    finally:
        os.close(fd)
    os.replace(tmp, KEY)
    ensure_dm_control()
    steps = [
        ["cryptsetup", "luksFormat", "--type", "luks2", "--batch-mode", "--pbkdf", "pbkdf2", "--iter-time", "200",
         "--label", "sdbuffer", "--key-file", KEY, dev],
        ["cryptsetup", "open", "--key-file", KEY, "--allow-discards", "--persistent", dev, MAPPER],
        [tool("mke2fs"), "-t", "ext4", "-F", "-L", "sdbuffer", "-m", "0", "-E", "stripe_width=1024", DM],
    ]
    for argv in steps:
        print("Running: " + " ".join(argv[:3]) + " ...")
        rc, out = run(argv, timeout=1800)
        if rc:
            return fail("%s failed: %s" % (argv[0], out[-400:]))
    uuid = luks_uuid(dev)
    save(INFO, {"uuid": uuid, "dev": dev, "formatted": int(time.time())})
    if open_buffer():
        return 1
    try:
        st = os.stat(SSD_SHARE) if ssd_ready() else None
        if st:
            os.chmod(BUF, stat.S_IMODE(st.st_mode))
            os.chown(BUF, st.st_uid, st.st_gid)
    except OSError:
        pass
    log("formatted %s as the buffer (LUKS %s)" % (dev, uuid))
    adopt_confs()
    print("Done: %s is the drive's buffer (%s). Android will call the card unsupported:"
          " never tap its Format, it would erase the buffer." % (dev, point()))
    return 0


def main(argv):
    os.environ["PATH"] = PATH
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "status":
        for k, v in status():
            print("%s=%s" % (k, v))
        return 0
    if os.geteuid() != 0:
        return fail("sdbuffer.py %s needs root: sudo python3 %s %s" % (cmd, argv[0], cmd))
    if cmd in ("away", "up", "tick", "close") and not load(INFO).get("uuid"):
        return 0                                  # no card is the buffer: root.sh's calls do nothing
    if cmd == "format":
        confirm = argv[argv.index("--confirm") + 1] if "--confirm" in argv[:-1] else None
        if "--confirm" in argv and confirm is None:
            return fail("format --confirm needs the token format --check printed.")
        return format_card(again="--again" in argv, check="--check" in argv, confirm=confirm)
    if cmd == "open":
        rc = open_buffer()
        print("buffer: %s, share points at %s" % ("open" if rc == 0 else "not open", point()))
        return rc
    if cmd == "close":
        return close_buffer()
    if cmd in ("point", "away"):
        if cmd == "point":
            clear_away()
        print("share points at %s" % point(away=cmd == "away"))
        return 0
    if cmd == "sync":
        return sync()
    if cmd in ("tick", "up"):
        if cmd == "up":
            clear_away()
        msg = tick()
        if msg:
            print(msg)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
