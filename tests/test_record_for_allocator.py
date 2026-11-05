# SPDX-License-Identifier: Apache-2.0
"""What the record gains for the allocator (templet:3159, the changes proposed
on its page): `replace_reservation` and the `measured` gauge, over every
backend in BACKENDS."""

import json
from pathlib import Path

import pytest
from backends import admitted, over_backends, url_for
from test_gauges import contract_only, series, values

from quarrylith import gauges
from quarrylith.record import contract, events, schema
from quarrylith.record.contract import Refused, open_record
from quarrylith.record.events import Event, MemoryView, apply

DATA = Path(__file__).resolve().parent / "data"
MIN = 60_000


def standing(record, clock, holder, memory, node="n1", **kw):
    (id,) = record.reserve(
        node, {"memory": memory}, holder=holder, from_at=clock(), until_at=None, **kw
    )
    return id


def host(record, memory=4096, name="n1"):
    """A node with `memory` and three slots; returns its name."""
    record.declare_node(name, ["process"], [])
    record.set_capacity(name, "memory", memory, "bytes")
    record.set_capacity(name, "slots", 3, "slots")
    return name


def live(record, resource=None, holder=None, **match):
    """The live reservations, read by node, task or run and filtered here by
    resource or holder."""
    return [
        r
        for r in record.read("reservations", live=True, **match)
        if resource in (None, r["resource"]) and holder in (None, r["holder"])
    ]


def replay(record):
    view = MemoryView()
    dump = record.dump()
    for raw in dump["journal"]:
        apply(view, Event.from_dict(raw))
    assert view.records() == dump["records"]
    return dump


# -- replace_reservation ------------------------------------------------------


@over_backends
def test_replace_reservation_is_one_event_that_swaps_the_amount(record, clock):
    """templet:3159 -- `replace_reservation(id, amount, why)` releases a live
    reservation and reserves `amount` of the same node, resource, holder and
    task from now with no end, in one event, and returns the new id."""
    host(record)
    admitted(record)
    admitted_task = record.file_task(
        "job", effort="0001", needs={"slots": 1}, sandbox="process"
    )
    old = standing(record, clock, "podman", 1024, task=admitted_task)
    clock.step(5_000)
    before = record.head()
    new = record.replace_reservation(old, 2048, "the machine grew")
    assert new != old and record.head() == before + 1
    released = record.get("reservations", old)
    assert released["released_at"] == clock() and released["why"] == "the machine grew"
    (made,) = live(record, node="n1", resource="memory")
    assert made == {
        "id": new,
        "node": "n1",
        "resource": "memory",
        "amount": 2048,
        "task": admitted_task,
        "run": None,
        "holder": "podman",
        "from_at": clock(),
        "until_at": None,
        "released_at": None,
        "why": None,
    }
    assert record.free("n1", "memory", from_at=clock(), until_at=None) == 4096 - 2048
    (event,) = [e for e in record.dump()["journal"] if e["seq"] == record.head()]
    assert event["kind"] == "reserve.replace" and event["subject"] == "node:n1"
    assert [k for k, _, _ in event["puts"]].count("reservations") == 2
    assert event["data"]["released"] == old and event["data"]["id"] == new
    replay(record)
    assert record.check() == []


@over_backends
def test_replace_reservation_is_checked_without_the_old_one(record, clock):
    """templet:3159 -- the room is checked without the reservation it
    replaces: a holder may grow into its own room, and shrink."""
    host(record)
    mine = standing(record, clock, "podman", 3000)
    standing(record, clock, "cache", 1000)
    assert record.free("n1", "memory", from_at=clock(), until_at=None) == 96
    grown = record.replace_reservation(mine, 3096, "grew to the last byte")
    shrunk = record.replace_reservation(grown, 500, "shrank")
    (podman,) = live(record, holder="podman")
    assert podman["id"] == shrunk and podman["amount"] == 500
    assert record.free("n1", "memory", from_at=clock(), until_at=None) == 4096 - 1500
    assert record.check() == []


@over_backends
def test_replace_reservation_past_the_room_is_refused_whole(record, clock):
    """templet:3159 -- an amount that does not fit, even without the old
    reservation, is refused whole: nothing is written, the old one stays
    live."""
    host(record)
    mine = standing(record, clock, "podman", 3000)
    standing(record, clock, "cache", 1000)
    head, dump = record.head(), record.dump()
    with pytest.raises(Refused, match="no room"):
        record.replace_reservation(mine, 3097, "one byte too many")
    assert record.head() == head and record.dump() == dump
    assert record.get("reservations", mine)["released_at"] is None
    assert record.check() == []


@over_backends
def test_replace_reservation_checks_every_instant_from_now(record, clock):
    """templet:3159 -- a later reservation of another holder counts: the
    replacement runs from now with no end, so it must fit at every instant
    after."""
    host(record)
    t = clock()
    mine = standing(record, clock, "podman", 1000)
    record.reserve(
        "n1", {"memory": 2000}, holder="later", from_at=t + 60_000, until_at=None
    )
    head = record.head()
    with pytest.raises(Refused, match="no room"):
        record.replace_reservation(mine, 2200, "too big for later")
    assert record.head() == head
    assert record.replace_reservation(mine, 2096, "fits at every instant")


@over_backends
def test_replace_reservation_refuses_what_it_cannot_replace(record, clock):
    """templet:3159 -- it refuses a reservation a live run holds (the run
    keeps its room), one released already, one that is not there, and an
    amount that is not an int from 1; each writes nothing."""
    host(record)
    admitted(record)
    task = record.file_task("job", effort="0001", needs={"slots": 1}, sandbox="process")
    run = record.start_run(
        task, "alpha", node="n1", sandbox="process", fence=None, expect_s=60
    )
    (held,) = live(record, run=run)
    gone = standing(record, clock, "cache", 100)
    record.release_reservation(gone, "done")
    mine = standing(record, clock, "podman", 100)
    head, dump = record.head(), record.dump()
    cases = [
        (held["id"], 2, "finish_run"),
        (gone, 100, "released already"),
        (9999, 100, "no reservation"),
        (mine, 0, "int from 1"),
        (mine, -5, "int from 1"),
        (mine, 1.5, "int from 1"),
        (mine, True, "int from 1"),
        (mine, "100", "int from 1"),
    ]
    for id, amount, why in cases:
        with pytest.raises(Refused, match=why):
            record.replace_reservation(id, amount, "no")
    assert record.head() == head and record.dump() == dump


@over_backends
def test_replace_reservation_to_the_same_amount_writes_nothing(record, clock):
    """templet:3159 -- replacing a reservation by its own amount, open ended,
    writes nothing and returns its id: a second `declare` of the same
    document leaves the record as it was."""
    host(record)
    mine = standing(record, clock, "podman", 700)
    head = record.head()
    assert record.replace_reservation(mine, 700, "same") == mine
    assert record.head() == head
    assert record.get("reservations", mine)["released_at"] is None


@over_backends
def test_replace_reservation_holds_the_invariants_under_replay(record, clock, tmp_path):
    """templet:3159 -- after replaces, the replay of the journal equals the
    records, `check()` finds nothing, and the dump loads into every backend
    and dumps the same."""
    host(record)
    a = standing(record, clock, "podman", 1000)
    b = standing(record, clock, "cache", 500)
    clock.step()
    a = record.replace_reservation(a, 2500, "grew")
    clock.step()
    b = record.replace_reservation(b, 100, "shrank")
    dump = replay(record)
    assert record.check() == []
    assert [e["kind"] for e in dump["journal"]].count("reserve.replace") == 2
    assert {r["id"] for r in live(record, resource="memory")} == {a, b}
    for name in sorted(contract.BACKENDS):
        if name not in ("sqlite", "henge"):
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
        finally:
            target.close()


def test_the_replace_event_is_a_kind_of_the_journal():
    """templet:3159 -- `reserve.replace` is in `EVENT_KINDS`, after
    `reserve.release`, and an event of it is checked like any other: its puts
    are whole reservations under their own keys."""
    kinds = list(events.EVENT_KINDS)
    assert kinds.index("reserve.replace") == kinds.index("reserve.release") + 1
    row = {
        "id": 1,
        "node": "n1",
        "resource": "memory",
        "amount": 1,
        "task": None,
        "run": None,
        "holder": "h",
        "from_at": 0,
        "until_at": None,
        "released_at": None,
        "why": None,
    }
    event = Event(
        seq=1,
        at=1,
        actor="a",
        via="cli",
        kind="reserve.replace",
        subject="node:n1",
        data={},
        puts=(("reservations", [1], row),),
    )
    events.check_event(event)
    with pytest.raises(schema.SchemaError):
        events.check_event(
            Event(**{**event.__dict__, "puts": (("reservations", [2], row),)})
        )


@over_backends
def test_a_record_written_before_the_replace_still_opens_and_replays(
    backend, record, clock, tmp_path
):
    """templet:3159 -- a dump written before `reserve.replace` existed (a
    frozen one, `tests/data/record-before-replace.json`) loads, replays to
    its records, checks clean, and takes a replace on its live standing
    reservation afterwards."""
    old = json.loads((DATA / "record-before-replace.json").read_text("utf-8"))
    assert "reserve.replace" not in {e["kind"] for e in old["journal"]}
    record.load(old)
    assert record.dump() == old
    assert record.check() == []
    view = MemoryView()
    for raw in old["journal"]:
        apply(view, Event.from_dict(raw))
    assert view.records() == old["records"]
    clock.at = old["journal"][-1]["at"] + 1000
    (podman,) = live(record, holder="podman")
    new = record.replace_reservation(podman["id"], 4096, "after the load")
    assert record.get("reservations", new)["amount"] == 4096
    replay(record)
    assert record.check() == []


# -- the `measured` gauge -----------------------------------------------------


def measure(record, clock, node="n1", **fields):
    value = {"node": node, "memory_available": 3000, "disk_free": 900}
    value.update(fields)
    return record.record_reading("host", "measure", value)


def measured(record, now, **kw):
    return values(gauges.by_name("measured"), record, now, **kw)


def test_measured_is_a_gauge_beside_the_table_ones():
    """templet:3159 -- `measured` is in `GAUGES` after `free`, with the labels
    node, resource and holder and the resource's unit."""
    names = [g.name for g in gauges.GAUGES]
    assert names.index("measured") == names.index("free") + 1
    g = gauges.by_name("measured")
    assert g.labels == ("node", "resource", "holder")
    assert g.unit == "the resource's" and g.help and callable(g.compute)


@over_backends
def test_measured_reads_the_latest_measure_reading_of_each_node(record, clock):
    """templet:3159 -- the host's figures (holder "") and each consumer's,
    from the latest `host/measure` reading of the node, sorted by label."""
    host(record)
    host(record, name="n2")
    measure(record, clock, node="n1", memory_available=1, disk_free=2)
    clock.step()
    measure(
        record,
        clock,
        node="n1",
        memory_available=2500,
        disk_free=800,
        consumers={"podman": {"memory": 1900}, "cache": {"memory": 40, "disk": 7}},
    )
    clock.step()
    measure(record, clock, node="n2", memory_available=111, disk_free=222)
    assert measured(record, clock()) == {
        series(node="n1", resource="disk", holder=""): 800,
        series(node="n1", resource="disk", holder="cache"): 7,
        series(node="n1", resource="memory", holder=""): 2500,
        series(node="n1", resource="memory", holder="cache"): 40,
        series(node="n1", resource="memory", holder="podman"): 1900,
        series(node="n2", resource="disk", holder=""): 222,
        series(node="n2", resource="memory", holder=""): 111,
    }
    rows = gauges.by_name("measured").compute(
        contract_only(record), now=clock(), window_ms=MIN
    )
    keys = [(lab["node"], lab["resource"], lab["holder"]) for lab, _ in rows]
    assert keys == sorted(keys) and len(keys) == 7


@over_backends
def test_measured_has_no_series_before_a_reading(record, clock):
    """templet:3159 -- a record with no `host/measure` reading has no
    `measured` series: the table gauges are unchanged, and a reading of
    another source or kind does not count."""
    host(record)
    record.record_reading("host", "other", {"node": "n1", "memory_available": 5})
    record.record_reading("budget", "measure", {"node": "n1", "memory_available": 5})
    assert measured(record, clock()) == {}


@over_backends
def test_measured_skips_what_it_cannot_read(record, clock):
    """templet:3159 -- a reading with no node, one that names a node the
    record does not hold live, and a figure that is not a number are
    skipped, never an error."""
    host(record)
    host(record, name="gone")
    record.retire_node("gone")
    record.record_reading("host", "measure", {"memory_available": 5})
    record.record_reading("host", "measure", ["not", "a", "dict"])
    measure(
        record,
        clock,
        memory_available="lots",
        disk_free=True,
        consumers={
            "podman": {"memory": 1900, "disk": None},
            "odd": "x",
            "worse": {"memory": [1]},
        },
    )
    # the newest readings are the ones to skip
    measure(record, clock, node="gone")
    measure(record, clock, node="nowhere")
    record.record_reading("host", "measure", {"memory_available": 5})
    assert measured(record, clock()) == {
        series(node="n1", resource="memory", holder="podman"): 1900,
    }


@over_backends
def test_measured_is_the_latest_reading_whatever_its_age(record, clock):
    """templet:3159 -- the gauge shows the latest reading of a node, not
    only one inside the window: the allocator judges a reading's age, not the
    gauge. A newer reading of another node does not hide it."""
    host(record)
    host(record, name="n2")
    measure(record, clock, node="n1", memory_available=42)
    clock.step(10 * 60 * MIN)
    measure(record, clock, node="n2", memory_available=7)
    got = measured(record, clock(), window_ms=MIN)
    assert got[series(node="n1", resource="memory", holder="")] == 42
    assert got[series(node="n2", resource="memory", holder="")] == 7


@over_backends
def test_measured_is_computed_from_the_record_alone(record, clock):
    """templet:3159 -- `measure` (every gauge at one seq) gives the new
    gauge's series in its order, from the contract's reads alone."""
    host(record)
    measure(record, clock)
    every = gauges.measure(contract_only(record), now=clock(), window_ms=MIN)
    assert list(every)[: list(every).index("measured") + 1][-2:] == ["free", "measured"]
    assert every["measured"] == [
        ({"node": "n1", "resource": "disk", "holder": ""}, 900),
        ({"node": "n1", "resource": "memory", "holder": ""}, 3000),
    ]
