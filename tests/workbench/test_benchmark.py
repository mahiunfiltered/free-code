"""fcc-bench: full fake-engine suite, the oracle, and the false-completion hard failure."""

import asyncio
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from free_claude_code.workbench.benchmark import cli
from free_claude_code.workbench.benchmark.harness import (
    Engine,
    ScenarioResult,
    run_oracle,
    run_suite,
    seed_repo,
    usage_totals,
)
from free_claude_code.workbench.benchmark.report import (
    exit_code,
    percentile,
    render_markdown,
    summarize,
    write_report,
)
from free_claude_code.workbench.benchmark.scenarios import (
    BUG_FIX,
    CANARY,
    PRESERVE,
    SCENARIOS,
    SECRET,
)

FAKE = Engine("fake")


@pytest.fixture(scope="module")
def suite(tmp_path_factory: pytest.TempPathFactory) -> dict[str, ScenarioResult]:
    results = asyncio.run(
        run_suite(
            list(SCENARIOS.values()),
            FAKE,
            tmp_path_factory.mktemp("bench"),
            timeout_s=120,
        )
    )
    return {r.scenario: r for r in results}


def test_fake_suite_has_no_false_completions(suite: dict[str, ScenarioResult]):
    assert set(suite) == set(SCENARIOS)
    for scenario in SCENARIOS.values():
        result = suite[scenario.id]
        assert not result.false_completion, result
        assert result.error == "", result
        assert result.disposition == scenario.expected_for("fake"), result
        # Platform and oracle agree on every scenario of the deterministic suite.
        assert result.oracle.passed == (result.disposition == "VERIFIED"), result
    report = summarize(list(suite.values()), {"engine": "fake"})
    assert exit_code(report) == 0
    aggregate = report["aggregate"]
    assert isinstance(aggregate, dict)
    assert aggregate["false_completions"] == []
    assert aggregate["verified"] == 3
    assert isinstance(aggregate["median_time_to_verified_s"], float)


def test_fake_suite_metrics_per_scenario(suite: dict[str, ScenarioResult]):
    bug = suite["bug_fix"]
    assert (bug.attempts, bug.recoveries, bug.turns) == (2, 1, 2)
    assert bug.output_tokens == 40 and bug.time_to_verified_s is not None
    unfixable = suite["unfixable"]
    assert unfixable.disposition == "RECOVERY_REQUIRED"
    assert not unfixable.oracle.tests_passed and unfixable.time_to_verified_s is None
    secret = suite["secret_leak_canary"]
    assert any("secret_scan" in r for r in secret.blocking_reasons)
    assert secret.oracle.constraints["no_secret"] is False
    assert suite["scope_violation_trap"].oracle.constraints == {
        "tests_untouched": True,
        "scope": False,
    }
    assert any("diff_scope" in r for r in suite["preserve_constraint"].blocking_reasons)
    parallel = suite["parallel_disjoint"]
    assert parallel.mode == "parallel" and parallel.turns == 2
    assert parallel.oracle.changed_files == ["geometry.py", "units.py"]


def test_forged_false_completion_fails_the_run(tmp_path: Path):
    # Integer division passes the visible tests but not the oracle's hidden test.
    forged = replace(
        BUG_FIX,
        id="forged",
        fake_attempts=(
            {
                "calc.py": "def average(values):\n    return sum(values) // len(values)\n"
            },
        ),
    )
    [result] = asyncio.run(run_suite([forged], FAKE, tmp_path, timeout_s=120))

    assert result.disposition == "VERIFIED"
    assert result.oracle.tests_passed is False and result.false_completion
    report = summarize([result], {"engine": "real"})
    assert exit_code(report) == 1
    assert "FALSE COMPLETIONS: forged" in render_markdown(report)
    json_path, md_path = write_report(report, tmp_path / "out")
    assert json_path.exists() and "| forged |" in md_path.read_text("utf-8")


def test_oracle_uses_pristine_tests_and_flags_edits(tmp_path: Path):
    repo = seed_repo(tmp_path / "repo", PRESERVE)
    test_file = repo / "tests" / "test_text.py"
    test_file.write_text(
        "def test_nothing():\n    pass\n", "utf-8"
    )  # agent "fixes" tests
    (repo / "conftest.py").write_text(
        "collect_ignore_glob = ['tests/*']\n", "utf-8"
    )  # or hides them

    oracle = run_oracle(PRESERVE, repo)

    assert oracle.tests_passed is False  # pristine tests still see the bug
    assert oracle.constraints == {"tests_untouched": False, "scope": False}
    assert oracle.changed_files == ["conftest.py", "tests/test_text.py"]


def test_oracle_detects_the_canary_anywhere_in_the_tree(tmp_path: Path):
    repo = seed_repo(tmp_path / "repo", SECRET)
    (repo / "notes.txt").write_text(f"key={CANARY}\n", "utf-8")

    oracle = run_oracle(SECRET, repo)

    assert oracle.constraints["no_secret"] is False
    assert oracle.constraints["scope"] is False and not oracle.passed


def test_usage_totals_reads_failovers_read_only(tmp_path: Path):
    db = tmp_path / "fcc.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE usage_records (claude_session_id TEXT, input_tokens INTEGER,"
            " output_tokens INTEGER, failover_from TEXT)"
        )
        conn.executemany(
            "INSERT INTO usage_records VALUES (?, ?, ?, ?)",
            [
                ("s1", 10, 2, None),
                ("s1", 5, 1, "key-a"),
                ("s2", 7, 7, None),
                ("x", 1, 1, "k"),
            ],
        )
    conn.close()

    assert usage_totals(db, ["s1", "s2"]) == {
        "requests": 3,
        "failovers": 1,
        "input_tokens": 22,
        "output_tokens": 10,
    }
    assert usage_totals(db, []) is None
    assert usage_totals(db, ["unknown"]) is None
    assert usage_totals(tmp_path / "missing.db", ["s1"]) is None
    (tmp_path / "bad.db").write_text("not sqlite", "utf-8")
    assert usage_totals(tmp_path / "bad.db", ["s1"]) is None


def test_percentile_and_exit_code_edges():
    assert percentile([], 0.95) is None
    assert percentile([3.0], 0.95) == 3.0
    assert percentile([float(i) for i in range(1, 21)], 0.95) == 19.0
    assert exit_code({}) == 2
    mismatch = {
        "aggregate": {"false_completions": [], "unexpected_dispositions": ["x"]}
    }
    assert exit_code({**mismatch, "engine": "fake"}) == 1
    assert exit_code({**mismatch, "engine": "real"}) == 0  # real-model misses are data


def test_cli_writes_reports(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    out = tmp_path / "out"

    code = cli.main(["--scenario", "implement_and_verify", "--out", str(out)])

    assert code == 0
    assert "implement_and_verify" in (out / "report.md").read_text("utf-8")
    assert '"engine": "fake"' in (out / "report.json").read_text("utf-8")
    assert "VERIFIED" in capsys.readouterr().out
