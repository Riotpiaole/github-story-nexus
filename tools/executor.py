import subprocess
import sys

def execute_python_tests(code_string: str) -> dict:
    """Writes code to a temporary file and runs a test assertion via subprocess."""
    print("-> [System Call] Executing code in an isolated subprocess...")
    
    # Create a complete test script combining the code and a unit test assertion
    test_script = f"""
{code_string}

# Simple test harness
try:
    assert is_palindrome("racecar") == True, "Failed racecar test"
    assert is_palindrome("hello") == False, "Failed hello test"
    print("ALL TESTS PASSED")
except AssertionError as e:
    print(f"TEST FAILED: {{e}}")
    exit(1)
except Exception as e:
    print(f"RUNTIME ERROR: {{e}}")
    exit(2)
"""
    
    try:
        # Run code string securely outside the main agent process
        result = subprocess.run(
            [sys.executable, "-c", test_script],
            capture_output=True,
            text=True,
            timeout=5
        )
        
        if result.returncode == 0:
            return {"success": True, "output": result.stdout.strip()}
        else:
            # Captures both stderr and stdout from the failed runtime
            error_msg = result.stderr.strip() or result.stdout.strip()
            return {"success": False, "output": error_msg}
            
    except subprocess.TimeoutExpired:
        return {"success": False, "output": "Execution timed out after 5 seconds."}
