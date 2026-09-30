"""Recovery ladder bounds, classification and recovery prompts."""

import sys
from pathlib import Path

import pytest

from free_claude_code.workbench.checkpoints import create_checkpoint, git
from free_claude_code.workbench.intent import (
    IntentContract,
    Scope,
    extract_deterministic,
)
from free_claude_code.workbench.recovery import (
    MAX_PER_FINGERPRINT,
    MAX_TOTAL,
    build_recovery_prompt,
    classify,
    decide,
)
from free_claude_code.workbench.verification.checks import CheckResult, GapRow
from free_claude_code.workbench.verification.failures import (
    Category,
    Failure,
    make_failure,
)
from free_claude_code.workbench.verification.gate import (
    Disposition,
    EvidencePackage,
    run_gate,
)
from free_claude_code.workbench.verification.profile import ProjectProfile

CONTRACT = IntentContract(
    goal="fix add",
    must=["fix add()"],
    must_not=["change the public API"],
    preserve=["the public API"],
    scope=Scope(allowed_paths=["src/**"], protected_paths=["tests/**"]),
)


def package(
    failures: list[Failure],
    disposition: Disposition = "FAILED_VERIFICATION",
    output: str = "",
) -> EvidencePackage:
    check = CheckResult(
        "unit_test", "unit_test", True, "failed" if failures else "passed", "unit_test failed",
        command=["uv", "run", "pytest"], exit_code=1, output=output, failures=failures,
    )  # fmt: skip
    return EvidencePackage(
        contract=CONTRACT, level="standard", disposition=disposition,
        blocking_reasons=["required check 'unit_test' failed"], checks=[check],
        gap_matrix=[GapRow("M1", "must", "fix add()", "failed", ["unit_test: failed"])],
        failures=failures, warnings=[], base_commit=None, checkpoint_commit=None,
        head_commit=None, snapshot_tree=None, diff_sha256=None, diff_stat="",
        changed_files=[], started_at="", completed_at="",
    )  # fmt: skip


def failure(category: Category = "test", message: str = "assert 2 == 3") -> Failure:
    return make_failure(
        "unit_test", category, "test_failed", message, "tests/test_a.py", 4
    )


def test_same_fingerprint_is_bounded_and_changes_strategy():
    evidence = package([failure()])
    history: list[str] = []
    strategies = []
    for _ in range(MAX_PER_FINGERPRINT):
        d = decide(evidence, history)
        assert d.action == "retry" and d.attempt == len(history) + 1
        strategies.append(d.strategy)
        assert d.repeated == bool(history)
        history.append(d.fingerprint or "")
    assert len(set(strategies)) == MAX_PER_FINGERPRINT  # strategy changes every time
    assert decide(evidence, history).action == "give_up"


def test_total_attempts_bounded_across_fingerprints():
    history = [f"fp{i}" for i in range(MAX_TOTAL)]
    d = decide(package([failure(message="new failure")]), history)
    assert d.action == "give_up" and "budget" in d.reason
    assert decide(package([failure(message="new")]), history[:-1]).action == "retry"


def test_security_scope_intent_ask_user_immediately():
    for category in ("security", "scope", "intent"):
        d = decide(package([failure(), failure(category, "bad")]), [])
        assert d.action == "ask_user", category


def test_environment_ladder_ends_with_ask_user():
    evidence = package([failure("environment", "'uv' is not recognized")])
    fp = decide(evidence, []).fingerprint or ""
    assert decide(evidence, []).strategy == "inspect_environment"
    assert decide(evidence, [fp]).strategy == "remediate_dependency"
    assert decide(evidence, [fp, fp]).action == "ask_user"


def test_verified_needs_review_and_unstructured():
    assert decide(package([], "VERIFIED"), []).action == "none"
    assert decide(package([], "NEEDS_REVIEW"), []).action == "ask_user"
    assert decide(package([]), []).action == "ask_user"


def test_classify_raw_output():
    assert (
        classify("'npm' is not recognized as an internal or external command", "build")
        == "environment"
    )
    assert classify("HTTP 429 rate limit exceeded", "") == "provider"
    assert classify("sqlite3.OperationalError: no such table: users", "") == "database"
    assert (
        classify("Traceback (most recent call last):\nValueError: x", "") == "runtime"
    )
    assert (
        classify("Traceback (most recent call last):\nValueError: x", "unit_test")
        == "test"
    )
    assert classify("the operation timed out", "") == "tool"
    assert classify("weird", "typecheck") == "type"
    assert classify("weird", "") == "runtime"


def test_recovery_prompt_contents():
    output = "\n".join(f"line {i}" for i in range(200))
    prompt = build_recovery_prompt(
        CONTRACT,
        package([failure()], output=output),
        attempt=2,
        repeated=True,
        strategy="reproduce_and_isolate",
    )
    assert "attempt 2/8" in prompt
    assert "SAME failure" in prompt and "change strategy" in prompt
    assert "`uv run pytest` exit 1" in prompt
    assert "assert 2 == 3 (tests/test_a.py:4)" in prompt
    assert "line 199" in prompt and "line 100" not in prompt  # trimmed to the tail
    assert "- M1 MUST: fix add()" in prompt
    assert "- N1 MUST_NOT: change the public API" in prompt
    assert "- P1 PRESERVE: the public API" in prompt
    assert "never modify ['tests/**']" in prompt
    assert "Do not weaken, skip or delete tests" in prompt
    first = build_recovery_prompt(CONTRACT, package([failure()]), attempt=1)
    assert "SAME failure" not in first


def test_must_row_failed_by_a_test_retries_the_test_failure():
    """The intent row only mirrors the failing test; the test failure drives recovery."""

    derived = make_failure(
        "intent_conformance", "intent", "must_violated", "M1 (must): unit_test: failed"
    )
    d = decide(package([failure(), derived]), [])
    assert d.action == "retry" and d.fingerprint == failure().fingerprint
    # Judge-only MUST failure (no failed check) still needs the user.
    assert decide(package([derived]), []).action == "ask_user"
    # A real MUST_NOT violation still wins over the test failure.
    must_not = make_failure(
        "intent_conformance", "intent", "must_not_violated", "N1 violated"
    )
    assert decide(package([failure(), must_not]), []).action == "ask_user"


@pytest.mark.asyncio
async def test_failing_test_with_preserve_tests_row_is_retried(tmp_path: Path):
    """ "Don't modify tests/" yields a PRESERVE row; a failing test must not violate it."""

    root = tmp_path / "repo"
    files = {
        ".gitignore": "__pycache__/\n",
        "pyproject.toml": "[tool.pytest.ini_options]\n",
        "calc.py": "def add(a, b):\n    return a - b\n",
        "tests/test_calc.py": (
            "import sys, pathlib\n"
            "sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))\n"
            "from calc import add\n\n"
            "def test_add():\n    assert add(2, 2) == 4\n"
        ),
    }
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    for args in (
        ("init", "-q"),
        ("config", "user.email", "t@example.com"),
        ("config", "user.name", "t"),
        ("add", "-A"),
        ("commit", "-q", "-m", "init"),
    ):
        git(root, *args)
    contract = extract_deterministic(
        "Fix add() in calc.py. Don't modify tests/."
    ).contract
    assert contract.preserve  # the rule under test: a PRESERVE row exists
    checkpoint = create_checkpoint(root)
    (root / "calc.py").write_text(
        "def add(a, b):\n    return a * b + 0\n", encoding="utf-8"
    )  # still wrong for (2, 3) style inputs? no: 2*2 == 4 passes; make it fail:
    (root / "calc.py").write_text(
        "def add(a, b):\n    return a * b + 1\n", encoding="utf-8"
    )
    profile = ProjectProfile(
        str(root),
        "python",
        {"test": [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]},
    )

    evidence = await run_gate(contract, str(root), checkpoint, profile=profile)

    assert evidence.disposition == "FAILED_VERIFICATION"
    rows = {r.kind: r for r in evidence.gap_matrix}
    assert rows["preserve"].status == "missing"
    assert not any(f.code == "preserve_violated" for f in evidence.failures)
    decision = decide(evidence, [])
    assert decision.action == "retry", decision.reason
    assert decision.reason.startswith("test")


def test_real_preserve_violation_still_asks_user():
    violated = make_failure(
        "intent_conformance", "intent", "preserve_violated", "P1 changed tests/x.py"
    )
    assert decide(package([failure(), violated]), []).action == "ask_user"
