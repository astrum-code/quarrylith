# SPDX-License-Identifier: Apache-2.0
"""A writer open before a load, the lock bypassed, killed or held across the
others (templet:3159, invariant 13). `test_load_two_writers.py` runs each handle
to its end; here a writer dies inside an operation, a redo is held while a writer
is answered, and a seeded scheduler and a bounded crossing of pairs run a loader
and fresh opens one store call at a time.

What must hold: an answered operation stays; an entry a writer committed is
finished by the next open, as ever; and `check()` finds a load in progress or
nothing. Every wait and every run here has a ceiling."""

import collections
import random
import threading
import time
from pathlib import Path

import pytest
from backends import PARTS, Clock, admitted
from hengestore import FakeStore, own_store
from test_load_batches import a_dump, holding, load_log, on_fake
from test_load_two_writers import moved_and_back

from quarrylith.record import henge
from quarrylith.record.contract import Refused, Unavailable, Unknown, open_record

ENDED = (Unavailable, Unknown, Refused)
#: The longest any one handle waits for another here.
WAIT_S = 5
BATCH = 8

try:
    from henge.store.client import Store
except ImportError:  # pragma: no cover - the client is a dependency
    Store = None


class Died(BaseException):
    """This handle's process died here."""


class Calling:
    """A client that runs `at(space, key, value, n)` before its n-th put."""

    def __init__(self, inner, at=None):
        self.inner, self.at, self.puts = inner, at, 0
        self.endpoint = getattr(inner, "endpoint", None)

    def put(self, space, key, value, *, expect=None):
        self.puts += 1
        if self.at:
            self.at(space, list(key), value, self.puts)
        return self.inner.put(space, key, value, expect=expect)

    def __getattr__(self, op):
        return getattr(self.inner, op)


def handle(client, actor):
    rec = henge.HengeRecord(client, "rec", actor=actor, via="load", timeout_s=1.0)
    rec.clock = Clock()
    return rec


def two_batches(folder, batch):
    """A dump of more than one batch whose items were filed in the first."""
    Path(folder).mkdir(parents=True, exist_ok=True)
    rec = open_record(f"sqlite:///{folder}/two.sqlite", actor="alpha", via="cli")
    rec.clock = Clock()
    rec.init()
    admitted(rec)
    ids = [
        rec.file_item(
            "task", f"i{i}", "b", filed_by="alpha", effort="0001", role="alpha"
        )
        for i in range(3)
    ]
    while rec.head() % batch:
        rec.comment(ids[1], "pad")
    for i in range(batch + 1):
        rec.comment(ids[1], f"more {i}")
    dump = rec.dump()
    rec.close()
    return dump, ids


def fresh(store_of):
    fake = store_of()
    first = handle(fake.client("w0"), "w0")
    first.init()
    first.close()
    return fake


def findings_of(fake):
    """What `check()` finds but a load in progress."""
    rec = handle(fake.client("reader"), "reader")
    try:
        return [f for f in rec.check() if "a load is in progress" not in f]
    finally:
        rec.close()


def a_writer_killed_between_batches(dump, ids, batch, j, store_of):
    """The writer meets the load at a batch's end, is told `Unavailable`, and its
    next operation takes the seq between two batches. It dies before the j-th put
    of that operation. Returns the store, and how the writer ended."""
    fake = fresh(store_of)
    journal = henge.spaces("rec")["journal"]
    state, result = {}, {}

    def dies(space, key, value, n):
        if "from" in state and n - state["from"] == j:
            raise Died()

    writer = handle(Calling(fake.client("W"), dies), "alpha")

    def at(space, key, value, n):
        if space == journal and key == [batch + 1] and not result.get("did"):
            result["did"] = True
            try:
                writer.note("past", "first")
            except ENDED:
                pass
            state["from"] = writer.store.puts
            try:
                writer.move(ids[0], "blocked", "slips in")
                result["writer"] = "answered"
            except Died:
                result["writer"] = "died"
            except ENDED as e:
                result["writer"] = type(e).__name__

    loader = handle(Calling(fake.client("L"), at), "loader")
    try:
        loader.load(dump)
    except ENDED:
        pass
    loader.close()
    return fake, result.get("writer")


def a_redo_held_across_a_writer(dump, ids, batch, j, store_of):
    """A redo of the first batch is held before its j-th put. The loader ends that
    batch; the writer takes the next seq, moves an item, and is answered; the redo
    goes on. Returns the store, how the writer ended, and if the redo was held."""
    fake = fresh(store_of)
    journal = henge.spaces("rec")["journal"]
    state_space = henge.spaces("rec")["state"]
    writer = handle(fake.client("W"), "alpha")
    reached, go = threading.Event(), threading.Event()
    result, opened = {}, {}

    def redo_at(space, key, value, n):
        if n == j:
            reached.set()
            assert go.wait(WAIT_S)

    def second():
        try:
            opened["rec"] = handle(Calling(fake.client("R"), redo_at), "r")
        except ENDED:
            pass

    thread = threading.Thread(target=second, daemon=True)

    def at(space, key, value, n):
        committed = (
            space == state_space
            and key == ["loading"]
            and isinstance(value, dict)
            and value.get("to") == batch
        )
        if committed and "started" not in result:
            result["started"] = "pending"
        elif result.get("started") == "pending":
            result["started"] = True
            thread.start()
            reached.wait(WAIT_S / 5)
        if space == journal and key == [batch + 1] and "writer" not in result:
            result["writer"] = "?"
            try:
                writer.note("past", "first")
            except ENDED:
                pass
            try:
                writer.move(ids[0], "blocked", "slips in")
                result["writer"] = "answered"
            except ENDED as e:
                result["writer"] = type(e).__name__
            go.set()
            thread.join(WAIT_S)

    loader = handle(Calling(fake.client("L"), at), "loader")
    try:
        loader.load(dump)
    except ENDED:
        pass
    loader.close()
    go.set()
    thread.join(WAIT_S)
    assert not thread.is_alive()
    if "rec" in opened:
        opened["rec"].close()
    return fake, result.get("writer"), reached.is_set()


@pytest.fixture
def batches(tmp_path, monkeypatch):
    monkeypatch.setattr(henge, "LOAD_BATCH", BATCH)
    return two_batches(tmp_path, BATCH)


def test_a_writer_killed_between_two_batches_is_finished(batches):
    """templet:3159 -- Henge, Recovery: an entry a writer committed is never lost,
    with a load in progress too. It dies at each put of its operation between two
    batches, and the next open finishes it: its item reads by its own state."""
    dump, ids = batches
    was = dump["records"]["items"][0]["state"]
    died = 0
    for j in range(1, 16):
        fake, ended = a_writer_killed_between_batches(dump, ids, BATCH, j, FakeStore)
        if ended != "died":
            break
        died += 1
        journal = henge.spaces("rec")["journal"]
        entry = fake.value(journal, [BATCH + 1])
        committed = entry is not None and "load" not in entry
        assert findings_of(fake) == [], j
        rec = on_fake(fake, "reader")
        state = rec.get("items", [ids[0]])["state"]
        held = [r["number"] for r in rec.read("items", state=state)]
        assert ids[0] in held, (j, state)
        assert state == ("blocked" if committed else was), j
        rec.close()
    assert died >= 4


def test_a_redo_held_across_a_writers_answered_operation(batches):
    """templet:3159 -- Henge, the guard: a stale redo goes on after a writer that
    slipped in between two batches was answered. The guard takes that entry for a
    commit, the redo stops, and the answered operation stands."""
    dump, ids = batches
    held = 0
    for j in range(1, 60):
        fake, ended, reached = a_redo_held_across_a_writer(
            dump, ids, BATCH, j, FakeStore
        )
        if not reached:
            break
        held += 1
        assert findings_of(fake) == [], j
        if ended == "answered":
            rec = on_fake(fake, "reader")
            assert rec.get("items", [ids[0]])["state"] == "blocked", j
            blocked = [r["number"] for r in rec.read("items", state="blocked")]
            assert ids[0] in blocked, j
            rec.close()
    assert held >= 10


def a_writer_ahead_of_the_load(dump, j):
    """The writer takes seq 1 ahead of the load's first entry, and dies before the
    j-th put of that operation; the load meets it and stops."""
    fake = fresh(FakeStore)
    journal = henge.spaces("rec")["journal"]
    state, result = {}, {}

    def dies(space, key, value, n):
        if "from" in state and n - state["from"] == j:
            raise Died()

    writer = handle(Calling(fake.client("W"), dies), "alpha")

    def at(space, key, value, n):
        if space == journal and key == [1] and "writer" not in result:
            result["writer"] = "?"
            state["from"] = writer.store.puts
            try:
                admitted(writer)
                result["writer"] = "answered"
            except Died:
                result["writer"] = "died"

    loader = handle(Calling(fake.client("L"), at), "loader")
    try:
        loader.load(dump)
    except ENDED:
        pass
    loader.close()
    return fake, result.get("writer")


def test_a_writer_killed_ahead_of_the_load_is_finished(tmp_path, monkeypatch):
    """templet:3159 -- Henge, Recovery: the writer dies at each put of an operation
    ahead of the load's first entry. Past its commit put, the next open finishes
    it; the load is refused for it, and nothing is torn."""
    monkeypatch.setattr(henge, "LOAD_BATCH", BATCH)
    dump = a_dump(tmp_path / "source", items=3)
    died = 0
    for j in range(1, 25):
        fake, ended = a_writer_ahead_of_the_load(dump, j)
        if ended != "died":
            break
        died += 1
        assert findings_of(fake) == [], j
        rec = on_fake(fake, "reader")
        if j >= 2:
            assert rec.head() == 1, j
            assert rec.get("efforts", ["0001"]) is not None, j
            with pytest.raises(Refused, match="not the dump's"):
                rec.load(dump)
        rec.close()
    assert died >= 5


def test_a_redo_of_a_writers_entry_held_across_its_next_operation(
    tmp_path, monkeypatch
):
    """templet:3159 -- Henge, the guard: a fresh open redoes a live writer's entry,
    held before each put, while that writer finishes it and is answered for its
    next. Each of the writer's commits takes the counter on: the redo's late put
    stops at the guard, and is not put over it."""
    monkeypatch.setattr(henge, "LOAD_BATCH", BATCH)
    dump = a_dump(tmp_path / "source", items=3)
    held = 0
    for j in range(1, 30):
        fake = fresh(FakeStore)
        journal = henge.spaces("rec")["journal"]
        reached, go = threading.Event(), threading.Event()
        opened, result = {}, {}

        def redo_at(space, key, value, n, j=j, reached=reached, go=go):
            if n == j:
                reached.set()
                assert go.wait(WAIT_S)

        def second(fake=fake, redo_at=redo_at, opened=opened):
            try:
                opened["rec"] = handle(Calling(fake.client("R"), redo_at), "r")
            except ENDED:
                pass

        thread = threading.Thread(target=second, daemon=True)

        def writer_at(space, key, value, n, thread=thread, result=result):
            if "started" in result and not thread.ident and n == result["started"] + 2:
                thread.start()
                reached.wait(WAIT_S / 5)

        writer = handle(Calling(fake.client("W"), writer_at), "alpha")

        def at(space, key, value, n, writer=writer, result=result):
            if space == journal and key == [1] and "writer" not in result:
                result["writer"] = "?"
                result["started"] = writer.store.puts
                try:
                    writer.file_effort(None, "s-a", "a", "owner-a", PARTS)
                    writer.file_effort(None, "s-b", "b", "owner-a", PARTS)
                    result["writer"] = "answered"
                except ENDED as e:
                    result["writer"] = type(e).__name__
                go.set()
                thread.join(WAIT_S)

        loader = handle(Calling(fake.client("L"), at), "loader")
        try:
            loader.load(dump)
        except ENDED:
            pass
        loader.close()
        go.set()
        thread.join(WAIT_S)
        assert not thread.is_alive()
        if "rec" in opened:
            opened["rec"].close()
        if not reached.is_set():
            break
        held += 1
        assert findings_of(fake) == [], j
        if result["writer"] == "answered":
            rec = on_fake(fake, "reader")
            assert len(rec.read("efforts")) == 2, j
            rec.close()
    assert held >= 5


def test_a_writers_entry_is_not_taken_for_a_loads(dump_two, monkeypatch):
    """templet:3159 -- Henge, Recovery: with a load in progress, an open leaves only
    a load's own entries past what `loading` names. A writer's at a+1 stays a
    commit, and a load's behind it, which no store's order gives, is two entries:
    another writer, refused, and neither is touched."""
    base, puts = load_log(dump_two, BATCH, monkeypatch)
    state = henge.spaces("rec")["state"]
    journal = henge.spaces("rec")["journal"]
    entries = [p for p in puts if p[0] == journal]
    start = next(p for p in puts if p[0] == state and p[1] == ["loading"])
    writers = {k: v for k, v in entries[0][2].items() if k != "load"}
    fake = holding(base + [start, (journal, [1], writers), entries[1]])
    with pytest.raises(Unavailable, match="two entries"):
        handle(fake.client("R"), "r")
    assert fake.value(state, ["applied"]) == 0


class Real:
    """A running store, as the cases ask for a fake."""

    def __init__(self, address):
        self.address = address

    def client(self, who=None):
        return Store.connect(self.address)


@pytest.mark.parametrize("what", ["killed", "held"])
def test_a_writer_slips_in_on_a_real_store(what, tmp_path, monkeypatch):
    """templet:3159 -- Henge: the two cases above on a real `henge single`, at a
    point inside the operation and inside the redo."""
    monkeypatch.setattr(henge, "LOAD_BATCH", BATCH)
    dump, ids = two_batches(tmp_path, BATCH)
    with own_store("single") as running:
        real = Real(f"ipc://{running.folder}/henge.sock")
        if what == "killed":
            _, ended = a_writer_killed_between_batches(
                dump, ids, BATCH, 4, lambda: real
            )
            assert ended == "died"
        else:
            _, ended, reached = a_redo_held_across_a_writer(
                dump, ids, BATCH, 2, lambda: real
            )
            assert reached
            assert ended == "answered"
        rec = henge.HengeRecord(
            real.client(), "rec", actor="r", via="load", timeout_s=5.0
        )
        rec.clock = Clock()
        try:
            assert [f for f in rec.check() if "a load is in progress" not in f] == []
            state = rec.get("items", [ids[0]])["state"]
            assert state == "blocked"
            assert ids[0] in [r["number"] for r in rec.read("items", state=state)]
        finally:
            rec.close()


def test_a_redo_reads_only_to_what_loading_named(dump_two, monkeypatch):
    """templet:3159 -- Henge, Recovery: a redo holds to the `to` it read. Between
    its read of `loading` and of the entries the loader ends that batch and puts
    entries of the next, not committed, and the redo leaves them alone."""
    base, puts = load_log(dump_two, BATCH, monkeypatch)
    state = henge.spaces("rec")["state"]
    journal = henge.spaces("rec")["journal"]
    committed = next(
        i
        for i, p in enumerate(puts)
        if p[0] == state and p[1] == ["loading"] and (p[2] or {}).get("to") == BATCH
    )
    later = next(
        i for i, p in enumerate(puts) if p[0] == journal and p[1] == [BATCH + 3]
    )
    fake = holding(base + puts[: committed + 1])

    class Moves:
        endpoint = None
        moved = False

        def __init__(self, inner):
            self.inner = inner

        def get(self, space, key):
            got = self.inner.get(space, key)
            if space == state and list(key) == ["loading"] and not self.moved:
                self.moved = True
                for sp, k, v in puts[committed + 1 : later + 1]:
                    fake._apply_put("L", sp, k, v, None)
            return got

        def put(self, space, key, value, *, expect=None):
            assert not (space == journal and key[0] > BATCH), key
            return self.inner.put(space, key, value, expect=expect)

        def __getattr__(self, op):
            return getattr(self.inner, op)

    client = Moves(fake.client("R"))
    rec = handle(client, "r")
    assert client.moved
    assert rec.head() == BATCH
    assert findings_of(fake) == []
    rec.close()


@pytest.fixture
def dump_two(tmp_path):
    return two_batches(tmp_path, BATCH)[0]


def a_split_met_by(fake, chunk_held, listed, faults=0):
    """A writer whose fifth effort splits `efforts`'s `*` chunk, its plan already
    read, when number 1 comes to hold `chunk_held`, listed by the directory or
    not. `faults`: the writer's fresh read of the directory fails that many
    times. Returns the writer, the key of number 1, and the directory's."""
    idx = henge.spaces("rec")["idx"]
    ix = ("efforts", "*", None)
    number, directory = henge.ix_key(ix, 1), henge.ix_key(ix, "dir")
    armed = {"on": False, "done": False}

    def at(space, key, value, n):
        if armed["on"] and not armed["done"] and space == idx and key == number:
            armed["done"] = True
            fake._apply_put("x", idx, number, chunk_held, None)
            if listed:
                entry = [[1, chunk_held[0]]]
                fake._apply_put("x", idx, directory, entry, None)
            if faults:
                fake.fault(
                    lambda who, op, sp, k, v: op == "get"
                    and sp == idx
                    and list(k) == directory,
                    Unavailable_of_store(),
                    times=faults,
                )

    rec = handle(Calling(fake.client("w"), at), "alpha")
    rec.init()
    for i in range(4):
        rec.file_effort(f"e{i}", f"s-{i}", f"e{i}", "owner-a", PARTS)
    armed["on"] = True
    return rec, number, directory


def Unavailable_of_store():
    from henge.core import errors

    return errors.Unavailable("the store is not answering")


def test_a_new_chunk_never_overwrites_a_listed_one(monkeypatch):
    """templet:3159 -- Henge, row 3: a split's new chunk goes `absent`, or is the
    chunk already there. A number the directory lists that holds some other chunk
    means the plan read across another's split: the writer stops, and that chunk
    stands."""
    monkeypatch.setattr(henge, "CHUNK_KEYS", 4)
    monkeypatch.setattr(henge, "PIECE_KEYS", 2)
    fake = FakeStore()
    idx = henge.spaces("rec")["idx"]
    held = [["zz"]]
    rec, number, _ = a_split_met_by(fake, held, listed=True)
    with pytest.raises((Unavailable, Unknown), match="stale plan"):
        rec.file_effort("e4", "s-4", "e4", "owner-a", PARTS)
    assert fake.value(idx, number) == held
    assert rec._closed is not None


def test_a_new_chunk_overwrites_a_leftover_no_directory_lists(monkeypatch):
    """templet:3159 -- Henge, row 3: a number no directory lists holds a dead
    writer's leftover, and the writer puts its own over it, with the version it
    read, and goes on."""
    monkeypatch.setattr(henge, "CHUNK_KEYS", 4)
    monkeypatch.setattr(henge, "PIECE_KEYS", 2)
    fake = FakeStore()
    idx = henge.spaces("rec")["idx"]
    held = [["zz"]]
    rec, number, directory = a_split_met_by(fake, held, listed=False)
    rec.file_effort("e4", "s-4", "e4", "owner-a", PARTS)
    assert rec._closed is None
    assert fake.value(idx, number) != held
    assert [n for n, _ in fake.value(idx, directory)] == [1]
    assert [r["id"] for r in rec.read("efforts")] == [f"e{i}" for i in range(5)]
    assert rec.check() == []
    rec.close()


@pytest.mark.parametrize("faults", [1, 1000])
def test_a_new_chunks_read_of_the_directory_is_tried_again(monkeypatch, faults):
    """templet:3159 -- Henge, row 3 and the record's timeout: the fresh read of the
    directory, at a number that holds another chunk, is a read inside a retry of a
    put. It is tried again after a pause, and past the timeout the operation ends
    as any such put does, `Unknown`, with the record closed."""
    monkeypatch.setattr(henge, "CHUNK_KEYS", 4)
    monkeypatch.setattr(henge, "PIECE_KEYS", 2)
    fake = FakeStore()
    rec, _, _ = a_split_met_by(fake, [["zz"]], listed=False, faults=faults)
    rec.timeout_s = 0.3
    if faults == 1:
        rec.file_effort("e4", "s-4", "e4", "owner-a", PARTS)
        assert rec._closed is None
        assert rec.check() == []
        rec.close()
    else:
        with pytest.raises(Unknown):
            rec.file_effort("e4", "s-4", "e4", "owner-a", PARTS)
        assert rec._closed is not None


def an_ack_killed_at(fake, j, kind):
    """The writer's fifth cursor or beat splits its `*` chunk, and it dies before
    the j-th put of that. Returns whether it died."""
    state = {"at": None}

    def dies(space, key, value, n):
        if state["at"] is not None and n == state["at"]:
            raise Died()

    client = Calling(fake.client("w"), dies)
    rec = handle(client, "alpha")
    rec.init()
    admitted(rec)
    if kind == "ack":
        for key in "cdef":
            rec.ack(key, 1)
    else:
        for key in (11, 12, 13, 14):
            rec.beat(key)
    state["at"] = client.puts + j
    try:
        if kind == "ack":
            rec.ack("g", 1)
        else:
            rec.beat(15)
    except Died:
        rec._closed = "killed"
        rec._shut()
        return True
    rec.close()
    return False


@pytest.mark.parametrize(
    "kind,keys",
    [
        ("ack", ["z", "y", "g"]),
        ("ack", ["0", "1", "g"]),
        ("beat", [20, 21, 15]),
        ("beat", [1, 2, 15]),
    ],
    ids=["ack-higher", "ack-lower", "beat-higher", "beat-lower"],
)
def test_a_dead_acks_leftover_stops_no_later_split(kind, keys, monkeypatch):
    """templet:3159 -- Henge, row 3, one writer and the lock held: an `ack` or a
    `beat` makes no entry, so nothing redoes one that dies inside a split, and its
    new chunks stay, listed by no directory. It dies before each put, and other
    keys, higher and lower, and its own, are taken, read, and `check()` is clean."""
    monkeypatch.setattr(henge, "CHUNK_KEYS", 4)
    monkeypatch.setattr(henge, "PIECE_KEYS", 2)
    monkeypatch.setattr(henge, "JOURNAL_CHUNK", 4)
    died = 0
    for j in range(1, 10):
        fake = FakeStore()
        if not an_ack_killed_at(fake, j, kind):
            break
        died += 1
        for key in keys:
            rec = on_fake(fake, "w2", actor="alpha", via="cli")
            if kind == "ack":
                rec.ack(key, 1)
            else:
                rec.beat(key)
            rec.close()
        rec = on_fake(fake, "reader")
        if kind == "ack":
            read = {r["consumer"] for r in rec.read("cursors")}
        else:
            read = {r["run"] for r in rec.read("beats")}
        assert set(keys) <= read, (j, read)
        assert rec.check() == [], j
        rec.close()
    assert died >= 3


def test_a_dead_acks_leftover_on_a_real_store_at_the_pages_bounds():
    """templet:3159 -- Henge, row 3: on a real `henge single`, at the page's own
    8,192 keys a chunk: an `ack` that splits dies after its new chunks and before
    the directory. Another consumer's `ack`, higher and lower, and its own, are
    taken, each by a new open."""
    with own_store("single") as running:
        address = f"ipc://{running.folder}/henge.sock"

        def opened(client):
            rec = henge.HengeRecord(
                client, "rec", actor="alpha", via="cli", timeout_s=10.0
            )
            rec.clock = Clock()
            return rec

        state = {"at": None}

        def dies(space, key, value, n):
            if state["at"] is not None and n == state["at"]:
                raise Died()

        client = Calling(Store.connect(address), dies)
        rec = opened(client)
        rec.init()
        admitted(rec)
        idx = henge.spaces("rec")["idx"]
        direct = Store.connect(address)
        keys = [[f"c{i:05d}"] for i in range(henge.CHUNK_KEYS)]
        direct.put(idx, ["cursors", "*", 0], keys)
        direct.close()
        rec._forget()
        state["at"] = client.puts + 3
        with pytest.raises(Died):
            rec.ack("c99999", 1)
        rec._closed = "killed"
        rec._shut()
        for who in ("c99998", "d00000", "c99999"):
            rec = opened(Store.connect(address))
            rec.ack(who, 1)
            assert rec._closed is None
            rec.close()
        rec = opened(Store.connect(address))
        try:
            read = {r["consumer"] for r in rec.read("cursors")}
            assert read == {"c99998", "d00000", "c99999"}
        finally:
            rec.close()


# -- a redo crossed with the loader, by pairs of calls ----------------------------


class Stop:
    """A client held before its k-th call: `act()` runs there, or it waits."""

    endpoint = None

    def __init__(self, inner, k, act=None):
        self.inner, self.k, self.act, self.n = inner, k, act, 0
        self.reached, self.go = threading.Event(), threading.Event()

    def _call(self, op, *a, **kw):
        self.n += 1
        if self.n == self.k:
            if self.act:
                self.act()
            else:
                self.reached.set()
                assert self.go.wait(WAIT_S)
        return getattr(self.inner, op)(*a, **kw)

    def put(self, *a, **k):
        return self._call("put", *a, **k)

    def get(self, *a, **k):
        return self._call("get", *a, **k)

    def get_many(self, *a, **k):
        return self._call("get_many", *a, **k)

    def sync(self, *a, **k):
        return self._call("sync", *a, **k)

    def __getattr__(self, op):
        return getattr(self.inner, op)


def a_pair(dump, i, j):
    """The loader stopped before its call i; a fresh open stopped before its call
    j; the loader goes to its end; the open goes on. Returns None if the loader
    has no call i, else what the open met: whether it reached its call j, and
    whether a stale plan stopped it."""
    fake = fresh(FakeStore)
    stopped = Stop(fake.client("R"), j)
    opened, state = {}, {}

    def second():
        try:
            opened["rec"] = henge.HengeRecord(
                stopped, "rec", actor="r", via="load", timeout_s=1.0
            )
        except ENDED as e:
            state["stale"] = "stale plan" in str(e)

    thread = threading.Thread(target=second, daemon=True)

    def act():
        state["acted"] = True
        thread.start()
        for _ in range(int(WAIT_S / 0.002)):
            if stopped.reached.wait(0.002) or not thread.is_alive():
                break
        state["reached"] = stopped.reached.is_set()

    client = Stop(fake.client("L"), None, act)
    loader = henge.HengeRecord(client, "rec", actor="l", via="load", timeout_s=1.0)
    loader.clock = Clock()
    client.k = client.n + i
    returned = False
    try:
        loader.load(dump)
        returned = True
    except ENDED:
        pass
    loader.close()
    stopped.go.set()
    if not state.get("acted"):
        return None
    thread.join(WAIT_S)
    assert not thread.is_alive()
    if "rec" in opened:
        opened["rec"].close()
    assert findings_of(fake) == [], (i, j)
    if returned:
        rec = on_fake(fake, "reader")
        assert rec.dump() == dump, (i, j)
        # a stop leaves nothing in the way of what comes after
        rec.note("past", "after the pair")
        assert rec.check() == [], (i, j)
        rec.close()
    return state["reached"], state.get("stale", False)


def some_pairs(seed, count, calls, opens):
    rng = random.Random(seed)
    return [(rng.randint(1, calls), rng.randint(1, opens)) for _ in range(count)]


def test_pairs_of_calls_around_a_split(tmp_path, monkeypatch):
    """templet:3159 -- Henge, invariant 13: a redo reads a directory, the loader
    splits and ends, and the redo reads the chunk: read apart, across a split, a
    plan is stale. Every pair of calls around that, and a seeded sample of the
    rest, at chunks of 4 keys, to a ceiling. The full crossing is a probe's; a
    load that returned is whole, and nothing is torn."""
    monkeypatch.setattr(henge, "LOAD_BATCH", BATCH)
    monkeypatch.setattr(henge, "CHUNK_KEYS", 4)
    monkeypatch.setattr(henge, "PIECE_KEYS", 2)
    monkeypatch.setattr(henge, "JOURNAL_CHUNK", 4)
    dump = a_dump(tmp_path / "source", items=2)
    pairs = [(i, j) for i in range(72, 79) for j in range(17, 22)]
    pairs += some_pairs(3159, 40, 150, 60)
    end = time.monotonic() + 25
    ran = stale = 0
    for i, j in pairs:
        got = a_pair(dump, i, j)
        ran += 1
        stale += 1 if got is not None and got[1] else 0
        if time.monotonic() > end:
            break
    assert ran >= 30
    # the window above is this dump's calls: if they move, say so
    assert stale >= 1, "no pair met a stale plan: the window has moved"


def test_pairs_of_calls_on_a_net_nothing(tmp_path, monkeypatch):
    """templet:3159 -- Henge, invariant 13: a seeded sample of the same pairs on a
    batch that nets to nothing, to a ceiling."""
    monkeypatch.setattr(henge, "LOAD_BATCH", 3)
    dump = moved_and_back(tmp_path, 3)[0]
    end = time.monotonic() + 10
    ran = 0
    for i, j in some_pairs(3160, 60, 90, 40):
        a_pair(dump, i, j)
        ran += 1
        if time.monotonic() > end:
            break
    assert ran >= 10


# -- a seeded scheduler ------------------------------------------------------------


class Scheduler:
    """One store call at a time, among the handles, by a seeded choice."""

    #: A gate no count of the loader's calls opens: only a batch's end does.
    SHUT = 10**9

    def __init__(self, rng):
        self.rng = rng
        self.cv = threading.Condition()
        self.parked, self.done, self.names = {}, set(), []
        self.calls = collections.Counter()
        self.grant = None
        self.deaths, self.gates = {}, {}
        #: The writer's gate opens at the loader's boundary-th put of `applied`.
        self.boundary = self.seen = 0

    def park(self, me):
        with self.cv:
            self.parked[me] = True
            self.cv.notify_all()
            while self.grant != me:
                if not self.cv.wait(WAIT_S):
                    raise RuntimeError("the scheduler stopped")
            self.grant = None
            del self.parked[me]
            self.calls[me] += 1
            if self.deaths.get(me) == self.calls[me]:
                raise Died()

    def finish(self, me):
        with self.cv:
            self.done.add(me)
            self.cv.notify_all()

    def run(self, stay):
        last = None
        deadline = time.monotonic() + WAIT_S * 2
        while True:
            with self.cv:
                while True:
                    alive = [n for n in self.names if n not in self.done]
                    if not alive:
                        return True
                    if self.grant is None and all(n in self.parked for n in alive):
                        break
                    if not self.cv.wait(1) or time.monotonic() > deadline:
                        return False
                ready = [
                    n
                    for n in alive
                    if self.gates.get(n, 0) <= self.calls["L"] or "L" in self.done
                ] or [n for n in alive if self.gates.get(n, 0) < Scheduler.SHUT]
                ready = ready or alive
                pick = (
                    last
                    if last in ready and self.rng.random() < stay
                    else self.rng.choice(ready)
                )
                last, self.grant = pick, pick
                self.cv.notify_all()


class Turn:
    """A client whose every call waits for its turn."""

    endpoint = None
    live = True

    def __init__(self, inner, scheduler, me):
        self.inner, self.scheduler, self.me = inner, scheduler, me

    def _call(self, op, *a, **k):
        if self.live:
            self.scheduler.park(self.me)
        return getattr(self.inner, op)(*a, **k)

    def put(self, space, key, value, **k):
        s = self.scheduler
        at_applied = list(key) == ["applied"] and space.endswith(".state")
        if self.me == "L" and s.boundary and at_applied:
            s.seen += 1
            if s.seen == s.boundary:
                s.gates["W"] = 0
        return self._call("put", space, key, value, **k)

    def get(self, *a, **k):
        return self._call("get", *a, **k)

    def get_many(self, *a, **k):
        return self._call("get_many", *a, **k)

    def sync(self, *a, **k):
        return self._call("sync", *a, **k)

    def durability(self, *a, **k):
        return self.inner.durability(*a, **k)

    def close(self):
        pass


def a_seeded_run(seed, batch, dump):
    """A loader, fresh opens that may resume it, and a writer opened before the
    load, each possibly dying at a seeded call, one store call at a time. Returns
    False where the scheduler ran out of time, and the run says nothing."""
    rng = random.Random(f"{seed}/{batch}")
    fake = fresh(FakeStore)
    scheduler = Scheduler(rng)

    def opened(me):
        rec = henge.HengeRecord(
            Turn(fake.client(me), scheduler, me),
            "rec",
            actor="alpha",
            via="load",
            timeout_s=1.0,
        )
        rec.clock = Clock()
        return rec

    def load():
        opened("L").load(dump)

    def opener(me, resume):
        def run():
            rec = opened(me)
            if resume:
                rec.load(dump)

        return run

    actors = {"L": load}
    scale = max(40, len(dump["journal"]) * 6)
    if rng.random() < 0.8:
        turn = Turn(fake.client("W"), scheduler, "W")
        turn.live = False
        writer = henge.HengeRecord(
            turn, "rec", actor="alpha", via="load", timeout_s=1.0
        )
        writer.clock = Clock()
        turn.live = True

        def writes():
            for i in range(rng.randint(2, 6)):
                try:
                    items = writer.read("items")
                    if items and rng.random() < 0.7:
                        it = rng.choice(items)
                        to = "blocked" if it["state"] != "blocked" else "ready"
                        writer.move(it["number"], to, "the writer")
                    else:
                        writer.note("past", f"n{i}")
                except Refused:
                    pass
                except Unavailable:
                    if writer._closed:
                        raise

        actors["W"] = writes
        scheduler.gates["W"] = rng.randint(1, scale)
        if rng.random() < 0.7:
            # to a batch's end, where it can take the next seq
            scheduler.boundary = rng.randint(
                1, max(1, len(dump["journal"]) // henge.LOAD_BATCH)
            )
            scheduler.gates["W"] = Scheduler.SHUT
    if rng.random() < 0.8:
        actors["R"] = opener("R", rng.random() < 0.4)
        scheduler.gates["R"] = rng.randint(1, scale)
    if rng.random() < 0.5:
        actors["R2"] = opener("R2", rng.random() < 0.4)
        scheduler.gates["R2"] = rng.randint(1, scale)
    for me in actors:
        if rng.random() < 0.3:
            scheduler.deaths[me] = rng.randint(1, scale)
    scheduler.names = list(actors)

    def wrapped(me, fn):
        def run():
            try:
                fn()
            except (Died, RuntimeError, AssertionError) + ENDED:
                pass
            finally:
                scheduler.finish(me)

        return run

    threads = [
        threading.Thread(target=wrapped(n, f), daemon=True) for n, f in actors.items()
    ]
    for t in threads:
        t.start()
    finished = scheduler.run(rng.choice([0.5, 0.9, 0.97]))
    for t in threads:
        t.join(WAIT_S)
    if not finished or any(t.is_alive() for t in threads):
        return False
    assert findings_of(fake) == [], (seed, batch)
    return True


@pytest.mark.parametrize("batch", [2, 8])
def test_a_seeded_scheduler_never_tears(batch, tmp_path, monkeypatch):
    """templet:3159 -- Henge, invariant 13: a seeded mix of a loader, fresh opens
    and a writer open before the load that moves items, mostly from a batch's end,
    with deaths, one store call at a time, on chunks of 4 keys. Nothing is torn, in
    as many runs as fit a ceiling."""
    monkeypatch.setattr(henge, "LOAD_BATCH", batch)
    monkeypatch.setattr(henge, "CHUNK_KEYS", 4)
    monkeypatch.setattr(henge, "PIECE_KEYS", 2)
    monkeypatch.setattr(henge, "JOURNAL_CHUNK", 4)
    dump = a_dump(tmp_path / "source", items=2)
    end = time.monotonic() + 15
    ran = 0
    for seed in range(400):
        if a_seeded_run(seed, batch, dump):
            ran += 1
        if time.monotonic() > end:
            break
    assert ran >= 10
