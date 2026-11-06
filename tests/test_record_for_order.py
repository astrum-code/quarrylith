# SPDX-License-Identifier: Apache-2.0
"""What the record gains for the order (templet:3159, the changes proposed on
its page): `landable(*, now)`, `start_run(..., placed=)` and a start that takes
a landing review of a parked effort, over every backend in BACKENDS.

Every fixture starts from one landing review that qualifies (`review_of`) and
each test spoils exactly one fact of it, so a rule that is missing shows as a
test that fails, never as a fixture that happened to pass."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from backends import PARTS, admitted, over_backends

from quarrylith.record import contract
from quarrylith.record.contract import Refused
from quarrylith.record.events import Event, MemoryView, apply

DATA = Path(__file__).resolve().parent / "data"

PLACED = {"effort": "0002", "rule": "landing", "why": "review 1 of 1 landing"}


def parked(rec, id="0002"):
    return rec.file_effort(id, f"e{id}", f"Effort {id}", "owner-a", PARTS, "parked")


def node(rec, slots=3):
    rec.declare_node("n1", ["process", "harness"], [])
    rec.set_capacity("n1", "slots", slots, "slots")


def item(rec, kind="task", effort="0002", **kw):
    kw.setdefault("filed_by", "alpha")
    kw.setdefault("role", "alpha")
    return rec.file_item(kind, "t", "body", effort=effort, **kw)


def change_of(rec, number, branch=None):
    """An open change of item `number`."""
    return rec.open_or_update_change(
        "widgets",
        branch or f"feat/{number}",
        "a1",
        number=number,
        title="work",
        by="alpha",
    )


def review_of(rec, effort="0002", *, change=True):
    """A review item of `effort`, with its task, of a target item that has an
    open change: what the landing tier reviews."""
    target = item(rec, effort=effort)
    review = item(rec, "review", effort=effort)
    rec.file_review(target, 1, "pool", item=review, author="alpha")
    made = None
    if change:
        made = change_of(rec, target)
    task = rec.file_task("review", number=review, needs={"slots": 1}, sandbox="process")
    return SimpleNamespace(target=target, review=review, task=task, change=made)


def grant(rec, n, holder="alpha"):
    return rec.grant(n, holder, branch=f"feat/{n}", machine="m1")


def start(rec, task, *, fence=None, holder="alpha", **kw):
    kw.setdefault("node", "n1")
    kw.setdefault("sandbox", "process")
    kw.setdefault("expect_s", 60)
    return rec.start_run(task, holder, fence=fence, **kw)


def land(rec, made, **kw):
    """Start the landing review `made`, under a lease granted now."""
    kw.setdefault("placed", dict(PLACED))
    return start(rec, made.task, fence=grant(rec, made.review), **kw)


def landable(rec, clock):
    return rec.landable(now=clock())


def numbers(got):
    return [i["number"] for i in got["items"]], [t["id"] for t in got["tasks"]]


def start_event(rec):
    return [e for e in rec.dump()["journal"] if e["kind"] == "run.start"][-1]


def replay(rec):
    dump = rec.dump()
    view = MemoryView()
    for raw in dump["journal"]:
        apply(view, Event.from_dict(raw))
    assert view.records() == dump["records"]
    return dump


@pytest.fixture
def world(record):
    admitted(record)
    parked(record)
    node(record)
    return record


# -- landable -----------------------------------------------------------------


@over_backends
def test_landable_gives_a_parked_efforts_review_of_an_open_change(world, clock):
    """templet:3159 -- `landable(now=)` is `{"items", "tasks"}`: a review
    item, ready, of a parked effort, whose reviewed item has an open change,
    and its open task; each in key order."""
    first = review_of(world)
    second = review_of(world)
    assert world.ready(now=clock()) == {"items": [], "tasks": []}
    got = landable(world, clock)
    assert set(got) == {"items", "tasks"}
    assert numbers(got) == (
        [first.review, second.review],
        [first.task, second.task],
    )
    assert got["items"][0] == world.get("items", first.review)
    assert got["tasks"][0] == world.get("tasks", first.task)
    assert world.check() == []


@over_backends
def test_landable_writes_nothing_and_takes_a_time(world, clock):
    """templet:3159 -- `landable` is a read: it writes nothing, and `now` is a
    time in milliseconds, as for `ready`."""
    review_of(world)
    head, dump = world.head(), world.dump()
    landable(world, clock)
    assert world.head() == head and world.dump() == dump
    for bad in ("soon", 1.5, None, True):
        with pytest.raises(Refused, match="milliseconds"):
            world.landable(now=bad)


@over_backends
def test_landable_leaves_out_what_ready_gives(world, clock):
    """templet:3159 -- a review of an admitted effort is `ready`'s, never
    `landable`'s, and a review linked to an admitted effort and a parked one
    is `ready`'s too: landable gives only what no linked effort admits."""
    mine = review_of(world, effort="0001")
    both = review_of(world)
    world.link("0001", "item", str(both.review))
    assert numbers(landable(world, clock)) == ([], [])
    got = world.ready(now=clock())
    assert numbers(got) == (
        [mine.target, mine.review, both.review],
        [mine.task, both.task],
    )


@over_backends
def test_landable_needs_a_parked_effort(world, clock):
    """templet:3159 -- an effort that is proposed or concluded is not parked:
    a review linked only to those is not given; one linked to a parked effort
    and a proposed one is."""
    world.file_effort("0003", "e3", "Three", "owner-a", PARTS, "proposed")
    world.file_effort("0004", "e4", "Four", "owner-a", PARTS, "parked")
    world.conclude("0004", "complete", "finished")
    other = review_of(world, effort="0003")
    ended = review_of(world, effort="0004")
    mixed = review_of(world)
    world.link("0003", "item", str(mixed.review))
    got = numbers(landable(world, clock))
    assert got == ([mixed.review], [mixed.task])
    assert other.review not in got[0] and ended.review not in got[0]
    world.set_standing("0002", "proposed", "no longer parked")
    assert numbers(landable(world, clock)) == ([], [])


@over_backends
def test_landable_follows_the_efforts_standing(world, clock):
    """templet:3159 -- the standing is read now: admitting the effort moves a
    review from `landable` to `ready`, and parking it again moves it back."""
    made = review_of(world)
    assert numbers(landable(world, clock)) == ([made.review], [made.task])
    world.set_standing("0002", "admitted", "go")
    assert numbers(landable(world, clock)) == ([], [])
    assert numbers(world.ready(now=clock())) == (
        [made.target, made.review],
        [made.task],
    )
    world.set_standing("0002", "parked", "pause")
    assert numbers(landable(world, clock)) == ([made.review], [made.task])
    assert world.ready(now=clock()) == {"items": [], "tasks": []}


@over_backends
def test_landable_gives_reviews_only(world, clock):
    """templet:3159 -- an item or a task that is not a review is never given:
    a task item, an agent task of a review item is the item's (given), a job
    with no item, and an agent task of a task item."""
    work = item(world)
    change_of(world, work)
    world.file_task("agent", number=work, needs={"slots": 1}, sandbox="process")
    world.file_task("job", effort="0002", needs={"slots": 1}, sandbox="process")
    assert numbers(landable(world, clock)) == ([], [])
    made = review_of(world)
    agent = world.file_task("agent", number=made.review, sandbox="process")
    assert numbers(landable(world, clock)) == (
        [made.review],
        [made.task, agent],
    )


@over_backends
def test_landable_gives_a_review_task_of_an_item_that_is_not_a_review(world, clock):
    """templet:3159 -- a task of kind `review` is a review whatever its
    item's kind, as the order reads it: the item it reviews is the one a
    review record names it for."""
    made = review_of(world)
    plain = item(world)
    world.file_review(made.target, 2, "pool", item=plain, author="alpha")
    task = world.file_task("review", number=plain, sandbox="process")
    nameless = item(world)
    world.file_task("review", number=nameless, sandbox="process")
    assert numbers(landable(world, clock)) == (
        [made.review],
        [made.task, task],
    )


@over_backends
def test_landable_needs_an_open_change_of_the_reviewed_item(world, clock):
    """templet:3159 -- the reviewed item has a change that is open: none, a
    merged one and a dropped one are not."""
    review_of(world, change=False)
    merged = review_of(world)
    dropped = review_of(world)
    world.record_merge(merged.change, "a1", "alpha", "pool-approved")
    world.drop_change(dropped.change, "alpha", "closed")
    unreviewing = item(world, "review")
    world.file_task("review", number=unreviewing, sandbox="process")
    assert numbers(landable(world, clock)) == ([], [])
    other = review_of(world)
    assert numbers(landable(world, clock)) == ([other.review], [other.task])


@over_backends
def test_landable_follows_the_latest_review_record(world, clock):
    """templet:3159 -- an item reviews what its latest review record names,
    by round and then by id: it moves to a target with a change, and from it."""
    made = review_of(world, change=False)
    elsewhere = item(world)
    change_of(world, elsewhere)
    assert numbers(landable(world, clock)) == ([], [])
    world.file_review(elsewhere, 2, "pool", item=made.review, author="alpha")
    assert numbers(landable(world, clock)) == ([made.review], [made.task])
    world.file_review(made.target, 3, "pool", item=made.review, author="alpha")
    assert numbers(landable(world, clock)) == ([], [])


@over_backends
def test_landable_leaves_out_an_item_that_is_not_admitted(world, clock):
    """templet:3159 -- a review item that is not admitted (filed by a machine)
    is not given, nor is its task; admitting it gives both."""
    made = review_of(world)
    bot = world.file_item(
        "review", "m", "b", filed_by="bot", machine=True, role="alpha", effort="0002"
    )
    world.file_review(made.target, 2, "pool", item=bot, author="alpha")
    task = world.file_task("review", number=bot, sandbox="process")
    assert numbers(landable(world, clock)) == ([made.review], [made.task])
    world.admit(bot, "alpha")
    assert numbers(landable(world, clock)) == (
        [made.review, bot],
        [made.task, task],
    )


@over_backends
def test_landable_leaves_out_an_item_with_a_live_dep(world, clock):
    """templet:3159 -- a live dep keeps a review from being given; lifting it
    gives it again."""
    made = review_of(world)
    dep = world.add_dep(made.review, blocker=item(world), why="waits")
    assert numbers(landable(world, clock)) == ([], [])
    world.lift_dep(dep, "alpha", "done")
    assert numbers(landable(world, clock)) == ([made.review], [made.task])


@over_backends
@pytest.mark.parametrize("on", ["item", "role", "holder"])
def test_landable_leaves_out_an_item_under_a_live_hold(world, clock, on):
    """templet:3159 -- a live hold on the review item, on its role or on the
    holder of its lease keeps it from being given; lifting it gives it
    again."""
    made = review_of(world)
    grant(world, made.review, "gamma")
    kw = {
        "item": {"number": made.review},
        "role": {"who": "alpha"},
        "holder": {"who": "gamma"},
    }[on]
    hold = world.hold(cause="user", why="wait", **kw)
    assert numbers(landable(world, clock)) == ([], [])
    world.lift_hold(hold, "alpha")
    assert numbers(landable(world, clock)) == ([], [made.task])


@over_backends
def test_landable_leaves_out_a_task_that_is_not_ready(world, clock):
    """templet:3159 -- a task before its `not_before`, out of attempts, or
    not open is not given."""
    made = review_of(world)
    world.edit_task(made.task, not_before=clock() + 10_000)
    assert numbers(landable(world, clock)) == ([made.review], [])
    clock.step(10_000)
    assert numbers(landable(world, clock)) == ([made.review], [made.task])
    fence = grant(world, made.review)
    run = start(world, made.task, fence=fence, placed=dict(PLACED))
    assert numbers(landable(world, clock))[1] == []
    world.finish_run(run, "failed", "again", footprint={}, cost={})
    assert numbers(landable(world, clock))[1] == []
    world.edit_task(made.task, loop={"max_attempts": 2})
    assert numbers(landable(world, clock))[1] == [made.task]
    world.end_task(made.task, "cancelled", "no longer needed")
    assert numbers(landable(world, clock))[1] == []


@over_backends
def test_landable_gives_the_open_task_of_a_claimed_review_item(world, clock):
    """templet:3159 -- as `ready` gives the open task of a claimed item, a
    claimed review item is not itself given (it is a resume), its open task
    is."""
    made = review_of(world)
    grant(world, made.review)
    assert numbers(landable(world, clock)) == ([], [made.task])
    assert world.ready(now=clock()) == {"items": [], "tasks": []}


@over_backends
def test_ready_stays_as_it_is(world, clock):
    """templet:3159 -- `ready` leaves out a parked effort's review, as it
    always has, and `landable` does not give an admitted effort's work."""
    work = item(world, effort="0001")
    agent = world.file_task("agent", number=work, sandbox="process")
    mine = review_of(world, effort="0001")
    review_of(world)
    assert numbers(world.ready(now=clock())) == (
        [work, mine.target, mine.review],
        [agent, mine.task],
    )
    assert numbers(landable(world, clock))[0] != [work, mine.review]
    assert work not in numbers(landable(world, clock))[0]
    assert agent not in numbers(landable(world, clock))[1]


# -- start_run(placed=) -------------------------------------------------------


@over_backends
def test_placed_is_carried_in_the_run_start_event(world, clock):
    """templet:3159 -- `start_run(..., placed=)` carries `{effort, rule, why}`
    in the `run.start` event's `data`, and a start without it carries no
    `placed` at all: the event is the same as before."""
    for n, rule in enumerate(("floor", "resume", "unblocks-then-age", "listed")):
        work = item(world, effort="0001")
        task = world.file_task("agent", number=work, sandbox="process")
        placed = {"effort": "0001", "rule": rule, "why": f"a {n} reason"}
        run = start(world, task, fence=grant(world, work), placed=placed)
        data = start_event(world)["data"]
        assert data["placed"] == placed and data["run"] == run
    plain = item(world, effort="0001")
    task = world.file_task("agent", number=plain, sandbox="process")
    start(world, task, fence=grant(world, plain))
    assert "placed" not in start_event(world)["data"]
    assert sorted(start_event(world)["data"]) == [
        "attempt",
        "fence",
        "holder",
        "node",
        "reservations",
        "run",
        "task",
    ]
    replay(world)
    assert world.check() == []


@over_backends
def test_placed_is_copied_not_kept_by_reference(world, clock):
    """templet:3159 -- the event holds what `placed` said when the run
    started; the caller's dict is not the event's."""
    work = item(world, effort="0001")
    task = world.file_task("agent", number=work, sandbox="process")
    placed = {"effort": "0001", "rule": "floor", "why": "first"}
    start(world, task, fence=grant(world, work), placed=placed)
    placed["why"] = "changed after"
    assert start_event(world)["data"]["placed"]["why"] == "first"


BAD_PLACED = (
    ("a list", [("effort", "0001")], "placed"),
    ("a string", "landing", "placed"),
    ("no why", {"effort": "0001", "rule": "floor"}, "placed"),
    ("no rule", {"effort": "0001", "why": "w"}, "placed"),
    ("no effort", {"rule": "floor", "why": "w"}, "placed"),
    ("an extra key", {"effort": "0001", "rule": "floor", "why": "w", "x": 1}, "placed"),
    (
        "an effort that is a number",
        {"effort": 1, "rule": "floor", "why": "w"},
        "effort",
    ),
    ("an empty effort", {"effort": "", "rule": "floor", "why": "w"}, "effort"),
    ("a why that is None", {"effort": "0001", "rule": "floor", "why": None}, "why"),
    ("an empty why", {"effort": "0001", "rule": "floor", "why": ""}, "why"),
    (
        "a rule that is none of RULES",
        {"effort": "0001", "rule": "x", "why": "w"},
        "rule",
    ),
    ("a rule in capitals", {"effort": "0001", "rule": "LANDING", "why": "w"}, "rule"),
    (
        "a rule that is a list",
        {"effort": "0001", "rule": ["floor"], "why": "w"},
        "rule",
    ),
)


@over_backends
@pytest.mark.parametrize(
    "placed", [p[1] for p in BAD_PLACED], ids=[p[0] for p in BAD_PLACED]
)
def test_placed_that_is_not_whole_is_refused_and_writes_nothing(world, clock, placed):
    """templet:3159 -- `placed` is `{effort, rule, why}`, three non-empty
    strings and nothing else, `rule` one of the order's `RULES`: anything else
    is refused before it writes."""
    work = item(world, effort="0001")
    task = world.file_task("agent", number=work, sandbox="process")
    fence = grant(world, work)
    head, dump = world.head(), world.dump()
    with pytest.raises(Refused, match="placed"):
        start(world, task, fence=fence, placed=placed)
    assert world.head() == head and world.dump() == dump
    assert world.get("tasks", task)["state"] == "open"


def test_the_rules_a_start_may_name_are_the_orders(tmp_path):
    """templet:3159 -- the record's list of rules is the order's: one list,
    so a rule the order names is a rule the record takes."""
    from quarrylith.order import sequence

    assert contract.PLACED_RULES == sequence.RULES
    assert "landing" in contract.PLACED_RULES


@over_backends
def test_check_reads_placed_in_the_journal(world, clock):
    """templet:3159 -- `check()` reads the `placed` of every `run.start`: a
    journal whose event carries one that is not whole, or a rule that is none
    of the rules, is a finding, and so is a `placed` that is not a dict."""
    work = item(world, effort="0001")
    task = world.file_task("agent", number=work, sandbox="process")
    start(world, task, fence=grant(world, work), placed=dict(PLACED, rule="floor"))
    assert world.check() == []
    good = world.dump()
    seq = start_event(world)["seq"]
    for what, placed in (
        ("a rule", dict(PLACED, rule="nope")),
        ("a key", {"effort": "0002", "rule": "floor"}),
        ("a list", ["floor"]),
        ("a None", None),
        ("an extra", dict(PLACED, extra=1)),
        ("an empty why", dict(PLACED, why="")),
    ):
        bad = json.loads(json.dumps(good))
        for event in bad["journal"]:
            if event["seq"] == seq:
                event["data"]["placed"] = placed
        found = contract._findings(bad)
        assert found and all(f"event {seq}" in f for f in found), what
        assert any("placed" in f for f in found), what


@over_backends
def test_a_record_written_before_placed_still_opens_and_replays(backend, record, clock):
    """templet:3159 -- a dump written before `placed` existed (the frozen
    `tests/data/record-before-replace.json`, whose one `run.start` carries
    none) loads, replays to its records, checks clean, gives a `landable`, and
    takes a start with `placed` afterwards."""
    old = json.loads((DATA / "record-before-replace.json").read_text("utf-8"))
    starts = [e for e in old["journal"] if e["kind"] == "run.start"]
    assert starts and all("placed" not in e["data"] for e in starts)
    record.load(old)
    assert record.dump() == old
    assert record.check() == []
    assert record.landable(now=old["journal"][-1]["at"]) == {
        "items": [],
        "tasks": [],
    }
    view = MemoryView()
    for raw in old["journal"]:
        apply(view, Event.from_dict(raw))
    assert view.records() == old["records"]
    clock.at = old["journal"][-1]["at"] + 1000
    work = record.file_item(
        "task", "t", "b", filed_by="alpha", role="alpha", effort="0001"
    )
    task = record.file_task("agent", number=work, sandbox="process")
    run = start(
        record,
        task,
        fence=grant(record, work),
        node=old["records"]["nodes"][0]["name"],
        placed={"effort": "0001", "rule": "floor", "why": "after the load"},
    )
    assert start_event(record)["data"]["run"] == run
    replay(record)
    assert record.check() == []


# -- a start that takes a landing review --------------------------------------


@over_backends
def test_a_landing_start_takes_a_parked_efforts_review(world, clock):
    """templet:3159 -- `start_run` with `placed["rule"]` `landing` starts a
    task `landable` gives: the run is made, the task is running, its needs
    are reserved, the event names the placement, and the journal replays."""
    made = review_of(world)
    assert numbers(landable(world, clock)) == ([made.review], [made.task])
    run = land(world, made)
    assert world.get("tasks", made.task)["state"] == "running"
    assert world.get("tasks", made.task)["attempts"] == 1
    assert world.get("runs", run)["task"] == made.task
    live = world.read("reservations", live=True, run=run)
    assert [(r["resource"], r["amount"]) for r in live] == [("slots", 1)]
    event = start_event(world)
    assert event["data"]["placed"] == PLACED and event["data"]["run"] == run
    assert numbers(landable(world, clock)) == ([], [])
    replay(world)
    assert world.check() == []


@over_backends
def test_a_landing_start_refuses_a_parked_efforts_review_without_it(world, clock):
    """templet:3159 -- the same review, started with no `placed` or a rule
    that is not `landing`, is the ready check as ever, and is refused: its
    effort is not admitted."""
    made = review_of(world)
    fence = grant(world, made.review)
    head = world.head()
    for placed in (None, dict(PLACED, rule="floor"), dict(PLACED, rule="resume")):
        with pytest.raises(Refused, match="no effort item .* is admitted"):
            start(world, made.task, fence=fence, placed=placed)
    assert world.head() == head and world.get("tasks", made.task)["state"] == "open"


@over_backends
def test_a_landing_start_refuses_what_is_not_a_review(world, clock):
    """templet:3159 -- a parked effort's task that is not a review (an agent
    task of a task item, a job of the effort, a resume with a live lease) is
    refused with `placed` rule `landing` as it is without."""
    work = item(world)
    change_of(world, work)
    agent = world.file_task("agent", number=work, sandbox="process")
    job = world.file_task("job", effort="0002", sandbox="process")
    fence = grant(world, work)
    head = world.head()
    for placed in (None, dict(PLACED)):
        with pytest.raises(Refused, match="no effort item .* is admitted|landable"):
            start(world, agent, fence=fence, placed=placed)
        with pytest.raises(Refused):
            start(world, job, placed=placed)
    assert world.head() == head
    assert world.read("runs") == []


@over_backends
def test_a_landing_start_refuses_a_review_of_nothing_to_land(world, clock):
    """templet:3159 -- a review whose reviewed item has no open change, and
    one that reviews nothing, are refused with the rule `landing`."""
    none = review_of(world, change=False)
    merged = review_of(world)
    world.record_merge(merged.change, "a1", "alpha", "pool-approved")
    nameless = item(world, "review")
    task = world.file_task("review", number=nameless, sandbox="process")
    for made in (none, merged):
        with pytest.raises(Refused, match="open change"):
            land(world, made)
    with pytest.raises(Refused, match="reviews nothing"):
        start(world, task, fence=grant(world, nameless), placed=dict(PLACED))
    assert world.read("runs") == []
    assert world.get("tasks", task)["state"] == "open"


@over_backends
def test_a_landing_start_refuses_a_task_with_no_item(world, clock):
    """templet:3159 -- a review task with no item reviews no item: it has no
    change to land, and is refused."""
    task = world.file_task("review", effort="0002", sandbox="process")
    with pytest.raises(Refused, match="no item"):
        start(world, task, placed=dict(PLACED))


@over_backends
def test_a_landing_start_takes_an_admitted_efforts_task_for_nothing(world, clock):
    """templet:3159 -- `landing` is a placement of what `landable` gives and
    nothing else: a ready task of an admitted effort, started with the rule
    `landing`, is refused, and started with another rule is not."""
    made = review_of(world, effort="0001")
    fence = grant(world, made.review)
    assert numbers(landable(world, clock)) == ([], [])
    with pytest.raises(Refused, match="landable"):
        start(world, made.task, fence=fence, placed=dict(PLACED))
    run = start(
        world,
        made.task,
        fence=fence,
        placed=dict(PLACED, effort="0001", rule="floor"),
    )
    assert world.get("runs", run)["task"] == made.task


@over_backends
def test_a_landing_start_is_decided_in_its_own_event(world, clock):
    """templet:3159 -- `landable` is read in the start's event, not before it:
    a review `landable` gave, whose change was merged, whose effort was
    admitted or whose task was started since, is refused, and a review not
    given before it was when the start is made is taken."""
    stale = review_of(world)
    admitting = review_of(world)
    taken = review_of(world)
    assert numbers(landable(world, clock))[1] == [
        stale.task,
        admitting.task,
        taken.task,
    ]
    fences = {m.task: grant(world, m.review) for m in (stale, admitting, taken)}
    world.record_merge(stale.change, "a1", "alpha", "pool-approved")
    with pytest.raises(Refused, match="open change"):
        start(world, stale.task, fence=fences[stale.task], placed=dict(PLACED))
    world.set_standing("0002", "admitted", "go")
    with pytest.raises(Refused, match="landable"):
        start(world, admitting.task, fence=fences[admitting.task], placed=dict(PLACED))
    world.set_standing("0002", "parked", "back")
    start(world, taken.task, fence=fences[taken.task], placed=dict(PLACED))
    with pytest.raises(Refused, match="running"):
        start(world, taken.task, fence=fences[taken.task], placed=dict(PLACED))
    world.set_standing("0002", "parked", "again")
    assert [r["task"] for r in world.read("runs")] == [taken.task]


@over_backends
def test_a_landing_start_keeps_the_lease_node_and_reservation_checks(world, clock):
    """templet:3159 -- `landing` replaces the ready check only: a start
    without the item's fence, with a stale fence, for a holder that does not
    hold the lease, on a node that does not run the sandbox, or past the
    node's room is refused as ever, and writes nothing."""
    made = review_of(world)
    fence = grant(world, made.review)
    world.edit_task(made.task, needs={"slots": 4})
    head, dump = world.head(), world.dump()
    with pytest.raises(Refused, match="fence"):
        start(world, made.task, placed=dict(PLACED))
    with pytest.raises(Refused, match="fence"):
        start(world, made.task, fence=fence + 1, placed=dict(PLACED))
    with pytest.raises(Refused, match="held by"):
        start(world, made.task, fence=fence, holder="beta", placed=dict(PLACED))
    with pytest.raises(Refused, match="does not run"):
        start(world, made.task, fence=fence, sandbox="vm", placed=dict(PLACED))
    with pytest.raises(Refused, match="no room"):
        start(world, made.task, fence=fence, placed=dict(PLACED))
    assert world.head() == head and world.dump() == dump
    world.edit_task(made.task, needs={"slots": 1})
    start(world, made.task, fence=fence, placed=dict(PLACED))


@over_backends
def test_a_landing_start_keeps_the_ready_reads_time_and_attempt_rules(world, clock):
    """templet:3159 -- a landing review's task keeps `not_before` and
    `loop.max_attempts`: before its time, or out of attempts, `landable` does
    not give it and a start with `landing` refuses it."""
    made = review_of(world)
    fence = grant(world, made.review)
    world.edit_task(made.task, not_before=clock() + 5_000)
    assert numbers(landable(world, clock))[1] == []
    with pytest.raises(Refused, match="not before"):
        start(world, made.task, fence=fence, placed=dict(PLACED))
    clock.step(5_000)
    run = start(world, made.task, fence=fence, placed=dict(PLACED))
    world.finish_run(run, "failed", "x", footprint={}, cost={})
    world.edit_task(made.task, loop={"max_attempts": 1})
    with pytest.raises(Refused, match="attempts"):
        start(world, made.task, fence=fence, placed=dict(PLACED))


@over_backends
def test_authorize_decides_who_starts_a_landing(world, clock):
    """templet:3159 -- who may start a landing is policy: `authorize` sees a
    landing start as the operation `start_run`, as it sees any, and its
    refusal ends it, writing nothing."""
    made = review_of(world)
    fence = grant(world, made.review)
    seen, refuse = [], []

    def policy(actor, name, before, after):
        seen.append(name)
        return refuse[0] if refuse else None

    world.authorize = policy
    refuse.append("a stranger starts no run")
    head, dump = world.head(), world.dump()
    with pytest.raises(Refused, match="a stranger starts no run"):
        start(world, made.task, fence=fence, placed=dict(PLACED))
    assert seen == ["start_run"]
    assert world.head() == head and world.dump() == dump
    refuse.clear()
    start(world, made.task, fence=fence, placed=dict(PLACED))
    assert seen == ["start_run", "start_run"]


@over_backends
def test_a_task_of_a_parked_effort_starts_only_as_landable_gives_it(world, clock):
    """templet:3159 -- invariant 9: over a matrix of tasks of parked,
    proposed, admitted and mixed efforts, reviews and not, with and without
    an open change, started with every rule and with none, a task starts with
    the rule `landing` exactly when `landable` gives it, and with another rule
    or none exactly when `ready` gives it."""
    world.file_effort("0003", "e3", "Three", "owner-a", PARTS, "proposed")
    cases = {}

    def make(name, kind, effort, change, also=()):
        work = item(world, effort=effort)
        if change:
            change_of(world, work)
        if kind == "review":
            review = item(world, "review", effort=effort)
            world.file_review(work, 1, "pool", item=review, author="alpha")
            work = review
        for other in also:
            world.link(other, "item", str(work))
        cases[name] = world.file_task(
            "review" if kind == "review" else "agent",
            number=work,
            needs={"slots": 1},
            sandbox="process",
            loop={"max_attempts": 99},
        )

    standings = (("0002", "parked"), ("0001", "admitted"), ("0003", "proposed"))
    for effort, label in standings:
        for kind in ("review", "agent"):
            for change in (True, False):
                make(f"{label}/{kind}/{change}", kind, effort, change)
    make("parked+admitted/review/True", "review", "0002", True, ("0001",))
    make("parked+proposed/review/True", "review", "0002", True, ("0003",))
    given = set(numbers(landable(world, clock))[1])
    ready = set(numbers(world.ready(now=clock()))[1])
    assert given and ready and not given & ready
    assert cases["parked/review/True"] in given
    assert cases["parked+proposed/review/True"] in given
    assert cases["parked+admitted/review/True"] in ready
    started = set()
    for name, task in sorted(cases.items()):
        number = world.get("tasks", task)["number"]
        fence = grant(world, number)
        for rule in (None, *contract.PLACED_RULES):
            placed = None if rule is None else dict(PLACED, rule=rule)
            expected = task in (given if rule == "landing" else ready)
            if expected:
                run = start(world, task, fence=fence, placed=placed)
                started.add(task)
                world.finish_run(run, "failed", "again", footprint={}, cost={})
            else:
                with pytest.raises(Refused):
                    start(world, task, fence=fence, placed=placed)
    assert started == given | ready
    assert {r["task"] for r in world.read("runs")} == given | ready
    for name in ("parked/review/False", "parked/agent/True", "proposed/review/True"):
        assert cases[name] not in started, name
    replay(world)
    assert world.check() == []
