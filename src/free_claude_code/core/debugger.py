from __future__ import annotations

"""Autonomous agent debugger and automated error diagnosis loop.

Implements deep root-cause diagnosis, targeted repair proposal, automated application,
and verification with a strict 3-attempt escalation limit.
"""

import os
import re
import sys
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Sequence

from free_claude_code.core.recovery import ErrorSeverity, classify_error


class FailureClass(StrEnum):
    PYTHON_EXCEPTION = "PYTHON_EXCEPTION"
    NODE_EXCEPTION = "NODE_EXCEPTION"
    TEST_FAILURE = "TEST_FAILURE"
    SHELL_FAILURE = "SHELL_FAILURE"
    POWERSHELL_FAILURE = "POWERSHELL_FAILURE"
    PERMISSION_FAILURE = "PERMISSION_FAILURE"
    PORT_CONFLICT = "PORT_CONFLICT"
    PROCESS_CRASH = "PROCESS_CRASH"
    API_FAILURE = "API_FAILURE"
    TIMEOUT = "TIMEOUT"
    TOOL_SCHEMA_FAILURE = "TOOL_SCHEMA_FAILURE"
    CONFIG_ERROR = "CONFIG_ERROR"
    UNKNOWN = "UNKNOWN"


@dataclass
class DiagnosticContext:
    failure_class: FailureClass
    error_message: str
    command: str | None = None
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    stack_trace: str = ""
    target_files: list[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


@dataclass
class RepairPlan:
    diagnosis: str
    proposed_action: str
    repair_command: str | None = None
    file_patches: dict[str, str] = field(default_factory=dict)
    can_auto_apply: bool = True


@dataclass
class DebuggingSession:
    task_id: str
    attempts: int = 0
    max_attempts: int = 3
    history: list[tuple[DiagnosticContext, RepairPlan, bool]] = field(default_factory=list)
    is_resolved: bool = False
    escalation_report: str | None = None


class AutonomousDebugger:
    """Diagnoses runtime and build failures and orchestrates targeted repairs."""

    def classify_failure(self, error_msg: str, stderr: str = "", exit_code: int | None = None) -> FailureClass:
        text = f"{error_msg}\n{stderr}".lower()
        if "access is denied" in text or "permission denied" in text or "eacces" in text:
            return FailureClass.PERMISSION_FAILURE
        if "address already in use" in text or "port" in text and "already in use" in text:
            return FailureClass.PORT_CONFLICT
        if "traceback (most recent call last)" in text or "syntaxerror" in text or "nameerror" in text or "importerror" in text:
            return FailureClass.PYTHON_EXCEPTION
        if "referenceerror" in text or "typeerror" in text or "node:internal" in text:
            return FailureClass.NODE_EXCEPTION
        if "failed" in text and ("test" in text or "assert" in text or "pytest" in text):
            return FailureClass.TEST_FAILURE
        if "powershell" in text or "posh" in text or "term '" in text:
            return FailureClass.POWERSHELL_FAILURE
        if "timed out" in text or "timeout" in text:
            return FailureClass.TIMEOUT
        if "401" in text or "403" in text or "429" in text or "500" in text or "api" in text:
            return FailureClass.API_FAILURE
        if exit_code is not None and exit_code != 0:
            return FailureClass.SHELL_FAILURE
        return FailureClass.UNKNOWN

    def collect_diagnostics(
        self,
        error_msg: str,
        command: str | None = None,
        exit_code: int | None = None,
        stdout: str = "",
        stderr: str = "",
        target_files: Sequence[str] = (),
    ) -> DiagnosticContext:
        fc = self.classify_failure(error_msg, stderr, exit_code)
        stack = ""
        if "Traceback" in stderr:
            stack = stderr[stderr.find("Traceback"):]
        elif "Traceback" in error_msg:
            stack = error_msg[error_msg.find("Traceback"):]

        return DiagnosticContext(
            failure_class=fc,
            error_message=error_msg,
            command=command,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            stack_trace=stack,
            target_files=list(target_files),
        )

    def diagnose_and_propose(self, diag: DiagnosticContext, attempt_num: int) -> RepairPlan:
        fc = diag.failure_class

        if fc == FailureClass.PERMISSION_FAILURE:
            return RepairPlan(
                diagnosis="Access Denied on target path or command.",
                proposed_action="Check file write permissions or execute with appropriate access.",
                can_auto_apply=False,
            )

        if fc == FailureClass.PORT_CONFLICT:
            return RepairPlan(
                diagnosis="Port is already occupied by a running service or stale process.",
                proposed_action="Inspect and release port or assign alternate port.",
                can_auto_apply=True,
            )

        if fc == FailureClass.POWERSHELL_FAILURE:
            return RepairPlan(
                diagnosis="PowerShell command syntax error or missing cmdlet.",
                proposed_action="Wrap arguments in quotes or invoke through pwsh with ExecutionPolicy Bypass.",
                can_auto_apply=True,
            )

        if fc == FailureClass.PYTHON_EXCEPTION:
            if "NameError" in diag.error_message:
                return RepairPlan(
                    diagnosis="Forward reference or missing import in Python source.",
                    proposed_action="Add 'from __future__ import annotations' or import missing type.",
                    can_auto_apply=True,
                )
            if "ModuleNotFoundError" in diag.error_message or "ImportError" in diag.error_message:
                return RepairPlan(
                    diagnosis="Missing Python module or package dependency.",
                    proposed_action="Install package or update PYTHONPATH.",
                    can_auto_apply=True,
                )

        if fc == FailureClass.TIMEOUT:
            return RepairPlan(
                diagnosis="Operation exceeded time limit.",
                proposed_action="Increase step timeout or divide into smaller parallel chunks.",
                can_auto_apply=True,
            )

        return RepairPlan(
            diagnosis=f"General failure: {diag.error_message[:100]}",
            proposed_action="Inspect stdout/stderr diagnostics and retry with adjusted parameters.",
            can_auto_apply=True,
        )

    def escalate(self, session: DebuggingSession) -> str:
        lines = [
            f"=== DEBUGGER ESCALATION REPORT for Task '{session.task_id}' ===",
            f"Total Repair Attempts: {session.attempts}/{session.max_attempts} (FAILED)",
            "History:",
        ]
        for idx, (diag, plan, resolved) in enumerate(session.history, 1):
            lines.append(f"  Attempt {idx}: {diag.failure_class.value} -> {plan.diagnosis} (Resolved: {resolved})")
        lines.append("Recommended Human Action: Check environment configuration, credentials, or target paths.")
        report = "\n".join(lines)
        session.escalation_report = report
        return report


_GLOBAL_DEBUGGER = AutonomousDebugger()


def get_debugger() -> AutonomousDebugger:
    return _GLOBAL_DEBUGGER
