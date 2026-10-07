"""Turns the raw result of one sandbox run into a verdict."""
from __future__ import annotations

from .models import (ACCEPTED, MEMORY_LIMIT, RUNTIME_ERROR, TIME_LIMIT, WRONG_ANSWER, RunResult)


def normalize(text: str) -> str:
    """Compare outputs the way most judges do: ignore line-ending style, trailing spaces
    on every line and trailing empty lines. Everything else must match exactly."""
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def outputs_match(actual: str, expected: str) -> bool:
    return normalize(actual) == normalize(expected)


def judge(run: RunResult, expected_stdout: str, time_limit_ms: int) -> str:
    """Order matters: a killed-by-OOM process also has a non-zero exit code, and a program
    that was killed for being slow must not be reported as a runtime error."""
    if run.oom:
        return MEMORY_LIMIT
    if run.timed_out or run.duration_ms > time_limit_ms:
        return TIME_LIMIT
    if run.exit_code != 0:
        if "MemoryError" in run.stderr[-2000:]:
            return MEMORY_LIMIT
        return RUNTIME_ERROR
    if run.output_truncated:
        return RUNTIME_ERROR            # output flood: the run was cut, the answer is incomplete
    return ACCEPTED if outputs_match(run.stdout, expected_stdout) else WRONG_ANSWER
