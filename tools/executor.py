import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

# Test file written to the repo root before each run, cleaned up after.
_TEST_FILES: dict[str, str] = {
    "pytest":    "test_solution.py",
    "unittest":  "test_solution.py",
    "jest":      "test_solution.test.js",
    "mocha":     "test_solution.test.js",
    "go test":   "solution_test.go",
    "cargo":     "tests/solution_test.rs",
}

_TEST_COMMANDS: dict[str, list[str]] = {
    "pytest":    ["pytest", "test_solution.py", "-v", "--tb=short"],
    "unittest":  ["python", "-m", "unittest", "test_solution", "-v"],
    "jest":      ["npx", "jest", "test_solution", "--no-coverage"],
    "mocha":     ["npx", "mocha", "test_solution.test.js"],
    "go test":   ["go", "test", "./..."],
    "cargo":     ["cargo", "test"],
}

_TIMEOUT = 120


class TestRunner:
    """Runs tests directly in the local repository using the project's detected test runner.

    The coder writes solution files in-place to local_repo_path during code generation.
    This runner writes only the generated test file to the repo root, executes the test
    command in that directory, then removes the test file.

    Timeout policy:
      120 s hard timeout for all test runners. TimeoutExpired is caught and
      returned as a failure result — not retried, as it indicates the solution
      itself is causing a hang.

    Not retried:
      Any non-zero exit from the test runner — test failure, not infrastructure failure.
      FileNotFoundError (test runner binary not on PATH) — raises immediately with
      a clear message so the caller can surface it as a configuration error.
    """

    def run_tests(
        self, test_code: str, test_runner: str, local_repo_path: str
    ) -> dict:
        """Writes the test file to the repo and runs the test command in-place.

        Args:
            test_code: Unit test source code produced by the coder.
            test_runner: Test runner key from project skills (e.g. 'pytest', 'jest').
            local_repo_path: Absolute path to the local repository clone.

        Returns:
            Dict with 'success' bool and 'output' str containing test results.
        """
        cmd = _TEST_COMMANDS.get(test_runner, _TEST_COMMANDS["pytest"])
        test_file_name = _TEST_FILES.get(test_runner, "test_solution.py")

        repo = Path(local_repo_path)
        test_file = repo / test_file_name
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text(test_code)

        log.info(
            "Running tests directly in repo (runner=%s, file=%s)...",
            test_runner, test_file_name,
        )
        try:
            result = subprocess.run(
                cmd,
                cwd=local_repo_path,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT,
            )

            if result.returncode == 0:
                log.info("Tests passed.")
                return {"success": True, "output": result.stdout.strip()}

            output = result.stderr.strip() or result.stdout.strip()
            log.warning("Tests failed:\n%s", output)
            return {"success": False, "output": output}

        except subprocess.TimeoutExpired:
            log.error("Test execution timed out after %d seconds.", _TIMEOUT)
            return {"success": False, "output": f"Execution timed out after {_TIMEOUT} seconds."}

        except FileNotFoundError:
            raise RuntimeError(
                f"Test runner '{cmd[0]}' is not installed or not on PATH. "
                f"Install it and ensure it is accessible in the environment."
            )

        finally:
            try:
                test_file.unlink(missing_ok=True)
            except OSError:
                pass


# Module-level singleton and backward-compat wrapper
_test_runner = TestRunner()


def execute_tests(test_code: str, test_runner: str, local_repo_path: str) -> dict:
    return _test_runner.run_tests(test_code, test_runner, local_repo_path)
