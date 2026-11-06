# SPDX-License-Identifier: Apache-2.0
"""A store a test starts ends with the test's process, however that ends.

Each test starts a real Python process that uses the `hengestore` fixture,
ends it by SIGTERM or SIGKILL, at the start of the store and once it serves,
and then waits (bounded) for the store's process group to be gone.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import hengestore
import pytest

pytestmark = pytest.mark.skipif(
    hengestore.missing() is not None, reason=hengestore.missing() or ""
)

TESTS = Path(__file__).resolve().parent

CHILD = """
import subprocess, sys
sys.path.insert(0, {tests!r})
import hengestore

real = subprocess.Popen


def popen(*a, **k):
    made = real(*a, **k)
    print("started", made.pid, flush=True)
    return made


subprocess.Popen = popen
running = hengestore.store("single")
print("serving", running.root, flush=True)
import time
time.sleep(600)
"""

CEILING_S = 30


def gone(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL], ids=["term", "kill"])
@pytest.mark.parametrize("moment", ["start", "serving"])
def test_a_store_ends_when_its_test_process_is_killed(sig, moment):
    child = subprocess.Popen(
        [sys.executable, "-c", CHILD.format(tests=str(TESTS))],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    pgid = None
    root = None
    try:
        end = time.monotonic() + 90
        want = "started" if moment == "start" else "serving"
        while time.monotonic() < end:
            line = child.stdout.readline()
            if not line:
                break
            parts = line.split()
            if parts[0] == "started":
                pgid = int(parts[1])
            if parts[0] == "serving":
                root = parts[1]
            if parts[0] == want:
                break
        assert pgid, "the test process started no store"
        os.kill(child.pid, sig)
        child.wait(30)
        end = time.monotonic() + CEILING_S
        while time.monotonic() < end and not gone(pgid):
            time.sleep(0.2)
        left = not gone(pgid)
        assert not left, f"the store's group {pgid} outlived its test process"
        if root:
            # The folder goes too once the store is gone, where it can.
            end = time.monotonic() + 10
            while time.monotonic() < end and Path(root).exists():
                time.sleep(0.2)
            assert not Path(root).exists(), f"{root} is left"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        if pgid and not gone(pgid):  # only what this test started
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
