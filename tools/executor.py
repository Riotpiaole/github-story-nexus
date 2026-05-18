import logging
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

DOCKER_IMAGE = "story-pr-runner"


def execute_python_tests(solution_code: str, test_code: str) -> dict:
    """
    Writes solution + test files to a temp dir and runs pytest inside a
    Docker container with no network access and capped resources.

    Build the runner image once with:
        docker build -t story-pr-runner -f Dockerfile.runner .
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        (tmppath / "solution.py").write_text(solution_code)
        (tmppath / "test_solution.py").write_text(test_code)

        log.info("Running tests inside Docker container (image=%s)...", DOCKER_IMAGE)
        try:
            result = subprocess.run(
                [
                    "docker", "run", "--rm",
                    "--network", "none",
                    "--memory", "128m",
                    "--cpus", "0.5",
                    "-v", f"{tmpdir}:/code",
                    DOCKER_IMAGE,
                    "pytest", "test_solution.py", "-v", "--tb=short",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )

            if result.returncode == 0:
                log.info("Tests passed.")
                return {"success": True, "output": result.stdout.strip()}
            else:
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
