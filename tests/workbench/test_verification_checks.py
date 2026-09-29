"""Check runners on real processes/repos: commands, parsers, secret_scan, diff_scope, probes."""

import http.server
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from free_claude_code.workbench.checkpoints import create_checkpoint, diff_since, git
from free_claude_code.workbench.intent import IntentContract, Scope
from free_claude_code.workbench.verification.checks import (
    CheckResult,
    conformance_result,
    diff_scope,
    gap_matrix,
    http_probe,
    run_command,
    run_command_check,
    secret_scan,
)
from free_claude_code.workbench.verification.failures import (
    make_failure,
    normalize,
    parse_failures,
)
from free_claude_code.workbench.verification.profile import HttpProbe, PlannedCheck

PYTEST = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]


def py_project(root: Path, *, failing: bool) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n", encoding="utf-8"
    )
    (root / "test_ok.py").write_text(
        "def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8"
    )
    if failing:
        (root / "test_bad.py").write_text(
            "def test_bad():\n    assert 1 + 1 == 3\n", encoding="utf-8"
        )
    return root


def make_repo(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    git(root, "config", "core.autocrlf", "false")
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------- commands


@pytest.mark.asyncio
async def test_real_pytest_pass_and_fail(tmp_path: Path):
    ok = await run_command_check(
        PlannedCheck("unit_test", "unit_test", True, PYTEST),
        str(py_project(tmp_path / "ok", failing=False)),
    )
    assert ok.status == "passed" and ok.exit_code == 0 and "1 passed" in ok.output

    bad = await run_command_check(
        PlannedCheck("unit_test", "unit_test", True, PYTEST),
        str(py_project(tmp_path / "bad", failing=True)),
    )
    assert bad.status == "failed" and bad.exit_code == 1
    [failure] = bad.failures
    assert (
        failure.path == "test_bad.py"
        and failure.line == 2
        and failure.category == "test"
    )
    assert "test_bad" in failure.message and "failed" in bad.summary


@pytest.mark.asyncio
async def test_timeout_kills_whole_process_tree(tmp_path: Path):
    pid_file = tmp_path / "child.pid"
    script = (
        "import subprocess, sys, time\n"
        "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open(r'{pid_file}', 'w').write(str(c.pid))\n"
        "time.sleep(60)\n"
    )
    started = time.monotonic()
    result = await run_command_check(
        PlannedCheck("build", "build", True, [sys.executable, "-c", script]),
        str(tmp_path),
        timeout=3,
    )
    assert result.status == "error" and result.failures[0].code == "timeout"
    assert result.failures[0].category == "tool"
    assert time.monotonic() - started < 20
    child = int(pid_file.read_text())
    deadline = time.monotonic() + 10
    while _alive(child) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not _alive(child)


@pytest.mark.asyncio
async def test_output_cap_keeps_tail(tmp_path: Path):
    run = await run_command(
        [sys.executable, "-c", "print('x' * 200000); print('THE-END')"],
        str(tmp_path),
        output_cap=1000,
    )
    assert run.exit_code == 0 and run.truncated
    assert "THE-END" in run.output and len(run.output) < 1200


@pytest.mark.asyncio
async def test_missing_executable_is_environment_error(tmp_path: Path):
    result = await run_command_check(
        PlannedCheck("lint", "lint", False, ["fcc-no-such-tool-xyz", "--x"]),
        str(tmp_path),
    )
    assert result.status == "error"
    assert (
        result.failures[0].category == "environment"
        and result.failures[0].code == "command_not_found"
    )


@pytest.mark.asyncio
async def test_empty_argv_is_skipped(tmp_path: Path):
    assert (
        await run_command_check(
            PlannedCheck("lint", "lint", False, None), str(tmp_path)
        )
    ).status == "skipped"


# --------------------------------------------------------------------------- parsers + fingerprints

SAMPLES = {
    "pytest": (
        "unit_test",
        "tests/test_a.py:12: AssertionError\nFAILED tests/test_a.py::test_x - assert 1 == 2\n",
        ("tests/test_a.py", 12, "test"),
    ),
    "ruff_concise": (
        "lint",
        "src/a.py:3:1: F401 [*] `os` imported but unused\n",
        ("src/a.py", 3, "build"),
    ),
    "ruff_full": (
        "lint",
        "F401 [*] `os` imported but unused\n --> src/a.py:3:8\n  |\n",
        ("src/a.py", 3, "build"),
    ),
    "ty": (
        "typecheck",
        "error[invalid-argument-type]: Argument is incorrect\n  --> src/b.py:9:5\n",
        ("src/b.py", 9, "type"),
    ),
    "mypy": (
        "typecheck",
        "src/c.py:4: error: Incompatible types  [assignment]\n",
        ("src/c.py", 4, "type"),
    ),
    "tsc_plain": (
        "typecheck",
        "src/x.ts(10,5): error TS2322: Type 'string' is not assignable\n",
        ("src/x.ts", 10, "type"),
    ),
    "tsc_pretty": (
        "typecheck",
        "src/x.ts:7:1 - error TS2304: Cannot find name 'foo'.\n",
        ("src/x.ts", 7, "type"),
    ),
    "jest": (
        "unit_test",
        "FAIL src/sum.test.js\n  \u25cf sum \u203a adds numbers\n",
        ("src/sum.test.js", None, "test"),
    ),
    "vitest": (
        "unit_test",
        " FAIL  src/sum.test.ts > sum > adds\n",
        ("src/sum.test.ts", None, "test"),
    ),
    "go": (
        "unit_test",
        "--- FAIL: TestSum (0.00s)\n    sum_test.go:8: got 3 want 4\nFAIL\n",
        ("sum_test.go", 8, "test"),
    ),
    "cargo": (
        "unit_test",
        "---- tests::adds stdout ----\nthread 'tests::adds' panicked at src/lib.rs:10:9:\nassertion failed\n",
        ("src/lib.rs", 10, "test"),
    ),
}


@pytest.mark.parametrize("name", list(SAMPLES))
def test_failure_parsers(name: str):
    kind, output, (path, line, category) = SAMPLES[name]
    [failure, *_] = parse_failures(kind, kind, output)
    assert (failure.path, failure.line, failure.category) == (path, line, category)
    assert len(failure.fingerprint) == 16


def test_generic_fallback_categorizes_environment():
    [f] = parse_failures(
        "build",
        "build",
        "noise\n'foo' is not recognized as an internal or external command\n",
    )
    assert f.category == "environment" and "not recognized" in f.message
    [g] = parse_failures("build", "build", "")
    assert g.message == "(no output)" and g.category == "build"


def test_fingerprint_ignores_temp_dirs_ids_and_durations_but_keeps_lines():
    a = make_failure(
        "t",
        "test",
        "c",
        "boom in 0.52s id 3f2a9c7e1b4d at C:\\Users\\me\\AppData\\Local\\Temp\\pytest-of-me\\pytest-12\\test_x0\\src\\a.py",
        "C:/Users/me/AppData/Local/Temp/pytest-of-me/pytest-12/test_x0/src/a.py",
        3,
    )
    b = make_failure(
        "t",
        "test",
        "c",
        "boom in 1.9s id 88aa77bb66cc at /tmp/pytest-of-ci/pytest-99/test_x3/src/a.py",
        "/tmp/pytest-of-ci/pytest-99/test_x3/src/a.py",
        3,
    )
    c = make_failure(
        "t",
        "test",
        "c",
        "boom in 1.9s id 88aa77bb66cc at /tmp/pytest-of-ci/pytest-99/test_x3/src/a.py",
        "/tmp/pytest-of-ci/pytest-99/test_x3/src/a.py",
        4,
    )
    assert a.fingerprint == b.fingerprint != c.fingerprint
    assert normalize("/tmp/tmpab12cd/src/a.py") == "<tmp>/src/a.py"
    assert "assert 1 == 2" in normalize("assert 1 == 2")


# --------------------------------------------------------------------------- secret_scan / diff_scope

NV_KEY = "nvapi-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"


def test_secret_scan_on_real_repo(tmp_path: Path):
    repo = make_repo(tmp_path, {"old.py": f"KEY = '{NV_KEY}'\n", "app.py": "x = 1\n"})
    cp = create_checkpoint(repo)
    (repo / "app.py").write_text(f"x = 1\ntoken = '{NV_KEY}'\n", encoding="utf-8")
    (repo / ".env").write_text(
        "OPENAI_API_KEY=abc123def456ghi\nDEBUG=true\nEMPTY=\n", encoding="utf-8"
    )
    (repo / ".env.example").write_text(
        "OPENAI_API_KEY=real-looking-value-123\n", encoding="utf-8"
    )
    (repo / "k.pem").write_text(
        "-----BEGIN RSA PRIVATE KEY-----\nMIIB\n", encoding="utf-8"
    )
    (repo / "aws.txt").write_text(
        "id=AKIAABCDEFGHIJKLMNOP\npassword = 'your-password-here'\n", encoding="utf-8"
    )
    result = secret_scan(
        PlannedCheck("secret_scan", "secret_scan", True), diff_since(cp)
    )
    assert result.status == "failed"
    found = {(f.path, f.line) for f in result.failures}
    assert found == {("app.py", 2), (".env", 1), ("k.pem", 1), ("aws.txt", 1)}
    assert all(f.category == "security" for f in result.failures)
    assert NV_KEY not in result.summary and all(
        NV_KEY not in f.message for f in result.failures
    )
    assert "old.py" not in {
        p for p, _ in found
    }  # pre-existing secret is not the task's


def test_secret_scan_clean_and_without_diff(tmp_path: Path):
    repo = make_repo(tmp_path, {"a.py": "x = 1\n"})
    cp = create_checkpoint(repo)
    (repo / "a.py").write_text("x = 2  # sk- is fine when short\n", encoding="utf-8")
    check = PlannedCheck("secret_scan", "secret_scan", True)
    assert secret_scan(check, diff_since(cp)).status == "passed"
    assert secret_scan(check, None).status == "skipped"


def test_diff_scope_allowed_and_protected(tmp_path: Path):
    repo = make_repo(
        tmp_path, {"src/a.py": "a\n", "src/legacy/old.py": "o\n", "README.md": "r\n"}
    )
    cp = create_checkpoint(repo)
    for name in ("src/a.py", "src/legacy/old.py", "README.md"):
        (repo / name).write_text("changed\n", encoding="utf-8")
    check = PlannedCheck("diff_scope", "diff_scope", True)
    contract = IntentContract(
        goal="g",
        scope=Scope(allowed_paths=["src/**"], protected_paths=["src/legacy/**"]),
    )
    result = diff_scope(check, contract, diff_since(cp))
    assert result.status == "failed"
    assert {(f.path, f.code) for f in result.failures} == {
        ("src/legacy/old.py", "protected_path"),
        ("README.md", "outside_scope"),
    }
    assert all(f.category == "scope" for f in result.failures)
    assert (
        diff_scope(check, IntentContract(goal="g"), diff_since(cp)).status == "passed"
    )
    assert diff_scope(check, contract, None).status == "skipped"


# --------------------------------------------------------------------------- conformance


def _passed_tests() -> list[CheckResult]:
    return [CheckResult("unit_test", "unit_test", True, "passed", "ok")]


def test_conformance_heuristics_on_real_diff(tmp_path: Path):
    repo = make_repo(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "x"\ndependencies = [\n    "httpx>=0.27",\n]\n',
            "api.py": "def public():\n    return 1\n",
            "gone.py": "x\n",
            "db/migrations/0001.sql": "create table a();\n",
        },
    )
    cp = create_checkpoint(repo)
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = [\n    "httpx>=0.27",\n    "requests>=2",\n]\n',
        encoding="utf-8",
    )
    (repo / "api.py").write_text("def renamed():\n    return 1\n", encoding="utf-8")
    (repo / "gone.py").unlink()
    (repo / "db/migrations/0002.sql").write_text("alter table a;\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "task committed")
    contract = IntentContract(
        goal="g",
        must=["add retries"],
        must_not=[
            "add new dependencies",
            "change the public API",
            "touch the database schema",
            "rewrite the docs",
        ],
        preserve=["the CLI output"],
        scope=Scope(
            prohibited_ops=[
                "dependency.add",
                "git.commit",
                "git.push",
                "fs.remove",
                "db.migration",
                "net.deploy",
            ]
        ),
    )
    head_moved = git(repo, "rev-parse", "HEAD").strip() != cp.head
    rows = {
        r.requirement_id: r
        for r in gap_matrix(contract, diff_since(cp), _passed_tests(), head_moved)
    }
    assert rows["M1"].status == "covered" and rows["P1"].status == "covered"
    assert rows["N1"].status == "failed" and "pyproject.toml" in rows["N1"].evidence[0]
    assert rows["N2"].status == "failed"  # removed `def public`
    assert rows["N3"].status == "failed"
    assert rows["N4"].status == "missing"  # no heuristic: needs review
    statuses = {rows[f"X{i}"].text: rows[f"X{i}"].status for i in range(1, 7)}
    assert statuses == {
        "dependency.add": "failed", "git.commit": "failed", "git.push": "missing",
        "fs.remove": "failed", "db.migration": "failed", "net.deploy": "missing",
    }  # fmt: skip
    result = conformance_result(
        PlannedCheck("intent_conformance", "intent_conformance", True),
        list(rows.values()),
    )
    assert result.status == "failed" and all(
        f.category == "intent" for f in result.failures
    )


def test_conformance_clean_diff_and_missing_evidence(tmp_path: Path):
    repo = make_repo(
        tmp_path,
        {
            "a.py": "x = 1\n",
            "package.json": '{\n  "version": "1.0.0",\n  "dependencies": {\n    "a": "1"\n  }\n}\n',
        },
    )
    cp = create_checkpoint(repo)
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "package.json").write_text(
        '{\n  "version": "1.0.1",\n  "dependencies": {\n    "a": "1"\n  }\n}\n',
        encoding="utf-8",
    )
    contract = IntentContract(
        goal="g",
        must=["bump x"],
        must_not=["no new dependencies", "push", "commit"],
        preserve=["src/keep.py"],
    )
    rows = gap_matrix(contract, diff_since(cp), _passed_tests(), head_moved=False)
    assert [r.status for r in rows] == ["covered"] * 5
    check = PlannedCheck("intent_conformance", "intent_conformance", True)
    assert conformance_result(check, rows).status == "passed"
    no_tests = gap_matrix(contract, None, [], head_moved=None)
    assert {r.status for r in no_tests} == {"missing"}
    assert conformance_result(check, no_tests).status == "needs_review"
    failed_tests = [CheckResult("unit_test", "unit_test", True, "failed", "boom")]
    assert gap_matrix(contract, None, failed_tests, None)[0].status == "failed"


def test_preserved_path_change_fails_row(tmp_path: Path):
    repo = make_repo(tmp_path, {"src/keep.py": "k\n"})
    cp = create_checkpoint(repo)
    (repo / "src/keep.py").write_text("changed\n", encoding="utf-8")
    [row] = gap_matrix(
        IntentContract(goal="g", preserve=["src/keep.py"]),
        diff_since(cp),
        _passed_tests(),
        False,
    )
    assert row.status == "failed"


def test_dont_modify_tests_is_decided_from_test_paths(tmp_path: Path):
    repo = make_repo(tmp_path, {"calc.py": "x = 1\n", "tests/test_calc.py": "t = 1\n"})
    cp = create_checkpoint(repo)
    contract = IntentContract(goal="g", must_not=["modify the tests"])
    (repo / "calc.py").write_text("x = 2\n", encoding="utf-8")
    [clean] = gap_matrix(contract, diff_since(cp), _passed_tests(), False)
    assert clean.status == "covered"
    (repo / "tests/test_calc.py").write_text("t = 2\n", encoding="utf-8")
    (repo / "web.spec.ts").write_text("x\n", encoding="utf-8")
    [broken] = gap_matrix(contract, diff_since(cp), _passed_tests(), False)
    assert broken.status == "failed"
    assert "tests/test_calc.py" in broken.evidence[0]
    assert "web.spec.ts" in broken.evidence[0]


# --------------------------------------------------------------------------- http probe


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200 if self.path == "/health" else 500)
        self.end_headers()
        self.wfile.write(b"body")

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.mark.asyncio
async def test_http_probe_statuses():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        ok = await http_probe(
            PlannedCheck("p", "http_probe", True, probe=HttpProbe(f"{base}/health"))
        )
        bad = await http_probe(
            PlannedCheck("p", "http_probe", True, probe=HttpProbe(f"{base}/boom"))
        )
    finally:
        server.shutdown()
        server.server_close()
    assert ok.status == "passed" and ok.exit_code == 200
    assert bad.status == "failed" and bad.failures[0].category == "API"
    down = await http_probe(
        PlannedCheck("p", "http_probe", True, probe=HttpProbe(base)), timeout=2
    )
    assert down.status == "error" and down.failures[0].code == "probe_unreachable"
    assert (await http_probe(PlannedCheck("p", "http_probe", True))).status == "skipped"


def test_preserve_inside_an_allowed_file_is_left_to_the_tests(tmp_path: Path):
    repo = make_repo(tmp_path, {"calc.py": "a\n", "other.py": "b\n"})
    cp = create_checkpoint(repo)
    (repo / "calc.py").write_text("fixed\n", encoding="utf-8")
    contract = IntentContract(
        goal="g",
        preserve=["All other code in calc.py besides add()"],
        scope=Scope(allowed_paths=["**/calc.py"]),
    )
    [row] = gap_matrix(contract, diff_since(cp), _passed_tests(), False)
    assert row.status == "covered"
    contract.scope.allowed_paths = ["other.py"]
    [row] = gap_matrix(contract, diff_since(cp), _passed_tests(), False)
    assert row.status == "failed"
