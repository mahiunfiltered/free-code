"""Fake `claude` for orchestration tests: acts on directives found in the prompt.

Directives (one per prompt line):
  WRITE <relpath> <content>   write a file under the cwd
  FAIL                        finish with an error result
  HANG                        never answer (until interrupted / stdin closes)
"""

import json
import os
import sys
import uuid

SESSION = str(uuid.uuid4())


def emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


emit({"type": "system", "subtype": "init", "session_id": SESSION, "model": "fake"})

for line in sys.stdin:
    message = json.loads(line)
    kind = message.get("type")
    if kind == "control_request":
        subtype = message["request"]["subtype"]
        emit(
            {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": message["request_id"],
                    "response": {},
                },
            }
        )
        if subtype == "interrupt":
            emit({"type": "result", "subtype": "error_during_execution"})
        continue
    if kind != "user":
        continue
    prompt = message["message"]["content"]
    directives = [ln.strip() for ln in prompt.splitlines()]
    if "HANG" in directives:
        continue
    blocks = []
    for directive in directives:
        if directive.startswith("WRITE "):
            _, rel, content = directive.split(" ", 2)
            path = os.path.join(os.getcwd(), rel)
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content + "\n")
            blocks.append({"type": "tool_use", "id": rel, "name": "Write", "input": {}})
    emit(
        {
            "type": "assistant",
            "session_id": SESSION,
            "message": {"content": [*blocks, {"type": "text", "text": "done"}]},
        }
    )
    failed = "FAIL" in directives
    emit(
        {
            "type": "result",
            "subtype": "error_during_execution" if failed else "success",
            "is_error": failed,
            "session_id": SESSION,
            "result": f"cwd={os.path.basename(os.getcwd())} wrote {len(blocks)} files",
            "total_cost_usd": 0.01,
            "num_turns": 1,
        }
    )
