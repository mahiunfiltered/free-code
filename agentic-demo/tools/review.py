async def review_code(code: str, context: str) -> dict:
    checks = {
        "has_main": "def main" in code,
        "has_entry_point": "if __name__" in code,
        "has_docstrings": '"""' in code or "'''" in code,
        "no_bare_except": "except:" not in code,
    }

    approved = all(checks.values())
    feedback = []

    if not checks["has_main"]:
        feedback.append("Add main() function")
    if not checks["has_entry_point"]:
        feedback.append("Add if __name__ == '__main__' guard")
    if not checks["has_docstrings"]:
        feedback.append("Add docstrings to functions")
    if not checks["no_bare_except"]:
        feedback.append("Avoid bare except clauses")

    return {
        "approved": approved,
        "checks": checks,
        "feedback": feedback,
        "summary": "Code approved" if approved else f"Issues found: {', '.join(feedback)}"
    }