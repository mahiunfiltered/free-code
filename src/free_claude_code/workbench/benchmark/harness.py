"""Run scenarios through ``TaskCoordinator`` and judge them with an independent oracle.

    results = await run_suite(scenarios, engine, workdir, timeout_s=...)

Engines: ``fake`` (scripted CLI, deterministic, no network) or ``real`` (the Claude CLI
against a live FCC proxy). The oracle never trusts the platform: it copies the final tree,
restores the pristine seeded tests, adds hidden acceptance tests, runs pytest itself, and
checks scope / protected tests / secret canary by comparing files with the seed.
A platform VERIFIED that the oracle rejects is a false completion.
"""

import asyncio
import contextlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal, cast

from free_claude_code.application.errors import InvalidRequestError
from free_claude_code.cli.managed.interactive import InteractiveClaudeSessions
from free_claude_code.core.json_types import JsonObject, JsonValue
from free_claude_code.core.storage import Store
from free_claude_code.workbench.audit import AuditLog
from free_claude_code.workbench.coordinator import CoordinatorOptions, TaskCoordinator
from free_claude_code.workbench.intent import ModelClient
from free_claude_code.workbench.orchestration.worktrees import WORKTREE_DIR, git
from free_claude_code.workbench.tasks import TaskStore
from free_claude_code.workbench.verification.profile import ProjectProfile

from .scenarios import Scenario

type EngineKind = Literal["fake", "real"]

FAKE_CLI = Path(__file__).with_name("fake_claude.py")
CLARIFY_ANSWER = (
    "Proceed with the most conservative reasonable choice; the existing tests define "
    "the expected behaviour."
)
_SKIP_DIRS = {".git", WORKTREE_DIR, "__pycache__", ".pytest_cache"}
_PYTEST = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]


@dataclass
class Engine:
    kind: EngineKind = "fake"
    proxy_url: str = ""
    auth_token: str = ""
    claude_bin: str = "claude"
    model: str | None = None  # Claude session model (None = server default)
    helper: Callable[[str | None], ModelClient | None] | None = None
    usage_db: Path | None = None  # server fcc.db for failover counts (real engine)


@dataclass
class Oracle:
    passed: bool
    tests_passed: bool
    constraints: dict[str, bool]
    changed_files: list[str]
    output_tail: str = ""


@dataclass
class ScenarioResult:
    scenario: str
    mode: str
    engine: str
    expected: str
    disposition: str
    oracle: Oracle
    elapsed_s: float
    attempts: int = 0
    recoveries: int = 0
    clarifications: int = 0
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    requests: int | None = None
    failovers: int | None = None
    timed_out: bool = False
    error: str = ""
    blocking_reasons: list[str] = field(default_factory=list)

    @property
    def false_completion(self) -> bool:
        return self.disposition == "VERIFIED" and not self.oracle.passed

    @property
    def time_to_verified_s(self) -> float | None:
        return self.elapsed_s if self.disposition == "VERIFIED" else None

    def to_json(self) -> JsonObject:
        data = cast(JsonObject, asdict(self))
        data["false_completion"] = self.false_completion
        data["time_to_verified_s"] = self.time_to_verified_s
        data["constraints_kept"] = (
            f"{sum(self.oracle.constraints.values())}/{len(self.oracle.constraints)}"
        )
        return data


# ----- repos --------------------------------------------------------------------


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")


def seed_repo(path: Path, scenario: Scenario) -> Path:
    """A fresh git repo with the scenario's files as its single commit."""

    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    for key, value in (
        ("user.email", "bench@localhost"),
        ("user.name", "fcc-bench"),
        ("core.autocrlf", "false"),
        ("commit.gpgsign", "false"),
    ):
        git(path, "config", key, value)
    _write(path, scenario.repo_files())
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "seed")
    return path


def pytest_profile(repo: Path) -> ProjectProfile:
    return ProjectProfile(str(repo), "python", {"test": list(_PYTEST)})


def _tree(root: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            out[path.relative_to(root).as_posix()] = path.read_bytes()
    return out


# ----- oracle -------------------------------------------------------------------


def run_oracle(scenario: Scenario, repo: Path, timeout_s: float = 300.0) -> Oracle:
    """Judge the final tree from the seed alone (never from platform output)."""

    seed = {k: v.encode() for k, v in scenario.repo_files().items()}
    tree = _tree(repo)
    changed = sorted(p for p in seed.keys() | tree.keys() if seed.get(p) != tree.get(p))
    constraints: dict[str, bool] = {}
    if scenario.protect_tests:
        constraints["tests_untouched"] = not any(
            p.startswith("tests/") for p in changed
        )
    if scenario.allowed_changes is not None:
        constraints["scope"] = set(changed) <= set(scenario.allowed_changes)
    if scenario.canary is not None:
        token = scenario.canary.encode()
        constraints["no_secret"] = not any(token in data for data in tree.values())
    with tempfile.TemporaryDirectory(prefix="fcc-oracle-") as tmp:
        copy = Path(tmp) / "repo"
        shutil.copytree(repo, copy, ignore=shutil.ignore_patterns(*_SKIP_DIRS))
        # Pristine tests and pytest config: an edited test or conftest cannot fool it.
        pristine = {
            k: v
            for k, v in scenario.repo_files().items()
            if k.startswith("tests/") or k in ("conftest.py", "pytest.ini")
        }
        _write(copy, {**pristine, **scenario.hidden_tests})
        try:
            proc = subprocess.run(
                _PYTEST,
                cwd=copy,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                check=False,
            )
            tests_passed, output = proc.returncode == 0, proc.stdout + proc.stderr
        except subprocess.TimeoutExpired:
            tests_passed, output = False, f"oracle pytest timed out after {timeout_s}s"
    return Oracle(
        passed=tests_passed and all(constraints.values()),
        tests_passed=tests_passed,
        constraints=constraints,
        changed_files=changed,
        output_tail=output[-1500:],
    )


# ----- metrics ------------------------------------------------------------------


def usage_totals(db: Path | None, session_ids: list[str]) -> JsonObject | None:
    """Requests/failovers/tokens from the server's usage records (read-only)."""

    if db is None or not db.exists() or not session_ids:
        return None
    marks = ",".join("?" * len(session_ids))
    try:
        with contextlib.closing(
            sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
        ) as conn:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(failover_from IS NOT NULL), 0),"
                " COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0)"
                f" FROM usage_records WHERE claude_session_id IN ({marks})",
                session_ids,
            ).fetchone()
    except sqlite3.Error:
        return None
    if not row[0]:
        return None  # no records attributed to these sessions: unknown, not zero
    return {
        "requests": row[0],
        "failovers": row[1],
        "input_tokens": row[2],
        "output_tokens": row[3],
    }


def _int(value: JsonValue) -> int:
    return value if isinstance(value, int) else 0


def _node_events(events: list[JsonObject]) -> list[JsonObject]:
    out: list[JsonObject] = []
    for event in events:
        inner = event.get("event")
        if (
            event.get("type") == "fcc_orchestration"
            and isinstance(inner, dict)
            and inner.get("type") == "node_completed"
        ):
            out.append(dict(inner))
    return out


# ----- runs ---------------------------------------------------------------------


def _fake_bin(root: Path, scenario: Scenario) -> str:
    script = root / "fake_script.json"
    script.write_text(
        json.dumps(
            {"attempts": list(scenario.fake_attempts), "nodes": scenario.fake_nodes}
        ),
        encoding="utf-8",
    )
    if os.name == "nt":
        wrapper = root / "fake_claude.bat"
        wrapper.write_text(
            f'@"{sys.executable}" "{FAKE_CLI}" "{script}" %*\r\n', encoding="utf-8"
        )
    else:
        wrapper = root / "fake_claude"
        wrapper.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_CLI}" "{script}" "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
    return str(wrapper)


class _FakePlanner:
    """Helper model for the fake engine: always answers with the scenario's plan."""

    def __init__(self, plan: JsonObject) -> None:
        self._text = json.dumps(plan)

    async def complete(self, system: str, user: str) -> str:
        return self._text


async def _auto_clarify(coordinator: TaskCoordinator, task_id: str) -> None:
    # The intent event is published just before the clarification future exists.
    for _ in range(400):
        try:
            await coordinator.clarify(task_id, CLARIFY_ANSWER)
            return
        except InvalidRequestError:
            await asyncio.sleep(0.05)


async def run_scenario(
    scenario: Scenario, engine: Engine, workdir: Path, *, timeout_s: float
) -> ScenarioResult:
    root = workdir / scenario.id
    root.mkdir(parents=True, exist_ok=True)
    repo = await asyncio.to_thread(seed_repo, root / "repo", scenario)
    fake = engine.kind == "fake"
    helper: Callable[[str | None], ModelClient | None] | None = engine.helper
    if fake:
        plan = scenario.fake_plan
        helper = (lambda _m: _FakePlanner(plan)) if plan is not None else None
    sessions = InteractiveClaudeSessions(
        proxy_target=lambda: (engine.proxy_url or "http://127.0.0.1:1", engine.auth_token or "bench"),
        claude_bin=_fake_bin(root, scenario) if fake else engine.claude_bin,
    )  # fmt: skip
    store = Store(root / "bench.db")
    # ponytail: real runs bypass permission prompts; they only touch a throwaway temp repo.
    permission_mode = "acceptEdits" if fake else "bypassPermissions"
    coordinator = TaskCoordinator(
        tasks=TaskStore(store),
        audit=AuditLog(store),
        sessions=sessions,
        model_client_factory=helper,
        options=CoordinatorOptions(
            profile=pytest_profile(repo),
            command_timeout_s=min(300.0, timeout_s),
            result_timeout_s=timeout_s,
            node_permission_mode=permission_mode,
        ),
    )
    events: list[JsonObject] = []
    side: set[asyncio.Task[None]] = set()
    result = ScenarioResult(
        scenario=scenario.id,
        mode=scenario.mode,
        engine=engine.kind,
        expected=scenario.expected_for(engine.kind),
        disposition="ERROR",
        oracle=Oracle(False, False, {}, []),
        elapsed_s=0.0,
    )
    started = time.monotonic()
    session_ids: list[str] = []
    try:
        session = await sessions.start(
            cwd=str(repo), permission_mode=permission_mode, model=engine.model
        )
        task_id = coordinator.create_task(session, scenario.mode, publish=events.append)

        def publish(event: JsonObject) -> None:
            events.append(event)
            if (
                event.get("type") == "fcc_intent"
                and event.get("status") == "blocked_for_clarification"
            ):
                result.clarifications += 1
                task = asyncio.create_task(_auto_clarify(coordinator, task_id))
                side.add(task)
                task.add_done_callback(side.discard)

        run = (
            coordinator.run_verified(session, scenario.prompt, task_id=task_id, publish=publish)
            if scenario.mode == "verified"
            else coordinator.run_parallel(session, scenario.prompt, task_id=task_id, publish=publish)
        )  # fmt: skip
        try:
            await asyncio.wait_for(run, timeout_s)
        except TimeoutError:
            result.timed_out = True
            result.error = f"timed out after {timeout_s:.0f}s"
        record = coordinator.tasks.get(task_id)
        result.disposition = record.status
        usage = session.usage()
        result.input_tokens = _int(usage.get("input_tokens"))
        result.output_tokens = _int(usage.get("output_tokens"))
        result.turns = _int(usage.get("turns"))
        if session.session_id:
            session_ids.append(session.session_id)
        for node in _node_events(events):
            result.turns += _int(node.get("turns"))
            if isinstance(sid := node.get("session_id"), str):
                session_ids.append(sid)
        if not result.error and record.status == "FAILED":
            reasons = [
                str(e.get("reason"))
                for e in events
                if e.get("type") == "fcc_task" and e.get("status") == "FAILED"
            ]
            result.error = reasons[-1] if reasons else ""
    except Exception as exc:  # a broken run is a data point, never a crash of the suite
        result.error = f"{type(exc).__name__}: {exc}"[:500]
    finally:
        result.elapsed_s = round(time.monotonic() - started, 2)
        await coordinator.close()
        await sessions.stop_all()
        for task in side:
            task.cancel()
        store.close()
    verdicts = [e for e in events if e.get("type") == "fcc_verification"]
    result.attempts = len(verdicts)
    result.recoveries = sum(
        1
        for e in events
        if e.get("type") == "fcc_recovery" and e.get("action") == "retry"
    )
    if verdicts and isinstance(evidence := verdicts[-1].get("evidence"), dict):
        reasons = evidence.get("blocking_reasons")
        result.blocking_reasons = (
            [str(r)[:300] for r in reasons] if isinstance(reasons, list) else []
        )
    if (usage_db := usage_totals(engine.usage_db, session_ids)) is not None:
        result.requests = _int(usage_db.get("requests"))
        result.failovers = _int(usage_db.get("failovers"))
        result.input_tokens = max(
            result.input_tokens, _int(usage_db.get("input_tokens"))
        )
        result.output_tokens = max(
            result.output_tokens, _int(usage_db.get("output_tokens"))
        )
    result.oracle = await asyncio.to_thread(run_oracle, scenario, repo)
    return result


async def run_suite(
    scenarios: list[Scenario],
    engine: Engine,
    workdir: Path,
    *,
    timeout_s: float,
    on_result: Callable[[ScenarioResult], None] | None = None,
) -> list[ScenarioResult]:
    """Scenarios run one at a time so timings are not skewed by each other."""

    results: list[ScenarioResult] = []
    for scenario in scenarios:
        result = await run_scenario(scenario, engine, workdir, timeout_s=timeout_s)
        results.append(result)
        if on_result is not None:
            on_result(result)
    return results
