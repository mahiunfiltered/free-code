"""One usage row per provider attempt (never the key, only its label)."""

import sqlite3
import time
from dataclasses import dataclass

from free_claude_code.core.json_types import JsonObject
from free_claude_code.core.storage import Store

from .endpoint_health import Clock, percentile

_MIGRATIONS = [
    "CREATE TABLE usage_records ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,"
    " provider_id TEXT NOT NULL, key_label TEXT NOT NULL, model TEXT,"
    " request_id TEXT, claude_session_id TEXT, input_tokens INTEGER,"
    " output_tokens INTEGER, latency_ms REAL, outcome TEXT NOT NULL,"
    " failure_kind TEXT, failover_from TEXT, attempt INTEGER NOT NULL)",
    "CREATE INDEX usage_records_ts ON usage_records (ts)",
    "CREATE INDEX usage_records_session ON usage_records (claude_session_id)",
]


@dataclass(frozen=True, slots=True)
class UsageRecord:
    provider_id: str
    key_label: str
    model: str | None
    request_id: str | None
    claude_session_id: str | None
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: float
    outcome: str
    attempt: int
    failure_kind: str | None = None
    failover_from: str | None = None


class UsageRecorder:
    """Append-only usage table owned by the ``usage`` Store feature."""

    def __init__(self, store: Store, *, clock: Clock = time.time) -> None:
        self._store = store
        self._clock = clock
        store.migrate("usage", _MIGRATIONS)

    def record(self, record: UsageRecord) -> None:
        self._store.execute(
            "INSERT INTO usage_records (ts, provider_id, key_label, model, request_id,"
            " claude_session_id, input_tokens, output_tokens, latency_ms, outcome,"
            " failure_kind, failover_from, attempt)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self._clock(),
                record.provider_id,
                record.key_label,
                record.model,
                record.request_id,
                record.claude_session_id,
                record.input_tokens,
                record.output_tokens,
                round(record.latency_ms, 1),
                record.outcome,
                record.failure_kind,
                record.failover_from,
                record.attempt,
            ),
        )

    def recent(self, limit: int = 50) -> list[JsonObject]:
        rows = self._store.query(
            "SELECT * FROM usage_records ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [dict(row) for row in rows]

    def endpoint_aggregates(self, minutes: float) -> list[JsonObject]:
        """Per provider/key: requests, errors, rate limits, latency p50/p95, tokens."""
        since = self._clock() - minutes * 60
        rows = self._store.query(
            "SELECT provider_id, key_label, outcome, latency_ms, input_tokens,"
            " output_tokens FROM usage_records WHERE ts >= ? ORDER BY id",
            (since,),
        )
        groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for row in rows:
            groups.setdefault((row["provider_id"], row["key_label"]), []).append(row)
        result: list[JsonObject] = []
        for (provider_id, label), items in groups.items():
            latencies = sorted(
                row["latency_ms"]
                for row in items
                if row["outcome"] == "ok" and row["latency_ms"] is not None
            )
            result.append(
                {
                    "provider_id": provider_id,
                    "label": label,
                    "requests": len(items),
                    "ok": sum(row["outcome"] == "ok" for row in items),
                    "errors": sum(
                        row["outcome"] not in ("ok", "cancelled") for row in items
                    ),
                    "rate_limits": sum(
                        row["outcome"] in ("rate_limited", "quota_exhausted")
                        for row in items
                    ),
                    "latency_p50_ms": percentile(latencies, 0.5) if latencies else None,
                    "latency_p95_ms": (
                        percentile(latencies, 0.95) if latencies else None
                    ),
                    "input_tokens": sum(row["input_tokens"] or 0 for row in items),
                    "output_tokens": sum(row["output_tokens"] or 0 for row in items),
                }
            )
        return result

    def session_totals(self, minutes: float | None = None) -> list[JsonObject]:
        """Token and request totals per Claude session id."""
        since = 0.0 if minutes is None else self._clock() - minutes * 60
        rows = self._store.query(
            "SELECT claude_session_id, COUNT(*) AS attempts,"
            " SUM(outcome = 'ok') AS ok,"
            " SUM(failover_from IS NOT NULL) AS failovers,"
            " COALESCE(SUM(input_tokens), 0) AS input_tokens,"
            " COALESCE(SUM(output_tokens), 0) AS output_tokens,"
            " MIN(ts) AS first_ts, MAX(ts) AS last_ts"
            " FROM usage_records WHERE claude_session_id IS NOT NULL AND ts >= ?"
            " GROUP BY claude_session_id ORDER BY last_ts DESC",
            (since,),
        )
        return [dict(row) for row in rows]
