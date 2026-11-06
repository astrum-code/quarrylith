# SPDX-License-Identifier: Apache-2.0
"""The landing tier (templet:3163, build step 5), over every backend in
BACKENDS: tier 4 of `order`, from the record's `landable`; `never` in `order`
with `Walk.never` and `Walk.only_never`; the walk's landing cap; and the
`start_run` half, over a real record.

Every fixture starts from landing reviews of a parked effort and admitted work
that either can or cannot start, and each test spoils exactly one fact, so a
rule that is missing shows as a test that fails. Times are UTC literals."""

from types import SimpleNamespace

import pytest
from backends import admitted, over_backends
from test_record_for_order import change_of
from test_sequence import NOON, effort, grant, item, job, node, run_of, task_of, tick
from test_walk import drive, refusal

from quarrylith.order.sequence import RULES, order, place
from quarrylith.order.settings import Settings
from quarrylith.order.walk import Walk
from quarrylith.record import contract
from quarrylith.record.contract import Refused


def landing_review(rec, effort="0002", role="r1", labels=(), **kw):
    """A review item of a parked effort, with its task, of a work item that
    has an open change: what the landing tier reviews."""
    target = item(rec, "work", effort=effort, role=role)
    review = rec.file_item(
        "review",
        "review",
        "body",
        role=role,
        effort=effort,
        filed_by="alpha",
        labels=list(labels),
    )
    rec.file_review(target, 1, "pool", item=review, author="alpha")
    change_of(rec, target)
    task = task_of(rec, review, "review", sandbox="process", **kw)
    return SimpleNamespace(target=target, review=review, task=task)


def land_run(rec, made):
    """A live landing run of `made`: started under the rule `landing`."""
    return rec.start_run(
        made.task,
        "alpha",
        node="n1",
        sandbox="process",
        fence=grant(rec, made.review),
        expect_s=60,
        placed={"effort": "0002", "rule": "landing", "why": "a live landing"},
    )


def world(rec, clock):
    """An admitted effort 0001, a parked 0002 and a node."""
    admitted(rec)
    effort(rec, "0002", standing="parked")
    node(rec)


def landing(sequence):
    return [c for c in sequence.candidates if c.rule == "landing"]


def settle(record, settings, now=NOON, **kw):
    return order(record, now=now, settings=settings, **kw)


# -- the tier ---------------------------------------------------------------


@over_backends
def test_the_landing_tier_opens_when_no_admitted_candidate_can_start(
    backend, record, clock
):
    """templet:3163 -- with tiers 1 to 3 holding nothing, a parked effort's
    review of an open change is a candidate: rule `landing`, tier 4, its
    parked effort named, its `why` the page's line; it is in `landable()`."""
    world(record, clock)
    made = landing_review(record)
    got = settle(record, Settings())
    (c,) = got.candidates
    assert (c.rule, c.tier, c.effort) == ("landing", 4, "0002")
    assert (c.task, c.item, c.key) == (made.task, made.review, ("task", made.task))
    assert c.why == (
        f"#{made.review} task {made.task}: effort 0002, landing -- "
        "review of a parked effort's change"
    )
    assert "landing" in RULES
    assert c.exempt == "review" and c.role == "r1"
    assert made.task in [t["id"] for t in record.landable(now=NOON)["tasks"]]


@over_backends
def test_a_landing_review_with_no_task_is_a_candidate_as_its_item(
    backend, record, clock
):
    """templet:3163 -- as any candidate, a review item with no open task is
    one as itself; with an open task, only as its task."""
    world(record, clock)
    bare = landing_review(record, role="r1")
    record.end_task(bare.task, "cancelled", "none")
    other = landing_review(record, role="r2")
    got = settle(record, Settings())
    assert [(c.item, c.task) for c in got.candidates] == [
        (bare.review, None),
        (other.review, other.task),
    ]


@over_backends
def test_a_landing_candidate_is_never_work_or_a_resume(backend, record, clock):
    """templet:3163 -- never a parked effort's resume or new work, nor a
    review whose reviewed item has no open change, nor a review of an
    effort that is not parked."""
    world(record, clock)
    effort(record, "0003", standing="proposed")
    fresh = item(record, "new work", effort="0002")
    task_of(record, fresh)
    resumed = item(record, "resumed", effort="0002")
    task_of(record, resumed)
    grant(record, resumed)
    job(record, effort="0002")
    unchanged = landing_review(record, role="r2")
    record.drop_change(
        record.read("changes", live=True, number=unchanged.target)[0]["id"],
        "alpha",
        "closed",
    )
    proposed = landing_review(record, effort="0003", role="r3")
    ok = landing_review(record, role="r4")
    got = settle(record, Settings())
    assert [c.item for c in got.candidates] == [ok.review]
    assert proposed.review not in [c.item for c in got.candidates]


@over_backends
def test_admitted_work_that_can_start_keeps_the_landing_tier_shut(
    backend, record, clock
):
    """templet:3163 -- a candidate of tiers 1 to 3 that passes the cuts means
    no landing candidate, whatever its tier."""
    world(record, clock)
    landing_review(record)
    work = item(record, "work", role="a")
    got = settle(record, Settings())
    assert [c.item for c in got.candidates] == [work]
    review = record.file_item(
        "review", "rv", "b", role="b", effort="0001", filed_by="alpha"
    )
    got = settle(record, Settings())
    assert [c.tier for c in got.candidates] == [1, 3]
    assert not landing(got)
    task_of(record, work)
    grant(record, work)
    got = settle(record, Settings())
    assert [c.tier for c in got.candidates] == [1, 2]
    assert review in [c.item for c in got.candidates]


@over_backends
def test_a_candidate_cut_by_the_role_opens_the_landing_tier(backend, record, clock):
    """templet:3163 -- tiers 1 to 3 hold no candidate that passes the walk's
    cuts when each is cut: here by its role's instances."""
    world(record, clock)
    running = item(record, "running", role="a")
    run_of(record, task_of(record, running), fence=grant(record, running))
    cut = item(record, "cut", role="a")
    made = landing_review(record, role="b")
    got = settle(record, Settings())
    assert [(c.item, c.tier) for c in got.candidates] == [
        (cut, 3),
        (made.review, 4),
    ]
    free = settle(record, Settings(instances={"a": 2}))
    assert [c.item for c in free.candidates] == [cut]


@over_backends
def test_a_candidate_cut_by_the_landing_limit_opens_the_landing_tier(
    backend, record, clock
):
    """templet:3163 -- cut by the landing limit too: new work in a repo at its
    limit does not shut the tier, and an exempt review does."""
    world(record, clock)
    made = landing_review(record, role="b")
    full = item(record, "full", role="a", labels=["repo:widgets"])
    got = settle(record, Settings(landing_limit=1))
    assert [(c.item, c.tier) for c in got.candidates] == [
        (full, 3),
        (made.review, 4),
    ]
    assert [c.item for c in settle(record, Settings(landing_limit=0)).candidates] == [
        full
    ]
    review = record.file_item(
        "review",
        "rv",
        "b",
        role="c",
        effort="0001",
        filed_by="alpha",
        labels=["repo:widgets"],
    )
    got = settle(record, Settings(landing_limit=1))
    assert [c.item for c in got.candidates] == [review, full]


@over_backends
def test_landing_candidates_sort_as_the_effort_order_does(backend, record, clock):
    """templet:3163 -- the landing candidates come after every other, by the
    parked efforts' rank and id, then the effort's rule: here a rank puts the
    higher id first, and within an effort the unblocking review leads."""
    admitted(record)
    node(record)
    effort(record, "0002", standing="parked", rank=2)
    effort(record, "0003", standing="parked", rank=1)
    effort(record, "0004", standing="parked")
    unranked = landing_review(record, effort="0004", role="r1")
    low = landing_review(record, effort="0002", role="r2")
    tick(clock)
    first = landing_review(record, effort="0003", role="r3")
    older = landing_review(record, effort="0003", role="r4")
    record.add_dep(item(record, "waits", effort="0003"), blocker=older.target, why="w")
    got = settle(record, Settings(), now=clock())
    assert [c.item for c in got.candidates] == [
        older.review,
        first.review,
        low.review,
        unranked.review,
    ]
    assert {c.effort for c in got.candidates} == {"0002", "0003", "0004"}


@over_backends
def test_landing_runs_leave_the_floor_whole(backend, record, clock):
    """templet:3163 -- live landing runs never take the floor from admitted
    reviews: with `review_slots` 2 and a landing run live, two admitted
    reviews are both floor; an admitted review running takes one."""
    world(record, clock)
    live = landing_review(record, role="r9")
    land_run(record, live)
    for name in ("a", "b", "c"):
        record.file_item(
            "review", name, "b", role=name, effort="0001", filed_by="alpha"
        )
    got = settle(record, Settings(review_slots=2))
    assert got.counts["landing"] == 1 and got.counts["reviews"] == 0
    assert [c.tier for c in got.candidates] == [1, 1, 3]


# -- the walk's cap ---------------------------------------------------------


@over_backends
def test_at_most_review_slots_landing_runs_at_once(backend, record, clock):
    """templet:3163 -- the walk's rule 3: a landing candidate is cut while
    live landing runs plus this walk's landing starts reach `review_slots`,
    after the role cut and the landing limit and before a hold."""
    world(record, clock)
    live = landing_review(record, role="r0")
    land_run(record, live)
    made = [landing_review(record, role=f"r{n}") for n in (1, 2, 3)]
    settings = Settings(review_slots=2)
    sequence = settle(record, settings)
    assert sequence.counts["landing"] == 1
    walk = Walk(sequence, settings=settings)
    started = drive(walk, sequence)
    assert started == [made[0].review]
    why = [w for c, w in walk.left if c.item == made[1].review]
    assert why == ["landing reviews: 2 of 2 busy"]
    assert [c.item for c, _ in walk.left] == [made[1].review, made[2].review]
    # a larger cap lets one more start; the cap is read from the settings
    wider = Settings(review_slots=3)
    again = Walk(settle(record, wider), settings=wider)
    assert len(drive(again, settle(record, wider))) == 2


@over_backends
def test_the_cap_counts_only_landing_candidates(backend, record, clock):
    """templet:3163 -- live landing runs cut a landing candidate and nothing
    else: admitted work still starts at the cap."""
    world(record, clock)
    for n in (0, 1):
        live = landing_review(record, role=f"r{n}")
        land_run(record, live)
    made = landing_review(record, role="r5")
    work = item(record, "work", role="a")
    settings = Settings(review_slots=2)
    sequence = settle(record, settings)
    assert [c.rule for c in sequence.candidates] == ["unblocks-then-age"]
    walk = Walk(sequence, settings=settings)
    assert walk.check(sequence.candidates[0]) is None
    # the same walk over a sequence whose admitted work is cut: the landing
    # candidate meets the cap
    record.hold(number=work, cause="user", why="wait")
    sequence = settle(record, settings)
    (c,) = sequence.candidates
    assert (c.rule, c.item) == ("landing", made.review)
    assert Walk(sequence, settings=settings).check(c) == (
        "landing reviews: 2 of 2 busy"
    )
    assert Walk(sequence, settings=Settings(review_slots=3)).check(c) is None


# -- never ------------------------------------------------------------------


def two_refusable(record, clock, **kw):
    admitted(record)
    node(record)
    effort(record, "0002", standing="parked")
    a = item(record, "a", role="r1")
    tick(clock)
    b = item(record, "b", role="r2")
    return a, b


@over_backends
def test_a_never_refusal_is_in_walk_never_and_only_never_is_true(
    backend, record, clock
):
    """templet:3163 -- `walk.never` maps a candidate's key to the refusal's
    text for each candidate refused `never`; when nothing started and every
    candidate that passed rules 1 to 3 was refused `never`, `only_never` is
    true."""
    a, b = two_refusable(record, clock)
    settings = Settings()
    sequence = settle(record, settings)
    walk = Walk(sequence, settings=settings)
    assert walk.never == {} and walk.only_never is False
    never = refusal("memory", waits=False, text="never fits: no node")
    assert drive(walk, sequence, {a: never, b: never}) == []
    assert walk.never == {
        ("item", a): "never fits: no node",
        ("item", b): "never fits: no node",
    }
    assert walk.only_never is True


@over_backends
@pytest.mark.parametrize(
    "answers",
    [
        {"a": "wait"},
        {"a": "never"},
        {"a": "never", "b": "wait"},
        {},
    ],
    ids=["one waits", "one started", "never and waits", "none refused"],
)
def test_only_never_needs_every_passing_candidate_refused_never(
    backend, record, clock, answers
):
    """templet:3163 -- `only_never` is false when something started, when a
    candidate that passed was refused with `waits` true, when it was refused
    for a while, or when no candidate was refused at all."""
    a, b = two_refusable(record, clock)
    settings = Settings()
    sequence = settle(record, settings)
    walk = Walk(sequence, settings=settings)
    named = {"a": a, "b": b}
    refusals = {
        named[k]: refusal("memory", waits=(v == "wait"), text=f"{v} refusal")
        for k, v in answers.items()
    }
    drive(walk, sequence, refusals)
    assert walk.only_never is False


@over_backends
def test_only_never_is_false_when_nothing_passed_the_cuts(backend, record, clock):
    """templet:3163 -- no candidate passed rules 1 to 3 (an empty sequence;
    every candidate cut by its role) is not "only never"."""
    admitted(record)
    node(record)
    empty = Walk(settle(record, Settings()), settings=Settings())
    assert empty.only_never is False
    running = item(record, "running", role="a")
    run_of(record, task_of(record, running), fence=grant(record, running))
    item(record, "cut", role="a")
    sequence = settle(record, Settings())
    walk = Walk(sequence, settings=Settings())
    assert drive(walk, sequence) == []
    assert walk.only_never is False


@over_backends
def test_a_candidate_held_ahead_still_passed_rules_one_to_three(backend, record, clock):
    """templet:3163 -- "passed rules 1 to 3" is before the hold: a candidate
    held behind a refused one has passed, and was not refused `never`."""
    a, b = two_refusable(record, clock)
    settings = Settings(hold_after_s=0)
    sequence = settle(record, settings)
    walk = Walk(sequence, settings=settings)
    first, second = sequence.candidates
    assert walk.check(first, {"memory": 1}) is None
    walk.refused(first, refusal("memory", text="short"))
    assert "held behind" in walk.check(second, {"memory": 1})
    assert walk.only_never is False
    assert walk.never == {}


@over_backends
def test_order_with_never_leaves_those_candidates_out_and_opens_the_tier(
    backend, record, clock
):
    """templet:3163 -- `order(..., never=walk.never)` leaves each candidate
    out, `Left` "never fits: " and the text, so tiers 1 to 3 hold none that
    passes and the landing candidates are held."""
    a, b = two_refusable(record, clock)
    made = landing_review(record, role="r3")
    settings = Settings()
    first = settle(record, settings)
    assert [c.item for c in first.candidates] == [a, b]
    walk = Walk(first, settings=settings)
    never = refusal("memory", waits=False, text="no node has the label")
    drive(walk, first, {a: never, b: never})
    assert walk.only_never

    second = settle(record, settings, never=walk.never)
    assert [(c.item, c.rule) for c in second.candidates] == [(made.review, "landing")]
    reasons = {(x.item, x.task): x.why for x in second.left}
    assert reasons[(a, None)] == f"item {a}: never fits: no node has the label"
    assert reasons[(b, None)] == f"item {b}: never fits: no node has the label"
    again = Walk(second, settings=settings, waiting=walk.waiting)
    assert drive(again, second) == [made.review]
    # `never` is not kept: the next round, given none, holds them again
    assert [c.item for c in settle(record, settings).candidates] == [a, b]


@over_backends
def test_never_names_a_task_by_its_key(backend, record, clock):
    """templet:3163 -- a candidate that is a task has the key ("task", id)
    and its `Left` names the task."""
    admitted(record)
    node(record)
    n = item(record, "work", role="a")
    task = task_of(record, n)
    got = settle(record, Settings(), never={("task", task): "no node"})
    assert got.candidates == ()
    assert [x.why for x in got.left] == [f"task {task}: never fits: no node"]
    # a key that is no candidate changes nothing
    kept = settle(record, Settings(), never={("task", 9999): "no node"})
    assert [c.task for c in kept.candidates] == [task]


@over_backends
def test_never_is_a_map_or_it_is_refused(backend, record, clock):
    """templet:3163 -- `never` is `{candidate key: text}`; anything else is
    refused, and None leaves nothing out."""
    admitted(record)
    for bad in ([("item", 1)], "x", {1: "x"}, {("item", 1): 2}, {("item",): "x"}):
        with pytest.raises(Refused, match="never"):
            settle(record, Settings(), never=bad)
    assert settle(record, Settings(), never={}) == settle(record, Settings())


@over_backends
def test_a_landing_candidate_refused_never_is_left_out_next_round(
    backend, record, clock
):
    """templet:3163 -- `never` leaves out a landing candidate as any other."""
    world(record, clock)
    bad = landing_review(record, role="r1")
    good = landing_review(record, role="r2")
    got = settle(record, Settings(), never={("task", bad.task): "no node"})
    assert [c.item for c in got.candidates] == [good.review]
    assert f"task {bad.task}: never fits: no node" in [x.why for x in got.left]


# -- never widens, place and the schedule -------------------------------------


@over_backends
def test_every_landing_candidate_is_in_landable_and_tiers_one_to_three_unchanged(
    backend, record, clock
):
    """templet:3163 -- every landing candidate is in `landable()` and none is
    in `ready()`; with the tier shut the sequence is exactly the sequence
    there was."""
    world(record, clock)
    made = [landing_review(record, role=f"r{n}") for n in range(3)]
    sequence = settle(record, Settings())
    landable = record.landable(now=NOON)
    ids = {t["id"] for t in landable["tasks"]}
    ready = record.ready(now=NOON)
    assert landing(sequence)
    for c in sequence.candidates:
        assert c.task in ids and c.task not in {t["id"] for t in ready["tasks"]}
    item(record, "admitted work", role="a")
    shut = settle(record, Settings())
    assert [c.rule for c in shut.candidates] == ["unblocks-then-age"]
    assert shut.counts == sequence.counts
    assert len(made) == 3


@over_backends
def test_the_landing_tier_keeps_schedules_flags_and_the_role_cut(
    backend, record, clock
):
    """templet:3163 -- a landing review follows the rules of every candidate:
    a night flag outside the window leaves it out; its role is cut by its
    instances."""
    world(record, clock)
    night = landing_review(record, role="r1", flags=["night"])
    same = landing_review(record, role="r2")
    other = landing_review(record, role="r2")
    settings = Settings()
    got = settle(record, settings)
    assert [c.item for c in got.candidates] == [same.review, other.review]
    assert any(x.task == night.task and "night" in x.why for x in got.left), got.left
    walk = Walk(got, settings=settings)
    assert drive(walk, got) == [same.review]


@over_backends
def test_place_gives_a_landing_review_what_the_order_gives(backend, record, clock):
    """templet:3163 -- `place` gives a landing review its tier, rule and
    effort, and while admitted work can start, a `Left` that says why."""
    world(record, clock)
    made = landing_review(record)
    got = settle(record, Settings())
    (c,) = got.candidates
    assert place(record, now=NOON, settings=Settings(), task=made.task) == c
    assert place(record, now=NOON, settings=Settings(), item=made.review) == c
    item(record, "admitted work", role="a")
    shut = place(record, now=NOON, settings=Settings(), task=made.task)
    assert shut.task == made.task and "landing" in shut.why
    assert "admitted work" in shut.why
    shut = place(record, now=NOON, settings=Settings(), item=made.review)
    assert shut.item == made.review and "landing" in shut.why


# -- the start ----------------------------------------------------------------


@over_backends
def test_a_landing_placed_by_the_order_starts_under_its_placement(
    backend, record, clock
):
    """templet:3163 -- the half over the record: the landing candidate the
    order names starts under `start_run(..., placed=)` with its effort, rule
    and `why`, which the event carries; a parked effort's non-review task is
    still refused with the rule `landing` and without."""
    world(record, clock)
    made = landing_review(record)
    work = item(record, "parked work", effort="0002")
    plain = task_of(record, work, sandbox="process")
    fence_work = grant(record, work)
    sequence = settle(record, Settings())
    (c,) = sequence.candidates
    placed = {"effort": c.effort, "rule": c.rule, "why": c.why}
    assert c.rule in contract.PLACED_RULES
    fence = grant(record, c.item)
    run = record.start_run(
        c.task,
        "alpha",
        node="n1",
        sandbox="process",
        fence=fence,
        expect_s=60,
        placed=placed,
    )
    event = [e for e in record.dump()["journal"] if e["kind"] == "run.start"][-1]
    assert event["data"]["placed"] == placed and event["data"]["run"] == run
    for given in (placed, None):
        with pytest.raises(Refused):
            record.start_run(
                plain,
                "alpha",
                node="n1",
                sandbox="process",
                fence=fence_work,
                expect_s=60,
                placed=given,
            )
    assert made.task == c.task
    assert record.check() == []
    # and the next sequence counts the live landing run
    assert settle(record, Settings()).counts["landing"] == 1
