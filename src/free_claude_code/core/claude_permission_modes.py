"""Claude Code permission modes accepted by FCC chat sessions.

``PERMISSION_MODES`` is ordered from least to most strict:

- ``bypassPermissions``: never asks (explicit ask/deny rules still apply).
- ``auto``: a classifier approves whatever it deems safe, edits and commands alike.
- ``acceptEdits``: file edits apply; commands still ask.
- ``default``: every edit and command asks.
- ``dontAsk``: anything that would ask is denied; only allow-listed tools run.
- ``plan``: read-only exploration; nothing is changed or executed.
"""

PERMISSION_MODES = (
    "bypassPermissions",
    "auto",
    "acceptEdits",
    "default",
    "dontAsk",
    "plan",
)


def strictness(mode: str) -> int:
    """Rank of ``mode`` in ``PERMISSION_MODES`` (higher is stricter); ValueError if unknown."""

    return PERMISSION_MODES.index(mode)


def effective_permission_mode(requested: str | None, preset_mode: str | None) -> str:
    """Mode a chat runs in: a policy preset's mode unless the user asked for a stricter one."""

    if preset_mode is None:
        return requested or "default"
    if requested is None or strictness(requested) < strictness(preset_mode):
        return preset_mode
    return requested
