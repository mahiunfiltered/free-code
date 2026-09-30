"""Adversarial intent-compiler corpus: deterministic rules, model add-only merge, firewall."""

import asyncio

import pytest

from free_claude_code.workbench.intent import (
    IntentContract,
    compile_intent,
    extract_deterministic,
    path_glob,
    render_contract,
    requirement_rows,
)


class FakeModel:
    def __init__(
        self, reply: str = "", *, exc: Exception | None = None, delay: float = 0
    ) -> None:
        self.reply, self.exc, self.delay = reply, exc, delay
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc
        return self.reply


def c(text: str) -> IntentContract:
    return extract_deterministic(text).contract


# --------------------------------------------------------------------------- deterministic corpus


def test_dont_change_yields_must_not_and_derived_preserve():
    k = c("Add a --verbose flag to the CLI. Don't change the public API.")
    assert k.must == ["Add a --verbose flag to the CLI"]
    assert k.must_not == ["change the public API"]
    assert k.preserve == ["the public API"]
    assert "diff_scope" in k.verification_requirements


def test_without_splits_main_clause_and_prohibition():
    k = c("Add caching without touching src/auth/")
    assert k.must == ["Add caching"]
    assert k.must_not == ["touching src/auth/"]
    assert k.scope.protected_paths == ["src/auth/**"]


def test_never_commit_and_dont_push_are_prohibited_ops():
    k = c("Fix the login bug, never commit and don't push")
    assert k.must == ["Fix the login bug"]
    assert set(k.scope.prohibited_ops) == {"git.commit", "git.push"}


def test_no_new_dependencies_and_no_migrations():
    k = c("Refactor the parser; no new dependencies; no migrations")
    assert "new dependencies" in k.must_not and "migrations" in k.must_not
    assert k.scope.prohibited_ops == ["dependency.add", "db.migration"]


def test_keep_unchanged_preserves_and_protects_path():
    k = c("Rename the helper and keep tests/test_cli.py unchanged")
    assert k.preserve == ["tests/test_cli.py"]
    assert k.scope.protected_paths == ["tests/test_cli.py"]


def test_keep_passing_preserves_without_protecting_path():
    k = c("Fix the bug. Keep tests/test_x.py passing")
    assert k.preserve == ["tests/test_x.py passing"]
    assert k.scope.protected_paths == []


def test_leave_alone_splits_on_comma_before_imperative():
    k = c("Leave the README alone, update docs/guide.md")
    assert k.must == ["update docs/guide.md"]
    assert k.must_not == ["Leave the README alone"]
    assert k.preserve == ["the README"]


def test_keep_with_and_is_not_split_into_a_must():
    k = c("Keep the header and footer")
    assert k.preserve == ["the header and footer"]
    assert k.must == []


def test_only_modify_multiple_files_and_directories():
    k = c("Only modify src/app.py and src/util.py. Add input validation.")
    assert k.scope.allowed_paths == ["src/app.py", "src/util.py"]
    assert k.must == ["Add input validation"]
    k2 = c("Only touch files under lib/ and add tests")
    assert k2.scope.allowed_paths == ["lib/**"]
    assert k2.must == ["add tests"]


def test_negated_scope_is_not_an_allowed_scope():
    k = c("Don't only modify the tests")
    assert k.scope.allowed_paths == []
    assert k.must_not == ["only modify the tests"]


def test_should_must_optional_buckets():
    k = c(
        "The API must return 404 for missing users; it should log the miss; optionally add metrics"
    )
    assert k.must == ["return 404 for missing users"]
    assert k.should == ["log the miss"]
    assert k.optional == ["add metrics"]


def test_dotfiles_and_bare_filenames_protected_anywhere():
    k = c("Update the handler but don't modify .env or config.yaml")
    assert set(k.scope.protected_paths) == {"**/.env", "**/config.yaml"}


def test_vague_request_blocks_for_clarification():
    r = extract_deterministic("make it faster")
    assert r.status == "blocked_for_clarification"
    assert r.questions and r.contract.unknowns == r.questions


def test_destructive_unspecific_is_critical_and_blocked():
    r = extract_deterministic("delete the old data")
    assert r.contract.risk == "critical"
    assert r.status == "blocked_for_clarification"


@pytest.mark.parametrize(
    ("text", "risk"),
    [
        ("Rotate the production database credentials", "high"),
        ("Translate the docs to French, do not edit code blocks", "low"),
        ("Add a health endpoint", "medium"),
    ],
)
def test_risk_heuristics(text: str, risk: str):
    assert c(text).risk == risk


def test_empty_and_whitespace_input_do_not_crash():
    r = extract_deterministic("   ")
    assert r.contract.must == [] and r.status == "ready_to_lock"


def test_path_glob_forms():
    assert path_glob("src/legacy") == "src/legacy/**"
    assert path_glob("src/legacy/") == "src/legacy/**"
    assert path_glob("./src/a.py") == "src/a.py"
    assert path_glob("a.py") == "**/a.py"
    assert path_glob("src/*.ts") == "src/*.ts"


def test_source_hash_ignores_whitespace_and_contract_roundtrips():
    a, b = c("Add  X.\n"), c("Add X.")
    assert a.source_text_hash == b.source_text_hash
    k = c("Only modify src/ and add tests; don't push")
    assert IntentContract.from_json(k.to_json()) == k


def test_from_json_tolerates_garbage():
    k = IntentContract.from_json(
        {"goal": "g", "must": "nope", "risk": "extreme", "version": "2", "scope": 3}
    )
    assert k.must == [] and k.risk == "medium" and k.version == 1


def test_render_contract_is_verbatim_and_has_rules():
    k = c("Add a flag. Don't change the public API. Only modify src/")
    block = render_contract(k)
    assert "- M1: Add a flag" in block
    assert "- N1: change the public API" in block
    assert "- P1: the public API" in block
    assert "only modify src/**" in block
    assert "smallest change" in block
    assert [r[0] for r in requirement_rows(k)] == ["M1", "N1", "P1"]


# --------------------------------------------------------------------------- model merge


@pytest.mark.asyncio
async def test_model_may_only_add_items():
    model = FakeModel(
        '{"must": ["Add a --verbose flag", "Document the flag in README"], "must_not": [],'
        ' "preserve": [], "acceptance_criteria": ["--verbose prints debug lines"],'
        ' "scope": {"protected_paths": ["secrets/"]}}'
    )
    r = await compile_intent(
        "Add a --verbose flag. Don't change the public API.", model
    )
    k = r.contract
    assert k.must == ["Add a --verbose flag", "Document the flag in README"]
    assert k.must_not == ["change the public API"]  # untouched despite empty model list
    assert k.preserve == ["the public API"]
    assert "secrets/**" in k.scope.protected_paths
    assert "--verbose prints debug lines" in k.acceptance_criteria
    assert not r.degraded and model.calls


@pytest.mark.asyncio
async def test_model_contradiction_is_kept_with_warning():
    model = FakeModel('{"must": ["add new dependencies for parsing"]}')
    r = await compile_intent("Rewrite the parser. No new dependencies.", model)
    assert "new dependencies" in r.contract.must_not
    assert "add new dependencies for parsing" in r.contract.must
    assert any("contradicts" in w for w in r.warnings)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model",
    [
        FakeModel("sorry, I cannot help"),
        FakeModel('{"must": "not a list"}'),
        FakeModel('{"assumptions": [{"text": "x", "impact": "enormous"}]}'),
        FakeModel("[1, 2, 3]"),
        FakeModel(exc=RuntimeError("provider down")),
        FakeModel("{}", delay=5),
    ],
)
async def test_invalid_model_output_degrades_to_deterministic(model: FakeModel):
    text = "Add a health endpoint; don't push"
    r = await compile_intent(text, model, timeout=0.2)
    assert r.degraded
    assert r.warnings and "degraded" in r.warnings[0]
    assert r.contract == extract_deterministic(text).contract


@pytest.mark.asyncio
async def test_json_in_prose_is_accepted():
    r = await compile_intent(
        "Add X", FakeModel('Here you go:\n```json\n{"should": ["add docs"]}\n```')
    )
    assert r.contract.should == ["add docs"] and not r.degraded


@pytest.mark.asyncio
async def test_assumption_firewall_blocks_high_and_irreversible_medium():
    reply = (
        '{"assumptions": [{"text": "use postgres", "impact": "high", "reversible": true},'
        ' {"text": "drop old column", "impact": "medium", "reversible": false},'
        ' {"text": "name it foo", "impact": "low", "reversible": true}]}'
    )
    r = await compile_intent("Add a users table", FakeModel(reply))
    assert r.status == "blocked_for_clarification"
    assert r.questions == [
        "Confirm assumption: use postgres",
        "Confirm assumption: drop old column",
    ]
    assert len(r.contract.assumptions) == 3
    allowed = await compile_intent(
        "Add a users table", FakeModel(reply), authorize_defaults=True
    )
    assert allowed.status == "ready_to_lock"


@pytest.mark.asyncio
async def test_model_ambiguity_blocks_and_risk_only_rises():
    r = await compile_intent(
        "Add a health endpoint",
        FakeModel(
            '{"ambiguities": [{"question": "Which port?", "impact": "high"}], "risk": "low"}'
        ),
    )
    assert r.status == "blocked_for_clarification" and r.questions == ["Which port?"]
    assert r.contract.risk == "medium"  # model cannot lower deterministic risk
    r2 = await compile_intent(
        "Add a health endpoint", FakeModel('{"risk": "critical"}')
    )
    assert r2.contract.risk == "critical"


@pytest.mark.asyncio
async def test_model_free_text_ops_become_must_not_items():
    model = FakeModel(
        '{"scope": {"prohibited_ops": ["git.push", "modify test files", "git.push"]}}'
    )
    r = await compile_intent("Fix add() in calc.py", model)
    assert r.contract.scope.prohibited_ops == ["git.push"]
    assert "modify test files" in r.contract.must_not


# --------------------------------------------------------------------------- scope-shaped must-not


TASK = "Fix add() in calc.py. Only modify calc.py and tests/test_calc.py."


@pytest.mark.asyncio
async def test_model_outside_rule_becomes_allowed_scope_not_must_not():
    model = FakeModel(
        '{"must_not": ["Modify any file outside calc.py and tests/test_calc.py",'
        ' "Delete the README"]}'
    )
    r = await compile_intent("Fix add() in calc.py", model)
    k = r.contract
    assert "Modify any file outside calc.py and tests/test_calc.py" not in k.must_not
    assert "Delete the README" in k.must_not  # unrelated model items still added
    assert k.scope.allowed_paths == ["**/calc.py", "tests/test_calc.py"]
    assert not k.scope.protected_paths
    assert any("scope restriction" in w for w in r.warnings)


@pytest.mark.asyncio
async def test_model_must_not_forbidding_user_allowed_path_is_dropped():
    model = FakeModel(
        '{"must_not": ["modify tests/test_calc.py", "touch setup.cfg"],'
        ' "scope": {"protected_paths": ["calc.py", "docs/"]}}'
    )
    r = await compile_intent(TASK, model)
    k = r.contract
    assert "modify tests/test_calc.py" not in k.must_not
    assert "touch setup.cfg" in k.must_not
    assert "**/calc.py" not in k.scope.protected_paths
    assert "docs/**" in k.scope.protected_paths
    assert (
        sum("user allowed" in w or "explicitly allowed" in w for w in r.warnings) == 2
    )


def test_user_outside_phrasing_allows_rather_than_protects():
    k = extract_deterministic(
        "Fix add(). Don't modify anything outside calc.py"
    ).contract
    assert k.must_not == ["modify anything outside calc.py"]
    assert k.scope.allowed_paths == ["**/calc.py"]
    assert k.scope.protected_paths == []


@pytest.mark.asyncio
async def test_model_directory_must_not_covering_allowed_file_is_dropped():
    r = await compile_intent(TASK, FakeModel('{"must_not": ["modify tests/"]}'))
    assert "modify tests/" not in r.contract.must_not
    assert any("explicitly allowed" in w for w in r.warnings)
