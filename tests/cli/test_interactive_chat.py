"""Interactive chat sessions: argv, control protocol, lifecycle, and transcripts."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from free_claude_code.cli.managed import interactive, transcripts
from free_claude_code.cli.managed.interactive import (
    MAX_REPLAY_EVENTS,
    ChatBudget,
    ChatSessionError,
    InteractiveClaudeSession,
    InteractiveClaudeSessions,
    build_interactive_claude_argv,
)
from free_claude_code.core.json_types import JsonObject

FAKE = Path(__file__).with_name("fake_claude_stream.py")


def _fake_claude(tmp_path: Path) -> str:
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


def _registry(tmp_path: Path) -> InteractiveClaudeSessions:
    return InteractiveClaudeSessions(
        proxy_target=lambda: ("http://127.0.0.1:1", "token"),
        claude_bin=_fake_claude(tmp_path),
    )


async def _next(events, predicate, timeout: float = 20.0):
    async def find():
        async for event in events:
            if predicate(event):
                return event
        raise AssertionError("stream ended")

    return await asyncio.wait_for(find(), timeout)


def test_argv_carries_protocol_flags_model_and_resume():
    argv = build_interactive_claude_argv(
        claude_bin="claude", permission_mode="plan", model="m1", resume_session_id="abc"
    )
    assert argv[0] == "claude"
    for flag in (
        "--input-format",
        "--output-format",
        "--include-partial-messages",
        "--verbose",
    ):
        assert flag in argv
    assert argv[argv.index("--permission-prompt-tool") + 1] == "stdio"
    assert argv[argv.index("--permission-mode") + 1] == "plan"
    assert argv[argv.index("--model") + 1] == "m1"
    assert argv[argv.index("--resume") + 1] == "abc"
    # User-level ~/.claude settings (plugins, hooks, pinned model) stay out.
    assert argv[argv.index("--setting-sources") + 1] == "project,local"
    bare = build_interactive_claude_argv(
        claude_bin="claude",
        permission_mode="default",
        model=None,
        resume_session_id=None,
    )
    assert "--model" not in bare and "--resume" not in bare


@pytest.mark.asyncio
async def test_start_rejects_bad_mode_and_missing_folder(tmp_path: Path):
    registry = _registry(tmp_path)
    with pytest.raises(ChatSessionError):
        await registry.start(cwd=str(tmp_path), permission_mode="yolo")
    with pytest.raises(ChatSessionError):
        await registry.start(cwd=str(tmp_path / "missing"))
    missing_bin = InteractiveClaudeSessions(
        proxy_target=lambda: ("http://x", "t"), claude_bin="definitely-not-claude-xyz"
    )
    with pytest.raises(ChatSessionError):
        await missing_bin.start(cwd=str(tmp_path))


@pytest.mark.asyncio
async def test_full_turn_with_permission_prompt_and_controls(tmp_path: Path):
    registry = _registry(tmp_path)
    session = await registry.start(cwd=str(tmp_path))
    events = session.subscribe()
    try:
        init = await _next(events, lambda e: e.get("type") == "fcc_initialize")
        assert init["response"]["models"][0]["value"] == "m1"
        hook = await _next(events, lambda e: e.get("type") == "fake_hook_answered")
        assert (
            hook["subtype"] == "error"
        )  # unsupported inbound control never hangs Claude
        assert session.session_id == "11111111-2222-3333-4444-555555555555"
        assert session.model == "fake-model"

        await session.send_user_message("hello")
        assert session.busy and session.has_messages
        echo = await _next(events, lambda e: e.get("type") == "fcc_user")
        assert echo["message"]["content"] == "hello"
        prompt = await _next(events, lambda e: e.get("type") == "control_request")
        assert prompt["request"]["tool_name"] == "Write"

        with pytest.raises(ChatSessionError):
            await session.respond_permission("not-pending", {"behavior": "allow"})
        await session.respond_permission(
            "perm_1", {"behavior": "allow", "updatedInput": prompt["request"]["input"]}
        )
        resolved = await _next(
            events, lambda e: e.get("type") == "fcc_permission_resolved"
        )
        assert resolved["behavior"] == "allow"
        assert resolved["tool_name"] == "Write"
        text = await _next(events, lambda e: e.get("type") == "assistant")
        assert text["message"]["content"][0]["text"] == "decision=allow"
        await _next(events, lambda e: e.get("type") == "result")
        assert not session.busy

        assert await session.control({"subtype": "set_model", "model": "m1"}) == {
            "model": "m1"
        }
        assert session.model == "m1"
        await session.control({"subtype": "set_permission_mode", "mode": "plan"})
        assert session.permission_mode == "plan"
        assert await session.control({"subtype": "interrupt"}) == {}
    finally:
        await events.aclose()
        await registry.stop_all()


@pytest.mark.asyncio
async def test_replay_marks_backlog_end_and_exit_releases_session(tmp_path: Path):
    registry = _registry(tmp_path)
    session = await registry.start(cwd=str(tmp_path))
    first = session.subscribe()
    await _next(first, lambda e: e.get("type") == "fcc_initialize")
    await first.aclose()

    replay = session.subscribe()
    kinds = []
    async for event in replay:
        kinds.append(event["type"])
        if event["type"] == "fcc_replay_end":
            break
    assert "fcc_initialize" in kinds and kinds[-1] == "fcc_replay_end"
    assert "stream_event" not in kinds

    await session.close()
    exit_event = await _next(replay, lambda e: e.get("type") == "fcc_exit")
    assert "code" in exit_event
    await replay.aclose()
    for _ in range(50):
        if registry.get(session.live_id) is None:
            break
        await asyncio.sleep(0.05)
    assert registry.get(session.live_id) is None
    with pytest.raises(ChatSessionError):
        await session.send_user_message("after exit")


async def _backlog(session) -> list[dict]:
    stream = session.subscribe()
    events = []
    try:
        async for event in stream:
            if event["type"] == "fcc_replay_end":
                return events
            events.append(event)
    finally:
        await stream.aclose()
    raise AssertionError("no replay end")


@pytest.mark.asyncio
async def test_permission_prompt_replayed_to_reconnecting_client_is_answerable(
    tmp_path: Path,
):
    registry = _registry(tmp_path)
    session = await registry.start(cwd=str(tmp_path))
    first = session.subscribe()
    try:
        await _next(first, lambda e: e.get("type") == "fcc_initialize")
        await session.send_user_message("hi")
        await _next(first, lambda e: e.get("type") == "control_request")
        await first.aclose()

        second = session.subscribe()
        replayed = await _next(
            second,
            lambda e: (
                e.get("type") == "control_request"
                and e["request"]["subtype"] == "can_use_tool"
            ),
        )
        await session.respond_permission(replayed["request_id"], {"behavior": "deny"})
        text = await _next(second, lambda e: e.get("type") == "assistant")
        assert text["message"]["content"][0]["text"] == "decision=deny"
        await second.aclose()
        with pytest.raises(ChatSessionError):
            await session.respond_permission(replayed["request_id"], {"behavior": "x"})
    finally:
        await registry.stop_all()


@pytest.mark.asyncio
async def test_writes_after_exit_fail_fast_and_close_is_idempotent(tmp_path: Path):
    never_started = InteractiveClaudeSession(
        live_id="x",
        cwd=str(tmp_path),
        permission_mode="default",
        model=None,
        resume_session_id=None,
    )
    await never_started.close()

    registry = _registry(tmp_path)
    session = await registry.start(cwd=str(tmp_path))
    events = session.subscribe()
    await _next(events, lambda e: e.get("type") == "fcc_initialize")
    await session.close()
    await _next(events, lambda e: e.get("type") == "fcc_exit")
    await events.aclose()
    await session.close()  # process already gone
    with pytest.raises(ChatSessionError):
        await asyncio.wait_for(session.control({"subtype": "interrupt"}), 5.0)
    await registry.stop_all()


@pytest.mark.asyncio
async def test_event_handler_error_does_not_stop_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    registry = _registry(tmp_path)
    session = await registry.start(cwd=str(tmp_path))
    original = session._handle_event
    failures = []

    async def flaky(event):
        if not failures:
            failures.append(event)
            raise RuntimeError("boom")
        await original(event)

    monkeypatch.setattr(session, "_handle_event", flaky)
    events = session.subscribe()
    try:
        await _next(events, lambda e: e.get("type") == "fcc_initialize")
        assert failures and not session.exited
    finally:
        await events.aclose()
        await registry.stop_all()


@pytest.mark.asyncio
async def test_unreadable_stdout_kills_process_and_publishes_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(interactive, "_STDOUT_LINE_LIMIT", 2048)
    registry = _registry(tmp_path)
    session = await registry.start(cwd=str(tmp_path))
    events = session.subscribe()
    try:
        await _next(events, lambda e: e.get("type") == "fcc_initialize")
        await session.send_user_message("__long_line__")
        await _next(events, lambda e: e.get("type") == "fcc_exit")
        assert session.exited
    finally:
        await events.aclose()
        await registry.stop_all()


@pytest.mark.asyncio
async def test_replay_buffer_is_bounded_but_keeps_open_prompts_and_latest_state(
    tmp_path: Path,
):
    session = InteractiveClaudeSession(
        live_id="x",
        cwd=str(tmp_path),
        permission_mode="default",
        model=None,
        resume_session_id=None,
    )
    session._publish({"type": "fcc_initialize", "response": {"n": 1}})
    await session._handle_event(
        {
            "type": "control_request",
            "request_id": "open",
            "request": {"subtype": "can_use_tool", "tool_name": "Write"},
        }
    )
    for n in range(MAX_REPLAY_EVENTS * 2):
        await session._handle_event({"type": "assistant", "n": n})
    session.model = "latest"
    session._publish(session._state_event())

    backlog = await _backlog(session)
    assert len(backlog) <= MAX_REPLAY_EVENTS
    prompt = next(e for e in backlog if e["type"] == "control_request")
    assert prompt["request_id"] == "open"
    assert any(e["type"] == "fcc_initialize" for e in backlog)
    assert backlog[-1]["type"] == "fcc_state" and backlog[-1]["model"] == "latest"
    ns = [e["n"] for e in backlog if e["type"] == "assistant"]
    assert ns == sorted(ns) and ns[-1] == MAX_REPLAY_EVENTS * 2 - 1


@pytest.mark.asyncio
async def test_search_files_validates_folder(tmp_path: Path):
    registry = _registry(tmp_path)
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    assert "a.py" in await registry.search_files(str(tmp_path), "A.P")
    with pytest.raises(ChatSessionError):
        await registry.search_files(str(tmp_path / "missing"), "")
    with pytest.raises(ChatSessionError):
        await registry.search_files(str(tmp_path / "a.py"), "")


def _write_jsonl(path: Path, entries: list[dict | str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")


def test_transcripts_list_titles_history_rename_delete(tmp_path: Path):
    sid = "aaaaaaaa-0000-0000-0000-000000000001"
    path = tmp_path / "proj" / f"{sid}.jsonl"
    _write_jsonl(
        path,
        [
            {"type": "attachment", "cwd": "/work/app"},
            {"type": "user", "isMeta": True, "message": {"content": "meta"}},
            {
                "type": "user",
                "message": {
                    "content": "<local-command-stdout>x</local-command-stdout>"
                },
            },
            {
                "type": "user",
                "cwd": "/work/app",
                "message": {"content": "Fix the  login bug"},
            },
            {
                "type": "assistant",
                "message": {"id": "a", "content": [{"type": "text", "text": "ok"}]},
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t", "content": "r"}
                    ]
                },
            },
            {"type": "user", "isSidechain": True, "message": {"content": "sub"}},
            {"type": "system", "subtype": "compact_boundary"},
            {
                "type": "user",
                "isCompactSummary": True,
                "message": {"content": "summary"},
            },
            "not-an-object",
        ],
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{broken json\n")
    _write_jsonl(
        tmp_path / "proj" / "bbbbbbbb-hooks-only.jsonl", [{"type": "attachment"}]
    )

    [summary] = transcripts.list_transcripts(root=tmp_path)
    assert summary.session_id == sid
    assert summary.title == "Fix the login bug"
    assert summary.cwd == "/work/app"

    events = transcripts.load_transcript_events(path)
    assert [e["type"] for e in events] == ["fcc_user", "assistant", "user", "system"]

    transcripts.rename_transcript(path, sid, "Login fix")
    assert transcripts.list_transcripts(root=tmp_path)[0].title == "Login fix"

    assert transcripts.find_transcript(sid, root=tmp_path) == path
    assert transcripts.find_transcript("../../etc/passwd", root=tmp_path) is None
    transcripts.delete_transcript(path)
    assert transcripts.list_transcripts(root=tmp_path) == []
    assert transcripts.list_transcripts(root=tmp_path / "nope") == []


def test_slash_command_prompt_becomes_title(tmp_path: Path):
    path = tmp_path / "p" / "cccccccc-0000.jsonl"
    _write_jsonl(
        path,
        [
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "text",
                            "text": "<command-name>/review</command-name><command-args>x</command-args>",
                        }
                    ]
                },
            },
            {"type": "ai-title", "aiTitle": "Review PR"},
        ],
    )
    assert transcripts.list_transcripts(root=tmp_path)[0].title == "Review PR"
    assert transcripts.load_transcript_events(path)[0]["type"] == "fcc_user"


@pytest.mark.asyncio
async def test_app_sessions_are_remembered_and_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store = tmp_path / "fcc" / "chat-sessions.json"
    registry = InteractiveClaudeSessions(
        proxy_target=lambda: ("http://127.0.0.1:1", "token"),
        claude_bin=_fake_claude(tmp_path),
        app_sessions_path=store,
    )
    session = await registry.start(cwd=str(tmp_path))
    events = session.subscribe()
    try:
        await _next(events, lambda e: e.get("type") == "fcc_initialize")
        assert not store.exists()  # warm process without a prompt is not an app chat
        await session.send_user_message("hi")
    finally:
        await events.aclose()
        await registry.stop_all()
    sid = "11111111-2222-3333-4444-555555555555"
    assert json.loads(store.read_text(encoding="utf-8")) == [sid]

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    for session_id in (sid, "99999999-0000-0000-0000-000000000000"):
        _write_jsonl(
            tmp_path / "claude" / "projects" / "p" / f"{session_id}.jsonl",
            [{"type": "user", "message": {"content": "prompt"}}],
        )
    reloaded = InteractiveClaudeSessions(
        proxy_target=lambda: ("http://x", "t"), app_sessions_path=store
    )
    flags = {t["session_id"]: t["app"] for t in reloaded.list_transcripts()}
    assert flags == {sid: True, "99999999-0000-0000-0000-000000000000": False}

    store.write_text("{not json", encoding="utf-8")
    broken = InteractiveClaudeSessions(
        proxy_target=lambda: ("x", "t"), app_sessions_path=store
    )
    assert not any(t["app"] for t in broken.list_transcripts())


def test_argv_pins_workspace_folder_in_system_prompt(tmp_path: Path):
    argv = build_interactive_claude_argv(
        claude_bin="claude",
        permission_mode="default",
        model=None,
        resume_session_id=None,
        cwd=str(tmp_path),
    )
    prompt = argv[argv.index("--append-system-prompt") + 1]
    assert str(tmp_path) in prompt
    assert ("Windows" in prompt) == (os.name == "nt")


# ----- policy, budgets, observers ---------------------------------------------

CODER = Path(__file__).resolve().parents[1] / "workbench" / "fake_claude_coder.py"


def _coder_registry(tmp_path: Path, **kwargs) -> InteractiveClaudeSessions:
    if os.name == "nt":
        wrapper = tmp_path / "coder.bat"
        wrapper.write_text(f'@"{sys.executable}" "{CODER}" %*\r\n', encoding="utf-8")
    else:
        wrapper = tmp_path / "coder"
        wrapper.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{CODER}" "$@"\n', encoding="utf-8"
        )
        wrapper.chmod(0o755)
    return InteractiveClaudeSessions(
        proxy_target=lambda: ("http://127.0.0.1:1", "token"),
        claude_bin=str(wrapper),
        **kwargs,
    )


def test_argv_carries_settings_max_turns_and_extra_system_prompt():
    argv = build_interactive_claude_argv(
        claude_bin="claude",
        permission_mode="acceptEdits",
        model=None,
        resume_session_id=None,
        cwd="/work",
        settings_json='{"permissions":{}}',
        max_turns=7,
        extra_system_prompt="EXTRA RULES",
    )
    assert argv[argv.index("--settings") + 1] == '{"permissions":{}}'
    assert argv[argv.index("--max-turns") + 1] == "7"
    system = argv[argv.index("--append-system-prompt") + 1]
    assert "/work" in system and system.endswith("EXTRA RULES")
    assert argv.count("--append-system-prompt") == 1
    only_extra = build_interactive_claude_argv(
        claude_bin="claude",
        permission_mode="default",
        model=None,
        resume_session_id=None,
        extra_system_prompt="X",
    )
    assert only_extra[only_extra.index("--append-system-prompt") + 1] == "X"
    assert "--settings" not in only_extra and "--max-turns" not in only_extra


def test_chat_budget_parsing():
    assert ChatBudget.from_json(None) is None
    assert ChatBudget.from_json({}) is None
    assert ChatBudget.from_json({"max_turns": None}) is None
    budget = ChatBudget.from_json(
        {"max_turns": 3, "max_minutes": 0.5, "max_output_tokens": 100}
    )
    assert budget == ChatBudget(3, 0.5, 100)
    assert budget.to_json() == {
        "max_turns": 3,
        "max_minutes": 0.5,
        "max_output_tokens": 100,
    }
    for bad in ({"max_turns": 0}, {"max_minutes": -1}, {"max_output_tokens": True}):
        with pytest.raises(ChatSessionError):
            ChatBudget.from_json(bad)
    with pytest.raises(ChatSessionError):
        ChatBudget.from_json({"max_turns": "5"})


@pytest.mark.asyncio
async def test_policy_preset_compiles_settings_and_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    calls: list[tuple[str, str]] = []
    captured: list[list[str]] = []

    def compiler(preset: str, cwd: str) -> tuple[str, str]:
        calls.append((preset, cwd))
        if preset == "bogus":
            raise ValueError("Unknown preset: 'bogus'")
        return '{"permissions":{"deny":["Edit(.git/**)"]}}', "acceptEdits"

    real_start = InteractiveClaudeSession.start

    async def spy(self, *, argv, env):
        captured.append(list(argv))
        await real_start(self, argv=argv, env=env)

    monkeypatch.setattr(InteractiveClaudeSession, "start", spy)
    registry = _coder_registry(tmp_path, policy_compiler=compiler)
    try:
        session = await registry.start(cwd=str(tmp_path), policy_preset="workspace")
        assert session.permission_mode == "acceptEdits"
        assert session.snapshot()["policy_preset"] == "workspace"
        argv = captured[-1]
        assert "Edit(.git/**)" in argv[argv.index("--settings") + 1]
        explicit = await registry.start(
            cwd=str(tmp_path), policy_preset="workspace", permission_mode="plan"
        )
        assert explicit.permission_mode == "plan"
        looser = await registry.start(
            cwd=str(tmp_path), policy_preset="workspace", permission_mode="auto"
        )
        assert looser.permission_mode == "acceptEdits"
        assert looser.snapshot()["preset_permission_mode"] == "acceptEdits"
        with pytest.raises(ChatSessionError, match="stricter"):
            await looser.control(
                {"subtype": "set_permission_mode", "mode": "bypassPermissions"}
            )
        with pytest.raises(ChatSessionError, match="Unknown permission mode"):
            await looser.control({"subtype": "set_permission_mode", "mode": "yolo"})
        await looser.control({"subtype": "set_permission_mode", "mode": "dontAsk"})
        assert looser.permission_mode == "dontAsk"
        with pytest.raises(ChatSessionError, match="Unknown preset"):
            await registry.start(cwd=str(tmp_path), policy_preset="bogus")
        assert calls[0] == ("workspace", str(tmp_path))
        plain = _coder_registry(tmp_path)
        with pytest.raises(ChatSessionError, match="not available"):
            await plain.start(cwd=str(tmp_path), policy_preset="workspace")
    finally:
        await registry.stop_all()


@pytest.mark.asyncio
async def test_token_budget_interrupts_and_blocks_new_prompts(tmp_path: Path):
    registry = _coder_registry(tmp_path)
    session = await registry.start(cwd=str(tmp_path), budget={"max_output_tokens": 20})
    events = session.subscribe()
    try:
        assert session.budget == ChatBudget(max_output_tokens=20)
        assert "budget" in session.snapshot() and "usage" in session.snapshot()
        await session.send_user_message("work\nUSAGE 50\nHANG")
        notice = await _next(events, lambda e: e.get("type") == "fcc_budget")
        assert notice == {
            "type": "fcc_budget",
            "kind": "tokens",
            "limit": 20,
            "used": 50,
            "action": "interrupted",
        }
        result = await _next(events, lambda e: e.get("type") == "result")
        assert result["subtype"] == "error_during_execution"  # interrupt reached Claude
        assert not session.busy
        usage = session.usage()
        assert usage["turns"] == 1 and usage["input_tokens"] == 10
        with pytest.raises(ChatSessionError, match="budget"):
            await session.send_user_message("more")
    finally:
        await events.aclose()
        await registry.stop_all()


@pytest.mark.asyncio
async def test_time_budget_reports_minutes(tmp_path: Path):
    registry = _coder_registry(tmp_path)
    session = await registry.start(cwd=str(tmp_path), budget={"max_minutes": 0.002})
    events = session.subscribe()
    try:
        await session.send_user_message("slow\nHANG")
        notice = await _next(events, lambda e: e.get("type") == "fcc_budget")
        assert notice["kind"] == "time" and notice["limit"] == 0.002
        assert isinstance(notice["used"], float) and notice["used"] < 1
        await _next(events, lambda e: e.get("type") == "result")
    finally:
        await events.aclose()
        await registry.stop_all()


@pytest.mark.asyncio
async def test_usage_accumulates_and_turn_budget_triggers(tmp_path: Path):
    registry = _coder_registry(tmp_path)
    session = await registry.start(cwd=str(tmp_path), budget={"max_turns": 1})
    events = session.subscribe()
    try:
        await session.send_user_message("one\nUSAGE 7")
        await _next(events, lambda e: e.get("type") == "result")
        assert session.usage()["output_tokens"] == 7
        await session.send_user_message("two")
        notice = await _next(events, lambda e: e.get("type") == "fcc_budget")
        assert notice["kind"] == "turns" and notice["used"] == 2
    finally:
        await events.aclose()
        await registry.stop_all()


@pytest.mark.asyncio
async def test_observer_sees_events_and_failures_are_contained(tmp_path: Path):
    seen: list[str] = []

    def observer(session: InteractiveClaudeSession, event: dict) -> None:
        seen.append(str(event.get("type")))
        if event.get("type") == "result":
            session.publish({"type": "fcc_note"})
            raise RuntimeError("observer bug")

    registry = _coder_registry(tmp_path, observer=observer)
    session = await registry.start(cwd=str(tmp_path))
    events = session.subscribe()
    try:
        await session.send_user_message("hi")
        await _next(events, lambda e: e.get("type") == "fcc_note")
        await _next(events, lambda e: e.get("type") == "fcc_state" and not e["busy"])
        assert {"fcc_state", "fcc_user", "assistant", "result"} <= set(seen)
    finally:
        await events.aclose()
        await registry.stop_all()


def test_plan_approval_mode_switch_is_clamped_to_the_preset():
    def session(preset_mode: str | None) -> InteractiveClaudeSession:
        return InteractiveClaudeSession(
            live_id="l",
            cwd=".",
            permission_mode="plan",
            model=None,
            resume_session_id=None,
            policy_preset="restricted" if preset_mode else None,
            preset_permission_mode=preset_mode,
        )

    decision: JsonObject = {
        "behavior": "allow",
        "updatedPermissions": [
            {"type": "setMode", "mode": "acceptEdits", "destination": "session"},
            {"type": "addRules", "rules": []},
        ],
    }
    clamped = session("dontAsk")._clamp_mode_updates(decision)
    assert clamped["updatedPermissions"] == [
        {"type": "setMode", "mode": "dontAsk", "destination": "session"},
        {"type": "addRules", "rules": []},
    ]
    assert session(None)._clamp_mode_updates(decision) is decision
    stricter: JsonObject = {
        "behavior": "allow",
        "updatedPermissions": [{"type": "setMode", "mode": "plan"}],
    }
    assert session("acceptEdits")._clamp_mode_updates(stricter) == stricter


def test_claude_config_dir_env_override_else_app_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert transcripts.claude_config_dir() == tmp_path / ".fcc" / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "custom"))
    assert transcripts.claude_config_dir() == tmp_path / "custom"
    assert transcripts.claude_projects_dir() == tmp_path / "custom" / "projects"


def test_real_claude_home_transcripts_are_not_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    entry: list[dict | str] = [{"type": "user", "message": {"content": "prompt"}}]
    _write_jsonl(
        tmp_path / ".claude" / "projects" / "p" / "real-session-1.jsonl", entry
    )
    assert transcripts.list_transcripts() == []
    _write_jsonl(
        tmp_path / ".fcc" / "claude" / "projects" / "p" / "app-session-1.jsonl", entry
    )
    assert [t.session_id for t in transcripts.list_transcripts()] == ["app-session-1"]
    assert transcripts.find_transcript("real-session-1") is None


@pytest.mark.asyncio
async def test_spawn_env_uses_app_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    real_spawn = asyncio.create_subprocess_exec
    seen: list[dict[str, str]] = []

    async def spy(*args, **kwargs):
        seen.append(kwargs["env"])
        return await real_spawn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    registry = _registry(tmp_path)
    await registry.start(cwd=str(tmp_path))
    await registry.stop_all()
    app_dir = tmp_path / ".fcc" / "claude"
    assert app_dir.is_dir()
    assert seen[0]["CLAUDE_CONFIG_DIR"] == str(app_dir)
