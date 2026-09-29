"""Persistent, bidirectional Claude Code sessions for the desktop-style chat UI.

Each session owns one ``claude`` process speaking stream-json on stdin/stdout
with the Agent SDK control protocol, so the UI gets every Claude Code feature:
tools, MCP, skills, slash commands, subagents, permission prompts, plan mode,
interrupts, and live model / permission-mode switching.
"""

import asyncio
import itertools
import json
import os
import shutil
import time
import uuid
from collections.abc import AsyncGenerator, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from free_claude_code.application.errors import InvalidRequestError
from free_claude_code.cli.claude_env import CLAUDE_BINARY_NAME
from free_claude_code.cli.process_registry import (
    kill_pid_tree_best_effort,
    register_pid,
    unregister_pid,
)
from free_claude_code.core.claude_permission_modes import PERMISSION_MODES
from free_claude_code.core.json_types import JsonObject, JsonValue

from . import project_files, transcripts
from .claude import build_managed_claude_env

CONTROL_TIMEOUT_S = 60.0
ABANDON_GRACE_S = 10.0
# Claude emits whole tool results on one line; the asyncio default (64 KiB) is too small.
_STDOUT_LINE_LIMIT = 64 * 1024 * 1024
_STDERR_TAIL_CHARS = 8_000
# Partial deltas are superseded by the final assistant message, so replay skips them.
_UNBUFFERED_TYPES = frozenset({"stream_event"})
# Replay log bounds: trim to the low mark once the high mark is exceeded.
MAX_REPLAY_EVENTS = 5_000
_REPLAY_TRIM_TO = 4_000


class ChatSessionError(InvalidRequestError):
    """User-facing failure while starting or driving a chat session."""


@dataclass(frozen=True, slots=True)
class ChatBudget:
    """Per-chat limits; exceeding one interrupts the turn and blocks new prompts."""

    max_turns: int | None = None
    max_minutes: float | None = None
    max_output_tokens: int | None = None

    @classmethod
    def from_json(cls, obj: Mapping[str, JsonValue] | None) -> ChatBudget | None:
        if not obj:
            return None
        values: list[float | None] = []
        for key in ("max_turns", "max_minutes", "max_output_tokens"):
            value = obj.get(key)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or value <= 0
            ):
                raise ChatSessionError(f"budget.{key} must be a positive number")
            values.append(value)
        turns, minutes, tokens = values
        budget = cls(
            max_turns=None if turns is None else max(1, int(turns)),
            max_minutes=None if minutes is None else float(minutes),
            max_output_tokens=None if tokens is None else max(1, int(tokens)),
        )
        return None if budget == cls() else budget

    def to_json(self) -> JsonObject:
        return {
            "max_turns": self.max_turns,
            "max_minutes": self.max_minutes,
            "max_output_tokens": self.max_output_tokens,
        }


type SessionObserver = Callable[[InteractiveClaudeSession, JsonObject], None]
"""Called with every published event (injection scanning, usage, audit)."""

type PolicyCompiler = Callable[[str, str], tuple[str, str]]
"""``(preset, cwd) -> (settings_json, permission_mode)``; raises ValueError."""


def build_interactive_claude_argv(
    *,
    claude_bin: str,
    permission_mode: str,
    model: str | None,
    resume_session_id: str | None,
    cwd: str | None = None,
    settings_json: str | None = None,
    max_turns: int | None = None,
    extra_system_prompt: str | None = None,
) -> list[str]:
    """Return the stream-json argv for one interactive chat process."""

    argv = [
        claude_bin,
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--permission-prompt-tool",
        "stdio",
        "--permission-mode",
        permission_mode,
        # Lets the UI switch into bypassPermissions later; it does not enable it.
        "--allow-dangerously-skip-permissions",
    ]
    if model:
        argv += ["--model", model]
    if resume_session_id:
        argv += ["--resume", resume_session_id]
    if settings_json:
        argv += ["--settings", settings_json]
    if max_turns:
        # Hidden in `claude --help` but supported; caps agentic turns per prompt.
        argv += ["--max-turns", str(max_turns)]
    system = "\n\n".join(
        part for part in (cwd and workspace_prompt(cwd), extra_system_prompt) if part
    )
    if system:
        argv += ["--append-system-prompt", system]
    return argv


def workspace_prompt(cwd: str) -> str:
    """Pin open models to the chosen folder; they often guess shell paths wrong.

    On Windows, Git Bash reports ``/tmp/x`` for ``%TEMP%\\x``, and models then
    write files to ``C:\\tmp``. Naming the native path avoids that.
    """

    lines = [
        f"The user's project folder is: {cwd}",
        "Create, read, and edit files inside this folder unless the user names another path.",
        "Prefer absolute paths built from this folder in file tools.",
    ]
    if os.name == "nt":
        lines.append(
            "This is Windows: use Windows paths like the one above in file tools; "
            "do not translate them from `pwd` output in Bash."
        )
    return "\n".join(lines)


class InteractiveClaudeSession:
    """One live Claude Code process plus its replayable event log."""

    def __init__(
        self,
        *,
        live_id: str,
        cwd: str,
        permission_mode: str,
        model: str | None,
        resume_session_id: str | None,
        on_release: Callable[[], None] | None = None,
        on_conversation: Callable[[str], None] | None = None,
        policy_preset: str | None = None,
        budget: ChatBudget | None = None,
        observer: SessionObserver | None = None,
    ) -> None:
        """``on_conversation`` fires with the session id once a prompt was sent."""

        self.live_id = live_id
        self.policy_preset = policy_preset
        self.budget = budget
        self._observer = observer
        self._input_tokens = 0
        self._output_tokens = 0
        self._turns = 0
        # Output tokens per assistant message id in the running turn.
        self._turn_output: dict[str, int] = {}
        self._busy_since: float | None = None
        self._busy_total_s = 0.0
        self._budget_hit: str | None = None
        self._budget_timer: asyncio.TimerHandle | None = None
        self._side_tasks: set[asyncio.Task[None]] = set()
        self._on_conversation = on_conversation
        self.cwd = cwd
        self.permission_mode = permission_mode
        self.model = model
        self.session_id = resume_session_id
        self.busy = False
        self.exited = False
        self.has_messages = False
        self._on_release = on_release
        self._events: list[JsonObject] = []
        self._subscribers: set[asyncio.Queue[JsonObject | None]] = set()
        self._pending_controls: dict[str, asyncio.Future[JsonObject]] = {}
        # Pending can_use_tool request id -> tool name.
        self._open_permission_ids: dict[str, str] = {}
        self._request_ids = itertools.count(1)
        self._process: asyncio.subprocess.Process | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._stderr_tail = ""
        self._write_lock = asyncio.Lock()

    # ----- lifecycle -------------------------------------------------------

    async def start(self, *, argv: list[str], env: Mapping[str, str]) -> None:
        try:
            self._process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
                env=dict(env),
                limit=_STDOUT_LINE_LIMIT,
            )
        except OSError as exc:
            raise ChatSessionError(f"Could not start Claude Code: {exc}") from exc
        register_pid(self._process.pid)
        self._tasks = [
            asyncio.create_task(self._read_stdout()),
            asyncio.create_task(self._read_stderr()),
        ]
        self._publish(self._state_event())
        # Initialize concurrently: it only resolves after startup hooks finish.
        self._tasks.append(asyncio.create_task(self._initialize()))

    async def close(self) -> None:
        """Close stdin so Claude exits cleanly, then force-kill stragglers."""

        process = self._process
        if process is None or process.returncode is not None:
            return
        if process.stdin is not None and not process.stdin.is_closing():
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except TimeoutError:
            kill_pid_tree_best_effort(process.pid)
            await process.wait()

    def _announce_conversation(self) -> None:
        if self.has_messages and self.session_id and self._on_conversation:
            self._on_conversation(self.session_id)

    # ----- outbound --------------------------------------------------------

    async def send_user_message(self, content: JsonValue) -> None:
        if self.exited:
            raise ChatSessionError("Session has ended; start it again to continue.")
        if self._budget_hit is not None:
            raise ChatSessionError(
                f"This chat's {self._budget_hit} budget is used up; "
                "start a new chat to continue."
            )
        self.busy = True
        self._start_clock()
        self.has_messages = True
        self._announce_conversation()
        message: JsonObject = {
            "type": "user",
            "message": {"role": "user", "content": content},
            "parent_tool_use_id": None,
            "session_id": self.session_id or "",
        }
        # Echo so every subscriber (and replay) shows the prompt immediately.
        self._publish({"type": "fcc_user", "message": message["message"]})
        self._publish(self._state_event())
        await self._write(message)

    async def control(self, request: JsonObject) -> JsonObject:
        """Send a control request (interrupt, set_model, ...) and await its reply."""

        subtype = request.get("subtype")
        request_id = f"fcc_{next(self._request_ids)}"
        future: asyncio.Future[JsonObject] = asyncio.get_running_loop().create_future()
        self._pending_controls[request_id] = future
        try:
            await self._write(
                {
                    "type": "control_request",
                    "request_id": request_id,
                    "request": request,
                }
            )
            response = await asyncio.wait_for(future, timeout=CONTROL_TIMEOUT_S)
        except TimeoutError as exc:
            raise ChatSessionError(f"Claude did not answer '{subtype}'.") from exc
        finally:
            self._pending_controls.pop(request_id, None)
        if subtype == "set_permission_mode" and isinstance(
            mode := request.get("mode"), str
        ):
            self.permission_mode = mode
            self._publish(self._state_event())
        if subtype == "set_model":
            model = request.get("model")
            self.model = model if isinstance(model, str) else None
            self._publish(self._state_event())
        return response

    async def respond_permission(self, request_id: str, decision: JsonObject) -> None:
        """Answer a ``can_use_tool`` request with an allow/deny PermissionResult."""

        if request_id not in self._open_permission_ids:
            raise ChatSessionError("That permission request is no longer pending.")
        tool_name = self._open_permission_ids.pop(request_id)
        await self._write(
            {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": request_id,
                    "response": decision,
                },
            }
        )
        self._publish(
            {
                "type": "fcc_permission_resolved",
                "request_id": request_id,
                "behavior": decision.get("behavior"),
                "tool_name": tool_name,
            }
        )

    # ----- subscribers -----------------------------------------------------

    async def subscribe(self) -> AsyncGenerator[JsonObject]:
        """Yield the replay log, then live events until the process exits."""

        queue: asyncio.Queue[JsonObject | None] = asyncio.Queue()
        backlog = list(self._events)
        self._subscribers.add(queue)
        try:
            for event in backlog:
                yield event
            # Lets a client that already rendered the saved transcript skip the backlog.
            yield {"type": "fcc_replay_end"}
            if self.exited:
                return
            while (event := await queue.get()) is not None:
                yield event
        finally:
            self._subscribers.discard(queue)
            if not self._subscribers and self._on_release is not None:
                asyncio.get_running_loop().call_later(
                    ABANDON_GRACE_S, self._reap_if_abandoned
                )

    def _reap_if_abandoned(self) -> None:
        """Close warm processes nobody used once their last viewer leaves."""

        if self._subscribers or self.has_messages or self.exited:
            return
        if self._on_release is not None:
            self._on_release()

    def snapshot(self) -> JsonObject:
        return {
            "live_id": self.live_id,
            "session_id": self.session_id,
            "cwd": self.cwd,
            "model": self.model,
            "permission_mode": self.permission_mode,
            "busy": self.busy,
            "exited": self.exited,
            "has_messages": self.has_messages,
            "policy_preset": self.policy_preset,
            "budget": None if self.budget is None else self.budget.to_json(),
            "usage": self.usage(),
        }

    def usage(self) -> JsonObject:
        """Session totals so far, including the running turn."""

        running = (
            time.monotonic() - self._busy_since if self._busy_since is not None else 0.0
        )
        return {
            "input_tokens": self._input_tokens,
            "output_tokens": self._output_tokens + sum(self._turn_output.values()),
            "turns": self._turns + len(self._turn_output),
            "elapsed_s": round(self._busy_total_s + running, 1),
        }

    def publish(self, event: JsonObject) -> None:
        """Add a synthetic event to the replay log and every live subscriber."""

        self._publish(event)

    # ----- budget ----------------------------------------------------------

    def _start_clock(self) -> None:
        if self._busy_since is not None:
            return
        self._busy_since = time.monotonic()
        minutes = self.budget.max_minutes if self.budget else None
        if minutes is not None:
            remaining = max(0.0, minutes * 60 - self._busy_total_s)
            self._budget_timer = asyncio.get_running_loop().call_later(
                remaining, self._time_budget_spent
            )

    def _stop_clock(self) -> None:
        if self._busy_since is not None:
            self._busy_total_s += time.monotonic() - self._busy_since
            self._busy_since = None
        if self._budget_timer is not None:
            self._budget_timer.cancel()
            self._budget_timer = None

    def _time_budget_spent(self) -> None:
        self._budget_timer = None
        if self.budget and self.budget.max_minutes is not None:
            elapsed = self.usage()["elapsed_s"]
            used = round(elapsed / 60, 2) if isinstance(elapsed, float) else elapsed
            self._exceed_budget("time", self.budget.max_minutes, used)

    def _check_budget(self) -> None:
        if self.budget is None or self._budget_hit is not None:
            return
        usage = self.usage()
        for kind, limit, used in (
            ("tokens", self.budget.max_output_tokens, usage["output_tokens"]),
            ("turns", self.budget.max_turns, usage["turns"]),
        ):
            if limit is not None and isinstance(used, int) and used > limit:
                self._exceed_budget(kind, limit, used)
                return

    def _exceed_budget(self, kind: str, limit: float, used: JsonValue) -> None:
        if self._budget_hit is not None:
            return
        self._budget_hit = kind
        self._publish(
            {
                "type": "fcc_budget",
                "kind": kind,
                "limit": limit,
                "used": used,
                "action": "interrupted",
            }
        )
        if self.busy and not self.exited:
            task = asyncio.get_running_loop().create_task(self._interrupt())
            self._side_tasks.add(task)
            task.add_done_callback(self._side_tasks.discard)

    async def _interrupt(self) -> None:
        try:
            await self.control({"subtype": "interrupt"})
        except ChatSessionError as exc:
            logger.warning("Budget interrupt failed: {}", exc)

    def _track_usage(self, event: JsonObject) -> None:
        kind = event.get("type")
        if kind == "assistant":
            message = event.get("message")
            if not isinstance(message, dict):
                return
            usage = message.get("usage")
            output = usage.get("output_tokens") if isinstance(usage, dict) else None
            key = str(message.get("id") or f"_{len(self._turn_output)}")
            self._turn_output[key] = max(
                self._turn_output.get(key, 0), output if isinstance(output, int) else 0
            )
            self._check_budget()
        elif kind == "result":
            usage = event.get("usage")
            usage = usage if isinstance(usage, dict) else {}
            tokens_in = usage.get("input_tokens")
            tokens_out = usage.get("output_tokens")
            turns = event.get("num_turns")
            self._input_tokens += tokens_in if isinstance(tokens_in, int) else 0
            self._output_tokens += (
                tokens_out
                if isinstance(tokens_out, int)
                else sum(self._turn_output.values())
            )
            self._turns += (
                turns if isinstance(turns, int) else max(1, len(self._turn_output))
            )
            self._turn_output.clear()
            self._stop_clock()
            self._check_budget()

    # ----- internals -------------------------------------------------------

    def _state_event(self) -> JsonObject:
        return {"type": "fcc_state", **self.snapshot()}

    def _publish(self, event: JsonObject) -> None:
        if event.get("type") not in _UNBUFFERED_TYPES:
            self._events.append(event)
            if len(self._events) > MAX_REPLAY_EVENTS:
                self._trim_events()
        for queue in self._subscribers:
            queue.put_nowait(event)
        if self._observer is not None:
            try:
                self._observer(self, event)
            except Exception:
                logger.exception("Chat event observer failed")

    def _trim_events(self) -> None:
        """Drop the oldest replay events, keeping what a reconnecting UI needs."""

        latest: dict[str, int] = {}
        for index, event in enumerate(self._events):
            if event.get("type") in ("fcc_state", "fcc_initialize"):
                latest[str(event["type"])] = index
        pinned = set(latest.values())
        drop = len(self._events) - _REPLAY_TRIM_TO
        kept: list[JsonObject] = []
        for index, event in enumerate(self._events):
            keep = index in pinned or (
                event.get("type") == "control_request"
                and event.get("request_id") in self._open_permission_ids
            )
            if drop > 0 and not keep:
                drop -= 1
                continue
            kept.append(event)
        self._events = kept

    async def _write(self, payload: JsonObject) -> None:
        process = self._process
        if (
            self.exited
            or process is None
            or process.stdin is None
            or process.stdin.is_closing()
        ):
            raise ChatSessionError("Claude Code is not running.")
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._write_lock:
            try:
                process.stdin.write(data)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise ChatSessionError("Claude Code closed its input.") from exc

    async def _initialize(self) -> None:
        try:
            response = await self.control({"subtype": "initialize"})
        except ChatSessionError as exc:
            logger.warning("Chat initialize failed: {}", exc)
            return
        self._publish({"type": "fcc_initialize", "response": response})

    async def _read_stdout(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        try:
            while line := await process.stdout.readline():
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    event = json.loads(text)
                except json.JSONDecodeError:
                    self._publish({"type": "fcc_raw", "text": text})
                    continue
                if not isinstance(event, dict):
                    continue
                try:
                    await self._handle_event(event)
                except Exception:
                    # One bad event must not stop the reader and wedge the session.
                    logger.exception("Chat event handling failed")
        except Exception as exc:
            # The stream is unusable (e.g. an over-limit line); end the process
            # so _on_exit can reap it instead of waiting forever.
            logger.warning("Chat stdout reader failed: {}", exc)
            kill_pid_tree_best_effort(process.pid)
        finally:
            await self._on_exit()

    async def _handle_event(self, event: JsonObject) -> None:
        kind = event.get("type")
        if kind == "control_response":
            self._resolve_control(event)
            return
        if kind == "control_request":
            await self._handle_inbound_control(event)
            return
        if kind == "control_cancel_request":
            request_id = event.get("request_id")
            if isinstance(request_id, str):
                self._open_permission_ids.pop(request_id, None)
                self._publish(
                    {"type": "fcc_permission_resolved", "request_id": request_id}
                )
            return
        session_id = event.get("session_id")
        if isinstance(session_id, str) and session_id and session_id != self.session_id:
            self.session_id = session_id
            self._announce_conversation()
            self._publish(self._state_event())
        if kind == "system" and event.get("subtype") == "init":
            if isinstance(mode := event.get("permissionMode"), str):
                self.permission_mode = mode
            if isinstance(model := event.get("model"), str):
                self.model = model
            self._publish(self._state_event())
        self._track_usage(event)
        self._publish(event)
        if kind == "result":
            self.busy = False
            self._publish(self._state_event())

    def _resolve_control(self, event: JsonObject) -> None:
        response = event.get("response")
        if not isinstance(response, dict):
            return
        future = self._pending_controls.get(str(response.get("request_id")))
        if future is None or future.done():
            return
        if response.get("subtype") == "error":
            future.set_exception(ChatSessionError(str(response.get("error"))))
            return
        payload = response.get("response")
        future.set_result(payload if isinstance(payload, dict) else {})

    async def _handle_inbound_control(self, event: JsonObject) -> None:
        request_id = event.get("request_id")
        request = event.get("request")
        if not isinstance(request_id, str) or not isinstance(request, dict):
            return
        if request.get("subtype") == "can_use_tool":
            self._open_permission_ids[request_id] = str(request.get("tool_name", ""))
            self._publish(event)
            return
        # We register no hooks or SDK MCP servers; never leave Claude waiting.
        await self._write(
            {
                "type": "control_response",
                "response": {
                    "subtype": "error",
                    "request_id": request_id,
                    "error": f"Unsupported control request: {request.get('subtype')}",
                },
            }
        )

    async def _read_stderr(self) -> None:
        process = self._process
        assert process is not None and process.stderr is not None
        while chunk := await process.stderr.read(65_536):
            text = self._stderr_tail + chunk.decode("utf-8", errors="replace")
            self._stderr_tail = text[-_STDERR_TAIL_CHARS:]

    async def _on_exit(self) -> None:
        process = self._process
        assert process is not None
        code = await process.wait()
        unregister_pid(process.pid)
        self.exited = True
        self.busy = False
        self._stop_clock()
        self._open_permission_ids.clear()
        for future in self._pending_controls.values():
            if not future.done():
                future.set_exception(ChatSessionError("Claude Code exited."))
        self._publish(self._state_event())
        self._publish(
            {"type": "fcc_exit", "code": code, "stderr": self._stderr_tail.strip()}
        )
        for queue in self._subscribers:
            queue.put_nowait(None)
        if self._on_release is not None:
            self._on_release()


def _load_id_set(path: Path | None) -> set[str]:
    if path is None:
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return set()
    return (
        {item for item in data if isinstance(item, str)}
        if isinstance(data, list)
        else set()
    )


class InteractiveClaudeSessions:
    """Registry of live chat sessions, keyed by an opaque live id."""

    def __init__(
        self,
        *,
        proxy_target: Callable[[], tuple[str, str]],
        claude_bin: str = CLAUDE_BINARY_NAME,
        app_sessions_path: Path | None = None,
        policy_compiler: PolicyCompiler | None = None,
        observer: SessionObserver | None = None,
    ) -> None:
        """``proxy_target`` returns the current ``(proxy_root_url, auth_token)``.

        ``app_sessions_path`` persists which transcripts were started from the chat
        UI, so the sidebar can hide headless sessions other tools create.
        """

        self._proxy_target = proxy_target
        self._claude_bin = claude_bin
        self._policy_compiler = policy_compiler
        self._observer = observer
        self._app_sessions_path = app_sessions_path
        self._app_session_ids = _load_id_set(app_sessions_path)
        self._sessions: dict[str, InteractiveClaudeSession] = {}
        self._closing: set[asyncio.Task[bool]] = set()

    def _close_soon(self, live_id: str) -> None:
        task = asyncio.create_task(self.close(live_id))
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    async def start(
        self,
        *,
        cwd: str,
        permission_mode: str | None = None,
        model: str | None = None,
        resume_session_id: str | None = None,
        policy_preset: str | None = None,
        budget: Mapping[str, JsonValue] | None = None,
        extra_system_prompt: str | None = None,
    ) -> InteractiveClaudeSession:
        """Start one chat process.

        ``policy_preset`` adds FCC permission rules via ``--settings``; an explicit
        ``permission_mode`` wins over the preset's mode.
        """

        if permission_mode is not None and permission_mode not in PERMISSION_MODES:
            raise ChatSessionError(f"Unknown permission mode '{permission_mode}'.")
        chat_budget = ChatBudget.from_json(budget)
        workspace = os.path.abspath(os.path.expanduser(cwd))
        if not os.path.isdir(workspace):
            raise ChatSessionError(f"Folder does not exist: {workspace}")
        settings_json: str | None = None
        if policy_preset:
            if self._policy_compiler is None:
                raise ChatSessionError("Policy presets are not available.")
            try:
                settings_json, preset_mode = self._policy_compiler(
                    policy_preset, workspace
                )
            except ValueError as exc:
                raise ChatSessionError(str(exc)) from exc
            permission_mode = permission_mode or preset_mode
        permission_mode = permission_mode or "default"
        claude_bin = shutil.which(self._claude_bin)
        if claude_bin is None:
            raise ChatSessionError(
                "Claude Code CLI not found on PATH. Install it with the FCC installer."
            )
        proxy_root_url, auth_token = self._proxy_target()
        live_id = uuid.uuid4().hex
        session = InteractiveClaudeSession(
            live_id=live_id,
            cwd=workspace,
            permission_mode=permission_mode,
            model=model,
            resume_session_id=resume_session_id,
            on_release=lambda: self._close_soon(live_id),
            on_conversation=self._remember_app_session,
            policy_preset=policy_preset or None,
            budget=chat_budget,
            observer=self._observer,
        )
        await session.start(
            argv=build_interactive_claude_argv(
                claude_bin=claude_bin,
                permission_mode=permission_mode,
                model=model,
                resume_session_id=resume_session_id,
                cwd=workspace,
                settings_json=settings_json,
                max_turns=chat_budget.max_turns if chat_budget else None,
                extra_system_prompt=extra_system_prompt,
            ),
            env=build_managed_claude_env(
                proxy_root_url=proxy_root_url,
                auth_token=auth_token,
                base_env=os.environ,
            ),
        )
        self._sessions[session.live_id] = session
        return session

    def get(self, live_id: str) -> InteractiveClaudeSession | None:
        return self._sessions.get(live_id)

    def live_sessions(self) -> list[InteractiveClaudeSession]:
        return list(self._sessions.values())

    async def close(self, live_id: str) -> bool:
        session = self._sessions.pop(live_id, None)
        if session is None:
            return False
        await session.close()
        return True

    async def search_files(self, cwd: str, query: str) -> list[str]:
        """Relative POSIX paths under ``cwd`` matching ``query`` (for @-mentions)."""

        root = os.path.abspath(os.path.expanduser(cwd))
        if not os.path.isdir(root):
            raise ChatSessionError(f"Folder does not exist: {root}")
        return await asyncio.to_thread(project_files.search_project_files, root, query)

    async def list_dirs(self, path: str) -> JsonObject:
        """Subfolders of ``path`` (home when empty) for the open-folder dialog."""

        try:
            return await asyncio.to_thread(project_files.list_directories, path)
        except NotADirectoryError as exc:
            raise ChatSessionError(f"Folder does not exist: {exc}") from exc
        except PermissionError as exc:
            raise ChatSessionError(f"Cannot open folder: {path}") from exc

    # ----- saved transcripts ------------------------------------------------

    def _remember_app_session(self, session_id: str) -> None:
        if session_id in self._app_session_ids:
            return
        self._app_session_ids.add(session_id)
        if self._app_sessions_path is None:
            return
        try:
            self._app_sessions_path.parent.mkdir(parents=True, exist_ok=True)
            self._app_sessions_path.write_text(
                json.dumps(sorted(self._app_session_ids)), encoding="utf-8"
            )
        except OSError as exc:
            logger.warning("Could not persist chat session ids: {}", exc)

    def list_transcripts(self) -> list[JsonObject]:
        return [
            {**summary.to_json(), "app": summary.session_id in self._app_session_ids}
            for summary in transcripts.list_transcripts()
        ]

    def transcript_events(self, session_id: str) -> list[JsonObject] | None:
        path = transcripts.find_transcript(session_id)
        return None if path is None else transcripts.load_transcript_events(path)

    def rename_transcript(self, session_id: str, title: str) -> bool:
        path = transcripts.find_transcript(session_id)
        if path is None:
            return False
        transcripts.rename_transcript(path, session_id, title)
        return True

    def delete_transcript(self, session_id: str) -> bool:
        path = transcripts.find_transcript(session_id)
        if path is None:
            return False
        transcripts.delete_transcript(path)
        return True

    async def stop_all(self) -> None:
        sessions = list(self._sessions.values())
        self._sessions.clear()
        await asyncio.gather(*(s.close() for s in sessions), return_exceptions=True)
