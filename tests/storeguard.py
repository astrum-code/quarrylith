# SPDX-License-Identifier: Apache-2.0
"""Run a command so that it cannot outlive the test process that started it.
Run as a script by `hengestore`, never collected.

    python storeguard.py --parent PID [--root DIR] [--grace S] -- COMMAND ...

The caller starts this process in a session of its own, so it leads the group
COMMAND runs in. It watches PID, the caller's: once that process is gone it
sends the group SIGTERM, and SIGKILL after the grace, and removes DIR. A test
process ended by `timeout`, SIGTERM or SIGKILL runs no `atexit`, so a store left
to it would stay; this guard does not depend on it.

The parent's pid comes from the caller, not from `getppid()` here: a parent
killed before this process has read it would otherwise look like the parent.
The pid is checked before COMMAND starts, too.
"""

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time

POLL_S = 0.2
SETTLE_S = 0.5


def parent_gone(parent: int) -> bool:
    if os.getppid() != parent:  # re-parented: the parent ended
        return True
    try:
        os.kill(parent, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="storeguard.py")
    parser.add_argument("--parent", type=int, required=True)
    parser.add_argument("--root", default=None)
    parser.add_argument("--grace", type=float, default=20.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("no command")
    if parent_gone(args.parent):
        remove(args.root)
        return 1
    child = subprocess.Popen(command)

    def forward(signum, _frame):
        child.send_signal(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    while child.poll() is None:
        if parent_gone(args.parent):
            return end(child, args.grace, args.root)
        time.sleep(POLL_S)
    return child.returncode


def remove(root):
    if root:
        shutil.rmtree(root, ignore_errors=True)


def end(child: subprocess.Popen, grace: float, root) -> int:
    """The parent is gone: the group told to stop, then cut off."""
    group = os.getpgrp()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    os.killpg(group, signal.SIGTERM)
    try:
        child.wait(grace)
    except subprocess.TimeoutExpired:
        pass
    time.sleep(SETTLE_S)  # its workers, past the main process
    remove(root)
    os.killpg(group, signal.SIGKILL)  # this process with it
    return 1


if __name__ == "__main__":
    sys.exit(main())
