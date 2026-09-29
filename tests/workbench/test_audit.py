import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from free_claude_code.core.storage import Store
from free_claude_code.workbench.audit import GENESIS_HASH, AuditLog


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    instance = Store(tmp_path / "fcc.db")
    yield instance
    instance.close()


def _fill(log: AuditLog) -> None:
    log.append(
        actor="user",
        action="permission",
        resource="Bash(git push)",
        decision="ask",
        outcome="approved",
    )
    log.append(actor="agent", action="secret.set", resource="NIM", outcome="ok")
    log.append(
        actor="gate", action="verification", resource="task-1", outcome="VERIFIED"
    )


def test_chain_links_and_verifies(store: Store) -> None:
    log = AuditLog(store)
    _fill(log)
    records = list(reversed(log.recent()))
    assert [r.id for r in records] == [1, 2, 3]
    assert records[0].prev_hash == GENESIS_HASH
    assert records[1].prev_hash == records[0].hash
    assert log.verify() == (True, None)


def test_empty_log_verifies(store: Store) -> None:
    assert AuditLog(store).verify() == (True, None)


def test_migration_is_idempotent(store: Store) -> None:
    AuditLog(store).append(actor="a", action="x")
    assert AuditLog(store).verify() == (True, None)


@pytest.mark.parametrize(
    "sql", ["UPDATE audit_log SET outcome = 'x'", "DELETE FROM audit_log"]
)
def test_triggers_block_update_and_delete(store: Store, sql: str) -> None:
    log = AuditLog(store)
    _fill(log)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.execute(sql)
    assert log.verify() == (True, None)


def _tamper(store: Store, sql: str) -> None:
    with store.transaction() as conn:
        conn.execute("DROP TRIGGER audit_log_no_update")
        conn.execute("DROP TRIGGER audit_log_no_delete")
        conn.execute(sql)


def test_raw_sql_edit_is_detected(store: Store) -> None:
    log = AuditLog(store)
    _fill(log)
    _tamper(store, "UPDATE audit_log SET outcome = 'denied' WHERE id = 2")
    assert log.verify() == (False, 2)


def test_rehashed_edit_breaks_the_next_link(store: Store) -> None:
    log = AuditLog(store)
    _fill(log)
    _tamper(store, "UPDATE audit_log SET hash = 'deadbeef' WHERE id = 1")
    assert log.verify() == (False, 1)


def test_deleted_middle_row_is_detected(store: Store) -> None:
    log = AuditLog(store)
    _fill(log)
    _tamper(store, "DELETE FROM audit_log WHERE id = 2")
    assert log.verify() == (False, 3)


def test_payload_is_redacted(store: Store) -> None:
    log = AuditLog(store)
    record = log.append(
        actor="user",
        action="config.change",
        payload={
            "api_key": "nvapi-abcdefghijk",
            "note": "Bearer sk-abcdefghijklmn",
            "n": 3,
        },
    )
    payload = json.loads(record.payload_json)
    assert payload["api_key"] == "<redacted>"
    assert "sk-abcdefghijklmn" not in record.payload_json
    assert payload["n"] == 3


def test_recent_filters_and_limit(store: Store) -> None:
    log = AuditLog(store)
    _fill(log)
    assert [r.action for r in log.recent(limit=2)] == ["verification", "secret.set"]
    assert [r.id for r in log.recent(actor="user")] == [1]
    assert log.recent(action="permission", decision="deny") == []
    with pytest.raises(ValueError, match="Unknown audit filters"):
        log.recent(bogus="x")
