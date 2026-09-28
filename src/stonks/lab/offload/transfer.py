"""Moving a lake snapshot to a remote lab worker (roadmap 14.9).

The server streams a snapshot folder as an uncompressed tar, file by file,
so a large Parquet lake never sits in memory or in a temporary archive.
The worker saves the stream, extracts it with the ``data`` filter (no
absolute paths, links out of the folder or special files) and adopts the
folder as its current snapshot.
"""

from __future__ import annotations

import shutil
import tarfile
import time
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path

from stonks.lab.offload.snapshot import SNAPSHOT_NAME, LakeSnapshots, SnapshotInfo

_BLOCK = tarfile.BLOCKSIZE
_CHUNK = 1 << 20

#: Job kinds a remote worker takes: they read only the lake snapshot and the
#: research rows it is sent. Studio lab runs read drafts and user code from
#: the server, so they stay with a worker that shares its data folder.
REMOTE_KINDS: frozenset[str] = frozenset({"lab_run", "lab_sweep"})


def iter_snapshot_tar(snapshots: LakeSnapshots, info: SnapshotInfo) -> Iterator[bytes]:
    """The snapshot folder as tar bytes, one chunk at a time."""
    root = info.directory
    for path in snapshots.files(info):
        member = tarfile.TarInfo(path.relative_to(root).as_posix())
        stat = path.stat()
        member.size = stat.st_size
        member.mtime = int(stat.st_mtime)
        member.mode = 0o644
        yield member.tobuf(format=tarfile.PAX_FORMAT)
        sent = 0
        with path.open("rb") as fh:
            while chunk := fh.read(_CHUNK):
                sent += len(chunk)
                yield chunk
        if sent != member.size:  # the file changed under us: never send a torn archive
            raise OSError(f"snapshot file {path} changed while it was sent")
        if member.size % _BLOCK:
            yield b"\0" * (_BLOCK - member.size % _BLOCK)
    yield b"\0" * (2 * _BLOCK)


def install_snapshot(
    snapshots: LakeSnapshots,
    name: str,
    chunks: Iterable[bytes],
    *,
    created_at: datetime,
    fingerprint: str = "",
) -> SnapshotInfo:
    """Write the tar ``chunks`` of snapshot ``name`` under ``snapshots.root``
    and make it current. A half-written download never becomes current."""
    if not SNAPSHOT_NAME.fullmatch(name):
        raise ValueError(f"not a snapshot name: {name!r}")
    root = snapshots.root
    root.mkdir(parents=True, exist_ok=True)
    stamp = f"{int(time.time() * 1000)}"
    archive = root / f".download-{name}-{stamp}.tar"
    staging = root / f".extract-{name}-{stamp}"
    try:
        with archive.open("wb") as fh:
            for chunk in chunks:
                fh.write(chunk)
        with tarfile.open(archive, mode="r:") as tar:
            tar.extractall(staging, filter="data")
        target = root / name
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        staging.rename(target)
    finally:
        archive.unlink(missing_ok=True)
        shutil.rmtree(staging, ignore_errors=True)
    return snapshots.adopt(name, created_at, fingerprint)


def has_snapshot(snapshots: LakeSnapshots, name: str) -> bool:
    info = snapshots.get(name)
    return info is not None and Path(info.path).is_file()
