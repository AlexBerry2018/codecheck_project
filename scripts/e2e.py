#!/usr/bin/env python3
"""End-to-end check of a running stack (`docker compose up --build`), standard library only.

    python scripts/e2e.py                       # against http://localhost:8000 (Kong)
    python scripts/e2e.py --base http://host:8000 --rate-limit

It walks through the acceptance scenarios of the design doc: UC-01..UC-06, FR-9 (idempotency),
FR-8 (regrade) and, with --rate-limit, NFR-9. Exit code 0 = everything passed.
Note: Kong allows only 5 auth calls per minute per IP, so the script waits when it gets a 429.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

OK, FAIL = "\033[32m✓\033[0m", "\033[31m✗\033[0m"


class Failure(Exception):
    pass


class Client:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

    def call(self, method: str, path: str, *, token: str | None = None, json_body=None, form=None,
             headers: dict | None = None, retry_429: bool = False):
        """Returns (status, parsed JSON or None, response headers)."""
        for attempt in range(4):
            data, hdrs = None, dict(headers or {})
            if json_body is not None:
                data, hdrs["Content-Type"] = json.dumps(json_body).encode(), "application/json"
            elif form is not None:
                data, hdrs["Content-Type"] = urllib.parse.urlencode(form).encode(), "application/x-www-form-urlencoded"
            if token:
                hdrs["Authorization"] = "Bearer " + token
            req = urllib.request.Request(self.base + path, data=data, headers=hdrs, method=method)
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    status, raw, out = resp.status, resp.read(), resp.headers
            except urllib.error.HTTPError as err:
                status, raw, out = err.code, err.read(), err.headers
            if status == 429 and retry_429 and attempt < 3:
                wait = min(int(out.get("Retry-After", "20")) + 1, 65)
                print(f"   (rate limited on {path}, waiting {wait}s)")
                time.sleep(wait)
                continue
            try:
                body = json.loads(raw) if raw else None
            except ValueError:
                body = None
            return status, body, out
        raise Failure("unreachable")


def check(cond: bool, label: str, detail: object = "") -> None:
    print(f" {OK if cond else FAIL} {label}")
    if not cond:
        raise Failure(f"{label}: {detail}")


def wait_verdict(c: Client, token: str, sid: int, timeout_s: int):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        status, body, _ = c.call("GET", f"/api/submissions/{sid}", token=token)
        if status == 200 and body["status"] != "queued":
            return body
        time.sleep(1.5)
    raise Failure(f"submission {sid} was not graded within {timeout_s}s "
                  "(first start: the grader pulls the sandbox image, see `docker compose logs grader-service`)")


def run(args: argparse.Namespace) -> None:
    c = Client(args.base)
    suffix = uuid.uuid4().hex[:8]
    teacher_mail, student_mail, password = f"teacher-{suffix}@example.com", f"student-{suffix}@example.com", "password123"

    print("UC-01 registration and login")
    st, body, _ = c.call("POST", "/api/auth/register", retry_429=True,
                         json_body={"email": teacher_mail, "password": password, "role": "teacher",
                                    "invite_code": args.invite_code})
    check(st == 201, "teacher registers with the invite code", (st, body))
    st, body, _ = c.call("POST", "/api/auth/register", retry_429=True,
                         json_body={"email": student_mail, "password": password})
    check(st == 201, "student registers", (st, body))
    st, body, _ = c.call("POST", "/api/auth/login", retry_429=True, json_body={"email": teacher_mail, "password": password})
    check(st == 200 and body["access_token"], "teacher logs in", (st, body))
    teacher = body["access_token"]
    st, body, _ = c.call("POST", "/api/auth/login", retry_429=True, json_body={"email": student_mail, "password": password})
    check(st == 200, "student logs in", (st, body))
    student = body["access_token"]
    st, _, _ = c.call("GET", "/api/courses")
    check(st == 401, "Kong rejects a request without a token")

    print("UC-02 teacher creates a course and an assignment")
    st, course, _ = c.call("POST", "/api/courses", token=teacher, json_body={"title": f"Python {suffix}"})
    check(st == 201, "course created", (st, course))
    tests = [{"stdin": "1 2\n", "expected_stdout": "3"},
             {"stdin": "10 20\n", "expected_stdout": "30"},
             {"stdin": "-5 5\n", "expected_stdout": "0", "is_hidden": True}]
    st, assignment, _ = c.call("POST", f"/api/courses/{course['id']}/assignments", token=teacher,
                               json_body={"title": "Sum", "description": "a + b", "tests": tests,
                                          "time_limit_ms": 1000, "memory_limit_mb": 64})
    aid = assignment["id"]
    check(st == 201 and assignment["tests_version"] == 1, "assignment created (tests_version 1)", (st, assignment))
    st, _, _ = c.call("POST", f"/api/courses/{course['id']}/assignments", token=student, json_body={"title": "x", "tests": tests})
    check(st == 403, "a student cannot create assignments")
    st, _, _ = c.call("POST", f"/api/courses/{course['id']}/enroll", token=student)
    check(st in (200, 201), "student enrolls")
    st, seen, _ = c.call("GET", f"/api/assignments/{aid}", token=student)
    check(st == 200 and len(seen["tests"]) == 2 and seen["hidden_tests"] == 1, "student sees public tests only", seen)

    def submit(code: str, key: str | None = None):
        return c.call("POST", "/api/submissions", token=student, form={"assignment_id": aid, "code": code},
                      headers={"Idempotency-Key": key} if key else None)

    print("UC-03 / UC-04 submit and read the verdict (the grader runs every test in a sandbox)")
    st, sub, hdrs = submit("a, b = map(int, input().split())\nprint(a + b)\n", key=f"k-{suffix}")
    check(st == 202 and sub["status"] == "queued", "correct solution accepted for checking (202, queued)", (st, sub))
    good = wait_verdict(c, student, sub["id"], args.timeout)
    check(good["verdict"] == "accepted" and good["tests_passed"] == 3, "verdict accepted, 3/3 tests", good)
    hidden = [t for t in good["tests"] if t["hidden"]]
    check(len(hidden) == 1 and "stdout" not in hidden[0], "hidden test reveals only its verdict", hidden)

    print("FR-9 idempotency")
    st, again, hdrs = submit("a, b = map(int, input().split())\nprint(a + b)\n", key=f"k-{suffix}")
    check(st == 202 and again["id"] == sub["id"] and hdrs.get("Idempotent-Replay") == "true",
          "the same Idempotency-Key returns the same submission", (st, again))

    print("FR-5 other verdicts")
    cases = {"wrong_answer": "a, b = map(int, input().split())\nprint(a - b)\n",
             "runtime_error": "print(1 / 0)\n",
             "time_limit_exceeded": "while True:\n    pass\n"}
    for expected, code in cases.items():
        st, s, _ = submit(code)
        check(st == 202, f"{expected}: accepted for checking", (st, s))
        verdict = wait_verdict(c, student, s["id"], args.timeout)
        check(verdict["verdict"] == expected, f"verdict is {expected}", verdict)

    print("UC-06 leaderboard")
    st, board, _ = c.call("GET", f"/api/leaderboard/{aid}", token=student)
    check(st == 200 and board["me"] and board["me"]["score"] == 100, "best attempt (100) is on the board", board)

    print("UC-05 regrade after the tests changed")
    new_tests = tests + [{"stdin": "7 8\n", "expected_stdout": "15"}]
    st, updated, _ = c.call("PUT", f"/api/assignments/{aid}", token=teacher, json_body={"tests": new_tests})
    check(st == 200 and updated["tests_version"] == 2, "teacher updates the tests (tests_version 2)", (st, updated))
    st, res, _ = c.call("POST", f"/api/submissions/assignments/{aid}/regrade", token=teacher)
    check(st == 200 and res["queued"] >= 1 and res["tests_version"] == 2, "regrade queued", (st, res))
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        _, now, _ = c.call("GET", f"/api/submissions/{sub['id']}", token=student)
        if now.get("tests_version") == 2 and now["status"] != "queued":
            break
        time.sleep(1.5)
    check(now.get("tests_total") == 4 and now["verdict"] == "accepted", "old solution re-checked: 4/4 tests", now)

    if args.rate_limit:
        print("NFR-9 rate limit")
        codes = [submit(f"print({i})\n")[0] for i in range(14)]
        check(429 in codes, "too many submissions end with HTTP 429", codes)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default=os.environ.get("BASE_URL", "http://localhost:8000"))
    parser.add_argument("--invite-code", default=os.environ.get("TEACHER_INVITE_CODE", "teacher-invite"))
    parser.add_argument("--timeout", type=int, default=120, help="seconds to wait for a verdict")
    parser.add_argument("--rate-limit", action="store_true", help="also check HTTP 429 (uses up the submit quota)")
    try:
        run(parser.parse_args())
    except Failure as exc:
        print(f"\n{FAIL} FAILED: {exc}")
        return 1
    except urllib.error.URLError as exc:
        print(f"\n{FAIL} cannot reach the API: {exc}. Is `docker compose up` running?")
        return 1
    print(f"\n{OK} all scenarios passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
