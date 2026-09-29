"""Project profile detection per ecosystem (never invent commands) and risk-based plans."""

import json
from pathlib import Path

import pytest

from free_claude_code.workbench.intent import IntentContract, Scope
from free_claude_code.workbench.verification.profile import (
    HttpProbe,
    ProjectProfile,
    detect_profile,
    plan_checks,
)


def write(root: Path, name: str, text: str) -> None:
    (root / name).parent.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(text, encoding="utf-8")


def test_node_scripts_and_package_manager(tmp_path: Path):
    write(
        tmp_path,
        "package.json",
        json.dumps(
            {
                "scripts": {
                    "test": "vitest",
                    "build": "vite build",
                    "lint": "eslint .",
                    "dev": "vite",
                }
            }
        ),
    )
    write(tmp_path, "pnpm-lock.yaml", "")
    p = detect_profile(tmp_path)
    assert p.ecosystem == "node"
    assert p.commands == {
        "build": ["pnpm", "run", "build"],
        "lint": ["pnpm", "run", "lint"],
        "test": ["pnpm", "run", "test"],
    }


def test_node_typecheck_from_declared_typescript_only(tmp_path: Path):
    write(
        tmp_path, "package.json", json.dumps({"devDependencies": {"typescript": "^5"}})
    )
    assert detect_profile(tmp_path).commands == {}  # no tsconfig -> nothing
    write(tmp_path, "tsconfig.json", "{}")
    assert detect_profile(tmp_path).commands == {
        "typecheck": ["npx", "tsc", "--noEmit"]
    }
    write(
        tmp_path,
        "package.json",
        json.dumps(
            {
                "scripts": {"typecheck": "tsc -b"},
                "devDependencies": {"typescript": "^5"},
            }
        ),
    )
    assert detect_profile(tmp_path).commands == {
        "typecheck": ["npm", "run", "typecheck"]
    }


def test_node_without_scripts_invents_nothing(tmp_path: Path):
    write(
        tmp_path,
        "package.json",
        json.dumps({"name": "x", "dependencies": {"jest": "1"}}),
    )
    assert detect_profile(tmp_path).commands == {}


def test_invalid_package_json_falls_through(tmp_path: Path):
    write(tmp_path, "package.json", "{not json")
    assert detect_profile(tmp_path).ecosystem == "none"


def test_python_uv_with_pytest_ruff_ty(tmp_path: Path):
    write(
        tmp_path,
        "pyproject.toml",
        '[project]\nname="x"\n[dependency-groups]\ndev = ["pytest>=8", "ruff>=0.5", "ty>=0.0.1"]\n',
    )
    write(tmp_path, "uv.lock", "")
    p = detect_profile(tmp_path)
    assert p.ecosystem == "python"
    assert p.commands == {
        "test": ["uv", "run", "pytest"],
        "lint": ["uv", "run", "ruff", "check", "."],
        "typecheck": ["uv", "run", "ty", "check"],
    }


def test_python_poetry_mypy_via_tool_table(tmp_path: Path):
    write(
        tmp_path,
        "pyproject.toml",
        '[tool.poetry]\nname="x"\n[tool.mypy]\nstrict=true\n',
    )
    write(tmp_path, "tests/conftest.py", "")
    p = detect_profile(tmp_path)
    assert p.commands == {
        "test": ["poetry", "run", "pytest"],
        "typecheck": ["poetry", "run", "mypy", "."],
    }


def test_python_requirements_only_and_no_false_matches(tmp_path: Path):
    write(
        tmp_path, "requirements.txt", "pytest-cov==1\nrequests\n"
    )  # pytest-cov is not pytest
    assert detect_profile(tmp_path).commands == {}
    write(tmp_path, "requirements-dev.txt", "pytest==8.0\n")
    assert detect_profile(tmp_path).commands == {"test": ["python", "-m", "pytest"]}


def test_python_broken_toml_still_detects_declared_deps(tmp_path: Path):
    write(tmp_path, "pyproject.toml", 'dependencies = ["pytest"\n[[[')
    assert detect_profile(tmp_path).commands["test"][-1] == "pytest"


def test_go_and_rust(tmp_path: Path):
    write(tmp_path / "go", "go.mod", "module x\n")
    assert detect_profile(tmp_path / "go").commands == {
        "build": ["go", "build", "./..."],
        "lint": ["go", "vet", "./..."],
        "test": ["go", "test", "./..."],
    }
    write(tmp_path / "rs", "Cargo.toml", "[package]\nname='x'\n")
    assert detect_profile(tmp_path / "rs").commands == {
        "typecheck": ["cargo", "check"],
        "test": ["cargo", "test"],
    }


def test_unknown_project(tmp_path: Path):
    p = detect_profile(tmp_path)
    assert p.ecosystem == "none" and p.commands == {} and p.notes


# --------------------------------------------------------------------------- plans

FULL = ProjectProfile(
    ".", "python", {"typecheck": ["t"], "test": ["u"], "build": ["b"], "lint": ["l"]}
)


def kinds(plan) -> dict[str, bool]:
    return {c.id: c.required for c in plan.checks}


def test_low_risk_is_minimal():
    plan = plan_checks(IntentContract(goal="g", risk="low"), FULL)
    assert plan.level == "minimal"
    assert kinds(plan) == {"typecheck": True, "unit_test": True, "secret_scan": True}


def test_medium_risk_is_standard_with_advisory_lint():
    plan = plan_checks(IntentContract(goal="g", risk="medium"), FULL)
    assert plan.level == "standard"
    assert kinds(plan) == {
        "typecheck": True, "unit_test": True, "build": True, "lint": False,
        "secret_scan": True, "diff_scope": True, "intent_conformance": True,
    }  # fmt: skip


@pytest.mark.parametrize("risk", ["high", "critical"])
def test_high_risk_is_deep_and_lint_required(risk):
    plan = plan_checks(
        IntentContract(goal="g", risk=risk),
        FULL,
        http_probes=[HttpProbe("http://x/health")],
    )
    assert plan.level == "deep" and kinds(plan)["lint"] is True
    assert plan.checks[-1].probe == HttpProbe("http://x/health")


@pytest.mark.parametrize(
    "contract",
    [
        IntentContract(goal="g", risk="low", must_not=["push"]),
        IntentContract(goal="g", risk="low", preserve=["the API"]),
        IntentContract(goal="g", risk="low", scope=Scope(allowed_paths=["src/**"])),
    ],
)
def test_constraints_force_diff_scope_and_conformance(contract):
    plan = plan_checks(contract, FULL)
    assert plan.level == "minimal"
    assert {"diff_scope", "intent_conformance"} <= set(kinds(plan))


def test_level_floor_raises_but_never_lowers():
    assert (
        plan_checks(IntentContract(goal="g", risk="low"), FULL, level="deep").level
        == "deep"
    )
    assert (
        plan_checks(IntentContract(goal="g", risk="high"), FULL, level="minimal").level
        == "deep"
    )


def test_missing_commands_are_reasons_not_inventions():
    plan = plan_checks(
        IntentContract(goal="g", risk="medium"), ProjectProfile(".", "none")
    )
    assert [c.id for c in plan.checks] == [
        "secret_scan",
        "diff_scope",
        "intent_conformance",
    ]
    assert any("declares no test command" in r for r in plan.reasons)
