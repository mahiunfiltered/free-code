from agents.base import BaseAgent, AgentMessage
from tools.review import review_code
from typing import Any


class ReviewerAgent(BaseAgent):
    def __init__(self):
        super().__init__("Reviewer", "Code Review & Quality Assurance")

    async def process(self, message: AgentMessage) -> AgentMessage:
        code = message.content
        self.add_memory(message)

        review = await review_code(code, self.get_context())

        return AgentMessage(
            sender=self.name,
            recipient=message.sender,
            content=review["summary"],
            metadata={"type": "review", **review},
        )