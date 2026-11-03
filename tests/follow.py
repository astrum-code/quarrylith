# SPDX-License-Identifier: Apache-2.0
"""A follower of a Henge record in its own process (templet:3159), run as
`python follow.py FOLDER NAME UNTIL WAIT [DROP]`: it prints `ready` once its
watch of `head` is up, then one JSON line per event it gets from cursor 0 on,
`{"seq", "at"}` (`at` the wall time it got it), until it has seq UNTIL or
WAIT seconds have passed. With DROP, the first DROP notes the watch's
subscriber receives are dropped, as a publisher drops them for a slow
subscriber."""

import json
import sys
import time

from henge.store.client import asyncstore

from quarrylith.record.henge import HengeFollower


def dropping(count):
    """The watch's subscriber, losing its next `count` notes."""
    left = [count]
    receive = asyncstore._Watched.receive

    async def lossy(self, timeout=None):
        end = None if timeout is None else time.monotonic() + timeout
        while True:
            wait = None if end is None else max(0.0, end - time.monotonic())
            got = await receive(self, timeout=wait)
            if got is not None and left[0] > 0:
                left[0] -= 1
                if end is not None and time.monotonic() >= end:
                    return None
                continue
            return got

    asyncstore._Watched.receive = lossy


def main(folder, name, until, wait, drop="0"):
    if int(drop):
        dropping(int(drop))
    follower = HengeFollower(folder, name)
    end = time.monotonic() + 10
    while follower._notes < 1 and time.monotonic() < end:
        time.sleep(0.01)
    print("ready", flush=True)
    cursor, until = 0, int(until)
    end = time.monotonic() + float(wait)
    while cursor < until and time.monotonic() < end:
        got = follower.events(cursor, wait=max(0.0, end - time.monotonic()))
        at = time.time()
        for event in got:
            print(json.dumps({"seq": event.seq, "at": at}), flush=True)
            cursor = event.seq
    follower.close()


if __name__ == "__main__":
    main(*sys.argv[1:])
