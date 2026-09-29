"""Verified and parallel task runs on a live Claude Code chat session.

    coordinator = TaskCoordinator(tasks=TaskStore(store), audit=AuditLog(store), ...)
    task_id = coordinator.start(session, prompt, mode="verified")      # background run
    status = await coordinator.run_verified(session, prompt)           # or await it directly
    await coordinator.clarify(task_id, "answers")  /  await coordinator.cancel(task_id)
    await coordinator.revert(task_id) -> reverted paths

Verified: compile intent (block for clarification) -> checkpoint -> send the prompt with the
rendered contract -> wait for Claude's ``result`` -> verification gate -> VERIFIED, or bounded
recovery prompts (``recovery.decide``) and re-verification until a final disposition.
Parallel: plan a task graph -> ``Orchestrator`` in the session's cwd -> gate on the
integrated result (no automated recovery; a failure ends in RECOVERY_REQUIRED).

Progress is published on the session stream as ``fcc_task`` / ``fcc_intent`` /
``fcc_checkpoint`` / ``fcc_verification*`` / ``fcc_recovery`` / ``fcc_orchestration``
events (docs/m0005-port/CONTRACTS.md). Only ``TaskStore.record_evidence`` sets VERIFIED.
"""

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from loguru import logger

from free_claude_code.application.errors import InvalidRequestError
from free_claude_code.cli.managed.interactive import (
    ChatSessionError,
    InteractiveClaudeSession,
    InteractiveClaudeSessions,
)
from free_claude_code.core.json_types import JsonObject
from free_claude_code.workbench.audit import AuditLog
from free_claude_code.workbench.checkpoints import (
    Checkpoint,
    create_checkpoint,
    revert_task,
)
from free_claude_code.workbench.intent import (
    CompiledIntent,
    IntentContract,
    ModelClient,
    compile_intent,
    render_contract,
)
from free_claude_code.workbench.memory import (
    MemoryRejectedError,
    SolutionMemory,
    hash_files,
    render_for_prompt,
)
from free_claude_code.workbench.orchestration.planner import fallback_plan, plan
from free_claude_code.workbench.orchestration.runner import (
    PARALLELISM,
    Orchestrator,
    OrchestratorOptions,
)
from free_claude_code.workbench.recovery import build_recovery_prompt, decide
from free_claude_code.workbench.tasks import TERMINAL, TaskStatus, TaskStore
from free_claude_code.workbench.verification.checks import CheckResult
from free_claude_code.workbench.verification.gate import EvidencePackage, run_gate
from free_claude_code.workbench.verification.profile import (
    Level,
    ProjectProfile,
    detect_profile,
    plan_checks,
)

type Mode = Literal["verified", "parallel"]
type Publish = Callable[[JsonObject], None]
type ModelClientFactory = Callable[[str | None], ModelClient | None]

MODES: tuple[Mode, ...] = ("verified", "parallel")
_OUTPUT_TAIL = 4_000
_INTERRUPT_TIMEOUT_S = 10.0


class TaskRunError(RuntimeError):
    """The run cannot continue (session ended, no result, orchestration failed)."""


@dataclass
class CoordinatorOptions:
    level: Level | None = None
    # Override project detection (benchmarks / tests pin their own commands).
    profile: ProjectProfile | None = None
    command_timeout_s: float = 600.0
    result_timeout_s: float = 3600.0
    # The model outcome judge can only fail rows, but a weak model makes that flaky.
    use_judge: bool = False
    node_permission_mode: str = "acceptEdits"


@dataclass
class _Run:
    task_id: str
    mode: Mode
    session: InteractiveClaudeSession
    publish: Publish
    model: ModelClient | None
    summary: str = ""


class TaskCoordinator:
    def __init__(
        self,
        *,
        tasks: TaskStore,
        audit: AuditLog,
        memory: SolutionMemory | None = None,
        sessions: InteractiveClaudeSessions | None = None,
        model_client_factory: ModelClientFactory | None = None,
        options: CoordinatorOptions | None = None,
    ) -> None:
        self.tasks = tasks
        self.audit = audit
        self.memory = memory
        self.sessions = sessions
        self.options = options or CoordinatorOptions()
        self._model_client_factory = model_client_factory
        self._runs: dict[str, asyncio.Task[TaskStatus]] = {}
        self._run_by_live: dict[str, str] = {}
        self._clarify: dict[str, asyncio.Future[str]] = {}
        self._orchestrators: dict[str, Orchestrator] = {}

    # ----- public -----------------------------------------------------------

    def start(
        self,
        session: InteractiveClaudeSession,
        prompt: str,
        *,
        mode: str,
        strategy: str = "balanced",
    ) -> str:
        """Create a task and run it in the background; returns its id."""

        if mode not in MODES:
            raise InvalidRequestError(f"Unknown mode '{mode}'.")
        if mode == "parallel" and strategy not in PARALLELISM:
            raise InvalidRequestError(f"Unknown strategy '{strategy}'.")
        if not prompt.strip():
            raise InvalidRequestError("A task needs a text prompt.")
        if session.exited:
            raise ChatSessionError("Session has ended; start it again to continue.")
        if session.busy or session.live_id in self._run_by_live:
            raise InvalidRequestError(
                "Claude is still working in this chat; wait or stop it first."
            )
        task_id = self.create_task(
            session, "verified" if mode == "verified" else "parallel", strategy
        )
        if mode == "verified":
            coro = self.run_verified(session, prompt, task_id=task_id)
        else:
            coro = self.run_parallel(
                session, prompt, strategy=strategy, task_id=task_id
            )
        task = asyncio.create_task(coro)
        self._runs[task_id] = task
        self._run_by_live[session.live_id] = task_id
        task.add_done_callback(lambda t: self._forget(task_id, session.live_id, t))
        return task_id

    def create_task(
        self,
        session: InteractiveClaudeSession,
        mode: Mode,
        strategy: str = "",
        publish: Publish | None = None,
    ) -> str:
        task_id = self.tasks.create(session.session_id or session.live_id, session.cwd)
        self.tasks.add_event(task_id, "task.mode", {"mode": mode, "strategy": strategy})
        (publish or session.publish)(
            {"type": "fcc_task", "task_id": task_id, "mode": mode, "status": "RECEIVED"}
        )
        return task_id

    def is_running(self, task_id: str) -> bool:
        return task_id in self._runs

    async def wait(self, task_id: str) -> TaskStatus:
        """Wait for a background run to stop; returns the task's status."""

        if (task := self._runs.get(task_id)) is not None:
            await asyncio.wait({task})
        return self.tasks.get(task_id).status

    async def clarify(self, task_id: str, answers: str) -> None:
        future = self._clarify.get(task_id)
        if future is None or future.done():
            raise InvalidRequestError("This task is not waiting for clarification.")
        if not answers.strip():
            raise InvalidRequestError("Clarification answers are empty.")
        future.set_result(answers.strip())

    async def cancel(self, task_id: str) -> bool:
        """Stop a running task, or cancel a stopped non-terminal one; False if unknown."""

        if (orchestrator := self._orchestrators.get(task_id)) is not None:
            await orchestrator.cancel()  # the run observes orchestration_cancelled
            return True
        if (task := self._runs.get(task_id)) is not None:
            task.cancel()
            await asyncio.wait({task})
            return True
        record = self.tasks.get(task_id)
        if record.status not in TERMINAL:
            self.tasks.transition(task_id, "CANCELLED", "cancelled by user")
        return True

    async def revert(self, task_id: str) -> list[str]:
        """Restore the files this task changed to their checkpoint content."""

        if task_id in self._runs:
            raise InvalidRequestError("Cancel the running task before reverting it.")
        checkpoint = self.tasks.get(task_id).checkpoint
        if checkpoint is None or not checkpoint.supported:
            raise InvalidRequestError("This task has no git checkpoint to revert to.")
        files = await asyncio.to_thread(revert_task, checkpoint)
        self.tasks.add_event(task_id, "task.reverted", {"files": list(files)})
        self.audit.append(
            actor="user",
            action="task.revert",
            resource=task_id,
            outcome="reverted",
            payload={"files": list(files[:200])},
        )
        return files

    async def close(self) -> None:
        runs = list(self._runs.values())
        for task in runs:
            task.cancel()
        await asyncio.gather(*runs, return_exceptions=True)

    # ----- verified ---------------------------------------------------------

    async def run_verified(
        self,
        session: InteractiveClaudeSession,
        prompt: str,
        *,
        task_id: str | None = None,
        publish: Publish | None = None,
    ) -> TaskStatus:
        """Drive one verified task to its final status (returned, and on the stream)."""

        run = self._new_run(session, "verified", task_id, publish)
        try:
            contract = await self._lock_intent(run, prompt)
            checkpoint = await self._checkpoint(run)
            message = "\n\n".join(
                part
                for part in (
                    prompt,
                    render_contract(contract),
                    self._memory_block(session.cwd, prompt),
                )
                if part
            )
            self._transition(run, "RUNNING")
            await self._turn(run, message)
            history: list[str] = []
            attempt = 0
            while True:
                attempt += 1
                package, status = await self._verify(run, contract, checkpoint, attempt)
                if status == "VERIFIED":
                    self._remember(run, contract, package)
                    return status
                decision = decide(package, history)
                self._emit(
                    run,
                    {
                        "type": "fcc_recovery",
                        "attempt": attempt,
                        "action": decision.action,
                        "strategy": decision.strategy,
                        "reason": decision.reason,
                    },
                )
                if decision.action != "retry":
                    if status == "FAILED_VERIFICATION":
                        self._transition(run, "RECOVERY_REQUIRED", decision.reason)
                    return "RECOVERY_REQUIRED"
                history.append(decision.fingerprint or "")
                self._transition(run, "RECOVERING", decision.reason)
                self._transition(run, "RUNNING", decision.strategy)
                await self._turn(
                    run,
                    build_recovery_prompt(
                        contract,
                        package,
                        decision.attempt,
                        decision.repeated,
                        decision.strategy,
                    ),
                )
        except asyncio.CancelledError:
            await self._cancelled(run)
            raise
        except Exception as exc:
            return self._failed(run, exc)

    # ----- parallel ---------------------------------------------------------

    async def run_parallel(
        self,
        session: InteractiveClaudeSession,
        prompt: str,
        *,
        strategy: str = "balanced",
        task_id: str | None = None,
        publish: Publish | None = None,
    ) -> TaskStatus:
        """Plan -> orchestrate node sessions -> integrate -> verification gate."""

        run = self._new_run(session, "parallel", task_id, publish)
        try:
            if self.sessions is None:
                raise TaskRunError("Parallel mode is not available.")
            contract = await self._lock_intent(run, prompt)
            checkpoint = await self._checkpoint(run)
            self._transition(run, "RUNNING")
            block = render_contract(contract)
            graph_id = run.task_id[:12]
            graph = (
                await plan(prompt, block, run.model, task_id=graph_id)
                if run.model is not None
                else fallback_plan(prompt, graph_id)
            )
            orchestrator = Orchestrator(
                graph,
                session.cwd,
                self.sessions,
                OrchestratorOptions(
                    permission_mode=self.options.node_permission_mode,
                    model=session.model,
                    max_parallel=PARALLELISM[strategy],
                    contract_block=block,
                ),
            )
            self._orchestrators[run.task_id] = orchestrator
            last: JsonObject = {}
            try:
                async for event in orchestrator.run():
                    self._emit(run, {"type": "fcc_orchestration", "event": event})
                    last = event
            finally:
                self._orchestrators.pop(run.task_id, None)
            if last.get("type") == "orchestration_cancelled":
                self._end(run, "CANCELLED", "cancelled by user")
                return "CANCELLED"
            if last.get("type") != "orchestration_completed":
                raise TaskRunError(str(last.get("error") or "orchestration failed"))
            completed = [
                n for n in graph.nodes if n.status == "completed" and n.summary
            ]
            run.summary = "\n".join(f"{n.id}: {n.summary}" for n in completed)
            package, status = await self._verify(run, contract, checkpoint, 1)
            if status == "VERIFIED":
                self._remember(run, contract, package)
                return status
            decision = decide(package, [])
            self._emit(
                run,
                {
                    "type": "fcc_recovery",
                    "attempt": 1,
                    "action": "ask_user"
                    if decision.action == "retry"
                    else decision.action,
                    "strategy": decision.strategy,
                    "reason": decision.reason,
                },
            )
            if status == "FAILED_VERIFICATION":
                self._transition(run, "RECOVERY_REQUIRED", decision.reason)
            return "RECOVERY_REQUIRED"
        except asyncio.CancelledError:
            await self._cancelled(run)
            raise
        except Exception as exc:
            return self._failed(run, exc)

    # ----- steps ------------------------------------------------------------

    def _new_run(
        self,
        session: InteractiveClaudeSession,
        mode: Mode,
        task_id: str | None,
        publish: Publish | None,
    ) -> _Run:
        model = (
            self._model_client_factory(session.model)
            if self._model_client_factory is not None
            else None
        )
        return _Run(
            task_id=task_id or self.create_task(session, mode, publish=publish),
            mode=mode,
            session=session,
            publish=publish or session.publish,
            model=model,
        )

    async def _lock_intent(self, run: _Run, prompt: str) -> IntentContract:
        compiled = await compile_intent(prompt, run.model)
        self.tasks.attach(run.task_id, contract=compiled.contract)
        self._transition(run, "INTENT_COMPILED")
        self._emit_intent(run, compiled)
        if compiled.status == "blocked_for_clarification":
            self._transition(
                run, "BLOCKED_FOR_CLARIFICATION", "; ".join(compiled.questions)[:500]
            )
            future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
            self._clarify[run.task_id] = future
            try:
                answers = await future
            finally:
                self._clarify.pop(run.task_id, None)
            self.tasks.add_event(
                run.task_id, "intent.clarified", {"answers": answers[:4000]}
            )
            compiled = await compile_intent(
                f"{prompt}\n\nClarification from the user: {answers}",
                run.model,
                authorize_defaults=True,
            )
            if compiled.status == "blocked_for_clarification":
                # The user already answered once; the contract records the open points.
                compiled.warnings.append(
                    "proceeding after clarification; still open: "
                    + "; ".join(compiled.questions)
                )
                compiled.status = "ready_to_lock"
                compiled.questions = []
            self.tasks.attach(run.task_id, contract=compiled.contract)
            self._transition(run, "INTENT_COMPILED", "clarified")
            self._emit_intent(run, compiled)
        self._transition(run, "REQUIREMENTS_LOCKED")
        return compiled.contract

    async def _checkpoint(self, run: _Run) -> Checkpoint:
        checkpoint = await asyncio.to_thread(create_checkpoint, run.session.cwd)
        self.tasks.attach(run.task_id, checkpoint=checkpoint)
        self._emit(
            run,
            {
                "type": "fcc_checkpoint",
                "checkpoint_id": checkpoint.id,
                "supported": checkpoint.supported,
                "head": checkpoint.head,
            },
        )
        return checkpoint

    async def _turn(self, run: _Run, text: str) -> JsonObject:
        """Send one prompt and wait for its ``result`` event."""

        session = run.session
        events = session.subscribe()
        try:
            replayed = False
            async for event in events:  # skip the replay backlog
                if event.get("type") == "fcc_replay_end":
                    replayed = True
                    break
            if not replayed or session.exited:
                raise TaskRunError("The Claude Code session has ended.")
            await session.send_user_message(text)
            async with asyncio.timeout(self.options.result_timeout_s):
                async for event in events:
                    kind = event.get("type")
                    if kind == "result":
                        if session.session_id:
                            self.tasks.rebind_session(run.task_id, session.session_id)
                        result = event.get("result")
                        run.summary = result if isinstance(result, str) else ""
                        return event
                    if kind == "fcc_exit":
                        raise TaskRunError(
                            f"Claude Code exited (code {event.get('code')})."
                        )
            raise TaskRunError("Claude Code ended without a result.")
        finally:
            await events.aclose()

    async def _verify(
        self,
        run: _Run,
        contract: IntentContract,
        checkpoint: Checkpoint,
        attempt: int,
    ) -> tuple[EvidencePackage, TaskStatus]:
        self._transition(run, "VERIFYING")
        cwd = run.session.cwd
        profile = self.options.profile or await asyncio.to_thread(detect_profile, cwd)
        planned = plan_checks(contract, profile, self.options.level)
        self._emit(
            run,
            {
                "type": "fcc_verification_started",
                "attempt": attempt,
                "level": planned.level,
                "checks": [
                    {
                        "id": check.id,
                        "kind": check.kind,
                        "command": list(check.argv) if check.argv else None,
                        "required": check.required,
                    }
                    for check in planned.checks
                ],
            },
        )
        # ponytail: checks are reported when the gate returns, not as each one ends;
        # stream them once run_gate grows a progress callback.
        package = await run_gate(
            contract,
            cwd,
            checkpoint,
            profile=profile,
            level=self.options.level,
            judge=run.model if self.options.use_judge else None,
            command_timeout=self.options.command_timeout_s,
        )
        for check in package.checks:
            self._emit(
                run,
                {
                    "type": "fcc_verification_check",
                    "attempt": attempt,
                    "check": _check_json(check),
                },
            )
        status = self.tasks.record_evidence(run.task_id, package)
        self._emit(
            run,
            {
                "type": "fcc_verification",
                "attempt": attempt,
                "disposition": package.disposition,
                "evidence": package.to_json(),
            },
        )
        self._emit_status(run, status, "; ".join(package.blocking_reasons[:3]))
        self.audit.append(
            actor="workbench",
            action="verification.completed",
            resource=run.task_id,
            decision=package.disposition,
            outcome=status,
            payload={
                "attempt": attempt,
                "mode": run.mode,
                "cwd": cwd,
                "blocking_reasons": list(package.blocking_reasons[:5]),
                "diff_sha256": package.diff_sha256,
            },
        )
        return package, status

    def _memory_block(self, cwd: str, prompt: str) -> str:
        if self.memory is None:
            return ""
        try:
            self.memory.mark_stale(cwd)
            return render_for_prompt(self.memory.search(cwd, prompt))
        except Exception as exc:  # memory is advisory; never block a task on it
            logger.warning("Solution memory lookup failed: {}", exc)
            return ""

    def _remember(
        self, run: _Run, contract: IntentContract, package: EvidencePackage
    ) -> None:
        """Store the verified outcome as a promoted solution pattern."""

        if self.memory is None or not package.changed_files:
            return
        cwd = run.session.cwd
        try:
            self.memory.add_candidate(
                project=cwd,
                title=contract.goal[:200],
                pattern=(run.summary or package.diff_stat)[:2000],
                applicability=contract.goal[:500],
                source_task_id=run.task_id,
                source_hashes=hash_files(cwd, package.changed_files),
            )
            self.memory.promote(run.task_id, verified=True)
        except (MemoryRejectedError, OSError) as exc:
            logger.info("Solution memory skipped: {}", exc)

    # ----- lifecycle helpers ------------------------------------------------

    def _emit(self, run: _Run, event: JsonObject) -> None:
        run.publish({**event, "task_id": run.task_id})

    def _emit_intent(self, run: _Run, compiled: CompiledIntent) -> None:
        self._emit(
            run,
            {
                "type": "fcc_intent",
                "status": compiled.status,
                "contract": compiled.contract.to_json(),
                "questions": list(compiled.questions),
                "warnings": list(compiled.warnings),
            },
        )

    def _emit_status(self, run: _Run, status: str, reason: str = "") -> None:
        event: JsonObject = {"type": "fcc_task", "mode": run.mode, "status": status}
        if reason:
            event["reason"] = reason
        self._emit(run, event)

    def _transition(self, run: _Run, to: TaskStatus, reason: str = "") -> None:
        self.tasks.transition(run.task_id, to, reason)
        self._emit_status(run, to, reason)

    def _end(self, run: _Run, to: TaskStatus, reason: str) -> None:
        if self.tasks.get(run.task_id).status not in TERMINAL:
            self._transition(run, to, reason)

    async def _cancelled(self, run: _Run) -> None:
        session = run.session
        if session.busy and not session.exited:
            with contextlib.suppress(ChatSessionError, TimeoutError):
                await asyncio.wait_for(
                    session.control({"subtype": "interrupt"}), _INTERRUPT_TIMEOUT_S
                )
        self._end(run, "CANCELLED", "cancelled by user")

    def _failed(self, run: _Run, exc: Exception) -> TaskStatus:
        logger.warning("Task {} failed: {}", run.task_id, exc)
        self._end(run, "FAILED", str(exc)[:500] or type(exc).__name__)
        return "FAILED"

    def _forget(
        self, task_id: str, live_id: str, task: asyncio.Task[TaskStatus]
    ) -> None:
        self._runs.pop(task_id, None)
        if self._run_by_live.get(live_id) == task_id:
            self._run_by_live.pop(live_id, None)
        if not task.cancelled() and (exc := task.exception()) is not None:
            logger.error("Task {} crashed: {!r}", task_id, exc)


def _check_json(check: CheckResult) -> JsonObject:
    status: str = check.status
    if status == "needs_review":
        status = "skipped"  # undecided; the evidence package keeps the precise status
    elif status == "failed" and not check.required:
        status = "advisory_failed"
    return {
        "id": check.id,
        "kind": check.kind,
        "status": status,
        "duration_ms": round(check.duration_s * 1000),
        "exit_code": check.exit_code,
        "output_tail": check.output[-_OUTPUT_TAIL:],
    }
