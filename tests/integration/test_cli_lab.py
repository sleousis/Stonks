"""CLI tests for `stonks lab run` on the trending fixture lake."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from stonks.cli import app
from stonks.registry.store import StrategyRegistry
from stonks.store.state import SqliteState

WINDOW = ["--start", "2025-10-01", "--end", "2026-04-01"]
UNIVERSE = ["--tickers", "UP.US,DOWN.US,FLAT.US"]
FAST = ["--tuner", "grid", "--grid-size", "2", "--budget", "4"]


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def lab_env(tmp_path, monkeypatch, lake_trending):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    lake_path = (tmp_path / "lake.duckdb").as_posix()  # the lake_trending file
    (tmp_path / "config" / "default.toml").write_text(
        f"""
[lake]
path = "{lake_path}"

[state]
path = "data/state.sqlite"

[registry]
artifacts_dir = "data/artifacts"

[lab.walk_forward]
n_splits = 3
test_days = 20
min_positive_share = 0.0
min_mean_score = -1000000000.0
""".strip()
    )
    (tmp_path / "data").mkdir()
    return tmp_path


def _run(runner, *args):
    return runner.invoke(app, ["lab", "run", *args], catch_exceptions=False)


def _result(path):
    return json.loads(path.read_text())


def _registered(env):
    state = SqliteState(env / "data" / "state.sqlite")
    state.migrate()
    try:
        registry = StrategyRegistry(state=state, artifacts_dir=env / "data" / "artifacts")
        return [(h, registry.get_reports(h.id)) for h in registry.list_all()]
    finally:
        state.close()


def test_lab_run_tunes_and_reports_a_verdict(runner, lab_env):
    out = lab_env / "result.json"
    r = _run(runner, "momentum", *UNIVERSE, *WINDOW, *FAST, "--json-out", str(out))
    assert r.exit_code == 0, r.output
    assert "verdict" in r.output
    doc = _result(out)
    assert doc["strategy"] == "stonks.strategies.examples.momentum:Momentum"
    assert set(doc["best_params"]) >= {"lookback_days", "threshold", "allocation"}
    # no --tests / --preset: the registry's "quick" preset
    assert [rep["test_id"] for rep in doc["survival_reports"]] == ["oos", "period_stability"]
    assert doc["verdict"] in ("pass", "fail")
    assert doc["registered_id"] is None
    assert doc["run_id"] and doc["n_trials_run"] == 4


def test_params_pin_values_and_walk_forward_defaults_come_from_config(runner, lab_env):
    out = lab_env / "result.json"
    r = _run(
        runner,
        "momentum",
        "--params",
        '{"lookback_days": 7}',
        *UNIVERSE,
        *WINDOW,
        *FAST,
        "--walk-forward",
        "--json-out",
        str(out),
    )
    assert r.exit_code == 0, r.output
    doc = _result(out)
    assert doc["best_params"]["lookback_days"] == 7
    wf = next(rep for rep in doc["survival_reports"] if rep["test_id"] == "walk_forward")
    assert wf["metrics"]["n_folds"] == 3.0  # [lab.walk_forward] n_splits
    assert "rolling" in wf["notes"]


def test_walk_forward_flags_override_config(runner, lab_env):
    out = lab_env / "result.json"
    r = _run(
        runner,
        "momentum",
        *UNIVERSE,
        *WINDOW,
        *FAST,
        "--walk-forward",
        "--wf-splits",
        "2",
        "--wf-test-days",
        "30",
        "--wf-anchored",
        "--json-out",
        str(out),
    )
    assert r.exit_code == 0, r.output
    wf = next(rep for rep in _result(out)["survival_reports"] if rep["test_id"] == "walk_forward")
    assert wf["metrics"]["n_folds"] == 2.0
    assert "anchored" in wf["notes"]


@pytest.mark.parametrize("flag,mode", [("--mcpt", "oos"), ("--mcpt-retune", "retune")])
def test_mcpt_options(runner, lab_env, flag, mode):
    out = lab_env / "result.json"
    r = _run(
        runner,
        "momentum",
        *UNIVERSE,
        *WINDOW,
        *FAST,
        flag,
        "--mcpt-permutations",
        "3",
        "--json-out",
        str(out),
    )
    assert r.exit_code == 0, r.output
    mcpt = next(rep for rep in _result(out)["survival_reports"] if rep["test_id"] == "mcpt")
    assert mcpt["metrics"]["n_permutations"] == 3.0
    assert f"mode={mode}" in mcpt["notes"]


#: Buy-and-hold never closes a trade, so the default PSR gate (BL-16: >= 20
#: closed trades) fails it. Registration tests run ``oos`` with the legacy
#: flat-Sharpe rule instead, through the survival-test options.
LEGACY_OOS = ["--test-option", "oos.mode=sharpe", "--test-option", "oos.min_trades=0"]


def test_register_puts_a_passing_wrapped_strategy_in_shadow(runner, lab_env):
    params = {
        "inner_class_path": "stonks.strategies.examples.buy_and_hold:BuyAndHold",
        "inner_params": {"ticker": "UP.US"},
    }
    r = _run(
        runner,
        "macro_regime_filter",
        "--params",
        json.dumps(params),
        *UNIVERSE,
        *WINDOW,
        *FAST,
        "--tests",
        "oos",
        *LEGACY_OOS,
        "--register",
    )
    assert r.exit_code == 0, r.output
    ((handle, reports),) = _registered(lab_env)
    assert handle.status == "shadow"
    assert handle.class_path == "stonks.strategies.macro_regime:MacroRegimeFilter"
    assert handle.params["inner_class_path"] == params["inner_class_path"]
    assert handle.params["inner_params"]["ticker"] == "UP.US"
    assert [rep.test_id for rep in reports] == ["oos"]
    assert handle.id in r.output


def test_register_skips_a_failing_strategy(runner, lab_env):
    r = _run(
        runner,
        "buy_and_hold",
        "--params",
        '{"ticker": "DOWN.US"}',
        *UNIVERSE,
        *WINDOW,
        *FAST,
        "--tests",
        "oos",
        "--register-if-passes",
    )
    assert r.exit_code == 0, r.output
    assert "fail" in r.output
    assert _registered(lab_env) == []


@pytest.mark.parametrize(
    "args,needle",
    [
        (["nope", *UNIVERSE, *WINDOW], "unknown strategy"),
        (["momentum", "--params", "{bad", *UNIVERSE, *WINDOW], "--params"),
        (["momentum", "--params", '{"nope": 1}', *UNIVERSE, *WINDOW], "nope"),
        (["momentum", *UNIVERSE, "--start", "2026-04-01", "--end", "2025-10-01"], "before"),
        (["momentum", *WINDOW], "--tickers"),
        (["momentum", *UNIVERSE, *WINDOW, "--tuner", "bayes"], "--tuner"),
        (["momentum", *UNIVERSE, *WINDOW, "--mcpt", "--mcpt-retune"], "--mcpt"),
        (["momentum", *UNIVERSE, *WINDOW, "--tests", "bogus"], "unknown tests"),
        (["momentum", *UNIVERSE, *WINDOW, "--preset", "huge"], "--preset"),
        (["momentum", *UNIVERSE, *WINDOW, "--cost-model", "free"], "--cost-model"),
        (["momentum", *UNIVERSE, *WINDOW, "--test-option", "oos"], "TEST.OPTION=VALUE"),
        (["momentum", *UNIVERSE, *WINDOW, "--test-option", "bogus.x=1"], "bogus"),
        (["momentum", *UNIVERSE, *WINDOW, "--test-option", "oos.nope=1"], "nope"),
        (["momentum", *UNIVERSE, *WINDOW, "--test-option", "oos.min_trades=-1"], "min_trades"),
        (["momentum", *UNIVERSE, *WINDOW, "--test-option", "pbo.max_pbo=0.3"], "pbo"),
    ],
)
def test_bad_input_is_a_usage_error(runner, lab_env, args, needle):
    r = runner.invoke(app, ["lab", "run", *args])
    assert r.exit_code == 2, r.output
    assert needle in r.output


def test_test_options_reach_the_survival_test(runner, lab_env):
    out = lab_env / "result.json"
    args = ["buy_and_hold", "--params", '{"ticker": "UP.US"}', *UNIVERSE, *WINDOW, *FAST]
    r = _run(runner, *args, "--tests", "oos", *LEGACY_OOS, "--json-out", str(out))
    assert r.exit_code == 0, r.output
    (rep,) = _result(out)["survival_reports"]
    assert rep["passed"], rep["notes"]  # 0 trades pass once min_trades=0


@pytest.mark.parametrize("flag,name", [([], "EW"), (["--benchmark", "UP.US"], "UP.US")])
def test_benchmark_reaches_the_result(runner, lab_env, flag, name):
    out = lab_env / "result.json"
    r = _run(runner, "momentum", *UNIVERSE, *WINDOW, *FAST, "--tests", "oos", *flag,
             "--json-out", str(out))  # fmt: skip
    assert r.exit_code == 0, r.output
    bench = _result(out)["benchmark"]
    assert bench["name"] == name  # "auto" falls back to EW: the lake has no SPY.US
    assert "excess_cagr" in bench


def test_benchmark_none_turns_it_off(runner, lab_env):
    out = lab_env / "result.json"
    r = _run(runner, "momentum", *UNIVERSE, *WINDOW, *FAST, "--tests", "oos",
             "--benchmark", "none", "--json-out", str(out))  # fmt: skip
    assert r.exit_code == 0, r.output
    assert _result(out)["benchmark"] is None


def test_embargo_bars_move_the_validation_window(runner, lab_env):
    starts = []
    for bars in ("0", "5"):
        out = lab_env / f"embargo_{bars}.json"
        r = _run(runner, "momentum", *UNIVERSE, *WINDOW, *FAST, "--tests", "oos",
                 "--embargo-bars", bars, "--json-out", str(out))  # fmt: skip
        assert r.exit_code == 0, r.output
    for run in _lab_runs(lab_env):
        starts.append(json.loads(run["manifest_json"])["dataset"]["val_window"][0])
    assert starts[0] < starts[1]


def test_embargo_that_leaves_no_validation_window_is_a_usage_error(runner, lab_env):
    r = runner.invoke(app, ["lab", "run", "momentum", *UNIVERSE, *WINDOW, "--embargo-bars", "500"])
    assert r.exit_code == 2, r.output
    assert "embargo" in r.output


def test_configured_embargo_that_leaves_no_validation_window_is_a_usage_error(runner, lab_env):
    cfg = lab_env / "config" / "default.toml"
    cfg.write_text(cfg.read_text() + "\n\n[lab]\nembargo_bars = 500\n")
    r = runner.invoke(app, ["lab", "run", "momentum", *UNIVERSE, *WINDOW, *FAST, "--tests", "oos"])
    assert r.exit_code == 2, r.output
    assert "embargo" in r.output


def test_walk_forward_wfe_and_matrix_flags(runner, lab_env):
    out = lab_env / "result.json"
    r = _run(runner, "momentum", *UNIVERSE, *WINDOW, *FAST, "--tests", "walk_forward",
             "--wf-min-wfe", "1.0", "--wf-matrix", "--json-out", str(out))  # fmt: skip
    assert r.exit_code == 0, r.output
    (wf,) = _result(out)["survival_reports"]
    assert "< 1.0" in wf["notes"]  # the WFE gate at the flag's value
    assert "matrix" in wf["notes"] or "matrix_cells" in wf["metrics"]


def test_register_defaults_to_the_promotion_preset():
    from stonks.cli import _lab_suite
    from stonks.lab.survival.registry import resolve_preset

    plain = _lab_suite(None, None, mcpt=False, walk_forward=False)
    registering = _lab_suite(None, None, mcpt=False, walk_forward=False, registers=True)
    assert plain == resolve_preset("quick")
    assert registering == resolve_preset("promotion")
    assert _lab_suite("oos", None, mcpt=False, walk_forward=False, registers=True) == ["oos"]


# ---- Integration 1: ledger, presets, workers, costs --------------------------


def _lab_runs(env):
    with SqliteState(env / "data" / "state.sqlite") as state:
        return state.sql("SELECT * FROM lab_runs ORDER BY started_at")


def test_hypothesis_and_premortem_are_pre_registered(runner, lab_env):
    out = lab_env / "result.json"
    r = _run(
        runner,
        "momentum",
        *UNIVERSE,
        *WINDOW,
        *FAST,
        "--tests",
        "oos",
        "--hypothesis",
        "trend persists",
        "--premortem",
        "chop",
        "--json-out",
        str(out),
    )
    assert r.exit_code == 0, r.output
    (run,) = _lab_runs(lab_env)
    assert run["id"] == _result(out)["run_id"]
    assert (run["hypothesis"], run["premortem"]) == ("trend persists", "chop")
    assert run["verdict"] in ("pass", "fail")


def test_preset_selects_the_registry_suite(runner, lab_env):
    from stonks.lab.survival.registry import resolve_preset

    out = lab_env / "result.json"
    r = _run(
        runner,
        "momentum",
        *UNIVERSE,
        *WINDOW,
        *FAST,
        "--preset",
        "standard",
        "--json-out",
        str(out),
    )
    assert r.exit_code == 0, r.output
    ids = [rep["test_id"] for rep in _result(out)["survival_reports"]]
    assert ids == resolve_preset("standard")


def test_workers_do_not_change_the_result(runner, lab_env):
    docs = []
    for workers in ("1", "2"):
        out = lab_env / f"result_{workers}.json"
        r = _run(
            runner,
            "momentum",
            *UNIVERSE,
            *WINDOW,
            *FAST,
            "--tests",
            "oos",
            "--workers",
            workers,
            "--json-out",
            str(out),
        )
        assert r.exit_code == 0, r.output
        docs.append(_result(out))
    one, two = docs
    assert one["best_params"] == two["best_params"]
    assert one["best_score"] == two["best_score"]
    assert one["survival_reports"] == two["survival_reports"]


def test_registered_strategy_meta_has_lab_provenance(runner, lab_env):
    params = {"ticker": "UP.US"}
    r = _run(
        runner,
        "buy_and_hold",
        "--params",
        json.dumps(params),
        *UNIVERSE,
        *WINDOW,
        *FAST,
        "--tests",
        "oos",
        *LEGACY_OOS,
        "--register-if-passes",
        "--hypothesis",
        "drift up",
    )
    assert r.exit_code == 0, r.output
    ((handle, _),) = _registered(lab_env)
    meta = json.loads((handle.artifact_path / "meta.json").read_text())
    (run,) = _lab_runs(lab_env)
    assert meta["lab_run_id"] == run["id"]
    assert meta["hypothesis"] == "drift up"


def test_cost_model_zero_differs_from_the_realistic_default(runner, lab_env):
    scores = {}
    for model in ("zero", "config"):
        out = lab_env / f"{model}.json"
        r = _run(
            runner,
            "momentum",
            *UNIVERSE,
            *WINDOW,
            *FAST,
            "--tests",
            "oos",
            "--cost-model",
            model,
            "--json-out",
            str(out),
        )
        assert r.exit_code == 0, r.output
        scores[model] = _result(out)["best_score"]
    assert scores["zero"] != scores["config"]
    runs = _lab_runs(lab_env)
    costs = [json.loads(r["manifest_json"])["costs"] for r in runs]
    assert costs[0] != costs[1]


def test_lab_ic_wraps_the_signal_eval_command(monkeypatch):
    import stonks.lab.signal_eval as signal_eval

    seen = {}

    def fake_main(argv=None, *, prog=None):
        seen["argv"], seen["prog"] = list(argv), prog
        return 0

    monkeypatch.setattr(signal_eval, "main", fake_main)
    result = CliRunner().invoke(
        app, ["lab", "ic", "--strategy", "momentum", "--tickers", "A.US,B.US", "--events"]
    )
    assert result.exit_code == 0, result.output
    assert seen == {
        "argv": ["--strategy", "momentum", "--tickers", "A.US,B.US", "--events"],
        "prog": "stonks lab ic",
    }


def test_preflight_warnings_are_printed_and_saved(runner, lab_env):
    out = lab_env / "result.json"
    r = _run(
        runner,
        "buy_and_hold",
        "--tickers", "UP.US,NOPE.US",
        *WINDOW, *FAST, "--tests", "oos",
        "--json-out", str(out),
    )  # fmt: skip
    assert r.exit_code == 0, r.output
    assert "missing_data" in r.output
    codes = [i["code"] for i in _result(out)["preflight"]["issues"]]
    assert "missing_data" in codes


def test_strict_turns_preflight_warnings_into_a_usage_error(runner, lab_env):
    r = runner.invoke(
        app,
        ["lab", "run", "buy_and_hold", "--tickers", "UP.US,NOPE.US", *WINDOW, *FAST,
         "--tests", "oos", "--strict"],
    )  # fmt: skip
    assert r.exit_code != 0
    assert "missing_data" in r.output


def test_no_preflight_skips_it(runner, lab_env):
    out = lab_env / "result.json"
    r = _run(
        runner,
        "buy_and_hold",
        "--tickers", "UP.US,NOPE.US",
        *WINDOW, *FAST, "--tests", "oos", "--no-preflight",
        "--json-out", str(out),
    )  # fmt: skip
    assert r.exit_code == 0, r.output
    assert _result(out)["preflight"] is None
