# SPDX-License-Identifier: Apache-2.0
"""A load, written in batches (templet:3159, "The load's batches"): what a
batch writes, and that a load cut or killed anywhere resumes and ends with the
same dump.

Two ways of stopping it. On `FakeStore`, a **cut**: the store holds exactly the
first n puts of a load, for every n, which is what an ordered store can hold
after a power cut ("after a power cut, a write survives only with every write
answered before it was sent"). On a real store, a **kill**: SIGKILL of the
loader, in its own process (`loader.py`), or of the store, which is restarted
on its folder. A kill leaves the store's page cache where it was, so only the
cut says anything of what a sync is for."""

import json
import math
import subprocess
import sys
from pathlib import Path

import pytest
from backends import Clock, admitted, over_backends, scheme, url_for
from hengestore import FakeStore, own_store
from loader import Killing, Point

from quarrylith.record import henge, schema
from quarrylith.record.contract import Refused, Unavailable, Unknown, open_record

LOADER = Path(__file__).resolve().parent / "loader.py"
#: A dump of about three batches of this many events, on the real stores.
BATCH = 8


def a_dump(folder, items=4):
    """A whole dump, made on SQLite: every kind of index edit a load makes.
    Items are moved twice (a match field's old value leaves its index, and
    the same record is written more than once in a batch), the counters of
    `meta` are written by most events, and a cursor and a beat are its
    bookkeeping."""
    Path(folder).mkdir(parents=True, exist_ok=True)
    rec = open_record(
        f"sqlite:///{folder}/source.sqlite",
        actor="alpha",
        via="cli",
    )
    rec.clock = Clock()
    rec.init()
    admitted(rec)
    rec.declare_node("n1", ["process"], [])
    rec.set_capacity("n1", "slots", 8, "slots")
    last = None
    for i in range(items):
        n = rec.file_item(
            "task", f"i{i}", "body", filed_by="alpha", effort="0001", role="alpha"
        )
        rec.comment(n, f"first on {i}")
        rec.grant(n, "alpha", branch=f"feat/{n}", machine="m1")
        rec.move(n, "blocked", "waits")
        rec.comment(n, f"second on {i}")
        rec.clock.step()
        last = rec.file_task(
            "job", needs={"slots": 1}, effort="0001", sandbox="process"
        )
    run = rec.start_run(
        last, "alpha", node="n1", sandbox="process", fence=None, expect_s=9
    )
    rec.ack("follower", 3)
    rec.beat(run)
    dump = rec.dump()
    rec.close()
    return dump


@pytest.fixture
def dump(tmp_path):
    return a_dump(tmp_path / "source")


def on_fake(fake, who, name="rec", **kw):
    kw.setdefault("actor", "loader")
    kw.setdefault("via", "load")
    kw.setdefault("timeout_s", 1.0)
    rec = henge.HengeRecord(fake.client(who), name, **kw)
    rec.clock = Clock()
    return rec


class Spy:
    """A client that logs every call with its value, and that no call overlaps
    another."""

    def __init__(self, inner):
        self.inner = inner
        self.endpoint = getattr(inner, "endpoint", None)
        self.calls = []

    def put(self, space, key, value, *, expect=None):
        out = self.inner.put(space, key, value, expect=expect)
        self.calls.append(("put", space, list(key), value))
        return out

    def get(self, space, key):
        self.calls.append(("get", space, list(key), None))
        return self.inner.get(space, key)

    def get_many(self, space, keys):
        keys = [list(k) for k in keys]
        self.calls.append(("get_many", space, keys, None))
        return self.inner.get_many(space, keys)

    def sync(self, space):
        out = self.inner.sync(space)
        self.calls.append(("sync", space, None, None))
        return out

    def __getattr__(self, op):
        return getattr(self.inner, op)


def spied_load(dump, batch, monkeypatch):
    """`dump` loaded on a fake at `batch`, every call of the load logged."""
    monkeypatch.setattr(henge, "LOAD_BATCH", batch)
    fake = FakeStore()
    rec = on_fake(fake, "w")
    rec.init()
    rec.store = spy = Spy(rec.store)
    rec._forget()
    rec.load(dump)
    return fake, rec, spy


def journal_of(fake, name="rec"):
    space = henge.spaces(name)["journal"]
    return {k[0]: fake.value(space, k) for s, k in fake.keys() if s == space}


# -- what a batch writes -----------------------------------------------------


@pytest.mark.parametrize("batch", [1, 3, BATCH, 1000])
def test_a_load_writes_the_same_journal_at_any_batch(dump, batch, monkeypatch):
    """templet:3159 -- Henge: the event format does not change. Whatever the
    batch, every journal entry is the one a load one at a time writes, priors
    included (an event's priors see the events before it in its batch), and
    the record reads the same by every index."""
    want_fake, one, _ = spied_load(dump, 1, monkeypatch)
    fake, rec, _ = spied_load(dump, batch, monkeypatch)
    assert journal_of(fake) == journal_of(want_fake)
    assert rec.dump() == dump
    assert rec.check() == []
    for kind in schema.RECORDS:
        for field in schema.RECORDS[kind].match:
            for row in rec.read(kind):
                value = row[field]
                assert rec.read(kind, **{field: value}) == one.read(
                    kind, **{field: value}
                )


def test_a_batch_puts_each_object_once_and_syncs_once(dump, monkeypatch):
    """templet:3159 -- Henge: between two syncs a load puts no object twice:
    the records, the chunks, `applied` and `head` of a batch go once, a record
    at its batch's last value. One sync per batch and one at the end, not one
    per event. Nothing is sent in parallel: each put is answered before the
    next (`Spy` is a plain sequence on one thread, and the load makes no
    thread)."""
    _, _, spy = spied_load(dump, BATCH, monkeypatch)
    events = len(dump["journal"])
    syncs = [i for i, c in enumerate(spy.calls) if c[0] == "sync"]
    assert len(syncs) == math.ceil(events / BATCH) + 1, syncs
    start = 0
    for end in syncs + [len(spy.calls)]:
        puts = [
            (c[1], json.dumps(c[2]))
            for c in spy.calls[start:end]
            if c[0] == "put" and c[2] != ["loading"]
        ]
        assert len(puts) == len(set(puts)), sorted(puts)
        start = end


def test_a_load_makes_fewer_puts_than_one_at_a_time(tmp_path, monkeypatch):
    """templet:3159 -- Henge: the point of a batch. A load at `LOAD_BATCH`
    makes less than half the puts of one at a time."""
    dump = a_dump(tmp_path / "source", items=20)
    batch = henge.LOAD_BATCH
    one = spied_load(dump, 1, monkeypatch)[2]
    many = spied_load(dump, batch, monkeypatch)[2]

    def puts(spy):
        return sum(1 for c in spy.calls if c[0] == "put")

    assert puts(many) * 2 < puts(one), (puts(many), puts(one))


def test_a_batch_reads_what_it_needs_at_once(tmp_path, monkeypatch):
    """templet:3159 -- Henge: a batch reads the objects it will put with
    `get_many`, at most a few calls a batch, with no `get` an event."""
    dump = a_dump(tmp_path / "source", items=20)
    _, _, spy = spied_load(dump, BATCH, monkeypatch)
    events = len(dump["journal"])
    batches = math.ceil(events / BATCH)
    gets = [c for c in spy.calls if c[0] == "get"]
    # the load's start and its end: a check, the fresh read of `loading`, and
    # the bookkeeping's rows, with no `get` for an event
    assert len(gets) < events / 4, [c[1:3] for c in gets]
    many = [c for c in spy.calls if c[0] == "get_many"]
    assert len(many) <= 6 * batches + 40, len(many)


def test_a_batch_is_named_and_within_one_read():
    """templet:3159 -- Henge: the batch is one named constant, and a committed
    batch's entries come back in one `get_many`."""
    assert 1 <= henge.LOAD_BATCH <= henge.MANY
    assert henge.LOAD_BATCH * 50 <= henge.KEPT


def test_nothing_is_reported_before_a_sync_covers_it(dump, monkeypatch):
    """templet:3159 -- Henge: `head` is what a follower reads. It moves only
    past a sync that came after every entry, record, chunk and `applied` of
    the events it names, and only ever to a batch's last seq or the journal's
    end. The load's last call is a sync, after every put."""
    _, _, spy = spied_load(dump, BATCH, monkeypatch)
    events = len(dump["journal"])
    state = henge.spaces("rec")["state"]
    journal = henge.spaces("rec")["journal"]
    assert spy.calls[-1][0] == "sync"
    synced = -1  # the index of the last sync seen
    moves = []
    for i, (op, space, key, value) in enumerate(spy.calls):
        if op == "sync":
            synced = i
        if op == "put" and space == state and key == ["head"]:
            moves.append(value)
            assert synced >= 0
            before = spy.calls[:synced]
            for seq in range(1, value + 1):
                assert ("put", journal, [seq]) in [c[:3] for c in before]
            assert any(
                c[:3] == ("put", state, ["applied"]) and c[3] == value for c in before
            )
    assert moves[-1] == events
    assert all(m % BATCH == 0 or m == events for m in moves), moves
    assert moves == sorted(set(moves))


# -- a cut, at every put -----------------------------------------------------


def load_log(dump, batch, monkeypatch):
    """The puts of a load on a fake, in order, and of the `init` before it."""
    monkeypatch.setattr(henge, "LOAD_BATCH", batch)
    fake = FakeStore()
    rec = on_fake(fake, "w")
    rec.init()
    base = [(s, list(k), fake.value(s, k)) for s, k in fake.keys()]
    rec.store = spy = Spy(rec.store)
    rec.load(dump)
    return base, [c[1:] for c in spy.calls if c[0] == "put"]


def holding(puts):
    """A store that holds exactly `puts`."""
    fake = FakeStore()
    for space, key, value in puts:
        fake._apply_put("cut", space, key, value, None)
    return fake


def resumes(fake, dump):
    """A handle on what a cut left: `check()` calls it complete or resumable,
    never partial and silent, and it ends with the dump."""
    rec = on_fake(fake, "w2")
    head = rec.head()
    state = henge.spaces("rec")["state"]
    loading = fake.value(state, ["loading"])
    findings = rec.check()
    if isinstance(loading, dict):
        assert any("load is in progress" in f for f in findings), findings
        assert [f for f in findings if "load is in progress" not in f] == []
        rec.load(dump)
    elif head == len(dump["journal"]):
        assert findings == []
    else:
        assert head == 0
        assert findings == []
        rec.load(dump)
    assert rec.head() == len(dump["journal"])
    assert rec.dump() == dump
    assert rec.check() == []
    assert fake.value(state, ["loading"]) is None
    rec.close()


@pytest.fixture
def small_chunks(monkeypatch):
    """Chunks of 4 keys, split into pieces of 2: a split in a few events."""
    monkeypatch.setattr(henge, "CHUNK_KEYS", 4)
    monkeypatch.setattr(henge, "PIECE_KEYS", 2)
    monkeypatch.setattr(henge, "JOURNAL_CHUNK", 4)


def a_directory_put(puts):
    """The first put of a split's directory, as its index."""
    idx = henge.spaces("rec")["idx"]
    return next(i for i, p in enumerate(puts) if p[0] == idx and p[1][-1] == "dir")


@pytest.mark.parametrize("batch", [1, 3, BATCH, 1000])
def test_a_load_cut_at_every_put_resumes(dump, batch, monkeypatch, small_chunks):
    """templet:3159 -- Henge: a load stopped after any put, a split's included,
    opens, is called in progress by `check()` (or is empty, or whole), and the
    same dump ends it with the same dump."""
    base, puts = load_log(dump, batch, monkeypatch)
    assert a_directory_put(puts) is not None
    for n in range(len(puts) + 1):
        resumes(holding(base + puts[:n]), dump)


def test_a_resume_cut_at_every_put_resumes_again(dump, monkeypatch, small_chunks):
    """templet:3159 -- Henge: the recovery and the load that follow a cut are
    cut too: inside a first batch, past its entries, and in a later one."""
    base, puts = load_log(dump, BATCH, monkeypatch)
    journal = henge.spaces("rec")["journal"]
    ends = [i for i, p in enumerate(puts) if p[0] == journal]
    for first in (ends[2] + 1, ends[BATCH - 1] + 2, ends[BATCH + 1] + 1):
        left = base + puts[:first]
        fake = holding(left)
        spy_client = Spy(fake.client("w2"))
        rec = henge.HengeRecord(spy_client, "rec", actor="l", via="load", timeout_s=1.0)
        rec.clock = Clock()
        rec.load(dump)
        again = [c[1:] for c in spy_client.calls if c[0] == "put"]
        for n in range(len(again) + 1):
            resumes(holding(left + again[:n]), dump)


def test_a_redo_of_a_batch_settles_a_directory_and_shrinks_its_split(
    dump, monkeypatch, small_chunks
):
    """templet:3159 -- Henge: a batch cut once its split's directory was put
    leaves the old chunk holding keys past its range, and a directory read, not
    answered. The open's redo puts that directory again and leaves every chunk
    within its range ("Only past an answer"; a redo as row 6 has it)."""
    base, puts = load_log(dump, BATCH, monkeypatch)
    idx = henge.spaces("rec")["idx"]
    at = a_directory_put(puts)
    key = puts[at][1]
    fake = holding(base + puts[: at + 1])
    client = Spy(fake.client("w2"))
    rec = henge.HengeRecord(client, "rec", actor="l", via="load", timeout_s=1.0)
    rec.clock = Clock()
    assert ("put", idx, key) in [c[:3] for c in client.calls]
    numbers = henge.ranges(fake.value(idx, key))
    assert len(numbers) >= 2
    for n, rng in numbers.items():
        chunk = fake.value(idx, key[:-1] + [n])
        assert chunk == henge.filtered(chunk, rng), (n, chunk)
    rec.load(dump)
    assert rec.dump() == dump
    assert rec.check() == []


def committed_at(puts, to):
    """The put that commits a batch: `loading`, naming its last seq."""
    state = henge.spaces("rec")["state"]
    return next(
        i
        for i, p in enumerate(puts)
        if p[0] == state
        and p[1] == ["loading"]
        and isinstance(p[2], dict)
        and p[2].get("to") == to
    )


def test_an_open_redoes_a_committed_batch_and_settles_each_entry(dump, monkeypatch):
    """templet:3159 -- Henge: an open that finds a load's batch committed and not
    applied redoes exactly that batch, each entry put again with the version
    read before it goes on past it ("Only past an answer"), then `applied`, one
    sync and `head` (row 6), so `head()` and `check()` read a whole prefix of
    the dump, with no resume."""
    base, puts = load_log(dump, BATCH, monkeypatch)
    journal = henge.spaces("rec")["journal"]
    state = henge.spaces("rec")["state"]
    fake = holding(base + puts[: committed_at(puts, BATCH) + 1])
    client = Spy(fake.client("w2"))
    rec = henge.HengeRecord(client, "rec", actor="l", via="load", timeout_s=1.0)
    rec.clock = Clock()
    assert rec.head() == BATCH
    put_again = [c[2] for c in client.calls if c[0] == "put" and c[1] == journal]
    assert put_again == [[s] for s in range(1, BATCH + 1)]
    assert fake.value(state, ["applied"]) == BATCH
    assert fake.value(state, ["head"]) == BATCH
    calls = [c[:3] for c in client.calls]
    applied = calls.index(("put", state, ["applied"]))
    head = calls.index(("put", state, ["head"]), applied)
    assert ("sync", journal, None) in calls[applied:head]
    assert any("load is in progress" in f for f in rec.check())
    rec.load(dump)
    assert rec.dump() == dump
    assert rec.check() == []


def test_an_open_redoes_no_entry_of_a_batch_not_committed(dump, monkeypatch):
    """templet:3159 -- Henge: entries past what `loading` names are a batch that
    was not committed. A part of a batch is another plan than the whole, and its
    loader may be alive, so an open redoes none of them and puts none of them:
    `head()` is the last batch's, and they are the load's to put again."""
    base, puts = load_log(dump, BATCH, monkeypatch)
    journal = henge.spaces("rec")["journal"]
    state = henge.spaces("rec")["state"]
    start = committed_at(puts, BATCH) + 1
    entries = [i for i, p in enumerate(puts) if p[0] == journal and i > start]
    fake = holding(base + puts[: entries[2] + 1])
    before = {s: fake.value(journal, [s]) for s in range(BATCH + 1, BATCH + 4)}
    client = Spy(fake.client("w2"))
    rec = henge.HengeRecord(client, "rec", actor="l", via="load", timeout_s=1.0)
    rec.clock = Clock()
    assert rec.head() == BATCH
    assert not [c for c in client.calls if c[0] == "put" and c[1] == journal]
    assert fake.value(state, ["applied"]) == BATCH
    assert before == {s: fake.value(journal, [s]) for s in before}
    assert any("load is in progress" in f for f in rec.check())
    rec.load(dump)
    assert rec.dump() == dump
    assert rec.check() == []


def test_entries_past_applied_with_no_load_mean_another_writer(dump, monkeypatch):
    """templet:3159 -- Henge: only a load in progress explains more than one
    entry past `applied`. With `loading` null, a second entry is another
    writer, as it always was: nothing is redone and the open is refused."""
    base, puts = load_log(dump, BATCH, monkeypatch)
    journal = henge.spaces("rec")["journal"]
    state = henge.spaces("rec")["state"]
    entries = [p for p in puts if p[0] == journal][:2]
    fake = holding(base + entries + [(state, ["loading"], None)])
    with pytest.raises(Unavailable, match="two entries"):
        on_fake(fake, "w2")
    assert fake.value(state, ["applied"]) == 0


def test_a_resumed_load_refuses_a_batch_that_is_not_the_dumps(dump, monkeypatch):
    """templet:3159 -- Henge: entries past `applied` that are not the dump's are
    `Refused` by the resumed load, committing nothing, and `loading` stays."""
    base, puts = load_log(dump, BATCH, monkeypatch)
    journal = henge.spaces("rec")["journal"]
    state = henge.spaces("rec")["state"]
    entries = [i for i, p in enumerate(puts) if p[0] == journal]
    left = base + puts[: entries[2] + 1]
    other = json.loads(json.dumps(left[-1][2]))
    other["event"]["actor"] = "somebody else"
    fake = holding(left + [(journal, [4], other)])
    rec = on_fake(fake, "w2")
    assert rec.head() == 0
    with pytest.raises(Refused, match="not the dump's"):
        rec.load(dump)
    assert fake.value(state, ["loading"])["digest"]
    assert max(journal_of(fake)) == 4
    with pytest.raises(Refused, match="load"):
        admitted(rec, "0002")


def test_another_writers_entry_met_inside_a_batch_stops_the_load(dump, monkeypatch):
    """templet:3159 -- Henge: a commit put of a batch that meets another entry
    stops there, nothing of the batch past it is put, and `loading` stays; the
    next open finds the load in progress and the resumed load refuses it."""
    monkeypatch.setattr(henge, "LOAD_BATCH", BATCH)
    fake = FakeStore()
    rec = on_fake(fake, "w")
    rec.init()
    journal = henge.spaces("rec")["journal"]
    state = henge.spaces("rec")["state"]
    foreign = {
        "event": {**dump["journal"][2], "actor": "somebody else"},
        "priors": [],
    }
    fake._apply_put("x", journal, [3], foreign, None)
    with pytest.raises(Unavailable):
        rec.load(dump)
    assert sorted(journal_of(fake)) == [1, 2, 3]
    assert fake.value(state, ["applied"]) == 0
    assert isinstance(fake.value(state, ["loading"]), dict)
    again = on_fake(fake, "w2")
    with pytest.raises(Refused, match="not the dump's"):
        again.load(dump)


# -- a kill, on a real store -------------------------------------------------


def kill_points(events):
    """Every batch boundary of a load of `events` at `BATCH` (the answer to
    `head`, `applied` and the batch's sync), and inside a batch: the first
    and a middle entry, each before and after its put, a record and a chunk,
    the load's start and its end."""
    points = [
        Point(space="state", key=["head"], value=seq)
        for seq in range(BATCH, events + 1, BATCH)
    ]
    batches = math.ceil(events / BATCH)
    points += [
        Point(space="state", key=["loading"], nth=2, after=False),
        Point(space="state", key=["loading"], nth=2),
        Point(space="state", key=["loading"], nth=batches + 2, after=False),
        Point(space="state", key=["applied"], value=BATCH),
        Point(op="sync", nth=1),
        Point(op="sync", nth=1, after=False),
        Point(space="state", key=["loading"], nth=1, after=False),
        Point(space="state", key=["loading"], nth=1),
        Point(space="journal", key=[1], after=False),
        Point(space="journal", key=[BATCH + 1]),
        Point(space="journal", key=[BATCH + 3], after=False),
        Point(space="rec", nth=2),
        Point(space="idx", nth=2),
        Point(space="state", key=["loading"], nth=batches + 2),
    ]
    return points


def run_loader(url, dump_path, point):
    return subprocess.run(
        [
            sys.executable,
            str(LOADER),
            url,
            str(dump_path),
            str(BATCH),
            "null" if point is None else point.to_json(),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )


def after_a_kill(url, dump, *, name="after"):
    """What a killed load left: it opens, `check()` calls it complete, in
    progress or empty, and the same dump ends it, whole, and it takes the next
    operation."""
    rec = open_record(url, actor="loader", via="load")
    rec.clock = Clock()
    try:
        head = rec.head()
        findings = rec.check()
        if findings:
            assert any("load is in progress" in f for f in findings), findings
            assert [f for f in findings if "load is in progress" not in f] == []
        else:
            assert head in (0, len(dump["journal"]))
        if head < len(dump["journal"]) or findings:
            rec.load(dump)
        assert rec.dump() == dump
        assert rec.check() == []
        n = rec.file_item(
            "task", name, "body", filed_by="alpha", effort="0001", role="alpha"
        )
        assert rec.get("items", [n])["title"] == name
    finally:
        rec.close()


@over_backends
def test_a_loader_killed_anywhere_resumes(backend, tmp_path):
    """templet:3159 -- every backend: SIGKILL of the loader's process at every
    batch boundary and inside a batch, the record then opened, resumed and
    compared with the dump. SQLite's load is one transaction, so a kill leaves
    it empty; `event` is the event it dies after."""
    dump = a_dump(tmp_path / "source")
    dump_path = tmp_path / "dump.json"
    dump_path.write_text(json.dumps(dump))
    events = len(dump["journal"])
    if scheme(backend) == "sqlite":
        points = [Point(event=seq) for seq in range(BATCH, events + 1, BATCH)] + [
            Point(event=1),
            Point(event=events),
        ]
    else:
        points = kill_points(events)
    for i, point in enumerate(points):
        url = url_for(backend, tmp_path / f"k{i}")
        rec = open_record(url, actor="loader", via="load")
        rec.init()
        rec.close()
        done = run_loader(url, dump_path, point)
        assert done.returncode == -9, (point, done.stdout, done.stderr)
        after_a_kill(url, dump)


@over_backends
def test_a_load_that_returned_is_there_when_its_process_is_killed(backend, tmp_path):
    """templet:3159 -- every backend: a loader killed the moment its load
    returned leaves the whole dump, no load in progress."""
    dump = a_dump(tmp_path / "source")
    dump_path = tmp_path / "dump.json"
    dump_path.write_text(json.dumps(dump))
    url = url_for(backend, tmp_path / "k")
    rec = open_record(url, actor="loader", via="load")
    rec.init()
    rec.close()
    done = run_loader(url, dump_path, None)
    assert done.returncode == -9, (done.stdout, done.stderr)
    assert done.stdout.strip() == "returned"
    rec = open_record(url, actor="loader", via="load")
    try:
        assert rec.dump() == dump
        assert rec.check() == []
    finally:
        rec.close()


STORE_PLACEMENTS = ["single", "mirror"]


@pytest.mark.parametrize("placement", STORE_PLACEMENTS)
def test_a_store_killed_inside_a_load_resumes(placement, tmp_path, monkeypatch):
    """templet:3159 -- Henge: SIGKILL of the store at a batch boundary and inside
    a batch, and at the load's end. The load gives `Unknown` or `Unavailable`,
    never returns; the store restarted on its folder, the record opens, and the
    same dump ends it with the same dump."""
    monkeypatch.setattr(henge, "LOAD_BATCH", BATCH)
    dump = a_dump(tmp_path / "source")
    events = len(dump["journal"])
    points = [
        Point(space="state", key=["head"], value=BATCH),
        Point(op="sync", nth=2, after=False),
        Point(space="journal", key=[BATCH + 2]),
        Point(space="rec", nth=BATCH + 1),
        Point(space="idx", nth=3),
        Point(space="state", key=["loading"], nth=2),
        Point(
            space="state",
            key=["loading"],
            nth=math.ceil(events / BATCH) + 2,
            after=False,
        ),
    ]
    assert events > 2 * BATCH
    with own_store(placement) as running:
        for i, point in enumerate(points):
            url = running.url()
            rec = open_record(url, actor="loader", via="load", timeout_s=1.0)
            rec.init()
            rec.store = Killing(rec.store, point, running.kill)
            with pytest.raises((Unknown, Unavailable)):
                rec.load(dump)
            assert rec.store is not None
            rec.close()
            running.restart()
            after_a_kill(url, dump, name=f"after{i}")


@pytest.mark.parametrize("placement", STORE_PLACEMENTS)
def test_a_load_that_returned_is_there_when_the_store_is_killed(placement, tmp_path):
    """templet:3159 -- Henge: a load that returned, followed at once by SIGKILL of
    the store, loses nothing. (The store's page cache outlives a kill, so what
    a power cut would take is `test_nothing_is_reported_before_a_sync_covers_it`
    and the closing sync's test in test_henge.py; this is the process.)"""
    dump = a_dump(tmp_path / "source")
    with own_store(placement) as running:
        url = running.url()
        rec = open_record(url, actor="loader", via="load", timeout_s=5.0)
        rec.init()
        rec.load(dump)
        running.kill()
        rec.close()
        running.restart()
        again = open_record(url, actor="loader", via="load")
        try:
            assert again._loading is None
            assert again.dump() == dump
            assert again.check() == []
        finally:
            again.close()
