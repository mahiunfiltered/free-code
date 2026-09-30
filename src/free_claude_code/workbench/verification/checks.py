"""Check runners (M0005 doc 08 §5-10). Each returns a CheckResult; none ever raises for
tool failures.

    run_command(argv, cwd, timeout=, output_cap=) -> CommandRun   # tree-kill on timeout
    run_command_check(check, cwd, timeout=, output_cap=) -> CheckResult
    secret_scan(diff) -> CheckResult          # added lines only; secrets never echoed
    diff_scope(contract, diff) -> CheckResult  # allowed / protected path globs
    http_probe(check, timeout=) -> CheckResult
    gap_matrix(contract, diff, results, head_moved) -> list[GapRow]
    conformance_result(check, rows) -> CheckResult
"""

import asyncio
import contextlib
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Literal

import httpx

from free_claude_code.cli.process_registry import (
    kill_pid_tree_best_effort,
    register_pid,
    unregister_pid,
)
from free_claude_code.workbench.checkpoints import TreeDiff
from free_claude_code.workbench.intent import (
    IntentContract,
    matches_any,
    path_glob,
    path_tokens,
    requirement_rows,
    split_scope_exclusion,
)
from free_claude_code.workbench.verification.failures import (
    Failure,
    make_failure,
    parse_failures,
)
from free_claude_code.workbench.verification.profile import PlannedCheck

type CheckStatus = Literal["passed", "failed", "error", "skipped", "needs_review"]
type RowStatus = Literal["covered", "missing", "failed"]

DEFAULT_OUTPUT_CAP = 64_000


@dataclass
class CheckResult:
    id: str
    kind: str
    required: bool
    status: CheckStatus
    summary: str
    command: list[str] | None = None
    exit_code: int | None = None
    duration_s: float = 0.0
    output: str = ""
    output_truncated: bool = False
    failures: list[Failure] = field(default_factory=list)


@dataclass
class GapRow:
    requirement_id: str
    kind: str
    text: str
    status: RowStatus
    evidence: list[str] = field(default_factory=list)


@dataclass
class CommandRun:
    exit_code: int | None
    output: str
    truncated: bool
    timed_out: bool
    duration_s: float
    error: str | None = None  # spawn failure (missing executable, bad cwd)


# --------------------------------------------------------------------------- commands


def _kill_tree(pid: int) -> None:
    if sys.platform == "win32":
        kill_pid_tree_best_effort(pid)
    else:
        with contextlib.suppress(OSError):
            os.killpg(pid, signal.SIGKILL)  # own session: pgid == pid


async def run_command(
    argv: list[str],
    cwd: str,
    *,
    timeout: float = 600.0,
    output_cap: int = DEFAULT_OUTPUT_CAP,
) -> CommandRun:
    """Run argv (no shell), keep the last `output_cap` chars, kill the tree on timeout."""

    started = time.monotonic()
    exe = shutil.which(argv[0]) or argv[0]  # resolves npm.cmd & co on Windows
    env = {**os.environ, "CI": "1", "NO_COLOR": "1", "PYTHONIOENCODING": "utf-8"}
    try:
        proc = await asyncio.create_subprocess_exec(
            exe,
            *argv[1:],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=sys.platform != "win32",
        )
    except OSError as exc:
        return CommandRun(
            None, str(exc), False, False, time.monotonic() - started, error=str(exc)
        )
    register_pid(proc.pid)
    buf = bytearray()
    total = 0

    async def pump() -> None:
        nonlocal total
        assert proc.stdout is not None
        while chunk := await proc.stdout.read(65536):
            total += len(chunk)
            buf.extend(chunk)
            if len(buf) > 2 * output_cap:
                del buf[: len(buf) - output_cap]

    timed_out = False
    try:
        await asyncio.wait_for(asyncio.gather(pump(), proc.wait()), timeout)
    except TimeoutError:
        timed_out = True
    finally:
        if proc.returncode is None:
            _kill_tree(proc.pid)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), 10)
        unregister_pid(proc.pid)
    text = buf.decode("utf-8", "replace")
    truncated = total > output_cap or len(text) > output_cap
    if truncated:
        text = f"...[truncated, {total} bytes total]...\n" + text[-output_cap:]
    return CommandRun(
        None if timed_out else proc.returncode,
        text,
        truncated,
        timed_out,
        time.monotonic() - started,
    )


async def run_command_check(
    check: PlannedCheck,
    cwd: str,
    *,
    timeout: float = 600.0,
    output_cap: int = DEFAULT_OUTPUT_CAP,
) -> CheckResult:
    argv = check.argv or []
    result = CheckResult(
        check.id, check.kind, check.required, "passed", "", command=argv
    )
    if not argv:
        result.status, result.summary = "skipped", "no command"
        return result
    run = await run_command(argv, cwd, timeout=timeout, output_cap=output_cap)
    result.exit_code, result.duration_s = run.exit_code, round(run.duration_s, 3)
    result.output, result.output_truncated = run.output, run.truncated
    if run.error is not None:
        result.status, result.summary = (
            "error",
            f"could not start {argv[0]}: {run.error}",
        )
        result.failures = [
            make_failure(check.id, "environment", "command_not_found", result.summary)
        ]
    elif run.timed_out:
        result.status, result.summary = (
            "error",
            f"{check.kind} timed out after {timeout:g}s",
        )
        result.failures = [make_failure(check.id, "tool", "timeout", result.summary)]
    elif run.exit_code == 0:
        result.summary = f"{check.kind} passed (exit 0)"
    else:
        result.status = "failed"
        result.failures = parse_failures(check.id, check.kind, run.output)
        result.summary = f"{check.kind} failed (exit {run.exit_code}): {result.failures[0].message.splitlines()[0][:200]}"
    return result


# --------------------------------------------------------------------------- diff helpers

_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("nvidia_api_key", re.compile(r"nvapi-[A-Za-z0-9_-]{20,}")),
    ("api_key_sk", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private_key", re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    (
        "secret_assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret|token|passw(?:or)?d)\b[\"']?\s*[:=]\s*[\"']([^\"'\s]{12,})[\"']"
        ),
    ),
]
_ENV_FILE = re.compile(r"(?:^|/)\.env(?:\.[\w-]+)?$")
_ENV_TEMPLATE = re.compile(r"\.(?:example|sample|template|dist)$")
_ENV_ASSIGN = re.compile(
    r"^\s*(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*\s*=\s*[\"']?([^\"'#\s]+)"
)
_PLACEHOLDER = re.compile(
    r"(?i)example|changeme|your[_-]|xxx|<|\$\{|dummy|placeholder|^(?:true|false|\d+)$"
)


def iter_added_lines(diff_text: str) -> list[tuple[str, int, str]]:
    """(path, new line number, content) for every '+' line of a unified diff."""

    out: list[tuple[str, int, str]] = []
    path, line = "", 0
    for raw in diff_text.splitlines():
        if raw.startswith("+++ "):
            path = raw[4:].removeprefix("b/")
        elif raw.startswith("@@"):
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", raw)
            line = int(m.group(1)) if m else 0
        elif raw.startswith("+"):
            out.append((path, line, raw[1:]))
            line += 1
        elif raw.startswith(" "):
            line += 1
    return out


def _redact(text: str, secret: str) -> str:
    return text.replace(secret, secret[:4] + "…[REDACTED]").strip()[:200]


def secret_scan(check: PlannedCheck, diff: TreeDiff | None) -> CheckResult:
    result = CheckResult(
        check.id, "secret_scan", check.required, "passed", "no secrets in added lines"
    )
    if diff is None:
        result.status, result.summary = "skipped", "no git checkpoint: diff unavailable"
        return result
    for path, line, content in iter_added_lines(diff.text):
        found: list[tuple[str, str]] = []
        for name, pattern in _SECRET_PATTERNS:
            m = pattern.search(content)
            if m and not _PLACEHOLDER.search(m.group(m.lastindex or 0)):
                found.append((name, m.group(m.lastindex or 0)))
        if _ENV_FILE.search(path) and not _ENV_TEMPLATE.search(path):
            m = _ENV_ASSIGN.match(content)
            if m and not _PLACEHOLDER.search(m.group(1)):
                found.append(("env_value", m.group(1)))
        for name, secret in found[:1]:
            msg = f"{name} added in {path}:{line}: {_redact(content, secret)}"
            result.failures.append(
                make_failure(check.id, "security", "secret_detected", msg, path, line)
            )
    if result.failures:
        result.status = "failed"
        result.summary = f"{len(result.failures)} secret(s) in added lines; first: {result.failures[0].message}"
    return result


def diff_scope(
    check: PlannedCheck, contract: IntentContract, diff: TreeDiff | None
) -> CheckResult:
    result = CheckResult(
        check.id, "diff_scope", check.required, "passed", "all changes within scope"
    )
    if diff is None:
        result.status, result.summary = "skipped", "no git checkpoint: diff unavailable"
        return result
    s = contract.scope
    for f in diff.files:
        if matches_any(f.path, s.protected_paths):
            result.failures.append(
                make_failure(
                    check.id,
                    "scope",
                    "protected_path",
                    f"changed protected path {f.path}",
                    f.path,
                )
            )
        elif s.allowed_paths and not matches_any(f.path, s.allowed_paths):
            result.failures.append(
                make_failure(
                    check.id,
                    "scope",
                    "outside_scope",
                    f"changed {f.path} outside allowed scope {s.allowed_paths}",
                    f.path,
                )
            )
    if result.failures:
        result.status = "failed"
        result.summary = f"{len(result.failures)} out-of-scope change(s): " + ", ".join(
            f.path or "" for f in result.failures[:5]
        )
    result.summary += f" ({len(diff.files)} file(s) changed)"
    return result


async def http_probe(check: PlannedCheck, *, timeout: float = 10.0) -> CheckResult:
    probe = check.probe
    result = CheckResult(check.id, "http_probe", check.required, "passed", "")
    if probe is None:
        result.status, result.summary = "skipped", "no probe configured"
        return result
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(probe.url)
    except httpx.HTTPError as exc:
        result.status, result.summary = (
            "error",
            f"GET {probe.url} failed: {type(exc).__name__}: {exc}",
        )
        result.failures = [
            make_failure(check.id, "API", "probe_unreachable", result.summary)
        ]
        return result
    result.duration_s = round(time.monotonic() - started, 3)
    result.exit_code = response.status_code
    result.summary = (
        f"GET {probe.url} -> {response.status_code} (expected {probe.expect_status})"
    )
    if response.status_code != probe.expect_status:
        result.status = "failed"
        result.output = response.text[:2000]
        result.failures = [
            make_failure(check.id, "API", "unexpected_status", result.summary)
        ]
    return result


# --------------------------------------------------------------------------- conformance


def _file_chunks(diff_text: str) -> dict[str, str]:
    chunks: dict[str, list[str]] = {}
    current: list[str] = []
    for raw in diff_text.splitlines():
        if raw.startswith("diff --git "):
            m = re.match(r"diff --git a/(.+?) b/(.+)$", raw)
            current = chunks.setdefault(m.group(2) if m else raw, [])
        current.append(raw)
    return {k: "\n".join(v) for k, v in chunks.items()}


def _changed_lines(chunk: str) -> list[str]:
    return [
        ln
        for ln in chunk.splitlines()
        if ln[:1] in "+-" and not ln.startswith(("+++", "---"))
    ]


_DEP_BLOCK = re.compile(
    r'"(?:dependencies|devDependencies|peerDependencies|optionalDependencies)"\s*:\s*\{'
)
_PY_DEP_LINE = re.compile(
    r'^\+\s*"[A-Za-z][\w.-]*(?:\[[^\]]*\])?\s*(?:[<>=~!;]|",?\s*$)'
)


def _dependency_hits(files: dict[str, str]) -> list[str]:
    hits = []
    for path, chunk in files.items():
        name = path.rsplit("/", 1)[-1]
        changed = _changed_lines(chunk)
        if name == "package.json":
            in_deps, hit = False, False
            for raw in chunk.splitlines():
                content = raw[1:] if raw[:1] in "+- " else raw
                if _DEP_BLOCK.search(content):
                    in_deps = True
                elif in_deps and re.match(r"^\s*\},?\s*$", content):
                    in_deps = False
                elif in_deps and raw[:1] in "+-" and not raw.startswith(("+++", "---")):
                    hit = True
            if hit:
                hits.append(path)
        elif (
            name == "pyproject.toml" and any(_PY_DEP_LINE.match(ln) for ln in changed)
        ) or (
            re.fullmatch(r"requirements.*\.txt|go\.mod|Cargo\.toml|Pipfile", name)
            and any(
                ln.startswith("+")
                and ln[1:].strip()
                and not ln[1:].strip().startswith("#")
                for ln in changed
            )
        ):
            hits.append(path)
    return hits


def _paths(files: dict[str, str], pattern: str) -> list[str]:
    rx = re.compile(pattern, re.I)
    return [p for p in files if rx.search(p)]


def _removed_public_api(files: dict[str, str]) -> list[str]:
    rx = re.compile(
        r"^-(?:export\b|(?:async\s+)?def\s+[A-Za-z]|class\s+[A-Za-z]|pub\s+fn\s)"
    )
    return [
        p
        for p, chunk in files.items()
        if any(rx.match(ln) for ln in _changed_lines(chunk))
    ]


def _code_fence_edits(files: dict[str, str]) -> list[str]:
    hits = []
    for path, chunk in files.items():
        in_fence = False
        for raw in chunk.splitlines():
            if raw.startswith(("@@", "+++", "---", "diff ")):
                continue
            if raw[:1] in "+-" and in_fence:
                hits.append(path)
                break
            if raw[1:].strip().startswith("```"):
                in_fence = not in_fence
    return hits


@dataclass
class _Ctx:
    files: dict[str, str]  # path -> per-file diff chunk (every changed file present)
    statuses: dict[str, str]
    head_moved: bool | None


# requirement-text pattern -> evaluator returning offending paths, or None if undecidable
_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"database|schema|migration", re.I), "db"),
    (re.compile(r"dependenc|packages?\b|librar", re.I), "deps"),
    (re.compile(r"\bauth", re.I), "auth"),
    (re.compile(r"public api|\bexports?\b|signatures?", re.I), "api"),
    (re.compile(r"code blocks?", re.I), "fence"),
    (re.compile(r"\bcommit", re.I), "commit"),
    (re.compile(r"\bpush", re.I), "push"),
    (re.compile(r"\b(?:delet|remov)\w*\b.*\bfiles?\b", re.I), "delete"),
    (re.compile(r"\btests?\b|\bspecs?\b", re.I), "tests"),
]
# Conventional test locations/names (pytest, jest/vitest, go).
_TEST_PATH = (
    r"(?:^|/)(?:tests?|__tests__|spec)/|(?:^|/)test_[^/]*\.py$|_test\.(?:py|go)$"
    r"|\.(?:test|spec)\.[jt]sx?$|(?:^|/)conftest\.py$"
)
_OP_RULE = {
    "dependency.add": "deps",
    "db.migration": "db",
    "git.commit": "commit",
    "git.push": "push",
    "fs.remove": "delete",
}


def _evaluate(rule: str, ctx: _Ctx) -> list[str] | None:
    match rule:
        case "db":
            return _paths(
                ctx.files, r"(?:^|/)(?:migrations?|alembic|prisma)/|\.sql$|schema"
            )
        case "deps":
            return _dependency_hits(ctx.files)
        case "auth":
            return _paths(ctx.files, r"auth|oauth|jwt|passport|credential")
        case "api":
            return _removed_public_api(ctx.files)
        case "fence":
            return _code_fence_edits(ctx.files)
        case "commit":
            return (
                None
                if ctx.head_moved is None
                else (["HEAD moved"] if ctx.head_moved else [])
            )
        case (
            "push"
        ):  # ponytail: no new commits => nothing new to push; cannot observe pushes
            return [] if ctx.head_moved is False else None
        case "delete":
            return [p for p, s in ctx.statuses.items() if s == "D"]
        case "tests":
            return _paths(ctx.files, _TEST_PATH)
    return None


def _row_from_rules(
    rid: str, kind: str, text: str, rules: list[str], globs: list[str], ctx: _Ctx
) -> GapRow:
    row = GapRow(rid, kind, text, "missing")
    decided = False
    for rule in rules:
        hits = _evaluate(rule, ctx)
        if hits is None:
            continue
        decided = True
        if hits:
            row.status = "failed"
            row.evidence.append(f"{rule}: violated by {', '.join(hits[:5])}")
    if globs:
        decided = True
        touched = [p for p in ctx.files if matches_any(p, globs)]
        if touched:
            row.status = "failed"
            row.evidence.append(f"changed {', '.join(touched[:5])}")
    if decided and row.status != "failed":
        row.status = "covered"
        row.evidence.append("diff heuristics found no violation")
    if not decided:
        row.evidence.append("no diff heuristic can decide this; needs review")
    return row


def gap_matrix(
    contract: IntentContract,
    diff: TreeDiff | None,
    results: list[CheckResult],
    head_moved: bool | None,
) -> list[GapRow]:
    """Gap matrix: requirement -> evidence -> status (covered | missing | failed).

    MUST: covered by a passing unit_test/http_probe, failed if one failed, else missing.
    PRESERVE: covered by passing regression evidence; failed only if a preserved path
    changed; a failing test leaves it missing (the test failure itself is retried).
    MUST_NOT / prohibited ops: diff heuristics; undecidable -> missing (needs review).
    """

    behaviour = [
        r
        for r in results
        if r.kind in ("unit_test", "http_probe") and r.status != "skipped"
    ]
    passing = [r.id for r in behaviour if r.status == "passed"]
    failing = [r.id for r in behaviour if r.status in ("failed", "error")]
    chunks = _file_chunks(diff.text) if diff is not None else {}
    ctx = (
        _Ctx(
            files={f.path: chunks.get(f.path, "") for f in diff.files},
            statuses={f.path: f.status for f in diff.files},
            head_moved=head_moved,
        )
        if diff is not None
        else None
    )
    rows: list[GapRow] = []
    for rid, kind, text in requirement_rows(contract):
        if kind in ("should", "optional"):
            continue
        if kind == "must_not":
            if ctx is None:
                rows.append(GapRow(rid, kind, text, "missing", ["diff unavailable"]))
                continue
            # Only the forbidding head names forbidden paths/topics: in "modify any
            # file outside a.py" a.py is the allowed exception, not a violation.
            head, excepted = split_scope_exclusion(text)
            rules = [name for rx, name in _RULES if rx.search(head)]
            row = _row_from_rules(
                rid,
                kind,
                text,
                rules,
                [path_glob(t) for t in path_tokens(head)],
                ctx,
            )
            if excepted:
                allowed = [path_glob(t) for t in excepted]
                outside = [p for p in ctx.files if not matches_any(p, allowed)]
                if outside:
                    row.status = "failed"
                    row.evidence.append(
                        f"changed outside {', '.join(allowed)}: {', '.join(outside[:5])}"
                    )
                elif row.status == "missing":
                    row.status = "covered"
                    row.evidence = [f"all changes within {', '.join(allowed)}"]
            rows.append(row)
            continue
        # A failing test fails a MUST (the behaviour is not there yet) but cannot
        # prove a PRESERVE was broken: it is already reported (and retried) as a
        # test failure, so the preserve row stays unconfirmed until tests pass.
        # Only a change to the preserved thing itself violates it (below).
        status: RowStatus = (
            "covered"
            if passing and not failing
            else "failed"
            if failing and kind == "must"
            else "missing"
        )
        row = GapRow(rid, kind, text, status)
        row.evidence = [f"{c}: failed" for c in failing] + [
            f"{c}: passed" for c in passing
        ]
        if not behaviour:
            row.evidence.append("no test or probe evidence available")
        globs = (
            [path_glob(t) for t in path_tokens(split_scope_exclusion(text)[0])]
            if kind == "preserve"
            else []
        )
        # A file the contract allows editing can only be partly preserved ("all other
        # code in calc.py"); a file-level check cannot judge that, the tests do.
        touched = [
            p
            for p in (ctx.files if ctx else {})
            if matches_any(p, globs)
            and not matches_any(p, contract.scope.allowed_paths)
        ]
        if touched:
            row.status = "failed"
            row.evidence.append(f"preserved path changed: {', '.join(touched[:5])}")
        rows.append(row)
    for i, op in enumerate(contract.scope.prohibited_ops, 1):
        rule = _OP_RULE.get(op)
        if ctx is None or rule is None:
            rows.append(
                GapRow(
                    f"X{i}",
                    "prohibited_op",
                    op,
                    "missing",
                    ["cannot observe this operation"],
                )
            )
        else:
            rows.append(_row_from_rules(f"X{i}", "prohibited_op", op, [rule], [], ctx))
    return rows


def conformance_result(check: PlannedCheck, rows: list[GapRow]) -> CheckResult:
    """intent_conformance: failed if any row failed, needs_review if any row lacks evidence."""

    result = CheckResult(check.id, "intent_conformance", check.required, "passed", "")
    for row in rows:
        if row.status == "failed":
            result.failures.append(
                make_failure(
                    check.id,
                    "intent",
                    f"{row.kind}_violated",
                    f"{row.requirement_id} ({row.kind}) '{row.text}': {'; '.join(row.evidence)}",
                )
            )
    missing = [r.requirement_id for r in rows if r.status == "missing"]
    if result.failures:
        result.status, result.summary = (
            "failed",
            f"{len(result.failures)} requirement(s) violated",
        )
    elif missing:
        result.status, result.summary = (
            "needs_review",
            f"no evidence for {', '.join(missing)}",
        )
    else:
        result.summary = f"all {len(rows)} requirement(s) covered"
    return result
