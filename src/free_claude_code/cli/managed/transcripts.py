"""Read Claude Code's on-disk session transcripts for the chat sidebar and history."""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from free_claude_code.config.paths import config_dir_path
from free_claude_code.core.json_types import JsonObject, JsonValue

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_TAG_RE = re.compile(r"<[^>]+>")
_COMMAND_NAME_RE = re.compile(r"<command-name>\s*(/?[^<\s]+)\s*</command-name>")
_TITLE_CHARS = 80
_TITLE_TYPES = {
    "custom-title": "customTitle",
    "ai-title": "aiTitle",
    "summary": "summary",
}


@dataclass(frozen=True, slots=True)
class TranscriptSummary:
    session_id: str
    title: str
    cwd: str
    updated_at: float

    def to_json(self) -> JsonObject:
        return {
            "session_id": self.session_id,
            "title": self.title,
            "cwd": self.cwd,
            "updated_at": self.updated_at,
        }


def claude_config_dir() -> Path:
    """Return the Claude config dir the chat app spawns with and reads from.

    An explicit CLAUDE_CONFIG_DIR in the server environment wins; otherwise the
    app uses ~/.fcc/claude so its history and memory never mix with the user's
    own Claude Code home.
    """

    explicit = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(explicit) if explicit else config_dir_path() / "claude"


def claude_projects_dir() -> Path:
    return claude_config_dir() / "projects"


def is_valid_session_id(session_id: str) -> bool:
    return bool(_SESSION_ID_RE.match(session_id))


# ponytail: in-process cache keyed by (mtime, size); fine for a local single-user UI.
_summary_cache: dict[Path, tuple[tuple[float, int], TranscriptSummary | None]] = {}


def list_transcripts(
    limit: int = 200, root: Path | None = None
) -> list[TranscriptSummary]:
    """Return the most recently updated sessions that contain a user prompt."""

    base = root or claude_projects_dir()
    if not base.is_dir():
        return []
    files = sorted(base.glob("*/*.jsonl"), key=_mtime, reverse=True)
    summaries: list[TranscriptSummary] = []
    for path in files:
        if len(summaries) >= limit:
            break
        if (summary := _cached_summary(path)) is not None:
            summaries.append(summary)
    return summaries


def find_transcript(session_id: str, root: Path | None = None) -> Path | None:
    if not is_valid_session_id(session_id):
        return None
    base = root or claude_projects_dir()
    return next(iter(base.glob(f"*/{session_id}.jsonl")), None)


def load_transcript_events(path: Path) -> list[JsonObject]:
    """Convert a transcript into the same event shapes the live stream emits."""

    events: list[JsonObject] = []
    for entry in _entries(path):
        if entry.get("isSidechain") or entry.get("isMeta"):
            continue
        kind = entry.get("type")
        message = entry.get("message")
        if kind == "system" and entry.get("subtype") == "compact_boundary":
            events.append({"type": "system", "subtype": "compact_boundary"})
        elif kind == "assistant" and isinstance(message, dict):
            events.append(
                {"type": "assistant", "message": message, "parent_tool_use_id": None}
            )
        elif kind == "user" and isinstance(message, dict):
            if entry.get("isCompactSummary"):
                continue
            if _is_tool_result(message.get("content")):
                events.append(
                    {"type": "user", "message": message, "parent_tool_use_id": None}
                )
            elif _prompt_text(message.get("content")) or _has_image(
                message.get("content")
            ):
                events.append({"type": "fcc_user", "message": message})
    return events


def rename_transcript(path: Path, session_id: str, title: str) -> None:
    line = {"type": "custom-title", "customTitle": title, "sessionId": session_id}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, ensure_ascii=False) + "\n")
    _summary_cache.pop(path, None)


def delete_transcript(path: Path) -> None:
    path.unlink(missing_ok=True)
    _summary_cache.pop(path, None)


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _cached_summary(path: Path) -> TranscriptSummary | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (stat.st_mtime, stat.st_size)
    cached = _summary_cache.get(path)
    if cached is not None and cached[0] == key:
        return cached[1]
    summary = _summarize(path, stat.st_mtime)
    _summary_cache[path] = (key, summary)
    return summary


def _summarize(path: Path, updated_at: float) -> TranscriptSummary | None:
    titles: dict[str, str] = {}
    first_prompt = ""
    cwd = ""
    for entry in _entries(path):
        kind = entry.get("type")
        if isinstance(kind, str) and kind in _TITLE_TYPES:
            value = entry.get(_TITLE_TYPES[kind])
            if isinstance(value, str) and value.strip():
                titles[kind] = value.strip()
            continue
        if not cwd and isinstance(entry.get("cwd"), str):
            cwd = str(entry["cwd"])
        if (
            first_prompt
            or kind != "user"
            or entry.get("isSidechain")
            or entry.get("isMeta")
        ):
            continue
        message = entry.get("message")
        if isinstance(message, dict):
            first_prompt = _prompt_text(message.get("content"))
    if not first_prompt:
        return None
    title = next(
        (titles[kind] for kind in _TITLE_TYPES if kind in titles),
        first_prompt,
    )
    return TranscriptSummary(
        session_id=path.stem,
        title=title[:_TITLE_CHARS],
        cwd=cwd,
        updated_at=updated_at,
    )


def _entries(path: Path) -> list[JsonObject]:
    entries: list[JsonObject] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict):
                    entries.append(entry)
    except OSError:
        return []
    return entries


def _blocks(content: JsonValue) -> list[JsonObject]:
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    return []


def _is_tool_result(content: JsonValue) -> bool:
    return any(block.get("type") == "tool_result" for block in _blocks(content))


def _has_image(content: JsonValue) -> bool:
    return any(block.get("type") == "image" for block in _blocks(content))


def _prompt_text(content: JsonValue) -> str:
    """Return a one-line prompt preview, or "" for tool results and hook noise."""

    if isinstance(content, str):
        text = content
    else:
        text = " ".join(
            str(block.get("text", ""))
            for block in _blocks(content)
            if block.get("type") == "text"
        )
    text = text.strip()
    if command := _COMMAND_NAME_RE.search(text):
        return command.group(1)
    if text.startswith("<"):
        # Local-command output, caveats, and system reminders are not prompts.
        return ""
    return " ".join(_TAG_RE.sub(" ", text).split())
