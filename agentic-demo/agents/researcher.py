from agents.base import BaseAgent, AgentMessage
from tools.search import web_search, search_codebase
from typing import Any


class ResearcherAgent(BaseAgent):
    def __init__(self):
        super().__init__("Researcher", "Research & Information Gathering")

    async def process(self, message: AgentMessage) -> AgentMessage:
        query = message.content
        self.add_memory(message)

        web_results = await web_search(query)
        code_results = await search_codebase(query)

        synthesis = self._synthesize(query, web_results, code_results)

        return AgentMessage(
            sender=self.name,
            recipient=message.sender,
            content=synthesis,
            metadata={"type": "research", "sources": len(web_results) + len(code_results)}
        )

    def _synthesize(self, query: str, web: list[dict], code: list[dict]) -> str:
        parts = [f"Research on: {query}\n"]
        if web:
            parts.append("Web sources:")
            for r in web[:3]:
                parts.append(f"  - {r['title']}: {r['snippet'][:100]}")
        if code:
            parts.append("\nCodebase matches:")
            for r in code[:3]:
                parts.append(f"  - {r['file']}:{r['line']}")
        return "\n".join(parts)