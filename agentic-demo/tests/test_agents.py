import pytest
from agents.base import BaseAgent, AgentMessage
from agents.researcher import ResearcherAgent
from agents.coder import CoderAgent
from agents.reviewer import ReviewerAgent
from agents.planner import PlannerAgent
from orchestrator.coordinator import AgentOrchestrator


class TestAgent(BaseAgent):
    async def process(self, message: AgentMessage) -> AgentMessage:
        return AgentMessage(
            sender=self.name,
            recipient=message.sender,
            content=f"Processed: {message.content}"
        )


@pytest.mark.asyncio
async def test_base_agent():
    agent = TestAgent("Test", "Testing")
    msg = AgentMessage("user", "Test", "Hello")
    response = await agent.process(msg)
    assert response.sender == "Test"
    assert "Hello" in response.content


@pytest.mark.asyncio
async def test_planner():
    agent = PlannerAgent()
    msg = AgentMessage("user", "Planner", "Build API")
    response = await agent.process(msg)
    assert "Plan for" in response.content
    assert "Research" in response.content


@pytest.mark.asyncio
async def test_researcher():
    agent = ResearcherAgent()
    msg = AgentMessage("user", "Researcher", "Python async")
    response = await agent.process(msg)
    assert "Research on" in response.content


@pytest.mark.asyncio
async def test_coder():
    agent = CoderAgent()
    msg = AgentMessage("user", "Coder", "Hello world")
    response = await agent.process(msg)
    assert "def main" in response.content


@pytest.mark.asyncio
async def test_reviewer():
    agent = ReviewerAgent()
    code = '''def main():\n    print("hi")\n\nif __name__ == "__main__":\n    main()'''
    msg = AgentMessage("user", "Reviewer", code)
    response = await agent.process(msg)
    assert response.content["approved"] is True


@pytest.mark.asyncio
async def test_orchestrator_full_flow():
    orch = AgentOrchestrator()
    result = await orch.run("Simple test task")
    assert "code" in result
    assert "review" in result
    assert result["review"]["approved"] is True