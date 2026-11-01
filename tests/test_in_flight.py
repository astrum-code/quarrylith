# SPDX-License-Identifier: Apache-2.0
"""Work in flight, over every backend in BACKENDS (templet:3159, build step 3):
leases and their view, tasks, nodes, capacity and reservations, runs started
and finished as single events, beats, and the ready read's tasks."""

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from backends import PARTS, URLS, admitted, over_backends, url_for

from quarrylith.record import contract, schema
from quarrylith.record.contract import Refused, open_record
from quarrylith.record.events import Event, MemoryView, apply

PROCS = Path(__file__).resolve().parent / "procs.py"


def item(rec, title="t", **kw):
    """An item filed ready (it has a role), under effort 0001."""
    kw.setdefault("filed_by", "alpha")
    kw.setdefault("effort", "0001")
    kw.setdefault("role", "alpha")
    return rec.file_item("task", title, f"body of {title}", **kw)


def grant(rec, n, holder="alpha", **kw):
    kw.setdefault("branch", f"feat/{n}")
    kw.setdefault("machine", "m1")
    return rec.grant(n, holder, **kw)


def node(rec, name="n1", slots=3, memory=4096):
    """A node that runs `harness` and `process`, with slots and memory."""
    rec.declare_node(name, ["process", "harness"], ["zone:a"])
    rec.set_capacity(name, "slots", slots, "slots")
    rec.set_capacity(name, "memory", memory, "bytes")


def job(rec, needs=None, **kw):
    """A task with no item, under effort 0001, that runs as a process."""
    kw.setdefault("effort", "0001")
    kw.setdefault("sandbox", "process")
    return rec.file_task("job", needs={"slots": 1} if needs is None else needs, **kw)


def agent_task(rec, n, needs=None, **kw):
    kw.setdefault("sandbox", "harness")
    return rec.file_task(
        "agent", number=n, needs={"slots": 1} if needs is None else needs, **kw
    )


def start(rec, task, holder="alpha", fence=None, **kw):
    kw.setdefault("node", "n1")
    kw.setdefault("sandbox", rec.get("tasks", task)["sandbox"])
    kw.setdefault("expect_s", 60)
    return rec.start_run(task, holder, fence=fence, **kw)


def finish(rec, run, outcome="done", **kw):
    kw.setdefault("footprint", {})
    kw.setdefault("cost", {})
    return rec.finish_run(run, outcome, kw.pop("reason", "finished"), **kw)


def peak(reservations, at):
    """The largest sum of `reservations` covering any instant from `at`: what
    the reservation invariant bounds. Written apart from the record's own."""
    points = {at} | {r["from_at"] for r in reservations if r["from_at"] > at}
    return max(
        sum(
            r["amount"]
            for r in reservations
            if r["from_at"] <= t and (r["until_at"] is None or t < r["until_at"])
        )
        for t in points
    )


def assert_never_past_capacity(dump):
    """Replay the journal event by event: after each one, for every node and
    resource, the live reservations never sum past its capacity at any
    instant from the event's time (invariant 6)."""
    view = MemoryView()
    for raw in dump["journal"]:
        event = Event.from_dict(raw)
        apply(view, event)
        if not any(k in ("reservations", "capacity") for k, _, _ in event.puts):
            continue
        live = [r for r in view.rows("reservations") if r["released_at"] is None]
        for cap in view.rows("capacity"):
            mine = [
                r
                for r in live
                if (r["node"], r["resource"]) == (cap["node"], cap["resource"])
            ]
            if mine:
                # capacity is bounded from the event's time on: set_capacity
                # may lower it below what past instants held
                held = peak(mine, event.at)
                assert held <= cap["amount"], (event.seq, cap, held)


def in_flight(rec, clock):
    """A run of mixed operations over every kind step 3 writes."""
    admitted(rec, "0001")
    node(rec)
    rec.declare_node("n2", ["vm"], [])
    n = item(rec, "leased", labels=["repo:widgets", "team:alpha"])
    fence = grant(rec, n, role="alpha")
    clock.step()
    rec.renew(n, "alpha", fence)
    t = agent_task(rec, n, labels=["team:beta"], flags=["needs:lab"])
    rec.edit_task(t, needs={"slots": 1, "memory": 512})
    run = start(rec, t, fence=fence, branch=f"feat/{n}", harness={"model": "m"})
    rec.run_phase(run, "testing", expect_s=600)
    rec.beat(run)
    clock.step()
    finish(rec, run, footprint={"memory": 400}, cost={"usd": 1.5}, result="ok")
    rec.move(n, "blocked", "waits on a review", fence=fence)
    rec.move(n, "claimed", "back to it", fence=fence)
    rec.release(n, "alpha", "handed on")
    j = job(rec, loop={"max_attempts": 2, "interval_s": 30})
    held = rec.reserve(
        "n1", {"memory": 1024}, holder="cache", from_at=clock(), until_at=None
    )
    other = start(rec, j, holder="beta")
    rec.release_reservation(held[0], "the cache shrank")
    finish(rec, other, "failed", reason="exit 1")
    rec.end_task(j, "failed", "gave up")
    rec.set_capacity("n2", "slots", 1, "slots")
    rec.retire_node("n2")
    rec.ack("follower", 4)
    return n, t, j


# -- the acceptance items -----------------------------------------------------


@over_backends
def test_a_second_grant_is_refused_and_fences_rise_from_1_per_item(record):
    """templet:3159 -- a second grant on an item with a live lease held by
    another is refused; fences rise from 1 per item."""
    admitted(record)
    a, b = item(record, "a"), item(record, "b")
    assert grant(record, a) == 1
    assert record.get("items", a)["state"] == "claimed"
    head, dump = record.head(), record.dump()
    with pytest.raises(Refused, match="held by alpha"):
        grant(record, a, "beta", machine="m2")
    assert record.head() == head and record.dump() == dump
    # the holder's own second grant is a resume: its fence, renewed
    assert grant(record, a) == 1
    assert len(record.read("leases", number=a)) == 1
    record.release(a, "alpha", "for another")
    assert record.get("items", a)["state"] == "ready"
    assert grant(record, a, "beta") == 2
    assert grant(record, b, "beta") == 1
    record.release(a, "beta", "again")
    assert grant(record, a, "gamma") == 3
    leases = record.read("leases", number=a)
    assert [lease["fence"] for lease in leases] == [1, 2, 3]
    assert [lease["holder"] for lease in leases] == ["alpha", "beta", "gamma"]
    assert [lease["released_at"] is None for lease in leases] == [False, False, True]
    assert record.check() == []


@over_backends
def test_a_lease_past_its_expiry_reads_expired_and_a_beat_moves_it(record, clock):
    """templet:3159 -- a lease past its expiry reads expired and still holds
    the item; a beat of the holder's live run moves its expiry and writes no
    event."""
    admitted(record)
    node(record)
    n = item(record)
    fence = grant(record, n)
    granted = clock()
    view = record.lease_view(n, now=clock())
    assert (view["state"], view["expiry"]) == ("running", granted + contract.LEASE_MS)
    assert (view["holder"], view["fence"]) == ("alpha", fence)
    run = start(record, agent_task(record, n), fence=fence)
    clock.step(contract.LEASE_MS)
    view = record.lease_view(n, now=clock())
    assert (view["state"], view["expiry"]) == ("expired", granted + contract.LEASE_MS)
    # expiry is a report, never a release: the item is still held
    assert record.get("items", n)["state"] == "claimed"
    with pytest.raises(Refused, match="held by alpha"):
        grant(record, n, "beta")
    head, journal = record.head(), record.dump()["journal"]
    record.beat(run)
    assert record.head() == head and record.dump()["journal"] == journal
    view = record.lease_view(n, now=clock())
    assert (view["state"], view["expiry"]) == ("running", clock() + contract.LEASE_MS)
    assert record.dump()["bookkeeping"]["beats"] == [{"run": run, "at": clock()}]
    # a beat at an earlier time leaves the later one
    beat_at = clock()
    clock.at -= 5000
    record.beat(run)
    clock.at = beat_at
    assert record.dump()["bookkeeping"]["beats"] == [{"run": run, "at": beat_at}]
    # only the live run's beats count: once it finishes, the lease runs from
    # its renewal again, and the holder renews it
    finish(record, run)
    clock.step(1000)
    record.beat(run)
    view = record.lease_view(n, now=clock())
    assert (view["state"], view["expiry"]) == ("expired", granted + contract.LEASE_MS)
    record.renew(n, "alpha", fence)
    assert record.lease_view(n, now=clock())["expiry"] == clock() + contract.LEASE_MS
    # a run under an older fence, still live, does not keep a new lease of
    # the same holder running: only the live lease's runs count
    stale = start(record, agent_task(record, n), fence=fence)
    record.release(n, "alpha", "for a fresh lease")
    renewed = clock()
    assert grant(record, n) == fence + 1
    clock.step(contract.LEASE_MS)
    record.beat(stale)
    view = record.lease_view(n, now=clock())
    assert (view["state"], view["expiry"]) == ("expired", renewed + contract.LEASE_MS)
    assert record.check() == []


@over_backends
def test_reserve_past_capacity_at_any_instant_is_refused_whole(record, clock):
    """templet:3159 -- reserve past capacity at any instant of its span is
    refused whole, across several resources."""
    node(record, slots=3, memory=4096)
    t = clock()
    # slots: 2 over [t, t+10s), 1 from t+5s on: [t+5s, t+10s) holds all 3
    first = record.reserve(
        "n1", {"slots": 2, "memory": 1024}, holder="a", from_at=t, until_at=t + 10_000
    )
    assert first == [1, 2]  # in resource order: memory, slots
    record.reserve("n1", {"slots": 1}, holder="b", from_at=t + 5_000, until_at=None)
    assert record.free("n1", "slots", from_at=t, until_at=t + 5_000) == 1
    assert record.free("n1", "slots", from_at=t, until_at=None) == 0
    assert record.free("n1", "slots", from_at=t + 10_000, until_at=None) == 2
    assert record.free("n1", "memory", from_at=t, until_at=None) == 3072
    assert record.free("n1", "gpu", from_at=t, until_at=None) == 0
    for until in (t, t - 1):  # an empty or reversed span, as reserve refuses
        with pytest.raises(Refused, match="ends after it starts"):
            record.free("n1", "slots", from_at=t, until_at=until)
    head, dump = record.head(), record.dump()
    for needs, span in [
        ({"memory": 1024, "slots": 1}, (t, t + 6_000)),  # slots full at t+5s
        ({"slots": 1}, (t + 9_999, t + 20_000)),  # full until t+10s
        ({"memory": 4096}, (t + 9_000, None)),  # memory: 1024 held until t+10s
        ({"slots": 1, "gpu": 1}, (t + 20_000, None)),  # no gpu at all
    ]:
        with pytest.raises(Refused, match="no room"):
            record.reserve("n1", needs, holder="c", from_at=span[0], until_at=span[1])
        assert record.head() == head and record.dump() == dump
    # what fits, to the last unit: a span ends before its until_at
    record.reserve(
        "n1", {"slots": 1, "memory": 3072}, holder="d", from_at=t, until_at=t + 5_000
    )
    record.reserve("n1", {"slots": 2}, holder="e", from_at=t + 10_000, until_at=None)
    assert record.free("n1", "slots", from_at=t, until_at=None) == 0
    assert record.free("n1", "memory", from_at=t, until_at=t + 5_000) == 0
    with pytest.raises(Refused, match="no room"):
        record.reserve("n1", {"slots": 1}, holder="f", from_at=t, until_at=None)
    record.release_reservation(first[1], "done early")
    assert record.free("n1", "slots", from_at=t, until_at=t + 5_000) == 2
    with pytest.raises(Refused, match="released"):
        record.release_reservation(first[1], "twice")
    assert_never_past_capacity(record.dump())
    assert record.check() == []


@over_backends
def test_two_processes_starting_runs_never_pass_a_nodes_capacity(
    backend, url, record, tmp_path
):
    """templet:3159 -- two processes starting runs at once on one node never
    hold reservations summing past its capacity (SQLite: separate processes;
    Henge: concurrent callers of the one writer)."""
    if backend != "sqlite":
        pytest.skip(f"{backend}: concurrent callers of the one writer, not processes")
    admitted(record)
    node(record, slots=3, memory=4096)
    tasks = {
        tag: [job(record, needs={"slots": 1, "memory": 1024}) for _ in range(20)]
        for tag in ("alpha", "beta")
    }
    record.close()
    gate = tmp_path / "go"
    procs = {
        tag: subprocess.Popen(
            [sys.executable, str(PROCS), "starter", url, tag, str(gate)]
            + [str(t) for t in ids],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for tag, ids in tasks.items()
    }
    time.sleep(0.5)
    gate.write_text("go")
    got = {}
    for tag, proc in procs.items():
        out, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err
        got[tag] = json.loads(out.strip().splitlines()[-1])
    assert all(got[tag]["started"] == 20 for tag in tasks)
    record = open_record(url, actor="alpha", via="cli")
    try:
        dump = record.dump()
        assert_never_past_capacity(dump)
        assert sum(got[tag]["refused"] for tag in tasks) > 0
        # each process was refused while it held fewer runs than the node's
        # slots: it decided against the other process's reservations
        assert all(got[tag]["contended"] >= 1 for tag in tasks), got
        starts = [e["actor"] for e in dump["journal"] if e["kind"] == "run.start"]
        assert len(starts) == 40
        # the two processes' starts interleave: they ran at once
        assert sum(a != b for a, b in zip(starts, starts[1:])) >= 4
        assert record.read("reservations", live=True) == []
        assert record.check() == []
    finally:
        record.close()


@over_backends
def test_concurrent_callers_of_one_record_never_pass_a_nodes_capacity(record):
    """templet:3159 -- callers in several threads of one record, starting runs
    at once on one node, never hold reservations summing past its
    capacity."""
    admitted(record)
    node(record, slots=2, memory=4096)
    tasks = {
        tag: [job(record, needs={"slots": 1}) for _ in range(15)]
        for tag in ("alpha", "beta", "gamma")
    }
    errors, refused = [], []

    def starter(tag):
        try:
            held = []
            for t in tasks[tag]:
                while True:
                    try:
                        held.append(start(record, t, tag))
                        break
                    except Refused as e:
                        if "no room" not in str(e):
                            raise
                        refused.append(tag)
                        if held:
                            finish(record, held.pop(0))
                        else:
                            time.sleep(0.001)
            for run in held:
                finish(record, run)
        except Exception as e:  # reported below
            errors.append(e)

    threads = [threading.Thread(target=starter, args=(tag,)) for tag in tasks]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert errors == []
    dump = record.dump()
    assert_never_past_capacity(dump)
    assert refused
    assert len([e for e in dump["journal"] if e["kind"] == "run.start"]) == 45
    assert record.check() == []


@over_backends
def test_start_run_with_a_stale_fence_or_past_max_attempts_reserves_nothing(
    record, clock
):
    """templet:3159 -- start_run with a fence that is not the live lease's, or
    past loop.max_attempts, is refused and reserves nothing."""
    admitted(record)
    node(record)
    n = item(record)
    fence = grant(record, n)
    t = agent_task(record, n, loop={"max_attempts": 2, "interval_s": 0})
    head, dump = record.head(), record.dump()
    for bad in (fence + 1, 0, None):
        with pytest.raises(Refused, match="fence"):
            start(record, t, fence=bad)
    with pytest.raises(Refused, match="alpha"):
        start(record, t, "beta", fence=fence)
    assert record.head() == head and record.dump() == dump
    assert record.read("reservations") == []
    first = start(record, t, fence=fence)
    assert record.get("tasks", t)["attempts"] == 1
    assert record.get("runs", first)["attempt"] == 1
    with pytest.raises(Refused, match="running"):
        start(record, t, fence=fence)
    finish(record, first, "failed")
    second = start(record, t, fence=fence)
    assert record.get("runs", second)["attempt"] == 2
    finish(record, second, "failed")
    head, dump = record.head(), record.dump()
    with pytest.raises(Refused, match="attempts"):
        start(record, t, fence=fence)
    assert record.head() == head and record.dump() == dump
    assert record.read("reservations", live=True) == []
    # a task with no item starts with no fence, and refuses one
    j = job(record)
    with pytest.raises(Refused, match="fence"):
        start(record, j, fence=1)
    record.end_task(j, "cancelled", "not now")
    with pytest.raises(Refused, match="cancelled"):
        start(record, j)
    assert record.check() == []


@over_backends
def test_finish_run_releases_the_runs_reservations_in_its_own_event(record, clock):
    """templet:3159 -- finish_run releases the run's reservations in its own
    event."""
    admitted(record)
    node(record, slots=3, memory=4096)
    j = job(record, needs={"slots": 2, "memory": 1024})
    head = record.head()
    run = start(record, j, "beta")
    (started,) = record.events(head)
    assert (started.kind, started.subject) == ("run.start", f"run:{run}")
    made = [v for k, _, v in started.puts if k == "reservations"]
    assert sorted((r["resource"], r["amount"]) for r in made) == [
        ("memory", 1024),
        ("slots", 2),
    ]
    assert all(
        (r["run"], r["task"], r["holder"], r["from_at"], r["until_at"])
        == (run, j, "beta", clock(), None)
        for r in made
    )
    assert record.get("tasks", j)["state"] == "running"
    assert record.free("n1", "slots", from_at=clock(), until_at=None) == 1
    clock.step()
    head = record.head()
    finish(
        record,
        run,
        "done",
        reason="exit 0",
        footprint={"memory": 900},
        cost={"usd": 0.5},
        result="all good",
    )
    (finished,) = record.events(head)
    assert (finished.kind, finished.subject) == ("run.finish", f"run:{run}")
    released = [v for k, _, v in finished.puts if k == "reservations"]
    assert sorted(r["id"] for r in released) == sorted(r["id"] for r in made)
    assert all(r["released_at"] == clock() for r in released)
    assert record.read("reservations", live=True) == []
    assert record.free("n1", "slots", from_at=clock(), until_at=None) == 3
    done = record.get("runs", run)
    assert (done["outcome"], done["reason"], done["finished_at"]) == (
        "done",
        "exit 0",
        clock(),
    )
    assert (done["footprint"], done["cost"]) == ({"memory": 900}, {"usd": 0.5})
    assert record.get("run_text", run)["result"] == "all good"
    assert record.get("tasks", j)["state"] == "open"
    with pytest.raises(Refused, match="finished"):
        finish(record, run)
    assert record.check() == []


@over_backends
def test_release_reservation_refuses_a_live_runs_reservation(record, clock):
    """templet:3159 -- release_reservation refuses a reservation whose run
    is live, since finish_run releases it: the run keeps its room, and
    another start finds none. A standing holder's reservation, and a task's
    own, release by id."""
    admitted(record)
    node(record, slots=1)
    run = start(record, job(record))
    other = job(record)
    (r,) = record.read("reservations", live=True, run=run)
    assert (r["resource"], r["amount"]) == ("slots", 1)
    head, dump = record.head(), record.dump()
    with pytest.raises(Refused, match="finish_run"):
        record.release_reservation(r["id"], "make room")
    with pytest.raises(Refused, match="no room"):
        start(record, other, "beta")
    assert record.head() == head and record.dump() == dump
    # what no live run holds releases by id
    (standing,) = record.reserve(
        "n1", {"memory": 1024}, holder="cache", from_at=clock(), until_at=None
    )
    t = job(record)
    (own,) = record.reserve(
        "n1", {"memory": 512}, task=t, holder="alpha", from_at=clock(), until_at=None
    )
    record.release_reservation(standing, "the cache shrank")
    record.release_reservation(own, "not needed")
    finish(record, run)
    assert record.read("reservations", live=True) == []
    assert_never_past_capacity(record.dump())
    assert record.check() == []


@over_backends
def test_a_reservation_is_held_only_while_something_holds_it(record, clock):
    """templet:3159 -- reserve refuses a finished run and an ended task, and
    end_task releases the task's own live reservations in its own event."""
    admitted(record)
    node(record)
    t = job(record)
    run = start(record, t)
    # a live run, and an open task, may hold more
    record.reserve(
        "n1", {"memory": 256}, run=run, holder="alpha", from_at=clock(), until_at=None
    )
    finish(record, run)
    assert record.read("reservations", live=True) == []
    head, dump = record.head(), record.dump()
    with pytest.raises(Refused, match="finished"):
        record.reserve(
            "n1", {"slots": 1}, run=run, holder="alpha", from_at=clock(), until_at=None
        )
    assert record.head() == head and record.dump() == dump
    own = record.reserve(
        "n1",
        {"slots": 1, "memory": 512},
        task=t,
        holder="alpha",
        from_at=clock(),
        until_at=None,
    )
    (standing,) = record.reserve(
        "n1", {"memory": 1024}, holder="cache", from_at=clock(), until_at=None
    )
    clock.step()
    head = record.head()
    record.end_task(t, "done", "finished")
    (ended,) = record.events(head)
    assert ended.kind == "task.end"
    released = [v for k, _, v in ended.puts if k == "reservations"]
    assert sorted(r["id"] for r in released) == sorted(own)
    assert all(
        (r["released_at"], r["why"]) == (clock(), "task ended") for r in released
    )
    assert [r["id"] for r in record.read("reservations", live=True)] == [standing]
    head, dump = record.head(), record.dump()
    with pytest.raises(Refused, match="ended"):
        record.reserve(
            "n1", {"slots": 1}, task=t, holder="alpha", from_at=clock(), until_at=None
        )
    assert record.head() == head and record.dump() == dump
    assert_never_past_capacity(record.dump())
    assert record.check() == []


@over_backends
def test_a_run_whose_lease_was_granted_again_frees_the_host_but_not_the_item(
    record, clock
):
    """templet:3159 -- for a run whose lease was released and granted again,
    its move and renew are refused, and its finish_run records the outcome
    and releases its reservations."""
    admitted(record)
    node(record)
    n = item(record)
    old = grant(record, n)
    run = start(record, agent_task(record, n), fence=old)
    record.release(n, "gamma", "stalled")
    new = grant(record, n, "beta")
    assert new == old + 1
    head, dump = record.head(), record.dump()
    with pytest.raises(Refused, match="fence"):
        record.move(n, "blocked", "the old run", fence=old)
    with pytest.raises(Refused, match="fence"):
        record.renew(n, "alpha", old)
    with pytest.raises(Refused, match="fence"):
        record.release(n, "alpha", "the old run", fence=old)
    assert record.head() == head and record.dump() == dump
    clock.step()
    finish(record, run, "failed", reason="its lease was lost")
    assert record.get("runs", run)["outcome"] == "failed"
    assert record.read("reservations", live=True) == []
    lease = record.lease_view(n, now=clock())
    assert (lease["holder"], lease["fence"], lease["state"]) == ("beta", new, "running")
    assert record.get("items", n)["state"] == "claimed"
    record.move(n, "blocked", "the new holder", fence=new)
    assert record.get("items", n)["state"] == "blocked"
    assert record.check() == []


@over_backends
def test_set_capacity_below_the_reserved_amount_is_refused(record, clock):
    """templet:3159 -- set_capacity below the reserved amount is refused."""
    node(record, slots=3)
    t = clock()
    record.reserve("n1", {"slots": 1}, holder="a", from_at=t, until_at=t + 10_000)
    record.reserve("n1", {"slots": 1}, holder="b", from_at=t + 5_000, until_at=None)
    head = record.head()
    for below in (1, 0):
        with pytest.raises(Refused, match="below"):
            record.set_capacity("n1", "slots", below, "slots")
    assert record.head() == head and record.get("capacity", ["n1", "slots"])[
        "amount"
    ] == (3)
    assert record.set_capacity("n1", "slots", 2, "slots") is True
    assert record.set_capacity("n1", "slots", 2, "slots") is False
    # from t+10s on only one slot is reserved
    clock.step(10_000)
    assert record.set_capacity("n1", "slots", 1, "slots") is True
    with pytest.raises(Refused, match="below"):
        record.set_capacity("n1", "slots", 0, "slots")
    with pytest.raises(Refused, match="node"):
        record.set_capacity("nowhere", "slots", 1, "slots")
    with pytest.raises(Refused):
        record.set_capacity("n1", "slots", -1, "slots")
    assert_never_past_capacity(record.dump())
    assert record.check() == []


# -- the rest of step 3 -------------------------------------------------------


@over_backends
def test_the_replay_holds_over_work_in_flight_and_a_dump_loads(record, clock, tmp_path):
    """templet:3159 -- after a run of step 3's operations, the replay of the
    journal equals the records, and a dump loads into every backend and dumps
    the same, beats and cursors included."""
    in_flight(record, clock)
    dump = record.dump()
    view = MemoryView()
    for raw in dump["journal"]:
        apply(view, Event.from_dict(raw))
    assert view.records() == dump["records"]
    for kind in ("leases", "tasks", "nodes", "capacity", "reservations", "runs"):
        assert dump["records"][kind], kind
    assert dump["records"]["run_text"] and dump["bookkeeping"]["beats"]
    assert record.check() == []
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
        finally:
            target.close()


@over_backends
def test_ready_gives_the_eligible_open_tasks(record, clock):
    """templet:3159 -- the ready read gives the open tasks whose item is
    eligible (ready, or claimed) or, with no item, whose effort is admitted,
    whose not_before has passed and whose attempts are under max_attempts;
    and leaves out every other."""
    admitted(record, "0001")
    record.file_effort("0002", "p", "Parked", "owner-a", PARTS, standing="parked")
    node(record, slots=10)
    ready_item = item(record, "ready")
    claimed = item(record, "claimed")
    grant(record, claimed)
    blocked = item(record, "with a dep")
    record.add_dep(blocked, what="a decision", why="w")
    parked = item(record, "parked", effort="0002")
    # claimed, but its one effort is parked: its task is not ready either
    claimed_parked = item(record, "claimed, parked", effort="0002")
    parked_fence = grant(record, claimed_parked)
    held = item(record, "held")
    record.hold(number=held, cause="quarantine", why="w")
    in_triage = record.file_item("task", "triage", "b", filed_by="a", effort="0001")
    t_ready = agent_task(record, ready_item)
    t_claimed = agent_task(record, claimed)
    t_claimed_parked = agent_task(record, claimed_parked)
    for n in (blocked, parked, held, in_triage):
        agent_task(record, n)
    j = job(record)
    job(record, effort="0002")
    job(record, effort=None)  # no item and no effort: never ready
    later = job(record, not_before=clock() + 1000)
    spent = job(record)
    finish(record, start(record, spent))
    running = job(record)
    start(record, running)
    ended = job(record)
    record.end_task(ended, "cancelled", "no")
    got = record.ready(now=clock())
    assert [i["number"] for i in got["items"]] == [ready_item]
    assert [t["id"] for t in got["tasks"]] == [t_ready, t_claimed, j]
    got = record.ready(now=clock() + 1000)
    assert [t["id"] for t in got["tasks"]] == [t_ready, t_claimed, j, later]
    head, dump = record.head(), record.dump()
    with pytest.raises(Refused, match="admitted"):
        start(record, t_claimed_parked, fence=parked_fence)
    assert record.head() == head and record.dump() == dump
    assert record.read("reservations", task=t_claimed_parked) == []
    # a held holder holds its item's tasks
    record.hold(who="alpha", cause="budget", why="over")
    assert [t["id"] for t in record.ready(now=clock())["tasks"]] == [j]


@over_backends
def test_a_lease_pauses_out_of_claimed_and_ends_with_its_item(record, clock):
    """templet:3159 -- a move out of claimed pauses the lease and a move back
    restarts it; a usage limit's paused_until holds its expiry back; release
    moves a claimed or blocked item to ready; a close releases the lease."""
    admitted(record)
    n = item(record)
    fence = grant(record, n)
    record.move(n, "blocked", "waits", fence=fence)
    view = record.lease_view(n, now=clock())
    assert (view["state"], view["expiry"], view["expires_at"]) == (
        "paused",
        None,
        None,
    )
    clock.step(10 * contract.LEASE_MS)
    assert record.lease_view(n, now=clock())["state"] == "paused"
    record.move(n, "claimed", "back", fence=fence)
    view = record.lease_view(n, now=clock())
    assert (view["state"], view["expiry"]) == ("running", clock() + contract.LEASE_MS)
    record.renew(n, "alpha", fence, paused_until=clock() + 5 * contract.LEASE_MS)
    view = record.lease_view(n, now=clock() + 5 * contract.LEASE_MS)
    assert (view["state"], view["expiry"]) == (
        "running",
        clock() + 6 * contract.LEASE_MS,
    )
    with pytest.raises(Refused, match="alpha"):
        record.renew(n, "beta", fence)
    record.move(n, "blocked", "waits again")
    record.release(n, "alpha", "handed back")
    assert record.get("items", n)["state"] == "ready"
    assert record.lease_view(n, now=clock()) is None
    with pytest.raises(Refused, match="no live lease"):
        record.release(n, "alpha", "twice")
    # a close releases the live lease
    fence = grant(record, n, "beta")
    record.move(n, "done", "finished", fence=fence)
    (lease,) = [x for x in record.read("leases", number=n) if x["fence"] == fence]
    assert (lease["released_at"], lease["released_by"], lease["why"]) == (
        clock(),
        "alpha",
        "closed",
    )
    assert record.read("leases", live=True) == []


@over_backends
def test_grant_refuses_a_hold_a_stale_expect_and_a_state_with_no_move(record):
    """templet:3159 -- grant refuses a live hold (on the item, the holder or
    the role), an `expect` that is not the state, and a state with no move to
    claimed; each writes nothing."""
    admitted(record)
    n = item(record, role="team-a")
    head = record.head()
    h = record.hold(number=n, cause="quarantine", why="w")
    with pytest.raises(Refused, match="hold"):
        grant(record, n)
    record.lift_hold(h, "alpha")
    for who in ("alpha", "team-a"):
        h = record.hold(who=who, cause="budget", why="w")
        with pytest.raises(Refused, match="hold"):
            grant(record, n)
        record.lift_hold(h, "alpha")
    with pytest.raises(Refused, match="ready"):
        grant(record, n, expect="blocked")
    triage = record.file_item("task", "t", "b", filed_by="a", effort="0001")
    with pytest.raises(Refused, match="triage"):
        grant(record, triage)
    assert record.read("leases") == []
    assert record.head() == head + 7  # the holds, their lifts and the item
    assert grant(record, n, expect="ready") == 1


@over_backends
def test_tasks_are_filed_edited_and_ended(record):
    """templet:3159 -- file_task checks its item, effort, needs and loop;
    edit_task writes nothing for no change and refuses an ended task;
    end_task refuses a task with a live run, and one already ended."""
    admitted(record)
    node(record)
    n = item(record)
    t = agent_task(record, n, labels=["team:a"], flags=["needs:lab"])
    task = record.get("tasks", t)
    assert (task["state"], task["attempts"], task["ended_at"]) == ("open", 0, None)
    assert task["loop"] == {"max_attempts": 1, "interval_s": 0}
    assert task["supervision"] == {"expect_s": None, "beat_s": None}
    for bad in (
        dict(number=999),
        dict(effort="0009"),
        dict(needs={"slots": 0}),
        dict(needs={"slots": 1.5}),
        dict(needs=["slots"]),
        dict(loop={"max_attempts": 0}),
        dict(loop={"tries": 3}),
        dict(supervision={"expect_s": -1}),
        dict(not_before=-1),
        dict(labels=["state:open"]),
    ):
        kw = {"sandbox": "process", **bad}
        with pytest.raises(Refused):
            record.file_task("job", **kw)
    with pytest.raises(Refused):
        record.file_task("chore", sandbox="process")
    assert record.edit_task(t, needs={"slots": 1}) is False
    assert record.edit_task(t, needs={"slots": 2}, labels=["z:1", "a:2"]) is True
    assert record.get("tasks", t)["labels"] == ["a:2", "z:1"]
    with pytest.raises(Refused):
        record.edit_task(t, attempts=5)
    fence = grant(record, n)
    run = start(record, t, fence=fence)
    with pytest.raises(Refused, match="live run"):
        record.end_task(t, "done", "early")
    # the run holds reservations for the task's needs (invariant 7)
    with pytest.raises(Refused, match="live run"):
        record.edit_task(t, needs={"slots": 1})
    assert record.edit_task(t, labels=["a:3"]) is True
    finish(record, run)
    with pytest.raises(Refused):
        record.end_task(t, "open", "not an end")
    record.end_task(t, "done", "finished")
    ended = record.get("tasks", t)
    assert ended["state"] == "done" and ended["ended_at"] is not None
    with pytest.raises(Refused, match="ended"):
        record.end_task(t, "failed", "twice")
    with pytest.raises(Refused, match="ended"):
        record.edit_task(t, needs={"slots": 1})
    assert record.check() == []


@over_backends
def test_nodes_are_declared_and_retired(record, clock):
    """templet:3159 -- declare_node sorts the drivers and refuses a repeat,
    updates a live node (False when nothing changes) and refuses a retired
    one; retire_node refuses a node with live reservations; start_run
    refuses a retired node and a sandbox the node does not run."""
    admitted(record)
    assert record.declare_node("n1", ["vm", "process"], ["zone:a"]) is True
    assert record.get("nodes", "n1")["drivers"] == ["process", "vm"]
    assert record.declare_node("n1", ["process", "vm"], ["zone:a"]) is False
    assert record.declare_node("n1", ["process"], ["zone:b"]) is True
    with pytest.raises(Refused, match="drivers"):
        record.declare_node("n2", ["podman", "podman"], [])
    record.set_capacity("n1", "slots", 2, "slots")
    j = job(record)
    with pytest.raises(Refused, match="harness"):
        start(record, j, sandbox="harness")
    run = start(record, j)
    with pytest.raises(Refused, match="live reservations"):
        record.retire_node("n1")
    finish(record, run)
    record.retire_node("n1")
    with pytest.raises(Refused, match="retired"):
        record.retire_node("n1")
    with pytest.raises(Refused, match="retired"):
        record.declare_node("n1", ["process"], [])
    with pytest.raises(Refused, match="retired"):
        start(record, job(record))
    with pytest.raises(Refused, match="retired"):
        record.reserve("n1", {"slots": 1}, holder="a", from_at=clock(), until_at=None)
    assert record.check() == []


@over_backends
def test_run_phase_moves_a_live_runs_phase_only(record, clock):
    """templet:3159 -- run_phase sets a live run's phase and expectation, and
    refuses a finished run."""
    admitted(record)
    node(record)
    run = start(record, job(record), expect_s=None)
    assert record.get("runs", run)["expect_until"] is None
    clock.step()
    record.run_phase(run, "testing", expect_s=90)
    got = record.get("runs", run)
    assert (got["phase"], got["phase_at"], got["expect_until"]) == (
        "testing",
        clock(),
        clock() + 90_000,
    )
    finish(record, run)
    with pytest.raises(Refused, match="finished"):
        record.run_phase(run, "late")


@over_backends
def test_effective_labels_are_merged_by_key_whatever_the_stored_order(record):
    """templet:3159 -- a task's effective labels are its item's, then its own,
    by key, the task's winning; `-` and `.` sort before `:`, so the stored
    order is not the key order."""
    admitted(record)
    n = item(record, labels=["a-b:x", "a:item", "a.c:y"])
    assert record.get("items", n)["labels"] == ["a-b:x", "a.c:y", "a:item"]
    t = agent_task(record, n, labels=["a:task", "z:1"])
    assert record.effective_labels(t) == ["a-b:x", "a.c:y", "a:task", "z:1"]
    t2 = agent_task(record, n, labels=["a-b:task"])
    assert record.effective_labels(t2) == ["a-b:task", "a.c:y", "a:item"]
    assert record.effective_labels(job(record, labels=["k:v"])) == ["k:v"]
    with pytest.raises(Refused):
        record.effective_labels(999)


@over_backends
def test_events_wait_inside_reading_is_refused(record):
    """events(wait=) inside reading() is refused: the reading holds every
    other caller back, so the wait could only end empty; events() with no
    wait reads as before."""
    admitted(record)
    with record.reading() as seq:
        with pytest.raises(Refused, match="reading"):
            record.events(0, wait=0.5)
        assert [e.seq for e in record.events(0)] == list(range(1, seq + 1))
    assert record.events(record.head(), wait=0.01) == []


def test_lease_ms_is_three_days():
    """templet:3159 -- a lease runs three days past its renewal, and a
    node's drivers are a list with no repeat."""
    assert contract.LEASE_MS == 3 * 24 * 3600 * 1000
    with pytest.raises(schema.SchemaError, match="nodes.drivers"):
        schema.validate(
            "nodes",
            {"name": "n", "drivers": ["vm", "vm"], "labels": [], "declared_at": 1},
        )
