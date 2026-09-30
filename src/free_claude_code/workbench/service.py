"""Workbench features behind the API ports: tasks, policy, secrets, audit, chat observer.

The runtime composes one ``WorkbenchService`` on the server-wide ``Store`` and exposes it
to the API through ``api.ports.WorkbenchPort`` (structural typing; no api import here).
"""

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

from free_claude_code.application.errors import InvalidRequestError
from free_claude_code.cli import secret_command
from free_claude_code.cli.managed.interactive import (
    InteractiveClaudeSession,
    InteractiveClaudeSessions,
)
from free_claude_code.config.loader import managed_vault
from free_claude_code.core.diagnostics import register_secret_value
from free_claude_code.core.json_types import JsonObject, JsonValue
from free_claude_code.core.storage import Store
from free_claude_code.core.vault import VaultError, validate_secret_name
from free_claude_code.workbench.audit import AuditLog
from free_claude_code.workbench.checkpoints import git, project_status
from free_claude_code.workbench.coordinator import (
    AnalysisClientFactory,
    CoordinatorOptions,
    ModelClientFactory,
    TaskCoordinator,
)
from free_claude_code.workbench.injection import scan
from free_claude_code.workbench.memory import SolutionMemory
from free_claude_code.workbench.policy import (
    Preset,
    compile_policy,
    load_project_rules,
)
from free_claude_code.workbench.tasks import TaskEvent, TaskRecord, TaskStore

type UsageLookup = Callable[[str], JsonObject | None]


def launch_policy(preset: str, cwd: str) -> tuple[str, str]:
    """``--settings`` JSON and permission mode for a chat started in ``cwd``."""

    try:
        branch: str | None = git(cwd, "rev-parse", "--abbrev-ref", "HEAD").strip()
    except subprocess.CalledProcessError, OSError:
        branch = None
    compiled = compile_policy(
        preset,
        project_rules=load_project_rules(Path(cwd)),
        branch=branch if branch and branch != "HEAD" else None,
    )
    return compiled.settings_json(), compiled.permission_mode


def policy_presets() -> list[JsonObject]:
    presets: list[JsonObject] = []
    for preset in Preset:
        compiled = compile_policy(preset)
        presets.append(
            {
                "id": preset.value,
                "permission_mode": compiled.permission_mode,
                "rules": [str(rule) for rule in compiled.rules],
            }
        )
    return presets


def _task_json(record: TaskRecord, mode: str | None) -> JsonObject:
    return {
        "id": record.id,
        "task_id": record.id,
        "session_id": record.session_id,
        "cwd": record.cwd,
        "mode": mode,
        "status": record.status,
        "contract": record.contract.to_json() if record.contract else None,
        "checkpoint": record.checkpoint.to_json() if record.checkpoint else None,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def _tool_result_text(block: JsonObject) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


class WorkbenchService:
    def __init__(
        self,
        store: Store,
        *,
        sessions: InteractiveClaudeSessions | None = None,
        model_client_factory: ModelClientFactory | None = None,
        analysis_client_factory: AnalysisClientFactory | None = None,
        usage_lookup: UsageLookup | None = None,
        options: CoordinatorOptions | None = None,
    ) -> None:
        self.tasks = TaskStore(store)
        self.audit = AuditLog(store)
        self.memory = SolutionMemory(store)
        self._sessions = sessions
        self._usage_lookup = usage_lookup
        self.coordinator = TaskCoordinator(
            tasks=self.tasks,
            audit=self.audit,
            memory=self.memory,
            sessions=sessions,
            model_client_factory=model_client_factory,
            analysis_client_factory=analysis_client_factory,
            options=options,
        )

    # ----- chat tasks -------------------------------------------------------

    async def start_task(
        self,
        live_id: str,
        content: JsonValue,
        *,
        mode: str,
        strategy: str,
        verify: bool = False,
        max_parallel: int | None = None,
    ) -> str | None:
        """Start a verified/parallel/ultra task; None when the live session is unknown."""

        session = self._sessions.get(live_id) if self._sessions else None
        if session is None:
            return None
        return self.coordinator.start(
            session,
            _prompt_text(content),
            mode=mode,
            strategy=strategy,
            images=_image_blocks(content),
            verify=verify,
            max_parallel=max_parallel,
        )

    async def resume(self, task_id: str, live_id: str | None = None) -> bool:
        """One more attempt for a RECOVERY_REQUIRED task; False if the task is unknown.

        Runs on ``live_id``, or else on the live chat of the task's Claude session.
        """

        if not self._exists(task_id):
            return False
        session = self._live_session(task_id, live_id)
        if session is None:
            raise InvalidRequestError("Open this task's chat to resume it.")
        self.coordinator.resume(session, task_id)
        return True

    def project(self, cwd: str) -> JsonObject:
        if not Path(cwd).is_dir():
            raise InvalidRequestError(f"Folder does not exist: {cwd}")
        return project_status(cwd)

    def close_interrupted(self) -> list[str]:
        """Startup: tasks left mid-run by a previous server process stop waiting."""

        closed = self.tasks.close_interrupted("server restarted")
        for task_id, before, after in closed:
            self.audit.append(
                actor="workbench",
                action="task.interrupted",
                resource=task_id,
                outcome=after,
                payload={"from": before, "reason": "server restarted"},
            )
        return [task_id for task_id, _, _ in closed]

    async def clarify(self, task_id: str, answers: str) -> bool:
        if not self._exists(task_id):
            return False
        await self.coordinator.clarify(task_id, answers)
        return True

    def task(self, task_id: str) -> JsonObject | None:
        if not self._exists(task_id):
            return None
        events = self.tasks.events(task_id)
        return {
            "task": _task_json(self.tasks.get(task_id), _mode(events)),
            "events": _events_json(events),
            "evidence": list(self.tasks.evidence(task_id)),
        }

    def evidence(self, task_id: str, fmt: str) -> str | None:
        """Latest evidence package as markdown/json; None if the task has none."""

        if fmt not in ("json", "markdown"):
            raise InvalidRequestError("format must be json or markdown")
        try:
            return self.tasks.export(
                task_id, "markdown" if fmt == "markdown" else "json"
            )
        except KeyError:
            return None

    async def revert(self, task_id: str) -> list[str] | None:
        if not self._exists(task_id):
            return None
        return await self.coordinator.revert(task_id)

    async def cancel(self, task_id: str) -> bool:
        if not self._exists(task_id):
            return False
        return await self.coordinator.cancel(task_id)

    def session_tasks(self, session_id: str) -> list[JsonObject]:
        """Every task of a Claude session with its evidence (reopened chats)."""

        tasks: list[JsonObject] = []
        for record in self.tasks.for_session(session_id):
            events = self.tasks.events(record.id)
            tasks.append(
                {
                    **_task_json(record, _mode(events)),
                    "evidence": list(self.tasks.evidence(record.id)),
                    "events": _events_json(events),
                }
            )
        return tasks

    def policy_presets(self) -> list[JsonObject]:
        return policy_presets()

    # ----- chat observer ----------------------------------------------------

    def observe(self, session: InteractiveClaudeSession, event: JsonObject) -> None:
        """Injection flags, per-session usage and permission audit for chat events."""

        kind = event.get("type")
        if kind == "user":
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                found = scan(_tool_result_text(block))
                if found.suspicious:
                    session.publish(
                        {
                            "type": "fcc_injection",
                            "tool_use_id": block.get("tool_use_id"),
                            "signals": list(found.signals),
                        }
                    )
        elif kind == "result" and self._usage_lookup and session.session_id:
            usage = self._usage_lookup(session.session_id)
            if usage is not None:
                session.publish({"type": "fcc_usage", **usage})
        elif (
            kind == "fcc_permission_resolved"
            and event.get("behavior")
            # A node's answer is audited on the node session; the chat copy is a relay.
            and "node_id" not in event
        ):
            self.audit.append(
                actor="user",
                action="permission.decision",
                resource=str(event.get("tool_name") or ""),
                decision=str(event.get("behavior")),
                outcome="answered",
                payload={
                    "live_id": session.live_id,
                    "session_id": session.session_id,
                    "request_id": event.get("request_id"),
                    "cwd": session.cwd,
                },
            )

    # ----- admin: secrets and audit -----------------------------------------

    def list_secrets(self) -> JsonObject:
        try:
            names: list[JsonValue] = list(secret_command.list_secrets())
        except (VaultError, OSError) as exc:
            return {"available": False, "error": str(exc), "names": []}
        return {"available": True, "error": None, "names": names}

    def set_secret(self, name: str, value: str) -> None:
        name = _secret_name(name)
        if not value.strip():
            raise InvalidRequestError("Secret value is empty.")
        register_secret_value(value)
        try:
            secret_command.set_secret(name, value)
        except (VaultError, OSError) as exc:
            self._audit_secret("secret.set", name, "error")
            raise InvalidRequestError(f"Vault error: {exc}") from exc
        self._audit_secret("secret.set", name, "stored")

    def delete_secret(self, name: str) -> bool:
        name = _secret_name(name)
        try:
            deleted = secret_command.delete_secret(name)
        except (VaultError, OSError) as exc:
            raise InvalidRequestError(f"Vault error: {exc}") from exc
        self._audit_secret("secret.delete", name, "deleted" if deleted else "missing")
        return deleted

    def migrate_secrets(self) -> JsonObject:
        try:
            migrated, backup = secret_command.migrate_env()
        except (VaultError, OSError, TimeoutError) as exc:
            raise InvalidRequestError(f"Vault error: {exc}") from exc
        vault = managed_vault()
        for name in migrated:
            register_secret_value(vault.get(name))
        self.audit.append(
            actor="admin",
            action="secret.migrate",
            resource="managed_env",
            outcome="migrated",
            payload={"keys": list(migrated)},
        )
        return {
            "migrated": list(migrated),
            "backup": str(backup) if backup else None,
        }

    def audit_records(
        self, *, limit: int = 100, action: str = "", actor: str = ""
    ) -> JsonObject:
        filters = {
            key: value for key, value in (("action", action), ("actor", actor)) if value
        }
        verified, first_bad = self.audit.verify()
        return {
            "verified": verified,
            "first_bad_id": first_bad,
            "records": [
                {
                    "id": record.id,
                    "ts": record.ts,
                    "actor": record.actor,
                    "action": record.action,
                    "resource": record.resource,
                    "decision": record.decision,
                    "outcome": record.outcome,
                    "payload": _json_object(record.payload_json),
                }
                for record in self.audit.recent(limit, **filters)
            ],
        }

    def record_config_apply(self, keys: list[str], result: JsonObject) -> None:
        """Audit an Admin config apply (field names only, never values)."""

        self.audit.append(
            actor="admin",
            action="config.apply",
            resource="managed_env",
            outcome="applied" if result.get("applied") else "rejected",
            payload={"keys": list(keys)},
        )

    async def close(self) -> None:
        await self.coordinator.close()

    # ----- helpers ----------------------------------------------------------

    def _live_session(
        self, task_id: str, live_id: str | None
    ) -> InteractiveClaudeSession | None:
        if self._sessions is None:
            return None
        if live_id:
            return self._sessions.get(live_id)
        session_id = self.tasks.get(task_id).session_id
        for session in self._sessions.live_sessions():
            if not session.exited and session_id in (
                session.session_id,
                session.live_id,
            ):
                return session
        return None

    def _exists(self, task_id: str) -> bool:
        try:
            self.tasks.get(task_id)
        except KeyError:
            return False
        return True

    def _audit_secret(self, action: str, name: str, outcome: str) -> None:
        self.audit.append(actor="admin", action=action, resource=name, outcome=outcome)


def _secret_name(name: str) -> str:
    try:
        return validate_secret_name(name)
    except ValueError as exc:
        raise InvalidRequestError(str(exc)) from exc


def _prompt_text(content: JsonValue) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _image_blocks(content: JsonValue) -> list[JsonObject]:
    if not isinstance(content, list):
        return []
    return [
        block
        for block in content
        if isinstance(block, dict) and block.get("type") == "image"
    ]


def _events_json(events: list[TaskEvent]) -> list[JsonValue]:
    return [
        {"ts": event.ts, "type": event.type, "payload": event.payload}
        for event in events
    ]


def _mode(events: list[TaskEvent]) -> str | None:
    for event in events:
        if event.type == "task.mode":
            mode = event.payload.get("mode")
            return mode if isinstance(mode, str) else None
    return None


def _json_object(text: str) -> JsonValue:
    try:
        return json.loads(text)
    except ValueError:
        return text
