import asyncio
from agents.reviewer import ReviewerAgent
from agents.base import AgentMessage

async def test():
    agent = ReviewerAgent()
    code = 'def main():\n    print("hi")\n\nif __name__ == "__main__":\n    main()'
    msg = AgentMessage('user', 'Reviewer', code)
    response = await agent.process(msg)
    print('Approved:', (response.metadata or {}).get('approved'))
    print('Feedback:', (response.metadata or {}).get('feedback'))
    print('Checks:', (response.metadata or {}).get('checks'))

asyncio.run(test())