# SPDX-License-Identifier: Apache-2.0
"""A load that is killed, in its own process (templet:3159): run as
`python loader.py URL DUMP BATCH POINT`, it opens the record and loads the
dump at `LOAD_BATCH` = BATCH, and SIGKILLs itself at POINT (JSON, see
`Point`; null: once the load returned, printing `returned` first).

`Killing` is the client that does it, and `Point` what it matches; a test
uses them in its own process to kill the store instead."""

import json
import os
import signal
import sys

from quarrylith.record import contract, henge, sqlite


class Point:
    """Where a load dies: the `nth` call (from 1) of `op` (`put`, `sync`) on
    `space` (`journal`, `rec`, `idx`, `state`), at `key` and with `value` if
    they are given, before it is sent or (`after`) once it is answered. On
    SQLite, which has one transaction and no puts, `event` is the event whose
    write the load dies after."""

    def __init__(
        self, op="put", space=None, key=None, value=None, nth=1, after=True, event=None
    ):
        self.op, self.space, self.key, self.value = op, space, key, value
        self.nth, self.after, self.event = nth, after, event
        self.hits = 0

    def to_json(self):
        return json.dumps(
            {
                "op": self.op,
                "space": self.space,
                "key": self.key,
                "value": self.value,
                "nth": self.nth,
                "after": self.after,
                "event": self.event,
            }
        )

    def __repr__(self):
        return self.to_json()

    def hit(self, op, space, key, value):
        if op != self.op:
            return False
        if self.space is not None and not (space or "").endswith(f".{self.space}"):
            return False
        if self.key is not None and list(key) != self.key:
            return False
        if self.value is not None and value != self.value:
            return False
        self.hits += 1
        return self.hits == self.nth


class Killing:
    """A client that calls `die()` at `point`."""

    def __init__(self, inner, point, die):
        self.inner, self.point, self.die = inner, point, die
        self.endpoint = getattr(inner, "endpoint", None)
        self.died = False

    def _die(self):
        self.died = True
        self.die()

    def put(self, space, key, value, *, expect=None):
        hit = self.point.hit("put", space, key, value)
        if hit and not self.point.after:
            self._die()
        out = self.inner.put(space, key, value, expect=expect)
        if hit and self.point.after:
            self._die()
        return out

    def sync(self, space):
        hit = self.point.hit("sync", space, None, None)
        if hit and not self.point.after:
            self._die()
        out = self.inner.sync(space)
        if hit and self.point.after:
            self._die()
        return out

    def __getattr__(self, op):
        return getattr(self.inner, op)


def kill_me():
    os.kill(os.getpid(), signal.SIGKILL)


def main(url, dump_path, batch, point):
    henge.LOAD_BATCH = int(batch)
    with open(dump_path, encoding="utf-8") as f:
        dump = json.load(f)
    point = None if point == "null" else Point(**json.loads(point))
    record = contract.open_record(url, actor="loader", via="load")
    if point is not None and point.event is not None:
        store = sqlite.SqliteRecord._store

        def store_then_die(self, event, priors):
            store(self, event, priors)
            if event.seq == point.event:
                kill_me()

        sqlite.SqliteRecord._store = store_then_die
    elif point is not None:
        record.store = Killing(record.store, point, kill_me)
    record.load(dump)
    print("returned", flush=True)
    kill_me()


if __name__ == "__main__":
    main(*sys.argv[1:])
