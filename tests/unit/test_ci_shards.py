"""The CI test shard split in ``tools/ci_shards.py``."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools import ci_shards

ROOT = Path(__file__).resolve().parents[2]


def _tree(tmp_path: Path, files: dict[str, int]) -> Path:
    for name, size in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x" * size)
    return tmp_path


def test_finds_test_modules_by_pytest_names_only(tmp_path):
    root = _tree(
        tmp_path,
        {
            "tests/unit/test_a.py": 1,
            "tests/unit/b_test.py": 1,
            "tests/unit/test_drift_test.py": 1,
            "tests/conftest.py": 1,
            "tests/helpers.py": 1,
            "tests/fixtures/data.json": 1,
        },
    )
    assert ci_shards.find_test_files(root / "tests", root) == [
        "tests/unit/b_test.py",
        "tests/unit/test_a.py",
        "tests/unit/test_drift_test.py",
    ]


def test_every_file_lands_in_exactly_one_shard(tmp_path):
    files = {f"tests/test_{i:02d}.py": (i * 37) % 101 + 1 for i in range(40)}
    root = _tree(tmp_path, files)
    shards = [ci_shards.shard(root / "tests", root, k, 4) for k in range(1, 5)]
    flat = [f for s in shards for f in s]
    assert sorted(flat) == sorted(files)
    assert len(flat) == len(set(flat))
    assert all(shards)


def test_split_is_deterministic_and_balanced_by_size(tmp_path):
    files = {"tests/test_big.py": 900, "tests/test_mid.py": 500, "tests/test_s1.py": 300}
    files |= {f"tests/test_t{i}.py": 100 for i in range(4)}
    root = _tree(tmp_path, files)
    first = [ci_shards.shard(root / "tests", root, k, 2) for k in (1, 2)]
    again = [ci_shards.shard(root / "tests", root, k, 2) for k in (1, 2)]
    assert first == again
    sizes = [sum(files[f] for f in s) for s in first]
    assert max(sizes) - min(sizes) <= 100


@pytest.mark.parametrize(("index", "total"), [(0, 4), (5, 4), (1, 0)])
def test_rejects_a_shard_outside_the_range(tmp_path, index, total):
    root = _tree(tmp_path, {"tests/test_a.py": 1})
    with pytest.raises(ValueError):
        ci_shards.shard(root / "tests", root, index, total)


def test_the_repo_suite_splits_into_the_ci_shards():
    shards = [ci_shards.shard(ROOT / "tests", ROOT, k, 4) for k in range(1, 5)]
    flat = [f for s in shards for f in s]
    assert sorted(flat) == ci_shards.find_test_files(ROOT / "tests", ROOT)
    assert "tests/unit/test_ci_shards.py" in flat
    assert all(shards)


def test_main_prints_one_path_per_line(tmp_path, capsys, monkeypatch):
    root = _tree(tmp_path, {"tests/test_a.py": 5, "tests/test_b.py": 3})
    monkeypatch.chdir(root)
    assert ci_shards.main(["1", "1"]) == 0
    assert capsys.readouterr().out.split() == ["tests/test_a.py", "tests/test_b.py"]


def test_main_rejects_bad_arguments(capsys):
    assert ci_shards.main(["x"]) == 2
    assert "usage" in capsys.readouterr().err
