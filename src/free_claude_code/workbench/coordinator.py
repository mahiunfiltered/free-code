"""Verified and parallel task runs on a live Claude Code chat session.

    coordinator = TaskCoordinator(tasks=TaskStore(store), audit=AuditLog(store), ...)
    task_id = coordinator.start(session, prompt, mode="verified")      # background run
    status = await coordinator.run_verified(session, prompt)           # or await it directly
    await coordinator.clarify(task_id, "answers")  /  await coordinator.cancel(task_id)
    coordinator.resume(session, task_id)          # RECOVERY_REQUIRED: one more attempt
    await coordinator.revert(task_id) -> reverted paths

Verified: compile intent (block for clarification) -> checkpoint -> send the prompt with the
rendered contract -> wait for Claude's ``result`` -> verification gate -> VERIFIED, or bounded
recovery prompts (``recovery.decide``) and re-verification until a final disposition.
Parallel: plan a task graph -> ``Orchestrator`` in the session's cwd -> gate on the
integrated result (no automated recovery; a failure ends in RECOVERY_REQUIRED). A
protected base branch (main/master/release/*) is never merged into: the run integrates
on ``fcc/<task_id>``, created from it, and revert resets that branch.
Ultra: the lead agent (fast model) routes the request -> ``direct`` (sent to the chat
as is) or ``orchestrate`` (plan with per-agent instructions -> checkpoint -> short
countdown -> sub-agents on a snapshot of the tree (git, dirty ok) or in place (no git)
-> diffs applied to the working tree -> the chat writes the final answer from the
sub-agent reports) -> COMPLETED, or the verification gate when ``verify`` is set.

Progress is published on the session stream as ``fcc_task`` / ``fcc_intent`` /
``fcc_checkpoint`` / ``fcc_verification*`` / ``fcc_recovery`` / ``fcc_orchestration`` /
``fcc_ultra`` events, plus ``fcc_node_permission`` / ``fcc_permission_resolved`` for
node permission prompts (docs/m0005-port/CONTRACTS.md). Only
``TaskStore.record_evidence`` sets VERIFIED.
"""

import asyncio
import contextlib
import time
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from loguru import logger

from free_claude_code.application.errors import InvalidRequestError
from free_claude_code.cli.managed.interactive import (
    ChatSessionError,
    InteractiveClaudeSession,
    InteractiveClaudeSessions,
)
from free_claude_code.core.json_types import JsonObject, JsonValue
from free_claude_code.workbench.audit import AuditLog
from free_claude_code.workbench.checkpoints import (
    Checkpoint,
    create_checkpoint,
    project_status,
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
from free_claude_code.workbench.orchestration.planner import (
    analyze,
    fallback_plan,
    plan,
)
from free_claude_code.workbench.orchestration.resources import memory_parallel_cap
from free_claude_code.workbench.orchestration.runner import (
    PARALLELISM,
    Orchestrator,
    OrchestratorOptions,
)
from free_claude_code.workbench.orchestration.worktrees import (
    BRANCH_PREFIX,
    GitError,
    reset_working_branch,
)
from free_claude_code.workbench.recovery import build_recovery_prompt, decide
from free_claude_code.workbench.tasks import TERMINAL, TaskEvent, TaskStatus, TaskStore
from free_claude_code.workbench.ultra import synthesis_prompt
from free_claude_code.workbench.verification.checks import CheckResult
from free_claude_code.workbench.verification.gate import EvidencePackage, run_gate
from free_claude_code.workbench.verification.profile import (
    Level,
    ProjectProfile,
    detect_profile,
    plan_checks,
)

type Mode = Literal["verified", "parallel", "ultra"]
type Publish = Callable[[JsonObject], None]
type ModelClientFactory = Callable[[str | None], ModelClient | None]
type AnalysisClientFactory = Callable[[], ModelClient | None]

MODES: tuple[Mode, ...] = ("verified", "parallel", "ultra")
MAX_ULTRA_PARALLEL = 6
_OUTPUT_TAIL = 4_000
_INTERRUPT_TIMEOUT_S = 10.0
NOT_GIT_WARNING = (
    "not a git repository: changes can't be checkpointed, reverted or "
    "scope-checked; result will be NEEDS_REVIEW at best"
)


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
    # Per parallel node (--max-turns); a smaller chat turn budget lowers it.
    node_max_turns: int = 30
    # A node waiting this long on an unanswered permission prompt fails
    # ("timed out waiting for approval of <tool>"); None: the node wall-time budget.
    node_permission_timeout_s: float | None = None
    # Ultra: concurrent sub-agents when the request does not say (1..6), the lead
    # agent's analysis budget (then: run directly), and the plan card's countdown.
    ultra_max_parallel: int = 4
    # A routing-only reply takes ~5 s on NIM, a full 3-agent plan 20-30 s (measured).
    ultra_analysis_timeout_s: float = 45.0
    ultra_dispatch_delay_s: float = 3.0


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
        analysis_client_factory: AnalysisClientFactory | None = None,
        options: CoordinatorOptions | None = None,
    ) -> None:
        self.tasks = tasks
        self.audit = audit
        self.memory = memory
        self.sessions = sessions
        self.options = options or CoordinatorOptions()
        self._model_client_factory = model_client_factory
        self._analysis_client_factory = analysis_client_factory
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
        images: Sequence[JsonObject] = (),
        verify: bool = False,
        max_parallel: int | None = None,
    ) -> str:
        """Create a task and run it in the background; returns its id.

        ``images`` (Anthropic image blocks) go with the first prompt: the chat
        message in Verified mode, every node prompt in Parallel/Ultra mode.
        ``verify`` and ``max_parallel`` (1..6) apply to Ultra mode only.
        """

        if mode not in MODES:
            raise InvalidRequestError(f"Unknown mode '{mode}'.")
        if mode == "parallel" and strategy not in PARALLELISM:
            raise InvalidRequestError(f"Unknown strategy '{strategy}'.")
        if max_parallel is not None and not 1 <= max_parallel <= MAX_ULTRA_PARALLEL:
            raise InvalidRequestError(
                f"max_parallel must be between 1 and {MAX_ULTRA_PARALLEL}."
            )
        if not prompt.strip():
            raise InvalidRequestError("A task needs a text prompt.")
        self._require_idle(session)
        task_mode = next(m for m in MODES if m == mode)  # narrows str -> Mode
        task_id = self.create_task(session, task_mode, strategy)
        if mode == "verified":
            coro = self.run_verified(session, prompt, task_id=task_id, images=images)
        elif mode == "ultra":
            coro = self.run_ultra(
                session,
                prompt,
                task_id=task_id,
                images=images,
                verify=verify,
                max_parallel=max_parallel,
            )
        else:
            coro = self.run_parallel(
                session, prompt, strategy=strategy, task_id=task_id, images=images
            )
        self._launch(session, task_id, coro)
        return task_id

    def resume(self, session: InteractiveClaudeSession, task_id: str) -> None:
        """Give a RECOVERY_REQUIRED task one more attempt in the background."""

        record = self.tasks.get(task_id)
        if task_id in self._runs:
            raise InvalidRequestError("This task is already running.")
        if record.status != "RECOVERY_REQUIRED":
            raise InvalidRequestError(
                f"Only a task that needs your attention can be resumed ({record.status})."
            )
        if record.contract is None or record.checkpoint is None:
            raise InvalidRequestError("This task has no locked contract to resume.")
        if Path(session.cwd).resolve() != Path(record.cwd).resolve():
            raise InvalidRequestError("Open this task's chat folder to resume it.")
        self._require_idle(session)
        self._launch(session, task_id, self.run_resume(session, task_id))

    def _require_idle(self, session: InteractiveClaudeSession) -> None:
        if session.exited:
            raise ChatSessionError("Session has ended; start it again to continue.")
        if session.busy or session.live_id in self._run_by_live:
            raise InvalidRequestError(
                "Claude is still working in this chat; wait or stop it first."
            )

    def _launch(
        self,
        session: InteractiveClaudeSession,
        task_id: str,
        coro: Coroutine[object, object, TaskStatus],
    ) -> None:
        task = asyncio.create_task(coro)
        self._runs[task_id] = task
        self._run_by_live[session.live_id] = task_id
        task.add_done_callback(lambda t: self._forget(task_id, session.live_id, t))

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
        """Restore the files this task changed to their checkpoint content.

        A parallel run that integrated on its own working branch first resets that
        branch to the base commit, removing the merge commits.
        """

        if task_id in self._runs:
            raise InvalidRequestError("Cancel the running task before reverting it.")
        record = self.tasks.get(task_id)
        checkpoint = record.checkpoint
        if checkpoint is None or not checkpoint.supported:
            raise InvalidRequestError("This task has no git checkpoint to revert to.")
        files: list[str] = []
        if (branch := _working_branch(self.tasks.events(task_id))) is not None:
            name, base_commit = branch
            try:
                files = await asyncio.to_thread(
                    reset_working_branch, Path(record.cwd), name, base_commit
                )
            except GitError as exc:
                raise InvalidRequestError(str(exc)) from exc
            self.tasks.add_event(
                task_id, "task.branch_reset", {"branch": name, "to": base_commit}
            )
        restored = await asyncio.to_thread(revert_task, checkpoint)
        files += [f for f in restored if f not in files]
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
        images: Sequence[JsonObject] = (),
    ) -> TaskStatus:
        """Drive one verified task to its final status (returned, and on the stream)."""

        run = self._new_run(session, "verified", task_id, publish)
        try:
            contract = await self._lock_intent(run, prompt)
            checkpoint = await self._checkpoint(run)
            # The chat process is already running, so per-task memory cannot go
            # through --append-system-prompt; it rides in the message, delimited.
            memory = self._memory_block(session.cwd, prompt)
            message = "\n\n".join(
                part
                for part in (
                    prompt,
                    render_contract(contract),
                    memory
                    and f"<verified_solution_memory>\n{memory}\n</verified_solution_memory>",
                )
                if part
            )
            self._transition(run, "RUNNING")
            await self._turn(run, message, images)
            history: list[str] = []
            attempt = 0
            while True:
                attempt += 1
                package, status = await self._verify(run, contract, checkpoint, attempt)
                if status == "VERIFIED":
                    self._remember(run, contract, package)
                    return status
                decision = decide(package, history)
                self._emit_recovery(
                    run, attempt, decision.action, decision.strategy, decision.reason
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

    async def run_resume(
        self,
        session: InteractiveClaudeSession,
        task_id: str,
        *,
        publish: Publish | None = None,
    ) -> TaskStatus:
        """One more attempt for a RECOVERY_REQUIRED task, asked for by the user.

        Re-runs the gate (the user may have fixed things), and if that still fails,
        sends one recovery prompt and verifies again. Ends VERIFIED or RECOVERY_REQUIRED.
        """

        record = self.tasks.get(task_id)
        mode = _task_mode(self.tasks.events(task_id))
        run = self._new_run(session, mode, task_id, publish)
        try:
            contract, checkpoint = record.contract, record.checkpoint
            if contract is None or checkpoint is None:
                raise TaskRunError("This task has no locked contract to resume.")
            attempt = len(self.tasks.evidence(task_id))
            self.tasks.add_event(task_id, "task.resumed", {"attempt": attempt + 1})
            self._transition(run, "RECOVERING", "resumed by user")
            attempt += 1
            package, status = await self._verify(run, contract, checkpoint, attempt)
            if status != "VERIFIED":
                decision = decide(package, [])
                strategy = (
                    decision.strategy
                    if decision.action == "retry"
                    else "alternative_approach"
                )
                self._emit_recovery(run, attempt, "retry", strategy, decision.reason)
                if status == "FAILED_VERIFICATION":
                    self._transition(run, "RECOVERING", decision.reason)
                self._transition(run, "RUNNING", strategy)
                await self._turn(
                    run,
                    build_recovery_prompt(contract, package, attempt, False, strategy),
                )
                attempt += 1
                package, status = await self._verify(run, contract, checkpoint, attempt)
            if status == "VERIFIED":
                self._remember(run, contract, package)
                return status
            reason = "; ".join(package.blocking_reasons[:3]) or "needs review"
            self._emit_recovery(run, attempt, "ask_user", "ask_user", reason)
            if status == "FAILED_VERIFICATION":
                self._transition(run, "RECOVERY_REQUIRED", reason)
            return "RECOVERY_REQUIRED"
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
        images: Sequence[JsonObject] = (),
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
            chat_turns = session.budget.max_turns if session.budget else None
            orchestrator = Orchestrator(
                graph,
                session.cwd,
                self.sessions,
                OrchestratorOptions(
                    permission_mode=self.options.node_permission_mode,
                    model=session.model,
                    max_parallel=await asyncio.to_thread(
                        memory_parallel_cap, PARALLELISM[strategy]
                    ),
                    contract_block=block,
                    policy_preset=session.policy_preset,
                    max_turns=min(
                        self.options.node_max_turns,
                        chat_turns or self.options.node_max_turns,
                    ),
                    # Node sessions start fresh, so memory is real system prompt here.
                    extra_system_prompt=self._memory_block(session.cwd, prompt) or None,
                    images=tuple(images),
                    working_branch=f"{BRANCH_PREFIX}/{run.task_id}",
                    permission_timeout_s=self.options.node_permission_timeout_s,
                ),
            )
            last = await self._orchestrate(run, orchestrator)
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
            self._emit_recovery(
                run,
                1,
                "ask_user" if decision.action == "retry" else decision.action,
                decision.strategy,
                decision.reason,
            )
            if status == "FAILED_VERIFICATION":
                self._transition(run, "RECOVERY_REQUIRED", decision.reason)
            return "RECOVERY_REQUIRED"
        except asyncio.CancelledError:
            await self._cancelled(run)
            raise
        except Exception as exc:
            return self._failed(run, exc)

    async def _orchestrate(self, run: _Run, orchestrator: Orchestrator) -> JsonObject:
        """Relay an orchestrator's events to the chat; returns its last event."""

        self._orchestrators[run.task_id] = orchestrator
        last: JsonObject = {}
        try:
            async for event in orchestrator.run():
                kind = event.get("type")
                # Node permission prompts go to the chat so the user can answer
                # them (POST .../live/<node live_id>/permissions/<request_id>).
                if kind == "node_permission":
                    self._emit(run, {**event, "type": "fcc_node_permission"})
                elif kind == "node_permission_resolved":
                    self._emit(run, {**event, "type": "fcc_permission_resolved"})
                else:
                    if kind == "integration_started" and run.mode == "ultra":
                        self._ultra(run, "integrating")
                    self._emit(
                        run,
                        {
                            "type": "fcc_orchestration",
                            "event": event,
                            "ts": time.time(),
                        },
                    )
                    last = event
        finally:
            self._orchestrators.pop(run.task_id, None)
            if orchestrator.working_branch:
                self.tasks.add_event(
                    run.task_id,
                    "parallel.working_branch",
                    {
                        "branch": orchestrator.working_branch,
                        "base_commit": orchestrator.base_commit,
                    },
                )
        return last

    # ----- ultra ------------------------------------------------------------

    async def run_ultra(
        self,
        session: InteractiveClaudeSession,
        prompt: str,
        *,
        task_id: str | None = None,
        publish: Publish | None = None,
        images: Sequence[JsonObject] = (),
        verify: bool = False,
        max_parallel: int | None = None,
    ) -> TaskStatus:
        """Lead agent analysis -> direct turn, or sub-agents + synthesis (+ gate)."""

        run = self._new_run(session, "ultra", task_id, publish)
        started = time.monotonic()
        try:
            self._ultra(run, "analyzing", request=prompt[:4_000])
            model = self._analysis_client()
            analysis = await analyze(
                prompt,
                model,
                task_id=run.task_id[:12],
                timeout_s=self.options.ultra_analysis_timeout_s,
            )
            analysis_ms = _ms(started)
            sessions = self.sessions  # no node sessions: everything runs directly
            graph = analysis.graph if sessions is not None else None
            route = "orchestrate" if graph is not None else "direct"
            self.tasks.add_event(
                run.task_id,
                "ultra.analysis",
                {
                    "route": route,
                    "reason": analysis.reason,
                    "degraded": analysis.degraded,
                    "analysis_ms": analysis_ms,
                    "nodes": len(graph.nodes) if graph else 0,
                },
            )
            if graph is None or sessions is None:
                self._ultra(
                    run,
                    "direct",
                    route="direct",
                    reason=analysis.reason,
                    degraded=analysis.degraded,
                    analysis_ms=analysis_ms,
                )
                if verify:
                    status = await self.run_verified(
                        session,
                        prompt,
                        task_id=run.task_id,
                        publish=run.publish,
                        images=images,
                    )
                else:
                    self._transition(run, "RUNNING", "direct")
                    await self._chat_turn(run, prompt, images)
                    self._transition(run, "COMPLETED")
                    status = "COMPLETED"
                self._ultra(run, "done", status=status, elapsed_ms=_ms(started))
                return status

            compiled = await compile_intent(prompt)  # deterministic: no model call
            contract = compiled.contract
            self.tasks.attach(run.task_id, contract=contract)
            self._transition(run, "INTENT_COMPILED")
            self._transition(run, "REQUIREMENTS_LOCKED")
            checkpoint = await self._checkpoint(run)
            parallel = max_parallel or self.options.ultra_max_parallel
            # Each node is a ~400 MB Claude Code process: fit them in free memory.
            effective = await asyncio.to_thread(memory_parallel_cap, parallel)
            workspace = "snapshot" if checkpoint.supported else "in_place"
            delay = self.options.ultra_dispatch_delay_s
            self._ultra(
                run,
                "planned",
                route="orchestrate",
                reason=analysis.reason,
                analysis_ms=analysis_ms,
                plan=graph.to_json(),
                max_parallel=parallel,
                effective_parallel=effective,
                parallel_limited_by="memory" if effective < parallel else None,
                workspace=workspace,
                dispatch_in_s=delay,
            )
            await asyncio.sleep(delay)  # the plan card's countdown; Stop cancels
            self._transition(run, "RUNNING")
            self._ultra(run, "dispatching")
            chat_turns = session.budget.max_turns if session.budget else None
            orchestrator = Orchestrator(
                graph,
                checkpoint.root or session.cwd,
                sessions,
                OrchestratorOptions(
                    permission_mode=self.options.node_permission_mode,
                    model=session.model,
                    max_parallel=effective,
                    contract_block=render_contract(contract),
                    policy_preset=session.policy_preset,
                    max_turns=min(
                        self.options.node_max_turns,
                        chat_turns or self.options.node_max_turns,
                    ),
                    extra_system_prompt=self._memory_block(session.cwd, prompt) or None,
                    images=tuple(images),
                    permission_timeout_s=self.options.node_permission_timeout_s,
                    workspace=workspace,
                    base_snapshot=checkpoint.commit,
                ),
            )
            last = await self._orchestrate(run, orchestrator)
            if last.get("type") == "orchestration_cancelled":
                self._end(run, "CANCELLED", "cancelled by user")
                self._ultra(run, "cancelled", elapsed_ms=_ms(started))
                return "CANCELLED"
            if last.get("type") != "orchestration_completed":
                raise TaskRunError(str(last.get("error") or "orchestration failed"))
            kept = last.get("kept_worktrees")
            self._ultra(run, "summarizing")
            await self._chat_turn(
                run,
                synthesis_prompt(
                    prompt,
                    graph,
                    orchestrator.integration,
                    [str(p) for p in kept] if isinstance(kept, list) else [],
                    task_id=run.task_id,
                    workspace=workspace,
                    ignored=sorted(orchestrator.ignored_files),
                ),
            )
            if verify:
                self._ultra(run, "verifying")
                package, status = await self._verify(run, contract, checkpoint, 1)
                if status == "VERIFIED":
                    self._remember(run, contract, package)
                else:
                    decision = decide(package, [])
                    self._emit_recovery(
                        run, 1, "ask_user", decision.strategy, decision.reason
                    )
                    if status == "FAILED_VERIFICATION":
                        self._transition(run, "RECOVERY_REQUIRED", decision.reason)
                    status = "RECOVERY_REQUIRED"
            else:
                self._transition(run, "COMPLETED")
                status = "COMPLETED"
            self._ultra(run, "done", status=status, elapsed_ms=_ms(started))
            return status
        except asyncio.CancelledError:
            await self._cancelled(run)
            self._ultra(run, "cancelled", elapsed_ms=_ms(started))
            raise
        except Exception as exc:
            status = self._failed(run, exc)
            self._ultra(run, "failed", error=str(exc)[:500], elapsed_ms=_ms(started))
            return status

    async def _chat_turn(
        self, run: _Run, text: str, images: Sequence[JsonObject] = ()
    ) -> None:
        """One chat turn that must end in a successful ``result``."""

        result = await self._turn(run, text, images)
        if result.get("subtype") != "success" or result.get("is_error") is True:
            detail = result.get("result")
            raise TaskRunError(
                f"Claude did not finish: {detail}"
                if isinstance(detail, str) and detail
                else f"Claude did not finish ({result.get('subtype')})."
            )

    def _analysis_client(self) -> ModelClient | None:
        """The lead agent's model: the dedicated (reasoning-off) analysis client, else
        the FCC default model (``None``) from the general factory."""

        if self._analysis_client_factory is not None:
            return self._analysis_client_factory()
        if self._model_client_factory is not None:
            return self._model_client_factory(None)
        return None

    def _ultra(self, run: _Run, phase: str, **fields: JsonValue) -> None:
        self._emit(
            run, {"type": "fcc_ultra", "phase": phase, "ts": time.time(), **fields}
        )

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
        if run.mode == "verified":
            status = await asyncio.to_thread(project_status, run.session.cwd)
            if not status["git"]:
                compiled.warnings.append(NOT_GIT_WARNING)
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

    async def _turn(
        self, run: _Run, text: str, images: Sequence[JsonObject] = ()
    ) -> JsonObject:
        """Send one prompt (plus image blocks) and wait for its ``result`` event."""

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
            await session.send_user_message(
                [*images, {"type": "text", "text": text}] if images else text
            )
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

    def _emit_recovery(
        self, run: _Run, attempt: int, action: str, strategy: str, reason: str
    ) -> None:
        self._emit(
            run,
            {
                "type": "fcc_recovery",
                "attempt": attempt,
                "action": action,
                "strategy": strategy,
                "reason": reason,
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


def _task_mode(events: Sequence[TaskEvent]) -> Mode:
    for event in events:
        if event.type == "task.mode":
            for mode in MODES:
                if event.payload.get("mode") == mode:
                    return mode
    return "verified"


def _ms(since: float) -> int:
    return round((time.monotonic() - since) * 1000)


def _working_branch(events: Sequence[TaskEvent]) -> tuple[str, str] | None:
    """``(branch, base_commit)`` of a parallel run that integrated on its own branch."""

    for event in events:
        if event.type == "parallel.working_branch":
            branch, base = event.payload.get("branch"), event.payload.get("base_commit")
            if isinstance(branch, str) and isinstance(base, str):
                return branch, base
    return None


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
