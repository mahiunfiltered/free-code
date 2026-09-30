"""Run a TaskGraph as concurrent Claude Code sessions, then integrate their work.

Workspaces (``OrchestratorOptions.workspace``):
- ``branches`` (Parallel): clean git repo; mutating nodes get a worktree/branch from
  HEAD and are merged into the base branch (a protected base gets a working branch).
- ``snapshot`` (Ultra, git): worktrees start from ``base_snapshot`` (a checkpoint
  commit of the dirty tree) and node diffs are applied to the working tree; the
  user's branch, index and HEAD are never touched.
- ``in_place`` (Ultra, no git): every node runs in the folder itself; write-scope
  leases keep concurrent writers disjoint, and a before/after scan reports files
  changed outside the scopes of the nodes that were running.
Write-scope leases serialize overlapping scopes in every workspace; read-only nodes
run in the main folder in ``plan`` mode. A node whose Claude Code process exits
without a ``result`` (crash, non-zero exit) is retried once in a fresh session
(``node_retry``; a git worktree is reset to the node's start commit first).
Tool-state paths (``workbench.ignore``) are never committed, reverted,
integrated or reported out of scope; they are listed once in
``orchestration_completed.ignored_files``. Progress streams from
:meth:`Orchestrator.run` as JSON events (see ``_TERMINAL`` and the ``type`` values
emitted below).
"""

import asyncio
import contextlib
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from loguru import logger

from free_claude_code.cli.managed.interactive import (
    ChatSessionError,
    InteractiveClaudeSession,
    InteractiveClaudeSessions,
)
from free_claude_code.core.json_types import JsonObject, JsonValue
from free_claude_code.workbench.ignore import (
    DEFAULT_DIFF_IGNORE,
    diff_ignore_patterns,
    split_ignored,
)
from free_claude_code.workbench.policy import is_protected_branch

from .graph import TaskGraph, TaskNode, in_scope
from .integration import (
    FileStamp,
    IntegrationResult,
    apply_to_worktree,
    changed_paths,
    enforce_scope,
    integrate,
    scan_tree,
)
from .leases import Lease, LeaseManager
from .worktrees import (
    BRANCH_PREFIX,
    GitError,
    commit_all,
    create_worktree,
    current_branch,
    exclude_worktree_dir,
    git,
    remove_worktree,
    require_clean_repo,
    uncommitted,
)

# Max concurrent nodes per routing strategy.
PARALLELISM = {"economy": 1, "balanced": 3, "fastest": 6}
_TERMINAL = frozenset({"node_completed", "node_failed", "node_cancelled"})
_SUMMARY_LIMIT = 4_000
_ACTIVITY_ARG = 80
# A crashed node process (no ``result``) gets one fresh session.
_MAX_ATTEMPTS = 2
type Workspace = Literal["branches", "snapshot", "in_place"]


class NodeFailedError(RuntimeError):
    """Claude finished the node without a successful result."""


class NodeCrashedError(ChatSessionError):
    """Claude Code exited before sending a ``result`` (crash or non-zero exit).

    The only node failure that is retried: timeouts, budgets, permission timeouts,
    error results and cancels are final.
    """


@dataclass
class OrchestratorOptions:
    permission_mode: str = "acceptEdits"
    model: str | None = None
    max_parallel: int = PARALLELISM["balanced"]
    node_timeout_s: float = 900.0
    # Enforced per node session (``--max-turns`` plus the chat turn budget).
    max_turns: int = 30
    # Intent contract rendered as text; copied verbatim into every node prompt.
    contract_block: str | None = None
    interrupt_timeout_s: float = 10.0
    # The chat's policy preset; node sessions get its rules and (stricter) mode.
    policy_preset: str | None = None
    # Appended to every node's system prompt (verified solution memory).
    extra_system_prompt: str | None = None
    # Image blocks of the user's message, sent with every node prompt.
    images: tuple[JsonObject, ...] = ()
    # Branch created from a protected base (main/master/release/*) to integrate on.
    working_branch: str | None = None
    # How long a node may wait on an unanswered permission prompt (None: until
    # node_timeout_s). Prompts surface as ``node_permission`` events.
    permission_timeout_s: float | None = None
    workspace: Workspace = "branches"
    # ``snapshot`` workspace: the checkpoint commit node worktrees start from.
    base_snapshot: str | None = None


class Orchestrator:
    def __init__(
        self,
        graph: TaskGraph,
        repo: Path | str,
        sessions: InteractiveClaudeSessions,
        options: OrchestratorOptions | None = None,
    ) -> None:
        self.graph = graph
        self.repo = Path(repo).resolve()
        self.sessions = sessions
        self.options = options or OrchestratorOptions()
        self.integration: IntegrationResult | None = None
        self.base_commit = ""
        # Set while the run integrates on its own branch (protected base); None once
        # dropped because nothing was merged into it.
        self.working_branch: str | None = None
        self._leases = LeaseManager()
        self._held: dict[str, Lease | None] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._live: dict[str, InteractiveClaudeSession] = {}
        # node id -> unanswered can_use_tool request id -> tool name.
        self._waiting: dict[str, dict[str, str]] = {}
        self._queue: asyncio.Queue[JsonObject] = asyncio.Queue()
        self._cancelled = False
        # node id -> [start, end] loop times (end None while running), for
        # attributing in-place changes to the nodes that ran at the same time.
        self._spans: dict[str, list[float | None]] = {}
        # Tool-state globs (defaults + the project's .fcc/verify.json), and the
        # ignored paths nodes changed.
        self._ignore = list(DEFAULT_DIFF_IGNORE)
        self.ignored_files: set[str] = set()

    # ----- public ------------------------------------------------------------

    async def cancel(self) -> None:
        """Interrupt and close every running node; pending nodes are cancelled."""

        if self._cancelled:
            return  # a second cancel would abort the sessions' interrupt/close cleanup
        self._cancelled = True
        for task in list(self._tasks.values()):
            task.cancel()

    async def run(self) -> AsyncGenerator[JsonObject]:
        graph = self.graph
        if errors := graph.validate():
            yield {"type": "orchestration_failed", "error": f"invalid graph: {errors}"}
            return
        workspace = self.options.workspace
        base_branch = ""
        self._ignore, warnings = await asyncio.to_thread(
            diff_ignore_patterns, self.repo
        )
        for warning in warnings:
            logger.warning("Orchestrator: {}", warning)
        try:
            if workspace == "branches":
                self.base_commit = await asyncio.to_thread(
                    require_clean_repo, self.repo
                )
                base_branch = await asyncio.to_thread(current_branch, self.repo)
                if is_protected_branch(base_branch):
                    # Never commit to a protected branch: integrate on a new branch.
                    working = (
                        self.options.working_branch
                        or f"{BRANCH_PREFIX}/{graph.task_id}-work"
                    )
                    await asyncio.to_thread(
                        git, self.repo, "checkout", "-q", "-b", working
                    )
                    self.working_branch = working
            elif workspace == "snapshot":
                if not self.options.base_snapshot:
                    raise GitError("The snapshot workspace needs a base snapshot.")
                await asyncio.to_thread(exclude_worktree_dir, self.repo)
                self.base_commit = self.options.base_snapshot
        except GitError as exc:
            yield {"type": "orchestration_failed", "error": str(exc)}
            return
        yield {
            "type": "orchestration_started",
            "task_id": graph.task_id,
            "workspace": workspace,
            "base_commit": self.base_commit,
            "base_branch": base_branch,
            "working_branch": self.working_branch,
            "max_parallel": self.options.max_parallel,
            "graph": graph.to_json(),
        }
        integration_branch = self.working_branch or base_branch
        try:
            async for event in self._run_nodes():
                yield event
        finally:
            if self._tasks:  # consumer stopped early: never leak sessions
                await self.cancel()
                await asyncio.gather(*self._tasks.values(), return_exceptions=True)

        if self._cancelled:
            for node in graph.nodes:
                if node.status in ("pending", "ready"):
                    node.status = "cancelled"
            await asyncio.to_thread(self._cleanup, keep_failed=False)
            await asyncio.to_thread(self._drop_working_branch, base_branch)
            yield {"type": "orchestration_cancelled", "graph": graph.to_json()}
            return

        if any(n.status == "completed" and n.branch for n in graph.nodes):
            yield {"type": "integration_started", "base_branch": integration_branch}
            try:
                if workspace == "snapshot":
                    self.integration = await asyncio.to_thread(
                        apply_to_worktree, self.repo, graph, self._ignore
                    )
                else:
                    self.integration = await asyncio.to_thread(
                        integrate, self.repo, graph, self.base_commit, self._ignore
                    )
            except GitError as exc:
                yield {"type": "integration_failed", "error": str(exc)}
            else:
                yield {"type": "integration_completed", **self.integration.to_json()}
        kept = await asyncio.to_thread(self._cleanup, keep_failed=True)
        if self.integration is None or not self.integration.merged:
            await asyncio.to_thread(self._drop_working_branch, base_branch)
        clean = all(n.status == "completed" for n in graph.nodes) and (
            self.integration is None or not self.integration.conflicts
        )
        yield {
            "type": "orchestration_completed",
            "status": "ready_for_verification" if clean else "needs_attention",
            "graph": graph.to_json(),
            "integration": self.integration.to_json() if self.integration else None,
            "kept_worktrees": list(kept),
            "working_branch": self.working_branch,
            "ignored_files": sorted(self.ignored_files),
        }

    # ----- scheduling --------------------------------------------------------

    async def _run_nodes(self) -> AsyncGenerator[JsonObject]:
        while True:
            if not self._cancelled:
                for node_id in self.graph.block_failed():
                    yield {"type": "node_blocked", "node_id": node_id}
                self._schedule()
            if not self._tasks:
                return
            event = await self._queue.get()
            if event.get("type") == "_task_done":
                if (terminal := self._reap(str(event["node_id"]))) is not None:
                    yield terminal
                continue
            yield event

    def _schedule(self) -> None:
        slots = self.options.max_parallel - len(self._tasks)
        for node in self.graph.ready():
            if slots <= 0:
                return
            lease = None
            if node.mutating:
                lease = self._leases.try_acquire(
                    self.graph.task_id,
                    node.id,
                    node.write_scope,
                    ttl_s=self.options.node_timeout_s * 2,
                )
                if lease is None:
                    continue  # an overlapping mutating node is running: serialize
            node.status = "running"
            self._held[node.id] = lease
            task = asyncio.create_task(self._run_node(node))
            task.add_done_callback(
                lambda _t, nid=node.id: self._queue.put_nowait(
                    {"type": "_task_done", "node_id": nid}
                )
            )
            self._tasks[node.id] = task
            slots -= 1

    def _reap(self, node_id: str) -> JsonObject | None:
        """Forget a finished task; report nodes that died without a terminal event."""

        task = self._tasks.pop(node_id)
        if lease := self._held.pop(node_id, None):
            self._leases.release(lease)
        node = self.graph.node(node_id)
        if node.status != "running":
            return None
        if task.cancelled():
            node.status = "cancelled"
            return {"type": "node_cancelled", "node_id": node_id}
        node.status = "failed"
        node.error = f"internal error: {task.exception()!r}"
        return {"type": "node_failed", "node_id": node_id, "error": node.error}

    # ----- one node ----------------------------------------------------------

    def _emit(self, event: JsonObject) -> None:
        self._queue.put_nowait(event)

    async def _run_node(self, node: TaskNode) -> None:
        session: InteractiveClaudeSession | None = None
        loop = asyncio.get_running_loop()
        span: list[float | None] = [loop.time(), None]
        self._spans[node.id] = span
        in_place = self.options.workspace == "in_place" and node.mutating
        try:
            async with asyncio.timeout(self.options.node_timeout_s):
                cwd = await asyncio.to_thread(self._prepare, node)
                before = await asyncio.to_thread(scan_tree, cwd) if in_place else {}
                attempt = 1
                while True:
                    session = await self._start_session(node, cwd, attempt)
                    try:
                        result = await self._drive(node, session)
                        break
                    except NodeCrashedError as exc:
                        if attempt >= _MAX_ATTEMPTS or self._cancelled:
                            raise
                        await self._release(node, session, interrupt=False)
                        session = None
                        attempt += 1
                        partial = await asyncio.to_thread(
                            self._reset_for_retry, node, cwd, before
                        )
                        self._emit(
                            {
                                "type": "node_retry",
                                "node_id": node.id,
                                "attempt": attempt,
                                "error": str(exc)[:500],
                                "reset": "worktree" if node.worktree else "none",
                                "partial_files": list(partial),
                            }
                        )
            node.session_id = session.session_id
            await self._close(node.id, interrupt=False)
            span[1] = loop.time()
            if in_place:
                after = await asyncio.to_thread(scan_tree, cwd)
                self._attribute(node, changed_paths(before, after))
            self._record_result(node, result)
            if node.mutating and node.worktree:
                await asyncio.to_thread(self._commit, node)
            node.status = "completed"
            self._emit(
                {
                    "type": "node_completed",
                    "node_id": node.id,
                    "summary": node.summary,
                    "cost_usd": node.cost_usd,
                    "turns": node.turns,
                    "session_id": node.session_id,
                    "branch": node.branch,
                    "reverted_out_of_scope": list(node.reverted_out_of_scope),
                    "changed_files": list(node.changed_files),
                    "out_of_scope": list(node.out_of_scope),
                    "elapsed_s": round(loop.time() - (span[0] or 0.0), 1),
                }
            )
        except asyncio.CancelledError:
            node.status = "cancelled"
            self._emit({"type": "node_cancelled", "node_id": node.id})
            raise
        except TimeoutError:
            spent = f"wall time budget of {self.options.node_timeout_s}s spent"
            if waiting := self._waiting.get(node.id):
                spent = f"{_approval_timeout(waiting)} ({spent})"
            self._fail(node, spent)
        except (ChatSessionError, GitError, NodeFailedError) as exc:
            self._fail(node, str(exc))
        finally:
            if span[1] is None:
                span[1] = loop.time()
            if session is not None:
                await self._release(node, session, interrupt=node.status != "completed")

    async def _start_session(
        self, node: TaskNode, cwd: Path, attempt: int
    ) -> InteractiveClaudeSession:
        mode = self.options.permission_mode if node.mutating else "plan"
        session = await self.sessions.start(
            cwd=str(cwd),
            permission_mode=mode,
            model=self.options.model,
            policy_preset=self.options.policy_preset,
            budget={"max_turns": self.options.max_turns},
            extra_system_prompt=self.options.extra_system_prompt,
        )
        self._live[node.id] = session
        self._emit(
            {
                "type": "node_started",
                "node_id": node.id,
                "role": node.role,
                "cwd": str(cwd),
                "branch": node.branch,
                "permission_mode": session.permission_mode,
                "live_id": session.live_id,
                "attempt": attempt,
            }
        )
        return session

    async def _release(
        self, node: TaskNode, session: InteractiveClaudeSession, *, interrupt: bool
    ) -> None:
        """Close the node's session; prompts nobody answered die with it."""

        for request_id in self._waiting.pop(node.id, {}):
            self._emit_resolved(node.id, session.live_id, request_id, None)
        await self._close(node.id, interrupt=interrupt)

    def _reset_for_retry(
        self, node: TaskNode, cwd: Path, before: dict[str, FileStamp]
    ) -> list[str]:
        """Undo a crashed attempt where possible; returns partial files left behind.

        A git worktree goes back to the node's start commit (tracked and untracked
        files). In place (no git) nothing can be restored: the files the attempt
        changed are returned so the retry event can note them.
        """

        if node.worktree:
            git(cwd, "reset", "-q", "--hard", node.start_commit or "HEAD")
            git(cwd, "clean", "-q", "-fd")
            return []
        if self.options.workspace == "in_place" and node.mutating:
            changed = changed_paths(before, scan_tree(cwd))
            return split_ignored(changed, self._ignore)[0]
        return []

    def _emit_resolved(
        self, node_id: str, live_id: str, request_id: str, behavior: str | None
    ) -> None:
        self._emit(
            {
                "type": "node_permission_resolved",
                "node_id": node_id,
                "live_id": live_id,
                "request_id": request_id,
                "behavior": behavior,
            }
        )

    def _fail(self, node: TaskNode, error: str) -> None:
        node.status = "failed"
        node.error = error
        self._emit(
            {
                "type": "node_failed",
                "node_id": node.id,
                "error": error,
                "worktree": node.worktree,
            }
        )

    def _attribute(self, node: TaskNode, changed: list[str]) -> None:
        """Split an in-place node's changed files into its own and out-of-scope ones.

        Files inside the scope of another node that ran at the same time are that
        node's; files outside every such scope are reported (not reverted: without
        git there is nothing to restore from).
        """

        start, end = self._spans[node.id]
        start, end = start or 0.0, end or float("inf")
        others: list[str] = []
        for other in self.graph.nodes:
            o_start, o_end = self._spans.get(other.id, [None, None])
            if other.id == node.id or o_start is None:
                continue
            if o_start < end and (o_end is None or o_end > start):
                others += other.write_scope
        changed, ignored = split_ignored(changed, self._ignore)
        self.ignored_files.update(ignored)
        node.changed_files = [p for p in changed if in_scope(p, node.write_scope)]
        node.out_of_scope = [
            p
            for p in changed
            if p not in node.changed_files and not in_scope(p, others)
        ]

    def _prepare(self, node: TaskNode) -> Path:
        """Return the node's cwd; mutating nodes get a worktree holding their deps' work."""

        if not node.mutating or self.options.workspace == "in_place":
            return self.repo
        worktree = create_worktree(
            self.repo, self.graph.task_id, node.id, self.base_commit
        )
        node.branch, node.worktree = worktree.branch, str(worktree.path)
        ancestors = self.graph.ancestors(node.id)
        for dep_id in self.graph.order():
            dep = self.graph.node(dep_id)
            if dep_id in ancestors and dep.status == "completed" and dep.branch:
                git(worktree.path, "merge", "-q", "--no-edit", dep.branch)
        node.start_commit = git(worktree.path, "rev-parse", "HEAD")
        return worktree.path

    async def _drive(
        self, node: TaskNode, session: InteractiveClaudeSession
    ) -> JsonObject:
        """Send the node prompt and wait for Claude's ``result`` event."""

        events = session.subscribe()
        waiting = self._waiting.setdefault(node.id, {})
        opened: dict[str, float] = {}  # request id -> loop time it was asked
        loop = asyncio.get_running_loop()
        try:
            prompt = self._prompt(node)
            content: JsonValue = (
                [*self.options.images, {"type": "text", "text": prompt}]
                if self.options.images
                else prompt
            )
            await session.send_user_message(content)
            while True:
                limit = self.options.permission_timeout_s
                wait = (
                    max(0.0, min(opened.values()) + limit - loop.time())
                    if limit is not None and opened
                    else None
                )
                try:
                    event = await asyncio.wait_for(anext(events), wait)
                except StopAsyncIteration:
                    break
                except TimeoutError:
                    raise NodeFailedError(_approval_timeout(waiting)) from None
                kind = event.get("type")
                if kind == "result":
                    return event
                if kind == "control_request":
                    request, request_id = event.get("request"), event.get("request_id")
                    if (
                        isinstance(request, dict)
                        and request.get("subtype") == "can_use_tool"
                        and isinstance(request_id, str)
                    ):
                        waiting[request_id] = str(request.get("tool_name") or "tool")
                        opened[request_id] = loop.time()
                        self._emit(
                            {
                                "type": "node_permission",
                                "node_id": node.id,
                                "live_id": session.live_id,
                                "request_id": request_id,
                                "request": request,
                            }
                        )
                    continue
                if kind == "fcc_permission_resolved":
                    request_id = event.get("request_id")
                    if isinstance(request_id, str) and waiting.pop(request_id, None):
                        opened.pop(request_id, None)
                        behavior = event.get("behavior")
                        self._emit_resolved(
                            node.id,
                            session.live_id,
                            request_id,
                            behavior if isinstance(behavior, str) else None,
                        )
                    continue
                if kind == "fcc_exit":
                    raise NodeCrashedError(
                        f"Claude Code exited (code {event.get('code')}): "
                        f"{event.get('stderr') or ''}".strip()
                    )
                if kind == "assistant" and (uses := _tool_uses(event)):
                    self._emit(
                        {
                            "type": "node_progress",
                            "node_id": node.id,
                            "tools": [name for name, _ in uses],
                            "activity": [f"{name} {arg}".strip() for name, arg in uses],
                        }
                    )
            raise NodeCrashedError("Claude Code ended without a result.")
        finally:
            await events.aclose()

    def _record_result(self, node: TaskNode, result: JsonObject) -> None:
        cost, turns, text = (
            result.get("total_cost_usd"),
            result.get("num_turns"),
            result.get("result"),
        )
        node.cost_usd = float(cost) if isinstance(cost, int | float) else None
        node.turns = turns if isinstance(turns, int) else None
        node.summary = (text if isinstance(text, str) else "")[:_SUMMARY_LIMIT]
        if result.get("subtype") != "success" or result.get("is_error") is True:
            raise NodeFailedError(node.summary or f"result: {result.get('subtype')}")

    def _commit(self, node: TaskNode) -> None:
        assert node.worktree is not None
        path = Path(node.worktree)
        commit_all(path, f"fcc: {node.id}: {node.objective[:60]}", self._ignore)
        self.ignored_files.update(uncommitted(path))
        node.reverted_out_of_scope += enforce_scope(path, node, self._ignore)
        if node.start_commit:
            diff = git(
                path, "diff", "--name-only", "--no-renames", node.start_commit, "HEAD"
            )
            node.changed_files = [p for p in diff.splitlines() if p]

    async def _close(self, node_id: str, *, interrupt: bool) -> None:
        session = self._live.pop(node_id, None)
        if session is None:
            return
        if interrupt and not session.exited:
            with contextlib.suppress(ChatSessionError, TimeoutError):
                await asyncio.wait_for(
                    session.control({"subtype": "interrupt"}),
                    self.options.interrupt_timeout_s,
                )
        if not await self.sessions.close(session.live_id):
            await session.close()

    def _drop_working_branch(self, base_branch: str) -> None:
        """Nothing was integrated: go back to the base branch and delete ours."""

        if self.working_branch is None:
            return
        git(self.repo, "checkout", "-q", base_branch, check=False)
        if git(self.repo, "symbolic-ref", "--short", "-q", "HEAD", check=False) == (
            base_branch
        ):
            git(self.repo, "branch", "-D", self.working_branch, check=False)
            self.working_branch = None

    def _cleanup(self, *, keep_failed: bool) -> list[str]:
        """Remove worktrees (and branches) unless kept for debugging; returns kept paths."""

        merged = set(self.integration.merged if self.integration else [])
        kept: list[str] = []
        for node in self.graph.nodes:
            if not node.worktree:
                continue
            if keep_failed and node.status != "cancelled" and node.id not in merged:
                kept.append(node.worktree)
                continue
            remove_worktree(self.repo, Path(node.worktree), node.branch)
            logger.debug("Removed worktree {}", node.worktree)
        return kept

    # ----- prompt ------------------------------------------------------------

    def _prompt(self, node: TaskNode) -> str:
        opts = self.options
        lines = [
            f"You are the {node.role} agent for node '{node.id}' of a larger task.",
            "",
            "Objective:",
            node.objective,
        ]
        if node.instructions:
            lines += ["", "Instructions from the lead agent:", node.instructions]
        if opts.contract_block:
            lines += [
                "",
                "Intent constraints (binding, verbatim):",
                opts.contract_block,
            ]
        lines.append("")
        if node.mutating:
            lines.append(
                "Write scope: modify ONLY files matching these repo-relative globs: "
                + ", ".join(node.write_scope)
                + (
                    ". Other agents work in this same folder at the same time; "
                    "changes outside your scope are reported."
                    if opts.workspace == "in_place"
                    else ". Changes outside the scope are reverted."
                )
            )
        else:
            lines.append("This node is read-only: do not create or modify files.")
        if node.acceptance_criteria:
            lines += ["", "Acceptance criteria:"]
            lines += [f"- {c}" for c in node.acceptance_criteria]
        deps = [self.graph.node(d) for d in node.depends_on]
        if deps:
            lines += ["", "Results of the nodes this one depends on:"]
            lines += [f"- {d.id}: {d.summary or '(no summary)'}" for d in deps]
        lines += [
            "",
            f"Budget: finish within {opts.max_turns} turns.",
            "Do not commit, push, or switch branches; the orchestrator integrates your work.",
            "Stop and summarize when done: reply with a short summary of what you "
            "changed or found and how the acceptance criteria are met.",
        ]
        return "\n".join(lines)


def _approval_timeout(waiting: dict[str, str]) -> str:
    tools = ", ".join(dict.fromkeys(waiting.values()))
    return f"timed out waiting for approval of {tools}"


def _tool_uses(event: JsonObject) -> list[tuple[str, str]]:
    """``(tool name, short argument)`` for each tool_use block of an assistant event."""

    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    uses: list[tuple[str, str]] = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name")
        if not isinstance(name, str):
            continue
        tool_input = block.get("input")
        arg = ""
        if isinstance(tool_input, dict):
            for key in (
                "file_path",
                "path",
                "command",
                "pattern",
                "url",
                "description",
            ):
                if isinstance(value := tool_input.get(key), str) and value:
                    arg = " ".join(value.split())[:_ACTIVITY_ARG]
                    break
        uses.append((name, arg))
    return uses
