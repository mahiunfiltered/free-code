"""Structured failures parsed from tool output, with stable fingerprints (M0005 doc 08 §11).

Parsers: pytest, ruff (concise + full), mypy, ty/rustc-style ("code: msg --> path:line"),
tsc, jest/vitest, go test, cargo test panics, then a generic last-lines fallback.
Fingerprints keep file:line but strip temp dirs, uuids/hex ids, addresses and durations,
so the same failure matches across runs.
"""

import hashlib
import re
from dataclasses import dataclass
from typing import Literal

type Category = Literal[
    "environment", "build", "type", "runtime", "logic", "API", "database", "UI",
    "security", "performance", "provider", "tool", "test", "scope", "intent",
]  # fmt: skip


@dataclass
class Failure:
    check: str  # check id that produced it
    category: Category
    code: str
    message: str
    path: str | None = None
    line: int | None = None
    fingerprint: str = ""


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_UUID = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I
)
_HEX = re.compile(
    r"\b0x[0-9a-f]+\b|\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{8,}\b", re.I
)
_DURATION = re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|s|sec|secs|seconds|m|min)\b", re.I)
# Absolute prefix up to the deepest temp-like dir plus its (random) child: <tmp>/rest
_TEMP = re.compile(
    r"(?:[A-Za-z]:)?/(?:[^\s/:\"']+/)*(?:tmp|temp|pytest-of-[^/\s]+|pytest-\d+)/[^\s/\"']+/",
    re.I,
)


def normalize(text: str) -> str:
    text = _TEMP.sub("<tmp>/", text.replace("\\", "/"))
    text = _UUID.sub("<id>", text)
    text = _HEX.sub("<hex>", text)
    return _DURATION.sub("<dur>", text).strip()


def make_failure(
    check: str,
    category: Category,
    code: str,
    message: str,
    path: str | None = None,
    line: int | None = None,
) -> Failure:
    norm_path = normalize(path) if path else ""
    key = "|".join([check, code, norm_path, str(line or ""), normalize(message)])
    return Failure(
        check=check,
        category=category,
        code=code,
        message=message.strip()[:2000],
        path=path.replace("\\", "/") if path else None,
        line=line,
        fingerprint=hashlib.sha256(key.encode()).hexdigest()[:16],
    )


_KIND_CATEGORY: dict[str, Category] = {
    "typecheck": "type",
    "build": "build",
    "lint": "build",
    "unit_test": "test",
    "secret_scan": "security",
    "diff_scope": "scope",
    "intent_conformance": "intent",
    "http_probe": "API",
}
_TEXT_CATEGORY: list[tuple[re.Pattern[str], Category, str]] = [
    (
        re.compile(
            r"is not recognized as an internal or external command|command not found|No such file or directory: '|ModuleNotFoundError: No module named|spawn \S+ ENOENT",
            re.I,
        ),
        "environment",
        "missing_dependency",
    ),
    (
        re.compile(r"\b(?:429|rate.?limit(?:ed)?|quota exceeded|overloaded)\b", re.I),
        "provider",
        "rate_limited",
    ),
    (
        re.compile(
            r"\b(?:sqlite3?\.\w*Error|OperationalError|psycopg|IntegrityError|relation \"\w+\" does not exist)",
            re.I,
        ),
        "database",
        "database_error",
    ),
    (re.compile(r"\b(?:timed? ?out|ETIMEDOUT)\b", re.I), "tool", "timeout"),
    (
        re.compile(
            r"^Traceback \(most recent call last\)|^\w+Error: |panicked at", re.M
        ),
        "runtime",
        "runtime_error",
    ),
]


def categorize(kind: str, output: str) -> tuple[Category, str]:
    """Best category + code for raw output of a check of the given kind."""

    for pattern, category, code in _TEXT_CATEGORY:
        if pattern.search(output):
            if category == "runtime" and kind in _KIND_CATEGORY:
                break  # a traceback inside a test/build run is still that check's failure
            return category, code
    return _KIND_CATEGORY.get(kind, "runtime"), f"{kind}_failed"


# --------------------------------------------------------------------------- parsers

_PYTEST = re.compile(r"^(?:FAILED|ERROR)\s+(\S+?)(?:::(\S+))?(?:\s+-\s+(.+))?$")
_RUFF_CONCISE = re.compile(r"^(.+?):(\d+):(\d+):\s+([A-Z]+\d+)\s+(.+)$")
_MYPY = re.compile(
    r"^(.+?\.pyi?):(\d+):(?:\d+:)?\s+error:\s+(.+?)(?:\s+\[([\w-]+)\])?$"
)
_ARROW_HEAD = re.compile(r"^(?:error(?:\[([\w-]+)\])?|([A-Z]+\d+))(?::)?\s+(.+)$")
_ARROW_LOC = re.compile(r"^\s*-->\s+(.+?):(\d+)(?::\d+)?\s*$")
_TSC = re.compile(
    r"^(.+?)(?:\((\d+),\d+\)|:(\d+):\d+)\s*[:-]\s*error\s+(TS\d+):\s*(.+)$"
)
_JEST_FAIL = re.compile(r"^FAIL\s+(\S+)(?:\s+>\s+(.+))?$")
_JEST_BULLET = re.compile(
    r"^(?:\u25cf|\u00d7|\u2715)\s+(.+)$"
)  # jest bullet, vitest cross marks
_GO_FAIL = re.compile(r"^--- FAIL:\s+(\S+)")
_GO_LOC = re.compile(r"^(\S+\.go):(\d+):\s*(.*)$")
_CARGO_PANIC = re.compile(r"panicked at (\S+?):(\d+):\d+:?\s*(.*)$")
_CARGO_TEST = re.compile(r"^---- (\S+) stdout ----$")


def _lines(output: str) -> list[str]:
    return _ANSI.sub("", output).splitlines()


def _parse_pytest(check: str, lines: list[str]) -> list[Failure]:
    out: list[Failure] = []
    for raw in lines:
        m = _PYTEST.match(raw.strip())
        if not m or "." not in m.group(1):
            continue
        path, test, msg = m.groups()
        loc = re.compile(rf"^{re.escape(path)}:(\d+):")
        line = next(
            (int(x.group(1)) for ln in lines if (x := loc.match(ln.strip()))), None
        )
        message = f"{test}: {msg}" if test and msg else (test or msg or raw.strip())
        out.append(
            make_failure(
                check,
                "test",
                "test_failed" if raw.startswith("FAILED") else "test_error",
                message,
                path,
                line,
            )
        )
    return out


def _parse_ruff_mypy(check: str, lines: list[str]) -> list[Failure]:
    out: list[Failure] = []
    category: Category = "type" if check == "typecheck" else "build"
    for raw in lines:
        s = raw.strip()
        if m := _MYPY.match(s):
            out.append(
                make_failure(
                    check,
                    "type",
                    m.group(4) or "type_error",
                    m.group(3),
                    m.group(1),
                    int(m.group(2)),
                )
            )
        elif m := _RUFF_CONCISE.match(s):
            out.append(
                make_failure(
                    check, category, m.group(4), m.group(5), m.group(1), int(m.group(2))
                )
            )
    return out


def _parse_arrow(check: str, lines: list[str]) -> list[Failure]:
    """ruff full format, ty and rustc: 'CODE msg' / 'error[code]: msg' then '--> path:line:col'."""

    out: list[Failure] = []
    category: Category = "type" if check == "typecheck" else "build"
    for i, raw in enumerate(lines):
        head = _ARROW_HEAD.match(raw.strip())
        if not head:
            continue
        for nxt in lines[i + 1 : i + 4]:
            if loc := _ARROW_LOC.match(nxt):
                code = head.group(1) or head.group(2) or "error"
                out.append(
                    make_failure(
                        check,
                        category,
                        code,
                        head.group(3),
                        loc.group(1),
                        int(loc.group(2)),
                    )
                )
                break
    return out


def _parse_tsc(check: str, lines: list[str]) -> list[Failure]:
    return [
        make_failure(
            check,
            "type",
            m.group(4),
            m.group(5),
            m.group(1),
            int(m.group(2) or m.group(3)),
        )
        for raw in lines
        if (m := _TSC.match(raw.strip()))
    ]


def _parse_jest(check: str, lines: list[str]) -> list[Failure]:
    out: list[Failure] = []
    current: str | None = None
    for raw in lines:
        s = raw.strip()
        if m := _JEST_FAIL.match(s):
            current = m.group(1)
            if m.group(2):  # vitest: "FAIL  path > suite > test"
                out.append(
                    make_failure(check, "test", "test_failed", m.group(2), current)
                )
        elif (m := _JEST_BULLET.match(s)) and current:
            out.append(make_failure(check, "test", "test_failed", m.group(1), current))
    return out


def _parse_go(check: str, lines: list[str]) -> list[Failure]:
    out: list[Failure] = []
    for i, raw in enumerate(lines):
        if not (m := _GO_FAIL.match(raw.strip())):
            continue
        loc = next(
            (x for ln in lines[i + 1 : i + 6] if (x := _GO_LOC.match(ln.strip()))), None
        )
        out.append(
            make_failure(check, "test", "test_failed", f"{m.group(1)}: {loc.group(3) if loc else ''}".strip(": "),
                         loc.group(1) if loc else None, int(loc.group(2)) if loc else None)
        )  # fmt: skip
    return out


def _parse_cargo(check: str, lines: list[str]) -> list[Failure]:
    out: list[Failure] = []
    test = ""
    for raw in lines:
        if m := _CARGO_TEST.match(raw.strip()):
            test = m.group(1)
        elif m := _CARGO_PANIC.search(raw):
            msg = f"{test}: {m.group(3)}" if test else m.group(3) or "panicked"
            out.append(
                make_failure(
                    check, "test", "test_failed", msg, m.group(1), int(m.group(2))
                )
            )
    return out


_PARSERS = (
    _parse_pytest,
    _parse_tsc,
    _parse_arrow,
    _parse_ruff_mypy,
    _parse_jest,
    _parse_go,
    _parse_cargo,
)


def parse_failures(check: str, kind: str, output: str) -> list[Failure]:
    """Structured failures from a failed command's output (never empty)."""

    lines = _lines(output)
    for parser in _PARSERS:
        found = parser(check, lines)
        if found:
            return found
    category, code = categorize(kind, output)
    tail = "\n".join([ln for ln in lines if ln.strip()][-20:]) or "(no output)"
    return [make_failure(check, category, code, tail)]
