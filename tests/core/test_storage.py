"""Shared SQLite store: migrations, transactions, persistence."""

import sqlite3
from pathlib import Path

import pytest

from free_claude_code.core.storage import Store


def test_migrations_apply_once_and_extend(tmp_path: Path):
    path = tmp_path / "db" / "fcc.db"
    store = Store(path)
    store.migrate("demo", ["CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)"])
    store.migrate("demo", ["CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)"])
    store.migrate(
        "demo",
        [
            "CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)",
            "ALTER TABLE t ADD COLUMN extra TEXT",
        ],
    )
    row_id = store.execute("INSERT INTO t (name, extra) VALUES (?, ?)", ("a", "x"))
    store.close()

    reopened = Store(path)
    reopened.migrate("demo", ["CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)"])
    rows = reopened.query("SELECT id, name, extra FROM t")
    assert [(r["id"], r["name"], r["extra"]) for r in rows] == [(row_id, "a", "x")]
    reopened.close()


def test_transaction_rolls_back_and_nests(tmp_path: Path):
    store = Store(tmp_path / "fcc.db")
    store.migrate("demo", ["CREATE TABLE t (v INTEGER)"])
    with pytest.raises(RuntimeError), store.transaction() as conn:
        conn.execute("INSERT INTO t VALUES (1)")
        store.execute("INSERT INTO t VALUES (2)")  # joins the outer transaction
        raise RuntimeError("boom")
    assert store.query("SELECT COUNT(*) AS n FROM t")[0]["n"] == 0

    failing = ["CREATE TABLE u (v INTEGER)", "NOT VALID SQL"]
    with pytest.raises(sqlite3.OperationalError):
        store.migrate("broken", failing)
    assert not store.query("SELECT name FROM sqlite_master WHERE name = 'u'"), (
        "failed migration must not leave partial schema"
    )
    store.close()
