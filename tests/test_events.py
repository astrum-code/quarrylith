# SPDX-License-Identifier: Apache-2.0
"""The journal's event and its fold (templet:3159, build step 1)."""

import copy
import dataclasses
import json
import random
import time

import pytest
from samples import sample

from quarrylith.record import events, schema
from quarrylith.record.events import EVENT_KINDS, VIAS, Event, MemoryView, apply
from quarrylith.record.schema import RECORDS, SchemaError

KINDS = (
    "effort.file effort.edit effort.standing effort.conclude effort.link"
    " effort.unlink item.file item.edit item.state item.admit dep.add dep.lift"
    " lease.grant lease.renew lease.release task.file task.edit task.end"
    " node.declare node.retire capacity.set reserve reserve.release run.start"
    " run.phase run.finish change.open change.edit change.drop check.record"
    " review.file review.verdict review.read merge comment question.ask"
    " question.answer statement hold.set hold.lift reading meta.set note import"
).split()


def event(seq=1, puts=(), **over):
    fields = dict(
        seq=seq,
        at=1000 + seq,
        actor="alpha",
        via="cli",
        kind="item.file",
        subject=f"item:{seq}",
        data={"number": seq},
        puts=tuple(puts),
    )
    fields.update(over)
    return Event(**fields)


def put(kind="items", **over):
    rec = sample(kind, **over)
    return (kind, schema.key_of(kind, rec), rec)


def test_event_kinds_are_the_pages_list():
    """templet:3159 -- EVENT_KINDS is the page's list, and VIAS the callers'."""
    assert list(EVENT_KINDS) == KINDS
    assert len(set(EVENT_KINDS)) == len(EVENT_KINDS)
    assert tuple(VIAS) == ("cli", "service", "hook", "import", "load")


def test_an_event_is_frozen_and_round_trips_as_plain_data():
    """templet:3159 -- an Event holds seq, at, actor, via, kind, subject, data
    and puts; as a dict it is plain data and comes back equal."""
    ev = event(2, [put("items", number=2), put("item_text", number=2)])
    assert [f.name for f in dataclasses.fields(Event)] == [
        "seq",
        "at",
        "actor",
        "via",
        "kind",
        "subject",
        "data",
        "puts",
    ]
    with pytest.raises(dataclasses.FrozenInstanceError):
        ev.seq = 3
    as_dict = ev.to_dict()
    assert json.loads(json.dumps(as_dict)) == as_dict
    assert isinstance(as_dict["puts"], list) and isinstance(as_dict["puts"][0], list)
    assert Event.from_dict(as_dict) == ev
    assert Event.from_dict(json.loads(json.dumps(as_dict))) == ev


def test_an_event_is_checked():
    """templet:3159 -- an event of an unknown kind or via, a seq below 1, a put
    of an undeclared kind or of an invalid record, or a put under the wrong
    key is refused."""
    assert events.check_event(event(1, [put()])) is None
    for bad in (
        event(kind="item.nope"),
        event(via="telepathy"),
        event(seq=0),
        event(seq=True),
        event(at="now"),
        event(actor=""),
        event(subject=3),
        event(data=[1]),
        event(puts=(("nope", [1], {}),)),
        event(puts=(("items", [1], {"number": 1}),)),
        event(puts=(("items", [2], sample("items", number=1)),)),
        event(puts=(("items", [1]),)),
        event(puts=[["items", [1], sample("items", number=1)]]),
    ):
        with pytest.raises((SchemaError, ValueError, TypeError)):
            events.check_event(bad)


def test_apply_writes_the_puts_in_order_by_key():
    """templet:3159 -- apply writes event.puts into the view, in order, by key:
    the later put of a key wins."""
    first = put("items", number=1, title="first")
    second = put("items", number=1, title="second")
    other = put("items", number=2)
    view = MemoryView()
    apply(view, event(1, [first, other, second]))
    assert view.get("items", [1])["title"] == "second"
    assert [r["number"] for r in view.rows("items")] == [1, 2]


def test_apply_touches_nothing_but_the_view():
    """templet:3159 -- apply calls view.put once per put, in order, and reads
    no clock, no counter and no other record."""
    calls = []

    class Spy:
        def put(self, kind, key, value):
            calls.append((kind, key, value))

        def __getattr__(self, name):
            raise AssertionError(f"apply touched view.{name}")

    puts = [put("items", number=3), put("item_text", number=3)]
    apply(Spy(), event(1, puts))
    assert calls == puts


def test_apply_twice_equals_apply_once():
    """templet:3159 -- applying one event twice leaves the record as applying it
    once did, whatever else was applied between."""
    ev1 = event(1, [put("items", number=1), put("item_text", number=1)])
    ev2 = event(2, [put("items", number=1, title="moved"), put("beats", run=4)])
    once, twice = MemoryView(), MemoryView()
    apply(once, ev1)
    apply(once, ev2)
    apply(twice, ev1)
    apply(twice, ev1)
    apply(twice, ev2)
    apply(twice, ev2)
    assert once.records() == twice.records()
    assert once.bookkeeping() == twice.bookkeeping()
    assert once.get("items", [1])["title"] == "moved"


def test_apply_reads_no_clock_and_no_randomness(monkeypatch):
    """templet:3159 -- apply with the clock and the random source made to fail
    still folds the event; the module names neither."""

    def boom(*args, **kwargs):
        raise AssertionError("apply read a clock or a counter")

    for name in ("time", "time_ns", "monotonic", "monotonic_ns", "perf_counter"):
        monkeypatch.setattr(time, name, boom)
    monkeypatch.setattr(random, "random", boom)
    view = MemoryView()
    apply(view, event(1, [put("items", number=9)]))
    assert view.get("items", [9]) is not None
    source = open(events.__file__).read()
    for word in ("import time", "import random", "import datetime", "import uuid"):
        assert word not in source


def test_the_view_gives_rows_in_key_order_as_lists():
    """templet:3159 -- rows come in the one key order, keys as lists, and the
    journaled records and the bookkeeping are apart."""
    view = MemoryView()
    puts = [put("items", number=n) for n in (10, 2, 33, 1)]
    puts += [put("cursors", consumer="b"), put("cursors", consumer="a")]
    puts += [put("beats", run=2)]
    apply(view, event(1, puts))
    assert [r["number"] for r in view.rows("items")] == [1, 2, 10, 33]
    assert list(view.records()) == [k for k, v in RECORDS.items() if v.journaled]
    assert all(rows == [] for k, rows in view.records().items() if k != "items")
    assert [r["consumer"] for r in view.bookkeeping()["cursors"]] == ["a", "b"]
    assert [r["run"] for r in view.bookkeeping()["beats"]] == [2]
    assert view.keys("items") == [[1], [2], [10], [33]]


def test_the_view_refuses_what_the_schema_refuses():
    """templet:3159 -- a put of an invalid record, or under a key that is not
    the record's, is refused, and nothing is written."""
    view = MemoryView()
    with pytest.raises(SchemaError):
        view.put("items", [1], {"number": 1})
    with pytest.raises(SchemaError):
        view.put("items", [2], sample("items", number=1))
    with pytest.raises(SchemaError):
        view.put("nope", [1], {})
    assert view.get("items", [1]) is None and view.rows("items") == []


def test_the_view_keeps_its_own_copy():
    """templet:3159 -- changing a value after the put, or a row read, does not
    change the record."""
    rec = sample("items", number=1, labels=["a:b"])
    view = MemoryView()
    view.put("items", [1], rec)
    rec["labels"].append("c:d")
    view.get("items", [1])["labels"].append("e:f")
    view.rows("items")[0]["title"] = "changed"
    assert view.get("items", [1])["labels"] == ["a:b"]
    assert view.get("items", [1])["title"] == "x"
    snapshot = copy.deepcopy(view.records())
    assert view.records() == snapshot
