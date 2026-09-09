#!/usr/bin/env python3
"""Simple test runner for the demo"""
import asyncio
import sys
sys.path.insert(0, '.')

from agents.base import AgentMessage
from agents.researcher import ResearcherAgent
from agents.coder import CoderAgent
from agents.reviewer import ReviewerAgent
from agents.planner import PlannerAgent
from orchestrator.coordinator import AgentOrchestrator


async def test_base_agent():
    from agents.base import BaseAgent
    class TestAgent(BaseAgent):
        async def process(self, msg):
            return AgentMessage(self.name, msg.sender, f"Processed: {msg.content}")
    agent = TestAgent("Test", "Testing")
    msg = AgentMessage("user", "Test", "Hello")
    response = await agent.process(msg)
    assert response.sender == "Test"
    assert "Hello" in response.content
    print("[PASS] test_base_agent")


async def test_planner():
    agent = PlannerAgent()
    msg = AgentMessage("user", "Planner", "Build API")
    response = await agent.process(msg)
    assert "Plan for" in response.content
    assert "Research" in response.content
    print("[PASS] test_planner")


async def test_researcher():
    agent = ResearcherAgent()
    msg = AgentMessage("user", "Researcher", "Python async")
    response = await agent.process(msg)
    assert "Research on" in response.content
    print("[PASS] test_researcher")


async def test_coder():
    agent = CoderAgent()
    msg = AgentMessage("user", "Coder", "Hello world")
    response = await agent.process(msg)
    assert "def main" in response.content
    print("[PASS] test_coder")


async def test_reviewer():
    agent = ReviewerAgent()
    code = 'def main():\n    """Main entry"""\n    print("hi")\n\nif __name__ == "__main__":\n    main()'
    msg = AgentMessage("user", "Reviewer", code)
    response = await agent.process(msg)
    assert response.content["approved"] is True
    print("[PASS] test_reviewer")


async def test_orchestrator_full_flow():
    orch = AgentOrchestrator()
    result = await orch.run("Simple test task")
    assert "code" in result
    assert "review" in result
    assert result["review"]["approved"] is True
    print("[PASS] test_orchestrator_full_flow")


async def main():
    tests = [
        test_base_agent,
        test_planner,
        test_researcher,
        test_coder,
        test_reviewer,
        test_orchestrator_full_flow,
    ]
    for test in tests:
        try:
            await test()
        except Exception as e:
            print(f"[FAIL] {test.__name__}: {e}")
            return 1
    print("\nAll tests passed!")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))