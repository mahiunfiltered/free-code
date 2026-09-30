"""Durable verified-task records: status lifecycle, evidence and events (SQLite Store).

    store = TaskStore(Store(path))
    create(session_id, cwd) -> task_id                  # status RECEIVED
    attach(task_id, *, contract=None, checkpoint=None)
    transition(task_id, to, reason="")                  # rejects VERIFIED / FAILED_VERIFICATION
    record_evidence(task_id, package) -> TaskStatus     # the gate's only path to VERIFIED
    get(task_id) / for_session(session_id) / evidence(task_id) / events(task_id)
    export(task_id, fmt="json"|"markdown") -> str       # latest evidence package
    close_interrupted(reason) -> [(task_id, from, to)]  # startup: runs died with the server

Lifecycle (M0005): RECEIVED -> INTENT_COMPILED -> (BLOCKED_FOR_CLARIFICATION ->)
REQUIREMENTS_LOCKED -> RUNNING -> VERIFYING -> VERIFIED | FAILED_VERIFICATION |
RECOVERY_REQUIRED (gate said NEEDS_REVIEW); FAILED_VERIFICATION -> RECOVERING -> RUNNING/
VERIFYING; CANCELLED / FAILED from any non-terminal state. An Ultra run that is not
verified goes RECEIVED (-> ... ) -> RUNNING -> COMPLETED: done, but with no claim of
verification. VERIFIED and FAILED_VERIFICATION
are set only by record_evidence from a VERIFYING task, and only when the package's
disposition matches one recomputed from its own checks and gap matrix.
"""

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast

from free_claude_code.core.json_types import JsonObject
from free_claude_code.core.storage import Store
from free_claude_code.workbench.checkpoints import Checkpoint
from free_claude_code.workbench.intent import IntentContract
from free_claude_code.workbench.verification.gate import (
    EvidencePackage,
    compute_disposition,
    render_markdown,
)

type TaskStatus = Literal[
    "RECEIVED", "INTENT_COMPILED", "BLOCKED_FOR_CLARIFICATION", "REQUIREMENTS_LOCKED",
    "RUNNING", "VERIFYING", "RECOVERING", "VERIFIED", "FAILED_VERIFICATION",
    "RECOVERY_REQUIRED", "CANCELLED", "FAILED", "COMPLETED",
]  # fmt: skip

TERMINAL: frozenset[str] = frozenset({"VERIFIED", "CANCELLED", "FAILED", "COMPLETED"})
GATE_ONLY: frozenset[str] = frozenset({"VERIFIED", "FAILED_VERIFICATION"})
_ALWAYS = {"CANCELLED", "FAILED"}
TRANSITIONS: dict[str, frozenset[str]] = {
    k: frozenset(v | _ALWAYS)
    for k, v in {
        # RECEIVED -> RUNNING: an Ultra run answered directly (no contract).
        "RECEIVED": {"INTENT_COMPILED", "RUNNING"},
        "INTENT_COMPILED": {"BLOCKED_FOR_CLARIFICATION", "REQUIREMENTS_LOCKED"},
        "BLOCKED_FOR_CLARIFICATION": {"INTENT_COMPILED", "REQUIREMENTS_LOCKED"},
        "REQUIREMENTS_LOCKED": {"RUNNING"},
        "RUNNING": {"VERIFYING", "COMPLETED"},
        "VERIFYING": {"VERIFIED", "FAILED_VERIFICATION", "RECOVERY_REQUIRED"},
        "FAILED_VERIFICATION": {"RECOVERING", "RECOVERY_REQUIRED"},
        "RECOVERING": {"RUNNING", "VERIFYING", "RECOVERY_REQUIRED"},
        "RECOVERY_REQUIRED": {"RECOVERING", "RUNNING"},
    }.items()
}
# States only a live run can leave; after a restart nothing drives them any more.
# FAILED_VERIFICATION keeps its evidence, so it becomes resumable RECOVERY_REQUIRED.
INTERRUPTED: dict[str, TaskStatus] = {
    **dict.fromkeys(
        (
            "RECEIVED", "INTENT_COMPILED", "BLOCKED_FOR_CLARIFICATION",
            "REQUIREMENTS_LOCKED", "RUNNING", "VERIFYING", "RECOVERING",
        ),
        "FAILED",
    ),
    "FAILED_VERIFICATION": "RECOVERY_REQUIRED",
}  # fmt: skip
_DISPOSITION_STATUS: dict[str, TaskStatus] = {
    "VERIFIED": "VERIFIED",
    "FAILED_VERIFICATION": "FAILED_VERIFICATION",
    "NEEDS_REVIEW": "RECOVERY_REQUIRED",
}

_MIGRATIONS = [
    """CREATE TABLE workbench_tasks (
        id TEXT PRIMARY KEY, session_id TEXT NOT NULL, cwd TEXT NOT NULL,
        contract_json TEXT, checkpoint_json TEXT, status TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    "CREATE INDEX workbench_tasks_session ON workbench_tasks (session_id, created_at)",
    """CREATE TABLE workbench_evidence (
        task_id TEXT NOT NULL REFERENCES workbench_tasks(id) ON DELETE CASCADE,
        attempt INTEGER NOT NULL, disposition TEXT NOT NULL, json TEXT NOT NULL,
        created_at TEXT NOT NULL, PRIMARY KEY (task_id, attempt))""",
    """CREATE TABLE workbench_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL REFERENCES workbench_tasks(id) ON DELETE CASCADE,
        ts TEXT NOT NULL, type TEXT NOT NULL, payload TEXT NOT NULL)""",
    "CREATE INDEX workbench_events_task ON workbench_events (task_id, id)",
]


class TransitionError(ValueError):
    """Illegal lifecycle transition or unauthorised VERIFIED claim."""


@dataclass
class TaskRecord:
    id: str
    session_id: str
    cwd: str
    status: TaskStatus
    contract: IntentContract | None
    checkpoint: Checkpoint | None
    created_at: str
    updated_at: str


@dataclass
class TaskEvent:
    ts: str
    type: str
    payload: JsonObject


def _now() -> str:
    return datetime.now(UTC).isoformat()


class TaskStore:
    def __init__(self, store: Store) -> None:
        self._store = store
        store.migrate("workbench_tasks", _MIGRATIONS)

    def create(self, session_id: str, cwd: str) -> str:
        task_id = uuid.uuid4().hex
        now = _now()
        with self._store.transaction() as conn:
            conn.execute(
                "INSERT INTO workbench_tasks (id, session_id, cwd, status, created_at, updated_at) VALUES (?, ?, ?, 'RECEIVED', ?, ?)",
                (task_id, session_id, cwd, now, now),
            )
            self._event(
                conn, task_id, "task.received", {"session_id": session_id, "cwd": cwd}
            )
        return task_id

    def attach(
        self,
        task_id: str,
        *,
        contract: IntentContract | None = None,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        self.get(task_id)
        with self._store.transaction() as conn:
            if contract is not None:
                conn.execute(
                    "UPDATE workbench_tasks SET contract_json = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(contract.to_json()), _now(), task_id),
                )
                self._event(
                    conn,
                    task_id,
                    "intent.compiled",
                    {"version": contract.version, "risk": contract.risk},
                )
            if checkpoint is not None:
                conn.execute(
                    "UPDATE workbench_tasks SET checkpoint_json = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(checkpoint.to_json()), _now(), task_id),
                )
                self._event(
                    conn,
                    task_id,
                    "checkpoint.created",
                    {"commit": checkpoint.commit, "supported": checkpoint.supported},
                )

    def rebind_session(self, task_id: str, session_id: str) -> None:
        """Attach the Claude session id once known (new chats get it after the first prompt)."""

        self._store.execute(
            "UPDATE workbench_tasks SET session_id = ?, updated_at = ? WHERE id = ?",
            (session_id, _now(), task_id),
        )

    def get(self, task_id: str) -> TaskRecord:
        rows = self._store.query(
            "SELECT * FROM workbench_tasks WHERE id = ?", (task_id,)
        )
        if not rows:
            raise KeyError(task_id)
        r = rows[0]
        return TaskRecord(
            id=r["id"],
            session_id=r["session_id"],
            cwd=r["cwd"],
            status=cast(TaskStatus, r["status"]),
            contract=IntentContract.from_json(json.loads(r["contract_json"]))
            if r["contract_json"]
            else None,
            checkpoint=Checkpoint.from_json(json.loads(r["checkpoint_json"]))
            if r["checkpoint_json"]
            else None,
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )

    def for_session(self, session_id: str) -> list[TaskRecord]:
        rows = self._store.query(
            "SELECT id FROM workbench_tasks WHERE session_id = ? ORDER BY created_at",
            (session_id,),
        )
        return [self.get(r["id"]) for r in rows]

    def _set_status(
        self, task_id: str, to: TaskStatus, reason: str, *, gate: bool
    ) -> None:
        with self._store.transaction() as conn:
            row = conn.execute(
                "SELECT status FROM workbench_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            current = row["status"]
            if to in GATE_ONLY and not gate:
                raise TransitionError(f"{to} can only be set by the verification gate")
            if to not in TRANSITIONS.get(current, frozenset()):
                raise TransitionError(f"illegal transition {current} -> {to}")
            conn.execute(
                "UPDATE workbench_tasks SET status = ?, updated_at = ? WHERE id = ?",
                (to, _now(), task_id),
            )
            self._event(
                conn,
                task_id,
                "task.status",
                {"from": current, "to": to, "reason": reason},
            )

    def transition(self, task_id: str, to: TaskStatus, reason: str = "") -> None:
        self._set_status(task_id, to, reason, gate=False)

    def close_interrupted(self, reason: str) -> list[tuple[str, str, TaskStatus]]:
        """Settle tasks whose run died with the server; returns (id, from, to) each."""

        rows = self._store.query(
            f"SELECT id, status FROM workbench_tasks WHERE status IN ({', '.join('?' * len(INTERRUPTED))})",
            tuple(INTERRUPTED),
        )
        closed: list[tuple[str, str, TaskStatus]] = []
        for row in rows:
            to = INTERRUPTED[row["status"]]
            self.transition(row["id"], to, reason)
            closed.append((row["id"], row["status"], to))
        return closed

    def record_evidence(self, task_id: str, package: EvidencePackage) -> TaskStatus:
        """Persist a gate evidence package and move VERIFYING -> its disposition's status."""

        recomputed, _ = compute_disposition(package.checks, package.gap_matrix)
        if recomputed != package.disposition:
            raise TransitionError(
                f"evidence claims {package.disposition} but its checks/gap matrix give {recomputed}"
            )
        to = _DISPOSITION_STATUS[package.disposition]
        with self._store.transaction() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(attempt), 0) AS n FROM workbench_evidence WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            attempt = int(row["n"]) + 1
            self._set_status(
                task_id, to, "; ".join(package.blocking_reasons[:3]), gate=True
            )
            conn.execute(
                "INSERT INTO workbench_evidence (task_id, attempt, disposition, json, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    task_id,
                    attempt,
                    package.disposition,
                    json.dumps(package.to_json()),
                    _now(),
                ),
            )
            self._event(
                conn,
                task_id,
                "verification.completed",
                {"attempt": attempt, "disposition": package.disposition},
            )
        return to

    def evidence(self, task_id: str) -> list[JsonObject]:
        rows = self._store.query(
            "SELECT json FROM workbench_evidence WHERE task_id = ? ORDER BY attempt",
            (task_id,),
        )
        return [json.loads(r["json"]) for r in rows]

    def add_event(
        self, task_id: str, type_: str, payload: JsonObject | None = None
    ) -> None:
        with self._store.transaction() as conn:
            self._event(conn, task_id, type_, payload or {})

    def events(self, task_id: str) -> list[TaskEvent]:
        rows = self._store.query(
            "SELECT ts, type, payload FROM workbench_events WHERE task_id = ? ORDER BY id",
            (task_id,),
        )
        return [TaskEvent(r["ts"], r["type"], json.loads(r["payload"])) for r in rows]

    def export(self, task_id: str, fmt: Literal["json", "markdown"] = "json") -> str:
        packages = self.evidence(task_id)
        if not packages:
            raise KeyError(f"no evidence for task {task_id}")
        return (
            render_markdown(packages[-1])
            if fmt == "markdown"
            else json.dumps(packages[-1], indent=2)
        )

    @staticmethod
    def _event(
        conn: sqlite3.Connection, task_id: str, type_: str, payload: JsonObject
    ) -> None:
        conn.execute(
            "INSERT INTO workbench_events (task_id, ts, type, payload) VALUES (?, ?, ?, ?)",
            (task_id, _now(), type_, json.dumps(payload)),
        )
