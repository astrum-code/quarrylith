# SPDX-License-Identifier: Apache-2.0
"""The contract, over every backend in BACKENDS (templet:3159, build step 2):
the operations on efforts, items, deps, comments, statements and holds, the
journal and its delivery, dump and load, check, and the ready read."""

import re
import threading
import time

import pytest
from backends import PARTS, URLS, admitted, over_backends, url_for
from samples import make_doc, sample

from quarrylith.record import contract, schema
from quarrylith.record.contract import MOVES, Refused, open_record
from quarrylith.record.events import Event, MemoryView, apply
from quarrylith.record.importing import compose
from quarrylith.record.schema import ITEM_STATES, RECORDS

#: The page's MOVES table, pair by pair.
OPEN = [s for s in ITEM_STATES if s not in ("done", "dropped")]
PAIRS = (
    {("triage", "ready")}
    | {(f, "claimed") for f in ("ready", "blocked")}
    | {("claimed", t) for t in ("blocked", "in-review", "ready")}
    | {("ready", "blocked"), ("blocked", "ready"), ("in-review", "blocked")}
    | {("in-review", "reviewed"), ("reviewed", "merge-ready")}
    | {("merge-ready", "reviewed")}
    | {
        (f, t)
        for f in ("in-review", "reviewed", "merge-ready")
        for t in ("ready", "done")
    }
    | {(f, t) for f in ("ready", "claimed", "blocked") for t in ("done", "dropped")}
    | {(f, t) for f in OPEN for t in ("done", "dropped")}
    | {(f, t) for f in ("done", "dropped") for t in ("ready", "triage")}
)


def task(rec, title="t", **kw):
    kw.setdefault("filed_by", "alpha")
    return rec.file_item("task", title, f"body of {title}", **kw)


def mixed(rec, clock):
    """A run of mixed operations over every kind step 2 writes."""
    admitted(rec, "0001")
    beta = rec.file_effort(None, "beta", "Beta", "owner-a", PARTS)
    clock.step()
    p = rec.file_item(
        "plan",
        "A plan",
        "the plan",
        role="alpha",
        filed_by="alpha",
        effort="0001",
        labels=["team:alpha", "repo:widgets"],
        flags=["needs:lab"],
    )
    c = task(rec, "A step", parent=p, role="alpha")
    loose = rec.file_item("finding", "Loose", "found", filed_by="beta", machine=True)
    rec.link(beta, "item", str(c))
    rec.link("0001", "plan", "plan-7")
    rec.link("0001", "effort", beta)
    clock.step()
    rec.edit_item(c, title="A step, renamed", body="do it well", tier=3, risk=2)
    for state in ("claimed", "in-review", "done", "ready"):
        rec.move(c, state, f"to {state}")
        clock.step()
    dep = rec.add_dep(p, blocker=c, why="the step first")
    rec.lift_dep(dep, "alpha", "done")
    rec.add_dep(c, what="a decision", why="waits")
    hold = rec.hold(number=loose, cause="quarantine", why="looks odd")
    rec.lift_hold(hold, "alpha")
    rec.hold(who="beta", cause="budget", why="over")
    rec.comment(c, "a comment", mark="answer")
    rec.comment(p, "another")
    first = rec.state("decision", "do it", effort="0001", number=p, where="c:1")
    rec.state("observation", "noted", supersedes=first)
    rec.admit(loose, "alpha")
    rec.edit_effort(beta, title="Beta, renamed", gates={"pace": 1.5})
    rec.set_standing(beta, "admitted", "go")
    rec.unlink("0001", "plan", "plan-7")
    rec.note("marker", f"item:{p}", detail="a note")
    rec.conclude(beta, "complete", "done")
    return p, c, loose


def efforts_of(rec, n):
    return sorted(
        link["effort"]
        for link in rec.read("effort_links", target=str(n), live=True)
        if link["kind"] == "item"
    )


def replay(dump):
    view = MemoryView()
    for raw in dump["journal"]:
        apply(view, Event.from_dict(raw))
    return view.records()


# -- the acceptance items -----------------------------------------------------


@over_backends
def test_a_refused_operation_writes_nothing_and_takes_no_number(record):
    """templet:3159 -- a refused operation writes no record, no event and
    takes no id or seq: the next operation gets the numbers it would have
    had."""
    admitted(record)
    n = task(record, "first", effort="0001")
    head, before = record.head(), record.dump()
    refusals = [
        lambda: task(record, effort="nope"),
        lambda: task(record, parent=999),
        lambda: task(record, number=50),
        lambda: task(record, labels=["state:ready"]),
        lambda: task(record, title="x" * (300 * 1024)),
        lambda: record.move(n, "merge-ready", "a skip"),
        lambda: record.move(999, "ready", "no item"),
        lambda: record.add_dep(n, blocker=n, why="itself"),
        lambda: record.add_dep(n, why="none of the three"),
        lambda: record.lift_dep(77, "alpha", "no dep"),
        lambda: record.comment(999, "on nothing"),
        lambda: record.hold(cause="c", why="neither number nor who"),
        lambda: record.lift_hold(5, "alpha"),
        lambda: record.file_effort("0001", "dup", "Dup", "owner-a", PARTS),
        lambda: record.conclude("0001", None, "no outcome"),
        lambda: record.link("0001", "item", "999"),
        lambda: record.state("musing", "not a type"),
        lambda: record.admit(n, "alpha"),
    ]
    for refuse in refusals:
        with pytest.raises(Refused):
            refuse()
        assert record.head() == head
        assert record.dump() == before
    m = task(record, "second", effort="0001")
    assert m == n + 1
    assert record.head() == head + 1
    assert record.add_dep(m, what="a decision", why="w") == 1
    assert record.comment(m, "the first comment") == 1
    assert record.hold(number=m, cause="c", why="w") == 1


@over_backends
def test_the_replay_of_the_journal_equals_the_records(record, clock):
    """templet:3159 -- after a run of mixed operations, the replay of the
    journal from seq 1 equals the journaled records of dump()."""
    mixed(record, clock)
    dump = record.dump()
    assert dump["format"] == 1
    assert set(dump["records"]) == {k for k, v in RECORDS.items() if v.journaled}
    assert [e["seq"] for e in dump["journal"]] == list(range(1, record.head() + 1))
    assert replay(dump) == dump["records"]
    assert record.check() == []


@over_backends
def test_events_come_in_order_with_no_gap_and_carry_every_put(record, clock):
    """templet:3159 -- events(cursor) gives seqs from cursor + 1 in order,
    with no gap, and an operation's event carries every put it made."""
    p, c, _ = mixed(record, clock)
    head = record.head()
    for cursor in (0, 1, head // 2, head - 1, head):
        got = record.events(cursor, limit=10_000)
        assert [e.seq for e in got] == list(range(cursor + 1, head + 1))
        assert all(isinstance(e, Event) for e in got)
    seen, cursor = [], 0
    while page := record.events(cursor, limit=3):
        assert len(page) <= 3
        seen += [e.seq for e in page]
        cursor = page[-1].seq
    assert seen == list(range(1, head + 1))
    assert record.events(head) == []

    before = record.dump()["records"]
    n = task(record, "carried", role="alpha", parent=p, labels=["team:alpha"])
    after = record.dump()["records"]
    (event,) = record.events(head)
    assert event.seq == head + 1 and event.kind == "item.file"
    assert event.subject == f"item:{n}" and event.actor == "alpha"
    changed = []
    for kind, rows in after.items():
        old = {tuple(schema.key_of(kind, r)): r for r in before[kind]}
        for row in rows:
            key = tuple(schema.key_of(kind, row))
            if old.get(key) != row:
                changed.append((kind, list(key), row))

    def by_key(puts):
        return sorted(([k, list(key), v] for k, key, v in puts), key=lambda p: p[:2])

    assert by_key(event.puts) == by_key(changed)
    assert {k for k, _, _ in event.puts} >= {"items", "item_text", "effort_links"}
    subject = record.events(0, subject=f"item:{c}")
    assert subject and all(e.subject == f"item:{c}" for e in subject)
    assert [e.seq for e in subject] == sorted(e.seq for e in subject)


@over_backends
def test_a_waiting_reader_wakes_within_a_second_of_a_commit(record):
    """templet:3159 -- a waiting events(..., wait=5) returns within a second
    of a commit by another caller."""
    admitted(record)
    head = record.head()
    started = time.monotonic()
    assert record.events(head, wait=0.3) == []
    assert 0.25 <= time.monotonic() - started < 2
    committed = {}

    def other_caller():
        time.sleep(0.5)
        task(record, "late", effort="0001")
        committed["at"] = time.monotonic()

    thread = threading.Thread(target=other_caller)
    thread.start()
    started = time.monotonic()
    got = record.events(head, wait=5)
    returned = time.monotonic()
    thread.join()
    assert [e.seq for e in got] == [head + 1]
    assert returned - committed["at"] < 1.0
    assert returned - started < 5


@over_backends
def test_ack_moves_a_cursor_forward_only_and_writes_no_event(record):
    """templet:3159 -- ack moves a cursor forward only, and an ack writes no
    event."""
    admitted(record)
    task(record, effort="0001")
    task(record, effort="0001")
    head = record.head()
    journal = record.dump()["journal"]
    assert record.cursor("follower") == 0
    record.ack("follower", 2)
    assert record.cursor("follower") == 2
    record.ack("follower", 1)
    assert record.cursor("follower") == 2
    record.ack("follower", head)
    assert record.cursor("follower") == head
    for bad in (head + 1, -1, "2", True):
        with pytest.raises(Refused):
            record.ack("follower", bad)
    assert record.cursor("follower") == head
    assert record.head() == head
    dump = record.dump()
    assert dump["journal"] == journal
    (row,) = dump["bookkeeping"]["cursors"]
    assert (row["consumer"], row["seq"]) == ("follower", head)


@over_backends
def test_a_dump_loaded_into_any_backend_dumps_the_same(record, clock, tmp_path):
    """templet:3159 -- dump() of a record loaded from another backend's
    dump() equals it."""
    mixed(record, clock)
    record.ack("follower", 3)
    dump = record.dump()
    for name in sorted(contract.BACKENDS):
        if name not in URLS:
            continue
        target = open_record(
            url_for(name, tmp_path / f"into-{name}"), actor="loader", via="load"
        )
        try:
            target.clock = clock
            target.init()
            target.load(dump)
            assert target.dump() == dump
            assert target.check() == []
            with pytest.raises(Refused, match="empty"):
                target.load(dump)
            n = task(target, "after the load", effort="0001")
            assert n == max(i["number"] for i in dump["records"]["items"]) + 1
            assert target.head() == len(dump["journal"]) + 1
        finally:
            target.close()


@over_backends
def test_a_composed_import_loads_and_dumps_the_same(record):
    """templet:3159 -- the dump compose makes of an import document loads
    into an empty record, which dumps it back unchanged."""
    dump = compose(make_doc())
    record.load(dump)
    assert record.dump() == dump
    assert record.check() == []


@over_backends
def test_read_on_a_field_outside_match_is_refused(record, clock):
    """templet:3159 -- read on a field outside the kind's match is refused."""
    p, c, loose = mixed(record, clock)
    for kind, field in (("items", "title"), ("item_text", "body"), ("efforts", "id")):
        with pytest.raises(Refused, match=field):
            record.read(kind, **{field: "x"})
    with pytest.raises(Refused):
        record.read("no-such-kind")
    with pytest.raises(Refused):
        record.read("items", state=3)
    with pytest.raises(Refused):
        record.read("item_text", live=True)
    assert [i["number"] for i in record.read("items", parent=p)] == [c]
    assert [i["number"] for i in record.read("items", parent=None)] == [p, loose]
    assert [i["number"] for i in record.read("items", desc=True)] == [loose, c, p]
    assert [i["number"] for i in record.read("items", limit=2)] == [p, c]
    assert [d["what"] for d in record.read("deps", live=True)] == ["a decision"]
    assert len(record.read("deps", live=False)) == 1
    assert record.read("items", state="ready", kind="task") == [record.get("items", c)]


@over_backends
def test_an_item_is_linked_to_unsorted_or_takes_its_parents_links(record):
    """templet:3159 -- an item filed with no effort and no parent is linked to
    `unsorted`; one with a parent takes its parent's links."""
    loose = task(record, "loose")
    assert efforts_of(record, loose) == ["unsorted"]
    assert record.get("efforts", "unsorted")["standing"] == "parked"
    assert efforts_of(record, task(record, "looser")) == ["unsorted"]
    admitted(record, "0001")
    admitted(record, "0002")
    p = task(record, "parent", effort="0001")
    record.link("0002", "item", str(p))
    child = task(record, "child", parent=p)
    assert efforts_of(record, child) == ["0001", "0002"]
    given = task(record, "given", parent=p, effort="0002")
    assert efforts_of(record, given) == ["0002"]
    grandchild = task(record, "grandchild", parent=child)
    assert efforts_of(record, grandchild) == ["0001", "0002"]
    # every item keeps an effort link (invariant 10)
    with pytest.raises(Refused, match="effort"):
        record.unlink("unsorted", "item", str(loose))
    # a target the readers would never find as the item's number
    for target in ("001", f"0{loose}", f"+{loose}"):
        with pytest.raises(Refused, match="number"):
            record.link("0001", "item", target)
    record.link("0001", "item", str(loose))
    record.unlink("unsorted", "item", str(loose))
    assert efforts_of(record, loose) == ["0001"]
    assert record.check() == []


@over_backends
def test_ready_leaves_out_what_is_not_eligible(record, clock):
    """templet:3159 -- ready() leaves out an item whose efforts are all
    parked, an unadmitted machine-filed should-fix item, an item with a live
    dep, and an item with a live hold; a must-fix machine-filed item is in it
    at once."""
    admitted(record, "0001")
    record.file_effort("0002", "parked", "Parked", "owner-a", PARTS, standing="parked")

    def ready_item(**kw):
        kw.setdefault("effort", "0001")
        kw.setdefault("role", "alpha")
        return task(record, **kw)

    good = ready_item()
    parked = ready_item(effort="0002")
    machine = ready_item(machine=True)
    must = ready_item(machine=True, must_fix=True)
    blocked = ready_item()
    dep = record.add_dep(blocked, what="a decision", why="w")
    held = ready_item()
    hold = record.hold(number=held, cause="quarantine", why="w")
    role_held = ready_item(role="beta")
    record.hold(who="beta", cause="budget", why="w")
    in_triage = task(record, effort="0001")
    unsorted = ready_item(effort=None)
    chained = ready_item(parent=good, effort=None)
    record.link("0002", "item", str(chained))
    record.unlink("0001", "item", str(chained))
    assert efforts_of(record, chained) == ["0002"]
    record.unlink("0002", "item", str(chained))  # its parent's 0001 is left
    assert efforts_of(record, chained) == []

    def ready():
        got = record.ready(now=clock())
        assert got["tasks"] == []
        return [i["number"] for i in got["items"]]

    assert ready() == [good, must, chained]
    assert record.get("items", machine)["admitted"] is False
    assert record.get("items", must)["admitted"] is True
    record.lift_dep(dep, "alpha", "done")
    record.lift_hold(hold, "alpha")
    record.admit(machine, "alpha")
    record.set_standing("0002", "admitted", "now")
    record.move(in_triage, "ready", "given a role")
    now = ready()
    assert now == sorted(
        [good, parked, machine, must, blocked, held, in_triage, chained]
    )
    assert role_held not in now and unsorted not in now


def test_moves_are_the_pages_pairs():
    """templet:3159 -- MOVES holds exactly the pairs of the page's table."""
    assert set(MOVES) == PAIRS
    for pair in [("done", "ready"), ("dropped", "triage"), ("claimed", "done")]:
        assert pair in MOVES
    assert ("in-review", "done") in MOVES
    assert ("triage", "claimed") not in MOVES


@over_backends
def test_an_item_in_review_may_be_blocked_and_the_move_replays(record):
    """templet:3159 -- `in-review` to `blocked` is a move, the one a hand-over
    that no next run of the author could cure ends in: it replays to the
    records and `check()` reports nothing of it. No other pair into `blocked`
    comes from a state that was not there before."""
    admitted(record)
    n = task(record, "handed over", effort="0001")
    for state in ("claimed", "in-review"):
        _walk(record, n, state)
    record.move(n, "blocked", "no next run could cure it", expect="in-review")
    assert record.get("items", n)["state"] == "blocked"
    assert [e.kind for e in record.events(0)][-1] == "item.state"
    dump = record.dump()
    assert replay(dump) == dump["records"]
    assert record.check() == []
    record.move(n, "ready", "cured")
    assert {f for f, t in MOVES if t == "blocked"} == {
        "ready",
        "claimed",
        "in-review",
    }


def _walk(record, n, to):
    """Move item n, from wherever it is, to `to` by moves in MOVES."""
    start = record.get("items", n)["state"]
    paths, seen = [[start]], {start}
    while paths:
        path = paths.pop(0)
        if path[-1] == to:
            for state in path[1:]:
                record.move(n, state, "a walk")
            return
        for f, t in sorted(MOVES):
            if f == path[-1] and t not in seen:
                seen.add(t)
                paths.append(path + [t])
    raise AssertionError(f"no walk from {start} to {to}")


@over_backends
def test_a_move_not_in_moves_is_refused_and_so_is_a_stale_expect(record, clock):
    """templet:3159 -- a move not in MOVES is refused, and move(...,
    expect=) with a stale state is refused; done and dropped set closed_at
    and a reopen clears it."""
    admitted(record)
    for f in ITEM_STATES:
        n = task(record, f"from {f}", effort="0001")
        _walk(record, n, f)
        head = record.head()
        for t in ITEM_STATES:
            if (f, t) not in MOVES:
                with pytest.raises(Refused, match="move"):
                    record.move(n, t, "not allowed")
        assert record.head() == head
        assert record.get("items", n)["state"] == f
    for f, t in sorted(MOVES):
        n = task(record, f"{f} to {t}", effort="0001")
        _walk(record, n, f)
        clock.step()
        record.move(n, t, "allowed", expect=f)
        item = record.get("items", n)
        assert item["state"] == t and item["updated_at"] == clock()
        assert (item["closed_at"] is not None) == (t in ("done", "dropped"))
    n = task(record, "stale", effort="0001")
    head = record.head()
    with pytest.raises(Refused, match="triage"):
        record.move(n, "ready", "stale", expect="blocked")
    assert record.head() == head
    record.move(n, "ready", "fresh", expect="triage")


@over_backends
def test_authorize_returning_a_reason_refuses_the_operation(opener):
    """templet:3159 -- authorize returning a reason refuses the operation
    with it."""
    calls = []

    def authorize(actor, op, before, after):
        calls.append((actor, op, before, after))
        if op == "move" and after[0][2]["state"] == "done":
            return "only the user closes an item"
        return None

    record = opener(authorize=authorize)
    record.init()
    admitted(record)
    n = task(record, role="alpha", effort="0001")
    head, dump = record.head(), record.dump()
    with pytest.raises(Refused, match="only the user closes an item") as refused:
        record.move(n, "done", "finished")
    assert refused.value.reason == "only the user closes an item"
    assert record.head() == head and record.dump() == dump
    actor, op, before, after = calls[-1]
    assert (actor, op) == ("alpha", "move")
    assert before[0][:2] == ["items", [n]] and before[0][2]["state"] == "ready"
    assert after[0][:2] == ["items", [n]] and after[0][2]["state"] == "done"
    filed = [c for c in calls if c[1] == "file_item"][0]
    assert filed[2][0][2] is None  # a new record has no before
    record.move(n, "blocked", "waits")
    assert record.get("items", n)["state"] == "blocked"


def _long_links_dump(clock, links):
    """A dump of one item linked to `links` efforts with 250-byte ids, in
    events of 200 puts."""
    at = clock()
    efforts = [
        sample(
            "efforts",
            id=f"{i:04d}" + "e" * 246,
            standing="admitted",
            created_at=at,
            order={"rule": "unblocks-then-age"},
        )
        for i in range(links)
    ]
    item = sample(
        "items",
        number=1,
        title="the parent",
        state="triage",
        filed_by="alpha",
        created_at=at,
        updated_at=at,
        source="local",
    )
    puts = [("efforts", e) for e in efforts]
    puts += [("items", item), ("item_text", sample("item_text", number=1))]
    puts += [
        (
            "effort_links",
            sample("effort_links", effort=e["id"], kind="item", target="1", at=at),
        )
        for e in efforts
    ]
    puts += [("meta", {"key": "next:items", "value": 2})]
    view, journal = MemoryView(), []
    for start in range(0, len(puts), 200):
        event = Event(
            seq=len(journal) + 1,
            at=at,
            actor="alpha",
            via="load",
            kind="import",
            subject="import:1",
            data={},
            puts=tuple(
                (k, schema.key_of(k, v), v) for k, v in puts[start : start + 200]
            ),
        )
        apply(view, event)
        journal.append(event.to_dict())
    return {
        "format": 1,
        "records": view.records(),
        "journal": journal,
        "bookkeeping": {"beats": [], "cursors": []},
    }


@over_backends
def test_an_entry_past_its_limit_is_refused_though_its_event_is_under(record, clock):
    """templet:3159 -- an operation whose journal entry, the event and its
    priors by schema.size, passes 768 KiB is refused on every backend, though
    its event alone is under it."""
    record.load(_long_links_dump(clock, 800))
    head, dump = record.head(), record.dump()
    with pytest.raises(Refused, match="journal entry") as refused:
        task(record, "child", parent=1)
    entry, event = map(
        int,
        re.search(r"of (\d+) bytes \(its event (\d+)\)", str(refused.value)).groups(),
    )
    limit = schema.LIMITS["entry_bytes"]
    assert event < limit < entry
    assert record.head() == head and record.dump() == dump
    # the refusal took no number
    assert task(record, "small") == 2


# -- the operations, one by one -----------------------------------------------


@over_backends
def test_efforts_are_filed_edited_and_concluded(record):
    """templet:3159 -- file_effort refuses an id taken, takes the next id from
    its counter when given none, set_standing refuses a concluded effort and
    conclude needs an outcome."""
    assert admitted(record, "0001") == "0001"
    assert record.file_effort(None, "b", "B", "owner-a", PARTS) == "0002"
    with pytest.raises(Refused, match="taken"):
        record.file_effort("0002", "c", "C", "owner-a", PARTS)
    with pytest.raises(Refused):
        record.file_effort("0003", "c", "C", "owner-a", {"colour": "blue"})
    with pytest.raises(Refused):
        record.file_effort("0003", "c", "C", "owner-a", PARTS, standing="concluded")
    effort = record.get("efforts", "0002")
    assert effort["standing"] == "proposed"
    assert effort["order"] == {"rule": "unblocks-then-age"}
    assert record.edit_effort("0002", title="B2", order={"rule": "age"}) is True
    assert record.edit_effort("0002", title="B2") is False
    with pytest.raises(Refused):
        record.edit_effort("0002", standing="admitted")
    record.set_standing("0002", "parked", "later")
    with pytest.raises(Refused):
        record.conclude("0002", "", "none")
    with pytest.raises(Refused):
        record.conclude("0002", "victory", "not an outcome")
    record.conclude("0002", "incomplete", "stopped")
    done = record.get("efforts", "0002")
    assert (done["standing"], done["outcome"]) == ("concluded", "incomplete")
    assert done["concluded_at"] is not None
    with pytest.raises(Refused, match="concluded"):
        record.set_standing("0002", "admitted", "again")
    with pytest.raises(Refused, match="concluded"):
        record.conclude("0002", "complete", "twice")
    record.link("0001", "effort", "0002")
    with pytest.raises(Refused, match="already"):
        record.link("0001", "effort", "0002")
    assert record.check() == []


@over_backends
def test_items_are_filed_and_edited(record, clock):
    """templet:3159 -- an item is filed in triage without a role and ready
    with one, sorted labels, a body in item_text; `number` only by an import;
    edit_item refuses a cycle of parents and writes nothing for no change."""
    admitted(record)
    n = task(record, "first", effort="0001", labels=["team:alpha", "repo:widgets"])
    item = record.get("items", n, text=True)
    assert item["state"] == "triage" and item["body"] == "body of first"
    assert item["labels"] == ["repo:widgets", "team:alpha"]
    assert (item["source"], item["admitted"], item["review_round"]) == (
        "local",
        True,
        0,
    )
    assert "body" not in record.get("items", n)
    m = task(record, "second", effort="0001", role="alpha", parent=n)
    assert record.get("items", m)["state"] == "ready"
    head = record.head()
    assert record.edit_item(m, title="second") is False
    assert record.head() == head
    clock.step()
    assert record.edit_item(m, title="Second", body="new body", tier=2) is True
    edited = record.get("items", m, text=True)
    assert (edited["title"], edited["body"], edited["tier"]) == (
        "Second",
        "new body",
        2,
    )
    assert edited["updated_at"] == clock()
    with pytest.raises(Refused, match="cycle"):
        record.edit_item(n, parent=m)
    with pytest.raises(Refused):
        record.edit_item(n, state="done")
    with pytest.raises(Refused):
        record.edit_item(n, kind="epic")
    with pytest.raises(Refused, match="import"):
        task(record, number=40)
    with pytest.raises(Refused):
        record.get("items", "one")
    assert record.get("items", 999) is None
    # validated before it is compared: 1.0 and True equal 1, and are refused
    assert record.edit_item(n, tier=1) is True
    head = record.head()
    for bad in (1.0, True):
        with pytest.raises(Refused, match="tier"):
            record.edit_item(n, tier=bad)
    assert record.head() == head and record.get("items", n)["tier"] == 1


@over_backends
def test_an_import_gives_an_items_number(opener):
    """templet:3159 -- `number` is taken only via import, and the counter goes
    on past it."""
    record = opener(via="import")
    record.init()
    admitted(record)
    assert task(record, number=40, effort="0001") == 40
    with pytest.raises(Refused, match="taken"):
        task(record, number=40, effort="0001")
    assert task(record, effort="0001") == 41


@over_backends
def test_deps_refuse_a_cycle_and_a_second_live_dep(record):
    """templet:3159 -- add_dep refuses a cycle and a dep already live, and
    lift_dep a dep already lifted."""
    admitted(record)
    a, b, c = (task(record, x, effort="0001") for x in "abc")
    record.add_dep(a, blocker=b, why="b first")
    record.add_dep(b, blocker=c, why="c first")
    with pytest.raises(Refused, match="cycle"):
        record.add_dep(c, blocker=a, why="round")
    with pytest.raises(Refused, match="live"):
        record.add_dep(a, blocker=b, why="again")
    with pytest.raises(Refused):
        record.add_dep(a, blocker=b, what="and this", why="two at once")
    with pytest.raises(Refused):
        record.add_dep(a, change=1, why="no such change")
    d = record.add_dep(a, what="a decision", why="w")
    record.lift_dep(d, "alpha", "decided")
    with pytest.raises(Refused, match="lifted"):
        record.lift_dep(d, "alpha", "twice")
    assert record.add_dep(a, what="a decision", why="again") == d + 1


@over_backends
def test_comments_statements_holds_and_notes(record):
    """templet:3159 -- comment, state, hold, lift_hold and note write their
    records (a note none) and refuse what is not there."""
    admitted(record)
    n = task(record, effort="0001")
    k = record.comment(n, "hello", mark="answer", ref="r-1")
    assert record.get("comments", k)["who"] == "alpha"
    with pytest.raises(Refused):
        record.comment(None, "on nothing at all")
    s = record.state("decision", "yes", effort="0001", number=n)
    with pytest.raises(Refused):
        record.state("decision", "yes", supersedes=s + 5)
    with pytest.raises(Refused):
        record.state("decision", "yes", effort="0009")
    h = record.hold(who="beta", cause="budget", why="over")
    with pytest.raises(Refused):
        record.hold(number=n, who="beta", cause="c", why="both")
    record.lift_hold(h, "alpha")
    with pytest.raises(Refused, match="lifted"):
        record.lift_hold(h, "alpha")
    head = record.head()
    seq = record.note("marker", f"item:{n}", detail="x")
    assert seq == head + 1
    (event,) = record.events(head)
    assert (event.kind, event.puts, event.data) == (
        "note",
        (),
        {"kind": "marker", "detail": "x"},
    )


@over_backends
def test_an_event_the_declarations_refuse_is_refused(record):
    """An event whose data is not plain data, or whose subject is not a str,
    is Refused (not a raw SchemaError) and writes nothing."""
    admitted(record)
    n = task(record, effort="0001", role="alpha")
    head, dump = record.head(), record.dump()
    for refuse in (
        lambda: record.note("k", "s", x=float("nan")),
        lambda: record.note("k", "s", x=object()),
        lambda: record.note("k", 12),
        lambda: record.set_standing("0001", "parked", object()),
        lambda: record.move(n, "blocked", {1: "a key that is not a str"}),
        lambda: record.lift_dep(
            record.add_dep(n, what="w", why="w"), "alpha", float("inf")
        ),
    ):
        with pytest.raises(Refused):
            refuse()
    # the dep the last one added is the one event written
    assert record.head() == head + 1
    assert record.dump()["records"]["efforts"] == dump["records"]["efforts"]
    assert record.get("items", n)["state"] == "ready"
    assert record.check() == []


@over_backends
def test_check_reports_an_entry_past_its_limit(backend, record, tmp_path, monkeypatch):
    """templet:3159 -- check() measures each event's entry as load does, its
    priors from the replay before it, and reports one past its limit: what
    load would refuse."""
    admitted(record)
    n = task(record, effort="0001", title="large")
    before = {"items": record.get("items", n), "item_text": record.get("item_text", n)}
    record.edit_item(n, body="b" * 5000)
    assert record.check() == []
    dump = record.dump()
    (edit,) = record.events(record.head() - 1)
    # the edit's priors: the item and its text as they were; the rest: none
    edit_priors = [
        [k, list(key), {f: before[k][f] for f in RECORDS[k].match}]
        for k, key, _ in edit.puts
    ]
    edit_size = contract.entry_size(edit, edit_priors)
    for other in record.events(0, limit=edit.seq - 1):
        priors = [[k, list(key), None] for k, key, _ in other.puts]
        assert contract.entry_size(other, priors) < 1000
    assert edit_size > 1000
    monkeypatch.setitem(schema.LIMITS, "entry_bytes", 1000)
    assert record.check() == [
        f"journal: event {edit.seq}'s entry is {edit_size} bytes, past the limit "
        "of 1000"
    ]
    fresh = open_record(url_for(backend, tmp_path / "fresh"), actor="l", via="load")
    try:
        fresh.init()
        with pytest.raises(Refused, match=f"a journal entry of {edit_size} bytes"):
            fresh.load(dump)
    finally:
        fresh.close()


@over_backends
def test_reading_sees_one_seq(record):
    """templet:3159 -- inside reading() every read sees the record at the
    seq it yields, and no operation runs inside it."""
    admitted(record)
    task(record, effort="0001")
    with record.reading() as seq:
        assert seq == record.head() == 2
        with pytest.raises(Refused):
            task(record, effort="0001")
        assert len(record.read("items")) == 1
    task(record, effort="0001")
    assert record.head() == 3


@over_backends
def test_another_threads_reading_holds_an_operation_back_not_refuses_it(record):
    """An operation in one thread waits while another thread is inside
    reading(), and succeeds once the reading ends; only an operation in the
    reading's own thread is refused."""
    admitted(record)
    inside, release = threading.Event(), threading.Event()
    got = {}

    def reader():
        with record.reading() as seq:
            got["seq"] = seq
            try:
                task(record, "same thread", effort="0001")
            except Refused as e:
                got["own"] = e
            try:
                record.ack("follower", 1)
            except Refused as e:
                got["own_ack"] = e
            try:
                record.events(0, wait=1.0)
            except Refused as e:
                got["own_wait"] = e
            inside.set()
            release.wait(10)

    def writer():
        try:
            got["number"] = task(record, "other thread", effort="0001")
            record.ack("follower", 1)
        except Exception as e:  # reported below
            got["error"] = e
        got["done"] = time.monotonic()

    def follower():
        # another thread's wait is held back too, then waits for real
        try:
            got["followed"] = record.events(1, wait=5)
        except Exception as e:  # reported below
            got["follow_error"] = e

    held = threading.Thread(target=reader)
    held.start()
    assert inside.wait(10)
    waiting = threading.Thread(target=writer)
    following = threading.Thread(target=follower)
    following.start()
    waiting.start()
    time.sleep(0.3)
    assert "done" not in got  # it waits for the reading
    assert "followed" not in got
    released = time.monotonic()
    release.set()
    held.join(10)
    waiting.join(10)
    following.join(10)
    assert "error" not in got, got.get("error")
    assert "follow_error" not in got, got.get("follow_error")
    assert "inside reading()" in str(got["own"])
    assert "inside reading()" in str(got["own_ack"])
    assert "inside reading()" in str(got["own_wait"])
    assert [e.seq for e in got["followed"]] == [2]
    assert got["number"] == 1 and got["done"] >= released
    assert record.head() == got["seq"] + 1 == 2
    assert record.cursor("follower") == 1
    assert record.check() == []


@over_backends
def test_an_uninitialised_record_refuses_and_init_runs_once(opener):
    """templet:3159 -- a record not yet initialised refuses every operation
    and read, and init() refuses a record that exists."""
    record = opener()
    assert record.initialised is False
    with pytest.raises(Refused, match="init"):
        admitted(record)
    with pytest.raises(Refused, match="init"):
        record.head()
    record.init()
    assert record.initialised is True
    with pytest.raises(Refused, match="exists"):
        record.init()
    assert record.head() == 0 and record.check() == []


@over_backends
def test_load_refuses_a_dump_that_is_not_whole(backend, record, clock, tmp_path):
    """templet:3159 -- load refuses a dump whose records are not the fold of
    its journal, whose seqs have a gap, or whose item has no effort link."""
    mixed(record, clock)
    dump = record.dump()
    fresh = open_record(url_for(backend, tmp_path / "fresh"), actor="l", via="load")
    try:
        fresh.init()
        bad = {**dump, "records": {**dump["records"]}}
        bad["records"]["items"] = dump["records"]["items"][:-1]
        with pytest.raises(Refused, match="fold"):
            fresh.load(bad)
        gap = {**dump, "journal": dump["journal"][:3] + dump["journal"][4:]}
        with pytest.raises(Refused, match="seq"):
            fresh.load(gap)
        with pytest.raises(Refused):
            fresh.load({**dump, "format": 2})
        assert fresh.head() == 0 and fresh.dump()["journal"] == []
        fresh.load(dump)
        assert fresh.dump() == dump
    finally:
        fresh.close()


@over_backends
def test_load_refuses_an_item_with_no_effort_link(record):
    """templet:3159 -- load refuses a dump with an item no effort is linked
    to (invariant 10)."""
    doc = make_doc()
    doc["records"]["effort_links"] = doc["records"]["effort_links"][:1]
    dump = compose(doc)
    with pytest.raises(Refused, match="item 2"):
        record.load(dump)
    assert record.head() == 0
