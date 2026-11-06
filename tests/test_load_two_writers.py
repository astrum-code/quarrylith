# SPDX-License-Identifier: Apache-2.0
"""A second writer against a load in batches, the lock bypassed (templet:3159,
invariant 13): "Even two live writers never tear the record".

Each run stops the loader at one of its calls, for every call, and runs another
handle there: a fresh open, three of them, or a writer opened before the load
began, trying to commit. The loader then goes on. A batch is what makes these
differ from a commit: it has many entries, so a second open can come between
them, and a batch's edits can net to nothing against a chunk the loader holds as
its own put, or split a chunk one way where a part of the batch would split it
another. The record is never torn: `check()` finds a load in progress or nothing,
and every read by an index agrees with the records."""

import json
import threading
from pathlib import Path

import pytest
from backends import Clock, admitted
from hengestore import FakeStore, own_store
from loader import Point
from test_load_batches import a_dump, on_fake

from quarrylith.record import henge, schema
from quarrylith.record.contract import Refused, Unavailable, Unknown, open_record

ENDED = (Refused, Unavailable, Unknown)


def moved_and_back(folder, batch):
    """A dump one of whose batches moves an item out of its state and back, so
    that its edits net to nothing on the state's chunk, which the loader holds
    as its own put from the batch before. Returns the dump, the item, its state
    and the seq of the move out."""
    Path(folder).mkdir(parents=True, exist_ok=True)
    rec = open_record(f"sqlite:///{folder}/moved.sqlite", actor="alpha", via="cli")
    rec.clock = Clock()
    rec.init()
    admitted(rec)
    first = [
        rec.file_item(
            "task", f"i{i}", "b", filed_by="alpha", effort="0001", role="alpha"
        )
        for i in range(3)
    ]
    while rec.head() % batch:
        rec.comment(first[1], "pad")
    state = rec.get("items", [first[0]])["state"]
    rec.comment(first[1], "first of the batch")
    rec.move(first[0], "blocked", "waits")
    out_at = rec.head()
    rec.move(first[0], state, "unblocked")
    rec.comment(first[1], "last of the batch")
    dump = rec.dump()
    rec.close()
    return dump, first[0], state, out_at


class Interrupting:
    """A client that runs `act()` before its k-th call of the store, any call
    but `close`, on the caller's own thread."""

    def __init__(self, inner, k, act):
        self.inner, self.k, self.act = inner, k, act
        self.endpoint = getattr(inner, "endpoint", None)
        self.calls = 0
        self.acted = False

    def __getattr__(self, op):
        call = getattr(self.inner, op)
        if op == "close":
            return call

        def counted(*a, **kw):
            self.calls += 1
            if self.calls == self.k:
                self.acted = True
                self.act()
            return call(*a, **kw)

        return counted


@pytest.fixture
def small_chunks(monkeypatch):
    """Chunks of 4 keys, split into pieces of 2: a split in a few events."""
    monkeypatch.setattr(henge, "CHUNK_KEYS", 4)
    monkeypatch.setattr(henge, "PIECE_KEYS", 2)
    monkeypatch.setattr(henge, "JOURNAL_CHUNK", 4)


def not_torn(fake, dump, returned):
    """What a loader that returned must leave: the whole dump, and every read
    by an index the records' own. One that did not return leaves a load in
    progress, found as that and as nothing else."""
    rec = on_fake(fake, "reader")
    try:
        findings = rec.check()
        others = [f for f in findings if "a load is in progress" not in f]
        assert others == [], others
        loading = fake.value(henge.spaces("rec")["state"], ["loading"])
        assert bool(findings) == isinstance(loading, dict), findings
        if returned:
            assert not isinstance(loading, dict)
            got = rec.dump()
            assert got["journal"][: len(dump["journal"])] == dump["journal"]
        elif isinstance(loading, dict):
            return rec.head()
        for kind, spec in schema.RECORDS.items():
            rows = rec.read(kind)
            for field in spec.match:
                for value in {json.dumps(r[field]) for r in rows}:
                    v = json.loads(value)
                    want = [r for r in rows if r[field] == v]
                    assert rec.read(kind, **{field: v}) == want, (kind, field, v)
    finally:
        rec.close()
    return None


def schedule(dump, act_of):
    """Stop the loader before its k-th call, for every k, with `act_of(fake)`
    run there. Each run ends `not_torn`. Returns what the runs ended as."""
    out = {"returned": 0, "ended": 0, "runs": 0}
    k = 1
    while True:
        fake = FakeStore()
        rec = on_fake(fake, "w0")
        rec.init()
        rec.close()
        act = act_of(fake)
        client = Interrupting(fake.client("w1"), None, act)
        w1 = henge.HengeRecord(client, "rec", actor="w1", via="load", timeout_s=1.0)
        w1.clock = Clock()
        client.k = client.calls + k
        returned = False
        try:
            w1.load(dump)
            returned = True
        except ENDED:
            pass
        w1.close()
        if not client.acted:
            assert returned
            return out
        not_torn(fake, dump, returned)
        out["returned" if returned else "ended"] += 1
        out["runs"] += 1
        k += 1


def opens_three_times(fake):
    """Three fresh opens, the lock bypassed; each may redo, and each is
    refused any operation while the load is in progress."""

    def act():
        for i in range(3):
            try:
                rec = on_fake(fake, f"w2.{i}")
            except ENDED:
                continue
            try:
                rec.note("past", f"note {i}")
            except ENDED:
                pass
            rec.close()

    return act


@pytest.mark.parametrize("batch", [2, 3, 8, 1000])
def test_a_second_open_at_every_call_of_a_load_with_a_net_nothing(
    batch, tmp_path, monkeypatch
):
    """templet:3159 -- Henge, invariant 13: a batch that moves an item out of its
    state and back nets to nothing on a chunk the loader holds as its own put.
    Fresh opens at every call of the load, between the batch's commit puts
    included, and the load still returns with every index the records'."""
    monkeypatch.setattr(henge, "LOAD_BATCH", batch)
    dump = moved_and_back(tmp_path, batch)[0]
    out = schedule(dump, opens_three_times)
    assert out["ended"] == 0, out
    assert out["runs"] > 10


@pytest.mark.parametrize("batch", [2, 3, 8, 1000])
def test_a_second_open_at_every_call_of_a_load_that_splits(
    batch, tmp_path, monkeypatch, small_chunks
):
    """templet:3159 -- Henge, invariant 13: chunks small enough that a batch
    splits them. A part of a batch would split a chunk otherwise than the whole
    does, and a directory keeps the first entry it holds for a chunk's number,
    so a redo of a part would lose keys from the load's. Fresh opens at every
    call of the load, and every key is there, by every index."""
    monkeypatch.setattr(henge, "LOAD_BATCH", batch)
    dump = a_dump(tmp_path / "source", items=6)
    out = schedule(dump, opens_three_times)
    assert out["ended"] == 0, out


@pytest.mark.parametrize("batch", [2, 8, 1000])
def test_a_writer_open_before_the_load_at_every_call_of_it(
    batch, tmp_path, monkeypatch, small_chunks
):
    """templet:3159 -- Henge, invariant 13: a writer opened before the load began
    does not read `loading`, and tries to commit at every call of the load. It
    takes the next seq only ahead of the load's entry, and the load then stops;
    past that it is stopped. Never torn, and a load that returned is whole."""
    monkeypatch.setattr(henge, "LOAD_BATCH", batch)
    dump = a_dump(tmp_path / "source", items=4)

    def act_of(fake):
        other = on_fake(fake, "w2")

        def act():
            for i in range(3):
                try:
                    other.note("past", f"note {i}")
                except ENDED:
                    pass

        return act

    out = schedule(dump, act_of)
    assert out["runs"] > 10


class Held:
    """A client stopped before its j-th put, until `go`."""

    def __init__(self, inner, j):
        self.inner, self.j, self.puts = inner, j, 0
        self.endpoint = getattr(inner, "endpoint", None)
        self.reached, self.go = threading.Event(), threading.Event()

    def put(self, space, key, value, *, expect=None):
        self.puts += 1
        if self.puts == self.j:
            self.reached.set()
            assert self.go.wait(30)
        return self.inner.put(space, key, value, expect=expect)

    def __getattr__(self, op):
        return getattr(self.inner, op)


def a_redo_left_behind(dump, batch, j):
    """The loader is stopped once a batch is committed, a second open starts and
    stops before its j-th put, in its redo, and the loader then goes on to its
    end before the open does. Returns whether the open reached that put."""
    fake = FakeStore()
    rec = on_fake(fake, "w0")
    rec.init()
    rec.close()
    state = henge.spaces("rec")["state"]
    client = fake.client("w1")
    opened, result = {}, {}
    held = Held(fake.client("w2"), j)

    def second():
        try:
            opened["rec"] = henge.HengeRecord(
                held, "rec", actor="w2", via="load", timeout_s=1.0
            )
        except ENDED as e:
            opened["error"] = e

    thread = threading.Thread(target=second)

    class Starts:
        """Past the batch's commit point, once: the second open, in a thread."""

        def __init__(self, inner):
            self.inner, self.started = inner, False
            self.endpoint = "w1"

        def put(self, space, key, value, *, expect=None):
            out = self.inner.put(space, key, value, expect=expect)
            done = isinstance(value, dict) and value.get("to") == batch
            if space == state and list(key) == ["loading"] and done:
                self.started = True
                thread.start()
                for _ in range(2000):
                    if held.reached.wait(0.001) or not thread.is_alive():
                        break
            return out

        def __getattr__(self, op):
            return getattr(self.inner, op)

    w1 = henge.HengeRecord(Starts(client), "rec", actor="w1", via="load", timeout_s=1.0)
    w1.clock = Clock()
    try:
        w1.load(dump)
        result["returned"] = True
    except ENDED as e:
        result["returned"] = False
        result["error"] = e
    w1.close()
    reached = held.reached.is_set()
    held.go.set()
    thread.join(30)
    assert not thread.is_alive()
    if "rec" in opened:
        opened["rec"].close()
    not_torn(fake, dump, result["returned"])
    assert result["returned"], result
    return reached


@pytest.mark.parametrize("what", ["net nothing", "splits"])
def test_a_redo_stopped_while_the_load_goes_on_to_its_end(
    what, tmp_path, monkeypatch, small_chunks
):
    """templet:3159 -- Henge, invariant 13: a second open's redo of a committed
    batch, stopped before each of its puts while the loader goes on and ends.
    Its late puts meet their versions, or it finds the load past it and stops:
    the load returned, whole, with every index the records'."""
    monkeypatch.setattr(henge, "LOAD_BATCH", 8)
    if what == "net nothing":
        monkeypatch.setattr(henge, "CHUNK_KEYS", 8192)
        monkeypatch.setattr(henge, "PIECE_KEYS", 4096)
        dump = moved_and_back(tmp_path, 8)[0]
    else:
        dump = a_dump(tmp_path / "source", items=6)
    reached = 0
    for j in range(1, 80):
        if a_redo_left_behind(dump, 8, j):
            reached += 1
        else:
            break
    assert reached > 5


def test_two_loads_of_the_same_dump_at_once(tmp_path, monkeypatch, small_chunks):
    """templet:3159 -- Henge, invariant 13: a second handle loads the same dump
    at every call of the first. One or both end it, or one is refused, and the
    record is never torn; whichever returned, the dump is whole."""
    monkeypatch.setattr(henge, "LOAD_BATCH", 8)
    dump = a_dump(tmp_path / "source", items=4)

    def act_of(fake):
        def act():
            try:
                rec = on_fake(fake, "w2")
            except ENDED:
                return
            try:
                rec.load(dump)
            except ENDED:
                pass
            rec.close()

        return act

    out = schedule(dump, act_of)
    assert out["runs"] > 10


def test_the_tear_on_a_real_store(tmp_path, monkeypatch):
    """templet:3159 -- Henge, invariant 13: on a real `henge single`, the loader
    held between a batch's commit puts, with the move out as the last entry a
    second open can read and the move back next. The load returns and a read by
    the state's index holds the record."""
    monkeypatch.setattr(henge, "LOAD_BATCH", 8)
    dump, target, state, out_at = moved_and_back(tmp_path, 8)
    with own_store("single") as running:
        address = f"ipc://{running.folder}/henge.sock"
        from henge.store.client import Store

        first = henge.HengeRecord(Store.connect(address), "tear", actor="w", via="load")
        first.clock = Clock()
        first.init()
        first.close()
        journal = henge.spaces("tear")["journal"]
        point = Point(space="journal", key=[out_at + 1], after=False)

        def second_open():
            rec = henge.HengeRecord(
                Store.connect(address), "tear", actor="w2", via="load", timeout_s=5.0
            )
            assert rec.head() % 8 == 0
            rec.close()

        from loader import Killing

        loader = henge.HengeRecord(
            Killing(Store.connect(address), point, second_open),
            "tear",
            actor="w1",
            via="load",
        )
        loader.clock = Clock()
        assert journal
        loader.load(dump)
        assert loader.store.died
        loader.close()
        rec = henge.HengeRecord(
            Store.connect(address), "tear", actor="r", via="load", timeout_s=5.0
        )
        rec.clock = Clock()
        try:
            assert rec.dump() == dump
            assert rec.check() == []
            assert target in [r["number"] for r in rec.read("items", state=state)]
        finally:
            rec.close()
