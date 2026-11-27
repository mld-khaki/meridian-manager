"""Buffer-copy safety against temporary directories only; never execute Android/root commands."""
import errno
import importlib.util
import os
import stat
import time
from pathlib import Path

import pytest


@pytest.fixture()
def buffer():
    path = Path(__file__).resolve().parent.parent / "tablet-bin/tabctl/sdbuffer.py"
    spec = importlib.util.spec_from_file_location("sdbuffer_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def folders(tmp_path):
    source, destination = tmp_path / "buffer", tmp_path / "ssd"
    source.mkdir()
    destination.mkdir()
    (source / "document").write_bytes(b"original data")
    return source, destination


def test_directory_flush_failure_keeps_source_data(buffer, tmp_path, monkeypatch):
    source, destination = folders(tmp_path)
    original = os.fsync

    def fail_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "destination directory flush failed")
        original(fd)

    monkeypatch.setattr(buffer.os, "fsync", fail_directory)
    result = buffer.move_tree(str(source), str(destination), settle=0, t_now=time.time() + 1)
    assert (source / "document").read_bytes() == b"original data"
    assert result["moved"] == 0 and result["errors"]


def test_source_modified_during_copy_is_not_deleted(buffer, tmp_path, monkeypatch):
    source, destination = folders(tmp_path)
    original = buffer.copy_meta

    def concurrent_write(st, src, dst):
        Path(src).write_bytes(b"new content written by a client")
        original(st, src, dst)

    monkeypatch.setattr(buffer, "copy_meta", concurrent_write)
    result = buffer.move_tree(str(source), str(destination), settle=0, t_now=time.time() + 1)
    assert (source / "document").read_bytes() == b"new content written by a client"
    assert not (destination / "document").exists()
    assert result["moved"] == 0 and result["errors"]


def test_verified_copy_still_moves_data_and_preserves_conflicting_destination(buffer, tmp_path):
    source, destination = folders(tmp_path)
    (destination / "document").write_bytes(b"different existing content")
    result = buffer.move_tree(str(source), str(destination), settle=0, t_now=time.time() + 1)
    assert result["moved"] == 1 and not result["errors"]
    assert not (source / "document").exists()
    assert (destination / "document").read_bytes() == b"different existing content"
    assert (destination / "document 2").read_bytes() == b"original data"


def test_retry_with_existing_copy_still_requires_directory_durability(buffer, tmp_path, monkeypatch):
    source, destination = folders(tmp_path)
    (destination / "document").write_bytes(b"original data")

    def failed_flush(path):
        raise OSError(errno.EIO, "directory flush failed")

    monkeypatch.setattr(buffer, "fsync_dir", failed_flush)
    result = buffer.move_tree(str(source), str(destination), settle=0, t_now=time.time() + 1)
    assert (source / "document").exists()
    assert result["same"] == 0 and result["errors"]
