"""Runs one solution against all tests of an assignment and builds the result."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import evaluator
from .models import ACCEPTED, SYSTEM_ERROR, CaseSpec, Grade, RunResult

SNIPPET_STDOUT = 2000
SNIPPET_STDERR = 1000
LOG_CAP = 64 * 1024


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def grade_submission(code: str, cases: list[CaseSpec], time_limit_ms: int, memory_limit_mb: int,
                     executor, max_parallel: int = 2) -> Grade:
    """Raises SandboxError when the sandbox itself fails (the caller retries)."""
    if not cases:
        return Grade(verdict=SYSTEM_ERROR, tests_passed=0, tests_total=0, time_ms=0, details=[], logs=[])

    def run_one(case: CaseSpec) -> tuple[CaseSpec, RunResult, str]:
        result = executor.run(code, case.stdin, time_limit_ms, memory_limit_mb)
        return case, result, evaluator.judge(result, case.expected_stdout, time_limit_ms)

    workers = max(1, min(max_parallel, len(cases)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sandbox") as pool:
        outcomes = list(pool.map(run_one, cases))          # keeps the order of `cases`

    details: list[dict[str, Any]] = []
    logs: list[dict[str, Any]] = []
    for case, run, verdict in outcomes:
        item: dict[str, Any] = {"index": case.index, "verdict": verdict,
                                "time_ms": run.duration_ms, "hidden": case.is_hidden}
        if not case.is_hidden:           # hidden tests leak nothing except the verdict
            item.update(input=_cut(case.stdin, 1000), expected=_cut(case.expected_stdout, SNIPPET_STDOUT),
                        stdout=_cut(run.stdout, SNIPPET_STDOUT), stderr=_cut(run.stderr, SNIPPET_STDERR))
        details.append(item)
        logs.append({"test_index": case.index, "verdict": verdict, "exit_code": run.exit_code,
                     "time_ms": run.duration_ms, "oom": run.oom, "timed_out": run.timed_out,
                     "stdout": _cut(run.stdout, LOG_CAP), "stderr": _cut(run.stderr, LOG_CAP)})

    passed = sum(1 for _, _, v in outcomes if v == ACCEPTED)
    overall = next((v for _, _, v in outcomes if v != ACCEPTED), ACCEPTED)
    return Grade(verdict=overall, tests_passed=passed, tests_total=len(outcomes),
                 time_ms=max(run.duration_ms for _, run, _ in outcomes), details=details, logs=logs)
