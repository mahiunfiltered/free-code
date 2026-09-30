"""Gate dispositions on real temp git projects, judge limits, evidence export."""

import json
import sys
from pathlib import Path

import pytest

from free_claude_code.workbench.checkpoints import (
    ChangedFile,
    TreeDiff,
    create_checkpoint,
    git,
)
from free_claude_code.workbench.ignore import (
    DEFAULT_DIFF_IGNORE,
    diff_ignore_patterns,
    split_ignored,
)
from free_claude_code.workbench.intent import IntentContract, Scope
from free_claude_code.workbench.verification.checks import CheckResult, GapRow
from free_claude_code.workbench.verification.gate import (
    compute_disposition,
    drop_ignored,
    render_markdown,
    run_gate,
)
from free_claude_code.workbench.verification.profile import ProjectProfile

PYTEST = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]


class FakeJudge:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.users: list[str] = []

    async def complete(self, system: str, user: str) -> str:
        self.users.append(user)
        return self.reply


def project(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    files = {
        ".gitignore": "__pycache__/\n",
        "pyproject.toml": "[tool.pytest.ini_options]\n",
        "src/calc.py": "def add(a, b):\n    return a + b\n",
        "test_calc.py": "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent / 'src'))\nfrom calc import add\n\ndef test_add():\n    assert add(2, 2) == 4\n",
    }
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def profile(root: Path, **commands: list[str]) -> ProjectProfile:
    return ProjectProfile(str(root), "python", {"test": PYTEST, **commands})


CONTRACT = IntentContract(
    goal="add a subtract helper",
    must=["add a subtract helper"],
    must_not=["add new dependencies"],
    scope=Scope(allowed_paths=["src/**"], prohibited_ops=["dependency.add"]),
    risk="medium",
)


def add_subtract(root: Path) -> None:
    (root / "src/calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_verified_when_all_evidence_passes(tmp_path: Path):
    root = project(tmp_path)
    cp = create_checkpoint(root)
    add_subtract(root)
    pkg = await run_gate(CONTRACT, str(root), cp, profile=profile(root))
    assert pkg.disposition == "VERIFIED", pkg.blocking_reasons
    assert pkg.level == "standard"
    assert [c.id for c in pkg.checks] == [
        "unit_test",
        "secret_scan",
        "diff_scope",
        "intent_conformance",
    ]
    assert (
        pkg.changed_files == ["src/calc.py"]
        and pkg.diff_sha256
        and pkg.base_commit == cp.head
    )
    assert pkg.checkpoint_commit == cp.commit and pkg.head_commit == cp.head
    assert {r.requirement_id: r.status for r in pkg.gap_matrix} == {
        "M1": "covered",
        "N1": "covered",
        "X1": "covered",
    }
    unit = pkg.checks[0]
    assert unit.command == PYTEST and unit.exit_code == 0 and unit.duration_s > 0


@pytest.mark.asyncio
async def test_failed_test_gives_failed_verification(tmp_path: Path):
    root = project(tmp_path)
    cp = create_checkpoint(root)
    (root / "src/calc.py").write_text(
        "def add(a, b):\n    return a - b\n", encoding="utf-8"
    )
    pkg = await run_gate(CONTRACT, str(root), cp, profile=profile(root))
    assert pkg.disposition == "FAILED_VERIFICATION"
    assert pkg.failures[0].path == "test_calc.py" and pkg.failures[0].category == "test"
    assert any("unit_test" in r for r in pkg.blocking_reasons)


@pytest.mark.asyncio
async def test_out_of_scope_change_fails_even_with_green_tests(tmp_path: Path):
    root = project(tmp_path)
    cp = create_checkpoint(root)
    add_subtract(root)
    (root / "README.md").write_text("docs\n", encoding="utf-8")
    pkg = await run_gate(CONTRACT, str(root), cp, profile=profile(root))
    assert pkg.disposition == "FAILED_VERIFICATION"
    assert any(f.category == "scope" and f.path == "README.md" for f in pkg.failures)


@pytest.mark.asyncio
async def test_no_declared_commands_needs_review(tmp_path: Path):
    root = project(tmp_path)
    cp = create_checkpoint(root)
    add_subtract(root)
    pkg = await run_gate(
        CONTRACT, str(root), cp, profile=ProjectProfile(str(root), "none")
    )
    assert pkg.disposition == "NEEDS_REVIEW"
    assert any("nothing executable proves" in r for r in pkg.blocking_reasons)


@pytest.mark.asyncio
async def test_non_git_project_needs_review(tmp_path: Path):
    root = tmp_path / "plain"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n", encoding="utf-8"
    )
    (root / "test_x.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    cp = create_checkpoint(root)
    pkg = await run_gate(CONTRACT, str(root), cp, profile=profile(root))
    assert pkg.checks[0].status == "passed"
    assert pkg.disposition == "NEEDS_REVIEW"
    assert any("no git checkpoint" in w for w in pkg.warnings)


@pytest.mark.asyncio
async def test_detects_profile_when_not_given(tmp_path: Path):
    root = project(tmp_path)
    (root / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    cp = create_checkpoint(root)
    pkg = await run_gate(IntentContract(goal="g", risk="low"), str(root), cp)
    assert pkg.profile_commands == {}  # pyproject declares no pytest: nothing invented
    assert pkg.disposition == "NEEDS_REVIEW"


@pytest.mark.asyncio
async def test_judge_cannot_upgrade_failed_row_but_can_fail_and_cover_missing(
    tmp_path: Path,
):
    root = project(tmp_path)
    cp = create_checkpoint(root)
    (root / "src/calc.py").write_text(
        "def add(a, b):\n    return 0\n", encoding="utf-8"
    )
    judge = FakeJudge(
        '{"verdicts": [{"id": "M1", "verdict": "pass", "reason": "looks right"}]}'
    )
    pkg = await run_gate(CONTRACT, str(root), cp, profile=profile(root), judge=judge)
    assert (
        pkg.gap_matrix[0].status == "failed"
        and pkg.disposition == "FAILED_VERIFICATION"
    )
    assert json.loads(judge.users[0])["requirements"][0]["id"] == "M1"

    root2 = project(tmp_path / "two")
    cp2 = create_checkpoint(root2)
    add_subtract(root2)
    judge_fail = FakeJudge(
        '{"verdicts": [{"id": "M1", "verdict": "fail", "reason": "no sub test"}]}'
    )
    pkg2 = await run_gate(
        CONTRACT, str(root2), cp2, profile=profile(root2), judge=judge_fail
    )
    assert (
        pkg2.disposition == "FAILED_VERIFICATION"
        and "judge: fail" in pkg2.gap_matrix[0].evidence[-1]
    )

    only_build = ProjectProfile(
        str(root2), "python", {"typecheck": [sys.executable, "-c", "pass"]}
    )
    no_judge = await run_gate(CONTRACT, str(root2), cp2, profile=only_build)
    assert (
        no_judge.gap_matrix[0].status == "missing"
        and no_judge.disposition == "NEEDS_REVIEW"
    )
    judged = await run_gate(
        CONTRACT,
        str(root2),
        cp2,
        profile=only_build,
        judge=FakeJudge('{"verdicts": [{"id": "M1", "verdict": "pass"}]}'),
    )
    assert judged.gap_matrix[0].status == "covered" and judged.disposition == "VERIFIED"


@pytest.mark.asyncio
async def test_invalid_judge_output_is_ignored_with_warning(tmp_path: Path):
    root = project(tmp_path)
    cp = create_checkpoint(root)
    add_subtract(root)
    pkg = await run_gate(
        CONTRACT,
        str(root),
        cp,
        profile=profile(root),
        judge=FakeJudge("I think it is fine"),
    )
    assert pkg.disposition == "VERIFIED"
    assert any("outcome judge ignored" in w for w in pkg.warnings)


@pytest.mark.asyncio
async def test_evidence_export_json_and_markdown(tmp_path: Path):
    root = project(tmp_path)
    cp = create_checkpoint(root)
    add_subtract(root)
    pkg = await run_gate(CONTRACT, str(root), cp, profile=profile(root))
    data = json.loads(json.dumps(pkg.to_json()))
    assert (
        data["disposition"] == "VERIFIED" and data["contract"]["must"] == CONTRACT.must
    )
    md = render_markdown(data)
    assert md == pkg.to_markdown()
    assert "# Evidence package: VERIFIED" in md and "| unit_test | passed | 0 |" in md
    assert (
        "| M1 | must | add a subtract helper | covered |" in md and "src/calc.py" in md
    )


def test_compute_disposition_rules():
    passed = CheckResult("unit_test", "unit_test", True, "passed", "ok")
    covered = GapRow("M1", "must", "x", "covered")
    assert compute_disposition([passed], [covered]) == ("VERIFIED", [])
    advisory_fail = CheckResult("lint", "lint", False, "failed", "style")
    assert compute_disposition([passed, advisory_fail], [covered])[0] == "VERIFIED"
    skipped = CheckResult("diff_scope", "diff_scope", True, "skipped", "no diff")
    assert compute_disposition([passed, skipped], [covered])[0] == "NEEDS_REVIEW"
    errored = CheckResult("build", "build", True, "error", "timeout")
    assert (
        compute_disposition([passed, errored, skipped], [covered])[0]
        == "FAILED_VERIFICATION"
    )
    assert (
        compute_disposition([passed], [GapRow("N1", "must_not", "x", "failed")])[0]
        == "FAILED_VERIFICATION"
    )
    assert compute_disposition([], [])[0] == "NEEDS_REVIEW"


@pytest.mark.asyncio
async def test_tool_state_files_are_ignored_and_reported(tmp_path: Path):
    root = project(tmp_path)
    cp = create_checkpoint(root)
    add_subtract(root)
    for name in (
        ".claude-flow/policy/state.json",
        "src/__pycache__/calc.cpython-314.pyc",
        ".pytest_cache/v/cache/nodeids",
        ".fcc-bench/run/report.json",
    ):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(
            '{"token": "not-a-real-secret-value"}\n', encoding="utf-8"
        )
    pkg = await run_gate(CONTRACT, str(root), cp, profile=profile(root))
    assert pkg.disposition == "VERIFIED", pkg.blocking_reasons
    assert pkg.changed_files == ["src/calc.py"]
    [warning] = [w for w in pkg.warnings if w.startswith("ignored ")]
    assert (
        ".claude-flow/policy/state.json" in warning and "3 tool/agent" in warning
    )  # __pycache__ is gitignored


@pytest.mark.asyncio
async def test_claude_settings_are_not_ignored(tmp_path: Path):
    root = project(tmp_path)
    cp = create_checkpoint(root)
    add_subtract(root)
    (root / ".claude").mkdir()
    (root / ".claude/settings.json").write_text("{}\n", encoding="utf-8")
    pkg = await run_gate(CONTRACT, str(root), cp, profile=profile(root))
    assert pkg.disposition == "FAILED_VERIFICATION"
    assert ".claude/settings.json" in pkg.changed_files


@pytest.mark.asyncio
async def test_project_verify_json_adds_ignore_globs(tmp_path: Path):
    root = project(tmp_path)
    (root / ".fcc").mkdir()
    (root / ".fcc/verify.json").write_text('{"ignore": ["build/**"]}', encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "verify config")
    cp = create_checkpoint(root)
    add_subtract(root)
    (root / "build").mkdir()
    (root / "build/out.txt").write_text("x\n", encoding="utf-8")
    pkg = await run_gate(CONTRACT, str(root), cp, profile=profile(root))
    assert pkg.disposition == "VERIFIED", pkg.blocking_reasons
    assert "build/out.txt" not in pkg.changed_files
    assert any("build/out.txt" in w for w in pkg.warnings)


def test_invalid_verify_json_warns_and_keeps_defaults(tmp_path: Path):
    (tmp_path / ".fcc").mkdir()
    (tmp_path / ".fcc/verify.json").write_text('{"ignore": "build"}', encoding="utf-8")
    patterns, warnings = diff_ignore_patterns(str(tmp_path))
    assert patterns == list(DEFAULT_DIFF_IGNORE)
    assert warnings and "list of globs" in warnings[0]
    (tmp_path / ".fcc/verify.json").write_text("{nope", encoding="utf-8")
    assert diff_ignore_patterns(str(tmp_path))[1]


def test_drop_ignored_removes_file_chunks_from_diff_text():
    text = (
        "diff --git a/src/a.py b/src/a.py\n+x = 1\n"
        "diff --git a/.claude-flow/s.json b/.claude-flow/s.json\n+{}\n"
        "diff --git a/b.py b/b.py\n+y = 2\n"
    )
    diff = TreeDiff(
        [ChangedFile("src/a.py", "M"), ChangedFile(".claude-flow/s.json", "A"),
         ChangedFile("b.py", "M")],
        text, "", "tree",
    )  # fmt: skip
    kept, ignored = drop_ignored(diff, list(DEFAULT_DIFF_IGNORE))
    assert ignored == [".claude-flow/s.json"]
    assert [f.path for f in kept.files] == ["src/a.py", "b.py"]
    assert ".claude-flow" not in kept.text and "+y = 2" in kept.text


def test_default_ignore_covers_plugin_state_folders():
    paths = [
        ".impeccable/state.json",
        "web/.impeccable/cache/x",
        ".claude-flow/policy/state.json",
        "src/app.py",
        ".claude/settings.json",  # tasks may edit settings: never ignored
    ]
    kept, ignored = split_ignored(paths, list(DEFAULT_DIFF_IGNORE))
    assert kept == ["src/app.py", ".claude/settings.json"]
    assert ignored == paths[:3]
