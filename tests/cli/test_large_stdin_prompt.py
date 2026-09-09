import os
import subprocess
import sys
import pytest

def test_large_prompt_generation_and_transport():
    """Verify that a 100 KB prompt can be constructed and streamed without error."""
    large_text = "Free Claude Code Large Prompt Line Test\n" * 2500  # ~100 KB
    assert len(large_text.encode("utf-8")) > 95000

    # Test that Python subprocess can receive the large stream cleanly
    proc = subprocess.run(
        [sys.executable, "-c", "import sys; data = sys.stdin.read(); print(f'RECEIVED_{len(data)}')"],
        input=large_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0
    assert f"RECEIVED_{len(large_text)}" in proc.stdout
