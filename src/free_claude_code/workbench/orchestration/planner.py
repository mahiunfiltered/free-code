"""Request -> TaskGraph via one model call, with a deterministic single-node fallback.

The model may add structure; it can never break the run: any call, parse, or
validation failure degrades to one implementation node covering the whole request.
"""

import json
import re
import uuid
from typing import Protocol

from loguru import logger

from free_claude_code.core.json_types import JsonValue

from .graph import ROLES, TaskGraph, TaskNode

MAX_NODES = 6
_NODE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")

SYSTEM_PROMPT = f"""You split a software change request into a small task graph for parallel coding agents.
Reply with ONE JSON object and nothing else, matching this schema:
{{"nodes": [{{
  "id": "lowercase-slug (a-z0-9_-), unique",
  "objective": "what this agent must do, self-contained",
  "role": one of {sorted(ROLES)},
  "depends_on": ["ids of nodes that must finish first"],
  "write_scope": ["repo-relative globs this node may modify, e.g. src/api/**; [] = read-only"],
  "acceptance_criteria": ["observable checks for this node"]
}}]}}
Rules:
- At most {MAX_NODES} nodes; prefer fewer. One node is fine for small changes.
- Nodes that can run in parallel must have non-overlapping write scopes.
- Write scopes are forward-slash globs relative to the repository root: no absolute paths, no "..".
- Explorer/reviewer/planner nodes are normally read-only (write_scope: []).
- The graph must be acyclic and every depends_on id must exist."""


class ModelClient(Protocol):
    async def complete(self, system: str, user: str) -> str: ...


async def plan(
    request_text: str,
    contract_block: str | None,
    model_client: ModelClient,
    *,
    task_id: str | None = None,
) -> TaskGraph:
    task_id = task_id or uuid.uuid4().hex[:12]
    user = request_text if not contract_block else f"{request_text}\n\n{contract_block}"
    try:
        raw = await model_client.complete(SYSTEM_PROMPT, user)
        return parse_plan(raw, task_id)
    except Exception as exc:
        logger.warning("Planner fell back to a single node: {}", exc)
        return fallback_plan(request_text, task_id)


def fallback_plan(request_text: str, task_id: str) -> TaskGraph:
    return TaskGraph(
        task_id=task_id,
        nodes=[
            TaskNode(
                id="implementation",
                objective=request_text,
                role="implementation",
                write_scope=["**"],
            )
        ],
    )


def parse_plan(raw: str, task_id: str) -> TaskGraph:
    """Parse and validate model output; raises ValueError when unusable."""

    data = _first_json_object(raw)
    items = data.get("nodes")
    if not isinstance(items, list) or not items:
        raise ValueError("plan has no nodes")
    nodes: list[TaskNode] = []
    for item in items[:MAX_NODES]:
        node = TaskNode.from_json(item)
        if node.status != "pending" or not _NODE_ID.match(node.id):
            raise ValueError(f"invalid node id or status: {node.id!r}")
        node.write_scope = [_clean_glob(g) for g in node.write_scope]
        nodes.append(node)
    # Trimming may orphan dependents of dropped nodes; drop those too.
    kept = {n.id for n in nodes}
    changed = True
    while changed:
        orphans = [n for n in nodes if not set(n.depends_on) <= kept]
        changed = bool(orphans)
        for orphan in orphans:
            nodes.remove(orphan)
            kept.discard(orphan.id)
    graph = TaskGraph(task_id=task_id, nodes=nodes)
    errors = graph.validate() if nodes else ["empty after trim"]
    if errors:
        raise ValueError(f"invalid plan: {errors}")
    return graph


def _clean_glob(glob: str) -> str:
    cleaned = glob.strip().replace("\\", "/").removeprefix("./")
    if (
        not cleaned
        or cleaned.startswith("/")
        or re.match(r"^[a-zA-Z]:", cleaned)
        or ".." in cleaned.split("/")
    ):
        raise ValueError(f"write scope must be a repo-relative glob: {glob!r}")
    return cleaned


def _first_json_object(raw: str) -> dict[str, JsonValue]:
    start = raw.find("{")
    if start < 0:
        raise ValueError("no JSON object in model output")
    value, _ = json.JSONDecoder().raw_decode(raw[start:])
    if not isinstance(value, dict):
        raise ValueError("model output is not a JSON object")
    return value
