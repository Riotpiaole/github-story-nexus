import logging
import subprocess

log = logging.getLogger(__name__)


class NPXClient:
    """Wraps npx-based skill discovery CLI calls.

    Retry policy — limited; NPX failures are usually permanent (tool not found,
    registry unreachable):
      TimeoutExpired (60 s) — npx registry network slowness; retried once.

    Not retried (non-transient):
      FileNotFoundError — npx not installed; raises RuntimeError immediately.
      Non-zero exit code from skills CLI — bad query or registry rejection.

    Exit code note: npx does not emit standard HTTP status codes.
    Network-related timeouts (OSError / socket errors inside npx) surface as
    TimeoutExpired from subprocess, which is the only retried condition.
    """

    _TIMEOUT = 60
    _MAX_TIMEOUT_RETRIES = 1

    def find_skills(self, query: str) -> dict:
        """Runs `npx skills find <query>` and returns parsed {skill_name: source_url}.

        Returns an empty dict on failure rather than raising, so the pipeline
        can continue without skills when the registry is unavailable.
        """
        try:
            result = self._run(["npx", "--yes", "skills", "find", query])
        except subprocess.TimeoutExpired:
            log.warning("skills find timed out — retrying once...")
            try:
                result = self._run(["npx", "--yes", "skills", "find", query])
            except subprocess.TimeoutExpired:
                log.error("skills find timed out on retry — skipping skill discovery.")
                return {}

        if result.returncode != 0:
            log.warning("skills find failed: %s", result.stderr.strip())
            return {}

        return self._parse_output(result.stdout)

    def add_skill(self, source: str, name: str) -> None:
        """Runs `npx skills add <source> --skill <name>`."""
        try:
            result = self._run(["npx", "--yes", "skills", "add", source, "--skill", name])
        except subprocess.TimeoutExpired:
            log.warning("skills add timed out for skill '%s' — skipping.", name)
            return

        if result.returncode == 0:
            log.info("Installed skill '%s' from %s", name, source)
        else:
            log.warning("Failed to install skill '%s': %s", name, result.stderr.strip())

    def _run(self, cmd: list[str]) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                cmd, capture_output=True, text=True, timeout=self._TIMEOUT
            )
        except FileNotFoundError:
            raise RuntimeError("npx not found — install Node.js to enable skill discovery.")

    @staticmethod
    def _parse_output(output: str) -> dict:
        """Parses `npx skills find` stdout into {skill_name: source_url}."""
        import re
        pattern = re.compile(r'^(\w+):\s*["\']?([^"\'#\n]+?)["\']?\s*$')
        skills: dict = {}
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            m = pattern.match(line)
            if m:
                skills[m.group(1)] = m.group(2).strip()
        return skills
