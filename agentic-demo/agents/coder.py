from agents.base import BaseAgent, AgentMessage
from tools.code_gen import generate_code, validate_code
from typing import Any


class CoderAgent(BaseAgent):
    def __init__(self):
        super().__init__("Coder", "Code Generation & Implementation")

    async def process(self, message: AgentMessage) -> AgentMessage:
        task = message.content
        self.add_memory(message)

        context = self.get_context()
        code = await generate_code(task, context)

        valid, issues = validate_code(code)
        if not valid:
            code = await self._fix_code(code, issues)

        return AgentMessage(
            sender=self.name,
            recipient=message.sender,
            content=code,
            metadata={"type": "code", "validated": valid}
        )

    async def _fix_code(self, code: str, issues: list[str]) -> str:
        fix_prompt = f"Fix these issues in the code:\n{code}\n\nIssues:\n" + "\n".join(issues)
        return await generate_code(fix_prompt, "")