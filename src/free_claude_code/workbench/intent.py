"""Intent contract compiler (M0005 docs 03, ADR-004/009/021).

Public API:
    compile_intent(text, model=None, *, authorize_defaults=False, timeout=45.0) -> CompiledIntent
    extract_deterministic(text) -> CompiledIntent            # no model, never fails
    render_contract(contract) -> str                         # system-prompt block for the agent
    requirement_rows(contract) -> list[tuple[id, kind, text]] # M1.., N1.., P1.. ids
    IntentContract.to_json() / IntentContract.from_json(obj)
    ModelClient  (Protocol: async complete(system, user) -> str)

Rules: deterministic extraction always runs and its MUST/MUST_NOT/PRESERVE items are never
removed or rewritten; a model may only ADD items (contradictions are kept with a warning),
except that model MUST_NOT/protected paths never forbid paths the user explicitly allowed
(dropped with a warning) and "modify anything outside X" items become allowed scope;
invalid model output degrades to the deterministic contract. High/critical ambiguities or
non-reversible assumptions block for clarification (assumption firewall).
"""

import asyncio
import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from typing import Literal, Protocol, cast

from free_claude_code.core.json_types import JsonObject, JsonValue

type Risk = Literal["low", "medium", "high", "critical"]
type IntentStatus = Literal["ready_to_lock", "blocked_for_clarification"]

RISKS: tuple[Risk, ...] = ("low", "medium", "high", "critical")
_LIST_FIELDS = ("must", "must_not", "preserve", "should", "optional")


class ModelClient(Protocol):
    async def complete(self, system: str, user: str) -> str: ...


@dataclass
class Scope:
    allowed_paths: list[str] = field(default_factory=list)
    protected_paths: list[str] = field(default_factory=list)
    allowed_ops: list[str] = field(default_factory=list)
    prohibited_ops: list[str] = field(default_factory=list)


@dataclass
class IntentContract:
    goal: str
    must: list[str] = field(default_factory=list)
    must_not: list[str] = field(default_factory=list)
    preserve: list[str] = field(default_factory=list)
    should: list[str] = field(default_factory=list)
    optional: list[str] = field(default_factory=list)
    scope: Scope = field(default_factory=Scope)
    assumptions: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    verification_requirements: list[str] = field(default_factory=list)
    risk: Risk = "medium"
    source_text_hash: str = ""
    version: int = 1

    def to_json(self) -> JsonObject:
        return cast(JsonObject, asdict(self))

    @classmethod
    def from_json(cls, obj: JsonObject) -> IntentContract:
        def strs(value: JsonValue) -> list[str]:
            return [str(v) for v in value] if isinstance(value, list) else []

        raw_scope = obj.get("scope")
        scope = raw_scope if isinstance(raw_scope, dict) else {}
        risk = obj.get("risk")
        version = obj.get("version")
        return cls(
            goal=str(obj.get("goal", "")),
            must=strs(obj.get("must")),
            must_not=strs(obj.get("must_not")),
            preserve=strs(obj.get("preserve")),
            should=strs(obj.get("should")),
            optional=strs(obj.get("optional")),
            scope=Scope(
                allowed_paths=strs(scope.get("allowed_paths")),
                protected_paths=strs(scope.get("protected_paths")),
                allowed_ops=strs(scope.get("allowed_ops")),
                prohibited_ops=strs(scope.get("prohibited_ops")),
            ),
            assumptions=strs(obj.get("assumptions")),
            unknowns=strs(obj.get("unknowns")),
            acceptance_criteria=strs(obj.get("acceptance_criteria")),
            verification_requirements=strs(obj.get("verification_requirements")),
            risk=risk if isinstance(risk, str) and risk in RISKS else "medium",
            source_text_hash=str(obj.get("source_text_hash", "")),
            version=version if isinstance(version, int) else 1,
        )


@dataclass
class CompiledIntent:
    contract: IntentContract
    status: IntentStatus
    questions: list[str]
    warnings: list[str]
    intent_class: str
    degraded: bool  # True when no (valid) model extraction contributed


# --------------------------------------------------------------------------- text helpers

_KW = (
    r"(?:don't|do not|never|must not|should not|shouldn't|cannot|can't|without|avoid"
    r"|no(?:thing)?|leave|keep|preserve|retain|maintain)"
)
_IMPERATIVE = (
    r"(?:add|fix|make|update|change|create|remove|delete|write|use|ensure|implement"
    r"|refactor|rename|move|run|test|return|handle|support|must|should|need|needs"
    r"|also|then|only|improve|convert|replace|document|translate)"
)
_SPLIT_RE = re.compile(
    rf"[.!?]+(?=\s|$)|;+|\n+|,?\s*\b(?:and|but|while)\b\s+(?=(?:{_KW}|{_IMPERATIVE})\b)"
    rf"|,\s*(?=(?:{_KW}|{_IMPERATIVE})\b)",
    re.IGNORECASE,
)


def _norm(text: str) -> str:
    return re.sub(r"[.,;:!?]+$", "", " ".join(text.lower().split()))


def _clean(text: str) -> str:
    return re.sub(r"^[,;:\s]+|[.,;:\s]+$", "", text).strip()


def _split_clauses(text: str) -> list[str]:
    return [c for c in (_clean(p) for p in _SPLIT_RE.split(text)) if c]


def _overlaps(a: str, b: str) -> bool:
    na, nb = _norm(a), _norm(b)
    return bool(na and nb) and (na in nb or nb in na)


_PATH_TOKEN_RE = re.compile(r"[\w.*/\\-]+")


def path_tokens(text: str) -> list[str]:
    """Path-like tokens: contain '/', are dotfiles (.env), or look like name.ext."""

    out: list[str] = []
    for raw in _PATH_TOKEN_RE.findall(text.replace("`", " ").replace("'", " ")):
        tok = raw.replace("\\", "/").strip(".,") if raw != ".env" else raw
        if raw.startswith(".") and not tok.startswith("."):
            tok = "." + tok
        if not re.search(r"[A-Za-z]", tok) or tok.lower() in {"e.g", "i.e", "etc"}:
            continue
        if (
            "/" in tok
            or re.fullmatch(r"\.[\w-]+(\.[\w-]+)*", tok)
            or re.fullmatch(r"[\w*-]+(\.[\w*-]+)*\.[A-Za-z][\w]{0,7}", tok)
        ):
            out.append(tok)
    return out


def path_glob(token: str) -> str:
    """'src/legacy' or 'src/legacy/' -> 'src/legacy/**'; bare 'x.py' -> '**/x.py'."""

    tok = token.strip("/").removeprefix("./")
    if "*" in tok:
        return tok
    last = tok.rsplit("/", 1)[-1]
    is_file = "." in last and not token.endswith("/")
    if not is_file:
        return f"{tok}/**"
    return tok if "/" in tok else f"**/{tok}"


def _glob_re(pattern: str) -> re.Pattern[str]:
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        else:
            ch = pattern[i]
            out.append("[^/]*" if ch == "*" else "[^/]" if ch == "?" else re.escape(ch))
            i += 1
    return re.compile("".join(out) + "$", re.I if sys.platform == "win32" else 0)


def matches_any(path: str, patterns: list[str]) -> bool:
    return any(
        _glob_re(p.replace("\\", "/").removeprefix("./")).match(path) for p in patterns
    )


_EXCLUSION = re.compile(
    r"\b(?:outside(?:\s+of)?|other than|except(?:\s+for)?|besides|apart from|beyond)\b",
    re.I,
)


def split_scope_exclusion(text: str) -> tuple[str, list[str]]:
    """Split "modify any file outside a.py and b/c.py" into its forbidding head and
    the excepted paths: ("modify any file", ["a.py", "b/c.py"]).

    Paths after an exclusion word are the allowed exception, never forbidden paths.
    """

    m = _EXCLUSION.search(text)
    if m is None:
        return text, []
    return text[: m.start()], path_tokens(text[m.end() :])


# --------------------------------------------------------------------------- deterministic rules

_NEGATIONS = [
    re.compile(r"\b(?:don't|do not|dont)\s+(.+)", re.I),
    re.compile(r"\bnever\s+(.+)", re.I),
    re.compile(r"\bmust not\s+(.+)", re.I),
    re.compile(r"\b(?:should not|shouldn't)\s+(.+)", re.I),
    re.compile(r"\b(?:cannot|can't)\s+(.+)", re.I),
    re.compile(r"\bwithout\s+(.+)", re.I),
    re.compile(r"\bavoid\s+(.+)", re.I),
    re.compile(r"\bnothing that\s+(.+)", re.I),
    re.compile(r"\bno\s+([a-z][\w-]*(?:\s+[a-z][\w-]*){0,4})", re.I),
]
_NEGATION_WORD = re.compile(
    r"\b(?:don't|do not|dont|never|must not|should not|shouldn't|cannot|can't|without|avoid|no)\b",
    re.I,
)
_STRIP_VERBS = re.compile(
    r"^(?:change|touch|modify|alter|replace|rename|remove|delete|break|edit|rewrite)"
    r"(?:s|d|ed|ing)?\s+(.+)",
    re.I,
)
_PRESERVE_TAIL = re.compile(
    r"\s+(?:unchanged|intact|as[- ]is|the same|alone|untouched|as it is)$", re.I
)
_SCOPE_RE = re.compile(
    r"\bonly\s+(?:modify|change|touch|edit|update)\s+(?:the\s+)?(?:files?\s+)?"
    r"(?:(?:under|in|within|inside)\s+)?(.+)",
    re.I,
)
_TECH_RE = re.compile(r"\busing\s+([A-Z][\w.+#-]*)")
_OPS: list[tuple[str, re.Pattern[str]]] = [
    ("git.push", re.compile(r"\bpush(?:ing|ed)?\b", re.I)),
    ("git.commit", re.compile(r"\bcommit(?:s|ting)?\b", re.I)),
    (
        "fs.remove",
        re.compile(
            r"\bdelet(?:e|ing)\b.*\bfiles?\b|\bremov(?:e|ing)\b.*\bfiles?\b", re.I
        ),
    ),
    (
        "dependency.add",
        re.compile(
            r"\bdependenc(?:y|ies)\b|\b(?:new|third[- ]party) (?:packages?|libraries|library)\b",
            re.I,
        ),
    ),
    ("db.migration", re.compile(r"\bmigrations?\b|\bschema changes?\b", re.I)),
]
_CLASS_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("redesign_ui", re.compile(r"\b(?:redesign|restyle|restyling)\b", re.I)),
    ("fix_bug", re.compile(r"\b(?:fix|fixes|fixed|fixing|bug|broken)\b", re.I)),
    ("refactor", re.compile(r"\b(?:refactor(?:ing)?|clean ?up)\b", re.I)),
    (
        "performance",
        re.compile(r"\b(?:performance|faster|optimi[sz]e|slow(?:er)?)\b", re.I),
    ),
    ("docs", re.compile(r"\b(?:translate|docs?|documentation|readme)\b", re.I)),
    ("security", re.compile(r"\b(?:security|vulnerab\w*)\b", re.I)),
    ("test", re.compile(r"\btest(?:s|ing)?\b", re.I)),
    (
        "build_feature",
        re.compile(r"\b(?:build|add|adding|implement(?:ing)?|creat(?:e|ing))\b", re.I),
    ),
    ("config_change", re.compile(r"\b(?:config(?:uration)?|settings?)\b", re.I)),
    ("question", re.compile(r"\?\s*$")),
]
_VAGUE_PRONOUN = re.compile(r"\b(?:it|that|them|this)\b", re.I)
_VAGUE_OUTCOME = re.compile(
    r"\b(?:faster|better|nicer|improved?|work(?:s|ing)?|cleaner)\b", re.I
)
_DESTRUCTIVE = re.compile(
    r"\b(?:delete|deleting|drop(?:ping)?|wipe[sd]?|wiping|reset(?:ting)?|remove|removing"
    r"|purge[sd]?|purging|truncate[sd]?|truncating)\b",
    re.I,
)
_UNSPECIFIC = re.compile(
    r"\b(?:old data|stuff|everything|the files|data|all of (?:it|them))\b", re.I
)
_SENSITIVE = re.compile(
    r"\b(?:production|prod|database|db|auth(?:entication|orization)?|payments?|billing"
    r"|security|secrets?|credentials?|passwords?)\b",
    re.I,
)


@dataclass
class _Draft:
    must: list[str] = field(default_factory=list)
    must_not: list[str] = field(default_factory=list)
    preserve: list[str] = field(default_factory=list)
    should: list[str] = field(default_factory=list)
    optional: list[str] = field(default_factory=list)
    scope: Scope = field(default_factory=Scope)
    questions: list[tuple[str, Risk]] = field(default_factory=list)

    def add(self, bucket: list[str], text: str) -> None:
        cleaned = _clean(text)
        if cleaned and _norm(cleaned) not in {_norm(x) for x in bucket}:
            bucket.append(cleaned)


def _add_unique(bucket: list[str], item: str) -> None:
    if item not in bucket:
        bucket.append(item)


def _scope_paths(rest: str) -> list[str]:
    # The scope list ends at the first clause-ish break ("and add ...", "; ...").
    head = re.split(
        rf"\s+(?:and|but)\s+(?={_IMPERATIVE}\b)|[;!?]|\.(?=\s|$)", rest, maxsplit=1
    )[0]
    return [path_glob(t) for t in path_tokens(head)]


def _must_not(d: _Draft, text: str) -> None:
    d.add(d.must_not, text)
    for op, pattern in _OPS:
        if pattern.search(text):
            _add_unique(d.scope.prohibited_ops, op)
    head, excepted = split_scope_exclusion(text)
    for tok in path_tokens(head):
        _add_unique(d.scope.protected_paths, path_glob(tok))
    for tok in excepted:  # "don't touch anything outside x.py" allows x.py
        _add_unique(d.scope.allowed_paths, path_glob(tok))
    stripped = _STRIP_VERBS.match(text)
    if stripped:
        d.add(d.preserve, stripped.group(1))


def _preserve(d: _Draft, text: str) -> None:
    target = _PRESERVE_TAIL.sub("", _clean(text))
    d.add(d.preserve, target)
    tokens = path_tokens(target)
    if tokens and (target != _clean(text) or " ".join(tokens) == target.strip("`'\" ")):
        for tok in tokens:
            _add_unique(d.scope.protected_paths, path_glob(tok))


def _positive(d: _Draft, clause: str) -> None:
    tech = _TECH_RE.search(clause)
    if tech:
        d.add(d.must, f"using {tech.group(1)}")
    if re.match(r"^\s*(?:optionally|if possible|nice to have|bonus)\b", clause, re.I):
        d.add(
            d.optional,
            re.sub(
                r"^\s*(?:optionally|if possible|nice to have|bonus)[,:]?\s*",
                "",
                clause,
                flags=re.I,
            ),
        )
        return
    should = re.search(r"\b(?:should|ideally|preferably)\s+(?!not\b)(.+)", clause, re.I)
    if should:
        d.add(d.should, should.group(1))
        return
    must = re.search(
        r"\b(?:must|needs? to|ha(?:ve|s) to)\s+(?!not\b)(.+)", clause, re.I
    )
    d.add(d.must, must.group(1) if must else clause)


def extract_deterministic(text: str) -> CompiledIntent:
    """Regex/clause extraction with no model; always succeeds."""

    d = _Draft()
    for clause in _split_clauses(text):
        scope = _SCOPE_RE.search(clause)
        if scope and not _NEGATION_WORD.search(clause[: scope.start()]):
            for glob in _scope_paths(scope.group(1)):
                _add_unique(d.scope.allowed_paths, glob)
            pre = _clean(clause[: scope.start()])
            if pre:
                _positive(d, pre)
            continue

        leave = re.search(
            r"\bleave\s+(.+?)\s+(?:alone|as[- ]is|unchanged|untouched|intact)\b",
            clause,
            re.I,
        )
        if leave:
            _must_not(d, leave.group(0))
            _preserve(d, leave.group(1))
            for rest in (clause[: leave.start()], clause[leave.end() :]):
                if _clean(rest):
                    _positive(d, _clean(rest))
            continue

        keep = re.match(
            r"^\s*(?:please\s+)?(?:keep|preserve|retain|maintain)\s+(.+)", clause, re.I
        )
        if keep:
            _preserve(d, keep.group(1))
            continue

        hits = [(m.start(), m.group(1)) for p in _NEGATIONS if (m := p.search(clause))]
        if hits:
            start, captured = min(hits)
            _must_not(d, captured)
            pre = _clean(clause[:start])
            if pre and not re.fullmatch(r"(?:please|and|but|also|then)", pre, re.I):
                _positive(d, pre)
            continue

        _positive(d, clause)
        if _VAGUE_PRONOUN.search(clause) and _VAGUE_OUTCOME.search(clause):
            d.questions.append(
                (
                    f'What specifically should change, and what observable outcome is expected for "{clause}"?',
                    "high",
                )
            )
        if _DESTRUCTIVE.search(clause) and _UNSPECIFIC.search(clause):
            d.questions.append(
                (f'Which specific data/files does "{clause}" refer to?', "critical")
            )

    intent_class = next((name for name, p in _CLASS_RULES if p.search(text)), "other")
    risk: Risk
    if any(impact == "critical" for _, impact in d.questions):
        risk = "critical"
    elif _DESTRUCTIVE.search(text) or _SENSITIVE.search(text):
        risk = "high"
    elif intent_class in {"docs", "redesign_ui"}:
        risk = "low"
    else:
        risk = "medium"

    contract = IntentContract(
        goal=d.must[0] if d.must else _norm(text),
        must=d.must,
        must_not=d.must_not,
        preserve=d.preserve,
        should=d.should,
        optional=d.optional,
        scope=d.scope,
        unknowns=[q for q, _ in d.questions],
        risk=risk,
        source_text_hash=hashlib.sha256(" ".join(text.split()).encode()).hexdigest(),
    )
    _finish(contract, intent_class)
    blocking = [q for q, impact in d.questions if impact in ("high", "critical")]
    return CompiledIntent(
        contract=contract,
        status="blocked_for_clarification" if blocking else "ready_to_lock",
        questions=blocking,
        warnings=[],
        intent_class=intent_class,
        degraded=True,
    )


def _finish(contract: IntentContract, intent_class: str) -> None:
    """(Re)derive acceptance criteria and verification requirements."""

    criteria = [
        f"{m} is implemented and demonstrated by a passing check" for m in contract.must
    ]
    criteria += [f"No change in the diff violates: {n}" for n in contract.must_not]
    criteria += [
        f"{p} behaves as before, verified by regression checks"
        for p in contract.preserve
    ]
    contract.acceptance_criteria = criteria
    reqs: list[str] = []
    by_class = {
        "fix_bug": ["regression_test"],
        "refactor": ["regression_test"],
        "performance": ["regression_test", "benchmark"],
        "security": ["security_scan"],
        "redesign_ui": ["browser_check", "unit_test"],
        "docs": ["manual_review"],
    }
    reqs += by_class.get(intent_class, ["unit_test"])
    if contract.must_not or contract.preserve or contract.scope.allowed_paths:
        reqs += ["diff_scope", "intent_conformance"]
    if contract.risk in ("high", "critical"):
        reqs.append("manual_review")
    contract.verification_requirements = list(dict.fromkeys(reqs))


# --------------------------------------------------------------------------- model extraction

_MODEL_SYSTEM = """You extract structured software-change intent from a user's request.
Respond with ONLY one JSON object, no prose, with these keys (all optional):
{"must": [str], "must_not": [str], "preserve": [str], "should": [str], "optional": [str],
 "scope": {"allowed_paths": [str], "protected_paths": [str],
           "prohibited_ops": ["git.push"|"git.commit"|"fs.remove"|"dependency.add"|"db.migration"]},
 "assumptions": [{"text": str, "impact": "low|medium|high|critical", "reversible": bool}],
 "ambiguities": [{"question": str, "impact": "low|medium|high|critical"}],
 "acceptance_criteria": [str], "risk": "low|medium|high|critical"}
Every constraint the user states explicitly must appear. Criteria must be observable and testable."""


def _first_json_object(text: str) -> JsonObject | None:
    start = text.find("{")
    while start != -1:
        try:
            obj, _ = json.JSONDecoder().raw_decode(text[start:])
        except ValueError:
            start = text.find("{", start + 1)
            continue
        return obj if isinstance(obj, dict) else None
    return None


def _str_list(obj: JsonObject, key: str) -> list[str]:
    value = obj.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"'{key}' must be a list of strings")
    return [str(v).strip() for v in value if str(v).strip()]


def _impact_items(
    obj: JsonObject, key: str, text_key: str
) -> list[tuple[str, Risk, bool]]:
    value = obj.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f"'{key}' must be a list")
    out: list[tuple[str, Risk, bool]] = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get(text_key), str):
            raise ValueError(f"'{key}' items need '{text_key}'")
        impact = item.get("impact", "medium")
        if impact not in RISKS:
            raise ValueError(f"invalid impact {impact!r}")
        out.append(
            (
                str(item[text_key]),
                cast(Risk, impact),
                item.get("reversible") is not False,
            )
        )
    return out


def _forbids_allowed(token: str, allowed: list[str]) -> bool:
    glob = path_glob(token)
    bare = glob.removeprefix("**/")
    return (
        glob in allowed
        or matches_any(bare, allowed)
        or matches_any(token, allowed)
        or any(matches_any(a.removeprefix("**/"), [glob]) for a in allowed)
    )


def _keep_model_must_not(
    contract: IntentContract, item: str, user_allowed: list[str], warnings: list[str]
) -> bool:
    """Model MUST_NOT items may only add constraints the user's scope permits.

    "Modify any file outside a.py" is a scope restriction: it becomes allowed paths,
    not a MUST_NOT whose paths look forbidden. An item forbidding a path the user
    explicitly allowed contradicts the deterministic scope and is dropped.
    """

    head, excepted = split_scope_exclusion(item)
    if excepted and not path_tokens(head):
        for tok in excepted:
            _add_unique(contract.scope.allowed_paths, path_glob(tok))
        warnings.append(
            f'model MUST_NOT "{item}" is a scope restriction; converted to allowed paths'
        )
        return False
    conflicts = [
        tok for tok in path_tokens(head) if _forbids_allowed(tok, user_allowed)
    ]
    if conflicts:
        warnings.append(
            f'dropped model MUST_NOT "{item}": it forbids {", ".join(conflicts)}, '
            "which the user explicitly allowed"
        )
        return False
    return True


async def compile_intent(
    text: str,
    model: ModelClient | None = None,
    *,
    authorize_defaults: bool = False,
    timeout: float = 45.0,  # free-tier NIM models often need 20-40 s
) -> CompiledIntent:
    """Deterministic extraction, optionally enriched (add-only) by a model."""

    result = extract_deterministic(text)
    if model is None:
        return result
    try:
        raw = await asyncio.wait_for(model.complete(_MODEL_SYSTEM, text), timeout)
        obj = _first_json_object(raw)
        if obj is None:
            raise ValueError("no JSON object in model response")
        lists = {
            key: _str_list(obj, key) for key in (*_LIST_FIELDS, "acceptance_criteria")
        }
        scope_raw = obj.get("scope", {})
        if not isinstance(scope_raw, dict):
            raise ValueError("'scope' must be an object")
        scope = {
            k: _str_list(scope_raw, k)
            for k in ("allowed_paths", "protected_paths", "prohibited_ops")
        }
        assumptions = _impact_items(obj, "assumptions", "text")
        ambiguities = _impact_items(obj, "ambiguities", "question")
        model_risk = obj.get("risk")
    except Exception as exc:  # any model/transport/shape failure degrades
        result.warnings.append(f"model extraction degraded: {exc}")
        return result

    c = result.contract
    deterministic_must_not = list(c.must_not)
    user_allowed = list(c.scope.allowed_paths)
    for key in _LIST_FIELDS:
        bucket: list[str] = getattr(c, key)
        for item in lists[key]:
            if any(_overlaps(item, existing) for existing in bucket):
                continue
            if key == "must_not" and not _keep_model_must_not(
                c, item, user_allowed, result.warnings
            ):
                continue
            bucket.append(item)
            for forbidden in deterministic_must_not if key == "must" else []:
                if _overlaps(item, forbidden):
                    result.warnings.append(
                        f'model MUST "{item}" contradicts MUST_NOT "{forbidden}"; both kept (intent lock)'
                    )
    known_ops = {op for op, _ in _OPS}
    for key, values in scope.items():
        bucket = getattr(c.scope, key)
        for v in values:
            if key == "prohibited_ops" and v not in known_ops:
                # Free-text op: keep the constraint as a MUST_NOT the gate can judge.
                if not any(_overlaps(v, existing) for existing in c.must_not):
                    c.must_not.append(v)
                continue
            if key == "protected_paths" and _forbids_allowed(v, user_allowed):
                result.warnings.append(
                    f'dropped model protected path "{v}": the user allowed editing it'
                )
                continue
            _add_unique(bucket, v if key == "prohibited_ops" else path_glob(v))
    for criterion in lists["acceptance_criteria"]:
        _add_unique(c.acceptance_criteria, criterion)
    if model_risk in RISKS and RISKS.index(cast(Risk, model_risk)) > RISKS.index(
        c.risk
    ):
        c.risk = cast(Risk, model_risk)

    for text_, impact, reversible in assumptions:
        c.assumptions.append(f"[{impact}] {text_}")
        blocks = impact in ("high", "critical") or (
            impact == "medium" and not reversible
        )
        if blocks and not authorize_defaults:
            result.questions.append(f"Confirm assumption: {text_}")
    for question, impact, _ in ambiguities:
        _add_unique(c.unknowns, question)
        if impact in ("high", "critical"):
            result.questions.append(question)

    criteria = list(c.acceptance_criteria)
    _finish(c, result.intent_class)
    for criterion in criteria:
        _add_unique(c.acceptance_criteria, criterion)
    result.status = "blocked_for_clarification" if result.questions else "ready_to_lock"
    result.degraded = False
    return result


# --------------------------------------------------------------------------- rendering

_PREFIX = {
    "must": "M",
    "must_not": "N",
    "preserve": "P",
    "should": "S",
    "optional": "O",
}


def requirement_rows(contract: IntentContract) -> list[tuple[str, str, str]]:
    """Stable (id, kind, text) rows: M1.. MUST, N1.. MUST_NOT, P1.. PRESERVE, S1.., O1.."""

    return [
        (f"{_PREFIX[kind]}{i}", kind, text)
        for kind in _PREFIX
        for i, text in enumerate(getattr(contract, kind), start=1)
    ]


def render_contract(contract: IntentContract) -> str:
    """Compact system-prompt block: constraints verbatim + minimal-change instruction."""

    lines = [
        f"<intent_contract version={contract.version} risk={contract.risk}>",
        f"GOAL: {contract.goal}",
    ]
    titles = {
        "must": "MUST",
        "must_not": "MUST NOT",
        "preserve": "PRESERVE",
        "should": "SHOULD",
    }
    rows = requirement_rows(contract)
    for kind, title in titles.items():
        items = [f"- {rid}: {text}" for rid, k, text in rows if k == kind]
        if items:
            lines += [f"{title}:", *items]
    s = contract.scope
    scope_parts = [
        f"only modify {', '.join(s.allowed_paths)}" if s.allowed_paths else "",
        f"never modify {', '.join(s.protected_paths)}" if s.protected_paths else "",
        f"prohibited operations {', '.join(s.prohibited_ops)}"
        if s.prohibited_ops
        else "",
    ]
    if any(scope_parts):
        lines.append("SCOPE: " + "; ".join(p for p in scope_parts if p))
    if contract.acceptance_criteria:
        lines += [
            "ACCEPTANCE CRITERIA:",
            *(f"- {c}" for c in contract.acceptance_criteria),
        ]
    lines += [
        "RULES: Make the smallest change that satisfies every MUST. No unrelated refactors,"
        " dependency upgrades, redesigns or migrations. MUST NOT and PRESERVE items are locked."
        " Completion is decided by the verification gate from real evidence, not by your summary.",
        "</intent_contract>",
    ]
    return "\n".join(lines)
