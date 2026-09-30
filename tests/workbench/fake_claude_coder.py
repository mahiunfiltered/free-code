"""Fake `claude` for coordinator tests: edits files per directives in the first prompt.

Directives (one per line of the first prompt):
  WRITE <relpath> <content>          write a one-line file under the cwd
  RETRY<n> WRITE <relpath> <content> applied on the n-th recovery prompt
  USAGE <n>                          the assistant message reports n output tokens
  TOOLRESULT <text>                  emit a user tool_result event carrying text
  HANG                               never answer until interrupted
  ASK <tool>                         ask can_use_tool for <tool>, wait for the answer;
                                     the result text gains "<tool>=<behavior>"
Recovery prompts are recognised by "Verification FAILED" (workbench.recovery).
"""

import json
import os
import sys
import uuid

SESSION = str(uuid.uuid4())
first: list[str] = []
retries = 0
hanging = False


def emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def result(subtype: str, text: str, output_tokens: int) -> None:
    emit(
        {
            "type": "result",
            "subtype": subtype,
            "is_error": subtype != "success",
            "session_id": SESSION,
            "result": text,
            "num_turns": 1,
            "usage": {"input_tokens": 10, "output_tokens": output_tokens},
        }
    )


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
                result("error_during_execution", "interrupted", 0)
                return None
    return None


emit({"type": "system", "subtype": "init", "session_id": SESSION, "model": "fake"})

for line in sys.stdin:
    message = json.loads(line)
    kind = message.get("type")
    if kind == "control_request":
        answer_control(message)
        if message["request"]["subtype"] == "interrupt" and hanging:
            hanging = False
            result("error_during_execution", "interrupted", 0)
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
    lines = [ln.strip() for ln in prompt.splitlines()]
    if "Verification FAILED" in prompt:
        retries += 1
        prefix = f"RETRY{retries} "
        directives = [ln[len(prefix) :] for ln in first if ln.startswith(prefix)]
    else:
        first = lines
        directives = [ln for ln in lines if not ln.startswith("RETRY")]
    answers = {
        d[4:]: ask(d[4:]) for d in dict.fromkeys(directives) if d.startswith("ASK ")
    }
    if None in answers.values():
        continue  # interrupted while waiting: the error result was sent
    tokens = 5
    blocks = []
    for directive in directives:
        if directive.startswith("WRITE "):
            _, rel, content = directive.split(" ", 2)
            path = os.path.join(os.getcwd(), rel)
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content + "\n")
            blocks.append({"type": "tool_use", "id": rel, "name": "Write", "input": {}})
        elif directive.startswith("USAGE "):
            tokens = int(directive.split()[1])
        elif directive.startswith("TOOLRESULT "):
            emit(
                {
                    "type": "user",
                    "session_id": SESSION,
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "tu_1",
                                "content": [{"type": "text", "text": directive[11:]}],
                            }
                        ],
                    },
                }
            )
    emit(
        {
            "type": "assistant",
            "session_id": SESSION,
            "message": {
                "id": f"msg_{uuid.uuid4().hex[:8]}",
                "content": [*blocks, {"type": "text", "text": "done"}],
                "usage": {"output_tokens": tokens},
            },
        }
    )
    if "HANG" in directives:
        hanging = True
        continue
    asked = "".join(f" {tool}={behavior}" for tool, behavior in answers.items())
    result("success", f"wrote {len(blocks)} files{asked}", tokens)
