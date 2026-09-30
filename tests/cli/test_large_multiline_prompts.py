r"""Tests for large and multiline prompt handling in Managed Claude sessions.

Verifies:
1. Short prompts (< 8192 chars) use CLI `-p <prompt>` args.
2. Large prompts (>= 8192 chars, 32KB, 64KB, 100KB, 250KB) bypass Windows CLI limits via stdin streaming.
3. Multiline prompts, code fences, newlines, tabs, and indentation are preserved verbatim.
4. Unicode, emojis, math symbols, and non-ASCII characters are preserved without corruption.
5. Windows special characters (&, |, <, >, ^, %, !, $, ", ', \) are safely piped without shell expansion.
6. ManagedClaudeSession.start_task accurately pipes stdin data and closes stdin.
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from free_claude_code.cli.managed.claude import (
    ManagedClaudeConfig,
    ManagedClaudeTaskRequest,
    build_managed_claude_invocation,
)
from free_claude_code.cli.managed.session import ManagedClaudeSession


def _config(**overrides: object) -> ManagedClaudeConfig:
    workspace_path = overrides.get("workspace_path", os.path.normpath("/tmp/workspace"))
    proxy_root_url = overrides.get("proxy_root_url", "http://localhost:8082")
    raw_allowed_dirs = overrides.get("allowed_dirs")
    allowed_dirs: list[str] = []
    if raw_allowed_dirs is not None:
        assert isinstance(raw_allowed_dirs, list)
        for directory in raw_allowed_dirs:
            assert isinstance(directory, str)
            allowed_dirs.append(directory)
    claude_bin = overrides.get("claude_bin", "claude")
    auth_token = overrides.get("auth_token", "proxy-token")

    assert isinstance(workspace_path, str)
    assert isinstance(proxy_root_url, str)
    assert isinstance(claude_bin, str)
    assert isinstance(auth_token, str)
    return ManagedClaudeConfig(
        workspace_path=workspace_path,
        proxy_root_url=proxy_root_url,
        allowed_dirs=allowed_dirs,
        claude_bin=claude_bin,
        auth_token=auth_token,
    )


def test_short_prompt_uses_command_line_argument() -> None:
    short_prompt = "Hello, please refactor this simple function."
    invocation = build_managed_claude_invocation(
        config=_config(),
        request=ManagedClaudeTaskRequest(prompt=short_prompt),
        base_env={},
    )

    assert invocation.prompt_input is None
    assert "-p" in invocation.argv
    p_idx = invocation.argv.index("-p")
    assert invocation.argv[p_idx + 1] == short_prompt


@pytest.mark.parametrize(
    "size_bytes", [8193, 32 * 1024, 64 * 1024, 100 * 1024, 250 * 1024]
)
def test_large_prompts_use_stdin_streaming(size_bytes: int) -> None:
    # Generate large prompt
    large_prompt = "A" * size_bytes
    invocation = build_managed_claude_invocation(
        config=_config(),
        request=ManagedClaudeTaskRequest(prompt=large_prompt),
        base_env={},
    )

    assert invocation.prompt_input == large_prompt
    assert "-p" in invocation.argv
    p_idx = invocation.argv.index("-p")
    # Inline large string is omitted from argv to avoid Windows 32KB command line limits
    assert invocation.argv[p_idx + 1] != large_prompt
    assert invocation.argv[p_idx + 1] == "--output-format"
    # Total argv length remains small and safe from Windows 32767 char limit
    total_argv_len = sum(len(arg) for arg in invocation.argv)
    assert total_argv_len < 500


def test_multiline_prompt_with_code_blocks_preserved() -> None:
    multiline_prompt = (
        "# Implementation Plan\n\n"
        "Here is the python code to inspect:\n\n"
        "```python\n"
        "def compute_metrics(data: list[float]) -> dict[str, float]:\n"
        '    """Calculate mean and standard deviation."""\n'
        "    if not data:\n"
        "        return {'mean': 0.0, 'std': 0.0}\n"
        "    mean = sum(data) / len(data)\n"
        "    variance = sum((x - mean) ** 2 for x in data) / len(data)\n"
        "    return {'mean': mean, 'std': variance ** 0.5}\n"
        "```\n\n"
        "Please review for performance optimizations."
    )

    invocation = build_managed_claude_invocation(
        config=_config(),
        request=ManagedClaudeTaskRequest(prompt=multiline_prompt),
        base_env={},
    )

    assert invocation.prompt_input is None
    p_idx = invocation.argv.index("-p")
    assert invocation.argv[p_idx + 1] == multiline_prompt


def test_unicode_and_emojis_preserved_intact() -> None:
    unicode_prompt = (
        "Multilingual test: 日本語, 한국어, 中文 (简体/繁體), العربية, Русский\n"
        "Emoji test: 🚀 🌟 🔥 🤖 💻 🧠 ⚡\n"
        "Math notation: ∫ f(x)dx = F(x) + C, ∑_{i=1}^n x_i, √2 ≈ 1.414, π ≈ 3.14159\n"
    ) * 300  # Expand to exceed 8KB

    invocation = build_managed_claude_invocation(
        config=_config(),
        request=ManagedClaudeTaskRequest(prompt=unicode_prompt),
        base_env={},
    )

    assert invocation.prompt_input == unicode_prompt
    p_idx = invocation.argv.index("-p")
    assert invocation.argv[p_idx + 1] == "--output-format"


def test_windows_special_characters_not_corrupted() -> None:
    special_prompt = (
        "Command line test with special shell metacharacters:\n"
        "echo %PATH% & dir /s | findstr /i test > output.txt < input.txt\n"
        'powershell -Command "Get-Process | Where-Object { $_.CPU -gt 10 }"\n'
        "delayed!expansion!test! ^carets^ and `backticks` and 'single' and \"double\" quotes\n"
        "Paths: C:\\Users\\Administrator\\AppData\\Local\\Temp\\test.log\n"
    ) * 100  # Expand to exceed 8KB

    invocation = build_managed_claude_invocation(
        config=_config(),
        request=ManagedClaudeTaskRequest(prompt=special_prompt),
        base_env={},
    )

    assert invocation.prompt_input == special_prompt
    p_idx = invocation.argv.index("-p")
    assert invocation.argv[p_idx + 1] == "--output-format"


@pytest.mark.asyncio
async def test_session_start_task_pipes_large_prompt_via_stdin() -> None:
    large_prompt = "Large prompt content to be sent via stdin" * 500
    mock_stdin = MagicMock()
    mock_stdin.write = MagicMock()
    mock_stdin.drain = AsyncMock()
    mock_stdin.close = MagicMock()
    mock_stdin.wait_closed = AsyncMock()

    mock_stdout = MagicMock()
    mock_stdout.read = AsyncMock(
        side_effect=[b'{"type":"init","session_id":"s1"}\n', b""]
    )
    mock_stdout.readline = AsyncMock(
        side_effect=[b'{"type":"init","session_id":"s1"}\n', b""]
    )

    mock_stderr = MagicMock()
    mock_stderr.read = AsyncMock(side_effect=[b""])
    mock_stderr.readline = AsyncMock(side_effect=[b""])

    mock_proc = MagicMock()
    mock_proc.pid = 12345
    mock_proc.stdin = mock_stdin
    mock_proc.stdout = mock_stdout
    mock_proc.stderr = mock_stderr
    mock_proc.returncode = None
    mock_proc.wait = AsyncMock(return_value=0)

    session = ManagedClaudeSession(
        workspace_path=os.path.normpath("/tmp/workspace"),
        proxy_root_url="http://localhost:8082",
        auth_token="token",
    )

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
        events = [event async for event in session.start_task(prompt=large_prompt)]

    mock_stdin.write.assert_called_once_with(large_prompt.encode("utf-8"))
    mock_stdin.drain.assert_awaited_once()
    mock_stdin.close.assert_called_once()
    mock_stdin.wait_closed.assert_awaited_once()
    assert any(e.get("session_id") == "s1" for e in events)
