"""Streaming a lake snapshot to a remote lab worker (roadmap 14.9)."""

from __future__ import annotations

import io
import json
import tarfile
from datetime import UTC, datetime

import pytest

from stonks.lab.offload.snapshot import CURRENT_FILE, LAKE_FILE, LakeSnapshots
from stonks.lab.offload.transfer import install_snapshot, iter_snapshot_tar

NAME = "20260928T010203000000Z"


@pytest.fixture
def server(tmp_path):
    root = tmp_path / "server"
    folder = root / NAME
    (folder / "bars" / "interval=1d").mkdir(parents=True)
    (folder / LAKE_FILE).write_bytes(b"duck" * 1000 + b"!")  # not a block multiple
    (folder / "bars" / "interval=1d" / "part-0.parquet").write_bytes(bytes(range(256)) * 9)
    (folder / "holds").mkdir()
    (folder / "holds" / "w1").touch()
    meta = {"dir": NAME, "created_at": "2026-09-28T01:02:03+00:00", "fingerprint": "fp"}
    (root / CURRENT_FILE).write_text(json.dumps(meta), encoding="utf-8")
    return LakeSnapshots(root)


def test_the_tar_stream_holds_every_file_but_the_hold_marks(server):
    info = server.current()
    data = b"".join(iter_snapshot_tar(server, info))
    assert len(data) % tarfile.BLOCKSIZE == 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
        names = sorted(tar.getnames())
    assert names == ["bars/interval=1d/part-0.parquet", LAKE_FILE]


def test_a_worker_installs_the_stream_and_makes_it_current(server, tmp_path):
    info = server.current()
    local = LakeSnapshots(tmp_path / "worker")
    created = datetime(2026, 9, 28, 1, 2, 3, tzinfo=UTC)
    got = install_snapshot(
        local, NAME, iter_snapshot_tar(server, info), created_at=created, fingerprint="fp"
    )
    assert got.directory.name == NAME
    assert got.path.read_bytes() == info.path.read_bytes()
    part = got.directory / "bars" / "interval=1d" / "part-0.parquet"
    assert part.read_bytes() == bytes(range(256)) * 9
    assert local.current() == got
    assert local.get(NAME) is not None
    assert not any(p.name.startswith(".") for p in local.root.iterdir() if p.is_dir())


def test_a_broken_download_never_becomes_current(server, tmp_path):
    info = server.current()
    local = LakeSnapshots(tmp_path / "worker")
    data = b"".join(iter_snapshot_tar(server, info))
    with pytest.raises(tarfile.TarError):
        install_snapshot(local, NAME, [data[:700]], created_at=datetime.now(UTC))
    assert local.current() is None
    assert local.get(NAME) is None


@pytest.mark.parametrize("bad", ["../x", "", ".hidden", "a/b"])
def test_snapshot_names_are_plain_folder_names(server, tmp_path, bad):
    assert server.get(bad) is None
    with pytest.raises(ValueError):
        install_snapshot(LakeSnapshots(tmp_path / "w"), bad, [], created_at=datetime.now(UTC))


def test_an_escaping_member_is_refused(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        member = tarfile.TarInfo("../evil.txt")
        member.size = 1
        tar.addfile(member, io.BytesIO(b"x"))
    with pytest.raises(tarfile.TarError):
        install_snapshot(
            LakeSnapshots(tmp_path / "w"), NAME, [buf.getvalue()], created_at=datetime.now(UTC)
        )
    assert not (tmp_path / "evil.txt").exists()
