from agents.base import AgentMessage
from agents.researcher import ResearcherAgent
from agents.coder import CoderAgent
from agents.reviewer import ReviewerAgent
from agents.planner import PlannerAgent
from typing import Any
import asyncio


class AgentOrchestrator:
    def __init__(self):
        self.agents = {
            "planner": PlannerAgent(),
            "researcher": ResearcherAgent(),
            "coder": CoderAgent(),
            "reviewer": ReviewerAgent(),
        }
        self.history: list[AgentMessage] = []

    async def run(self, goal: str) -> dict[str, Any]:
        print(f"\n{'='*50}")
        print(f"ORCHESTRATOR: Starting workflow for: {goal}")
        print(f"{'='*50}\n")

        plan_msg = await self._send("user", "planner", goal)
        print(f"[PLANNER] -> {plan_msg.content[:100]}...\n")

        research_msg = await self._send("planner", "researcher", plan_msg.content)
        print(f"[RESEARCHER] -> {research_msg.content[:100]}...\n")

        code_msg = await self._send("researcher", "coder", research_msg.content)
        print(f"[CODER] -> Generated code ({len(code_msg.content)} chars)\n")

        review_msg = await self._send("coder", "reviewer", code_msg.content)
        print(f"[REVIEWER] -> {review_msg.content.get('summary', 'Review complete')}\n")

        if not review_msg.content.get("approved", False):
            print("  -> Fixing issues...")
            fix_msg = await self._send("reviewer", "coder", f"Fix: {review_msg.content['feedback']}")
            code_msg = fix_msg
            review_msg = await self._send("coder", "reviewer", fix_msg.content)
            print(f"  -> Re-review: {review_msg.content.get('summary')}\n")

        return {
            "goal": goal,
            "plan": plan_msg.content,
            "research": research_msg.content,
            "code": code_msg.content,
            "review": review_msg.content,
            "history": self.history
        }

    async def _send(self, sender: str, recipient: str, content: str) -> AgentMessage:
        msg = AgentMessage(sender=sender, recipient=recipient, content=content)
        self.history.append(msg)

        if recipient in self.agents:
            response = await self.agents[recipient].process(msg)
            self.history.append(response)
            return response

        return AgentMessage(sender=recipient, recipient=sender, content="Agent not found")