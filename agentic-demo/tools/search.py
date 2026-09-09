async def web_search(query: str) -> list[dict]:
    return [
        {"title": f"Result for '{query}'", "snippet": f"Sample search result about {query}", "url": "https://example.com"},
        {"title": f"Guide: {query}", "snippet": f"Comprehensive guide on {query} with examples", "url": "https://guide.example.com"},
    ]


async def search_codebase(query: str) -> list[dict]:
    return [
        {"file": "src/main.py", "line": 42, "match": f"def handle_{query.lower().replace(' ', '_')}"},
        {"file": "src/utils.py", "line": 15, "match": f"# TODO: implement {query}"},
    ]