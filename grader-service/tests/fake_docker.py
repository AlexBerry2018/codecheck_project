#!/usr/bin/env python3
"""A fake `docker` CLI for tests: understands just enough of `run`, `inspect`, `kill`, `rm`,
`ps` and `pull` to exercise DockerExecutor without a Docker daemon.

`run` really executes the student code (CC_CODE) with the local Python and records the
exit code in a state directory, like the daemon would. `docker kill NAME` records 137.
Behaviour switches through the FAKE_DOCKER_MODE environment variable:
  ok       - normal
  down     - every command fails like an unreachable daemon
  oom      - `inspect` reports OOMKilled=true, exit code 137
  offline  - `pull` fails (registry unreachable), the image is already on the host
  noimage  - `pull` fails and the image is not on the host
"""
import os
import subprocess
import sys

STATE = os.environ["FAKE_DOCKER_STATE"]
MODE = os.environ.get("FAKE_DOCKER_MODE", "ok")
LOG = os.path.join(STATE, "calls.log")


def log(line):
    with open(LOG, "a") as fh:
        fh.write(line + "\n")


def path(name):
    return os.path.join(STATE, name + ".state")


def main(argv):
    if MODE == "down":
        sys.stderr.write("Cannot connect to the Docker daemon\n")
        return 1
    cmd = argv[0]
    log(" ".join(argv))
    if cmd == "ps":
        return 0
    if cmd == "pull":
        if MODE in ("offline", "noimage"):
            sys.stderr.write("Error response from daemon: Get https://registry-1.docker.io/v2/: i/o timeout\n")
            return 1
        return 0
    if cmd == "image":                       # docker image inspect IMAGE
        if MODE == "noimage":
            sys.stderr.write("Error: No such image\n")
            return 1
        print("sha256:fake")
        return 0
    if cmd == "rm":
        for name in argv[2:]:
            if os.path.exists(path(name)):
                os.remove(path(name))
        return 0
    if cmd == "kill":
        with open(path(argv[1]), "w") as fh:
            fh.write("137")
        return 0
    if cmd == "inspect":
        name = argv[-1]
        if not os.path.exists(path(name)):
            sys.stderr.write(f"Error: No such object: {name}\n")
            return 1
        code = open(path(name)).read().strip()
        oom = "true" if MODE == "oom" else "false"
        print(f"{137 if MODE == 'oom' else code} {oom}")
        return 0
    if cmd == "run":
        name = argv[argv.index("--name") + 1]
        boot = argv[-1]
        proc = subprocess.run([sys.executable, "-I", "-c", boot], stdin=sys.stdin,
                              env={"PATH": os.environ["PATH"], "CC_CODE": os.environ["CC_CODE"]})
        if not os.path.exists(path(name)):          # `kill` may have written 137 already
            with open(path(name), "w") as fh:
                fh.write(str(proc.returncode))
        return proc.returncode
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
