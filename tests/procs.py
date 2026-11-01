# SPDX-License-Identifier: Apache-2.0
"""The other processes of the SQLite tests (templet:3159): each is run as
`python procs.py <what> <url> ...` and prints one JSON line when it is done.

- `writer URL TAG COUNT GATE`: waits for the file GATE, then files COUNT items
  and a comment on each, and prints the numbers it got;
- `waiter URL CURSOR WAIT`: prints `waiting`, then waits for events after
  CURSOR, and prints their seqs and the wall time it returned;
- `killed URL`: files an item and is killed (SIGKILL) inside its transaction,
  after the journal row and the puts are written and before the commit;
- `starter URL TAG GATE TASK...`: waits for the file GATE, then starts a run
  of each task on node `n1`, holding every run it started; when a start finds
  no room it finishes every run it holds, pauses a moment for the other
  process, and tries again. It finishes the rest at the end, and prints how
  many starts it made and how many found no room.
"""

import json
import os
import signal
import sys
import time

from quarrylith.record import contract, sqlite
from quarrylith.record.contract import Refused


def writer(url, tag, count, gate):
    record = contract.open_record(url, actor=tag, via="cli")
    while not os.path.exists(gate):
        time.sleep(0.002)
    numbers = []
    for i in range(int(count)):
        n = record.file_item("task", f"{tag} {i}", "b", filed_by=tag, effort="0001")
        record.comment(n, f"{tag} on {n}")
        numbers.append(n)
    print(json.dumps({"numbers": numbers}), flush=True)


def waiter(url, cursor, wait):
    record = contract.open_record(url, actor="reader", via="cli")
    print("waiting", flush=True)
    got = record.events(int(cursor), wait=float(wait))
    print(json.dumps({"seqs": [e.seq for e in got], "at": time.time()}), flush=True)


def killed(url):
    record = contract.open_record(url, actor="doomed", via="cli")
    store = sqlite.SqliteRecord._store

    def store_then_die(self, event, priors):
        store(self, event, priors)
        os.kill(os.getpid(), signal.SIGKILL)

    sqlite.SqliteRecord._store = store_then_die
    record.file_item("task", "never", "b", filed_by="doomed", effort="0001")


def starter(url, tag, gate, *tasks):
    record = contract.open_record(url, actor=tag, via="service")
    while not os.path.exists(gate):
        time.sleep(0.002)
    held, started, refused = [], 0, 0

    def finish(run):
        record.finish_run(run, "done", "made room", footprint={}, cost={})

    for task in tasks:
        while True:
            try:
                run = record.start_run(
                    int(task),
                    tag,
                    node="n1",
                    sandbox="process",
                    fence=None,
                    expect_s=60,
                )
            except Refused as e:
                if "no room" not in str(e):
                    raise
                refused += 1
                while held:
                    finish(held.pop(0))
                time.sleep(0.02)
                continue
            held.append(run)
            started += 1
            break
    for run in held:
        finish(run)
    print(json.dumps({"started": started, "refused": refused}), flush=True)


if __name__ == "__main__":
    {"writer": writer, "waiter": waiter, "killed": killed, "starter": starter}[
        sys.argv[1]
    ](*sys.argv[2:])
