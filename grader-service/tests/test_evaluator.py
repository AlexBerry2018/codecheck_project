import unittest

from app import evaluator
from app.models import (ACCEPTED, MEMORY_LIMIT, RUNTIME_ERROR, TIME_LIMIT, WRONG_ANSWER, RunResult)


def run(stdout="", stderr="", exit_code=0, duration_ms=10, **kw):
    return RunResult(stdout=stdout, stderr=stderr, exit_code=exit_code, duration_ms=duration_ms, **kw)


class NormalizeTests(unittest.TestCase):
    def test_ignores_trailing_whitespace_and_blank_lines(self):
        self.assertEqual(evaluator.normalize("1 2  \r\n3\n\n\n"), "1 2\n3")

    def test_keeps_inner_blank_lines_and_leading_spaces(self):
        self.assertEqual(evaluator.normalize("a\n\n  b"), "a\n\n  b")

    def test_empty(self):
        self.assertEqual(evaluator.normalize(""), "")
        self.assertTrue(evaluator.outputs_match("\n\n", ""))

    def test_case_and_inner_spaces_matter(self):
        self.assertFalse(evaluator.outputs_match("Yes", "yes"))
        self.assertFalse(evaluator.outputs_match("1  2", "1 2"))


class JudgeTests(unittest.TestCase):
    def test_accepted(self):
        self.assertEqual(evaluator.judge(run("42\n"), "42", 1000), ACCEPTED)

    def test_wrong_answer(self):
        self.assertEqual(evaluator.judge(run("41\n"), "42", 1000), WRONG_ANSWER)

    def test_runtime_error(self):
        self.assertEqual(evaluator.judge(run("", "Traceback...", 1), "", 1000), RUNTIME_ERROR)

    def test_timeout_wins_over_exit_code(self):
        self.assertEqual(evaluator.judge(run(exit_code=-9, timed_out=True, duration_ms=1200), "", 1000), TIME_LIMIT)

    def test_slow_but_finished_is_time_limit(self):
        self.assertEqual(evaluator.judge(run("42", duration_ms=1100), "42", 1000), TIME_LIMIT)

    def test_oom_wins_over_everything(self):
        self.assertEqual(evaluator.judge(run(exit_code=137, oom=True, timed_out=True), "", 1000), MEMORY_LIMIT)

    def test_memory_error_in_traceback(self):
        self.assertEqual(evaluator.judge(run(stderr="...\nMemoryError\n", exit_code=1), "", 1000), MEMORY_LIMIT)

    def test_output_flood_is_runtime_error(self):
        self.assertEqual(evaluator.judge(run("x" * 10, output_truncated=True), "x" * 10, 1000), RUNTIME_ERROR)


if __name__ == "__main__":
    unittest.main()
