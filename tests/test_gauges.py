# SPDX-License-Identifier: Apache-2.0
"""The gauges, over every backend in BACKENDS (templet:3159, build step 4):
each computed from a fixture record through the contract alone, and `stalls`
at its margins."""

import pytest
from backends import admitted, over_backends
from samples import load_sample

from quarrylith import gauges
from quarrylith.gauges import GAUGES, Gauge
from quarrylith.record.importing import compose

MIN = 60_000
DAY = 24 * 60 * MIN

#: The page's table of gauges, in its order.
PAGE = (
    "capacity",
    "reserved",
    "free",
    "queue_depth",
    "runs_active",
    "run_age",
    "filed_to_start",
    "start_to_done",
    "outcomes",
    "unmerged_changes",
    "review_rounds",
    "budget_pace",
    "footprint",
    "stalls",
)

#: What a gauge may call on the record: the contract's reads, nothing else.
READS = (
    "get",
    "read",
    "reading",
    "ready",
    "free",
    "lease_view",
    "effective_labels",
    "head",
)


class ContractOnly:
    """The record, with only the contract's reads: any other attribute, a
    private one or an operation, fails the test."""

    def __init__(self, record):
        self._record = record

    def __getattr__(self, name):
        if name not in READS:
            raise AssertionError(f"a gauge read {name!r}, outside the contract")
        return getattr(self._record, name)


def at(t0, minutes):
    return t0 + int(minutes * MIN)


def fixture(record, clock):
    """A record with work in flight and landed: two nodes, three items, five
    tasks and their runs, changes, reviews and budget readings, at minute 60
    of the clock."""
    t0 = clock()
    admitted(record, "0001")
    record.file_effort("0002", "e2", "Parked", "lead", {}, standing="parked")
    record.declare_node("n1", ["harness", "process"], [])
    record.set_capacity("n1", "slots", 4, "slots")
    record.set_capacity("n1", "memory", 4096, "bytes")
    record.declare_node("n2", ["vm"], [])
    record.set_capacity("n2", "slots", 2, "slots")
    record.reserve("n1", {"memory": 1024}, holder="cache", from_at=t0, until_at=None)

    def item(title, role, effort, labels):
        return record.file_item(
            "task",
            title,
            "",
            role=role,
            effort=effort,
            labels=labels,
            filed_by="lead",
        )

    i1 = item("one", "alpha", "0001", ["repo:widgets"])
    i2 = item("two", "beta", "0001", ["repo:gadgets"])
    item("three", None, "0002", [])
    t_a = record.file_task(
        "agent", number=i1, needs={"memory": 512, "slots": 1}, sandbox="harness"
    )
    t_b = record.file_task("agent", number=i2, needs={"slots": 1}, sandbox="harness")

    def job():
        return record.file_task(
            "job", effort="0001", needs={"slots": 1}, sandbox="process"
        )

    j1, j2, j3 = job(), job(), job()

    def start(task, holder, fence, expect_s):
        return record.start_run(
            task,
            holder,
            node="n1",
            sandbox=record.get("tasks", task)["sandbox"],
            fence=fence,
            expect_s=expect_s,
        )

    def finish(run, outcome, footprint):
        record.finish_run(run, outcome, "", footprint=footprint, cost={})

    clock.at = at(t0, 10)
    r1 = start(t_a, "alpha", record.grant(i1, "alpha", branch="b1", machine="m"), 60)
    clock.at = at(t0, 20)
    r2 = start(t_b, "beta", record.grant(i2, "beta", branch="b2", machine="m"), None)
    clock.at = at(t0, 30)
    r3 = start(j1, "gamma", None, 600)
    clock.at = at(t0, 35)
    finish(r3, "done", {"memory": 300})
    clock.at = at(t0, 40)
    r4 = start(j2, "gamma", None, 60)
    clock.at = at(t0, 42)
    r5 = start(j3, "gamma", None, None)
    clock.at = at(t0, 45)
    finish(r5, "failed", {"memory": 700, "note": "not a number"})

    c1 = record.open_or_update_change(
        "widgets", "feat/1", "h1", title="one", number=i1, by="alpha"
    )
    c2 = record.open_or_update_change(
        "widgets", "feat/2", "h2", title="two", number=i2, by="beta"
    )
    record.record_merge(c2, "h2", "user", "reviewed")
    c3 = record.open_or_update_change("gadgets", "feat/3", "h3", title="x", by="beta")
    record.open_or_update_change("gadgets", "feat/4", "h4", title="y", by="beta")
    record.drop_change(c3, "beta", "not needed")
    for target, round_, heads in ((i1, 1, {str(c1): "h1"}), (i1, 2, {}), (i2, 1, {})):
        record.file_review(target, round_, "pool", author="alpha", heads=heads)

    def reading(minutes, used, resets):
        clock.at = at(t0, minutes)
        value = {"provider": "acme", "resets_at": resets, "utilization": used}
        record.record_reading("budget", "five_hour", value)

    reading(20, 0.9, at(t0, 25))
    reading(30, 0.2, at(t0, 300))
    reading(50, 0.3, at(t0, 300))
    record.record_reading("other", "five_hour", {"utilization": 0.99})

    clock.at = at(t0, 50)
    record.beat(r1)
    record.beat(r2)
    clock.at = at(t0, 55)
    record.run_phase(r2, "testing")
    clock.at = at(t0, 60)
    return t0, (r1, r2, r4)


def values(gauge, record, now, window_ms=DAY):
    got = gauge.compute(ContractOnly(record), now=now, window_ms=window_ms)
    return {tuple(sorted(labels.items())): value for labels, value in got}


def series(**labels):
    return tuple(sorted(labels.items()))


def test_gauges_are_the_pages_list():
    """templet:3159 -- `GAUGES` holds the page's gauges, in its order, each a
    `Gauge` naming its unit, its labels and its help."""
    assert tuple(g.name for g in GAUGES) == PAGE
    for g in GAUGES:
        assert isinstance(g, Gauge)
        assert g.unit and g.help and callable(g.compute)
        assert isinstance(g.labels, tuple) and g.labels
    assert gauges.by_name("stalls").labels == ("node",)


@over_backends
def test_every_gauge_computes_from_a_fixture_through_the_contract(record, clock):
    """templet:3159 -- every gauge in `GAUGES` computes from a fixture record,
    through the contract only, to the expected values."""
    t0, (r1, r2, r4) = fixture(record, clock)
    now = clock()
    want = {
        "capacity": {
            series(node="n1", resource="memory"): 4096,
            series(node="n1", resource="slots"): 4,
            series(node="n2", resource="slots"): 2,
        },
        "reserved": {
            series(node="n1", resource="memory"): 1536,
            series(node="n1", resource="slots"): 3,
            series(node="n2", resource="slots"): 0,
        },
        "free": {
            series(node="n1", resource="memory"): 2560,
            series(node="n1", resource="slots"): 1,
            series(node="n2", resource="slots"): 2,
        },
        "queue_depth": {
            series(of="items", state="claimed", effort="0001", label="*"): 2,
            series(of="items", state="claimed", effort="0001", label="repo:widgets"): 1,
            series(of="items", state="claimed", effort="0001", label="repo:gadgets"): 1,
            series(of="items", state="triage", effort="0002", label="*"): 1,
            series(of="tasks", state="running", effort="0001", label="*"): 3,
            series(of="tasks", state="running", effort="0001", label="repo:widgets"): 1,
            series(of="tasks", state="running", effort="0001", label="repo:gadgets"): 1,
            series(of="tasks", state="open", effort="0001", label="*"): 2,
        },
        "runs_active": {
            series(phase="started", node="n1"): 2,
            series(phase="testing", node="n1"): 1,
        },
        "run_age": {
            series(run=str(r1)): 3000,
            series(run=str(r2)): 2400,
            series(run=str(r4)): 1200,
        },
        "filed_to_start": {
            series(kind="agent", effort="0001", stat="p50"): 600,
            series(kind="agent", effort="0001", stat="p90"): 1200,
            series(kind="job", effort="0001", stat="p50"): 2400,
            series(kind="job", effort="0001", stat="p90"): 2520,
        },
        "start_to_done": {
            series(kind="job", effort="0001", stat="p50"): 180,
            series(kind="job", effort="0001", stat="p90"): 300,
        },
        "outcomes": {
            series(kind="job", outcome="done"): 1,
            series(kind="job", outcome="failed"): 1,
        },
        "unmerged_changes": {
            series(repo="gadgets"): 1,
            series(repo="widgets"): 1,
        },
        "review_rounds": {
            series(effort="0001", stat="median"): 1,
            series(effort="0001", stat="worst"): 2,
        },
        "budget_pace": {
            series(provider="acme", window="five_hour"): 30,
        },
        "footprint": {
            series(kind="job", resource="memory"): 700,
        },
        "stalls": {
            series(node="n1"): 1,
        },
    }
    assert set(want) == set(PAGE)
    for g in GAUGES:
        got = values(g, record, now)
        assert got == pytest.approx(want[g.name]), g.name
    # a window that ends before the runs finished sees none of them
    early = {g.name: values(g, record, at(t0, 33), 3 * MIN) for g in GAUGES}
    assert early["outcomes"] == {} and early["start_to_done"] == {}
    assert early["footprint"] == {}
    assert early["filed_to_start"] == {
        series(kind="job", effort="0001", stat="p50"): 1800,
        series(kind="job", effort="0001", stat="p90"): 1800,
    }
    # the whole set, in one reading, as the command gives it
    every = gauges.measure(ContractOnly(record), now=now, window_ms=DAY)
    assert list(every) == list(PAGE)
    for name, rows in every.items():
        got = {tuple(sorted(lab.items())): v for lab, v in rows}
        assert got == pytest.approx(want[name]), name


@over_backends
def test_stalls_counts_a_run_past_its_margin_and_not_one_inside_it(record, clock):
    """templet:3159 -- `stalls` counts a live run that is alive (it beats)
    with no phase change past `expect_until` + 5 min, or 10 min with no
    expectation, and not one inside its margin; nor one that stopped
    beating."""
    t0 = clock()
    admitted(record)
    record.declare_node("n1", ["process"], [])
    record.declare_node("n2", ["process"], [])
    for name in ("n1", "n2"):
        record.set_capacity(name, "slots", 9, "slots")

    def run(node, expect_s, beat_s=None):
        task = record.file_task(
            "job",
            effort="0001",
            needs={"slots": 1},
            sandbox="process",
            supervision={"beat_s": beat_s},
        )
        return record.start_run(
            task, "alpha", node=node, sandbox="process", fence=None, expect_s=expect_s
        )

    expecting = run("n1", 60)  # stalls past t0 + 1 min + 5 min
    quiet = run("n1", None)  # stalls past t0 + 10 min
    other = run("n2", None)
    run("n2", None)  # never beats: not alive, never a stall
    beating = run("n2", None, beat_s=30)  # alive only while it beats each 30 s

    def beat_all(minute):
        clock.at = at(t0, minute)
        for r in (expecting, quiet, other, beating):
            record.beat(r)

    def stalls(now):
        return values(gauges.by_name("stalls"), record, now)

    beat_all(5)
    assert stalls(at(t0, 6)) == {series(node="n1"): 0, series(node="n2"): 0}
    assert stalls(at(t0, 6) + 1) == {series(node="n1"): 1, series(node="n2"): 0}
    beat_all(9.5)
    assert stalls(at(t0, 10)) == {series(node="n1"): 1, series(node="n2"): 0}
    assert stalls(at(t0, 10) + 1) == {series(node="n1"): 2, series(node="n2"): 2}
    # a phase change restarts the margin
    clock.at = at(t0, 11)
    record.run_phase(other, "testing", expect_s=120)
    beat_all(11)
    assert stalls(at(t0, 11) + 1) == {series(node="n1"): 2, series(node="n2"): 1}
    # past two missed beats it is not alive, so not a stall
    assert stalls(at(t0, 12)) == {series(node="n1"): 2, series(node="n2"): 1}
    assert stalls(at(t0, 12) + 1) == {series(node="n1"): 2, series(node="n2"): 0}
    # a finished run is no stall
    record.finish_run(quiet, "done", "", footprint={}, cost={})
    assert stalls(at(t0, 12) + 1)[series(node="n1")] == 1


@over_backends
def test_the_gauges_compute_over_the_sample_import(record):
    """Every gauge computes over the sample import, whose runs sit on the
    retired node `legacy`."""
    doc = load_sample()
    record.load(compose(doc))
    now = doc["source"]["read_at"]
    every = gauges.measure(record, now=now, window_ms=30 * DAY)
    assert list(every) == list(PAGE)
    assert every["capacity"] == [] and every["stalls"] == []
    assert dict(every["unmerged_changes"]) == {} or every["unmerged_changes"] == [
        ({"repo": "widgets"}, 1)
    ]
    assert every["outcomes"]
    assert every["budget_pace"] == [
        ({"provider": "", "window": "five_hour"}, pytest.approx((0.82 - 0.2) * 100 / 4))
    ]
