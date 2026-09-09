from agents.base import BaseAgent, AgentMessage
from typing import Any


class PlannerAgent(BaseAgent):
    def __init__(self):
        super().__init__("Planner", "Task Planning & Coordination")

    async def process(self, message: AgentMessage) -> AgentMessage:
        goal = message.content
        self.add_memory(message)

        plan = self._create_plan(goal)

        return AgentMessage(
            sender=self.name,
            recipient=message.sender,
            content=plan,
            metadata={"type": "plan", "steps": len(plan.split("\n"))}
        )

    def _create_plan(self, goal: str) -> str:
        steps = [
            "1. Research requirements and existing solutions",
            "2. Design architecture and data models",
            "3. Implement core functionality",
            "4. Write tests and validate",
            "5. Review and refine",
            "6. Document and deploy"
        ]
        return f"Plan for: {goal}\n\n" + "\n".join(steps)