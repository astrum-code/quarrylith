# SPDX-License-Identifier: Apache-2.0
"""The laptop plugin's declaration and table (templet:3161, build step 2):
`declare` brings the record to the allocation document from given figures,
and `place`, `hold`, `release` and `headroom` answer from the table. Nothing
here probes the host. Every test that touches a record runs over every backend
in BACKENDS."""

import hashlib
import json
import math
from fractions import Fraction
from pathlib import Path

import pytest
from backends import FIXTURES, Clock, admitted, over_backends, url_for

from quarrylith.allocator import Fit, Refusal, load_document, unit_of
from quarrylith.allocator.host import HostPlacement
from quarrylith.record import contract
from quarrylith.record.contract import Refused

SAMPLE = Path(__file__).resolve().parent / "data" / "allocation-sample.toml"
GIB = 2**30
#: What a probe would have read on a 48 GiB host with a 200 GiB work allowance.
FIGURES = {
    "capacity": {"memory": 48 * GIB, "disk": 200 * GIB},
    "consumers": {"yard": {"memory": 3 * GIB // 2}},
}
#: The sample's standing consumers, as the table holds them.
PODMAN = math.ceil(Fraction("15.4") * GIB)  # 15.4 GiB, rounded up
STANDING = {
    ("desktop", "memory"): 10 * GIB,
    ("podman-machine", "memory"): PODMAN,
    ("podman-machine", "disk"): 40 * GIB,
    ("yard", "memory"): 3 * GIB // 2,
}


def sample_text(max_age=1_000_000):
    """The sample, its host reading kept fresh for `max_age` seconds: the
    tests step the clock by minutes and hours between a reading and a
    placement."""
    return SAMPLE.read_text().replace("max_age_s = 180", f"max_age_s = {max_age}")


def host_reading(
    record,
    clock,
    node="laptop",
    available=47 * GIB,
    total=48 * GIB,
    disk_free=190 * GIB,
    **more,
):
    """A host measurement, as `measure` writes it, taken now: generous by
    default, so a placement test is not about the measured room."""
    value = {
        "node": node,
        "memory_total": total,
        "memory_available": available,
        "method": "given",
        "disk_free": disk_free,
        "consumers": {},
        "drift": [],
    }
    return record.record_reading("host", "measure", {**value, **more})


def sample(old=None, new=None):
    text = sample_text()
    if old is not None:
        assert old in text
        text = text.replace(old, new)
    return load_document(text)


def declare(record, clock, document=None, figures=FIGURES):
    """Declare `document` (the sample) and return the Declaration."""
    document = document or sample()
    return HostPlacement(record).declare(document, figures=figures, now=clock())


def declared(record, clock, document=None, figures=FIGURES):
    """Declare `document` (the sample), and give the host a measurement."""
    result = declare(record, clock, document, figures)
    assert result.ok, result
    host_reading(record, clock)
    return HostPlacement(record)


def standing(record, node="laptop"):
    """The live reservations no task and no run holds: {(holder, resource): n}."""
    return {
        (r["holder"], r["resource"]): r["amount"]
        for r in record.read("reservations", live=True, node=node)
        if r["task"] is None and r["run"] is None
    }


def make_task(record, kind="agent", sandbox="harness", labels=(), needs=None, **kw):
    """File a task of the admitted effort; an agent is of the claude-code
    harness unless its labels say otherwise."""
    labels = list(labels)
    if kind in ("agent", "review") and not any(
        x.startswith("harness:") for x in labels
    ):
        labels.append("harness:claude-code")
    return record.file_task(
        kind, effort="0001", labels=labels, needs=needs, sandbox=sandbox, **kw
    )


def start(record, task, fit, holder="worker"):
    """What the services do with a Fit: the task's needs set to the Fit's, then
    the run started on its node."""
    record.edit_task(task, needs=fit.needs)
    return record.start_run(
        task, holder, node=fit.node, sandbox=fit.sandbox, fence=None, expect_s=60
    )


def run(record, placement, clock, task, holder="worker"):
    fit = placement.place(task, now=clock())
    assert isinstance(fit, Fit), fit
    return start(record, task, fit, holder)


def kinds(record, since):
    """The journal's event kinds after seq `since`."""
    return [e["kind"] for e in record.dump()["journal"] if e["seq"] > since]


# -- declare ----------------------------------------------------------------------


@over_backends
def test_declare_of_the_sample_makes_the_node_the_capacity_and_the_consumers(
    record, clock
):
    """templet:3161 -- `declare` of the sample on an empty record, with given
    figures, makes the node with its drivers, every capacity (each "measure"
    from its figure, and `lane.<name>` at the lane's max), each consumer's
    reservations (holder, no task, no run, no end) and the document reading
    with its sha256 and the figures."""
    document = sample()
    result = HostPlacement(record).declare(document, figures=FIGURES, now=clock())
    assert result.ok and result.refusals == {} and result.failed == ()
    assert result.wrote
    node = record.get("nodes", "laptop")
    assert node["drivers"] == ["harness", "podman", "process", "vm"]
    assert node["retired_at"] is None
    rows = {c["resource"]: c for c in record.read("capacity", node="laptop")}
    assert rows["memory"]["amount"] == 48 * GIB
    assert rows["disk"]["amount"] == 200 * GIB
    assert rows["lane.widgets"]["amount"] == 4
    assert rows["slots.local"]["amount"] == 0
    assert rows["podman.memory"]["amount"] == 14 * GIB
    for resource, row in rows.items():
        assert row["unit"] == unit_of(resource), resource
    expected = {r: v for r, v in document.capacity.items() if v != "measure"}
    assert {r: c["amount"] for r, c in rows.items() if r in expected} == expected
    held = [
        r
        for r in record.read("reservations", live=True, node="laptop")
        if r["task"] is None
    ]
    assert {(r["holder"], r["resource"]): r["amount"] for r in held} == STANDING
    for r in held:
        assert r["run"] is None and r["until_at"] is None
        assert r["from_at"] == clock()
    (reading,) = record.read("readings", source="allocator", kind="document")
    assert reading["value"]["figures"] == FIGURES
    data = reading["value"]["document"]
    assert data["node"] == "laptop"
    assert data["capacity"]["memory"] == 48 * GIB
    assert data["consumer"][2]["memory"] == 3 * GIB // 2
    digest = reading["value"]["sha256"]
    assert len(digest) == 64 and int(digest, 16) >= 0
    unresolved = json.dumps(
        document_data(document), sort_keys=True, separators=(",", ":")
    )
    assert digest == hashlib.sha256(unresolved.encode()).hexdigest()
    assert record.check() == []


def document_data(document):
    from quarrylith.allocator import to_data

    return to_data(document)


@over_backends
def test_a_second_declare_of_the_same_document_and_figures_writes_nothing(
    record, clock
):
    """templet:3161 -- a second `declare` of the same document and figures
    writes nothing: the head is unchanged, the document reading included."""
    declared(record, clock)
    head = record.head()
    clock.step()
    result = declare(record, clock)
    assert result.ok and result.wrote == ()
    assert record.head() == head
    assert len(record.read("readings", source="allocator", kind="document")) == 1


@over_backends
def test_a_measure_with_no_figure_is_refused_naming_the_key(record, clock):
    """templet:3161 -- a "measure" with no figure is refused, naming the key,
    and the record is unchanged."""
    from quarrylith.allocator import DocumentError

    head = record.head()
    for figures, key in (
        ({"capacity": {"memory": 48 * GIB}, "consumers": FIGURES["consumers"]}, "disk"),
        ({"capacity": FIGURES["capacity"], "consumers": {}}, "yard"),
    ):
        with pytest.raises(DocumentError) as raised:
            declare(record, clock, figures=figures)
        assert key in str(raised.value) and "measure" in str(raised.value)
    assert record.head() == head
    assert record.read("nodes") == []


@over_backends
def test_declare_with_a_changed_consumer_replaces_its_reservation_in_one_event(
    record, clock
):
    """templet:3161 -- `declare` of the sample with a consumer's amount changed
    replaces its reservation in one event, and a consumer the document no
    longer names is released. A hold of another holder is not touched."""
    placement = declared(record, clock)
    ids = placement.hold("vm:lab-1", {"memory": 4 * GIB, "lab": 1}, now=clock())
    assert isinstance(ids, list)
    before = standing(record)
    clock.step()
    mark = record.head()
    bigger = sample('memory = "15.4GiB"', 'memory = "16GiB"')
    result = HostPlacement(record).declare(bigger, figures=FIGURES, now=clock())
    assert result.ok
    # the replace, then the new document reading: nothing else
    assert kinds(record, mark) == ["reserve.replace", "reading"]
    now = standing(record)
    assert now[("podman-machine", "memory")] == 16 * GIB
    same = [k for k in before if k != ("podman-machine", "memory")]
    assert {k: now[k] for k in same} == {k: before[k] for k in same}
    clock.step()
    mark = record.head()
    desktop = (
        '[[consumer]]\nname = "desktop"\nmemory = "10GiB"\n'
        'why = "the operating system and the user\'s own applications"\n'
    )
    text = sample_text().replace('memory = "15.4GiB"', 'memory = "16GiB"')
    assert desktop in text
    gone = load_document(text.replace(desktop, ""))
    result = HostPlacement(record).declare(gone, figures=FIGURES, now=clock())
    assert result.ok
    assert kinds(record, mark) == ["reserve.release", "reading"]
    after = standing(record)
    assert ("desktop", "memory") not in after
    assert after[("vm:lab-1", "memory")] == 4 * GIB
    assert after[("vm:lab-1", "lab")] == 1
    assert record.check() == []


@over_backends
def test_declare_of_consumers_that_do_not_fit_is_refused_before_any_write(
    record, clock
):
    """templet:3161 -- a document whose consumers do not fit is refused before
    any write, with a Refusal per consumer, and the record is unchanged."""
    figures = {**FIGURES, "capacity": {"memory": 11 * GIB, "disk": 200 * GIB}}
    head = record.head()
    result = declare(record, clock, figures=figures)
    assert not result.ok and result.wrote == () and result.failed == ()
    assert record.head() == head
    assert record.read("nodes") == []
    # desktop's 10 GiB fits; the machine's 15.4 and the yard's 1.5 do not fit
    # in the 1 GiB left
    assert list(result.refusals) == ["podman-machine", "yard"]
    machine = result.refusals["podman-machine"]
    assert isinstance(machine, Refusal)
    assert machine.task is None and machine.waits is False
    (short,) = [s for s in machine.shorts if s.resource == "memory"]
    assert (short.cause, short.asked) == ("room", PODMAN)
    assert (short.free, short.capacity) == (GIB, 11 * GIB)
    assert short.holders == [("desktop", 10 * GIB)]
    assert machine.text.startswith("allocator: hold refused on laptop")
    yard = result.refusals["yard"]
    (short,) = yard.shorts
    assert (short.asked, short.free) == (3 * GIB // 2, GIB)


@over_backends
def test_declare_releases_the_newer_of_two_holds_of_one_consumer_and_resource(
    record, clock
):
    """templet:3161 -- the newer of two live holds of one consumer and
    resource is released by `declare` (invariant 8), and the older stays."""
    declared(record, clock)
    older = [
        r["id"]
        for r in record.read("reservations", live=True, node="laptop")
        if r["holder"] == "desktop"
    ]
    clock.step()
    (newer,) = record.reserve(
        "laptop", {"memory": GIB}, holder="desktop", from_at=clock(), until_at=None
    )
    result = declare(record, clock)
    assert result.ok and result.wrote
    live = [
        r["id"]
        for r in record.read("reservations", live=True, node="laptop")
        if r["holder"] == "desktop"
    ]
    assert live == older and newer not in live


@over_backends
def test_declare_with_a_capacity_below_what_is_reserved_reports_what_failed(
    effort, clock
):
    """templet:3161 -- a `set_capacity` below what a run reserved is the
    record's refusal, passed on: `declare` reports what it wrote and what it
    did not, writes no document reading, and a second `declare` finishes."""
    record = effort
    placement = declared(record, clock)
    task = make_task(record)
    held = run(record, placement, clock, task)
    smaller = load_document(
        sample_text()
        .replace('"slots.gate" = 2', '"slots.gate" = 1')
        .replace('"slots.claude-code" = 4', '"slots.claude-code" = 0')
    )
    clock.step()
    result = HostPlacement(record).declare(smaller, figures=FIGURES, now=clock())
    assert not result.ok and result.refusals == {}
    assert [what for what, _ in result.failed] == ["capacity slots.claude-code"]
    assert "reserved" in result.failed[0][1]
    assert any("slots.gate" in line for line in result.wrote)
    assert record.get("capacity", ["laptop", "slots.gate"])["amount"] == 1
    assert record.get("capacity", ["laptop", "slots.claude-code"])["amount"] == 4
    assert len(record.read("readings", source="allocator", kind="document")) == 1
    record.finish_run(held, "done", "ok", footprint={}, cost={})
    clock.step()
    result = HostPlacement(record).declare(smaller, figures=FIGURES, now=clock())
    assert result.ok and len(result.wrote) == 2
    assert record.get("capacity", ["laptop", "slots.claude-code"])["amount"] == 0
    assert len(record.read("readings", source="allocator", kind="document")) == 2


@over_backends
def test_declare_orders_the_capacity_around_the_consumers(record, clock):
    """templet:3161 -- a capacity that grows is set before the consumers that
    need it, and one that shrinks after the consumers that shrink with it, so
    a document that moves both at once is written whole, not refused."""
    declared(record, clock)
    clock.step()
    # the machine shrinks to 8 GiB and the memory with it, below the 26.9 GiB
    # the old consumers held: the capacity can only fall once the machine has
    smaller = sample_text().replace('memory = "15.4GiB"', 'memory = "8GiB"')
    figures = {**FIGURES, "capacity": {"memory": 22 * GIB, "disk": 200 * GIB}}
    result = HostPlacement(record).declare(
        load_document(smaller), figures=figures, now=clock()
    )
    assert result.ok, result
    assert record.get("capacity", ["laptop", "memory"])["amount"] == 22 * GIB
    assert standing(record)[("podman-machine", "memory")] == 8 * GIB
    # the machine grows to 30 GiB with the memory: the capacity first
    clock.step()
    bigger = sample_text().replace('memory = "15.4GiB"', 'memory = "30GiB"')
    figures = {**FIGURES, "capacity": {"memory": 60 * GIB, "disk": 200 * GIB}}
    result = HostPlacement(record).declare(
        load_document(bigger), figures=figures, now=clock()
    )
    assert result.ok, result
    assert record.get("capacity", ["laptop", "memory"])["amount"] == 60 * GIB
    assert standing(record)[("podman-machine", "memory")] == 30 * GIB
    assert record.check() == []


@over_backends
def test_declare_checks_a_capacity_below_unchanged_consumers_before_any_write(
    record, clock
):
    """templet:3161 -- the whole document is checked before any write, a
    capacity that shrinks below consumers the document leaves unchanged
    included: a Refusal per consumer that no longer fits, and nothing written
    (not even the other capacities the document changes)."""
    declared(record, clock)
    clock.step()
    head = record.head()
    other = sample_text().replace('"slots.codex" = 3', '"slots.codex" = 5')
    figures = {**FIGURES, "capacity": {"memory": 11 * GIB, "disk": 200 * GIB}}
    result = HostPlacement(record).declare(
        load_document(other), figures=figures, now=clock()
    )
    assert not result.ok and result.wrote == () and result.failed == ()
    assert record.head() == head
    assert record.get("capacity", ["laptop", "slots.codex"])["amount"] == 3
    # desktop's 10 GiB fits in 11; the machine and the yard do not fit in 1
    assert list(result.refusals) == ["podman-machine", "yard"]
    (short,) = [s for s in result.refusals["podman-machine"].shorts]
    assert (short.cause, short.free, short.capacity) == ("room", GIB, 11 * GIB)


@over_backends
def test_two_consumers_that_swap_amounts_on_an_exact_table_fit_in_any_order(
    record, clock
):
    """templet:3161 -- the shrinks are checked and written before the grows, so
    a document that swaps two consumers' amounts on a table with no spare room
    is not refused for the order its consumers are written in."""
    text = sample_text()
    wide = text.replace('memory = "15.4GiB"', 'memory = "16GiB"')
    exact = {
        **FIGURES,
        "capacity": {"memory": 55 * GIB // 2, "disk": 200 * GIB},
    }
    declared(record, clock, load_document(wide), exact)
    ten = 'name = "desktop"\nmemory = "10GiB"'
    swapped = wide.replace(ten, 'name = "desktop"\nmemory = "16GiB"').replace(
        'name = "podman-machine"\nmemory = "16GiB"',
        'name = "podman-machine"\nmemory = "10GiB"',
    )
    assert swapped != wide
    for document in (swapped, wide):
        clock.step()
        result = HostPlacement(record).declare(
            load_document(document), figures=exact, now=clock()
        )
        assert result.ok, result
    now = standing(record)
    assert now[("desktop", "memory")] == 10 * GIB
    assert now[("podman-machine", "memory")] == 16 * GIB
    assert record.check() == []


@over_backends
def test_declare_without_figures_is_refused_naming_the_key(record, clock):
    """templet:3161 -- `figures=None` is a "measure" with no figure too: the
    DocumentError names the key, and the record is unchanged."""
    from quarrylith.allocator import DocumentError

    head = record.head()
    with pytest.raises(DocumentError, match="capacity.memory"):
        HostPlacement(record).declare(sample(), figures=None, now=clock())
    assert record.head() == head


@over_backends
def test_declare_with_changed_figures_and_an_equal_document_writes_the_new_ones(
    record, clock
):
    """templet:3161 -- the same document with other figures has the same
    sha256 and is not the same declaration: the capacity and a new reading are
    written."""
    declared(record, clock)
    clock.step()
    other = {**FIGURES, "capacity": {"memory": 64 * GIB, "disk": 200 * GIB}}
    result = HostPlacement(record).declare(sample(), figures=other, now=clock())
    assert result.ok and result.wrote
    assert record.get("capacity", ["laptop", "memory"])["amount"] == 64 * GIB
    first, second = record.read("readings", source="allocator", kind="document")
    assert first["value"]["sha256"] == second["value"]["sha256"]
    assert second["value"]["figures"] == other


@over_backends
def test_a_standing_hold_with_an_end_is_replaced_by_one_without(record, clock):
    """templet:3161 -- a consumer's hold that has an end is not the declared
    one even when its amount is: `declare` replaces it by one with no end."""
    declared(record, clock)
    (old,) = [
        r["id"]
        for r in record.read("reservations", live=True, node="laptop")
        if r["holder"] == "desktop"
    ]
    record.release_reservation(old, "test")
    clock.step()
    record.reserve(
        "laptop",
        {"memory": 10 * GIB},
        holder="desktop",
        from_at=clock(),
        until_at=clock() + 3_600_000,
    )
    mark = record.head()
    result = declare(record, clock)
    assert result.ok
    assert kinds(record, mark) == ["reserve.replace"]
    (made,) = [
        r
        for r in record.read("reservations", live=True, node="laptop")
        if r["holder"] == "desktop"
    ]
    assert made["until_at"] is None and made["amount"] == 10 * GIB


@over_backends
def test_declare_leaves_the_old_node_of_a_renamed_node_alone(record, clock):
    """templet:3161 -- a document that names another node does not release the
    old node's holds or retire it: that is a person's act outside `declare`.
    Both nodes show in `headroom`."""
    placement = declared(record, clock)
    before = standing(record)
    clock.step()
    renamed = sample('node = "laptop"', 'node = "desk"')
    result = HostPlacement(record).declare(renamed, figures=FIGURES, now=clock())
    assert result.ok
    assert standing(record, "laptop") == before
    assert record.get("nodes", "laptop")["retired_at"] is None
    assert standing(record, "desk") == before
    assert sorted(placement.headroom(now=clock())) == ["desk", "laptop"]


@over_backends
def test_a_hold_a_failed_declare_made_stays_until_a_person_releases_it(effort, clock):
    """templet:3161 -- a `declare` that fails halfway and is followed by a
    different document may leave a stale hold: one the failed document named
    and neither the last written one nor the new one does. It over-reserves
    and never over-commits, and `release` frees it."""
    record = effort
    placement = declared(record, clock)
    run(record, placement, clock, make_task(record))
    text = sample_text()
    extra = '\n[[consumer]]\nname = "extra"\nmemory = "1GiB"\nwhy = "an addition"\n'
    clock.step()
    failing = text.replace('"slots.claude-code" = 4', '"slots.claude-code" = 0')
    result = HostPlacement(record).declare(
        load_document(failing + extra), figures=FIGURES, now=clock()
    )
    assert not result.ok and standing(record)[("extra", "memory")] == GIB
    clock.step()
    assert declare(record, clock).ok
    assert standing(record)[("extra", "memory")] == GIB
    assert placement.headroom(now=clock())["laptop"]["memory"] == (
        48 * GIB - 10 * GIB - PODMAN - 3 * GIB // 2 - GIB - 2 * GIB
    )
    assert len(placement.release("extra", why="stale")) == 1
    assert ("extra", "memory") not in standing(record)


@over_backends
def test_an_unreadable_document_reading_is_no_document(effort, clock):
    """templet:3161 -- a stored reading that does not parse (an older shape, a
    format that is not 1) is no document: `place` and `hold` answer `node`
    instead of raising, `release` finds nothing, and `declare` writes a new
    reading over it."""
    record = effort
    task = make_task(record, "job", "process")
    placement = HostPlacement(record)
    for bad in ({"sha256": "x"}, {"document": {"format": 2}}, {"document": 5}):
        record.record_reading("allocator", "document", bad)
        clock.step()
        refusal = placement.place(task, now=clock())
        assert [s.cause for s in refusal.shorts] == ["node"], bad
        refusal = placement.hold("x", {"memory": 1}, now=clock())
        assert [s.cause for s in refusal.shorts] == ["node"], bad
        assert placement.release("x", why="none") == []
    assert declare(record, clock).ok
    host_reading(record, clock)
    assert isinstance(placement.place(task, now=clock()), Fit)


@over_backends
def test_place_reads_the_latest_document_after_a_redeclare(effort, clock):
    """templet:3161 -- a document that changes a class is the one `place`
    answers from once declared: the newest reading, not the first."""
    record = effort
    placement = declared(record, clock)
    task = make_task(record)
    assert placement.place(task, now=clock()).needs["memory"] == 2 * GIB
    text = sample_text()
    old = (
        'name = "agent"\nmatch = { kind = ["agent", "review"] }\n'
        'needs = { memory = "2GiB"'
    )
    assert old in text
    clock.step()
    result = HostPlacement(record).declare(
        load_document(text.replace(old, old.replace("2GiB", "3GiB"))),
        figures=FIGURES,
        now=clock(),
    )
    assert result.ok
    assert placement.place(task, now=clock()).needs["memory"] == 3 * GIB


# -- place ------------------------------------------------------------------------


@over_backends
def test_place_of_a_task_that_fits_gives_a_fit_and_writes_nothing(effort, clock):
    """templet:3161 -- `place` of a task that fits gives a Fit with the node,
    the class and the needs, and writes nothing (head unchanged)."""
    placement = declared(effort, clock)
    task = make_task(effort, labels=["repo:widgets", "class:deep"])
    head = effort.head()
    fit = placement.place(task, now=clock())
    assert effort.head() == head
    assert isinstance(fit, Fit)
    assert (fit.task, fit.node, fit.run_class, fit.sandbox) == (
        task,
        "laptop",
        "agent",
        "harness",
    )
    assert fit.needs == {
        "memory": 2 * GIB,
        "disk": 4 * GIB,
        "slots.claude-code": 1,
        "budget.anthropic": 1,
        "lane.widgets": 1,
    }
    assert "agent" in fit.why and "laptop" in fit.why and "\n" not in fit.why
    assert placement.place(task, now=clock()) == fit


@over_backends
def test_place_past_the_tables_free_is_refused_room_with_waits_by_what_holds(
    effort, clock
):
    """templet:3161 -- `place` of a task past the table's free is refused
    `room`, naming each short resource with asked, free, capacity and the
    largest holders. `waits` is true when the needs fit beside the
    reservations no run holds, and false when they do not."""
    record = effort
    placement = declared(record, clock)
    # a run holds 18 GiB: the VM's 5 do not fit in what is left, but would
    # beside the standing consumers alone
    filler = make_task(record, "job", "process", needs={"memory": 18 * GIB})
    run(record, placement, clock, filler)
    vm = make_task(record, "job", "vm")
    refusal = placement.place(vm, now=clock())
    assert isinstance(refusal, Refusal)
    assert (refusal.task, refusal.run_class) == (vm, "vm")
    (short,) = refusal.shorts
    left = 48 * GIB - sum(v for (_, r), v in STANDING.items() if r == "memory")
    assert short.cause == "room" and short.resource == "memory"
    assert short.asked == 5 * GIB and short.free == left - 18 * GIB
    assert short.capacity == 48 * GIB
    assert short.node == "laptop" and short.waits is True and short.until is None
    assert short.holders[0] == ("worker", 18 * GIB)
    assert [h for h, _ in short.holders[1:]] == ["podman-machine", "desktop"]
    assert len(short.holders) == 3
    assert refusal.waits is True and refusal.until is None
    assert refusal.needs["memory"] == 5 * GIB
    assert refusal.text.startswith(f"allocator: task {vm} (vm) refused on laptop")
    assert refusal.text.endswith("; waits")
    assert "\n" not in refusal.text


@over_backends
def test_room_never_waits_when_the_standing_consumers_leave_too_little(effort, clock):
    """templet:3161 -- a 5 GiB VM on a table whose standing consumers leave
    4 GiB is refused `room` with waits false: finishing runs cannot help."""
    record = effort
    standing_memory = 10 * GIB + PODMAN + 3 * GIB // 2
    figures = {
        **FIGURES,
        "capacity": {"memory": standing_memory + 4 * GIB, "disk": 200 * GIB},
    }
    placement = declared(record, clock, figures=figures)
    vm = make_task(record, "job", "vm")
    refusal = placement.place(vm, now=clock())
    (short,) = refusal.shorts
    assert (short.cause, short.asked, short.free) == ("room", 5 * GIB, 4 * GIB)
    assert short.waits is False and refusal.waits is False
    assert refusal.text.endswith("; never")


@over_backends
def test_a_lane_at_its_max_refuses_a_task_in_it_and_a_task_outside_fits(effort, clock):
    """templet:3161 -- a lane at its `max` refuses a task in it as `room` on
    `lane.<name>`, with waits true, while a task in no lane, of the same
    class, fits. A task whose needs ask 3 slots fits only when 3 are free
    together."""
    record = effort
    placement = declared(record, clock, sample("max = 4", "max = 1"))
    inside = ["repo:widgets"]
    first = make_task(record, "gate", "process", labels=inside)
    run(record, placement, clock, first)
    second = make_task(record, "gate", "process", labels=inside)
    refusal = placement.place(second, now=clock())
    assert isinstance(refusal, Refusal)
    (short,) = refusal.shorts
    assert (short.cause, short.resource) == ("room", "lane.widgets")
    assert (short.asked, short.free, short.capacity) == (1, 0, 1)
    assert short.waits is True and refusal.waits is True
    other = make_task(record, "gate", "process", labels=["repo:gadgets"])
    fit = placement.place(other, now=clock())
    assert isinstance(fit, Fit) and "lane.widgets" not in fit.needs
    assert fit.run_class == "gate"
    # three slots at once: only when three are free together
    trilithon = make_task(record, needs={"slots.claude-code": 3})
    assert isinstance(placement.place(trilithon, now=clock()), Fit)
    holder = make_task(record, needs={"slots.claude-code": 2})
    held = run(record, placement, clock, holder)
    refusal = placement.place(trilithon, now=clock())
    (short,) = refusal.shorts
    assert (short.resource, short.asked, short.free) == ("slots.claude-code", 3, 2)
    assert short.waits is True
    record.finish_run(held, "done", "ok", footprint={}, cost={})
    assert isinstance(placement.place(trilithon, now=clock()), Fit)


@over_backends
def test_undeclared_driver_and_node_never_wait(effort, clock):
    """templet:3161 -- a resource with no capacity row is `undeclared`; a
    sandbox the node does not run is `driver`; with no document declared the
    answer is `node`. All three have waits false."""
    record = effort
    nothing = make_task(record, "job", "process")
    placement = HostPlacement(record)
    refusal = placement.place(nothing, now=clock())
    assert isinstance(refusal, Refusal) and refusal.waits is False
    (short,) = refusal.shorts
    assert short.cause == "node" and short.waits is False
    assert refusal.text.startswith(f"allocator: task {nothing} refused")
    declared(record, clock)
    odd = make_task(record, "job", "process", needs={"mystery": 1})
    refusal = placement.place(odd, now=clock())
    (short,) = refusal.shorts
    assert (short.cause, short.resource) == ("undeclared", "mystery")
    assert short.asked == 1 and short.waits is False and refusal.waits is False
    far = make_task(record, "job", "elsewhere")
    refusal = placement.place(far, now=clock())
    (short,) = refusal.shorts
    assert (short.cause, short.node, short.resource) == ("driver", "laptop", None)
    assert short.waits is False and refusal.waits is False
    # both at once: every short is collected, in CAUSES order
    both = make_task(record, "job", "elsewhere", needs={"mystery": 1})
    refusal = placement.place(both, now=clock())
    assert [s.cause for s in refusal.shorts] == ["undeclared", "driver"]
    # a retired node is a node that is not declared
    for holder, _ in list(standing(record)):
        placement.release(holder, why="test")
    record.retire_node("laptop")
    refusal = placement.place(nothing, now=clock())
    assert [s.cause for s in refusal.shorts] == ["node"]


@over_backends
def test_a_fit_then_start_run_takes_exactly_its_needs(effort, clock):
    """templet:3161 -- a Fit's needs set on the task by `edit_task`, then
    `start_run`, take exactly those reservations. When another start takes
    the room in between, `start_run` is refused and `place` then gives
    `room`."""
    record = effort
    placement = declared(record, clock)
    task = make_task(record, labels=["repo:widgets"])
    fit = placement.place(task, now=clock())
    run_id = start(record, task, fit)
    taken = {
        r["resource"]: r["amount"]
        for r in record.read("reservations", live=True, run=run_id)
    }
    assert taken == fit.needs
    record.finish_run(run_id, "done", "ok", footprint={}, cost={})
    # a race: another start takes the last room between place and start_run
    left = record.free("laptop", "slots.claude-code", from_at=clock(), until_at=None)
    assert left == 4
    mine = make_task(record, needs={"slots.claude-code": 4})
    fit = placement.place(mine, now=clock())
    assert isinstance(fit, Fit)
    rival = make_task(record, needs={"slots.claude-code": 1})
    run(record, placement, clock, rival, holder="rival")
    record.edit_task(mine, needs=fit.needs)
    with pytest.raises(Refused, match="no room"):
        record.start_run(
            mine, "worker", node="laptop", sandbox="harness", fence=None, expect_s=60
        )
    refusal = placement.place(mine, now=clock())
    assert isinstance(refusal, Refusal)
    (short,) = refusal.shorts
    assert (short.cause, short.resource) == ("room", "slots.claude-code")
    assert (short.asked, short.free) == (4, 3)


def script(record, clock):
    """The same operations at the same nows: declare, place a spread of tasks,
    start a run, hold, place again. Returns every answer as plain data."""
    out = []
    placement = HostPlacement(record)
    admitted(record)
    out.append(placement.declare(sample(), figures=FIGURES, now=clock()).wrote)
    host_reading(record, clock)
    tasks = [
        make_task(record, labels=["repo:widgets"]),
        make_task(record, "job", "vm"),
        make_task(record, "gate", "process"),
        make_task(record, "job", "process", needs={"mystery": 1}),
        make_task(record, "job", "elsewhere"),
        make_task(record, "job", "podman"),
    ]

    def ask():
        for task in tasks:
            answer = placement.place(task, now=clock())
            out.append(
                answer.as_data()
                if isinstance(answer, Refusal)
                else [answer.node, answer.run_class, answer.needs, answer.why]
            )

    ask()
    run(record, placement, clock, tasks[0])
    clock.step()
    held = placement.hold(
        "vm:a", {"memory": 4 * GIB, "lab": 1}, task=tasks[1], now=clock()
    )
    out.append(held if isinstance(held, list) else held.as_data())
    ask()
    out.append(placement.headroom(now=clock()))
    return json.loads(json.dumps(out))


def test_the_same_operations_give_equal_answers_on_every_backend(tmp_path):
    """templet:3161 -- the same operations at the same nows give equal Fits
    and Refusals on every backend."""
    answers, skipped = {}, []
    names = sorted(f for n in contract.BACKENDS for f in FIXTURES.get(n, [n]))
    for backend in names:
        try:
            url = url_for(backend, tmp_path / backend)
        except pytest.skip.Exception as skip:
            skipped.append(f"{backend}: {skip}")
            continue
        clock = Clock()
        record = contract.open_record(url, actor="alpha", via="cli")
        record.clock = clock
        try:
            record.init()
            answers[backend] = script(record, clock)
        finally:
            record.close()
    assert "sqlite" in answers
    first = next(iter(answers.values()))
    assert any(isinstance(item, dict) for item in first)
    for backend, found in answers.items():
        assert found == first, backend
    if skipped:
        pytest.skip("; ".join(skipped))


# -- hold and release -------------------------------------------------------------


@over_backends
def test_hold_with_a_task_outlives_its_runs_and_is_released_by_end_task(effort, clock):
    """templet:3161 -- `hold` with a task makes the task's own reservation,
    kept across two of its runs and released by `end_task`, or earlier by
    `release`."""
    record = effort
    placement = declared(record, clock)
    task = make_task(record, "job", "process", loop={"max_attempts": 3})
    ids = placement.hold(
        "vm:mission", {"memory": 4 * GIB, "lab": 1}, task=task, now=clock()
    )
    assert isinstance(ids, list) and len(ids) == 2
    mine = [r for r in record.read("reservations", live=True, task=task)]
    assert sorted(r["resource"] for r in mine) == ["lab", "memory"]
    assert all(r["run"] is None and r["holder"] == "vm:mission" for r in mine)
    assert all(r["node"] == "laptop" and r["until_at"] is None for r in mine)
    for _ in range(2):
        clock.step()
        first = run(record, placement, clock, task)
        record.finish_run(first, "done", "ok", footprint={}, cost={})
        assert {
            r["id"] for r in record.read("reservations", live=True, task=task)
        } == set(ids)
    record.end_task(task, "done", "mission over")
    assert record.read("reservations", live=True, task=task) == []
    # released earlier by release, and only the holder's own
    other = make_task(record, "job", "process")
    placement.hold("vm:other", {"lab": 1}, task=other, now=clock())
    placement.hold("session", {"memory": GIB}, now=clock())
    released = placement.release("vm:other", why="lab down")
    assert len(released) == 1
    assert record.get("reservations", released[0])["why"] == "lab down"
    assert [
        r["holder"] for r in record.read("reservations", live=True, task=other)
    ] == []
    assert standing(record)[("session", "memory")] == GIB
    assert placement.release("vm:other", why="again") == []


@over_backends
def test_release_leaves_the_reservations_a_live_run_holds(effort, clock):
    """templet:3161 -- `release` frees a holder's reservations that no live run
    holds, and never a run's."""
    record = effort
    placement = declared(record, clock)
    task = make_task(record, "job", "process")
    run(record, placement, clock, task, holder="worker")
    assert placement.release("worker", why="not mine") == []
    assert record.read("reservations", live=True, run=record.read("runs")[0]["id"])


@over_backends
def test_hold_refuses_on_the_table_as_place_does_and_a_second_hold_is_refused(
    effort, clock
):
    """templet:3161 -- `hold` refuses on the table as `place` does (a Refusal
    naming the resource, with waits by what holds it), and a second live hold
    of one holder and resource is refused."""
    record = effort
    placement = declared(record, clock)
    head = record.head()
    refusal = placement.hold("model", {"memory": 40 * GIB}, now=clock())
    assert isinstance(refusal, Refusal) and record.head() == head
    (short,) = refusal.shorts
    assert (short.cause, short.resource, short.asked) == ("room", "memory", 40 * GIB)
    assert short.free == 48 * GIB - sum(
        v for (_, r), v in STANDING.items() if r == "memory"
    )
    assert short.waits is False and refusal.task is None
    assert refusal.text.startswith("allocator: hold refused on laptop")
    refusal = placement.hold("model", {"mystery": 1}, now=clock())
    assert [s.cause for s in refusal.shorts] == ["undeclared"]
    ids = placement.hold("session", {"memory": GIB}, now=clock())
    assert len(ids) == 1
    with pytest.raises(Refused, match="session"):
        placement.hold("session", {"memory": GIB, "disk": GIB}, now=clock())
    assert "disk" not in {r for (h, r) in standing(record) if h == "session"}
    # without a document and without a node, there is nowhere to hold
    bare = HostPlacement(record)
    assert [
        s.cause
        for s in bare.hold("x", {"memory": 1}, node="nowhere", now=clock()).shorts
    ] == ["node"]


@over_backends
def test_hold_before_any_document_is_refused_node(record, clock):
    """templet:3161 -- with no document declared and no node given, `hold`
    answers `node`, with waits false."""
    placement = HostPlacement(record)
    refusal = placement.hold("x", {"memory": 1}, now=clock())
    assert [s.cause for s in refusal.shorts] == ["node"]
    assert refusal.waits is False


@over_backends
def test_a_hold_of_another_resource_or_task_is_not_a_second_hold(effort, clock):
    """templet:3161 -- a second hold is one holder's standing hold of the same
    resource. The same holder's hold of another resource, a task's own hold
    beside a standing one, and two tasks' holds under one holder are not."""
    record = effort
    placement = declared(record, clock)
    assert placement.hold("session", {"memory": GIB}, now=clock())
    assert placement.hold("session", {"disk": GIB}, now=clock())
    one = make_task(record, "job", "process")
    two = make_task(record, "job", "process")
    assert placement.hold("vm:smoke", {"lab": 1}, task=one, now=clock())
    assert placement.hold("vm:smoke", {"lab": 1}, task=two, now=clock())
    assert placement.hold("session", {"memory": GIB}, task=one, now=clock())
    with pytest.raises(Refused, match="vm:smoke"):
        placement.hold("vm:smoke", {"lab": 1}, task=one, now=clock())


@over_backends
def test_declare_leaves_a_tasks_hold_of_a_consumers_name_alone(effort, clock):
    """templet:3161 -- a task's own hold under a consumer's name is not the
    consumer's: it neither blocks the consumer's hold nor is replaced or
    released with it."""
    record = effort
    placement = declared(record, clock)
    task = make_task(record, "job", "process")
    (own,) = placement.hold("desktop", {"memory": GIB}, task=task, now=clock())
    clock.step()
    result = declare(record, clock, sample('memory = "10GiB"', 'memory = "11GiB"'))
    assert result.ok
    assert standing(record)[("desktop", "memory")] == 11 * GIB
    kept = record.get("reservations", own)
    assert kept["released_at"] is None and kept["amount"] == GIB
    assert kept["task"] == task


@over_backends
def test_release_frees_only_the_node_asked_for(effort, clock):
    """templet:3161 -- `release` of a holder on a node leaves its holds on
    another node, and with no node means the declared one."""
    record = effort
    placement = declared(record, clock)
    record.declare_node("other", ["process"], [])
    record.set_capacity("other", "memory", 4 * GIB, "bytes")
    assert placement.hold("session", {"memory": GIB}, now=clock())
    assert placement.hold("session", {"memory": GIB}, node="other", now=clock())
    assert len(placement.release("session", node="other", why="a")) == 1
    assert standing(record, "laptop")[("session", "memory")] == GIB
    assert ("session", "memory") not in standing(record, "other")
    assert placement.hold("session", {"memory": GIB}, node="other", now=clock())
    assert len(placement.release("session", why="b")) == 1
    assert ("session", "memory") not in standing(record, "laptop")
    assert standing(record, "other")[("session", "memory")] == GIB


# -- headroom ---------------------------------------------------------------------


@over_backends
def test_headroom_gives_each_resources_free_on_each_live_node(effort, clock):
    """templet:3161 -- `headroom` gives, per node and resource, what a start
    may still take: the table's free (the budget and the measurement narrow it
    in their own steps), and writes nothing."""
    record = effort
    placement = declared(record, clock)
    head = record.head()
    room = placement.headroom(now=clock())
    assert record.head() == head
    assert list(room) == ["laptop"]
    left = 48 * GIB - sum(v for (_, r), v in STANDING.items() if r == "memory")
    assert room["laptop"]["memory"] == left
    assert room["laptop"]["disk"] == 200 * GIB - 40 * GIB
    assert room["laptop"]["slots.claude-code"] == 4
    assert room["laptop"]["slots.local"] == 0
    assert room["laptop"]["lane.widgets"] == 4
    assert room["laptop"]["podman.memory"] == 14 * GIB
    task = make_task(record, labels=["repo:widgets"])
    run(record, placement, clock, task)
    after = placement.headroom(now=clock())["laptop"]
    assert after["memory"] == left - 2 * GIB
    assert after["slots.claude-code"] == 3 and after["lane.widgets"] == 3
    for holder, _ in list(standing(record)):
        placement.release(holder, why="done")
    # the table has 46 GiB free; the measurement leaves 47 less the 4 GiB floor
    assert placement.headroom(now=clock())["laptop"]["memory"] == 43 * GIB


@over_backends
def test_headroom_lists_live_nodes_only_and_never_a_negative(effort, clock):
    """templet:3161 -- a retired node is not in `headroom`, and a free the
    record would give below zero is 0."""
    record = effort
    placement = declared(record, clock)
    record.declare_node("spare", ["process"], [])
    record.set_capacity("spare", "memory", GIB, "bytes")
    assert sorted(placement.headroom(now=clock())) == ["laptop", "spare"]
    record.retire_node("spare")
    assert list(placement.headroom(now=clock())) == ["laptop"]

    class Overdrawn:
        """A reader whose table has been overdrawn: `free` is below zero."""

        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def free(self, *args, **kw):
            return -5

    room = HostPlacement(Overdrawn(record)).headroom(now=clock())
    assert room["laptop"] and set(room["laptop"].values()) == {0}
