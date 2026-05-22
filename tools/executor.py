import logging
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

DOCKER_IMAGE = "story-pr-runner"

_TEST_COMMANDS: dict[str, list[str]] = {
    "pytest":    ["pytest", "test_solution.py", "-v", "--tb=short"],
    "unittest":  ["python", "-m", "unittest", "test_solution", "-v"],
    "jest":      ["npx", "jest", "test_solution", "--no-coverage"],
    "mocha":     ["npx", "mocha", "test_solution.js"],
    "go test":   ["go", "test", "./..."],
    "cargo":     ["cargo", "test"],
}


class DockerClient:
    """Wraps Docker CLI for sandboxed test execution.

    Retry policy — run_tests is NOT retried by default:
      Test failures (exit code != 0) are expected workflow outcomes, not errors.
      TimeoutExpired is caught and returned as a failure result.

    The one retried scenario is exit code 125:
      125 — Docker daemon error: container failed to start due to a transient daemon
            fault, not a test failure. Retried once after a brief wait.

    Not retried:
      Any non-zero exit from the test runner — test failure, not infrastructure failure.
      FileNotFoundError (Docker not installed) — raises immediately.

    Build the runner image once with:
        docker build -t story-pr-runner -f Dockerfile.runner .
    """

    _DAEMON_ERROR_CODE = 125
    _MAX_DAEMON_RETRIES = 1

    def run_tests(
        self, solution_code: str, test_code: str, test_runner: str = "pytest"
    ) -> dict:
        """Writes solution + test files to a temp dir and runs the test command
        inside a Docker container with no network access and capped resources.

        Args:
            solution_code: Solution code to be tested.
            test_code: Unit test code targeting the solution.
            test_runner: Test runner key from project skills (e.g. 'pytest', 'jest').
                         Defaults to 'pytest' when not detected from skills.

        Returns:
            Dict with 'success' bool and 'output' str containing test results.
        """
        cmd = _TEST_COMMANDS.get(test_runner, _TEST_COMMANDS["pytest"])

        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            (tmppath / "solution.py").write_text(solution_code)
            (tmppath / "test_solution.py").write_text(test_code)

            return self._run_docker(tmpdir, cmd, test_runner, retries_left=self._MAX_DAEMON_RETRIES)

    def _run_docker(
        self, tmpdir: str, cmd: list[str], test_runner: str, retries_left: int
    ) -> dict:
        log.info(
            "Running tests inside Docker container (image=%s, runner=%s)...",
            DOCKER_IMAGE, test_runner,
        )
        try:
            result = subprocess.run(
                [
                    "docker", "run", "--rm",
                    "--network", "none",
                    "--memory", "128m",
                    "--cpus", "0.5",
                    "-v", f"{tmpdir}:/code",
                    DOCKER_IMAGE,
                    *cmd,
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )

            if result.returncode == 0:
                log.info("Tests passed.")
                return {"success": True, "output": result.stdout.strip()}

            if result.returncode == self._DAEMON_ERROR_CODE and retries_left > 0:
                # 125 — Docker daemon error (transient); retry once
                log.warning(
                    "Docker daemon error (exit 125) — retrying (%d left)...", retries_left
                )
                return self._run_docker(tmpdir, cmd, test_runner, retries_left - 1)

            output = result.stderr.strip() or result.stdout.strip()
            log.warning("Tests failed:\n%s", output)
            return {"success": False, "output": output}

        except subprocess.TimeoutExpired:
            log.error("Test execution timed out after 60 seconds.")
            return {"success": False, "output": "Execution timed out after 60 seconds."}

        except FileNotFoundError:
            raise RuntimeError(
                "Docker is not installed or not on PATH. "
                "Install Docker and build the runner image:\n"
                "  docker build -t story-pr-runner -f Dockerfile.runner ."
            )


# Module-level singleton and backward-compat wrapper
_docker_client = DockerClient()


def execute_tests(solution_code: str, test_code: str, test_runner: str = "pytest") -> dict:
    return _docker_client.run_tests(solution_code, test_code, test_runner)
