"""Permission-mode strictness and the preset-vs-requested resolution rule."""

import pytest

from free_claude_code.core.claude_permission_modes import (
    PERMISSION_MODES,
    effective_permission_mode,
    strictness,
)


def test_modes_are_ordered_least_to_most_strict():
    assert PERMISSION_MODES == (
        "bypassPermissions",
        "auto",
        "acceptEdits",
        "default",
        "dontAsk",
        "plan",
    )
    assert strictness("plan") > strictness("dontAsk") > strictness("default")
    with pytest.raises(ValueError):
        strictness("yolo")


@pytest.mark.parametrize(
    ("requested", "preset", "expected"),
    [
        (None, None, "default"),
        ("auto", None, "auto"),
        (None, "dontAsk", "dontAsk"),
        (None, "acceptEdits", "acceptEdits"),
        ("bypassPermissions", "acceptEdits", "acceptEdits"),  # looser: preset wins
        ("default", "acceptEdits", "default"),  # stricter: user wins
        ("default", "dontAsk", "dontAsk"),
        ("plan", "dontAsk", "plan"),  # plan is always allowed
        ("plan", "bypassPermissions", "plan"),
        ("acceptEdits", "acceptEdits", "acceptEdits"),
    ],
)
def test_effective_mode_is_the_preset_unless_a_stricter_one_is_asked(
    requested: str | None, preset: str | None, expected: str
):
    assert effective_permission_mode(requested, preset) == expected
