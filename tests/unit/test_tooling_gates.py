"""The CI gates in ``tools/`` (TT-01, TT-02)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import coverage_gate, mutation, pyright_gate

ROOT = Path(__file__).resolve().parents[2]


# ---- pyright baseline ----------------------------------------------------------


def _diag(file: str, rule: str, line: int = 1, message: str = "bad") -> dict:
    return {
        "file": str(ROOT / file),
        "severity": "error",
        "message": message,
        "rule": rule,
        "range": {"start": {"line": line - 1, "character": 0}},
    }


def _report(*diags: dict) -> dict:
    return {"generalDiagnostics": list(diags), "summary": {"errorCount": len(diags)}}


def test_counts_group_errors_by_file_and_rule_with_repo_relative_paths():
    report = _report(
        _diag("src/stonks/a.py", "reportArgumentType", 3),
        _diag("src/stonks/a.py", "reportArgumentType", 9),
        _diag("src/stonks/b.py", "reportCallIssue"),
        {**_diag("src/stonks/b.py", "reportCallIssue"), "severity": "warning"},
    )
    assert pyright_gate.error_counts(report, ROOT) == {
        "src/stonks/a.py::reportArgumentType": 2,
        "src/stonks/b.py::reportCallIssue": 1,
    }


def test_errors_in_the_baseline_pass_and_line_moves_do_not_matter():
    baseline = {"src/stonks/a.py::reportArgumentType": 2}
    report = _report(
        _diag("src/stonks/a.py", "reportArgumentType", 30),
        _diag("src/stonks/a.py", "reportArgumentType", 40),
    )
    new, fixed = pyright_gate.compare(pyright_gate.error_counts(report, ROOT), baseline)
    assert new == {}
    assert fixed == {}


def test_a_new_error_fails_and_a_fixed_one_is_reported():
    baseline = {"src/stonks/a.py::reportArgumentType": 2, "src/stonks/c.py::reportCallIssue": 1}
    report = _report(
        _diag("src/stonks/a.py", "reportArgumentType"),
        _diag("src/stonks/a.py", "reportArgumentType"),
        _diag("src/stonks/a.py", "reportArgumentType"),
    )
    new, fixed = pyright_gate.compare(pyright_gate.error_counts(report, ROOT), baseline)
    assert new == {"src/stonks/a.py::reportArgumentType": 1}
    assert fixed == {"src/stonks/c.py::reportCallIssue": 1}


def test_gate_main_exits_1_on_new_errors_and_0_on_update(tmp_path, capsys):
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(_report(_diag("src/stonks/a.py", "reportCallIssue"))))
    baseline = tmp_path / "baseline.json"
    baseline.write_text("{}")

    assert pyright_gate.main(["--report", str(report_path), "--baseline", str(baseline)]) == 1
    assert "src/stonks/a.py" in capsys.readouterr().out

    assert (
        pyright_gate.main(["--report", str(report_path), "--baseline", str(baseline), "--update"])
        == 0
    )
    assert json.loads(baseline.read_text()) == {"src/stonks/a.py::reportCallIssue": 1}
    assert pyright_gate.main(["--report", str(report_path), "--baseline", str(baseline)]) == 0


def test_strict_paths_come_from_the_pyright_config():
    strict = pyright_gate.strict_paths()
    assert "src/stonks/core" in strict


def test_an_error_under_a_strict_path_fails_even_when_baselined(tmp_path, capsys):
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(_report(_diag("src/stonks/core/x.py", "reportCallIssue"))))
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"src/stonks/core/x.py::reportCallIssue": 1}))
    args = ["--report", str(report_path), "--baseline", str(baseline)]
    assert pyright_gate.main([*args, "--strict", "src/stonks/core"]) == 1
    assert "strict" in capsys.readouterr().out
    # --update never writes a strict path into the baseline
    assert pyright_gate.main([*args, "--strict", "src/stonks/core", "--update"]) == 1
    assert json.loads(baseline.read_text()) == {"src/stonks/core/x.py::reportCallIssue": 1}


def test_the_checked_in_baseline_has_no_strict_path():
    data = json.loads((ROOT / "tools" / "pyright-baseline.json").read_text())
    strict = pyright_gate.strict_paths()
    assert not [k for k in data if any(k.startswith(p + "/") for p in strict)]


def test_the_checked_in_baseline_is_sorted_json():
    path = ROOT / "tools" / "pyright-baseline.json"
    data = json.loads(path.read_text())
    assert list(data) == sorted(data)
    assert all(isinstance(n, int) and n > 0 for n in data.values())


# ---- coverage per package ----------------------------------------------------------


def _file(lines: int, missed: int, branches: int, missed_branches: int) -> dict:
    return {
        "summary": {
            "num_statements": lines,
            "missing_lines": missed,
            "num_branches": branches,
            "missing_branches": missed_branches,
        }
    }


def _cov(files: dict) -> dict:
    return {"files": files}


def test_package_coverage_is_the_combined_line_and_branch_number():
    data = _cov(
        {
            "src/stonks/core/a.py": _file(90, 0, 10, 10),  # 90 of 100
            "src/stonks/core/b.py": _file(10, 0, 0, 0),  # 10 of 10
            "src/stonks/other/c.py": _file(10, 10, 0, 0),
        }
    )
    got = coverage_gate.package_percent(data, "core")
    assert got == pytest.approx(100 * 100 / 110)


def test_windows_paths_are_matched_too():
    data = _cov({"src\\stonks\\auth\\a.py": _file(10, 1, 0, 0)})
    assert coverage_gate.package_percent(data, "auth") == pytest.approx(90.0)


def test_gate_fails_only_the_packages_below_their_floor():
    data = _cov(
        {
            "src/stonks/core/a.py": _file(100, 10, 0, 0),  # 90
            "src/stonks/auth/a.py": _file(100, 1, 0, 0),  # 99
        }
    )
    failures = coverage_gate.check(data, {"core": 92.0, "auth": 95.0})
    assert [f[0] for f in failures] == ["core"]


def test_a_gated_package_with_no_files_fails():
    failures = coverage_gate.check(_cov({}), {"portfolio": 95.0})
    assert failures and failures[0][0] == "portfolio"


def test_the_configured_floors_cover_the_five_strict_packages():
    assert set(coverage_gate.PACKAGE_FLOORS) == {
        "core",
        "production",
        "execution",
        "auth",
        "portfolio",
    }
    assert all(floor <= coverage_gate.TARGET for floor in coverage_gate.PACKAGE_FLOORS.values())


# ---- mutation testing (TT-04) --------------------------------------------------------


def test_surviving_rate_ignores_incompetent_mutants():
    assert mutation.surviving_rate(["killed"] * 9 + ["survived", "incompetent"]) == 10.0
    assert mutation.surviving_rate(["incompetent"]) is None


def test_outcome_names_read_the_enum_value():
    import enum

    class Outcome(str, enum.Enum):  # noqa: UP042 - mirrors cosmic-ray, prints as Outcome.KILLED
        KILLED = "killed"

    assert str(Outcome.KILLED) != "killed"
    assert mutation.outcome_name(Outcome.KILLED) == "killed"
    assert mutation.outcome_name("SURVIVED") == "survived"


def test_annotation_spans_cover_argument_return_and_variable_annotations():
    source = "def f(a: str | None, b=1) -> int | None:\n    x: int | str = 1\n    return a | b\n"
    spans = mutation.annotation_spans(source)
    assert mutation.in_spans((1, 13), spans)  # the | in str | None
    assert mutation.in_spans((1, 34), spans)  # the | in the return type
    assert mutation.in_spans((2, 11), spans)  # the | in x's annotation
    assert not mutation.in_spans((3, 13), spans)  # a | b is real code


def test_every_target_names_real_modules_and_tests():
    for target in mutation.TARGETS:
        assert (ROOT / target.module).exists(), target.module
        for test in target.tests:
            assert (ROOT / test).is_file(), test


def test_the_targets_cover_the_money_paths():
    modules = {t.module for t in mutation.TARGETS}
    for money_path in (
        "src/stonks/portfolio/orders.py",  # orders
        "src/stonks/backtest/fills.py",  # fills
        "src/stonks/production/risk.py",  # risk
        "src/stonks/production/rules",  # risk rules
        "src/stonks/execution/reconcile.py",  # ledger
        "src/stonks/production/pnl.py",  # P&L
        "src/stonks/execution/drift.py",  # broker versus ledger drift (19.15)
    ):
        assert money_path in modules


def test_config_runs_only_the_target_tests_without_xdist():
    target = mutation.TARGETS[0]
    text = mutation.config_text(target, python="python")
    assert f'module-path = "{target.module}"' in text
    assert "-p no:xdist" in text
    assert all(test in text for test in target.tests)
    assert 'name = "local"' in text
