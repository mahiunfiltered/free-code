async def generate_code(task: str, context: str) -> str:
    template = f'''# Generated code for: {task}
# Context: {context[:200] if context else "None"}

def main():
    """Main entry point"""
    print("Task:", "{task}")
    result = process()
    return result


def process():
    """Core processing logic"""
    data = fetch_data()
    transformed = transform(data)
    return save(transformed)


def fetch_data():
    return [1, 2, 3, 4, 5]


def transform(data):
    return [x * 2 for x in data]


def save(data):
    print(f"Saved: {{data}}")
    return data


if __name__ == "__main__":
    main()
'''
    return template


def validate_code(code: str) -> tuple[bool, list[str]]:
    issues = []
    if "def main" not in code:
        issues.append("Missing main function")
    if "if __name__" not in code:
        issues.append("Missing entry point guard")
    return len(issues) == 0, issues