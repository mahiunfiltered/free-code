"""Task store: lifecycle transitions, gate-only VERIFIED, evidence, events, export."""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from free_claude_code.core.storage import Store
from free_claude_code.workbench.checkpoints import Checkpoint
from free_claude_code.workbench.intent import IntentContract
from free_claude_code.workbench.tasks import TaskStatus, TaskStore, TransitionError
from free_claude_code.workbench.verification.checks import (
    CheckResult,
    CheckStatus,
    GapRow,
    RowStatus,
)
from free_claude_code.workbench.verification.gate import (
    Disposition,
    EvidencePackage,
    compute_disposition,
)

CONTRACT = IntentContract(goal="g", must=["do x"], risk="low")


def package(
    check_status: CheckStatus = "passed",
    row_status: RowStatus = "covered",
    claim: Disposition | None = None,
) -> EvidencePackage:
    checks = [CheckResult("unit_test", "unit_test", True, check_status, "s")]
    rows = [GapRow("M1", "must", "do x", row_status)]
    disposition, reasons = compute_disposition(checks, rows)
    return EvidencePackage(
        contract=CONTRACT, level="minimal", disposition=claim or disposition,
        blocking_reasons=reasons, checks=checks, gap_matrix=rows, failures=[],
        warnings=[], base_commit="abc", checkpoint_commit="def", head_commit="abc",
        snapshot_tree="t", diff_sha256="h", diff_stat=" a.py | 1 +", changed_files=["a.py"],
        started_at="s", completed_at="c",
    )  # fmt: skip


@pytest.fixture
def tasks(tmp_path: Path) -> Iterator[TaskStore]:
    store = Store(tmp_path / "fcc.db")
    yield TaskStore(store)
    store.close()


def to_verifying(tasks: TaskStore) -> str:
    task_id = tasks.create("sess-1", "/repo")
    steps: tuple[TaskStatus, ...] = (
        "INTENT_COMPILED",
        "REQUIREMENTS_LOCKED",
        "RUNNING",
        "VERIFYING",
    )
    for status in steps:
        tasks.transition(task_id, status)
    return task_id


def test_happy_path_to_verified(tasks: TaskStore):
    task_id = to_verifying(tasks)
    assert tasks.record_evidence(task_id, package()) == "VERIFIED"
    assert tasks.get(task_id).status == "VERIFIED"
    [stored] = tasks.evidence(task_id)
    assert stored["disposition"] == "VERIFIED"
    types = [e.type for e in tasks.events(task_id)]
    assert types[0] == "task.received" and types[-1] == "verification.completed"
    with pytest.raises(TransitionError):
        tasks.transition(task_id, "RUNNING")  # terminal


@pytest.mark.parametrize("target", ["VERIFIED", "FAILED_VERIFICATION"])
def test_non_gate_cannot_set_gate_statuses(tasks: TaskStore, target):
    task_id = to_verifying(tasks)
    with pytest.raises(TransitionError, match="verification gate"):
        tasks.transition(task_id, target)
    assert tasks.get(task_id).status == "VERIFYING"


def test_forged_disposition_is_rejected(tasks: TaskStore):
    task_id = to_verifying(tasks)
    with pytest.raises(TransitionError, match="claims VERIFIED"):
        tasks.record_evidence(task_id, package(check_status="failed", claim="VERIFIED"))
    assert tasks.evidence(task_id) == [] and tasks.get(task_id).status == "VERIFYING"


def test_evidence_only_from_verifying(tasks: TaskStore):
    task_id = tasks.create("s", "/r")
    with pytest.raises(
        TransitionError, match="illegal transition RECEIVED -> VERIFIED"
    ):
        tasks.record_evidence(task_id, package())
    assert tasks.evidence(task_id) == []


def test_illegal_transitions(tasks: TaskStore):
    task_id = tasks.create("s", "/r")
    with pytest.raises(TransitionError):
        tasks.transition(task_id, "RUNNING")
    with pytest.raises(KeyError):
        tasks.transition("nope", "CANCELLED")
    tasks.transition(task_id, "CANCELLED", "user stopped")
    with pytest.raises(TransitionError):
        tasks.transition(task_id, "FAILED")


def test_failure_recovery_loop_and_needs_review(tasks: TaskStore):
    task_id = to_verifying(tasks)
    assert (
        tasks.record_evidence(
            task_id, package(check_status="failed", row_status="failed")
        )
        == "FAILED_VERIFICATION"
    )
    tasks.transition(task_id, "RECOVERING")
    tasks.transition(task_id, "VERIFYING")
    assert (
        tasks.record_evidence(task_id, package(row_status="missing"))
        == "RECOVERY_REQUIRED"
    )
    assert [p["disposition"] for p in tasks.evidence(task_id)] == [
        "FAILED_VERIFICATION",
        "NEEDS_REVIEW",
    ]
    tasks.transition(task_id, "RUNNING")
    tasks.transition(task_id, "VERIFYING")
    assert tasks.record_evidence(task_id, package()) == "VERIFIED"
    assert len(tasks.evidence(task_id)) == 3


def test_blocked_for_clarification_path(tasks: TaskStore):
    task_id = tasks.create("s", "/r")
    tasks.transition(task_id, "INTENT_COMPILED")
    tasks.transition(task_id, "BLOCKED_FOR_CLARIFICATION")
    tasks.transition(task_id, "INTENT_COMPILED")
    tasks.transition(task_id, "REQUIREMENTS_LOCKED")
    assert tasks.get(task_id).status == "REQUIREMENTS_LOCKED"


def test_attach_persists_and_survives_reopen(tmp_path: Path):
    path = tmp_path / "fcc.db"
    first = Store(path)
    tasks = TaskStore(first)
    task_id = tasks.create("sess", "/repo")
    cp = Checkpoint("cp1", "/repo", "/repo", "abc", "def", "now")
    tasks.attach(task_id, contract=CONTRACT, checkpoint=cp)
    tasks.add_event(task_id, "agent.turn", {"n": 1})
    first.close()
    second = Store(path)
    reopened = TaskStore(second)
    record = reopened.get(task_id)
    assert record.contract == CONTRACT and record.checkpoint == cp
    assert [t.id for t in reopened.for_session("sess")] == [task_id]
    assert reopened.events(task_id)[-1].payload == {"n": 1}
    with pytest.raises(KeyError):
        reopened.get("missing")
    with pytest.raises(KeyError):
        reopened.attach("missing", contract=CONTRACT)
    second.close()


def test_export_json_and_markdown(tasks: TaskStore):
    task_id = to_verifying(tasks)
    with pytest.raises(KeyError):
        tasks.export(task_id)
    tasks.record_evidence(task_id, package())
    assert json.loads(tasks.export(task_id))["disposition"] == "VERIFIED"
    md = tasks.export(task_id, "markdown")
    assert (
        md.startswith("# Evidence package: VERIFIED")
        and "| M1 | must | do x | covered |" in md
    )
