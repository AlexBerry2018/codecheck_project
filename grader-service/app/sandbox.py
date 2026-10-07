"""Sandboxes that run untrusted student code.

DockerExecutor  - production mode: one throw-away container per run, no network, memory,
                  CPU and PID limits, read-only filesystem, non-root user, all capabilities
                  dropped. Needs the docker CLI and access to a Docker daemon.
LocalExecutor   - development and unit tests only: a plain child process with ulimit.
                  It is NOT a security boundary.

Both return a RunResult. `SandboxError` means "the infrastructure failed" (Docker is down,
the image is missing): the grader retries such a message, it is not the student's fault.
"""
from __future__ import annotations

import logging
import math
import os
import subprocess
import sys
import threading
import time
import uuid
from typing import Callable, Optional

from .models import RunResult

log = logging.getLogger("grader.sandbox")

OUTPUT_CAP = 64 * 1024     # bytes kept per stream; more output kills the run
SLACK_MS = 300             # a run may finish this much after the limit and still be measured

# The student's code travels in an environment variable (never on a command line) and is
# executed in the module namespace, so `if __name__ == "__main__"` works as usual.
BOOTSTRAP = "import os;exec(compile(os.environ['CC_CODE'],'solution.py','exec'))"


class SandboxError(Exception):
    """Infrastructure failure, the run says nothing about the solution. Retry it."""


def _run_capped(args: list[str], stdin_bytes: bytes, timeout_s: float, env: dict[str, str],
                cap: int = OUTPUT_CAP, on_kill: Optional[Callable[[], None]] = None):
    """Run a process with a wall-clock timeout and a cap on captured output.

    Returns (returncode, stdout, stderr, timed_out, output_truncated, wall_ms).
    Output beyond `cap` bytes kills the process, so a print-forever loop cannot eat the
    memory of the grader.
    """
    start = time.monotonic()
    proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env)
    bufs = {"out": bytearray(), "err": bytearray()}
    flags = {"over": False}
    kill_lock = threading.Lock()

    def kill() -> None:
        with kill_lock:
            try:
                proc.kill()
            except OSError:
                pass
            if on_kill is not None:
                try:
                    on_kill()
                except Exception:                      # noqa: BLE001 - best effort
                    log.exception("kill callback failed")

    def pump(stream, key: str) -> None:
        buf = bufs[key]
        fd = stream.fileno()
        try:
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    return
                room = cap - len(buf)
                if room > 0:
                    buf.extend(chunk[:room])
                if len(chunk) > room:
                    if not flags["over"]:
                        flags["over"] = True
                        kill()
        except OSError:
            return

    def feed() -> None:
        try:
            if stdin_bytes:
                proc.stdin.write(stdin_bytes)
        except OSError:
            pass                                       # the program closed stdin early: fine
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    threads = [
        threading.Thread(target=pump, args=(proc.stdout, "out"), daemon=True),
        threading.Thread(target=pump, args=(proc.stderr, "err"), daemon=True),
        threading.Thread(target=feed, daemon=True),
    ]
    for t in threads:
        t.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill()
        proc.wait()
    wall_ms = int((time.monotonic() - start) * 1000)
    for t in threads:
        t.join(timeout=5)
    for stream in (proc.stdout, proc.stderr):
        try:
            stream.close()
        except OSError:
            pass
    return proc.returncode, bytes(bufs["out"]), bytes(bufs["err"]), timed_out, flags["over"], wall_ms


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


class LocalExecutor:
    """Dev/test sandbox: `ulimit` for memory and CPU, nothing else. Never use it in production."""

    mode = "local"
    ready = True

    def run(self, code: str, stdin: str, time_limit_ms: int, memory_mb: int) -> RunResult:
        cpu_s = max(1, math.ceil(time_limit_ms / 1000) + 1)
        vm_kb = (memory_mb + 128) * 1024               # headroom for the interpreter itself
        script = f'ulimit -v {vm_kb}; ulimit -t {cpu_s}; exec "$@"'
        args = ["/bin/sh", "-c", script, "sh", sys.executable, "-I", "-c", BOOTSTRAP]
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "CC_CODE": code}
        rc, out, err, timed_out, over, wall_ms = _run_capped(
            args, stdin.encode("utf-8"), time_limit_ms / 1000 + SLACK_MS / 1000, env)
        stderr = _decode(err)
        if over:
            stderr += "\n[output limit exceeded]"
        return RunResult(stdout=_decode(out), stderr=stderr, exit_code=rc if rc is not None else -1,
                         duration_ms=wall_ms, timed_out=timed_out, oom=False, output_truncated=over)


class DockerExecutor:
    """Production sandbox: `docker run` with every restriction we can think of."""

    mode = "docker"

    def __init__(self, image: str, docker_bin: str = "docker") -> None:
        self.image = image
        self.docker_bin = docker_bin
        self.baseline_ms = 0           # cost of starting an empty container, subtracted from timings
        self.ready = False

    # ------------------------------------------------------------------ helpers
    def _docker(self, *args: str, timeout: float = 20) -> subprocess.CompletedProcess:
        return subprocess.run([self.docker_bin, *args], capture_output=True, text=True, timeout=timeout)

    def build_args(self, name: str, memory_mb: int) -> list[str]:
        return [
            self.docker_bin, "run", "-i", "--name", name,
            "--label", "codecheck.sandbox=1",
            "--network", "none",
            "--memory", f"{memory_mb}m", "--memory-swap", f"{memory_mb}m",
            "--cpus", "1", "--pids-limit", "64",
            "--ulimit", "nofile=256:256",
            "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--user", "65534:65534",
            "-e", "CC_CODE",           # value is taken from our environment, not from argv
            self.image, "python", "-I", "-c", BOOTSTRAP,
        ]

    def _inspect(self, name: str) -> tuple[int, bool]:
        proc = self._docker("inspect", "-f", "{{.State.ExitCode}} {{.State.OOMKilled}}", name)
        if proc.returncode != 0:
            raise SandboxError(f"container {name} did not run: {proc.stderr.strip()[:300]}")
        code, oom = proc.stdout.split()
        return int(code), oom.lower() == "true"

    # ------------------------------------------------------------------ lifecycle
    def calibrate(self) -> None:
        """Pull the image, drop containers left over from a crash and measure the cost of an
        empty container. Raises SandboxError when Docker is not usable."""
        try:
            leftovers = self._docker("ps", "-aq", "--filter", "label=codecheck.sandbox=1")
            if leftovers.returncode != 0:
                raise SandboxError(f"docker is not available: {leftovers.stderr.strip()[:300]}")
            ids = leftovers.stdout.split()
            if ids:
                self._docker("rm", "-f", *ids)
            pull = self._docker("pull", "-q", self.image, timeout=300)
            if pull.returncode != 0:
                # registry unreachable (offline, VPN, rate limit): an image that is already on the
                # host is good enough, so `docker pull python:3.12-alpine` once beforehand is a fix
                local = self._docker("image", "inspect", "-f", "{{.Id}}", self.image)
                if local.returncode != 0:
                    raise SandboxError(f"cannot pull {self.image} and it is not on this host: "
                                       f"{pull.stderr.strip()[:300]}")
                log.warning("cannot pull %s, using the copy that is already on this host: %s",
                            self.image, pull.stderr.strip()[:200])
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxError(f"docker is not usable: {exc}") from exc

        timings = []
        for _ in range(3):
            res = self.run_raw("pass", "", 5000, 64)
            timings.append(res.duration_ms + self.baseline_ms)   # baseline is still 0 here
        self.baseline_ms = min(timings)
        self.ready = True
        log.info("sandbox ready", extra={"ctx": {"image": self.image, "baseline_ms": self.baseline_ms}})

    # ------------------------------------------------------------------ run
    def run(self, code: str, stdin: str, time_limit_ms: int, memory_mb: int) -> RunResult:
        return self.run_raw(code, stdin, time_limit_ms, memory_mb)

    def run_raw(self, code: str, stdin: str, time_limit_ms: int, memory_mb: int) -> RunResult:
        name = "cc-run-" + uuid.uuid4().hex[:12]
        env = dict(os.environ)
        env["CC_CODE"] = code
        timeout_s = (time_limit_ms + self.baseline_ms + SLACK_MS) / 1000
        try:
            rc, out, err, timed_out, over, wall_ms = _run_capped(
                self.build_args(name, memory_mb), stdin.encode("utf-8"), timeout_s, env,
                on_kill=lambda: self._docker("kill", name, timeout=10))
            try:
                exit_code, oom = self._inspect(name)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise SandboxError(f"docker inspect failed: {exc}") from exc
        except OSError as exc:
            raise SandboxError(f"cannot start docker: {exc}") from exc
        finally:
            try:
                self._docker("rm", "-f", name, timeout=15)
            except (OSError, subprocess.TimeoutExpired):
                log.warning("could not remove sandbox container", extra={"ctx": {"container": name}})
        stderr = _decode(err)
        if over:
            stderr += "\n[output limit exceeded]"
        return RunResult(stdout=_decode(out), stderr=stderr, exit_code=exit_code,
                         duration_ms=max(0, wall_ms - self.baseline_ms),
                         timed_out=timed_out, oom=oom, output_truncated=over)


def make_executor(mode: str, image: str):
    if mode == "docker":
        return DockerExecutor(image)
    if mode == "local":
        log.warning("SANDBOX_MODE=local: student code runs WITHOUT isolation, development only")
        return LocalExecutor()
    raise ValueError(f"unknown SANDBOX_MODE: {mode!r}")
