"""Verified-only solution memory (M0005 ADR-008) in the shared Store.

Entries start as ``candidate``; only a caller holding verification evidence may
promote them. Only ``promoted`` entries are searchable and injected into prompts,
and an entry goes ``stale`` once any source file it was learned from changes.
"""

import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Row
from typing import Literal, cast

from free_claude_code.core.storage import Store

type MemoryStatus = Literal["candidate", "promoted", "stale"]

MIGRATIONS = [
    "CREATE TABLE memory_entries ("
    " id INTEGER PRIMARY KEY,"
    " project TEXT NOT NULL,"
    " title TEXT NOT NULL,"
    " pattern TEXT NOT NULL,"
    " applicability TEXT NOT NULL,"
    " source_task_id TEXT NOT NULL,"
    " source_hashes TEXT NOT NULL,"
    " status TEXT NOT NULL,"
    " confidence REAL NOT NULL,"
    " created_at REAL NOT NULL,"
    " updated_at REAL NOT NULL)",
    "CREATE INDEX memory_entries_project ON memory_entries (project, status)",
]

_SECRET = re.compile(
    r"(?<![A-Za-z0-9])(?:nvapi-[A-Za-z0-9._-]{8,}|sk-[A-Za-z0-9._-]{8,}"
    r"|AKIA[0-9A-Z]{16})|-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"
)
_TERM = re.compile(r"[a-z0-9]+")
_RENDER_LIMIT = 600


class MemoryRejectedError(ValueError):
    """The entry was refused (secret detected, unverified promotion, bad input)."""


@dataclass(frozen=True)
class MemoryEntry:
    id: int
    project: str
    title: str
    pattern: str
    applicability: str
    source_task_id: str
    source_hashes: dict[str, str]
    status: MemoryStatus
    confidence: float
    created_at: float
    updated_at: float


def hash_files(root: Path | str, paths: list[str]) -> dict[str, str]:
    """sha256 of each repo-relative file ("" when missing) for staleness tracking."""

    base = Path(root)
    hashes: dict[str, str] = {}
    for rel in paths:
        file = base / rel
        hashes[rel] = (
            hashlib.sha256(file.read_bytes()).hexdigest() if file.is_file() else ""
        )
    return hashes


class SolutionMemory:
    def __init__(self, store: Store, clock: Callable[[], float] = time.time) -> None:
        self._store = store
        self._clock = clock
        store.migrate("memory", MIGRATIONS)

    def add_candidate(
        self,
        *,
        project: str,
        title: str,
        pattern: str,
        applicability: str,
        source_task_id: str,
        source_hashes: Mapping[str, str],
        confidence: float = 0.5,
    ) -> MemoryEntry:
        if not 0.0 <= confidence <= 1.0:
            raise MemoryRejectedError("confidence must be within 0..1")
        if not title.strip() or not pattern.strip():
            raise MemoryRejectedError("title and pattern are required")
        text = "\n".join([title, pattern, applicability, *source_hashes])
        if _SECRET.search(text):
            raise MemoryRejectedError("possible secret detected; entry not stored")
        now = self._clock()
        entry_id = self._store.execute(
            "INSERT INTO memory_entries (project, title, pattern, applicability,"
            " source_task_id, source_hashes, status, confidence, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'candidate', ?, ?, ?)",
            (
                project,
                title,
                pattern,
                applicability,
                source_task_id,
                json.dumps(dict(source_hashes), sort_keys=True),
                confidence,
                now,
                now,
            ),
        )
        return self.get(entry_id)

    def get(self, entry_id: int) -> MemoryEntry:
        rows = self._store.query(
            "SELECT * FROM memory_entries WHERE id = ?", (entry_id,)
        )
        if not rows:
            raise KeyError(entry_id)
        return _entry(rows[0])

    def promote(self, task_id: str, *, verified: bool) -> int:
        """Promote the task's candidates; only verification evidence may do this."""

        if verified is not True:
            raise MemoryRejectedError("only a VERIFIED task may promote memory")
        return self._set_status(
            "UPDATE memory_entries SET status = 'promoted', updated_at = ?"
            " WHERE source_task_id = ? AND status = 'candidate'",
            task_id,
        )

    def mark_stale(self, project: str) -> int:
        """Stale every live entry whose source files changed on disk under ``project``."""

        stale = 0
        for row in self._store.query(
            "SELECT * FROM memory_entries WHERE project = ? AND status != 'stale'",
            (project,),
        ):
            entry = _entry(row)
            current = hash_files(project, list(entry.source_hashes))
            if current != entry.source_hashes:
                stale += self._set_status(
                    "UPDATE memory_entries SET status = 'stale', updated_at = ?"
                    " WHERE id = ?",
                    entry.id,
                )
        return stale

    def search(self, project: str, query: str, limit: int = 5) -> list[MemoryEntry]:
        """Promoted entries ranked by term overlap (title x3, applicability x2, pattern x1)."""

        # ponytail: term overlap, not BM25; fine for per-project memory sizes.
        terms = set(_TERM.findall(query.lower()))
        scored: list[tuple[float, float, MemoryEntry]] = []
        for row in self._store.query(
            "SELECT * FROM memory_entries WHERE project = ? AND status = 'promoted'",
            (project,),
        ):
            entry = _entry(row)
            score = sum(
                3 * (t in _terms(entry.title))
                + 2 * (t in _terms(entry.applicability))
                + (t in _terms(entry.pattern))
                for t in terms
            )
            if score:
                scored.append((score * entry.confidence, entry.updated_at, entry))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [entry for _, _, entry in scored[:limit]]

    def _set_status(self, sql: str, key: str | int) -> int:
        with self._store.transaction() as conn:
            return conn.execute(sql, (self._clock(), key)).rowcount


def render_for_prompt(entries: list[MemoryEntry]) -> str:
    """Compact block for ``--append-system-prompt``; empty when nothing applies."""

    if not entries:
        return ""
    lines = ["Verified solution patterns from earlier work in this project:"]
    for entry in entries:
        pattern = " ".join(entry.pattern.split())[:_RENDER_LIMIT]
        lines.append(
            f"- {entry.title} (applies when: {entry.applicability}): {pattern}"
        )
    lines.append("Use them only where they fit; the current request always wins.")
    return "\n".join(lines)


def _terms(text: str) -> set[str]:
    return set(_TERM.findall(text.lower()))


def _entry(row: Row) -> MemoryEntry:
    hashes = json.loads(row["source_hashes"])
    return MemoryEntry(
        id=row["id"],
        project=row["project"],
        title=row["title"],
        pattern=row["pattern"],
        applicability=row["applicability"],
        source_task_id=row["source_task_id"],
        source_hashes={str(k): str(v) for k, v in hashes.items()},
        status=cast(MemoryStatus, row["status"]),
        confidence=row["confidence"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
