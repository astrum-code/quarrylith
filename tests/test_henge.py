# SPDX-License-Identifier: Apache-2.0
"""The Henge backend's own acceptance (templet:3159): every `Henge:` item of
the record's page. The rest of the suite runs over Henge too, on the single
store and on the mirror (`over_backends`).

Most tests run on a real store (`hengestore.store`): a crash is a client that
dies at a put (`Crashing`), and the record is then abandoned as a killed
process leaves it. The two-writer and late-request tests run on `FakeStore`,
which can hold back any one put and release it later."""

import itertools
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from backends import PARTS, Clock, admitted
from hengestore import (
    Copy,
    FakeStore,
    Guarded,
    Refusal,
    fresh_name,
    held_by,
    in_process_mirror,
    mirror_state,
    store,
)

from quarrylith.record import henge, schema
from quarrylith.record.contract import Refused, Unavailable, Unknown, open_record
from quarrylith.record.events import Event, MemoryView, apply

FOLLOW = Path(__file__).resolve().parent / "follow.py"

try:
    from henge.core import errors
    from henge.store.client import Store
except ImportError:  # pragma: no cover - the client is a dependency
    errors = Store = None


class Crash(BaseException):
    """The writer's process dies here."""


class Crashing:
    """A client that dies at the first put `match(space, key)` takes: before
    it is sent, or once it is answered (an error is an answer too)."""

    def __init__(self, inner, match, *, after=False):
        self.inner, self.match, self.after = inner, match, after
        self.endpoint = getattr(inner, "endpoint", None)
        self.fired = False

    def put(self, space, key, value, *, expect=None):
        hit = not self.fired and self.match(space, list(key))
        if hit and not self.after:
            self.fired = True
            raise Crash(f"before {space} {key}")
        try:
            return self.inner.put(space, key, value, expect=expect)
        finally:
            if hit:
                self.fired = True
                raise Crash(f"after {space} {key}")

    def __getattr__(self, op):
        return getattr(self.inner, op)


class Recording:
    """A client that logs every call, `(op, space, key)`, and checks that no
    call is sent while another is still waiting for its answer."""

    def __init__(self, inner):
        self.inner = inner
        self.endpoint = getattr(inner, "endpoint", None)
        self.calls = []
        self.waiting = 0
        self.overlapped = 0

    def _call(self, op, space, key, *args, **kw):
        self.waiting += 1
        if self.waiting > 1:
            self.overlapped += 1
        self.calls.append((op, space, None if key is None else list(key)))
        try:
            return getattr(self.inner, op)(
                space, *([key] if key is not None else []), *args, **kw
            )
        finally:
            self.waiting -= 1

    def put(self, space, key, value, *, expect=None):
        return self._call("put", space, key, value, expect=expect)

    def get(self, space, key):
        return self._call("get", space, key)

    def get_many(self, space, keys):
        keys = [list(k) for k in keys]
        self.waiting += 1
        if self.waiting > 1:
            self.overlapped += 1
        self.calls.append(("get_many", space, keys))
        try:
            return self.inner.get_many(space, keys)
        finally:
            self.waiting -= 1

    def sync(self, space):
        return self._call("sync", space, None)

    def durability(self, space):
        return self._call("durability", space, None)

    def close(self):
        return self.inner.close()


def abandon(record):
    """The writer's process is gone: no more puts, and the kernel releases
    its lock."""
    record._closed = "crashed"
    record._shut()


@pytest.fixture
def single():
    return store("single")


@pytest.fixture
def raw(single):
    client = Store.connect(single.address)
    yield client
    client.close()


def opened(url, clock=None, **kw):
    kw.setdefault("actor", "alpha")
    kw.setdefault("via", "cli")
    rec = open_record(url, **kw)
    rec.clock = clock or Clock()
    return rec


def item(rec, title="t", **kw):
    kw.setdefault("effort", "0001")
    kw.setdefault("role", "alpha")
    return rec.file_item("task", title, "body", filed_by="alpha", **kw)


def fake_record(fake, who, name="rec", clock=None, **kw):
    kw.setdefault("actor", who.split(".")[0])
    kw.setdefault("via", "cli")
    kw.setdefault("timeout_s", 2.0)
    rec = henge.HengeRecord(fake.client(who), name, **kw)
    rec.clock = clock or Clock()
    return rec


def top(fake, name="rec"):
    """The highest seq in the fake's journal."""
    space = henge.spaces(name)["journal"]
    seqs = [k[0] for s, k in fake.keys() if s == space]
    return max(seqs, default=0)


# -- the writer lock ------------------------------------------------------------


def test_a_second_open_of_a_held_record_is_refused_naming_the_holder(single):
    """templet:3159 -- Henge: a second open_henge on a held record is refused,
    naming the holder's pid, and so is a readonly open; the lock file
    <folder>/quarrylith.<name>.lock is the only thing the open writes into the
    store's folder."""
    name = fresh_name()
    url = single.url(name)
    before = set(os.listdir(single.folder))
    first = opened(url)
    assert set(os.listdir(single.folder)) - before == {f"quarrylith.{name}.lock"}
    assert os.get_inheritable(first._lockfd) is False
    first.init()
    with pytest.raises(Refused, match=f"held by pid {os.getpid()}"):
        opened(url)
    with pytest.raises(Refused, match=f"held by pid {os.getpid()}"):
        opened(url, readonly=True)
    with pytest.raises(Refused, match="held by pid"):
        henge.open_henge(single.folder, name, actor="beta", via="cli")
    assert set(os.listdir(single.folder)) - before == {f"quarrylith.{name}.lock"}
    first.close()
    ro = opened(url, readonly=True)
    with pytest.raises(Refused, match="read-only"):
        admitted(ro)
    with pytest.raises(Refused, match="held by pid"):
        opened(url)
    ro.close()
    again = opened(url)
    admitted(again)
    again.close()


# -- the report -----------------------------------------------------------------


class Reporting:
    """A client whose report says what the test says, and whose next calls
    can fail `Unavailable`."""

    def __init__(self, inner, **report):
        self.inner, self.report, self.down = inner, report, 0
        self.endpoint = getattr(inner, "endpoint", None)
        self.asked = 0

    def durability(self, space):
        self.asked += 1
        return {**self.inner.durability(space), **self.report}

    def get(self, space, key):
        if self.down:
            self.down -= 1
            raise errors.Unavailable("the store is not answering")
        return self.inner.get(space, key)

    def __getattr__(self, op):
        return getattr(self.inner, op)


def test_a_store_without_sync_is_refused_and_one_not_power_safe_is_undurable(
    single, monkeypatch
):
    """templet:3159 -- Henge: a store without sync and durability is refused at
    open; one whose report is not power_safe and ordered reads durable false,
    and a shared open of it is refused unless allow_undurable."""
    fake = FakeStore()
    client = fake.client("w")
    for missing in ("sync", "durability"):

        class Lacking:
            def __getattr__(self, op, missing=missing):
                if op == missing:
                    raise AttributeError(op)
                return getattr(client, op)

        with pytest.raises(Refused, match=missing):
            henge.HengeRecord(Lacking(), "rec", actor="a", via="cli")

    class Refusing:
        def durability(self, space):
            raise errors.Invalid("no operation durability")

        def __getattr__(self, op):
            return getattr(client, op)

    with pytest.raises(Refused, match="durability"):
        henge.HengeRecord(Refusing(), "rec", actor="a", via="cli")

    url = single.url()
    rec = opened(url)
    assert rec.durable is True and rec.report["placement"] == "single"
    rec.init()
    rec.close()
    inner = henge._connect
    for report in (
        {"power_safe": False},
        {"ordered": False},
        {"full_sync": None, "power_safe": False},
    ):
        monkeypatch.setattr(
            henge, "_connect", lambda a, t, r=report: Reporting(inner(a, t), **r)
        )
        rec = opened(url)
        assert rec.durable is False
        rec.close()
        with pytest.raises(Refused, match="shared record is durable"):
            opened(url, shared=True)
        rec = opened(url, shared=True, allow_undurable=True)
        assert rec.shared and not rec.durable
        rec.close()
    monkeypatch.setattr(henge, "_connect", inner)
    rec = opened(url, shared=True)
    assert rec.durable
    rec.close()


def test_after_an_unavailable_the_report_is_read_again(single, monkeypatch):
    """templet:3159 -- Henge: after an Unavailable, the report is read again
    before the next operation, and a shared record whose report has turned
    not power-safe refuses the operation."""
    inner = henge._connect
    made = []

    def connect(a, t):
        made.append(Reporting(inner(a, t)))
        return made[-1]

    monkeypatch.setattr(henge, "_connect", connect)
    rec = opened(single.url(), shared=True)
    client = made[-1]
    rec.init()
    admitted(rec)
    n = item(rec)
    asked = client.asked
    rec._forget()  # so the next operation reads the store
    client.down = 1
    with pytest.raises(Unavailable):
        rec.edit_item(n, title="x")
    assert client.asked == asked
    rec.edit_item(n, title="y")
    assert client.asked == asked + 1
    assert rec.get("items", n)["title"] == "y"
    # turned not power-safe: refused, naming the report, until it is again
    client.report = {"power_safe": False}
    rec._forget()
    client.down = 1
    with pytest.raises(Unavailable):
        rec.edit_item(n, title="z")
    head = rec.head()
    with pytest.raises(Unavailable, match="power-safe"):
        rec.edit_item(n, title="z")
    with pytest.raises(Unavailable, match="power_safe"):
        rec.comment(n, "still not")
    assert rec.head() == head
    client.report = {}
    rec.edit_item(n, title="z")
    assert rec.head() == head + 1
    rec.close()


# -- one put at a time, and its one sync ----------------------------------------


def test_one_sync_per_operation_after_its_last_put_and_before_head(single):
    """templet:3159 -- Henge: an operation's sync(space) comes after its last
    record and index put and before its head put and its return, once per
    operation; and no put is sent before the one before it is answered."""
    rec = opened(single.url())
    rec.init()
    admitted(rec)
    rec.store = spy = Recording(rec.store)
    rec._forget()
    s = henge.spaces(rec.name)
    ops = [
        lambda: item(rec, "one", role="alpha"),
        lambda: rec.comment(1, "a comment"),
        lambda: rec.edit_item(1, title="two"),
        lambda: rec.grant(1, "alpha", branch="b", machine="m"),
        lambda: rec.move(1, "blocked", "waits"),
    ]
    for op in ops:
        spy.calls.clear()
        op()
        puts = [c for c in spy.calls if c[0] == "put"]
        syncs = [i for i, c in enumerate(spy.calls) if c[0] == "sync"]
        assert len(syncs) == 1, spy.calls
        sync = syncs[0]
        commit = spy.calls.index(("put", s["journal"], [rec.head()]))
        after = [c for c in spy.calls[commit + 1 :] if c[0] == "put"]
        before_sync = [c for c in spy.calls[commit + 1 : sync] if c[0] == "put"]
        assert [c[1] for c in after[:-1]] == [c[1] for c in before_sync]
        assert after[-1] == ("put", s["state"], ["head"])
        assert spy.calls[sync - 1] == ("put", s["state"], ["applied"])
        assert spy.calls[-1] == ("put", s["state"], ["head"])
        assert any(c[1] == s["rec"] for c in before_sync)
        assert any(c[1] == s["idx"] for c in before_sync)
        assert puts[0] == ("put", s["journal"], [rec.head()])
    assert spy.overlapped == 0
    rec.close()


def test_ack_and_beat_are_synced_after_their_last_put(single):
    """templet:3159 -- Henge: `ack` and `beat` are synced, as on SQLite: each
    ends with one sync(space) after its last put, so a cursor or a beat its
    caller heard of survives a power cut."""
    rec = opened(single.url())
    rec.init()
    admitted(rec)
    item(rec, "one")
    rec.store = spy = Recording(rec.store)
    ops = [
        lambda: rec.ack("c", 1),
        lambda: rec.beat(7),
        lambda: rec.ack("c", rec.head()),
        lambda: rec.beat(7),
    ]
    for op in ops:
        spy.calls.clear()
        op()
        seen = [c[0] for c in spy.calls]
        assert seen.count("sync") == 1, spy.calls
        assert seen[-2:] == ["put", "sync"], spy.calls
    assert spy.overlapped == 0
    rec.close()


# -- no object deleted ------------------------------------------------------------


def test_no_object_is_ever_deleted_and_a_late_load_start_meets_conflict(single, raw):
    """templet:3159 -- Henge: no object is ever deleted (the store fake refuses
    delete); a load's end puts loading to null, and a late absent put of the
    load's start then meets Conflict."""
    fake = FakeStore()
    rec = fake_record(fake, "w")
    rec.init()
    admitted(rec)
    n = item(rec)
    rec.move(n, "blocked", "x")
    rec.ack("c", 2)
    rec.beat(1)
    dump = rec.dump()
    target = fake_record(fake, "t", name="loaded")
    target.init()
    target.load(dump)
    assert target.dump() == dump
    assert not [c for c in fake.log if "delete" in c[1]]
    with pytest.raises(Refusal):
        fake.client("x").delete(henge.spaces("rec")["rec"], ["items", n])
    # on the real store: loading is null, never deleted, so a late absent put
    # of the load's start meets Conflict
    url = single.url()
    real = opened(url, actor="loader", via="load")
    real.init()
    real.load(dump)
    s = henge.spaces(real.name)
    got = raw.get(s["state"], ["loading"])
    assert got is not None and got.value is None
    with pytest.raises(errors.Conflict):
        raw.put(s["state"], ["loading"], {"digest": "late"}, expect={"absent": True})
    assert real.dump() == dump
    real.close()
    with pytest.raises(Refusal):
        Guarded(raw).delete(s["rec"], ["items", n])


# -- a follower in another process ----------------------------------------------


def follow(single, name, until, wait, drop=0):
    proc = subprocess.Popen(
        [
            sys.executable,
            str(FOLLOW),
            str(single.folder),
            name,
            str(until),
            str(wait),
            str(drop),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    line = proc.stdout.readline().strip()
    assert line == "ready", line + proc.stderr.read()
    return proc


def test_a_follower_in_another_process_gets_every_event_by_watch(single):
    """templet:3159 -- Henge: a follower in another process, connected over
    ipc://, gets every event up to head in order, with no gap, by watch and
    get_many; with a watch note dropped, it still sees a commit within
    1.5 s."""
    rec = opened(single.url())
    rec.init()
    admitted(rec)
    for i in range(3):
        item(rec, f"before {i}")
    proc = follow(single, rec.name, 20, 30)
    for i in range(16):
        item(rec, f"while {i}")
        if i % 5 == 0:
            time.sleep(0.2)
    assert rec.head() == 20
    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 0, err
    seqs = [json.loads(line)["seq"] for line in out.splitlines()]
    assert seqs == list(range(1, 21))
    # the note of the next commit is dropped: the catch-up read still sees it
    for drop in (1, 2):
        head = rec.head()
        proc = follow(single, rec.name, head + 1, 10, drop=drop)
        time.sleep(0.3)
        item(rec, f"dropped {drop}")
        committed = time.time()
        out, err = proc.communicate(timeout=30)
        assert proc.returncode == 0, err
        got = [json.loads(line) for line in out.splitlines()]
        assert [g["seq"] for g in got][-1] == head + 1
        assert got[-1]["at"] - committed < 1.5, got
    rec.close()


# -- crashes ----------------------------------------------------------------------


def test_a_crash_after_the_journal_put_is_recovered_at_the_next_open(single):
    """templet:3159 -- Henge: a crash after the journal put and before applied
    is recovered at the next open, and the replay still equals the records."""
    url = single.url()
    clock = Clock()
    rec = opened(url, clock)
    rec.init()
    admitted(rec)
    n = item(rec)
    s = henge.spaces(rec.name)
    points = {
        # once the commit put is answered
        "journal": (lambda space, key: space == s["journal"], True),
        # once the item's record is put, before any index
        "record": (lambda space, key: space == s["rec"] and key[0] == "items", True),
        # after applied and the sync, before head
        "head": (lambda space, key: space == s["state"] and key == ["head"], False),
    }
    for after_key, (match, after) in points.items():
        seq = rec.head() + 1
        rec.store = Crashing(rec.store, match, after=after)
        with pytest.raises(Crash):
            rec.edit_item(n, title=f"crashed {after_key}")
        abandon(rec)
        rec = opened(url, clock)
        assert rec.head() == seq
        assert rec.get("items", n)["title"] == f"crashed {after_key}"
        assert rec.check() == []
        dump = rec.dump()
        assert dump["journal"][-1]["seq"] == seq
    rec.close()


@pytest.mark.parametrize("what", ["move", "file"])
def test_a_crash_between_a_record_put_and_its_index_puts_is_recovered(single, what):
    """templet:3159 -- Henge: a crash between a record put and its index puts
    (an item moved from ready to claimed, and an item filed) is recovered:
    read by every match field agrees with dump(), and dump() holds every
    record."""
    url = single.url()
    clock = Clock()
    rec = opened(url, clock)
    rec.init()
    admitted(rec)
    n = item(rec, "first")
    item(rec, "second", role=None)
    s = henge.spaces(rec.name)
    # dies at its first index put: every record of the event is put
    rec.store = Crashing(rec.store, lambda space, key: space == s["idx"])
    with pytest.raises(Crash):
        if what == "move":
            rec.grant(n, "alpha", branch="b", machine="m")
        else:
            item(rec, "third", role="beta")
    abandon(rec)
    rec = opened(url, clock)
    assert rec.check() == []
    dump = rec.dump()
    items = dump["records"]["items"]
    assert len(items) == (2 if what == "move" else 3)
    if what == "move":
        assert rec.get("items", n)["state"] == "claimed"
        assert len(dump["records"]["leases"]) == 1
    for kind, rows in dump["records"].items():
        assert rec.read(kind) == rows
        for field in schema.RECORDS[kind].match:
            for value in {json.dumps(r[field]) for r in rows}:
                v = json.loads(value)
                want = [r for r in rows if r[field] == v and type(r[field]) is type(v)]
                assert rec.read(kind, **{field: v}) == want, (kind, field, v)
    assert rec.read("items", state="ready") == [
        i for i in items if i["state"] == "ready"
    ]
    rec.close()


@pytest.fixture
def small_chunks(monkeypatch):
    """Chunks of 8 keys, split into pieces of 4: a split in a few events."""
    monkeypatch.setattr(henge, "CHUNK_KEYS", 8)
    monkeypatch.setattr(henge, "PIECE_KEYS", 4)
    monkeypatch.setattr(henge, "JOURNAL_CHUNK", 8)


def efforts(rec, ids):
    for e in ids:
        rec.file_effort(e, f"s-{e}", f"Effort {e}", "owner-a", PARTS)


def chunks_of(raw, s, ix):
    d = raw.get(s["idx"], henge.ix_key(ix, "dir"))
    directory = [] if d is None else d.value
    out = {}
    for n in [0] + [n for n, _ in directory]:
        got = raw.get(s["idx"], henge.ix_key(ix, n))
        out[n] = None if got is None else got.value
    return directory, out


@pytest.mark.parametrize("at", ["new chunk", "directory", "shrunk chunk"])
def test_a_crash_after_each_put_of_a_split_is_recovered(single, raw, small_chunks, at):
    """templet:3159 -- Henge: a crash after each put of a split (B, the
    directory, A) is recovered: read gives every key once, and the directory
    lists every chunk; a redo after the directory put makes no second split;
    a key below every key of a directory goes in chunk 0 with no directory
    put."""
    url = single.url()
    clock = Clock()
    rec = opened(url, clock)
    rec.init()
    ids = [f"e{i:02d}" for i in range(10, 18)]
    efforts(rec, ids)  # eight: chunk 0 is full
    s = henge.spaces(rec.name)
    ix = ("efforts", "*", None)
    assert chunks_of(raw, s, ix)[0] == []
    key = {
        "new chunk": henge.ix_key(ix, 1),
        "directory": henge.ix_key(ix, "dir"),
        "shrunk chunk": henge.ix_key(ix, 0),
    }[at]
    rec.store = Crashing(
        rec.store, lambda space, k: space == s["idx"] and k == key, after=True
    )
    with pytest.raises(Crash):
        efforts(rec, ["e18"])  # the ninth: the chunk splits
    abandon(rec)
    rec = opened(url, clock)
    assert rec.check() == []
    got = [e["id"] for e in rec.read("efforts")]
    assert got == ids + ["e18"]
    directory, chunks = chunks_of(raw, s, ix)
    # one split into pieces of at most 4, whatever the crash point: no
    # second split on the redo, and the old chunk rewritten to its range
    assert [n for n, _ in directory] == [1, 2]
    assert chunks[0] == [[i] for i in ids[:4]]
    assert chunks[1] == [[i] for i in ids[4:]]
    assert chunks[2] == [["e18"]]
    # a key below every key: chunk 0, and no directory put
    rec.store = spy = Recording(rec.store)
    efforts(rec, ["a00"])
    puts = [c for c in spy.calls if c[0] == "put"]
    assert ("put", s["idx"], henge.ix_key(ix, "dir")) not in puts
    assert ("put", s["idx"], henge.ix_key(ix, 0)) in puts
    assert raw.get(s["idx"], henge.ix_key(ix, 0)).value[0] == ["a00"]
    assert [e["id"] for e in rec.read("efforts")] == ["a00"] + ids + ["e18"]
    # every load of a chunk filters it to its range: chunk 0 holding chunk
    # 1's keys again, as a split stopped after its directory put leaves it,
    # still reads each key once
    stale = raw.get(s["idx"], henge.ix_key(ix, 0))
    raw.put(
        s["idx"],
        henge.ix_key(ix, 0),
        stale.value + [[i] for i in ids[4:]],
        expect={"version": stale.version},
    )
    rec._forget()
    assert [e["id"] for e in rec.read("efforts")] == ["a00"] + ids + ["e18"]
    rec.close()


# -- errors at the commit point -----------------------------------------------------


def test_errors_at_the_commit_point_are_resolved_or_unknown():
    """templet:3159 -- Henge: a Conflict at the commit point that is the
    writer's own retried put (its entry read back with tuples as lists) puts
    the entry again with the version read, and goes on as committed once that
    put is answered; an error at the commit point re-sends the same put and
    returns the operation's result; a store unreachable past the timeout
    raises Unknown, and the next open finishes an entry that landed."""
    entry = {"event": {"puts": [("items", (1,), {"labels": ("a:b",)})]}, "priors": ()}
    assert henge.same_entry(entry, json.loads(json.dumps(entry)))
    assert not henge.same_entry(entry, {**entry, "priors": [1]})
    fake = FakeStore()
    rec = fake_record(fake, "w")
    rec.init()
    admitted(rec)
    J = henge.spaces("rec")["journal"]

    def commit(who, op, space, key, value):
        return op == "put" and space == J

    # landed, its reply lost: the re-sent put meets Conflict with this entry
    fake.fault(commit, errors.Unavailable("reply lost"), after=True)
    n = item(rec, "landed")
    assert rec.get("items", n)["title"] == "landed"
    seq = rec.head()
    sent = [c for c in fake.log if c[1] == "put" and c[2] == J and c[3] == [seq]]
    # read back, not answered: put again with the version read, and answered
    assert len(sent) == 3
    assert isinstance(sent[1][4], errors.Conflict)
    assert not isinstance(sent[2][4], Exception)
    # not landed, twice: re-sent until it lands, and the result returned
    fake.fault(commit, errors.Unavailable("no answer"), times=2)
    m = item(rec, "re-sent")
    assert m == n + 1 and rec.get("items", m)["title"] == "re-sent"
    assert rec.check() == []
    # unreachable past the timeout, the entry landed: Unknown, and the record
    # closed
    rec.timeout_s = 0.3
    landed = []

    def first_commit(*a):
        if commit(*a) and not landed:
            landed.append(1)
            return True
        return False

    # the commit put lands, its reply is lost, and then the store answers
    # nothing at all
    fake.fault(first_commit, errors.Unavailable("reply lost"), after=True)
    fake.fault(lambda *a: bool(landed), errors.Unavailable("down"), times=10**6)
    with pytest.raises(Unknown):
        item(rec, "unknown")
    with pytest.raises(Unavailable, match="closed"):
        item(rec, "after")
    fake.faults.clear()
    again = fake_record(fake, "w2")
    assert [i["title"] for i in again.read("items")] == ["landed", "re-sent", "unknown"]
    assert again.check() == []
    # unreachable past the timeout, nothing landed: Unknown, and nothing to
    # finish
    again.timeout_s = 0.3
    fake.fault(commit, errors.Unavailable("no answer"), times=10**6)
    head = again.head()
    with pytest.raises(Unknown):
        item(again, "lost")
    fake.faults.clear()
    third = fake_record(fake, "w3")
    assert third.head() == head and third.check() == []


def _within(fake, start, timeout_s):
    """`start()` in a thread: (its result or error, the seconds it took). One
    still running past ten timeouts loops: every call then fails, which ends
    it, and the test reports the loop."""
    out = {}
    began = time.monotonic()
    thread = threading.Thread(target=lambda: out.update(r=_capture(start)))
    thread.daemon = True
    thread.start()
    thread.join(timeout_s * 10 + 5)
    took = time.monotonic() - began
    if thread.is_alive():
        fake.fault(lambda *a: True, errors.Unavailable("stopped"), times=10**9)
        thread.join(30)
        return "still looping", took
    return out.get("r"), took


@pytest.mark.parametrize(
    "path",
    [
        "record refused",
        "record conflict",
        "commit conflict",
        "ack refused",
        "open refused",
        "recovery refused",
    ],
)
def test_a_put_the_store_keeps_refusing_gives_up_at_the_timeout(path):
    """templet:3159 -- Henge: a put the store keeps refusing, or keeps
    answering Conflict, while its reads still answer, gives the operation up
    at the record's timeout: past the commit point Unknown, the record closed
    (rows 2-5, and the commit put); before it Unavailable (recovery's redo,
    rows 7-13)."""
    fake = FakeStore()
    s = henge.spaces("rec")
    rec = fake_record(fake, "w")
    rec.init()
    admitted(rec)
    item(rec, "first")
    rec.ack("c", 1)
    timeout_s = rec.timeout_s = 0.5

    def puts(space_name, key0=None):
        m = _in(space_name, key0)
        return lambda who, op, space, key, value: op == "put" and m(
            who, space, key, value
        )

    def refuse(match, exc):
        fake.fault(match, exc, times=10**9)

    def reopen():
        return fake_record(fake, "w2", timeout_s=timeout_s)

    want, start = Unknown, lambda: item(rec, "second")
    if path == "record refused":
        refuse(puts("rec", "items"), errors.Unavailable("refused"))
    elif path == "record conflict":
        refuse(puts("rec", "items"), errors.Conflict("moved"))
    elif path == "commit conflict":
        refuse(puts("journal"), errors.Conflict("moved"))
    elif path == "ack refused":
        refuse(puts("rec", "cursors"), errors.Unavailable("refused"))
        want, start = Unavailable, lambda: rec.ack("c", rec.head())
    elif path == "open refused":
        rec.close()
        refuse(
            _and(puts("state"), lambda w, op, sp, k, v: k == ["writer"]),
            errors.Unavailable("refused"),
        )
        want, start = Unavailable, reopen
    else:
        # an entry committed and not applied: the next open redoes it
        rec.store = Crashing(
            rec.store, lambda space, key: space == s["journal"], after=True
        )
        with pytest.raises(Crash):
            item(rec, "crashed")
        abandon(rec)
        refuse(puts("rec", "items"), errors.Unavailable("refused"))
        want, start = Unavailable, reopen
    got, took = _within(fake, start, timeout_s)
    # it waited the timeout out, and gave up well before ten of them (a
    # wall-clock bound any tighter flakes on a loaded host)
    assert type(got) is want, got
    assert took >= timeout_s, took
    if want is Unknown:
        assert rec._closed is not None


def _counted(fake, op, space_name, key=None, *, exc, tries):
    """Every `op` of the space (and key) refused with `exc`, each try's time
    appended to `tries`."""
    space = henge.spaces("rec")[space_name]

    def match(who, o, sp, k, value):
        hit = o == op and sp == space and (key is None or k == key)
        if hit:
            tries.append(time.monotonic())
        return hit

    fake.fault(match, exc, times=10**9)


@pytest.mark.parametrize("path", ["read", "put", "commit put", "sync"])
def test_a_store_that_refuses_at_once_is_tried_again_after_a_pause(path, monkeypatch):
    """templet:3159 -- Henge: a call the store refuses at once is tried again
    after a pause, each pause the double of the one before, up to `TRY_MS`,
    until the record's timeout: a read retried, a put, the commit put and the
    sync. The store is not hit as fast as the loop turns."""
    monkeypatch.setattr(henge, "TRY_MS", 200)
    fake = FakeStore()
    rec = fake_record(fake, "w")
    rec.init()
    admitted(rec)
    item(rec, "first")
    timeout_s = rec.timeout_s = 1.0
    tries = []
    refused = errors.Unavailable("refused at once")
    start = lambda: item(rec, "second")  # noqa: E731
    if path == "read":
        rec.close()
        _counted(fake, "get", "state", ["writer"], exc=refused, tries=tries)
        start = lambda: fake_record(fake, "w2", timeout_s=timeout_s)  # noqa: E731
    elif path == "put":
        _counted(fake, "put", "rec", exc=refused, tries=tries)
    elif path == "commit put":
        _counted(fake, "put", "journal", exc=refused, tries=tries)
    else:
        _counted(fake, "sync", "journal", exc=refused, tries=tries)
    got, took = _within(fake, start, timeout_s)
    assert isinstance(got, Unavailable), got
    assert took >= timeout_s, took
    # the last pause is cut short by the deadline
    gaps = [b - a for a, b in zip(tries, tries[1:])][:-1]
    assert 5 <= len(tries) <= 20, (len(tries), gaps)
    assert gaps[0] < 0.1 and max(gaps) >= 0.15, gaps
    assert max(gaps) <= 0.2 + 0.25, gaps


@pytest.mark.parametrize(
    "path,exc,want",
    [
        ("record put", errors.TooLarge, Unknown),
        ("commit put", errors.Invalid, Unknown),
        ("ack", errors.Forbidden, Unavailable),
        ("read", errors.Forbidden, Unavailable),
    ],
)
def test_a_refusal_for_good_gives_up_at_once(path, exc, want):
    """templet:3159 -- Henge: a refusal the store gives for good (TooLarge,
    Invalid, Forbidden) is not tried again: the operation gives up at once,
    as it would at the timeout (past the commit point Unknown, the record
    closed; before it Unavailable)."""
    fake = FakeStore()
    rec = fake_record(fake, "w")
    rec.init()
    admitted(rec)
    item(rec, "first")
    timeout_s = rec.timeout_s = 5.0
    tries = []
    refused = exc("refused for good")
    start = lambda: item(rec, "second")  # noqa: E731
    if path == "record put":
        _counted(fake, "put", "rec", exc=refused, tries=tries)
    elif path == "commit put":
        _counted(fake, "put", "journal", exc=refused, tries=tries)
    elif path == "ack":
        _counted(fake, "put", "rec", ["cursors", "c"], exc=refused, tries=tries)
        start = lambda: rec.ack("c", 1)  # noqa: E731
    else:
        rec.close()
        _counted(fake, "get", "state", ["writer"], exc=refused, tries=tries)
        start = lambda: fake_record(fake, "w2", timeout_s=timeout_s)  # noqa: E731
    got, took = _within(fake, start, timeout_s)
    assert type(got) is want, got
    assert len(tries) == 1, tries
    assert took < timeout_s / 2, took
    if want is Unknown:
        assert rec._closed is not None


# -- the mirror: a put one copy did not take ----------------------------------------


class OneCopyMisses:
    """A mirror's faults: once `arm(space)` is called, w1 refuses the
    replicate of the next put to that space, so the leader, w0, has applied
    the put and answers it `Unavailable` ("written here, held by 1 of the 2
    copies"); `lose(made)` loses w0 for good (its thread raises, and every
    restart of it fails, as with a faulted volume)."""

    def __init__(self):
        self.space = None
        self.refused = 0
        self._lose = threading.Event()
        self._lost = threading.Event()

    def arm(self, space):
        self.space = space.encode()

    def hook(self, made):
        from henge.core import envelope

        if made.config.name == "w0":
            if self._lost.is_set():
                raise RuntimeError("w0: its start made to fail")
            real_round = made.housekeeping
            lose = self._lose

            def housekeeping():
                if lose.is_set():
                    lose.clear()
                    raise RuntimeError("w0: made to raise")
                real_round()

            made.housekeeping = housekeeping
            return
        real = made.answer

        async def answer(data, budget=None, by=None):
            asked = envelope.decode(data)
            records = (asked.get("args") or {}).get("records") or []
            space = self.space
            if (
                space is not None
                and asked.get("op") == "replicate"
                and any(space in bytes(r[0]["k"]) for r in records)
            ):
                self.space = None
                self.refused += 1
                return envelope.encode(
                    envelope.failed(
                        asked.get("id"), errors.Unavailable.code, "made to fail"
                    )
                )
            return await real(data, budget, by)

        made.answer = answer

    def lose(self, made):
        self._lost.set()
        self._lose.set()
        end = time.monotonic() + 30
        while mirror_state(made).get("worker") != "w1":
            assert time.monotonic() < end, mirror_state(made)
            time.sleep(0.05)
        # the file comes before the router's own view: until that follows, a
        # sync names w0 and is refused, and a record's open tries it only once
        client = Store.connect(f"ipc://{made.folder}/henge.sock")
        try:
            while True:
                try:
                    client.sync("quarrylith.lose.journal")
                    return
                except errors.Unavailable:
                    assert time.monotonic() < end, "no sync answered after the loss"
                    time.sleep(0.05)
        finally:
            client.close()


def _steered(monkeypatch, made):
    """Every record opened from here reads through `Copy`: the map returned
    says which worker's copy answers a space's reads alone."""
    reads = {}
    inner = henge._connect
    monkeypatch.setattr(henge, "_connect", lambda a, t: Copy(inner(a, t), made, reads))
    return reads


def _whole_on(url, reads, worker):
    """The record as `worker`'s copy alone holds it: events() from 0 has no
    gap up to head, and check() is clean."""
    reads.clear()
    reads[None] = worker
    reader = opened(url)
    try:
        seqs = [e.seq for e in reader.events(0)]
        assert seqs == list(range(1, reader.head() + 1)), seqs
        assert reader.check() == []
    finally:
        reader.close()
        reads.clear()


@pytest.mark.parametrize("path", ["commit read-back", "redo's entry", "skipped chunk"])
def test_on_a_mirror_the_writer_goes_on_only_past_puts_answered_to_it(
    path, monkeypatch
):
    """templet:3159 -- Henge, the mirror: w1 refuses the replicate of one put
    once, so the leader w0 applied it and answered it Unavailable. The writer
    reads the value back from w0 -- the commit put's own entry, the entry a
    redo starts from, or a chunk whose retry has nothing left to put -- and
    puts it again with the version read, answered, before it goes on. With
    the store's default scan pause and no worker lost, w1's copy alone then
    gives events() with no gap and a clean check()."""
    misses = OneCopyMisses()
    with in_process_mirror(worker_hook=misses.hook) as made:
        reads = _steered(monkeypatch, made)
        name = fresh_name("m")
        s = henge.spaces(name)
        url = f"henge:{made.folder}#{name}"
        rec = opened(url, timeout_s=1.0)
        rec.init()
        admitted(rec)
        item(rec, "first")
        seq = rec.head() + 1
        if path == "commit read-back":
            reads[s["journal"]] = "w0"
            misses.arm(s["journal"])
            item(rec, "second")
        elif path == "redo's entry":
            # the read-back answered by w1, which lacks it: the operation
            # is Unknown; the next open reads the entry from w0 and redoes it
            reads[s["journal"]] = "w1"
            misses.arm(s["journal"])
            with pytest.raises(Unknown):
                item(rec, "second")
            reads[s["journal"]] = "w0"
            rec = opened(url, timeout_s=1.0)
        else:
            reads[s["idx"]] = "w0"
            misses.arm(s["idx"])
            item(rec, "second")
        assert misses.refused == 1
        assert rec.head() == seq
        rec.close()
        # read before any later put of the same object ships it to w1 whole
        assert held_by(made, "w1", s["journal"], [seq]) is not None
        _whole_on(url, reads, "w1")


def test_on_a_mirror_a_commit_read_back_survives_the_leader_lost(monkeypatch):
    """templet:3159 -- Henge, the mirror: the commit put answered Unavailable
    (w1 refused its replicate) and read back from the leader, w0, is put
    again with the version read before the operation returns; w0 then lost
    for good, the record on w1 alone has no hole below head, and goes on."""
    misses = OneCopyMisses()
    with in_process_mirror(
        worker_hook=misses.hook,
        restart_s=0.3,
        # the scan never makes w1 whole within the test
        worker_settings={"scan_pause_ms": 600_000},
    ) as made:
        reads = _steered(monkeypatch, made)
        name = fresh_name("m")
        s = henge.spaces(name)
        url = f"henge:{made.folder}#{name}"
        rec = opened(url, timeout_s=10.0)
        rec.init()
        admitted(rec)
        item(rec, "first")
        reads[s["journal"]] = "w0"
        misses.arm(s["journal"])
        n = item(rec, "second")
        assert misses.refused == 1
        rec.close()
        reads.clear()
        misses.lose(made)
        again = opened(url, timeout_s=10.0)
        try:
            assert again.check() == []
            seqs = [e.seq for e in again.events(0)]
            assert seqs == list(range(1, again.head() + 1)), seqs
            assert again.get("items", n)["title"] == "second"
            item(again, "third")
            assert again.check() == []
        finally:
            again.close()


@pytest.mark.parametrize("lose", [False, True], ids=["no worker lost", "w0 lost"])
def test_on_a_mirror_an_open_settles_the_applied_it_reads(monkeypatch, lose):
    """templet:3159 -- Henge, the mirror: a writer dies once its put of
    `applied` is answered Unavailable (w1 refused its replicate), so the new
    `applied` is on the leader, w0, alone. The next open's recovery reads it
    from w0, finds no entry past it, and puts it again with the version read,
    answered, before the writer goes on past it: w1 never holds `head` past
    `applied`. With the leader then lost while the next operation's writer
    dies before its own `applied` put, an open on w1 alone redoes that
    operation."""
    misses = OneCopyMisses()
    with in_process_mirror(
        worker_hook=misses.hook,
        restart_s=0.3,
        # the scan never makes w1 whole within the test
        worker_settings={"scan_pause_ms": 600_000},
    ) as made:
        reads = _steered(monkeypatch, made)
        name = fresh_name("m")
        s = henge.spaces(name)
        url = f"henge:{made.folder}#{name}"
        rec = opened(url, timeout_s=5.0)
        rec.init()
        admitted(rec)
        item(rec, "first")
        seq = rec.head() + 1

        def applied(space, key):
            return space == s["state"] and key == ["applied"]

        misses.arm(s["state"])
        rec.store = Crashing(rec.store, applied, after=True)
        with pytest.raises(Crash):
            item(rec, "second")
        abandon(rec)
        assert misses.refused == 1
        assert held_by(made, "w1", s["state"], ["applied"]).value == seq - 1
        # the next open's recovery reads state from w0
        reads[s["state"]] = "w0"
        rec = opened(url, timeout_s=5.0)
        reads.clear()
        assert held_by(made, "w1", s["state"], ["applied"]).value == seq
        assert held_by(made, "w1", s["state"], ["head"]).value == seq
        if not lose:
            rec.close()
            _whole_on(url, reads, "w1")
            return
        rec.store = Crashing(rec.store, applied)
        with pytest.raises(Crash):
            item(rec, "third")
        abandon(rec)
        misses.lose(made)
        again = opened(url, timeout_s=10.0)
        try:
            assert again.head() == seq + 1
            assert [i["title"] for i in again.read("items")][-2:] == [
                "second",
                "third",
            ]
            assert again.check() == []
            item(again, "fourth")
            assert again.check() == []
        finally:
            again.close()


class _ArmAt:
    """A client that arms `misses` just before it sends the put that
    `match(space, key, value)` takes, so w1 refuses that put's replicate."""

    def __init__(self, inner, misses, match):
        self.inner, self.misses, self.match = inner, misses, match
        self.endpoint = getattr(inner, "endpoint", None)

    def put(self, space, key, value, *, expect=None):
        if self.match(space, list(key), value):
            self.misses.arm(space)
        return self.inner.put(space, key, value, expect=expect)

    def __getattr__(self, op):
        return getattr(self.inner, op)


def _settled_mirror(misses):
    # the scan never makes w1 whole within the test
    return in_process_mirror(
        worker_hook=misses.hook,
        restart_s=0.3,
        worker_settings={"scan_pause_ms": 600_000},
    )


def test_an_open_settles_the_head_it_reads(monkeypatch):
    """templet:3159 -- Henge, the mirror: a writer dies once its put of `head`
    is answered Unavailable (w1 refused its replicate), so the new `head` is
    on w0 alone. The next open's recovery reads it from w0 and puts it again
    with the version read, answered: w1 then holds `applied` and `head` at
    that seq, and its copy alone is whole."""
    misses = OneCopyMisses()
    with _settled_mirror(misses) as made:
        reads = _steered(monkeypatch, made)
        name = fresh_name("m")
        s = henge.spaces(name)
        url = f"henge:{made.folder}#{name}"
        rec = opened(url, timeout_s=5.0)
        rec.init()
        admitted(rec)
        item(rec, "first")
        seq = rec.head() + 1

        def head(space, key, value=None):
            return space == s["state"] and key == ["head"]

        rec.store = Crashing(_ArmAt(rec.store, misses, head), head, after=True)
        with pytest.raises(Crash):
            item(rec, "second")
        abandon(rec)
        assert misses.refused == 1
        assert held_by(made, "w1", s["state"], ["head"]).value == seq - 1
        reads[s["state"]] = "w0"
        rec = opened(url, timeout_s=5.0)
        reads.clear()
        rec.close()
        assert held_by(made, "w1", s["state"], ["applied"]).value == seq
        assert held_by(made, "w1", s["state"], ["head"]).value == seq
        _whole_on(url, reads, "w1")


def test_after_a_commit_conflict_the_next_commit_settles_the_applied_read():
    """templet:3159 -- Henge, row 6: a writer whose commit put meets another
    writer's entry recovers, and finds no entry past the `applied` it reads.
    It stays open, and its next operation puts that `applied` again, as read,
    before its commit put goes on past it."""
    fake = FakeStore()
    clock = Clock()
    s = henge.spaces("rec")
    w = fake_record(fake, "w", clock=clock)
    w.init()
    admitted(w)
    item(w, "w's first")
    x = fake_record(fake, "x", clock=clock)  # the lock bypassed
    item(x, "x's")
    a = x._high
    x.close()
    with pytest.raises(Unavailable, match=f"seq {a} was taken"):
        item(w, "w's second")
    assert w._closed is None and w._high == a
    start = len(fake.log)
    item(w, "w's third")
    mine = [c for c in fake.log[start:] if c[0] == "w" and c[1] == "put"]
    commit = next(
        i for i, c in enumerate(mine) if c[2] == s["journal"] and c[3] == [a + 1]
    )
    settles = [
        c
        for c in mine[:commit]
        if c[2] == s["state"]
        and c[3] == ["applied"]
        and not isinstance(c[4], Exception)
    ]
    assert len(settles) == 1, mine
    assert fake.value(s["state"], ["applied"]) == a + 1


def test_on_a_mirror_a_writer_settles_the_applied_read_after_a_commit_conflict(
    monkeypatch,
):
    """templet:3159 -- Henge, the mirror: a second live writer, x, commits seq
    and dies once its put of `applied` is answered Unavailable, so that
    `applied` is on w0 alone. w's commit put at seq meets x's entry, and its
    recovery reads `applied` from w0 with no entry past it. w's next operation
    puts that `applied` again, answered, before it commits past it. With w
    dead before its own `applied` put and the leader then lost, an open on w1
    alone redoes w's operation."""
    misses = OneCopyMisses()
    with _settled_mirror(misses) as made:
        reads = _steered(monkeypatch, made)
        name = fresh_name("m")
        s = henge.spaces(name)
        url = f"henge:{made.folder}#{name}"
        w = opened(url, timeout_s=5.0)
        w.init()
        admitted(w)
        item(w, "w's first")
        seq = w.head() + 1
        x = henge.HengeRecord(  # the lock bypassed
            henge._connect(f"ipc://{made.folder}/henge.sock", 5.0),
            name,
            actor="x",
            via="cli",
            timeout_s=5.0,
        )
        x.clock = Clock()

        def applied(space, key):
            return space == s["state"] and key == ["applied"]

        misses.arm(s["state"])
        x.store = Crashing(x.store, applied, after=True)
        with pytest.raises(Crash):
            item(x, "x's")
        abandon(x)
        assert misses.refused == 1
        reads[s["state"]] = "w0"
        with pytest.raises(Unavailable, match=f"seq {seq} was taken"):
            item(w, "w's second")
        reads.clear()
        assert w._closed is None
        top = seq + 1
        w.store = Crashing(
            w.store, lambda space, key: applied(space, key) and w._high == top
        )
        with pytest.raises(Crash):
            item(w, "w's third")
        abandon(w)
        assert held_by(made, "w1", s["state"], ["applied"]).value == seq
        misses.lose(made)
        again = opened(url, timeout_s=10.0)
        try:
            assert again.head() == top
            assert [i["title"] for i in again.read("items")][-2:] == [
                "x's",
                "w's third",
            ]
            assert again.check() == []
            item(again, "after")
            assert again.check() == []
        finally:
            again.close()


def _a_dump(made, n):
    src = opened(f"henge:{made.folder}#{fresh_name('src')}")
    src.init()
    admitted(src)
    for i in range(n):
        item(src, f"i{i}")
    dump = src.dump()
    src.close()
    return dump


def test_on_a_mirror_a_resumed_load_settles_the_loading_it_read(monkeypatch):
    """templet:3159 -- Henge, the mirror: a loader dies once its put of the
    load's start is answered Unavailable (w1 refused its replicate), so
    `loading` is on w0 alone. The next open reads it from w0, and its load of
    the same dump puts `loading` again, as read, answered, before its first
    event. With that load stopped part-way and the leader then lost, an open
    on w1 alone still refuses every other operation, and the load of the same
    dump finishes."""
    misses = OneCopyMisses()
    with _settled_mirror(misses) as made:
        reads = _steered(monkeypatch, made)
        dump = _a_dump(made, 6)
        name = fresh_name("m")
        s = henge.spaces(name)
        url = f"henge:{made.folder}#{name}"
        rec = opened(url, actor="loader", via="load", timeout_s=5.0)
        rec.init()

        def loading(space, key):
            return space == s["state"] and key == ["loading"]

        misses.arm(s["state"])
        rec.store = Crashing(rec.store, loading, after=True)
        with pytest.raises(Crash):
            rec.load(dump)
        abandon(rec)
        assert misses.refused == 1
        assert held_by(made, "w1", s["state"], ["loading"]) is None
        reads[s["state"]] = "w0"
        rec = opened(url, actor="loader", via="load", timeout_s=5.0)
        reads.clear()
        rec.store = Crashing(
            rec.store, lambda space, key: space == s["journal"] and key == [4]
        )
        with pytest.raises(Crash):
            rec.load(dump)
        abandon(rec)
        held = held_by(made, "w1", s["state"], ["loading"])
        assert held is not None and isinstance(held.value, dict), held
        misses.lose(made)
        again = opened(url, actor="loader", via="load", timeout_s=10.0)
        try:
            assert again.head() == 3
            with pytest.raises(Refused, match="load"):
                item(again, "mid-load")
            again.load(dump)
            assert again.head() == len(dump["journal"])
            assert again.check() == []
        finally:
            again.close()


def test_on_a_mirror_an_open_settles_the_schema_it_reads(monkeypatch):
    """templet:3159 -- Henge, the mirror: `init` dies once its put of
    `schema` is answered Unavailable (w1 refused its replicate), so `schema`
    is on w0 alone. The next open reads it from w0 and puts it again, as
    read, answered, before it goes on. With the leader then lost, the record
    on w1 alone is initialised, and goes on."""
    misses = OneCopyMisses()
    with _settled_mirror(misses) as made:
        reads = _steered(monkeypatch, made)
        name = fresh_name("m")
        s = henge.spaces(name)
        url = f"henge:{made.folder}#{name}"
        rec = opened(url, timeout_s=5.0)

        def schema_put(space, key, value=None):
            return space == s["state"] and key == ["schema"]

        rec.store = Crashing(
            _ArmAt(rec.store, misses, schema_put), schema_put, after=True
        )
        with pytest.raises(Crash):
            rec.init()
        abandon(rec)
        assert misses.refused == 1
        assert held_by(made, "w1", s["state"], ["schema"]) is None
        reads[s["state"]] = "w0"
        rec = opened(url, timeout_s=5.0)
        reads.clear()
        assert rec.initialised
        assert held_by(made, "w1", s["state"], ["schema"]) is not None
        admitted(rec)
        item(rec, "one")
        rec.close()
        misses.lose(made)
        again = opened(url, timeout_s=10.0)
        try:
            assert again.initialised
            item(again, "two")
            assert again.check() == []
            with pytest.raises(Refused, match="exists"):
                again.init()
        finally:
            again.close()


@pytest.mark.parametrize("read", ["null", "absent"])
def test_on_a_mirror_an_open_settles_a_loading_it_goes_on_past(monkeypatch, read):
    """templet:3159 -- Henge, the mirror: the open goes on past a `loading`
    that is null or absent, so it puts it again first. `null`: a loader dies
    once its put of the load's end (null) is answered Unavailable, so w1
    still holds the load in progress; the next open reads null from w0 and
    puts it again, answered, and with the leader then lost the record on w1
    alone takes operations. `absent`: a loader dies once its put of the
    load's start is answered Unavailable, so w0 alone holds it; an open that
    reads `loading` absent from w1 puts null `absent`, which the leader
    refuses, so it does not go on; read from w0, it finds the load in
    progress, and the load of the same dump finishes."""
    misses = OneCopyMisses()
    with _settled_mirror(misses) as made:
        reads = _steered(monkeypatch, made)
        dump = _a_dump(made, 3)
        name = fresh_name("m")
        s = henge.spaces(name)
        url = f"henge:{made.folder}#{name}"
        rec = opened(url, actor="loader", via="load", timeout_s=5.0)
        rec.init()
        puts = []

        def nth(space, key):
            if space == s["state"] and key == ["loading"]:
                puts.append(key)
                return len(puts) == (2 if read == "null" else 1)
            return False

        def armed(space, key, value):
            if space != s["state"] or key != ["loading"]:
                return False
            return value is None if read == "null" else isinstance(value, dict)

        rec.store = Crashing(_ArmAt(rec.store, misses, armed), nth, after=True)
        with pytest.raises(Crash):
            rec.load(dump)
        abandon(rec)
        assert misses.refused == 1
        if read == "null":
            assert isinstance(held_by(made, "w1", s["state"], ["loading"]).value, dict)
            reads[s["state"]] = "w0"
            rec = opened(url, timeout_s=5.0)
            reads.clear()
            assert held_by(made, "w1", s["state"], ["loading"]).value is None
            item(rec, "after the load")
            rec.close()
            misses.lose(made)
            again = opened(url, timeout_s=10.0)
            try:
                item(again, "after the leader")
                assert again.check() == []
            finally:
                again.close()
            return
        assert held_by(made, "w1", s["state"], ["loading"]) is None
        reads[s["state"]] = "w1"
        with pytest.raises(Unavailable):
            opened(url, timeout_s=2.0)
        reads[s["state"]] = "w0"
        rec = opened(url, actor="loader", via="load", timeout_s=5.0)
        reads.clear()
        try:
            with pytest.raises(Refused, match="load"):
                item(rec, "mid-load")
            rec.load(dump)
            assert rec.head() == len(dump["journal"])
            assert rec.check() == []
        finally:
            rec.close()


class _FirstFromLeader(Copy):
    """A client whose first read of one object the leader's copy, w0, answers,
    and every later read of it the copy that lacks it, w1. Every other read of
    that space w0 answers."""

    def __init__(self, inner, made, space, key):
        super().__init__(inner, made, {space: "w0"})
        self.space, self.key, self.n = space, key, 0

    def get(self, space, key):
        if space == self.space and list(key) == self.key:
            self.n += 1
            return held_by(self.made, "w0" if self.n == 1 else "w1", space, key)
        return super().get(space, key)


def _once(match):
    """`match`, true of the first put it takes and of no later one."""
    seen = []

    def once(space, key, value=None):
        if seen or not match(space, key):
            return False
        seen.append(key)
        return True

    return once


def _init_dead_at_schema(made, misses):
    """A record whose `init` died once its put of `schema` was answered
    Unavailable, so `schema` is on the leader, w0, alone."""
    name = fresh_name("m")
    s = henge.spaces(name)
    url = f"henge:{made.folder}#{name}"
    rec = opened(url, timeout_s=5.0)

    def at_schema(space, key, value=None):
        return space == s["state"] and key == ["schema"]

    rec.store = Crashing(_ArmAt(rec.store, misses, at_schema), at_schema, after=True)
    with pytest.raises(Crash):
        rec.init()
    abandon(rec)
    assert misses.refused == 1
    assert held_by(made, "w1", s["state"], ["schema"]) is None
    return s, url, at_schema


def test_on_a_mirror_an_open_settles_the_schema_it_first_read(monkeypatch):
    """templet:3159 -- Henge, the mirror: `schema` on w0 alone. The open's
    first read is answered by w0, and every later one by w1, which lacks it.
    The open is `initialised` from its first read, so it puts that value again
    with the version it read, answered, whatever a second read would say. With
    the leader then lost, the record on w1 alone is initialised, and goes
    on."""
    misses = OneCopyMisses()
    with _settled_mirror(misses) as made:
        s, url, _ = _init_dead_at_schema(made, misses)
        inner = henge._connect
        monkeypatch.setattr(
            henge,
            "_connect",
            lambda a, t: _FirstFromLeader(inner(a, t), made, s["state"], ["schema"]),
        )
        rec = opened(url, timeout_s=5.0)
        monkeypatch.setattr(henge, "_connect", inner)
        assert rec.initialised
        assert held_by(made, "w1", s["state"], ["schema"]) is not None
        admitted(rec)
        item(rec, "one")
        rec.close()
        misses.lose(made)
        again = opened(url, timeout_s=10.0)
        try:
            assert again.initialised
            item(again, "two")
            assert again.check() == []
            with pytest.raises(Refused, match="exists"):
                again.init()
        finally:
            again.close()


def test_on_a_mirror_an_open_does_not_go_on_when_a_retry_reads_the_schema_absent(
    monkeypatch,
):
    """templet:3159 -- Henge, the mirror: `schema` on w0 alone, and the open's
    put of it answered Unavailable too. The retry's reads are answered by w1,
    which lacks it, so the open puts the value it first read `absent`: the
    leader answers `Conflict`, and the open is refused at its timeout rather
    than going on. Read from w0, the next open settles it."""
    misses = OneCopyMisses()
    with _settled_mirror(misses) as made:
        s, url, at_schema = _init_dead_at_schema(made, misses)
        inner = henge._connect
        monkeypatch.setattr(
            henge,
            "_connect",
            lambda a, t: _FirstFromLeader(
                _ArmAt(inner(a, t), misses, _once(at_schema)),
                made,
                s["state"],
                ["schema"],
            ),
        )
        with pytest.raises(Unavailable, match="did not take the put"):
            opened(url, timeout_s=2.0)
        monkeypatch.setattr(henge, "_connect", inner)
        assert misses.refused == 2
        assert held_by(made, "w1", s["state"], ["schema"]) is None
        reads = _steered(monkeypatch, made)
        reads[s["state"]] = "w0"
        rec = opened(url, timeout_s=5.0)
        rec.close()
        assert held_by(made, "w1", s["state"], ["schema"]) is not None


def _split_dead_at_directory(made, misses):
    """A record of eight efforts whose ninth's split died once its directory
    put was answered Unavailable, so the directory is on w0 alone. Needs
    `small_chunks`."""
    name = fresh_name("m")
    s = henge.spaces(name)
    url = f"henge:{made.folder}#{name}"
    rec = opened(url, timeout_s=5.0)
    rec.init()
    ids = [f"e{i:02d}" for i in range(10, 18)]
    efforts(rec, ids)
    dkey = henge.ix_key(("efforts", "*", None), "dir")

    def at_dir(space, key, value=None):
        return space == s["idx"] and key == dkey

    rec.store = Crashing(_ArmAt(rec.store, misses, at_dir), at_dir, after=True)
    with pytest.raises(Crash):
        efforts(rec, ["e18"])
    abandon(rec)
    assert misses.refused == 1
    assert held_by(made, "w1", s["idx"], dkey) is None
    return s, url, ids + ["e18"], dkey, at_dir


@pytest.mark.parametrize("lose", [False, True], ids=["no worker lost", "w0 lost"])
def test_on_a_mirror_a_redo_settles_a_directory_it_reads_with_no_split_left(
    monkeypatch, small_chunks, lose
):
    """templet:3159 -- Henge, the mirror: a split's directory on w0 alone, and
    its writer dead. The redo reads that directory from w0, so it has no split
    left to do and shrinks the old chunk. It first puts the directory again,
    as read, answered: w1 holds it before the chunk that needs it shrinks.
    w1's copy alone then reads every effort, and with w0 lost the record has
    them all and goes on."""
    misses = OneCopyMisses()
    with _settled_mirror(misses) as made:
        reads = _steered(monkeypatch, made)
        s, url, ids, dkey, _ = _split_dead_at_directory(made, misses)
        reads[s["idx"]] = "w0"
        rec = opened(url, timeout_s=5.0)
        reads.clear()
        rec.close()
        assert held_by(made, "w1", s["idx"], dkey) is not None
        if not lose:
            _whole_on(url, reads, "w1")
            return
        misses.lose(made)
        again = opened(url, timeout_s=10.0)
        try:
            assert [e["id"] for e in again.read("efforts")] == ids
            assert again.check() == []
            efforts(again, ["e19"])
            assert [e["id"] for e in again.read("efforts")] == ids + ["e19"]
            assert again.check() == []
        finally:
            again.close()


def test_a_redo_puts_the_directory_it_reads_before_the_chunk_it_shrinks(small_chunks):
    """templet:3159 -- Henge, row 6: a directory read with no split left is put
    again before the old chunk shrinks, so no copy holds the shrunk chunk
    without the directory that lists the keys it lost."""
    fake = FakeStore()
    clock = Clock()
    s = henge.spaces("rec")
    w = fake_record(fake, "w", clock=clock)
    w.init()
    efforts(w, [f"e{i:02d}" for i in range(10, 18)])
    ix = ("efforts", "*", None)
    dkey = henge.ix_key(ix, "dir")
    w.store = Crashing(
        w.store, lambda space, key: space == s["idx"] and key == dkey, after=True
    )
    with pytest.raises(Crash):
        efforts(w, ["e18"])
    abandon(w)
    start = len(fake.log)
    after = fake_record(fake, "after", clock=clock)
    after.close()
    puts = [
        c[3]
        for c in fake.log[start:]
        if c[0] == "after" and c[1] == "put" and c[2] == s["idx"]
    ]
    assert puts.index(dkey) < puts.index(henge.ix_key(ix, 0))


def test_on_a_mirror_a_redo_does_not_go_on_when_a_retry_reads_the_directory_absent(
    monkeypatch, small_chunks
):
    """templet:3159 -- Henge, the mirror: the redo's put of the directory it
    read is answered Unavailable too, and the retry's reads are answered by
    w1, which lacks it. It puts `[]` `absent`, the leader answers `Conflict`,
    and the open is refused at its timeout: no empty directory lands, and no
    chunk shrinks past it."""
    misses = OneCopyMisses()
    with _settled_mirror(misses) as made:
        s, url, ids, dkey, at_dir = _split_dead_at_directory(made, misses)
        inner = henge._connect
        monkeypatch.setattr(
            henge,
            "_connect",
            lambda a, t: _FirstFromLeader(
                _ArmAt(inner(a, t), misses, _once(at_dir)), made, s["idx"], dkey
            ),
        )
        with pytest.raises(Unavailable):
            opened(url, timeout_s=2.0)
        monkeypatch.setattr(henge, "_connect", inner)
        assert misses.refused == 2
        assert held_by(made, "w1", s["idx"], dkey) is None
        assert held_by(made, "w0", s["idx"], dkey).value
        reads = _steered(monkeypatch, made)
        reads[s["idx"]] = "w0"
        rec = opened(url, timeout_s=5.0)
        reads.clear()
        rec.close()
        _whole_on(url, reads, "w1")


@pytest.mark.parametrize("kind", ["cursors", "beats"])
def test_on_a_mirror_a_new_cursor_or_beat_settles_a_directory_it_reads(
    monkeypatch, small_chunks, kind
):
    """templet:3159 -- Henge, the mirror: a cursor's or a beat's split, with no
    event and so no redo. Its directory is on w0 alone and its writer dead. The
    next open's next new rows read that directory from w0, and find no split.
    The first of them puts the directory again, as read, answered. With w0
    lost, the record on w1 alone has every row."""
    misses = OneCopyMisses()
    with _settled_mirror(misses) as made:
        reads = _steered(monkeypatch, made)
        name = fresh_name("m")
        s = henge.spaces(name)
        url = f"henge:{made.folder}#{name}"

        def touch(rec, n):
            if kind == "cursors":
                rec.ack(f"c{n}", 0)
            else:
                rec.beat(n)

        rec = opened(url, timeout_s=5.0)
        rec.init()
        rows = range(10, 18)
        for n in rows:
            touch(rec, n)
        dkey = henge.ix_key((kind, "*", None), "dir")

        def at_dir(space, key, value=None):
            return space == s["idx"] and key == dkey

        rec.store = Crashing(_ArmAt(rec.store, misses, at_dir), at_dir, after=True)
        with pytest.raises(Crash):
            touch(rec, 18)
        abandon(rec)
        assert misses.refused == 1
        assert held_by(made, "w1", s["idx"], dkey) is None
        reads[s["idx"]] = "w0"
        rec = opened(url, timeout_s=5.0)
        touch(rec, 18)
        touch(rec, 19)
        reads.clear()
        rec.close()
        assert held_by(made, "w1", s["idx"], dkey) is not None
        misses.lose(made)
        again = opened(url, timeout_s=10.0)
        try:
            assert len(again.dump()["bookkeeping"][kind]) == len(rows) + 2
            assert again.check() == []
        finally:
            again.close()


class _DownAfterLoadStart:
    """The store takes the load's start, its answer is lost, and it answers
    nothing more: a store down past the record's timeout."""

    def __init__(self, inner, space):
        self.inner, self.space, self.down = inner, space, False
        self.endpoint = getattr(inner, "endpoint", None)

    def _gate(self):
        if self.down:
            raise errors.Unavailable("down")

    def put(self, space, key, value, *, expect=None):
        self._gate()
        out = self.inner.put(space, key, value, expect=expect)
        if space == self.space and list(key) == ["loading"] and value is not None:
            self.down = True
            raise errors.Unavailable("the answer is lost")
        return out

    def get(self, space, key):
        self._gate()
        return self.inner.get(space, key)

    def get_many(self, space, keys):
        self._gate()
        return self.inner.get_many(space, keys)

    def sync(self, space):
        self._gate()
        return self.inner.sync(space)

    def durability(self, space):
        self._gate()
        return self.inner.durability(space)

    def __getattr__(self, op):
        return getattr(self.inner, op)


def test_a_load_whose_start_put_fails_closes_the_record(single):
    """templet:3159 -- Henge: a load's start put that fails may still be on the
    store, so the writer does not know whether `loading` is there. The record
    closes: no operation commits past it on that handle. The next open reads
    `loading`, refuses every other operation, and the same dump finishes."""
    source = opened(f"henge:{single.folder}#{fresh_name('src')}")
    source.init()
    admitted(source)
    item(source, "i0")
    dump = source.dump()
    source.close()
    name = fresh_name("t")
    url = f"henge:{single.folder}#{name}"
    rec = opened(url, actor="loader", via="load", timeout_s=1.0)
    rec.init()
    rec.store = _DownAfterLoadStart(rec.store, henge.spaces(name)["state"])
    with pytest.raises(Unavailable):
        rec.load(dump)
    assert rec._closed is not None
    with pytest.raises(Unavailable, match="closed"):
        item(rec, "past the load")
    again = opened(url, actor="loader", via="load", timeout_s=5.0)
    try:
        assert again.head() == 0
        with pytest.raises(Refused, match="load"):
            item(again, "mid-load")
        again.load(dump)
        assert again.head() == len(dump["journal"])
        assert again.check() == []
    finally:
        again.close()


def test_a_load_start_after_an_open_checks_the_journal_first():
    """templet:3159 -- Henge, row 9: the open has put `loading` itself, so the
    load's start needs no fresh read, and still checks `journal/[head+1]`
    before it puts. A second writer's entry found there stops the load before
    `loading` is put: the record closes, and the next open's redo of that
    entry leaves a record that takes operations."""
    clock = Clock()
    dump = _source_dump(clock)
    fake = FakeStore()
    s = henge.spaces("rec")
    first = fake_record(fake, "first", clock=clock)
    first.init()
    first.close()
    x = fake_record(fake, "x", clock=clock)  # the lock bypassed
    w = fake_record(fake, "w", clock=clock)
    x.store = Crashing(x.store, lambda space, key: space == s["rec"])
    with pytest.raises(Crash):
        admitted(x)
    abandon(x)
    with pytest.raises(Unavailable, match=r"journal/\[1\] is there"):
        w.load(dump)
    assert w._closed is not None
    assert fake.value(s["state"], ["loading"]) is None
    after = fake_record(fake, "after", clock=clock)
    try:
        assert after._loading is None and after.head() == 1
        item(after, "after")
        assert after.head() == 2
    finally:
        after.close()


def test_a_failed_settle_leaves_applied_for_the_next_commit():
    """templet:3159 -- Henge, row 6: after a step-2 `Conflict`, the put of
    `applied` before the next commit is refused until the timeout. That
    operation raises `Unavailable` and did not happen, the record stays open,
    and the next operation still puts `applied` before its commit put."""
    fake = FakeStore()
    clock = Clock()
    s = henge.spaces("rec")
    w = fake_record(fake, "w", clock=clock, timeout_s=0.2)
    w.init()
    admitted(w)
    item(w, "w's first")
    x = fake_record(fake, "x", clock=clock)  # the lock bypassed
    item(x, "x's")
    a = x._high
    x.close()
    with pytest.raises(Unavailable, match=f"seq {a} was taken"):
        item(w, "w's second")
    refusing = [True]

    def refused(who, op, space, key, value):
        return (
            refusing
            and who == "w"
            and op == "put"
            and space == s["state"]
            and key == ["applied"]
        )

    fake.fault(refused, errors.Unavailable("refused"), times=10**9)
    with pytest.raises(Unavailable, match="did not take the put"):
        item(w, "refused")
    assert w._closed is None and w._high == a
    assert fake.value(s["journal"], [a + 1]) is None
    refusing.clear()
    start = len(fake.log)
    item(w, "w's third")
    mine = [c for c in fake.log[start:] if c[0] == "w" and c[1] == "put"]
    commit = next(
        i for i, c in enumerate(mine) if c[2] == s["journal"] and c[3] == [a + 1]
    )
    settles = [
        c
        for c in mine[:commit]
        if c[2] == s["state"]
        and c[3] == ["applied"]
        and not isinstance(c[4], Exception)
    ]
    assert len(settles) == 1, mine


def test_a_loading_changed_under_a_resumed_load_closes_the_record():
    """templet:3159 -- Henge, the resumable load: an open finds a load in
    progress, and another writer ends it before this one's load resumes. The
    resumed load's put of `loading` meets `Conflict`, its fresh read finds no
    load, and the record closes, with no event of the dump committed."""
    clock = Clock()
    dump = _source_dump(clock)
    fake = FakeStore()
    s = henge.spaces("rec")
    first = fake_record(fake, "first", clock=clock)
    first.init()
    first.store = Crashing(first.store, lambda space, key: space == s["journal"])
    with pytest.raises(Crash):
        first.load(dump)
    abandon(first)
    w = fake_record(fake, "w", clock=clock)
    assert w._loading is not None
    x = fake_record(fake, "x", clock=clock)  # the lock bypassed
    x._put(x.S, ["loading"], lambda cur: None, why="load")
    x.close()
    with pytest.raises(Unavailable, match="the load in progress changed"):
        w.load(dump)
    assert w._closed is not None
    assert top(fake) == 0


def test_on_a_mirror_a_resumed_load_refuses_events_that_are_not_the_dumps(monkeypatch):
    """templet:3159 -- Henge, the mirror: an operation's commit put is on w0
    alone and its writer dead. An open and a load's start read `journal/[1]`
    from w1 and find an empty record. The load's first commit meets that entry,
    and recovery redoes it. The load of the same dump, resumed, compares the
    record's events with the dump's and refuses, committing nothing."""
    misses = OneCopyMisses()
    with _settled_mirror(misses) as made:
        reads = _steered(monkeypatch, made)
        dump = _a_dump(made, 3)
        name = fresh_name("m")
        s = henge.spaces(name)
        url = f"henge:{made.folder}#{name}"
        rec = opened(url, timeout_s=2.0)
        rec.init()

        def first(space, key, value=None):
            return space == s["journal"] and key == [1]

        rec.store = Crashing(_ArmAt(rec.store, misses, first), first, after=True)
        with pytest.raises(Crash):
            efforts(rec, ["orphan"])
        abandon(rec)
        assert misses.refused == 1
        reads[s["journal"]] = "w1"
        rec = opened(url, actor="loader", via="load", timeout_s=2.0)
        assert rec.head() == 0

        def from_the_commit_on(space, key, value=None):
            # the commit's read-back, past the load's check, finds the entry
            if space == s["journal"]:
                reads[s["journal"]] = "w0"
            return False

        rec.store = _ArmAt(rec.store, misses, from_the_commit_on)
        with pytest.raises(Unavailable, match="seq 1 was taken"):
            rec.load(dump)
        reads.clear()
        rec.close()
        rec = opened(url, actor="loader", via="load", timeout_s=2.0)
        try:
            assert rec.head() == 1
            with pytest.raises(Refused, match="are not the dump's"):
                rec.load(dump)
            assert rec.head() == 1
        finally:
            rec.close()


# -- the resumable load ------------------------------------------------------------


def test_a_load_stopped_part_way_goes_on_with_the_same_dump(single, tmp_path):
    """templet:3159 -- Henge: a load stopped part-way continues from head() + 1
    at the next open when given the same dump, refuses a different dump, and
    refuses every other operation until it ends."""
    source = opened(single.url())
    source.init()
    admitted(source)
    for i in range(6):
        n = item(source, f"i{i}")
        source.comment(n, f"c{i}")
    source.ack("follower", 4)
    dump = source.dump()
    source.close()
    url = single.url()
    rec = opened(url, actor="loader", via="load")
    rec.init()
    s = henge.spaces(rec.name)
    rec.store = Crashing(
        rec.store, lambda space, key: space == s["journal"] and key == [6]
    )
    with pytest.raises(Crash):
        rec.load(dump)
    abandon(rec)
    rec = opened(url, actor="loader", via="load")
    assert rec.head() == 5
    for op in (
        lambda: admitted(rec, "0002"),
        lambda: rec.ack("follower", 1),
        lambda: rec.beat(1),
        lambda: rec.comment(1, "between"),
    ):
        with pytest.raises(Refused, match="load"):
            op()
    smaller = {**dump, "journal": dump["journal"][:-2]}
    view = MemoryView()
    for raw_event in smaller["journal"]:
        apply(view, Event.from_dict(raw_event))
    smaller = {**smaller, "records": view.records(), "bookkeeping": {}}
    with pytest.raises(Refused, match="another dump"):
        rec.load(smaller)
    rec.load(dump)
    assert rec.dump() == dump
    assert rec.check() == []
    item(rec, "after")
    rec.close()


# -- chunks -------------------------------------------------------------------------


def _bulk(kind, rows, seq):
    puts = tuple((kind, schema.key_of(kind, r), r) for r in rows)
    return Event(
        seq=seq,
        at=1,
        actor="bulk",
        via="load",
        kind="import",
        subject=None,
        data={},
        puts=puts,
    ).to_dict()


def test_a_chunk_splits_past_its_bounds_and_a_read_spans_chunks():
    """templet:3159 -- Henge: a chunk splits past 8,192 keys or 512 KiB, a
    read across chunks gives every key in order, and get_many is never called
    with more than 1,000 keys."""
    assert not henge.over([[i] for i in range(8192)])
    assert henge.over([[i] for i in range(8193)])
    long = [["k" * 240 + f"{i:05d}"] for i in range(2200)]
    assert len(long) < 8192 and henge.over(long)
    assert not henge.over(long[:1900])
    cut = henge.pieces([[i] for i in range(9000)])
    assert [len(p) for p in cut] == [4096, 4096, 808]
    assert all(schema.size(p) <= henge.PIECE_BYTES for p in henge.pieces(long))
    # 9,000 texts in one event, and 2,200 long keys in two
    texts = [{"number": i, "body": ""} for i in range(1, 9001)]
    metas = [{"key": "k" * 240 + f"{i:05d}", "value": i} for i in range(2200)]
    journal = [_bulk("item_text", texts, 1)] + [
        _bulk("meta", metas[i : i + 550], 2 + i // 550) for i in range(0, 2200, 550)
    ]
    view = MemoryView()
    for raw_event in journal:
        apply(view, Event.from_dict(raw_event))
    dump = {
        "format": 1,
        "records": view.records(),
        "journal": journal,
        "bookkeeping": {"beats": [], "cursors": []},
    }
    fake = FakeStore()
    rec = fake_record(fake, "w", via="load")
    rec.init()
    rec.load(dump)
    idx = henge.spaces("rec")["idx"]
    tdir = fake.value(idx, henge.ix_key(("item_text", "*", None), "dir"))
    assert [n for n, _ in tdir] == [1, 2]
    mdir = fake.value(idx, henge.ix_key(("meta", "*", None), "dir"))
    assert len(mdir) >= 1
    for n in [0] + [n for n, _ in mdir]:
        chunk = fake.value(idx, henge.ix_key(("meta", "*", None), n))
        assert schema.size(chunk) <= henge.CHUNK_BYTES
    fresh = fake_record(fake, "r")
    assert fresh.read("item_text") == texts
    assert fresh.read("meta") == sorted(metas, key=lambda m: m["key"])
    assert fresh.dump() == dump
    sizes = [len(c[3]) for c in fake.log if c[1] == "get_many"]
    assert sizes and max(sizes) <= 1000


# -- check()'s Henge findings ----------------------------------------------------------


def test_a_chunk_of_keys_that_escape_long_is_measured():
    """templet:3159 -- Henge: a chunk's 512 KiB are `schema.size`'s, so a
    chunk of few keys whose text escapes long (a control character counts six
    bytes) still splits by its bytes."""
    long = [["\x01" * 256, "\x01" * 256, "\x01" * 250 + f"{i:06d}"] for i in range(470)]
    assert schema.size(long) > henge.CHUNK_BYTES
    assert henge.over(long)
    assert not henge.over(long[:60])


def test_the_journal_index_keeps_its_width_whatever_the_split_bound(monkeypatch):
    """templet:3159 -- Henge: a subject's journal chunk is `seq // 8192`, on
    disk, whatever bound a record's chunk splits at."""
    monkeypatch.setattr(henge, "CHUNK_KEYS", 4)
    monkeypatch.setattr(henge, "PIECE_KEYS", 2)
    fake = FakeStore()
    rec = fake_record(fake, "w")
    rec.init()
    admitted(rec)
    n = item(rec, "one")
    for i in range(10):
        rec.comment(n, f"c{i}")
    s = henge.spaces("rec")
    keys = [k for sp, k in fake.keys() if sp == s["idx"] and k[0] == "journal"]
    assert {k[-1] for k in keys} == {0}, keys
    # the item's subject: its filing and its ten comments
    by = {k[1]: fake.value(s["idx"], k) for k in keys}
    subject, seqs = max(by.items(), key=lambda p: len(p[1]))
    assert len(seqs) > 4
    assert [e.seq for e in rec.events(0, subject=subject)] == seqs


def test_check_reports_a_hole_a_record_past_applied_and_a_bad_chunk():
    """templet:3159 -- Henge: check() reports a journal hole, a record past
    applied and an index chunk that disagrees with the records."""
    fake = FakeStore()
    rec = fake_record(fake, "w")
    rec.init()
    admitted(rec)
    n = item(rec)
    item(rec, "two")
    assert rec.check() == []
    s = henge.spaces("rec")
    raw = fake.client("raw")

    def put(space, key, value):
        got = raw.get(space, key)
        expect = {"absent": True} if got is None else {"version": got.version}
        raw.put(space, key, value, expect=expect)

    # an index chunk that disagrees: an item's state index loses it
    state = rec.get("items", n)["state"]
    ix = henge.ix_key(("items", "state", state), 0)
    put(s["idx"], ix, [k for k in fake.value(s["idx"], ix) if k != [n]])
    rec._forget()
    found = rec.check()
    assert any(f"index of items.state = {state!r}" in f for f in found), found
    put(s["idx"], ix, sorted(fake.value(s["idx"], ix) + [[n]]))
    rec._forget()
    assert rec.check() == []
    # a record past applied
    record = dict(fake.value(s["rec"], ["items", n]), _seq=rec.head() + 5)
    put(s["rec"], ["items", n], record)
    rec._forget()
    assert any("past applied" in f for f in rec.check())
    record["_seq"] = rec.head()
    put(s["rec"], ["items", n], record)
    # a hole: applied and head past an absent entry
    head = rec.head()
    put(s["state"], ["applied"], head + 2)
    put(s["state"], ["head"], head + 2)
    put(s["journal"], [head + 2], fake.value(s["journal"], [head]))
    rec.close()
    again = fake_record(fake, "w2")
    found = again.check()
    assert any(f"journal/[{head + 1}] is absent" in f for f in found), found


# -- no read lists a space ----------------------------------------------------


def test_no_read_lists_a_space(single):
    """templet:3159 -- Henge: no read lists a space (a store fake that refuses
    listing runs the whole suite): every Henge record of the suite reaches
    its store through `Guarded`, which refuses a listing, a delete and a
    get_many past 1,000 keys."""
    rec = opened(single.url())
    assert isinstance(rec.store, Guarded)
    for op in ("rows", "delete", "trash", "scan_keys", "history", "read", "usage"):
        with pytest.raises(Refusal):
            getattr(rec.store, op)
    with pytest.raises(Refusal):
        rec.store.get_many("x", [[i] for i in range(1001)])
    rec.init()
    admitted(rec)
    item(rec)
    assert rec.dump()["records"]["items"]
    rec.close()


# -- two live writers, every row of the put table -----------------------------

#: The record's page, whose table "Every put the writer makes" the runs follow.
PAGE = Path(__file__).resolve().parents[1] / "docs" / "templets" / "3159-the-record.md"


def page_rows():
    """The put table's rows, read from the page: {n: its put path}."""
    text = PAGE.read_text(encoding="utf-8")
    _, _, table = text.partition("**Every put the writer makes**")
    rows = {}
    for line in table.splitlines():
        m = re.match(r"\s*\| (\d+) \| ([^|]+?) \|", line)
        if m:
            rows[int(m.group(1))] = m.group(2)
        elif rows and not line.strip().startswith("|"):
            break
    return rows


#: How the held writer's operation ends, per row run through `race`: the
#: error it raises (Unknown past the commit point, else Unavailable), or None
#: for a success.
OUTCOMES = {
    1: Unavailable,
    2: Unknown,
    3: Unknown,
    4: Unknown,
    5: Unknown,
    9: Unavailable,
    10: Unknown,
    11: None,
    12: Unavailable,
    13: Unavailable,
}


class Writers:
    """Two writers, `w1` and `w2`, on one record of a fake (the lock
    bypassed), each opened again when it closes. It checks, after every
    operation, that applied, head and every cursor never step back."""

    def __init__(self, fake, clock):
        self.fake, self.clock = fake, clock
        self.n = itertools.count()
        self.live = {}
        self.ended = []  # (who, the error)
        self.errors = []  # (who, the error, its record or None)
        self.seen = {}
        self.s = henge.spaces("rec")

    def open(self, tag):
        who = f"{tag}.{next(self.n)}"
        try:
            rec = fake_record(self.fake, who, clock=self.clock)
        except (Unavailable, Refused) as e:
            self.ended.append((who, e))
            self.errors.append((who, e, None))
            return None
        self.live[tag] = (who, rec)
        return rec

    def get(self, tag):
        if tag in self.live:
            who, rec = self.live[tag]
            if rec._closed is None:
                return rec
            del self.live[tag]
        return self.open(tag)

    def run(self, tag, op):
        rec = self.get(tag)
        if rec is None:
            return None
        who = self.live[tag][0]
        try:
            return op(rec)
        except Refused:
            return None
        except Unavailable as e:
            self.errors.append((who, e, rec))
            if rec._closed is not None:
                self.ended.append((who, e))
                self.live.pop(tag, None)
            return e
        finally:
            self.monotone()

    def monotone(self):
        now = {
            "applied": self.fake.value(self.s["state"], ["applied"]),
            "head": self.fake.value(self.s["state"], ["head"]),
        }
        for space, key in self.fake.keys():
            if space == self.s["rec"] and key[0] == "cursors":
                now[tuple(key)] = self.fake.value(space, key)["seq"]
            if space == self.s["rec"] and key[0] == "beats":
                now[tuple(key)] = self.fake.value(space, key)["at"]
        for k, v in now.items():
            if v is None:
                continue
            assert v >= self.seen.get(k, v), (k, v, self.seen[k])
            self.seen[k] = v

    def committed_past(self, tag, seq, tries=8, touch=False):
        """`tag` runs operations until it has committed past `seq`. With
        `touch`, each moves the newest item, so the event past `seq` writes
        that item's record and its state's index chunks: what the other
        writer, held, may then put over."""

        def op(r, i):
            items = r.read("items")
            if not touch or not items:
                return r.note("past", f"{tag}-{seq}-{i}")
            last = items[-1]
            to = "blocked" if last["state"] == "ready" else "ready"
            return r.move(last["number"], to, "past")

        for i in range(tries):
            self.run(tag, lambda r: op(r, i))
            rec = self.live.get(tag, (None, None))[1]
            if rec is not None and rec._high > seq and top(self.fake) > seq:
                return
        raise AssertionError(f"{tag} did not commit past {seq}")

    def interleave(self, count, seed):
        rng = random.Random(seed)
        for i in range(count):
            tag = rng.choice(["w1", "w2"])
            self.clock.step(rng.randint(1, 50))
            self.run(tag, lambda r: _some_op(r, rng, tag, i))

    def finish(self):
        # a writer that found another writer live closed: it puts nothing more
        for who, error, rec in self.errors:
            if "another writer is live" in str(error) and rec is not None:
                assert rec._closed is not None, (who, error)
        for who, rec in list(self.live.values()):
            rec.close()
        reader = fake_record(self.fake, "reader", clock=self.clock)
        assert reader.check() == []
        dump = reader.dump()
        reader.close()
        # a writer that ended in Unknown or Unavailable put nothing after its
        # last read that found a journal entry it had not put (a put the
        # store refused is not one it put)
        J = self.s["journal"]
        for who, error in self.ended:
            mine = [c for c in self.fake.log if c[0] == who]
            put_seqs = {
                tuple(c[3])
                for c in mine
                if c[1] == "put" and c[2] == J and not isinstance(c[4], Exception)
            }
            found = [
                i
                for i, c in enumerate(mine)
                if c[1] == "get"
                and c[2] == J
                and c[4] is not None
                and tuple(c[3]) not in put_seqs
            ]
            if found and isinstance(error, (Unknown, Unavailable)):
                later = [c for c in mine[found[-1] + 1 :] if c[1].startswith("put")]
                assert later == [], (who, error, later)
        return dump


def _some_op(rec, rng, tag, i):
    if rec.get("efforts", "0001") is None:
        return admitted(rec)
    items = rec.read("items")
    pick = rng.random()
    if not items or pick < 0.3:
        return item(rec, f"{tag} {i}")
    n = rng.choice(items)["number"]
    if pick < 0.45:
        return rec.comment(n, f"{tag} says {i}")
    if pick < 0.6:
        return rec.edit_item(n, title=f"{tag} edit {i}")
    if pick < 0.75:
        return rec.move(
            n, rng.choice(["blocked", "ready", "claimed", "done", "triage"]), "moved"
        )
    if pick < 0.85:
        return rec.ack(f"c{rng.randint(1, 2)}", rng.randint(0, rec.head()))
    if pick < 0.93:
        return rec.beat(rng.randint(1, 3))
    return rec.grant(n, tag, branch="b", machine="m")


def _who(tag):
    return lambda who, space, key, value: who == tag or who.startswith(tag + ".")


def race(
    writers,
    tag,
    action,
    match,
    *,
    op="put",
    late=False,
    past=True,
    during=None,
    touch=False,
):
    """`action(record of tag)` in a thread, its next call `match` takes held
    back; the other writer does `during` and commits past it, then the call
    is released."""
    other = "w2" if tag == "w1" else "w1"
    who = writers.live[tag][0]
    held = writers.fake.hold(match, op=op, late=late)
    result = {}

    def run():
        try:
            result["value"] = action()
        except BaseException as e:  # reported to the test
            result["error"] = e

    thread = threading.Thread(target=run)
    thread.start()
    assert held.reached.wait(30), "the held call was never made"
    if during is not None:
        during()
    if past:
        writers.committed_past(other, top(writers.fake), touch=touch)
    held.release()
    thread.join(30)
    assert not thread.is_alive()
    # the held writer is checked like any other that ended
    if isinstance(result.get("error"), (Unknown, Unavailable)):
        writers.ended.append((who, result["error"]))
    writers.monotone()
    return result


def _setup(fake, clock, *, init=True, items=3):
    writers = Writers(fake, clock)
    w2 = writers.open("w2")
    if init:
        w2.init()
        admitted(w2)
        for i in range(items):
            item(w2, f"setup {i}")
    return writers


def _in(space_name, key0=None, last=None):
    s = henge.spaces("rec")

    def match(who, space, key, value):
        if space != s[space_name]:
            return False
        if key0 is not None and key[0] != key0:
            return False
        if last is not None and key[-1] != last:
            return False
        return True

    return match


def _and(*matches):
    return lambda *a: all(m(*a) for m in matches)


def _nth(match, n):
    count = [0]

    def nth(*a):
        if match(*a):
            count[0] += 1
            return count[0] == n
        return False

    return nth


def _run_row(fake, clock, row, case):
    """One run of one row: its held put, then the interleaving. Returns the
    record's dump at the end."""
    if row == 8:
        writers = _setup(fake, clock, init=False)
        w1 = writers.open("w1")
        who = writers.live.pop("w1")[0]

        def both_init():
            writers.get("w2").init()
            admitted(writers.get("w2"))
            writers.committed_past("w2", 0)

        held = fake.hold(_and(_who("w1"), _in("state")))
        result = {}
        thread = threading.Thread(target=lambda: result.update(r=_capture(w1.init)))
        thread.start()
        assert held.reached.wait(30)
        both_init()
        held.release()
        thread.join(30)
        assert isinstance(result["r"], Unavailable), result
        writers.ended.append((who, result["r"]))
    elif row in (9, 10, 11):
        writers = _setup(fake, clock, init=False)
        writers.get("w2").init()
        source = _source_dump(clock)
        w1 = writers.open("w1")
        if row == 9:
            match = _and(_who("w1"), _in("state"), lambda w, sp, k, v: k == ["loading"])
        elif row == 10:
            match = _and(_who("w1"), _nth(_in("rec", "items"), 3))
        else:
            match = _and(
                _who("w1"),
                _in("state"),
                lambda w, sp, k, v: k == ["loading"] and v is None,
            )
        result = race(writers, "w1", lambda: w1.load(source), match)
        _ended_as(row, result, w1)
    elif row == 6:
        writers = _setup(fake, clock)
        if case == "two entries":
            a = top(fake)
            held = fake.hold(
                _and(_who("w1"), _in("journal"), lambda w, sp, k, v: k == [a + 1]),
                op="get",
            )
            result = {}
            thread = threading.Thread(
                target=lambda: result.update(r=_capture(lambda: writers.open("w1")))
            )
            thread.start()
            assert held.reached.wait(30)
            writers.committed_past("w2", a + 1)
            held.release()
            thread.join(30)
            assert "w1" not in writers.live
            assert isinstance(writers.ended[-1][1], Unavailable)
            assert "two entries" in str(writers.ended[-1][1])
        else:
            # w2 commits and is slow at its first record put; w1 opens and
            # redoes that entry, held at its own put number `case`
            w2 = writers.get("w2")
            slow = fake.hold(_and(_who("w2"), _in("rec")))
            t2 = threading.Thread(target=lambda: _capture(lambda: item(w2, "slow")))
            t2.start()
            assert slow.reached.wait(30)
            mine = fake.hold(_nth(_and(_who("w1"), lambda w, sp, k, v: True), case))
            result = {}
            t1 = threading.Thread(
                target=lambda: result.update(r=_capture(lambda: writers.open("w1")))
            )
            t1.start()
            while not mine.reached.is_set() and t1.is_alive():
                time.sleep(0.005)
            reached = mine.reached.is_set()
            slow.release()
            t2.join(30)
            if reached:
                writers.committed_past("w2", top(fake), touch=True)
                mine.release()
            t1.join(30)
            writers.monotone()
            if not reached:
                fake.holds.remove(mine)
                return None
    elif row == 7:
        writers = _setup(fake, clock)
        held = fake.hold(
            _and(_who("w1"), _in("state"), lambda w, sp, k, v: k == ["writer"])
        )
        result = {}
        t1 = threading.Thread(
            target=lambda: result.update(r=_capture(lambda: writers.open("w1")))
        )
        t1.start()
        assert held.reached.wait(30)
        writers.committed_past("w2", top(fake))
        writers.get("w2").close()
        writers.open("w2")  # puts writer anew
        writers.committed_past("w2", top(fake))
        held.release()
        t1.join(30)
    else:
        split = row == 3 and case != "chunk"
        writers = _setup(fake, clock, items=8 if split else 3)
        w1 = writers.open("w1")
        if row == 1:
            match = _and(_who("w1"), _in("journal"))
            action = lambda: item(w1, "w1's")  # noqa: E731
        elif row == 2:
            match = _and(_who("w1"), _in("rec", "items"))
            action = lambda: item(w1, "w1's")  # noqa: E731
        elif row == 3:
            ix = ("items", "*", None)
            match = _and(
                _who("w1"),
                _in("idx"),
                {
                    "chunk": lambda w, sp, k, v: k == ["items", "state", "ready", 0],
                    "new chunk": lambda w, sp, k, v: k == henge.ix_key(ix, 1),
                    "directory": lambda w, sp, k, v: k == henge.ix_key(ix, "dir"),
                    "shrunk chunk": lambda w, sp, k, v: k == henge.ix_key(ix, 0)
                    and isinstance(v, list)
                    and len(v) <= henge.PIECE_KEYS,
                }[case],
            )
            action = lambda: item(w1, "w1's")  # noqa: E731
        elif row == 4:
            match = _and(_who("w1"), _in("state"), lambda w, sp, k, v: k == [case])
            action = lambda: item(w1, "w1's")  # noqa: E731
        elif row == 5:
            first = _and(_who("w1"), _in("rec", "items"))
            fake.fault(
                lambda w, op, sp, k, v: op == "put" and first(w, sp, k, v),
                errors.Unavailable("no answer"),
            )
            match = first
            action = lambda: item(w1, "w1's")  # noqa: E731
        during = None
        if row == 12:
            w1.ack("c1", 1)
            match = _and(_who("w1"), _in("rec", "cursors"))
            action = lambda: w1.ack("c1", 2)  # noqa: E731

            def acks():
                writers.run("w2", lambda r: r.ack("c1", r.head()))

            during = acks

        elif row == 13:
            w1.beat(1)
            match = _and(_who("w1"), _in("rec", "beats"))
            action = lambda: w1.beat(1)  # noqa: E731

            def beats():
                clock.step(5000)
                writers.run("w2", lambda r: r.beat(1))

            during = beats

        result = race(
            writers, "w1", action, match, during=during, touch=row in (2, 3, 5)
        )
        _ended_as(row, result, w1)
    writers.interleave(200, seed=row * 1000 + sum(map(ord, str(case))))
    return writers.finish()


def _ended_as(row, result, rec):
    """The held writer's operation ended as the row says: its error's own
    class (an Unknown is also an Unavailable), or a success; past the commit
    point, an Unknown leaves the record closed."""
    want, got = OUTCOMES[row], result.get("error")
    if want is None:
        assert got is None, (row, got)
        return
    assert type(got) is want, (row, got)
    if want is Unknown:
        assert rec._closed is not None, (row, got)


def _capture(fn):
    try:
        return fn()
    except BaseException as e:  # reported to the test
        return e


def _source_dump(clock):
    fake = FakeStore()
    rec = fake_record(fake, "source", clock=clock)
    rec.init()
    admitted(rec)
    for i in range(4):
        n = item(rec, f"loaded {i}")
        rec.comment(n, "c")
    return rec.dump()


RUNS = [
    (1, None),
    (2, None),
    (3, "chunk"),
    (3, "new chunk"),
    (3, "directory"),
    (3, "shrunk chunk"),
    (4, "applied"),
    (4, "head"),
    (5, None),
    (6, "each of its puts"),
    (6, "two entries"),
    (7, None),
    (8, None),
    (9, None),
    (10, None),
    (11, None),
    (12, None),
    (13, None),
]


def test_every_row_of_the_put_table_has_a_run():
    """templet:3159 -- the two-writer test lists the table's rows and fails if
    a row has no run."""
    rows = page_rows()
    assert sorted(rows) == list(range(1, len(rows) + 1)) and len(rows) >= 13, rows
    assert sorted({row for row, _ in RUNS}) == sorted(rows)
    raced = {row for row, _ in RUNS} - {6, 7, 8}
    assert sorted(OUTCOMES) == sorted(raced)


@pytest.mark.parametrize("row,case", RUNS, ids=[f"row{r}-{c}" for r, c in RUNS])
def test_two_live_writers_never_tear_the_record(row, case, small_chunks):
    """templet:3159 -- Henge, two live writers, every row of the put table:
    two writers on one record (the lock bypassed for the second) run 200
    interleaved operations over a store fake that can hold back any one put
    and release it later, once per row, each time holding back a put of that
    row until the other writer has committed past it. Each run: the replay
    equals the records, check() is clean, applied, head and every cursor
    never step back, and a writer that ends in Unknown or Unavailable made no
    put after a check found journal/[high+1]."""
    if case == "each of its puts":
        # an open whose recovery is held at each of its own puts in turn,
        # until it makes no more
        dumps = []
        for k in itertools.count(1):
            dump = _run_row(FakeStore(), Clock(), row, k)
            if dump is None:
                break
            dumps.append(dump)
        assert len(dumps) >= 10
    else:
        dumps = [_run_row(FakeStore(), Clock(), row, case)]
    for dump in dumps:
        # the interleaving ran: both writers' operations, past the setup's
        assert len(dump["journal"]) >= 50


# -- late requests from a writer that has gone --------------------------------


@pytest.mark.parametrize(
    "what",
    ["record", "chunk", "directory", "applied", "head", "cursor", "beat", "commit"],
)
def test_a_late_request_of_a_gone_writer_never_overwrites(what, small_chunks):
    """templet:3159 -- Henge: a late request from a writer that has gone (held
    back, then released after a new writer committed) never overwrites a
    newer value, whatever the object (a record, a chunk, a directory,
    applied, head, a cursor, a beat), and a late commit put at a seq the new
    writer took is recovered as that entry."""
    fake = FakeStore()
    clock = Clock()
    s = henge.spaces("rec")
    old = fake_record(fake, "old", clock=clock)
    old.init()
    admitted(old)
    for i in range(8):
        item(old, f"i{i}")
    old.ack("c", 1)
    old.beat(1)
    matches = {
        "record": _in("rec", "items"),
        "chunk": _and(_in("idx", "items"), lambda w, sp, k, v: k[-1] == 0),
        "directory": _and(_in("idx", "items"), lambda w, sp, k, v: k[-1] == "dir"),
        "applied": _and(_in("state"), lambda w, sp, k, v: k == ["applied"]),
        "head": _and(_in("state"), lambda w, sp, k, v: k == ["head"]),
        "cursor": _in("rec", "cursors"),
        "beat": _in("rec", "beats"),
        "commit": _in("journal"),
    }
    held = fake.hold(_and(_who("old"), matches[what]), late=True)
    # the old writer's put is late; it then dies before it puts again
    gone = Crashing(old.store, lambda space, key: True)
    old.store = _Then(old.store, gone)
    clock.step()
    try:
        if what == "cursor":
            old.ack("c", old.head())
        elif what == "beat":
            old.beat(1)
        else:
            item(old, "the old writer's ninth")  # splits the items' chunk 0
    except (Crash, Unavailable):
        pass
    assert held.reached.is_set()
    seq = old._high
    abandon(old)
    new = fake_record(fake, "new", clock=clock)
    clock.step(1000)
    for i in range(3):
        item(new, f"new {i}")
    new.ack("c", new.head())
    new.beat(1)
    key = held.call[2]
    space = held.call[1]
    before = fake.value(space, key)
    held.release()
    assert isinstance(held.result, errors.Conflict), held.result
    assert fake.value(space, key) == before
    if what == "commit":
        # the entry at the old writer's seq is the new writer's own
        assert fake.value(s["journal"], [seq + 1])["event"]["actor"] == "new"
    reader = fake_record(fake, "reader", clock=clock)
    assert reader.check() == []
    assert reader.cursor("c") == new.head()
    assert reader.get("beats", 1)["at"] == clock()


def test_a_late_commit_that_lands_first_is_recovered_as_that_entry():
    """templet:3159 -- the late commit put lands before the new writer
    commits: the new writer's open recovers it as that entry."""
    fake = FakeStore()
    clock = Clock()
    old = fake_record(fake, "old", clock=clock)
    old.init()
    admitted(old)
    held = fake.hold(_and(_who("old"), _in("journal")), late=True)
    old.store = _Then(old.store, Crashing(old.store, lambda space, key: True))
    with pytest.raises((Crash, Unavailable)):
        item(old, "late")
    abandon(old)
    held.release()
    assert not isinstance(held.result, Exception)
    new = fake_record(fake, "new", clock=clock)
    assert [i["title"] for i in new.read("items")] == ["late"]
    assert new.check() == []


class _Then:
    """The first put through `first` (a held one), every later one through
    `then` (the writer dies)."""

    def __init__(self, first, then):
        self.first, self.then, self.used = first, then, False
        self.endpoint = getattr(first, "endpoint", None)

    def put(self, *a, **k):
        if self.used:
            return self.then.put(*a, **k)
        try:
            return self.first.put(*a, **k)
        except errors.Unavailable:
            self.used = True
            raise

    def __getattr__(self, op):
        return getattr(self.first, op)


@pytest.mark.parametrize("what", ["chunk", "applied", "head"])
def test_a_retry_with_nothing_left_to_put_still_checks_the_journal(what):
    """templet:3159 -- Henge, row 5: a retry whose fresh read leaves nothing
    to put (another writer put the same value) still checks journal/[seq+1]
    first; finding it, the writer puts nothing more, closes the record, and
    the operation raises Unknown."""
    fake = FakeStore()
    s = henge.spaces("rec")
    rec = fake_record(fake, "w")
    rec.init()
    admitted(rec)
    item(rec, "first")
    match = {
        "chunk": _and(_in("idx", "items"), lambda w, sp, k, v: k[-1] == 0),
        "applied": _and(_in("state"), lambda w, sp, k, v: k == ["applied"]),
        "head": _and(_in("state"), lambda w, sp, k, v: k == ["head"]),
    }[what]
    held = fake.hold(_and(_who("w"), match))
    result = {}
    thread = threading.Thread(
        target=lambda: result.update(r=_capture(lambda: item(rec, "second")))
    )
    thread.start()
    assert held.reached.wait(30)
    _, space, key, value, _ = held.call
    seq = rec._high
    # another writer has put the same value, and committed the next seq
    other = fake.client("other")
    got = other.get(space, key)
    expect = {"absent": True} if got is None else {"version": got.version}
    other.put(space, key, value, expect=expect)
    entry = {"event": "another writer's", "priors": []}
    other.put(s["journal"], [seq + 1], entry, expect={"absent": True})
    held.release()
    thread.join(30)
    assert isinstance(result["r"], Unknown), result
    assert rec._closed is not None
    mine = [c for c in fake.log if c[0] == "w"]
    found = [
        i
        for i, c in enumerate(mine)
        if c[1] == "get" and c[2] == s["journal"] and c[3] == [seq + 1]
    ]
    assert found and [c for c in mine[found[-1] + 1 :] if c[1].startswith("put")] == []


def test_monotone_values_keep_their_order_under_a_retry():
    """templet:3159 -- Henge: applied and head are put only upward, a cursor
    takes the larger of the value read and its own, and a beat the later at:
    each put retried after a Conflict whose fresh read shows a value moved on
    with no new journal entry (so the check passes) keeps that value."""
    fake = FakeStore()
    clock = Clock()
    rec = fake_record(fake, "w", clock=clock)
    rec.init()
    admitted(rec)
    rec.ack("c", 1)
    rec.beat(1)
    s = henge.spaces("rec")
    raw = fake.client("raw")

    def move(space, key, value):
        got = raw.get(space, key)
        raw.put(space, key, value, expect={"version": got.version})

    # applied and head moved on by hand: the next operation leaves them
    high = rec.head()
    move(s["state"], ["applied"], high + 5)
    move(s["state"], ["head"], high + 5)
    item(rec, "after")
    assert fake.value(s["state"], ["applied"]) == high + 5
    assert fake.value(s["state"], ["head"]) == high + 5
    # a cursor and a beat written past this writer's, while its put waits
    for kind, op, value, field in (
        (
            "cursors",
            lambda: rec.ack("c", 2),
            {"consumer": "c", "seq": 3, "at": 1},
            "seq",
        ),
        ("beats", lambda: rec.beat(1), {"run": 1, "at": clock() + 60_000}, "at"),
    ):
        key = [kind, *schema.key_of(kind, value)]
        held = fake.hold(_and(_who("w"), _in("rec", kind)))
        thread = threading.Thread(target=lambda: _capture(op))
        thread.start()
        assert held.reached.wait(30)
        move(s["rec"], key, value)
        held.release()
        thread.join(30)
        assert fake.value(s["rec"], key)[field] == value[field]
