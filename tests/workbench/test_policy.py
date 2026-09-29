import json
from pathlib import Path

import pytest

from free_claude_code.workbench.policy import (
    DESTRUCTIVE_COMMANDS,
    PolicyError,
    Preset,
    Rule,
    builtin_rules,
    compile_policy,
    explain,
    load_project_rules,
    validate_rule,
)


def _perms(
    preset: str, *, branch: str | None = None, project_rules: list[Rule] | None = None
) -> dict:
    compiled = compile_policy(preset, branch=branch, project_rules=project_rules)
    return json.loads(compiled.settings_json())["permissions"]


@pytest.mark.parametrize(
    ("preset", "mode"),
    [
        ("restricted", "dontAsk"),
        ("workspace", "acceptEdits"),
        ("privileged", "bypassPermissions"),
    ],
)
def test_preset_modes(preset: str, mode: str) -> None:
    compiled = compile_policy(preset)
    assert compiled.permission_mode == mode
    assert _perms(preset)["defaultMode"] == mode
    assert json.loads(compiled.settings_json()) == compiled.settings


@pytest.mark.parametrize("preset", list(Preset))
def test_builtin_rules_always_present(preset: Preset) -> None:
    perms = _perms(preset)
    for rule in builtin_rules():
        if rule.decision == "ask":
            # restricted may upgrade a builtin ask to deny; it never drops it.
            assert rule.rule in perms["ask"]
        else:
            assert rule.rule in perms["deny"]
    assert "Read(//**/.env*)" in perms["ask"]
    assert "Read(//**/id_ed25519*)" in perms["ask"]
    assert "Edit(//**/.git/**)" in perms["deny"]
    assert "Bash(git push *)" in perms["ask"]
    assert "PowerShell(git push *)" in perms["ask"]
    assert "Bash(git push *--force*)" in perms["deny"]
    assert "Bash(git push -f *)" in perms["deny"]
    for name in DESTRUCTIVE_COMMANDS:
        assert f"Bash({name} *)" in perms["ask"]
        assert f"PowerShell({name} *)" in perms["ask"]


def test_restricted_is_read_only() -> None:
    perms = _perms("restricted")
    assert {"Edit", "Write", "NotebookEdit"} <= set(perms["deny"])
    assert {"Read", "Grep", "Glob", "Bash(git status *)"} <= set(perms["allow"])
    assert "Bash(rm *)" in perms["deny"]
    assert "Bash(curl *)" in perms["deny"]
    assert perms["disableBypassPermissionsMode"] == "disable"


def test_workspace_allows_project_edits_and_asks_network() -> None:
    perms = _perms("workspace")
    assert perms["allow"] == ["Edit(./**)"]
    assert "Bash(curl *)" in perms["ask"]
    assert "PowerShell(Invoke-WebRequest *)" in perms["ask"]
    assert "disableBypassPermissionsMode" not in perms


def test_privileged_adds_no_allows() -> None:
    assert _perms("privileged")["allow"] == []


def test_protected_branch_asks_for_commits() -> None:
    assert "Bash(git commit *)" in _perms("workspace", branch="main")["ask"]
    assert (
        "PowerShell(git merge *)" in _perms("privileged", branch="release/1.0")["ask"]
    )
    assert "Bash(git commit *)" not in _perms("workspace", branch="feature/x")["ask"]


def test_project_allow_rules_rejected() -> None:
    with pytest.raises(PolicyError, match="only tighten"):
        compile_policy("workspace", project_rules=[Rule("allow", "Bash", "nope")])


def test_project_rules_tighten() -> None:
    perms = _perms(
        "privileged", project_rules=[Rule("deny", "Bash(npm publish *)", "p")]
    )
    assert "Bash(npm publish *)" in perms["deny"]


def test_unknown_preset() -> None:
    with pytest.raises(PolicyError, match="Unknown preset"):
        compile_policy("yolo")


@pytest.mark.parametrize("preset", list(Preset))
def test_all_generated_rules_are_valid_and_unique(preset: Preset) -> None:
    perms = _perms(preset)
    for decision in ("allow", "ask", "deny"):
        assert len(perms[decision]) == len(set(perms[decision]))
        for rule in perms[decision]:
            validate_rule(rule)


@pytest.mark.parametrize(
    "rule",
    [
        "Bash",
        "Bash(npm run *)",
        "Bash(ls:*)",
        "Read(./.env)",
        "Read(~/.ssh/**)",
        "Edit(//c/Users/me/**)",
        "Edit(/src/**/*.ts)",
        "WebFetch(domain:example.com)",
        "PowerShell(Remove-Item *)",
        "mcp__github__get_issue",
    ],
)
def test_valid_rules(rule: str) -> None:
    validate_rule(rule)


@pytest.mark.parametrize(
    "rule",
    [
        "",
        "Bash(",
        "Bassh(ls)",
        "Bash(git:* push)",
        "Bash(command:rm *)",
        "Bash( ls )",
        "Read(C:\\secrets\\**)",
        "Read(C:/secrets/**)",
        "Write(docs/**)",
        "NotebookEdit(nb/**)",
        "WebFetch(example.com)",
        "mcp__github__get(x)",
    ],
)
def test_invalid_rules(rule: str) -> None:
    with pytest.raises(PolicyError):
        validate_rule(rule)


def test_explain_lists_mode_and_rules_most_restrictive_first() -> None:
    lines = explain("workspace")
    assert lines[0] == "MODE  acceptEdits"
    decisions = [line.split()[0] for line in lines[1:]]
    assert decisions == sorted(decisions, key=["DENY", "ASK", "ALLOW"].index)
    assert any("Bash(git push *)" in line for line in lines)


def test_load_project_rules(tmp_path: Path) -> None:
    assert load_project_rules(tmp_path) == []
    policy = tmp_path / ".fcc" / "policy.json"
    policy.parent.mkdir()
    policy.write_text(
        json.dumps({"deny": ["Bash(npm publish *)"], "ask": ["Read(./secrets/**)"]})
    )
    rules = load_project_rules(tmp_path)
    assert {(r.decision, r.rule) for r in rules} == {
        ("deny", "Bash(npm publish *)"),
        ("ask", "Read(./secrets/**)"),
    }


@pytest.mark.parametrize(
    ("content", "match"),
    [
        ({"allow": ["Bash"]}, "may not contain allow"),
        ({"deny": "Bash"}, "list of strings"),
        ({"deny": ["Bogus(x)"]}, "Unknown tool"),
        ({"other": []}, "unknown keys"),
        ([], "JSON object"),
    ],
)
def test_load_project_rules_rejects_bad_files(
    tmp_path: Path, content: object, match: str
) -> None:
    policy = tmp_path / ".fcc" / "policy.json"
    policy.parent.mkdir()
    policy.write_text(json.dumps(content))
    with pytest.raises(PolicyError, match=match):
        load_project_rules(tmp_path)


def test_load_project_rules_rejects_invalid_json(tmp_path: Path) -> None:
    policy = tmp_path / ".fcc" / "policy.json"
    policy.parent.mkdir()
    policy.write_text("{nope")
    with pytest.raises(PolicyError, match="Cannot read"):
        load_project_rules(tmp_path)
