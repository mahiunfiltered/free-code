"""Runtime capabilities consumed by the HTTP API adapter."""

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from free_claude_code.application.connected_accounts import (
    ConnectedAccountLoginMode,
    ConnectedAccountStatus,
)
from free_claude_code.application.model_metadata import ProviderModelRefreshResult
from free_claude_code.application.ports import RequestRuntimePort, TaskController
from free_claude_code.config.admin.state import ConfigInputValue
from free_claude_code.core.json_types import JsonObject, JsonValue


class AdminRuntimePort(Protocol):
    """Runtime operations exposed by the local Admin API."""

    async def apply_admin_config(
        self, updates: Mapping[str, ConfigInputValue]
    ) -> JsonObject: ...

    def admin_status(self) -> JsonObject: ...

    async def test_provider(self, provider_id: str) -> JsonObject: ...

    async def refresh_models(self) -> ProviderModelRefreshResult: ...

    async def request_restart(self) -> None: ...

    async def connected_account_status(
        self, provider_id: str
    ) -> ConnectedAccountStatus: ...

    async def start_connected_account_login(
        self,
        provider_id: str,
        mode: ConnectedAccountLoginMode,
    ) -> ConnectedAccountStatus: ...

    async def cancel_connected_account_login(
        self, provider_id: str
    ) -> ConnectedAccountStatus: ...

    async def disconnect_connected_account(
        self, provider_id: str
    ) -> ConnectedAccountStatus: ...


class ChatSessionPort(Protocol):
    """One live Claude Code process driven by the local chat UI."""

    @property
    def live_id(self) -> str: ...

    def snapshot(self) -> JsonObject: ...

    def subscribe(self) -> AsyncIterator[JsonObject]: ...

    async def send_user_message(self, content: JsonValue) -> None: ...

    async def control(self, request: JsonObject) -> JsonObject: ...

    async def respond_permission(
        self, request_id: str, decision: JsonObject
    ) -> None: ...


class ChatRuntimePort(Protocol):
    """Live chat sessions plus Claude Code's saved transcripts."""

    async def start(
        self,
        *,
        cwd: str,
        permission_mode: str | None = None,
        model: str | None = None,
        resume_session_id: str | None = None,
        policy_preset: str | None = None,
        budget: Mapping[str, JsonValue] | None = None,
    ) -> ChatSessionPort: ...

    def get(self, live_id: str) -> ChatSessionPort | None: ...

    def live_sessions(self) -> Sequence[ChatSessionPort]: ...

    async def close(self, live_id: str) -> bool: ...

    def list_transcripts(self) -> list[JsonObject]: ...

    def transcript_events(self, session_id: str) -> list[JsonObject] | None: ...

    def rename_transcript(self, session_id: str, title: str) -> bool: ...

    def delete_transcript(self, session_id: str) -> bool: ...

    async def search_files(self, cwd: str, query: str) -> list[str]: ...

    async def list_dirs(self, path: str) -> JsonObject: ...


class EndpointPoolPort(Protocol):
    """Provider key pool health and usage exposed by the local Admin API."""

    def endpoint_health(self) -> JsonObject: ...

    def usage_summary(self, minutes: float) -> JsonObject: ...

    def reset_endpoint(self, provider_id: str, label: str) -> bool: ...


class WorkbenchPort(Protocol):
    """Verified/parallel/ultra tasks, policy presets, vault secrets and the audit log.

    Lookups return ``None``/``False`` for unknown ids; invalid requests raise
    ``InvalidRequestError`` (rendered as HTTP 400).
    """

    async def start_task(
        self,
        live_id: str,
        content: JsonValue,
        *,
        mode: str,
        strategy: str,
        verify: bool = False,
        max_parallel: int | None = None,
    ) -> str | None: ...

    async def clarify(self, task_id: str, answers: str) -> bool: ...

    async def resume(self, task_id: str, live_id: str | None = None) -> bool: ...

    def project(self, cwd: str) -> JsonObject: ...

    def task(self, task_id: str) -> JsonObject | None: ...

    def evidence(self, task_id: str, fmt: str) -> str | None: ...

    async def revert(self, task_id: str) -> list[str] | None: ...

    async def cancel(self, task_id: str) -> bool: ...

    def session_tasks(self, session_id: str) -> list[JsonObject]: ...

    def policy_presets(self) -> list[JsonObject]: ...

    def list_secrets(self) -> JsonObject: ...

    def set_secret(self, name: str, value: str) -> None: ...

    def delete_secret(self, name: str) -> bool: ...

    def migrate_secrets(self) -> JsonObject: ...

    def audit_records(
        self, *, limit: int = 100, action: str = "", actor: str = ""
    ) -> JsonObject: ...


@dataclass(frozen=True, slots=True)
class ApiServices:
    """Complete runtime boundary required to construct the API application."""

    requests: RequestRuntimePort
    admin: AdminRuntimePort
    tasks: TaskController
    chat: ChatRuntimePort | None = None
    endpoints: EndpointPoolPort | None = None
    workbench: WorkbenchPort | None = None
