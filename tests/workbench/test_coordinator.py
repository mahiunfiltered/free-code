"""Verified / parallel task runs end-to-end: fake Claude CLI + real git repo + real pytest."""

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio

from free_claude_code.application.errors import InvalidRequestError
from free_claude_code.cli.managed.interactive import (
    InteractiveClaudeSession,
    InteractiveClaudeSessions,
)
from free_claude_code.core.json_types import JsonObject, JsonValue
from free_claude_code.core.storage import Store
from free_claude_code.workbench.audit import AuditLog
from free_claude_code.workbench.coordinator import CoordinatorOptions, TaskCoordinator
from free_claude_code.workbench.memory import SolutionMemory
from free_claude_code.workbench.orchestration.worktrees import git
from free_claude_code.workbench.tasks import TaskStore
from free_claude_code.workbench.verification.profile import ProjectProfile

FAKE = Path(__file__).with_name("fake_claude_coder.py")
BUGGY = "def add(a, b):\n    return a - b\n"
FIX = "WRITE calc.py def add(a, b): return a + b"
WRONG = "WRITE calc.py def add(a, b): return a * b"


def fake_claude(tmp_path: Path) -> str:
    if os.name == "nt":
        wrapper = tmp_path / "fake_claude.bat"
        wrapper.write_text(f'@"{sys.executable}" "{FAKE}" %*\r\n', encoding="utf-8")
    else:
        wrapper = tmp_path / "fake_claude"
        wrapper.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n', encoding="utf-8"
        )
        wrapper.chmod(0o755)
    return str(wrapper)


def make_repo(tmp_path: Path) -> Path:
    """A tiny pytest project whose only test fails until add() is fixed."""

    repo = tmp_path / "proj"
    (repo / "tests").mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    for key, value in (
        ("user.email", "t@example.com"),
        ("user.name", "Test"),
        ("core.autocrlf", "false"),
        ("commit.gpgsign", "false"),
    ):
        git(repo, "config", key, value)
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", "utf-8")
    (repo / "conftest.py").write_text("", "utf-8")  # puts the root on sys.path
    (repo / "calc.py").write_text(BUGGY, "utf-8")
    (repo / "tests" / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        "utf-8",
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    return repo


def obj(value: JsonValue) -> JsonObject:
    """Narrow a JSON value to an object (fails the test otherwise)."""

    assert isinstance(value, dict), value
    return value


def objs(value: JsonValue) -> list[JsonObject]:
    assert isinstance(value, list), value
    return [obj(item) for item in value]


def pytest_profile(repo: Path) -> ProjectProfile:
    return ProjectProfile(
        str(repo),
        "python",
        {"test": [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]},
    )


@dataclass
class Env:
    repo: Path
    store: Store
    tasks: TaskStore
    audit: AuditLog
    memory: SolutionMemory
    sessions: InteractiveClaudeSessions
    coordinator: TaskCoordinator
    events: list[JsonObject]

    async def session(self, **kwargs) -> InteractiveClaudeSession:
        return await self.sessions.start(
            cwd=str(self.repo), permission_mode="acceptEdits", **kwargs
        )

    def types(self) -> list[str]:
        return [str(e["type"]) for e in self.events]

    def of(self, kind: str) -> list[JsonObject]:
        return [e for e in self.events if e["type"] == kind]


@pytest_asyncio.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    store = Store(tmp_path / "fcc.db")
    tasks, audit, memory = TaskStore(store), AuditLog(store), SolutionMemory(store)
    sessions = InteractiveClaudeSessions(
        proxy_target=lambda: ("http://127.0.0.1:1", "token"),
        claude_bin=fake_claude(tmp_path),
    )
    repo = make_repo(tmp_path)
    coordinator = TaskCoordinator(
        tasks=tasks,
        audit=audit,
        memory=memory,
        sessions=sessions,
        options=CoordinatorOptions(profile=pytest_profile(repo), command_timeout_s=120),
    )
    yield Env(repo, store, tasks, audit, memory, sessions, coordinator, [])
    await coordinator.close()
    await sessions.stop_all()
    store.close()


def task_id_of(env: Env) -> str:
    return str(env.of("fcc_task")[0]["task_id"])


async def wait_status(env: Env, task_id: str, status: str, timeout: float = 30.0):
    async def poll() -> None:
        while env.tasks.get(task_id).status != status:
            await asyncio.sleep(0.05)

    await asyncio.wait_for(poll(), timeout)


@pytest.mark.asyncio
async def test_verified_run_passes_and_promotes_memory(env: Env):
    session = await env.session()
    prompt = f"Fix add in calc.py so the tests pass\n{FIX}"

    status = await env.coordinator.run_verified(
        session, prompt, publish=env.events.append
    )

    assert status == "VERIFIED"
    task_id = task_id_of(env)
    assert env.tasks.get(task_id).status == "VERIFIED"
    assert env.tasks.get(task_id).session_id == session.session_id
    for kind in (
        "fcc_intent",
        "fcc_checkpoint",
        "fcc_verification_started",
        "fcc_verification_check",
        "fcc_verification",
    ):
        assert kind in env.types(), kind
    assert all(e["task_id"] == task_id for e in env.events)
    [verdict] = env.of("fcc_verification")
    assert verdict["disposition"] == "VERIFIED" and verdict["attempt"] == 1
    assert obj(verdict["evidence"])["changed_files"] == ["calc.py"]
    checks = {
        obj(c["check"])["id"]: obj(c["check"]) for c in env.of("fcc_verification_check")
    }
    assert checks["unit_test"]["status"] == "passed"
    assert isinstance(checks["unit_test"]["duration_ms"], int)
    statuses = [e["status"] for e in env.of("fcc_task")]
    assert statuses[:5] == [
        "RECEIVED",
        "INTENT_COMPILED",
        "REQUIREMENTS_LOCKED",
        "RUNNING",
        "VERIFYING",
    ]
    assert statuses[-1] == "VERIFIED"
    [audit] = env.audit.recent(action="verification.completed")
    assert audit.decision == "VERIFIED" and audit.resource == task_id
    assert env.memory.search(str(env.repo), "fix add calc tests")
    assert "VERIFIED" in env.tasks.export(task_id, "markdown")


@pytest.mark.asyncio
async def test_failing_test_triggers_recovery_then_verifies(env: Env):
    session = await env.session()
    prompt = f"Fix add in calc.py so the tests pass\n{WRONG}\nRETRY1 {FIX}"

    status = await env.coordinator.run_verified(
        session, prompt, publish=env.events.append
    )

    assert status == "VERIFIED"
    [recovery] = env.of("fcc_recovery")
    assert recovery["action"] == "retry" and recovery["attempt"] == 1
    dispositions = [e["disposition"] for e in env.of("fcc_verification")]
    assert dispositions == ["FAILED_VERIFICATION", "VERIFIED"]
    assert "RECOVERING" in [e["status"] for e in env.of("fcc_task")]
    assert len(env.tasks.evidence(task_id_of(env))) == 2


@pytest.mark.asyncio
async def test_unfixable_failure_stops_at_recovery_bounds(env: Env):
    session = await env.session()
    prompt = f"Fix add in calc.py so the tests pass\n{WRONG}"

    status = await env.coordinator.run_verified(
        session, prompt, publish=env.events.append
    )

    assert status == "RECOVERY_REQUIRED"
    actions = [e["action"] for e in env.of("fcc_recovery")]
    assert actions == ["retry", "retry", "retry", "give_up"]
    assert env.tasks.get(task_id_of(env)).status == "RECOVERY_REQUIRED"
    assert len(env.of("fcc_verification")) == 4


@pytest.mark.asyncio
async def test_blocked_intent_waits_for_clarification(env: Env):
    session = await env.session()
    task_id = env.coordinator.start(session, f"make it better\n{FIX}", mode="verified")

    await wait_status(env, task_id, "BLOCKED_FOR_CLARIFICATION")
    with pytest.raises(InvalidRequestError):
        await env.coordinator.clarify(task_id, "   ")
    with pytest.raises(InvalidRequestError):
        env.coordinator.start(session, "another", mode="verified")  # one run per chat
    await env.coordinator.clarify(task_id, "add() must return the sum")

    assert await env.coordinator.wait(task_id) == "VERIFIED"
    kinds = [e.type for e in env.tasks.events(task_id)]
    assert "intent.clarified" in kinds
    with pytest.raises(InvalidRequestError):
        await env.coordinator.clarify(task_id, "too late")


@pytest.mark.asyncio
async def test_revert_restores_only_task_files(env: Env):
    (env.repo / "notes.txt").write_text(
        "user work\n", "utf-8"
    )  # pre-existing, untracked
    session = await env.session()
    prompt = f"Fix add in calc.py so the tests pass\n{FIX}\nWRITE extra.py x = 1"

    assert (
        await env.coordinator.run_verified(session, prompt, publish=env.events.append)
        == "VERIFIED"
    )
    reverted = await env.coordinator.revert(task_id_of(env))

    assert sorted(reverted) == ["calc.py", "extra.py"]
    assert (env.repo / "calc.py").read_text("utf-8") == BUGGY
    assert not (env.repo / "extra.py").exists()
    assert (env.repo / "notes.txt").read_text("utf-8") == "user work\n"
    [audit] = env.audit.recent(action="task.revert")
    assert audit.outcome == "reverted"


@pytest.mark.asyncio
async def test_revert_without_git_checkpoint_is_rejected(env: Env, tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    session = await env.sessions.start(cwd=str(plain), permission_mode="acceptEdits")
    status = await env.coordinator.run_verified(
        session, f"Fix add\n{FIX}", publish=env.events.append
    )
    assert status in ("RECOVERY_REQUIRED", "FAILED")
    [checkpoint] = env.of("fcc_checkpoint")
    assert checkpoint["supported"] is False
    with pytest.raises(InvalidRequestError, match="no git checkpoint"):
        await env.coordinator.revert(task_id_of(env))


@pytest.mark.asyncio
async def test_budget_interrupt_fails_the_task(env: Env):
    session = await env.session(budget={"max_output_tokens": 20})
    status = await env.coordinator.run_verified(
        session, "Fix add in calc.py\nUSAGE 50\nHANG", publish=env.events.append
    )
    seen: list[JsonObject] = []
    replay = session.subscribe()
    async for event in replay:
        if event["type"] == "fcc_replay_end":
            break
        seen.append(event)
    await replay.aclose()

    assert status == "FAILED"
    budget = [e for e in seen if e["type"] == "fcc_budget"]
    assert budget and budget[0]["kind"] == "tokens" and budget[0]["used"] == 50
    reason = env.of("fcc_task")[-1].get("reason")
    assert isinstance(reason, str) and "budget" in reason


@pytest.mark.asyncio
async def test_cancel_interrupts_a_running_task(env: Env):
    session = await env.session()
    task_id = env.coordinator.start(
        session, "Fix add in calc.py\nHANG", mode="verified"
    )
    await wait_status(env, task_id, "RUNNING")

    assert await env.coordinator.cancel(task_id) is True
    assert env.tasks.get(task_id).status == "CANCELLED"
    assert not env.coordinator.is_running(task_id)


@pytest.mark.asyncio
async def test_start_validates_mode_strategy_and_prompt(env: Env):
    session = await env.session()
    for kwargs in (
        {"mode": "yolo"},
        {"mode": "parallel", "strategy": "reckless"},
    ):
        with pytest.raises(InvalidRequestError):
            env.coordinator.start(session, "x", **kwargs)
    with pytest.raises(InvalidRequestError):
        env.coordinator.start(session, "  ", mode="verified")


@pytest.mark.asyncio
async def test_parallel_mode_integrates_and_verifies(env: Env):
    session = await env.session()

    status = await env.coordinator.run_parallel(
        session,
        f"Fix add in calc.py so the tests pass\n{FIX}",
        strategy="economy",
        publish=env.events.append,
    )

    assert status == "VERIFIED"
    orchestration = [obj(e["event"])["type"] for e in env.of("fcc_orchestration")]
    assert orchestration[0] == "orchestration_started"
    assert "integration_completed" in orchestration
    assert orchestration[-1] == "orchestration_completed"
    assert "return a + b" in (env.repo / "calc.py").read_text("utf-8")
    assert env.of("fcc_verification")[0]["disposition"] == "VERIFIED"


@pytest.mark.asyncio
async def test_parallel_mode_refuses_a_dirty_repo(env: Env):
    (env.repo / "calc.py").write_text("dirty = True\n", "utf-8")
    session = await env.session()

    status = await env.coordinator.run_parallel(
        session, f"Fix add\n{FIX}", publish=env.events.append
    )

    assert status == "FAILED"
    last = env.of("fcc_task")[-1]
    assert last["status"] == "FAILED" and "calc.py" in str(last.get("reason"))
