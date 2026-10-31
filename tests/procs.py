# SPDX-License-Identifier: Apache-2.0
"""The other processes of the SQLite tests (templet:3159): each is run as
`python procs.py <what> <url> ...` and prints one JSON line when it is done.

- `writer URL TAG COUNT GATE`: waits for the file GATE, then files COUNT items
  and a comment on each, and prints the numbers it got;
- `waiter URL CURSOR WAIT`: prints `waiting`, then waits for events after
  CURSOR, and prints their seqs and the wall time it returned;
- `killed URL`: files an item and is killed (SIGKILL) inside its transaction,
  after the journal row and the puts are written and before the commit.
"""

import json
import os
import signal
import sys
import time

from quarrylith.record import contract, sqlite


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


if __name__ == "__main__":
    {"writer": writer, "waiter": waiter, "killed": killed}[sys.argv[1]](*sys.argv[2:])
