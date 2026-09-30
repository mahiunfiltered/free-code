"""Scripted stand-in for the ``claude`` CLI (``fcc-bench --engine fake``).

    python fake_claude.py <script.json> [ignored claude args...]

Speaks the stream-json protocol an ``InteractiveClaudeSession`` expects. Script:
``{"attempts": [{relpath: content | null}, ...], "nodes": {node_id: {relpath: content}}}``.
The first prompt applies ``attempts[0]``; the n-th recovery prompt ("Verification FAILED")
applies ``attempts[n]`` (none left: no edits, like a stuck agent); a parallel node prompt
("node '<id>'") applies ``nodes[id]``. ``null`` content deletes the file.
"""

import json
import re
import sys
import uuid
from pathlib import Path

from free_claude_code.core.json_types import JsonObject, JsonValue

SESSION = str(uuid.uuid4())
_NODE = re.compile(r"node '([^']+)'")


def _emit(payload: JsonObject) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _apply(edits: JsonObject) -> list[JsonValue]:
    blocks: list[JsonValue] = []
    for rel, content in edits.items():
        path = Path.cwd() / rel
        if not isinstance(content, str):
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")
        blocks.append({"type": "tool_use", "id": uuid.uuid4().hex[:8], "name": "Write",
                       "input": {"file_path": rel}})  # fmt: skip
    return blocks


def main() -> None:
    script = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    attempts: list[dict] = script.get("attempts", [])
    nodes: dict = script.get("nodes", {})
    retries = 0
    _emit({"type": "system", "subtype": "init", "session_id": SESSION, "model": "fake"})
    for line in sys.stdin:
        message = json.loads(line)
        if message.get("type") == "control_request":
            _emit({"type": "control_response", "response": {
                "subtype": "success", "request_id": message["request_id"], "response": {}}})  # fmt: skip
            continue
        if message.get("type") != "user":
            continue
        prompt = message["message"]["content"]
        prompt = prompt if isinstance(prompt, str) else json.dumps(prompt)
        if "Verification FAILED" in prompt:
            retries += 1
            edits = attempts[retries] if retries < len(attempts) else {}
        elif node := _NODE.search(prompt):
            edits = nodes.get(node.group(1), {})
        else:
            edits = attempts[0] if attempts else {}
        blocks = _apply(edits)
        usage = {"input_tokens": 100, "output_tokens": 20}
        _emit({"type": "assistant", "session_id": SESSION, "message": {
            "id": f"msg_{uuid.uuid4().hex[:8]}", "usage": usage,
            "content": [*blocks, {"type": "text", "text": "done"}]}})  # fmt: skip
        _emit({"type": "result", "subtype": "success", "is_error": False,
               "session_id": SESSION, "result": f"edited {len(blocks)} file(s)",
               "num_turns": 1, "usage": usage})  # fmt: skip


if __name__ == "__main__":
    main()
