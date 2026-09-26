"""Source files are clean UTF-8: no mojibake from a cp1252 round trip."""

from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
# UTF-8 bytes of an em dash / ellipsis / quotes decoded as cp1252 start "â€";
# a stray "Â" before a space or symbol is the same accident.
MOJIBAKE = ("â€", "Â ", "Ã©", "�")


def test_no_mojibake_in_src():
    offenders = [
        f"{path.relative_to(SRC)}:{n}"
        for path in SRC.rglob("*")
        if path.suffix in {".py", ".sql", ".toml", ".md"}
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if any(m in line for m in MOJIBAKE)
    ]
    assert offenders == []
