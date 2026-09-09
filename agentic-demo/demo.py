#!/usr/bin/env python3
"""
Multi-Agent AI Demo
Demonstrates agentic workflow: Planner → Researcher → Coder → Reviewer
"""
import asyncio
from orchestrator.coordinator import AgentOrchestrator


async def main():
    orchestrator = AgentOrchestrator()

    goals = [
        "Build a REST API for a todo list with CRUD operations",
        "Create a data validation pipeline for CSV files",
        "Implement a caching layer with TTL support",
    ]

    for i, goal in enumerate(goals, 1):
        print(f"\n{'#'*60}")
        print(f"DEMO {i}/{len(goals)}: {goal}")
        print(f"{'#'*60}")

        result = await orchestrator.run(goal)

        print(f"\n--- FINAL CODE ---")
        print(result["code"][:500] + "..." if len(result["code"]) > 500 else result["code"])
        print(f"\n--- REVIEW ---")
        print(result["review"]["summary"])

    print(f"\n{'='*60}")
    print("DEMO COMPLETE - All workflows executed successfully!")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())