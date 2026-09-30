"""Task graph (DAG) of Claude Code nodes: validation, readiness, failure propagation, JSON."""

import re
from dataclasses import dataclass, field
from typing import Literal, cast, get_args

from free_claude_code.core.json_types import JsonObject, JsonValue

type Role = Literal[
    "explorer", "planner", "implementation", "ui", "test", "reviewer", "integration"
]
type NodeStatus = Literal[
    "pending",
    "ready",
    "running",
    "completed",
    "failed",
    "blocked",
    "cancelled",
    "skipped",
]
ROLES: frozenset[str] = frozenset(get_args(Role.__value__))
STATUSES: frozenset[str] = frozenset(get_args(NodeStatus.__value__))
# Dependency states that can never become "completed"; their dependents are blocked.
_DEAD = frozenset({"failed", "blocked", "cancelled", "skipped"})
_WILDCARD = re.compile(r"[*?\[]")


@dataclass
class TaskNode:
    id: str
    objective: str
    role: Role = "implementation"
    depends_on: list[str] = field(default_factory=list)
    # Repo-relative globs; empty means the node is read-only.
    write_scope: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    # Detailed brief from the lead agent (Ultra mode): what to do, files, constraints.
    instructions: str = ""
    status: NodeStatus = "pending"
    summary: str = ""
    error: str | None = None
    branch: str | None = None
    worktree: str | None = None
    # Commit the node's branch started from (after merging its dependencies).
    start_commit: str | None = None
    session_id: str | None = None
    cost_usd: float | None = None
    turns: int | None = None
    reverted_out_of_scope: list[str] = field(default_factory=list)
    # Files the node changed inside its scope, and (in-place runs, which cannot
    # revert) files changed outside every concurrently running node's scope.
    changed_files: list[str] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)

    @property
    def mutating(self) -> bool:
        return bool(self.write_scope)

    def to_json(self) -> JsonObject:
        return {
            "id": self.id,
            "objective": self.objective,
            "role": self.role,
            "depends_on": list(self.depends_on),
            "write_scope": list(self.write_scope),
            "acceptance_criteria": list(self.acceptance_criteria),
            "instructions": self.instructions,
            "status": self.status,
            "summary": self.summary,
            "error": self.error,
            "branch": self.branch,
            "worktree": self.worktree,
            "start_commit": self.start_commit,
            "session_id": self.session_id,
            "cost_usd": self.cost_usd,
            "turns": self.turns,
            "reverted_out_of_scope": list(self.reverted_out_of_scope),
            "changed_files": list(self.changed_files),
            "out_of_scope": list(self.out_of_scope),
        }

    @classmethod
    def from_json(cls, data: JsonValue) -> TaskNode:
        """Strictly parse a node; raises ValueError on any malformed field."""

        if not isinstance(data, dict):
            raise ValueError("node must be an object")
        node_id = _str(data, "id")
        role = data.get("role", "implementation")
        if role not in ROLES:
            raise ValueError(f"node {node_id}: unknown role {role!r}")
        status = data.get("status", "pending")
        if status not in STATUSES:
            raise ValueError(f"node {node_id}: unknown status {status!r}")
        cost = data.get("cost_usd")
        turns = data.get("turns")
        return cls(
            id=node_id,
            objective=_str(data, "objective"),
            role=cast(Role, role),
            depends_on=_str_list(data, "depends_on"),
            write_scope=_str_list(data, "write_scope"),
            acceptance_criteria=_str_list(data, "acceptance_criteria"),
            instructions=_opt_str(data, "instructions") or "",
            status=cast(NodeStatus, status),
            summary=_opt_str(data, "summary") or "",
            error=_opt_str(data, "error"),
            branch=_opt_str(data, "branch"),
            worktree=_opt_str(data, "worktree"),
            start_commit=_opt_str(data, "start_commit"),
            session_id=_opt_str(data, "session_id"),
            cost_usd=float(cost) if isinstance(cost, int | float) else None,
            turns=turns if isinstance(turns, int) else None,
            reverted_out_of_scope=_str_list(data, "reverted_out_of_scope"),
            changed_files=_str_list(data, "changed_files"),
            out_of_scope=_str_list(data, "out_of_scope"),
        )


@dataclass
class TaskGraph:
    task_id: str
    nodes: list[TaskNode]

    def node(self, node_id: str) -> TaskNode:
        for node in self.nodes:
            if node.id == node_id:
                return node
        raise KeyError(node_id)

    def validate(self) -> list[str]:
        """Return error codes (empty when the graph is a valid DAG)."""

        errors: list[str] = []
        ids = [n.id for n in self.nodes]
        known = set(ids)
        errors += sorted({f"duplicate_id:{i}" for i in ids if ids.count(i) > 1})
        for node in self.nodes:
            if node.role not in ROLES:
                errors.append(f"unknown_role:{node.id}")
            for dep in node.depends_on:
                if dep == node.id:
                    errors.append(f"self_dependency:{node.id}")
                elif dep not in known:
                    errors.append(f"missing_dependency:{node.id}>{dep}")
        if not errors and len(self.order()) != len(self.nodes):
            errors.append("cycle")
        return errors

    def order(self) -> list[str]:
        """Topological order (Kahn, ties by declaration order); nodes on cycles are omitted."""

        remaining = list(self.nodes)
        done: set[str] = set()
        ordered: list[str] = []
        progressed = True
        while remaining and progressed:
            progressed = False
            for node in list(remaining):
                if all(d in done for d in node.depends_on):
                    ordered.append(node.id)
                    done.add(node.id)
                    remaining.remove(node)
                    progressed = True
        return ordered

    def ready(self) -> list[TaskNode]:
        """Pending nodes whose dependencies all completed."""

        status = {n.id: n.status for n in self.nodes}
        return [
            n
            for n in self.nodes
            if n.status in ("pending", "ready")
            and all(status.get(d) == "completed" for d in n.depends_on)
        ]

    def block_failed(self) -> list[str]:
        """Mark pending dependents of dead nodes as blocked (transitively); returns their ids."""

        blocked: list[str] = []
        changed = True
        while changed:
            changed = False
            status = {n.id: n.status for n in self.nodes}
            for node in self.nodes:
                if node.status in ("pending", "ready") and any(
                    status.get(d) in _DEAD for d in node.depends_on
                ):
                    node.status = "blocked"
                    blocked.append(node.id)
                    changed = True
        return blocked

    def ancestors(self, node_id: str) -> set[str]:
        seen: set[str] = set()
        stack = list(self.node(node_id).depends_on)
        while stack:
            dep = stack.pop()
            if dep not in seen:
                seen.add(dep)
                stack += self.node(dep).depends_on
        return seen

    def to_json(self) -> JsonObject:
        return {"task_id": self.task_id, "nodes": [n.to_json() for n in self.nodes]}

    @classmethod
    def from_json(cls, data: JsonValue) -> TaskGraph:
        if not isinstance(data, dict):
            raise ValueError("graph must be an object")
        nodes = data.get("nodes")
        if not isinstance(nodes, list):
            raise ValueError("graph.nodes must be a list")
        return cls(
            task_id=_str(data, "task_id"), nodes=[TaskNode.from_json(n) for n in nodes]
        )


def glob_to_regex(glob: str) -> re.Pattern[str]:
    """``**`` spans directories, ``*``/``?`` stay within one path segment."""

    parts: list[str] = []
    i = 0
    while i < len(glob):
        if glob.startswith("**/", i):
            parts.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            parts.append(".*")
            i += 2
        elif glob[i] == "*":
            parts.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(glob[i]))
            i += 1
    return re.compile("".join(parts) + r"\Z")


def in_scope(path: str, globs: list[str]) -> bool:
    rel = path.replace("\\", "/")
    return any(glob_to_regex(g).match(rel) for g in globs)


def scopes_overlap(a: list[str], b: list[str]) -> bool:
    """Conservative: may report overlap that does not exist, never misses a real one."""

    for g1 in a:
        for g2 in b:
            lit1, lit2 = not _WILDCARD.search(g1), not _WILDCARD.search(g2)
            if lit1 and lit2:
                if g1 == g2:
                    return True
            elif lit1 or lit2:
                literal, pattern = (g1, g2) if lit1 else (g2, g1)
                if in_scope(literal, [pattern]):
                    return True
            else:
                p1 = _WILDCARD.split(g1, maxsplit=1)[0]
                p2 = _WILDCARD.split(g2, maxsplit=1)[0]
                if p1.startswith(p2) or p2.startswith(p1):
                    return True
    return False


def _str(data: dict[str, JsonValue], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _opt_str(data: dict[str, JsonValue], key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) else None


def _str_list(data: dict[str, JsonValue], key: str) -> list[str]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{key} must be a list of strings")
    return [v for v in value if isinstance(v, str)]
