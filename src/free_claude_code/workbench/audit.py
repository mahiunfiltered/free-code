"""Tamper-evident, append-only audit log (M0005 09 section 7) in the shared ``Store``.

Each row stores ``hash = sha256(canonical_json(row without hash))`` where the row includes
``prev_hash``, chaining every record to the one before it. SQLite triggers reject UPDATE and
DELETE, and ``verify()`` detects edits made by bypassing them (e.g. dropping the triggers).
"""

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime

from free_claude_code.core.diagnostics import redact_sensitive_error_text
from free_claude_code.core.json_types import JsonObject, JsonValue
from free_claude_code.core.storage import SqlParam, Store
from free_claude_code.core.trace import sanitize_trace_value

GENESIS_HASH = "0" * 64
_FILTERS = ("actor", "action", "resource", "decision", "outcome")

MIGRATIONS = [
    """CREATE TABLE audit_log (
        id INTEGER PRIMARY KEY,
        ts TEXT NOT NULL,
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        resource TEXT NOT NULL,
        decision TEXT NOT NULL,
        outcome TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        prev_hash TEXT NOT NULL,
        hash TEXT NOT NULL
    )""",
    "CREATE INDEX audit_log_action ON audit_log (action, id)",
    """CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
       BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END""",
    """CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
       BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END""",
]


@dataclass(frozen=True, slots=True)
class AuditRecord:
    id: int
    ts: str
    actor: str
    action: str
    resource: str
    decision: str
    outcome: str
    payload_json: str
    prev_hash: str
    hash: str


def _hash(fields: JsonObject) -> str:
    canonical = json.dumps(
        fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _unhashed(record: AuditRecord) -> JsonObject:
    fields: JsonObject = asdict(record)
    del fields["hash"]
    return fields


def _record(row: sqlite3.Row) -> AuditRecord:
    return AuditRecord(**{name: row[name] for name in AuditRecord.__dataclass_fields__})


def _redact(value: JsonValue) -> JsonValue:
    if isinstance(value, str):
        return redact_sensitive_error_text(value)
    if isinstance(value, Mapping):
        return {key: _redact(item) for key, item in value.items()}
    if isinstance(value, Sequence):
        return [_redact(item) for item in value]
    return value


def _redact_payload(payload: JsonObject | None) -> str:
    """Drop credential-named keys, then scrub credential-shaped strings."""

    return json.dumps(
        _redact(sanitize_trace_value(payload or {})), sort_keys=True, ensure_ascii=False
    )


class AuditLog:
    def __init__(self, store: Store) -> None:
        self._store = store
        store.migrate("audit", MIGRATIONS)

    def append(
        self,
        *,
        actor: str,
        action: str,
        resource: str = "",
        decision: str = "",
        outcome: str = "",
        payload: JsonObject | None = None,
    ) -> AuditRecord:
        with self._store.transaction() as conn:
            last = conn.execute(
                "SELECT id, hash FROM audit_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
            draft = AuditRecord(
                id=last["id"] + 1 if last else 1,
                ts=datetime.now(UTC).isoformat(),
                actor=actor,
                action=action,
                resource=resource,
                decision=decision,
                outcome=outcome,
                payload_json=_redact_payload(payload),
                prev_hash=last["hash"] if last else GENESIS_HASH,
                hash="",
            )
            record = replace(draft, hash=_hash(_unhashed(draft)))
            columns = asdict(record)
            conn.execute(
                f"INSERT INTO audit_log ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                tuple(columns.values()),
            )
        return record

    def verify(self) -> tuple[bool, int | None]:
        """Return ``(ok, first_bad_id)`` after re-hashing the whole chain."""

        expected_prev = GENESIS_HASH
        expected_id = 1
        # ponytail: loads the whole table; page by id if the log grows past memory.
        for row in self._store.query("SELECT * FROM audit_log ORDER BY id"):
            record = _record(row)
            if (
                record.id != expected_id
                or record.prev_hash != expected_prev
                or record.hash != _hash(_unhashed(record))
            ):
                return False, record.id
            expected_prev = record.hash
            expected_id += 1
        return True, None

    def recent(self, limit: int = 100, **filters: str) -> list[AuditRecord]:
        unknown = set(filters) - set(_FILTERS)
        if unknown:
            raise ValueError(f"Unknown audit filters: {sorted(unknown)}")
        where = " AND ".join(f"{key} = ?" for key in filters)
        params: list[SqlParam] = [*filters.values(), max(1, min(limit, 1000))]
        rows = self._store.query(
            f"SELECT * FROM audit_log {'WHERE ' + where if where else ''} ORDER BY id DESC LIMIT ?",
            params,
        )
        return [_record(row) for row in rows]
