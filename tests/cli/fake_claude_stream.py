"""Minimal stand-in for `claude` speaking stream-json + the SDK control protocol."""

import json
import sys

SESSION = "11111111-2222-3333-4444-555555555555"


def emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def reply(request_id: str, response: dict) -> None:
    emit(
        {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": request_id,
                "response": response,
            },
        }
    )


emit(
    {
        "type": "system",
        "subtype": "init",
        "session_id": SESSION,
        "permissionMode": "default",
        "model": "fake-model",
    }
)
# An inbound control request we do not register handlers for; the session must answer it.
emit(
    {
        "type": "control_request",
        "request_id": "hook_1",
        "request": {"subtype": "hook_callback"},
    }
)

for line in sys.stdin:
    message = json.loads(line)
    kind = message.get("type")
    if kind == "control_request":
        request = message["request"]
        subtype = request["subtype"]
        if subtype == "initialize":
            reply(
                message["request_id"],
                {
                    "commands": [{"name": "compact", "description": "Compact"}],
                    "models": [{"value": "m1", "displayName": "M1"}],
                },
            )
        elif subtype == "set_model":
            reply(message["request_id"], {"model": request.get("model")})
        else:
            reply(message["request_id"], {})
    elif kind == "control_response":
        # Only the unsupported hook_callback reaches here with an error, or a permission decision.
        response = message["response"]
        if response["request_id"] == "hook_1":
            emit({"type": "fake_hook_answered", "subtype": response["subtype"]})
            continue
        decision = response["response"]
        emit(
            {
                "type": "assistant",
                "session_id": SESSION,
                "message": {
                    "id": "m",
                    "content": [
                        {"type": "text", "text": f"decision={decision['behavior']}"}
                    ],
                },
            }
        )
        emit(
            {
                "type": "result",
                "subtype": "success",
                "session_id": SESSION,
                "result": "done",
            }
        )
    elif kind == "user":
        text = message["message"]["content"]
        if text == "__long_line__":
            # Exceeds the reader's (test-lowered) line limit, then keeps waiting.
            emit({"type": "assistant", "padding": "x" * 8192})
            continue
        emit(
            {
                "type": "stream_event",
                "session_id": SESSION,
                "event": {"type": "message_start"},
            }
        )
        emit(
            {
                "type": "control_request",
                "request_id": "perm_1",
                "request": {
                    "subtype": "can_use_tool",
                    "tool_name": "Write",
                    "input": {"file_path": "x", "content": text},
                },
            }
        )
