"""Runs real child processes through LocalExecutor (no Docker needed)."""
import unittest

from app import evaluator
from app.models import ACCEPTED, MEMORY_LIMIT, RUNTIME_ERROR, TIME_LIMIT, WRONG_ANSWER
from app.sandbox import LocalExecutor

EX = LocalExecutor()


def verdict(code, stdin="", expected="", time_ms=1000, mem_mb=64):
    result = EX.run(code, stdin, time_ms, mem_mb)
    return evaluator.judge(result, expected, time_ms), result


class LocalExecutorTests(unittest.TestCase):
    def test_reads_stdin_and_prints(self):
        v, r = verdict("a, b = map(int, input().split())\nprint(a + b)", "2 3\n", "5")
        self.assertEqual(v, ACCEPTED, r)

    def test_name_main_guard_works(self):
        code = "def main():\n    print('ok')\nif __name__ == '__main__':\n    main()\n"
        self.assertEqual(verdict(code, "", "ok")[0], ACCEPTED)

    def test_wrong_answer(self):
        self.assertEqual(verdict("print(1)", "", "2")[0], WRONG_ANSWER)

    def test_exception_is_runtime_error(self):
        v, r = verdict("raise ValueError('boom')")
        self.assertEqual(v, RUNTIME_ERROR)
        self.assertIn("ValueError", r.stderr)

    def test_syntax_error_is_runtime_error(self):
        self.assertEqual(verdict("print(")[0], RUNTIME_ERROR)

    def test_sys_exit_nonzero_is_runtime_error(self):
        self.assertEqual(verdict("import sys; sys.exit(3)")[0], RUNTIME_ERROR)

    def test_infinite_loop_is_time_limit(self):
        v, r = verdict("while True:\n    pass", time_ms=500)
        self.assertEqual(v, TIME_LIMIT)
        self.assertTrue(r.timed_out or r.duration_ms > 500)

    def test_sleep_is_time_limit(self):
        self.assertEqual(verdict("import time; time.sleep(5)", time_ms=400)[0], TIME_LIMIT)

    def test_memory_hog_is_memory_limit(self):
        v, r = verdict("x = bytearray(900 * 1024 * 1024)\nprint(len(x))", mem_mb=32, time_ms=3000)
        self.assertEqual(v, MEMORY_LIMIT, r)

    def test_output_flood_is_cut_and_not_accepted(self):
        v, r = verdict("while True:\n    print('x' * 1000)", time_ms=3000)
        self.assertTrue(r.output_truncated)
        self.assertEqual(v, RUNTIME_ERROR)
        self.assertLessEqual(len(r.stdout), 64 * 1024)

    def test_program_that_ignores_stdin_does_not_hang(self):
        v, _ = verdict("print('hi')", stdin="x" * 200000, expected="hi", time_ms=2000)
        self.assertEqual(v, ACCEPTED)

    def test_utf8_roundtrip(self):
        self.assertEqual(verdict("print(input()[::-1])", "привет\n", "тевирп")[0], ACCEPTED)


if __name__ == "__main__":
    unittest.main()
