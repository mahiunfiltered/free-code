"""FCC-owned per-request context shared from API ingress to provider adapters."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_claude_session_id: ContextVar[str | None] = ContextVar(
    "fcc_claude_session_id", default=None
)


def current_claude_session_id() -> str | None:
    """Claude Code session id of the request being served, if the client sent one."""
    return _claude_session_id.get()


@contextmanager
def bind_claude_session_id(session_id: str | None) -> Iterator[None]:
    """Bind the Claude session id for the enclosed request scope."""
    token = _claude_session_id.set(session_id)
    try:
        yield
    finally:
        _claude_session_id.reset(token)
