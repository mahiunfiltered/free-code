"""Request -> TaskGraph via one model call, with deterministic fallbacks.

The model may add structure; it can never break the run: any call, parse, or
validation failure degrades to one implementation node covering the whole request
(:func:`plan`), or to running the request directly in the chat (:func:`analyze`).
"""

import asyncio
import json
import re
import uuid
from dataclasses import dataclass
from typing import Literal, Protocol

from loguru import logger

from free_claude_code.core.json_types import JsonValue

from .graph import ROLES, TaskGraph, TaskNode

MAX_NODES = 6
# Output cap for the analysis call: a 6-node plan fits in ~1k tokens.
ANALYSIS_MAX_TOKENS = 1_500
_NODE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")

_NODE_SCHEMA = f"""{{
  "id": "lowercase-slug (a-z0-9_-), unique",
  "objective": "one line: what this agent must achieve",
  "instructions": "brief for this agent (1-3 sentences): what to do, files, constraints",
  "role": one of {sorted(ROLES)},
  "depends_on": ["ids of nodes that must finish first"],
  "write_scope": ["repo-relative globs this node may modify, e.g. src/api/**; [] = read-only"],
  "acceptance_criteria": ["1-3 short observable checks"]
}}"""

_NODE_RULES = f"""- At most {MAX_NODES} nodes; prefer fewer.
- Nodes that can run in parallel must have non-overlapping write scopes.
- Write scopes are forward-slash globs relative to the project root: no absolute paths, no "..".
- Explorer/reviewer/planner nodes are normally read-only (write_scope: []).
- Each agent sees only its own brief: make objective + instructions self-contained.
- The graph must be acyclic and every depends_on id must exist."""

SYSTEM_PROMPT = f"""You split a software change request into a small task graph for parallel coding agents.
Reply with ONE JSON object and nothing else, matching this schema:
{{"nodes": [{_NODE_SCHEMA}]}}
Rules:
- One node is fine for small changes.
{_NODE_RULES}"""

ANALYZE_PROMPT = f"""You are the lead agent of a team of coding agents working in one project folder.
Decide how to handle the user's request. Reply with ONE JSON object and nothing else:
{{"route": "direct" or "orchestrate",
 "reason": "one short sentence",
 "nodes": [{_NODE_SCHEMA}]}}
Choose "direct" (omit "nodes") for questions, explanations, reviews, and changes to one
file or to tightly coupled code: you will do it yourself right away.
Choose "orchestrate" whenever the request has 2 or more independent deliverables
(separate modules, files, features, components, or test suites), even if each one is
small: parallel sub-agents finish them faster than one agent working through them in turn.
Rules for "orchestrate":
- 2 to {MAX_NODES} nodes; parallel nodes must not write the same files.
{_NODE_RULES}"""


class ModelClient(Protocol):
    async def complete(self, system: str, user: str) -> str: ...


type Route = Literal["direct", "orchestrate"]


@dataclass
class Analysis:
    """The lead agent's decision: run directly, or orchestrate ``graph``."""

    route: Route
    reason: str
    graph: TaskGraph | None = None
    # True when no model decided (none configured, timeout, unusable reply).
    degraded: bool = False


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


async def analyze(
    request_text: str,
    model_client: ModelClient | None,
    *,
    task_id: str,
    timeout_s: float = 20.0,
) -> Analysis:
    """Classify and (for multi-part work) plan in one model call; never raises."""

    if model_client is None:
        return Analysis("direct", "no analysis model configured", degraded=True)
    try:
        async with asyncio.timeout(timeout_s):
            raw = await model_client.complete(ANALYZE_PROMPT, request_text)
        return parse_analysis(raw, task_id)
    except Exception as exc:
        reason = f"analysis unavailable ({str(exc) or type(exc).__name__})"
        logger.warning("Ultra analysis fell back to direct: {}", reason)
        return Analysis("direct", reason, degraded=True)


def parse_analysis(raw: str, task_id: str) -> Analysis:
    """Parse the lead agent's reply; raises ValueError when unusable.

    A plan that validates to a single node runs directly: a lone sub-agent would
    only add worktree and session overhead.
    """

    data = _first_json_object(raw)
    route, reason = data.get("route"), data.get("reason")
    reason = reason.strip() if isinstance(reason, str) else ""
    if route == "direct":
        return Analysis("direct", reason)
    if route != "orchestrate":
        raise ValueError(f"unknown route {route!r}")
    graph = _graph_from(data, task_id)
    if len(graph.nodes) < 2:
        return Analysis("direct", reason or "one sub-task: running it directly")
    return Analysis("orchestrate", reason, graph)


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

    return _graph_from(_first_json_object(raw), task_id)


def _graph_from(data: dict[str, JsonValue], task_id: str) -> TaskGraph:
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
