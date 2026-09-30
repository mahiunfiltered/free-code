import os
import subprocess
import io
import sys
import time

# Ensure UTF-8 output encoding for Windows consoles
if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8")
if isinstance(sys.stderr, io.TextIOWrapper):
    sys.stderr.reconfigure(encoding="utf-8")

def run_test(name, cmd_args, cwd="D:\\Claude code", timeout=120):
    print(f"\n==================================================")
    print(f"RUNNING ACCEPTANCE TEST: {name}")
    print(f"COMMAND: {' '.join(cmd_args)}")
    print(f"==================================================")
    
    start_time = time.time()
    try:
        proc = subprocess.run(
            cmd_args,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout
        )
        duration = time.time() - start_time
        print(f"[STATUS] Exit Code: {proc.returncode} (took {duration:.2f}s)")
        print(f"[STDOUT]:\n{proc.stdout}")
        if proc.stderr:
            print(f"[STDERR]:\n{proc.stderr}")
            
        combined_output = proc.stdout + "\n" + proc.stderr
        
        # Check for banned errors
        banned_errors = [
            "PostToolUse:Bash hook error",
            "/bin/bash: cannot execute binary file",
            "cannot execute binary file"
        ]
        
        has_banned_error = False
        for err in banned_errors:
            if err in combined_output:
                print(f"[WARNING/FAIL] Found banned error in output: '{err}'")
                has_banned_error = True
                
        return {
            "name": name,
            "success": proc.returncode == 0 and not has_banned_error,
            "exit_code": proc.returncode,
            "duration": duration,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "has_banned_error": has_banned_error
        }
    except subprocess.TimeoutExpired:
        duration = time.time() - start_time
        print(f"[TIMEOUT] Command timed out after {timeout}s")
        return {
            "name": name,
            "success": False,
            "exit_code": -1,
            "duration": duration,
            "stdout": "",
            "stderr": "TIMEOUT",
            "has_banned_error": False
        }
    except Exception as e:
        duration = time.time() - start_time
        print(f"[ERROR] Exception running test: {e}")
        return {
            "name": name,
            "success": False,
            "exit_code": -2,
            "duration": duration,
            "stdout": "",
            "stderr": str(e),
            "has_banned_error": False
        }

def main():
    results = []
    base_cmd = ["uv", "run", "fcc-claude"]
    
    # 1. Real model direct prompt test
    results.append(run_test(
        "TEST 1: Direct Prompt & Model Completion",
        base_cmd + ["-p", "Reply exactly: REAL_MODEL_TEST_OK"]
    ))
    
    # 2. PowerShell command execution via tool
    results.append(run_test(
        "TEST 2: Tool Execution - PowerShell Command",
        base_cmd + ["--dangerously-skip-permissions", "-p", "Execute PowerShell command: Write-Output 'POWERSHELL_OK' and print the output."]
    ))
    
    # 3. Git command execution via tool
    results.append(run_test(
        "TEST 3: Tool Execution - Git Version",
        base_cmd + ["--dangerously-skip-permissions", "-p", "Run git --version and report the output."]
    ))
    
    # 4. Node command execution via tool
    results.append(run_test(
        "TEST 4: Tool Execution - Node Version",
        base_cmd + ["--dangerously-skip-permissions", "-p", "Run node --version and report the output."]
    ))
    
    # 5. Python command execution via tool
    results.append(run_test(
        "TEST 5: Tool Execution - Python Version",
        base_cmd + ["--dangerously-skip-permissions", "-p", "Run python --version and report the output."]
    ))
    
    # 6. File Write tool execution
    test_file_path = "D:\\Claude code\\CLAUDE_RUNTIME_TEST.txt"
    if os.path.exists(test_file_path):
        os.remove(test_file_path)
        
    results.append(run_test(
        "TEST 6: Tool Execution - File Write",
        base_cmd + ["--dangerously-skip-permissions", "-p", f"Write the exact text 'CLAUDE_RUNTIME_OK' to file {test_file_path}"]
    ))
    
    # 7. File Read tool execution
    results.append(run_test(
        "TEST 7: Tool Execution - File Read",
        base_cmd + ["--dangerously-skip-permissions", "-p", f"Read file {test_file_path} and print its contents."]
    ))
    
    # 8. Concept / Slash command explanation
    results.append(run_test(
        "TEST 8: Knowledge / Slash Command Explanation",
        base_cmd + ["-p", "Explain what slash commands like /help or /init are in Claude Code in one sentence."]
    ))
    
    # 9. Multiline and Unicode Prompt Handling
    multiline_prompt = (
        "Here is a code snippet:\n"
        "```python\n"
        "# Process test\n"
        "def hello_world():\n"
        "    print('Hello world!')\n"
        "```\n"
        "Reply with a 5-word summary of what this code does."
    )
    results.append(run_test(
        "TEST 9: Multiline & Code Block Prompt",
        base_cmd + ["-p", multiline_prompt]
    ))
    
    # 10. Controlled Error Handling / Recovery
    results.append(run_test(
        "TEST 10: Controlled Command Recovery",
        base_cmd + ["--dangerously-skip-permissions", "-p", "Run a nonexistent command: __nonexistent_test_cmd_12345__ and report if it failed."]
    ))
    
    # Check that across all tests, zero banned errors occurred
    all_banned_errors = any(r["has_banned_error"] for r in results)
    all_passed = all(r["success"] for r in results)
    
    print("\n" + "="*60)
    print("ACCEPTANCE TEST SUITE SUMMARY")
    print("="*60)
    for i, r in enumerate(results, 1):
        status = "PASSED [OK]" if r["success"] else "FAILED [X]"
        print(f"{i}. {r['name']}: {status} (took {r['duration']:.2f}s)")
        
    print("="*60)
    if all_passed and not all_banned_errors:
        print("ALL ACCEPTANCE TESTS PASSED SUCCESSFULLY!")
        print("No /bin/bash hook errors, all tools and model requests succeeded.")
    else:
        print("SOME TESTS ENCOUNTERED FAILURES - PLEASE REVIEW LOGS ABOVE.")

if __name__ == "__main__":
    main()
