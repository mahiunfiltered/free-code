"""Prompt-injection signals for untrusted content (M0005 09 section 9).

External content (tool output, web pages, repository files) is data, never instructions:
``scan`` flags suspicious text for the UI and ``wrap_untrusted`` delimits it with its source.
Heuristic only: a clean scan is not proof of safety.
"""

import re
from dataclasses import dataclass

UNTRUSTED_OPEN = "<<<UNTRUSTED_CONTENT"
UNTRUSTED_CLOSE = "UNTRUSTED_CONTENT>>>"

_OVERRIDE = re.compile(
    r"\b(ignore|disregard|forget)\s+(?:(?:all|any|the|previous|prior|above|earlier|your)\s+){1,3}"
    r"(instructions|rules|prompts?|directions)\b",
    re.I,
)
_ROLE = (
    re.compile(r"\byou\s+are\s+now\b", re.I),
    re.compile(r"\bact\s+as\s+the\s+system\b", re.I),
    re.compile(r"<\s*/?\s*system\s*>", re.I),
    re.compile(r"\[\s*system\s*\]", re.I),
    re.compile(r"^\s*#{1,6}\s*system\b", re.I | re.M),
    re.compile(r"^\s*(system|assistant)\s*:", re.I | re.M),
)
_EXFIL = re.compile(
    r"\b(send|post|upload|email|share|paste|reveal|print)\b[\s\S]{0,40}?"
    r"(api[\s_-]*keys?|tokens?|secrets?|passwords?|credentials|\.env)\b",
    re.I,
)
# Zero-width / format characters and bidi overrides used to hide text from a human reviewer.
_HIDDEN_CHARS = re.compile(
    "[\u200b-\u200f\u2060-\u2064\ufeff\u202a-\u202e\u2066-\u2069]"
)
_HTML_COMMENT = re.compile(
    r"<!--[\s\S]{0,200}?\b(ignore|disregard|system|instructions|override)\b[\s\S]{0,200}?-->",
    re.I,
)
_DISPLAY_NONE = re.compile(
    r"display\s*:\s*none[^>]{0,100}>[\s\S]{0,200}?\b(ignore|disregard|system|instructions|override)\b",
    re.I,
)


@dataclass(frozen=True, slots=True)
class InjectionScan:
    signals: tuple[str, ...]

    @property
    def suspicious(self) -> bool:
        return bool(self.signals)


def scan(content: str) -> InjectionScan:
    checks = (
        ("override_instructions", bool(_OVERRIDE.search(content))),
        ("role_impersonation", any(p.search(content) for p in _ROLE)),
        ("exfiltration_request", bool(_EXFIL.search(content))),
        (
            "hidden_text",
            bool(
                _HIDDEN_CHARS.search(content)
                or _HTML_COMMENT.search(content)
                or _DISPLAY_NONE.search(content)
            ),
        ),
    )
    return InjectionScan(tuple(name for name, hit in checks if hit))


def _sanitize(text: str) -> str:
    text = _HIDDEN_CHARS.sub("", text)
    while UNTRUSTED_OPEN in text or UNTRUSTED_CLOSE in text:  # nested markers re-form
        text = text.replace(UNTRUSTED_OPEN, "").replace(UNTRUSTED_CLOSE, "")
    return text


def wrap_untrusted(content: str, source: str) -> str:
    """Delimit content so it cannot close its own wrapper or pose as trusted input."""

    clean_source = " ".join(_sanitize(source).split())
    return f"{UNTRUSTED_OPEN} source={clean_source}\n{_sanitize(content)}\n{UNTRUSTED_CLOSE}"
