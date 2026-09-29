"""Small durable SQLite store shared by FCC features (usage, health, audit, evidence).

Each feature owns its tables and registers them as named migrations, so modules
evolve their schema independently without a central schema file:

    store.migrate("usage", [
        "CREATE TABLE usage_records (...)",   # version 1
        "ALTER TABLE usage_records ADD ...",  # version 2
    ])
"""

import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

type SqlParam = str | int | float | bytes | None


class Store:
    """Thread-safe SQLite access with WAL, per-feature migrations and transactions."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # ponytail: one shared connection behind a lock; per-thread pools if contention shows up.
        self._conn = sqlite3.connect(
            str(path), check_same_thread=False, isolation_level=None, timeout=10.0
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA busy_timeout = 10000")
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " feature TEXT PRIMARY KEY, version INTEGER NOT NULL)"
            )

    def migrate(self, feature: str, statements: Sequence[str]) -> None:
        """Apply statements whose index >= the feature's recorded version, atomically."""

        with self.transaction() as conn:
            row = conn.execute(
                "SELECT version FROM schema_migrations WHERE feature = ?", (feature,)
            ).fetchone()
            current = row["version"] if row else 0
            for statement in statements[current:]:
                conn.execute(statement)
            if len(statements) > current:
                conn.execute(
                    "INSERT INTO schema_migrations (feature, version) VALUES (?, ?)"
                    " ON CONFLICT(feature) DO UPDATE SET version = excluded.version",
                    (feature, len(statements)),
                )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a block atomically; nested use joins the outer transaction."""

        with self._lock:
            if self._conn.in_transaction:
                yield self._conn
                return
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def execute(self, sql: str, params: Sequence[SqlParam] = ()) -> int:
        """Run one write statement; returns the last inserted row id."""

        with self.transaction() as conn:
            return conn.execute(sql, params).lastrowid or 0

    def query(self, sql: str, params: Sequence[SqlParam] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
