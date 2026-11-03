# SPDX-License-Identifier: Apache-2.0
"""The Henge stores the tests run on (templet:3159).

- `store(placement)`: a running `henge single DIR` (`--mirror` for the
  mirror), started once per test process in a short folder under /tmp (a
  Unix socket's path is at most 104 bytes on macOS) and stopped at exit. A
  record's name is fresh per test (`fresh_name`). Without henge-store-serve
  installed, the Henge tests skip with that reason, never pass.
- `Guarded`: the client every record of the suite gets. It passes on only
  the operations the record may use (`put`, `get`, `get_many`, `sync`,
  `durability`, `close`), and refuses a delete, any listing, and a
  `get_many` of more than 1,000 keys.
- `FakeStore`: an in-memory store with the same operations, whose values go
  through msgpack as the real store's do (tuples come back as lists). It
  logs every call in order, and can hold back any one put and release it
  later, or answer it `Unavailable` and land it later (a late request).
"""

import atexit
import itertools
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import msgpack
import pytest

try:
    from henge.core import errors
    from henge.store.client import Store
except ImportError:  # the client is quarrylith's own dependency
    errors = Store = None

#: What the record may ask of a store; anything else is refused.
ALLOWED = ("put", "get", "get_many", "sync", "durability", "close")

_STORES = {}
_NAMES = itertools.count(1)
_LOCK = threading.Lock()


def missing():
    """Why no store can run here, or None."""
    try:
        import henge.store.serve.single  # noqa: F401
    except ImportError:
        return "henge-store-serve is not installed: no store to run the Henge tests"
    if not (Path(sys.executable).parent / "henge").exists():
        return f"no henge command beside {sys.executable}"
    return None


def fresh_name(tag="r"):
    return f"{tag}{os.getpid()}-{next(_NAMES)}"


class Running:
    """One `henge single` process."""

    def __init__(self, placement):
        self.placement = placement
        self.root = Path(tempfile.mkdtemp(prefix="qh", dir="/tmp")).resolve()
        self.folder = self.root / "s"
        self.log = open(self.root / "henge.log", "wb")
        command = [str(Path(sys.executable).parent / "henge"), "single"]
        if placement == "mirror":
            command.append("--mirror")
        self.proc = subprocess.Popen(
            command + [str(self.folder)],
            stdout=self.log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.address = f"ipc://{self.folder}/henge.sock"
        self._wait()

    def _wait(self, wait_s=60):
        end = time.monotonic() + wait_s
        while time.monotonic() < end:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"henge single exited {self.proc.returncode}: "
                    + (self.root / "henge.log").read_text()[-2000:]
                )
            if (self.folder / "henge.sock").exists():
                client = Store.connect(self.address, timeout_ms=1000, tries=1)
                try:
                    report = client.durability("probe.journal")
                    if report.get("workers") and all(
                        v == "serving" for v in report["workers"].values()
                    ):
                        return
                except errors.HengeError:
                    pass
                finally:
                    client.close()
            time.sleep(0.1)
        raise RuntimeError(f"henge single did not come up in {self.folder}")

    def url(self, name=None):
        return f"henge:{self.folder}#{name or fresh_name()}"

    def stop(self):
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.log.close()
        shutil.rmtree(self.root, ignore_errors=True)


def store(placement="single"):
    """The test process's store of `placement` (single or mirror), started on
    first use. Skips when no store can run here."""
    why = missing()
    if why:
        pytest.skip(why)
    with _LOCK:
        if placement not in _STORES:
            _STORES[placement] = Running(placement)
        return _STORES[placement]


def stop_all():
    with _LOCK:
        for running in _STORES.values():
            running.stop()
        _STORES.clear()


atexit.register(stop_all)


class Refusal(AssertionError):
    """What a guarded store raises for an operation the record must not use."""


class Guarded:
    """A store client that passes on only what the record may use."""

    def __init__(self, inner, *, log=None):
        self._inner = inner
        self.endpoint = getattr(inner, "endpoint", None)
        self.calls = log if log is not None else []

    def __getattr__(self, op):
        if op not in ALLOWED:
            raise Refusal(f"the record called {op}: it may use only {ALLOWED}")
        return getattr(self._inner, op)

    def get_many(self, space, keys):
        keys = list(keys)
        if len(keys) > 1000:
            raise Refusal(f"get_many of {len(keys)} keys, past 1,000")
        return self._inner.get_many(space, keys)


def guarding(connect):
    """`connect` (the backend's), with every client it makes guarded."""

    def guarded(address, timeout_s):
        return Guarded(connect(address, timeout_s))

    guarded.inner = connect
    return guarded


# -- the fake -----------------------------------------------------------------


def _roundtrip(value):
    return msgpack.unpackb(msgpack.packb(value, use_bin_type=True), raw=False)


def _k(space, key):
    return (space, msgpack.packb(list(key), use_bin_type=True))


class Item:
    def __init__(self, value, version):
        self.value, self.version, self.meta = value, version, {}


class Held:
    """A put held back: `reached` is set when it arrives; `release()` lands it
    (its `expect` checked then) and, for a held caller, answers it."""

    def __init__(self, store, match, late, op):
        self.store, self.match, self.late, self.op = store, match, late, op
        self.reached = threading.Event()
        self.released = threading.Event()
        self.call = None
        self.result = None

    def release(self):
        if self.late:
            with self.store.lock:
                try:
                    self.result = self.store._apply_put(*self.call)
                except errors.HengeError as e:
                    self.result = e
            self.store.log.append(("late", *self.call[:3], self.result))
        self.released.set()


class FakeStore:
    """An in-memory store: `client(who)` is one writer's connection."""

    def __init__(self):
        self.objects = {}
        self.lock = threading.RLock()
        self.log = []  # (who, op, space, key, result) in order
        self.versions = itertools.count(1)
        self.report = {
            "space": None,
            "placement": "single",
            "durability": "batch",
            "full_sync": "F_FULLFSYNC",
            "power_safe": True,
            "ordered": True,
            "copies": 1,
            "degraded": False,
            "workers": {"w0": "serving"},
            "cut_bytes": 0,
        }
        self.holds = []
        #: (who, op, space, key) -> exception to raise once, before or after
        self.faults = []

    def client(self, who="w"):
        return FakeClient(self, who)

    def hold(self, match, *, late=False, op="put"):
        """Hold back the next put (or get) for which `match(who, space, key,
        value)` is true: its caller waits (late: is answered `Unavailable` at
        once, and the put lands at `release()`) until `release()`."""
        held = Held(self, match, late, op)
        self.holds.append(held)
        return held

    def fault(self, match, exc, *, after=False, times=1):
        """Raise `exc` at the next `times` puts `match` takes: before the put
        lands, or after it (a reply lost)."""
        self.faults.append([match, exc, after, times])

    def keys(self):
        """Every (space, key) the fake holds: the test's own listing."""
        with self.lock:
            return [(space, msgpack.unpackb(k, raw=False)) for space, k in self.objects]

    def value(self, space, key):
        got = self.objects.get(_k(space, key))
        return None if got is None else got[0]

    def _apply_put(self, who, space, key, value, expect):
        k = _k(space, key)
        now = self.objects.get(k)
        if expect is not None:
            if expect.get("absent"):
                if now is not None:
                    raise errors.Conflict("expected absent; it is there")
            elif "version" in expect:
                if now is None:
                    raise errors.Conflict("expected a version; it is absent")
                if now[1] != expect["version"]:
                    raise errors.Conflict("expected another version")
        version = next(self.versions).to_bytes(8, "big")
        self.objects[k] = (_roundtrip(value), version)
        return version


class FakeClient:
    def __init__(self, store, who):
        self.fake, self.who = store, who
        self.endpoint = f"fake:{who}"
        self.closed = False

    def _logged(self, op, space, key, result):
        self.fake.log.append((self.who, op, space, key, result))

    def _fault(self, op, space, key, value, after):
        for f in list(self.fake.faults):
            match, exc, when, times = f
            if when == after and match(self.who, op, space, key, value):
                f[3] -= 1
                if f[3] <= 0:
                    self.fake.faults.remove(f)
                raise exc

    def _held(self, op, space, key, value, expect=None):
        held = None
        with self.fake.lock:
            for h in self.fake.holds:
                if h.op == op and h.match(self.who, space, key, value):
                    held = h
                    h.call = (self.who, space, list(key), value, expect)
                    self.fake.holds.remove(h)
                    break
        if held is None:
            return
        self._logged(f"{op}-held", space, list(key), None)
        held.reached.set()
        if held.late:
            raise errors.Unavailable("no answer (held back, it lands later)")
        if not held.released.wait(60):
            raise RuntimeError("a held call was never released")

    def put(self, space, key, value, *, expect=None):
        if self.closed:
            raise errors.Unavailable("closed")
        self._fault("put", space, key, value, False)
        self._held("put", space, key, value, expect)
        with self.fake.lock:
            try:
                version = self.fake._apply_put(self.who, space, key, value, expect)
            except errors.HengeError as e:
                self._logged("put", space, list(key), e)
                raise
            self._logged("put", space, list(key), version)
        self._fault("put", space, key, value, True)
        return version

    def get(self, space, key):
        if self.closed:
            raise errors.Unavailable("closed")
        self._fault("get", space, key, None, False)
        self._held("get", space, key, None)
        with self.fake.lock:
            got = self.fake.objects.get(_k(space, key))
            self._logged("get", space, list(key), None if got is None else got[0])
            return None if got is None else Item(_roundtrip(got[0]), got[1])

    def get_many(self, space, keys):
        keys = [list(k) for k in keys]
        if len(keys) > 1000:
            raise Refusal(f"get_many of {len(keys)} keys")
        self._fault("get_many", space, keys, None, False)
        with self.fake.lock:
            out = []
            for key in keys:
                got = self.fake.objects.get(_k(space, key))
                out.append(None if got is None else Item(_roundtrip(got[0]), got[1]))
            self.fake.log.append((self.who, "get_many", space, keys, None))
            return out

    def sync(self, space):
        self._fault("sync", space, None, None, False)
        self._logged("sync", space, None, None)

    def durability(self, space):
        self._fault("durability", space, None, None, False)
        return dict(self.fake.report, space=space)

    def close(self):
        self.closed = True

    def delete(self, *a, **k):
        raise Refusal("the record deleted an object")
