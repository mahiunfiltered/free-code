from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any
import json


@dataclass
class AgentMessage:
    sender: str
    recipient: str
    content: str
    metadata: dict[str, Any] | None = None


class BaseAgent(ABC):
    def __init__(self, name: str, role: str):
        self.name = name
        self.role = role
        self.memory: list[AgentMessage] = []

    @abstractmethod
    async def process(self, message: AgentMessage) -> AgentMessage:
        pass

    def add_memory(self, msg: AgentMessage):
        self.memory.append(msg)

    def get_context(self, limit: int = 5) -> str:
        recent = self.memory[-limit:]
        return "\n".join(f"{m.sender}: {m.content}" for m in recent)