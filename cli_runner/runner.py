import concurrent.futures
import time
from dataclasses import dataclass


@dataclass
class CheckResult:
    name: str
    description: str
    passed: bool
    message: str
    duration_ms: float


def _run_one(check, verbose: bool) -> CheckResult:
    start = time.monotonic()
    try:
        passed, message = check.run(verbose)
    except Exception as exc:
        passed = False
        message = f"{type(exc).__name__}: {exc}"
    duration_ms = (time.monotonic() - start) * 1000
    return CheckResult(
        name=check.NAME,
        description=check.DESCRIPTION,
        passed=passed,
        message=message,
        duration_ms=duration_ms,
    )


def run_checks(checks: list, verbose: bool = False) -> list[CheckResult]:
    """Runs all checks in parallel and returns results sorted by name."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(len(checks), 1)) as pool:
        futures = [pool.submit(_run_one, c, verbose) for c in checks]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]
    return sorted(results, key=lambda r: r.name)


def print_results(results: list[CheckResult], verbose: bool = False) -> None:
    print()
    for r in results:
        icon = "\033[32m✓\033[0m" if r.passed else "\033[31m✗\033[0m"
        label = "\033[32mPASS\033[0m" if r.passed else "\033[31mFAIL\033[0m"
        print(f"  {icon}  {r.name:<12}  {label}  ({r.duration_ms:>5.0f} ms)  {r.description}")
        if r.message and (not r.passed or verbose):
            for line in r.message.splitlines():
                print(f"               {line}")
    print()
    passed = sum(1 for r in results if r.passed)
    total = len(results)
    color = "\033[32m" if passed == total else "\033[31m"
    print(f"  {color}{passed}/{total} checks passed.\033[0m\n")
