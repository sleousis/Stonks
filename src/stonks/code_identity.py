"""What code is running: a hash of the package source (P46).

A lab manifest records the git commit when a checkout is there. A
deployed image has no ``.git``, so the manifest also records
:func:`source_hash`, a sha256 over every ``.py`` file of the installed
``stonks`` package. Two processes running the same code give the same
hash on any platform (line endings are normalised), and a one-line edit
changes it. The factor panel cache puts it in every slot, so a code change
never serves a panel the old code computed.
"""

from __future__ import annotations

import functools
import hashlib
from pathlib import Path

__all__ = ["package_dir", "source_hash"]


def package_dir() -> Path:
    """The folder of the installed ``stonks`` package (a checkout's
    ``src/stonks`` or a virtualenv's ``site-packages/stonks``)."""
    return Path(__file__).resolve().parent


def source_hash(root: Path | None = None) -> str:
    """sha256 over the sorted ``**/*.py`` files under ``root`` (the
    package by default): each file's relative path and its bytes with
    ``\\r\\n`` read as ``\\n``. Cached per process for the package."""
    if root is None:
        return _package_source_hash()
    return _hash_tree(Path(root))


@functools.cache
def _package_source_hash() -> str:
    return _hash_tree(package_dir())


def _hash_tree(root: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(
        (p for p in root.rglob("*.py") if "__pycache__" not in p.parts),
        key=lambda p: p.relative_to(root).as_posix(),
    )
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
        digest.update(b"\0")
    return digest.hexdigest()
