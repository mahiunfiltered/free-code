"""Local Claude-desktop-style chat UI backed by live Claude Code sessions."""

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from free_claude_code.core.claude_permission_modes import PERMISSION_MODES
from free_claude_code.core.json_types import JsonObject, JsonValue

from .admin_routes import is_loopback_host, require_loopback_admin
from .dependencies import get_services
from .model_catalog import build_chat_models_response
from .ports import ApiServices, ChatRuntimePort, ChatSessionPort


def require_same_origin(request: Request) -> None:
    """The chat API can run shell commands, so only this page may call it.

    A loopback ``Host`` blocks DNS rebinding; an exact ``Origin`` match blocks
    pages served from other localhost ports.
    """

    host = request.headers.get("host", "")
    if not is_loopback_host(urlsplit(f"//{host}").hostname):
        raise HTTPException(status_code=403, detail="Chat UI is local-only")
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc.lower() != host.lower():
        raise HTTPException(status_code=403, detail="Chat UI is same-origin only")


router = APIRouter(
    dependencies=[Depends(require_loopback_admin), Depends(require_same_origin)]
)

STATIC_DIR = Path(__file__).resolve().parent / "chat_static"
_ASSETS = frozenset({"chat.css", "chat.js"})
# Control requests the UI may forward; everything else stays owned by the server.
_UI_CONTROL_SUBTYPES = frozenset(
    {
        "interrupt",
        "set_permission_mode",
        "set_model",
        "set_max_thinking_tokens",
        "mcp_status",
        "get_context_usage",
    }
)


class StartPayload(BaseModel):
    cwd: str
    permission_mode: str = "default"
    model: str | None = None
    resume_session_id: str | None = None


class MessagePayload(BaseModel):
    content: JsonValue


class ControlPayload(BaseModel):
    request: JsonObject = Field(default_factory=dict)


class PermissionPayload(BaseModel):
    decision: JsonObject


class RenamePayload(BaseModel):
    title: str = Field(min_length=1, max_length=200)


def _chat(services: ApiServices = Depends(get_services)) -> ChatRuntimePort:
    if services.chat is None:
        raise HTTPException(status_code=503, detail="Chat is not available")
    return services.chat


def _live(live_id: str, chat: ChatRuntimePort) -> ChatSessionPort:
    session = chat.get(live_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Chat session not found")
    return session


@router.get("/chat", include_in_schema=False)
async def chat_page():
    return FileResponse(STATIC_DIR / "index.html")


@router.get("/chat/assets/{filename}", include_in_schema=False)
async def chat_asset(filename: str):
    if filename not in _ASSETS:
        raise HTTPException(status_code=404, detail="Chat asset not found")
    return FileResponse(STATIC_DIR / filename)


@router.get("/chat/api/sessions")
async def sessions(chat: ChatRuntimePort = Depends(_chat)) -> JsonObject:
    return {
        "home": os.path.expanduser("~"),
        "transcripts": list(chat.list_transcripts()),
        "live": [session.snapshot() for session in chat.live_sessions()],
    }


@router.get("/chat/api/files")
async def files(cwd: str, q: str = "", chat: ChatRuntimePort = Depends(_chat)):
    return {"files": await chat.search_files(cwd, q)}


@router.get("/chat/api/dirs")
async def dirs(path: str = "", chat: ChatRuntimePort = Depends(_chat)) -> JsonObject:
    return await chat.list_dirs(path)


@router.get("/chat/api/models")
async def models(services: ApiServices = Depends(get_services)) -> JsonObject:
    return build_chat_models_response(
        services.requests.current_settings(), services.requests
    )


@router.get("/chat/api/transcripts/{session_id}")
async def transcript(session_id: str, chat: ChatRuntimePort = Depends(_chat)):
    events = chat.transcript_events(session_id)
    if events is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"events": events}


@router.patch("/chat/api/transcripts/{session_id}")
async def rename(
    session_id: str, payload: RenamePayload, chat: ChatRuntimePort = Depends(_chat)
):
    if not chat.rename_transcript(session_id, payload.title.strip()):
        raise HTTPException(status_code=404, detail="Session not found")
    return {"ok": True}


@router.delete("/chat/api/transcripts/{session_id}")
async def delete(session_id: str, chat: ChatRuntimePort = Depends(_chat)):
    if not chat.delete_transcript(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    return {"ok": True}


@router.post("/chat/api/live")
async def start(payload: StartPayload, chat: ChatRuntimePort = Depends(_chat)):
    session = await chat.start(
        cwd=payload.cwd,
        permission_mode=payload.permission_mode,
        model=payload.model or None,
        resume_session_id=payload.resume_session_id or None,
    )
    return session.snapshot()


@router.get("/chat/api/live/{live_id}/events")
async def events(
    live_id: str, request: Request, chat: ChatRuntimePort = Depends(_chat)
):
    session = _live(live_id, chat)

    async def stream():
        async for event in session.subscribe():
            if await request.is_disconnected():
                return
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post("/chat/api/live/{live_id}/messages")
async def send_message(
    live_id: str, payload: MessagePayload, chat: ChatRuntimePort = Depends(_chat)
):
    await _live(live_id, chat).send_user_message(payload.content)
    return {"ok": True}


@router.post("/chat/api/live/{live_id}/control")
async def control(
    live_id: str, payload: ControlPayload, chat: ChatRuntimePort = Depends(_chat)
):
    subtype = payload.request.get("subtype")
    if subtype not in _UI_CONTROL_SUBTYPES:
        raise HTTPException(status_code=400, detail="Unsupported control request")
    if (
        subtype == "set_permission_mode"
        and payload.request.get("mode") not in PERMISSION_MODES
    ):
        raise HTTPException(status_code=400, detail="Unknown permission mode")
    return {"response": await _live(live_id, chat).control(payload.request)}


@router.post("/chat/api/live/{live_id}/permissions/{request_id}")
async def permission(
    live_id: str,
    request_id: str,
    payload: PermissionPayload,
    chat: ChatRuntimePort = Depends(_chat),
):
    if payload.decision.get("behavior") not in {"allow", "deny"}:
        raise HTTPException(status_code=400, detail="behavior must be allow or deny")
    await _live(live_id, chat).respond_permission(request_id, payload.decision)
    return {"ok": True}


@router.delete("/chat/api/live/{live_id}")
async def close(live_id: str, chat: ChatRuntimePort = Depends(_chat)):
    if not await chat.close(live_id):
        raise HTTPException(status_code=404, detail="Chat session not found")
    return {"ok": True}
