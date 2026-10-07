"""Plain data holders. Nothing here touches the network, Docker or a database."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# verdicts (FR-5)
ACCEPTED = "accepted"
WRONG_ANSWER = "wrong_answer"
RUNTIME_ERROR = "runtime_error"
TIME_LIMIT = "time_limit_exceeded"
MEMORY_LIMIT = "memory_limit_exceeded"
SYSTEM_ERROR = "system_error"


@dataclass
class RunResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    timed_out: bool = False
    oom: bool = False
    output_truncated: bool = False


@dataclass(frozen=True)
class CaseSpec:
    """One test case of an assignment."""
    index: int
    stdin: str
    expected_stdout: str
    is_hidden: bool = False


@dataclass
class AssignmentTests:
    assignment_id: str
    tests_version: int
    time_limit_ms: int
    memory_limit_mb: int
    cases: list[CaseSpec] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "assignment_id": self.assignment_id,
            "tests_version": self.tests_version,
            "time_limit_ms": self.time_limit_ms,
            "memory_limit_mb": self.memory_limit_mb,
            "tests": [
                {"index": c.index, "stdin": c.stdin, "expected_stdout": c.expected_stdout,
                 "is_hidden": c.is_hidden}
                for c in self.cases
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AssignmentTests":
        cases = [
            CaseSpec(index=int(t["index"]), stdin=t.get("stdin", ""),
                     expected_stdout=t.get("expected_stdout", ""),
                     is_hidden=bool(t.get("is_hidden", False)))
            for t in data.get("tests", [])
        ]
        cases.sort(key=lambda c: c.index)
        return cls(
            assignment_id=str(data["assignment_id"] if "assignment_id" in data else data["id"]),
            tests_version=int(data["tests_version"]),
            time_limit_ms=int(data.get("time_limit_ms", 2000)),
            memory_limit_mb=int(data.get("memory_limit_mb", 128)),
            cases=cases,
        )


@dataclass
class Grade:
    verdict: str
    tests_passed: int
    tests_total: int
    time_ms: int
    details: list[dict[str, Any]]   # goes into submission.graded (hidden tests: verdict only)
    logs: list[dict[str, Any]]      # goes into MongoDB run_logs (full, truncated)
