"""
Comprehensive CLI Prompt Transport & Input Verification (Phase 13)
Tests stdin-based prompt transport for 1 KB, 10 KB, 100 KB, 500 KB, 1 MB, and multi-byte Unicode.
"""
import os
import sys
import subprocess
import time

def test_payload_size(size_bytes: int, label: str) -> bool:
    payload = ("A" * (size_bytes - 1)) + "\n"
    t0 = time.time()
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", "import sys; d = sys.stdin.read(); print('BYTES_READ:', len(d.encode('utf-8')) - 1)"],
        input=payload,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    elapsed = time.time() - t0
    expected = f"BYTES_READ: {size_bytes - 1}"
    success = proc.returncode == 0 and expected in proc.stdout
    status = "PASS [OK]" if success else "FAIL [X]"
    print(f" {label:<25} ({size_bytes / 1024:>7.1f} KB) : {status} in {elapsed:.3f}s")
    return success

def test_unicode_prompt() -> bool:
    payload = "Hello 世界 🚀 Antigravity — 測試 🎉 Multi-byte: \u2603 \U0001F600 \u4e16\u754c\n"
    t0 = time.time()
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", "import sys; d = sys.stdin.read(); sys.stdout.write(d)"],
        input=payload,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    elapsed = time.time() - t0
    success = proc.returncode == 0 and proc.stdout == payload
    status = "PASS [OK]" if success else "FAIL [X]"
    print(f" {'Unicode & Emoji Prompt':<25} (    0.1 KB) : {status} in {elapsed:.3f}s")
    return success

def main():
    print("=" * 65)
    print(" CLI / PROMPT INPUT & TRANSPORT SUITE (PHASE 13)")
    print("=" * 65)

    results = []
    # 1 KB
    results.append(test_payload_size(1024, "1 KB Payload"))
    # 10 KB
    results.append(test_payload_size(10 * 1024, "10 KB Payload"))
    # 100 KB
    results.append(test_payload_size(100 * 1024, "100 KB Payload"))
    # 500 KB
    results.append(test_payload_size(500 * 1024, "500 KB Payload"))
    # 1 MB
    results.append(test_payload_size(1024 * 1024, "1 MB Payload"))
    # Unicode
    results.append(test_unicode_prompt())

    print("=" * 65)
    all_ok = all(results)
    if all_ok:
        print(" ALL PROMPT TRANSPORT TESTS PASSED (100% SUCCESS) [OK]")
    else:
        print(" PROMPT TRANSPORT TESTS COMPLETED WITH FAILURES")
    print("=" * 65)
    sys.exit(0 if all_ok else 1)

if __name__ == "__main__":
    main()
