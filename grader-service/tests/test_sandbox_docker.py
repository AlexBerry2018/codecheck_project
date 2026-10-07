"""DockerExecutor against a fake `docker` binary (tests/fake_docker.py)."""
import os
import shutil
import stat
import tempfile
import unittest

from app import evaluator
from app.models import ACCEPTED, MEMORY_LIMIT, RUNTIME_ERROR, TIME_LIMIT
from app.sandbox import BOOTSTRAP, DockerExecutor, SandboxError

FAKE = os.path.join(os.path.dirname(__file__), "fake_docker.py")


class DockerExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.chmod(FAKE, os.stat(FAKE).st_mode | stat.S_IXUSR)
        os.environ["FAKE_DOCKER_STATE"] = self.tmp
        os.environ["FAKE_DOCKER_MODE"] = "ok"
        self.ex = DockerExecutor("python:3.12-alpine", docker_bin=FAKE)

    def tearDown(self):
        os.environ.pop("FAKE_DOCKER_MODE", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def calls(self):
        with open(os.path.join(self.tmp, "calls.log")) as fh:
            return fh.read().splitlines()

    def test_build_args_has_every_restriction(self):
        args = self.ex.build_args("cc-run-x", 128)
        joined = " ".join(args)
        for needle in ("--network none", "--memory 128m", "--memory-swap 128m", "--cpus 1",
                       "--pids-limit 64", "--read-only", "--cap-drop ALL",
                       "--security-opt no-new-privileges", "--user 65534:65534", "-e CC_CODE"):
            self.assertIn(needle, joined)
        self.assertEqual(args[-5:], ["python:3.12-alpine", "python", "-I", "-c", BOOTSTRAP][-5:])
        # the solution must never appear on the command line
        self.assertNotIn("print(", joined)

    def test_run_accepted_and_container_removed(self):
        res = self.ex.run("print(int(input()) * 2)", "21\n", 2000, 64)
        self.assertEqual(evaluator.judge(res, "42", 2000), ACCEPTED)
        calls = self.calls()
        self.assertTrue(any(c.startswith("rm -f cc-run-") for c in calls))
        self.assertEqual(os.listdir(self.tmp).count("calls.log"), 1)
        self.assertEqual([f for f in os.listdir(self.tmp) if f.endswith(".state")], [])

    def test_runtime_error(self):
        res = self.ex.run("1/0", "", 2000, 64)
        self.assertEqual(evaluator.judge(res, "", 2000), RUNTIME_ERROR)

    def test_timeout_kills_the_container(self):
        res = self.ex.run("import time; time.sleep(4)", "", 300, 64)
        self.assertEqual(evaluator.judge(res, "", 300), TIME_LIMIT)
        self.assertTrue(any(c.startswith("kill cc-run-") for c in self.calls()))

    def test_oom_flag_from_inspect(self):
        os.environ["FAKE_DOCKER_MODE"] = "oom"
        res = self.ex.run("print(1)", "", 2000, 64)
        self.assertEqual(evaluator.judge(res, "1", 2000), MEMORY_LIMIT)

    def test_docker_down_is_an_infrastructure_error(self):
        os.environ["FAKE_DOCKER_MODE"] = "down"
        with self.assertRaises(SandboxError):
            self.ex.run("print(1)", "", 2000, 64)

    def test_missing_docker_binary_is_an_infrastructure_error(self):
        ex = DockerExecutor("python:3.12-alpine", docker_bin="/nonexistent/docker")
        with self.assertRaises(SandboxError):
            ex.run("print(1)", "", 2000, 64)

    def test_calibrate_sets_baseline_and_ready(self):
        self.assertFalse(self.ex.ready)
        self.ex.calibrate()
        self.assertTrue(self.ex.ready)
        self.assertGreaterEqual(self.ex.baseline_ms, 0)

    def test_calibrate_uses_local_image_when_registry_is_unreachable(self):
        os.environ["FAKE_DOCKER_MODE"] = "offline"
        self.ex.calibrate()
        self.assertTrue(self.ex.ready)

    def test_calibrate_fails_when_pull_fails_and_image_is_missing(self):
        os.environ["FAKE_DOCKER_MODE"] = "noimage"
        with self.assertRaises(SandboxError) as ctx:
            self.ex.calibrate()
        self.assertIn("not on this host", str(ctx.exception))
        self.assertFalse(self.ex.ready)

    def test_calibrate_fails_when_docker_is_down(self):
        os.environ["FAKE_DOCKER_MODE"] = "down"
        with self.assertRaises(SandboxError):
            self.ex.calibrate()
        self.assertFalse(self.ex.ready)


if __name__ == "__main__":
    unittest.main()
