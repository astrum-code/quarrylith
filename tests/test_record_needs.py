# SPDX-License-Identifier: Apache-2.0
"""What the record gives a service, over every backend in BACKENDS
(templet:3159): a per-operation actor (`acting`) and a run's process group
(`run_pgid`), and that a record written before either still opens and replays.

`data/record-before-the-actor.json` and `.sql` were written by the record as it
stood before these two: its dump, and its SQLite file as text."""

import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from backends import admitted, one_writer, over_backends, url_for

from quarrylith.record.contract import Refused, open_record
from quarrylith.record.events import MemoryView, apply

DATA = Path(__file__).resolve().parent / "data"
BEFORE = DATA / "record-before-the-actor.json"
BEFORE_SQL = DATA / "record-before-the-actor.sql"
#: The run the record written before holds live, and the one it finished.
BEFORE_LIVE, BEFORE_FINISHED = 2, 1


def item(rec, title="t", **kw):
    kw.setdefault("filed_by", "alpha")
    kw.setdefault("effort", "0001")
    kw.setdefault("role", "alpha")
    return rec.file_item("task", title, f"body of {title}", **kw)


def node(rec, name="n1"):
    rec.declare_node(name, ["process"], ["zone:a"])
    rec.set_capacity(name, "slots", 3, "slots")


def job(rec):
    return rec.file_task("job", needs={"slots": 1}, effort="0001", sandbox="process")


def a_run(rec, **kw):
    """A live run of a new job, on node n1 (made if it is not there)."""
    if rec.get("nodes", "n1") is None:
        node(rec)
    kw.setdefault("expect_s", 60)
    return rec.start_run(
        job(rec), "alpha", fence=None, node="n1", sandbox="process", **kw
    )


def finish(rec, run):
    rec.finish_run(run, "done", "finished", footprint={}, cost={})


def last(rec):
    return rec.events(rec.head() - 1)[-1]


class Spy:
    """An `authorize` that records each call and refuses the actors in
    `deny`."""

    def __init__(self, *deny):
        self.deny = deny
        self.calls = []

    def __call__(self, actor, op, before, after):
        self.calls.append((actor, op, before, after))
        if actor in self.deny:
            return f"{actor} may not {op}"
        return None


# -- the per-operation actor ----------------------------------------------------


@over_backends
def test_acting_names_the_caller_to_authorize_and_on_the_event(opener):
    """templet:3159 -- inside `acting(actor, via)` an operation calls
    `authorize` with that actor and its event names that actor and via; outside
    it, and after it, the actor and via fixed at open."""
    spy = Spy()
    rec = opener(actor="scheduler", via="service", authorize=spy)
    rec.init()
    admitted(rec)
    with rec.acting("runner:a", "cli"):
        n = item(rec, "theirs")
    assert spy.calls[-1][0] == "runner:a"
    event = last(rec)
    assert (event.actor, event.via) == ("runner:a", "cli")
    assert event.subject == f"item:{n}"
    item(rec, "its own")
    assert spy.calls[-1][0] == "scheduler"
    event = last(rec)
    assert (event.actor, event.via) == ("scheduler", "service")
    assert rec.check() == []


@over_backends
def test_acting_stamps_every_field_that_names_the_actor(opener):
    """templet:3159 -- inside `acting`, the data's `by`, `added_by`, `set_by`,
    `released_by`, `declared_by` and `who` name that actor, not the one fixed
    at open."""
    rec = opener(actor="scheduler", via="service")
    rec.init()
    admitted(rec)
    who = "user-a"
    with rec.acting(who, "service"):
        n = item(rec, "filed")
        other = item(rec, "other")
        rec.link("0001", "plan", "a-plan")
        dep = rec.add_dep(n, what="a thing", why="it waits")
        hold = rec.hold(number=other, cause="a cause", why="held")
        comment = rec.comment(n, "words")
        question = rec.ask(n, "why?", kind="blocked")
        statement = rec.state("decision", "so it is", number=n)
        rec.declare_node("n1", ["process"], [])
        rec.set_capacity("n1", "slots", 3, "slots")
        change = rec.open_or_update_change(
            "repo", "feat/x", "abc", title="a title", by=who
        )
        rec.record_check(change, "abc", "gate", "green")
    assert rec.get("deps", dep)["added_by"] == who
    assert rec.get("holds", hold)["set_by"] == who
    assert rec.get("comments", comment)["who"] == who
    assert rec.get("questions", question)["who"] == who
    assert rec.get("statements", statement)["who"] == who
    assert rec.get("capacity", ["n1", "slots"])["declared_by"] == who
    assert rec.get("checks", [change, "abc", "gate"])["by"] == who
    assert rec.get("effort_links", ["0001", "plan", "a-plan"])["by"] == who
    assert rec.get("effort_links", ["0001", "item", str(n)])["by"] == who
    with rec.acting(who, "service"):
        rec.lift_hold(hold, who)
        rec.grant(other, "alpha", branch="feat/o", machine="m1")
        rec.move(other, "dropped", "no longer")
    lease = rec.read("leases", number=other)[0]
    assert lease["released_by"] == who
    assert {e.actor for e in rec.events(1)} == {who}
    assert rec.check() == []


@over_backends
def test_a_caller_authorize_denies_writes_nothing(opener):
    """templet:3159 -- `authorize` sees the caller inside `acting`: it refuses
    that caller's operation, and nothing is written; the same operation passes
    under the actor fixed at open."""
    spy = Spy("runner:a")
    rec = opener(actor="scheduler", via="service", authorize=spy)
    rec.init()
    admitted(rec)
    head, dump = rec.head(), rec.dump()
    with rec.acting("runner:a", "service"):
        with pytest.raises(Refused, match="runner:a may not file_item"):
            item(rec, "denied")
    assert rec.head() == head and rec.dump() == dump
    item(rec, "allowed")
    assert rec.head() == head + 1


@over_backends
def test_the_actor_comes_back_after_an_error_and_a_nested_span(opener):
    """templet:3159 -- a span ends with its body, on an exception too, and a
    span inside a span gives the outer one's actor back."""
    rec = opener(actor="scheduler", via="service")
    rec.init()
    admitted(rec)
    with pytest.raises(Refused):
        with rec.acting("runner:a", "cli"):
            rec.move(404, "done", "no such item")
    item(rec, "after an error")
    assert (last(rec).actor, last(rec).via) == ("scheduler", "service")
    with pytest.raises(ValueError):
        with rec.acting("runner:a", "cli"):
            raise ValueError("the caller's own")
    item(rec, "after the caller's error")
    assert (last(rec).actor, last(rec).via) == ("scheduler", "service")
    with rec.acting("outer", "cli"):
        with rec.acting("inner", "hook"):
            item(rec, "inner")
            assert (last(rec).actor, last(rec).via) == ("inner", "hook")
        item(rec, "outer")
        assert (last(rec).actor, last(rec).via) == ("outer", "cli")
    item(rec, "after")
    assert (last(rec).actor, last(rec).via) == ("scheduler", "service")


@over_backends
def test_another_threads_operation_waits_for_the_span_under_its_own_actor(opener):
    """templet:3159 -- a span holds the record's lock: another thread's
    operation waits for it to end and is written under that thread's actor, so
    a span never names anyone else's operation."""
    rec = opener(actor="scheduler", via="service")
    rec.init()
    admitted(rec)
    begun, got = threading.Event(), {}

    def other():
        begun.set()
        try:
            got["n"] = item(rec, "the other thread's")
        except Exception as e:  # reported below
            got["error"] = e

    thread = threading.Thread(target=other)
    with rec.acting("runner:a", "cli"):
        thread.start()
        assert begun.wait(10)
        time.sleep(0.3)
        assert "n" not in got and "error" not in got  # it waits for the span
        mine = item(rec, "the span's")
    thread.join(10)
    assert "error" not in got, got.get("error")
    actors = {e.subject: e.actor for e in rec.events(0) if e.kind == "item.file"}
    assert actors == {f"item:{mine}": "runner:a", f"item:{got['n']}": "scheduler"}


@over_backends
def test_no_wait_and_no_operation_where_a_span_would_hold_them_up(opener):
    """templet:3159 -- `events(wait=)` inside a span is refused: the span holds
    every other thread's operation back, so it could only end empty. A read
    with no wait goes on, and an operation inside `reading()` is refused as
    ever, in a span or not."""
    rec = opener(actor="scheduler", via="service")
    rec.init()
    admitted(rec)
    head = rec.head()
    started = time.monotonic()
    with rec.acting("runner:a", "cli"):
        with pytest.raises(Refused, match="inside acting"):
            rec.events(head, wait=5)
        assert rec.events(head) == []
        with rec.reading():
            with pytest.raises(Refused, match="inside reading"):
                item(rec, "in a reading")
    assert time.monotonic() - started < 3
    assert rec.head() == head


@over_backends
def test_bookkeeping_goes_on_in_a_span_and_writes_no_event(opener):
    """templet:3159 -- `beat` and `ack` run inside a span, write no event and
    do not reach `authorize`: nothing of them names an actor."""
    spy = Spy("runner:a")
    rec = opener(actor="scheduler", via="service", authorize=spy)
    rec.init()
    admitted(rec)
    run = a_run(rec)
    head, calls = rec.head(), len(spy.calls)
    with rec.acting("runner:a", "service"):
        rec.beat(run)
        rec.ack("follower", 1)
    assert rec.head() == head and len(spy.calls) == calls
    assert rec.cursor("follower") == 1


@over_backends
@pytest.mark.parametrize(
    "actor,via,why",
    [
        ("", "service", "an actor is a non-empty str"),
        (None, "service", "an actor is a non-empty str"),
        (7, "service", "an actor is a non-empty str"),
        ("runner:a", "telepathy", "no via"),
        ("runner:a", None, "no via"),
    ],
)
def test_acting_refuses_what_an_open_refuses(opener, actor, via, why):
    """templet:3159 -- `acting` refuses what an open refuses, before the span
    begins: the record goes on under the actor it had."""
    rec = opener(actor="scheduler", via="service")
    rec.init()
    admitted(rec)
    with pytest.raises(Refused, match=why):
        with rec.acting(actor, via):
            raise AssertionError("the span began")
    item(rec)
    assert (last(rec).actor, last(rec).via) == ("scheduler", "service")


@over_backends
def test_the_rule_on_a_number_follows_the_spans_via(opener):
    """templet:3159 -- a span's via is the operation's in every rule: `number`
    is taken only via import, whichever the handle was opened with."""
    rec = opener(actor="scheduler", via="service")
    rec.init()
    admitted(rec)
    with pytest.raises(Refused):
        item(rec, "numbered", number=40)
    with rec.acting("importer", "import"):
        assert item(rec, "numbered", number=40) == 40
    with pytest.raises(Refused):
        item(rec, "numbered", number=41)


# -- a run's process group --------------------------------------------------------


@over_backends
def test_run_pgid_sets_a_live_runs_group_in_its_own_event(record):
    """templet:3159 -- `run_pgid(id, pgid)` sets a live run's `pgid`: one event
    `run.pgid`, of the run's subject, with the run, its task and the group in
    its data, putting the run and changing nothing else of it. A later phase
    and the finish keep it."""
    admitted(record)
    run = a_run(record)
    before = record.get("runs", run)
    assert before["pgid"] is None
    record.clock.step()
    head = record.head()
    record.run_pgid(run, 4321)
    assert record.head() == head + 1
    event = last(record)
    assert event.kind == "run.pgid" and event.subject == f"run:{run}"
    assert event.data == {"run": run, "task": before["task"], "pgid": 4321}
    assert event.puts == (("runs", [run], {**before, "pgid": 4321}),)
    # the stall rule reads phase_at: a group sent late moves nothing of it
    assert record.get("runs", run) == {**before, "pgid": 4321}
    assert [e.kind for e in record.events(0, subject=f"run:{run}")][-1] == "run.pgid"
    record.run_phase(run, "running", expect_s=60)
    assert record.get("runs", run)["pgid"] == 4321
    finish(record, run)
    assert record.get("runs", run)["pgid"] == 4321
    assert record.check() == []


@over_backends
@pytest.mark.parametrize("again", [4321, 9876])
def test_run_pgid_is_refused_when_it_is_set(record, again):
    """templet:3159 -- a run's `pgid` is set once: a second `run_pgid`, of the
    same group or another, is refused, naming the one it holds, and writes
    nothing."""
    admitted(record)
    run = a_run(record)
    record.run_pgid(run, 4321)
    head = record.head()
    with pytest.raises(Refused, match="4321"):
        record.run_pgid(run, again)
    assert record.head() == head
    assert record.get("runs", run)["pgid"] == 4321


@over_backends
def test_run_pgid_is_refused_for_a_run_not_live(record):
    """templet:3159 -- `run_pgid` is refused for a finished run and for a run
    that is not there, and writes nothing."""
    admitted(record)
    run = a_run(record)
    finish(record, run)
    head = record.head()
    with pytest.raises(Refused, match="finished"):
        record.run_pgid(run, 4321)
    with pytest.raises(Refused, match="no run"):
        record.run_pgid(404, 4321)
    assert record.head() == head
    assert record.get("runs", run)["pgid"] is None


@over_backends
@pytest.mark.parametrize("pgid", [1, 0, -5, True, "4321", 43.5, None])
def test_run_pgid_is_refused_for_what_is_no_group_to_signal(record, pgid):
    """templet:3159 -- a `pgid` is an int above 1: to `killpg`, 0 is the
    caller's own group, 1 is init's, and a negative one no group."""
    admitted(record)
    run = a_run(record)
    head = record.head()
    with pytest.raises(Refused, match="a pgid"):
        record.run_pgid(run, pgid)
    assert record.head() == head
    assert record.get("runs", run)["pgid"] is None


@over_backends
def test_run_pgid_goes_through_authorize_under_the_callers_actor(opener):
    """templet:3159 -- `run_pgid` reaches `authorize` as the operation
    `run_pgid`, with the run before and after, and under `acting` the caller's
    actor; a denied one writes nothing."""
    spy = Spy("runner:b")
    rec = opener(actor="scheduler", via="service", authorize=spy)
    rec.init()
    admitted(rec)
    run = a_run(rec)
    held = rec.get("runs", run)
    with rec.acting("runner:b", "service"):
        with pytest.raises(Refused, match="runner:b may not run_pgid"):
            rec.run_pgid(run, 4321)
    assert rec.get("runs", run)["pgid"] is None
    with rec.acting("runner:a", "service"):
        rec.run_pgid(run, 4321)
    actor, op, before, after = spy.calls[-1]
    assert (actor, op) == ("runner:a", "run_pgid")
    assert before == [["runs", [run], held]]
    assert after == [["runs", [run], {**held, "pgid": 4321}]]
    assert last(rec).actor == "runner:a"


@over_backends
def test_a_read_only_handle_sets_no_group(backend, record, opener):
    """templet:3159 -- `run_pgid` is an operation like any other: a read-only
    handle is refused."""
    admitted(record)
    run = a_run(record)
    one_writer(backend, record)
    reader = opener(readonly=True)
    with pytest.raises(Refused, match="read-only"):
        reader.run_pgid(run, 4321)


@over_backends
def test_an_event_of_a_group_replays_and_a_dump_of_it_loads(
    backend, record, tmp_path, clock
):
    """templet:3159 -- `run.pgid` goes through the fold: the replay gives the
    records `dump()` holds, a `dump` of it loads into an empty record of any
    backend, and that record's `check()` is clean and its dump the same."""
    admitted(record)
    run = a_run(record)
    record.run_pgid(run, 4321)
    other = a_run(record)
    finish(record, other)
    dump = record.dump()
    view = MemoryView()
    for event in record.events(0, limit=1000):
        apply(view, event)
    assert view.records() == dump["records"]
    assert record.check() == []
    fresh = open_record(url_for(backend, tmp_path / "fresh"), actor="l", via="load")
    fresh.clock = clock
    try:
        fresh.init()
        fresh.load(json.loads(json.dumps(dump)))
        assert fresh.dump() == dump
        assert fresh.check() == []
        assert fresh.get("runs", run)["pgid"] == 4321
    finally:
        fresh.close()


# -- what `record check` says of a group ------------------------------------------


def _append(dump, kind, run, **fields):
    """`dump` with one more event, of `kind`, putting `run` with `fields`."""
    runs = dump["records"]["runs"]
    at = next(i for i, r in enumerate(runs) if r["id"] == run)
    value = {**runs[at], **fields}
    runs[at] = value
    last_event = dump["journal"][-1]
    dump["journal"].append(
        {
            **last_event,
            "seq": last_event["seq"] + 1,
            "kind": kind,
            "subject": f"run:{run}",
            "data": {"run": run},
            "puts": [["runs", [run], value]],
        }
    )
    return dump


def _started_with_a_group(dump, run):
    """`dump` as if its `run.start` of `run` had set a group, and it had kept
    it."""
    for raw in dump["journal"]:
        for kind, key, value in raw["puts"]:
            if kind == "runs" and key == [run]:
                value["pgid"] = 4321
    for row in dump["records"]["runs"]:
        if row["id"] == run:
            row["pgid"] = 4321
    return dump


def _imported_with_a_group(dump, run):
    """`dump` as if an import had brought `run`, with its group."""
    dump = _started_with_a_group(dump, run)
    for raw in dump["journal"]:
        if raw["kind"] == "run.start" and raw["data"]["run"] == run:
            raw["kind"] = "import"
    return dump


FINDINGS = {
    "moved by a second run.pgid": (
        lambda d, live, _: _append(
            _append(d, "run.pgid", live, pgid=4321), "run.pgid", live, pgid=9876
        ),
        "its pgid set again",
    ),
    "set again to the same group": (
        lambda d, live, _: _append(
            _append(d, "run.pgid", live, pgid=4321), "run.pgid", live, pgid=4321
        ),
        "its pgid set again",
    ),
    "set by a run.phase": (
        lambda d, live, _: _append(d, "run.phase", live, pgid=4321),
        "its pgid changed by a run.phase",
    ),
    "moved by a run.finish": (
        lambda d, live, _: _append(
            _append(d, "run.pgid", live, pgid=4321),
            "run.finish",
            live,
            pgid=9876,
            finished_at=9,
        ),
        "its pgid changed by a run.finish",
    ),
    "on a finished run": (
        lambda d, _, done: _append(d, "run.pgid", done, pgid=4321),
        "its pgid set when it is finished",
    ),
    "to no group to signal": (
        lambda d, live, _: _append(d, "run.pgid", live, pgid=1),
        "its pgid set to 1",
    ),
    "a start that brings one": (
        lambda d, live, _: _started_with_a_group(d, live),
        "its pgid changed by a run.start",
    ),
}


@over_backends
@pytest.mark.parametrize("name", sorted(FINDINGS))
def test_record_check_reports_a_group_not_set_once(
    backend, record, tmp_path, clock, name
):
    """templet:3159 -- `check()`, on a record loaded from a dump that is the
    fold of its journal, reports a run whose `pgid` was not set once, by a
    `run.pgid`, on a live run, to a group above 1."""
    admitted(record)
    live, done = a_run(record), a_run(record)
    finish(record, done)
    change, words = FINDINGS[name]
    dump = change(json.loads(json.dumps(record.dump())), live, done)
    fresh = open_record(url_for(backend, tmp_path / "fresh"), actor="l", via="load")
    fresh.clock = clock
    try:
        fresh.init()
        fresh.load(dump)
        found = [f for f in fresh.check() if "pgid" in f]
        assert len(found) == 1 and words in found[0], found
    finally:
        fresh.close()


@over_backends
def test_record_check_takes_a_group_an_import_brought(backend, record, tmp_path, clock):
    """templet:3159 -- a run an import brought holds whatever group it had, and
    `check()` finds nothing of it; a `run_pgid` of it is refused as for any run
    with a group."""
    admitted(record)
    run = a_run(record)
    dump = _imported_with_a_group(json.loads(json.dumps(record.dump())), run)
    _append(dump, "run.phase", run, phase="running")
    fresh = open_record(url_for(backend, tmp_path / "fresh"), actor="l", via="load")
    fresh.clock = clock
    try:
        fresh.init()
        fresh.load(dump)
        assert [f for f in fresh.check() if "pgid" in f] == []
        with pytest.raises(Refused, match="4321"):
            fresh.run_pgid(run, 9876)
    finally:
        fresh.close()


# -- a record written before these ------------------------------------------------


@over_backends
def test_a_record_written_before_loads_replays_and_takes_a_group(
    backend, tmp_path, clock
):
    """templet:3159 -- a dump written before `run.pgid` and `acting` loads on
    every backend, is the same dump, and is clean; its live run then takes a
    group under a caller's actor, and the record is clean after."""
    dump = json.loads(BEFORE.read_text(encoding="utf-8"))
    kinds = {raw["kind"] for raw in dump["journal"]}
    assert "run.start" in kinds and "run.pgid" not in kinds
    rec = open_record(url_for(backend, tmp_path / "old"), actor="l", via="load")
    rec.clock = clock
    try:
        rec.init()
        rec.load(json.loads(json.dumps(dump)))
        assert rec.dump() == dump
        assert rec.check() == []
        assert rec.get("runs", BEFORE_LIVE)["pgid"] is None
        with pytest.raises(Refused, match="finished"):
            rec.run_pgid(BEFORE_FINISHED, 4321)
        with rec.acting("runner:a", "service"):
            rec.run_pgid(BEFORE_LIVE, 4321)
        assert rec.get("runs", BEFORE_LIVE)["pgid"] == 4321
        event = last(rec)
        assert (event.kind, event.actor) == ("run.pgid", "runner:a")
        assert rec.check() == []
    finally:
        rec.close()


def test_a_sqlite_file_written_before_opens_replays_and_takes_a_group(tmp_path):
    """templet:3159 -- a SQLite file written before `run.pgid` and `acting`
    opens, with no migration, and is clean; its live run takes a group, and the
    file then opens again."""
    path = tmp_path / "before.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(BEFORE_SQL.read_text(encoding="utf-8"))
    conn.close()
    url = f"sqlite:///{path}"
    rec = open_record(url, actor="alpha", via="cli")
    dump = json.loads(BEFORE.read_text(encoding="utf-8"))
    assert rec.dump() == dump
    assert rec.check() == []
    rec.run_pgid(BEFORE_LIVE, 4321)
    assert rec.check() == []
    rec.close()
    rec = open_record(url, actor="alpha", via="cli")
    assert rec.get("runs", BEFORE_LIVE)["pgid"] == 4321
    assert rec.check() == []
    rec.close()
