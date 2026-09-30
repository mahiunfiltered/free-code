"""Fake `claude` for orchestration tests: acts on directives found in the prompt.

Directives (one per prompt line):
  WRITE <relpath> <content>   write a file under the cwd
  FAIL                        finish with an error result
  CRASH                       exit with code 3 (after WRITEs) without a result
  CRASH_ONCE <marker path>    like CRASH unless the marker exists; creates it
  HANG                        never answer (until interrupted / stdin closes)
  ASK <tool>                  ask can_use_tool for <tool> and wait for the answer;
                              the result text gains "<tool>=<behavior>"
Image blocks in the prompt are counted in the result text ("saw N images").
"""

import json
import os
import sys
import uuid

SESSION = str(uuid.uuid4())


def emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def answer_control(message: dict) -> None:
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


def ask(tool: str) -> str | None:
    """Ask to use ``tool``; the answer's behavior, or None when interrupted."""

    request_id = f"perm_{tool}"
    emit(
        {
            "type": "control_request",
            "request_id": request_id,
            "request": {
                "subtype": "can_use_tool",
                "tool_name": tool,
                "input": {"command": "echo hi"},
                "permission_suggestions": [],
            },
        }
    )
    for raw in sys.stdin:
        reply = json.loads(raw)
        if reply.get("type") == "control_response":
            response = reply["response"]
            if response.get("request_id") == request_id:
                return response["response"].get("behavior")
        elif reply.get("type") == "control_request":
            answer_control(reply)
            if reply["request"]["subtype"] == "interrupt":
                emit({"type": "result", "subtype": "error_during_execution"})
                return None
    return None


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
    content = message["message"]["content"]
    if isinstance(content, list):  # text + image blocks
        images = sum(1 for b in content if b.get("type") == "image")
        content = "\n".join(b["text"] for b in content if b.get("type") == "text")
    else:
        images = 0
    prompt = content
    directives = [ln.strip() for ln in prompt.splitlines()]
    if "HANG" in directives:
        continue
    answers = {
        d[4:]: ask(d[4:]) for d in dict.fromkeys(directives) if d.startswith("ASK ")
    }
    if None in answers.values():
        continue  # interrupted while waiting: the error result was sent
    blocks = []
    for directive in directives:
        if directive.startswith("WRITE "):
            _, rel, content = directive.split(" ", 2)
            path = os.path.join(os.getcwd(), rel)
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content + "\n")
            blocks.append({"type": "tool_use", "id": rel, "name": "Write", "input": {}})
    once = [d.split(" ", 1)[1] for d in directives if d.startswith("CRASH_ONCE ")]
    if "CRASH" in directives or (once and not os.path.exists(once[0])):
        if once:
            open(once[0], "w").close()
        os._exit(3)
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
            "result": f"cwd={os.path.basename(os.getcwd())} wrote {len(blocks)} files"
            + (f" saw {images} images" if images else "")
            + "".join(f" {tool}={behavior}" for tool, behavior in answers.items()),
            "total_cost_usd": 0.01,
            "num_turns": 1,
        }
    )
