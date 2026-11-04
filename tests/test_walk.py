# SPDX-License-Identifier: Apache-2.0
"""The walk (templet:3163, build step 3), over every backend in BACKENDS: the
per-role cut, the landing limit and its count, and the holds behind a refused
candidate. The walk reads the allocator's refusal by attribute name, so these
tests give it plain objects with those four attributes and nothing else.

Each ordering test builds its fixture so that every other order of the keys it
checks gives a different answer. Times are UTC literals, never read from the
module under test."""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
from backends import admitted, over_backends
from test_sequence import NOON, grant, item, job, node, run_of, task_of, tick, utc

from quarrylith.order.sequence import order
from quarrylith.order.settings import Settings
from quarrylith.order.walk import Walk

SRC = Path(__file__).resolve().parent.parent / "src" / "quarrylith" / "order"
MINUTE = 60_000
HOUR = 60 * MINUTE


# -- fixtures -------------------------------------------------------------


def refusal(*resources, waits=True, until=None, text="no room"):
    """What the walk reads of the allocator's refusal, and nothing more."""
    shorts = tuple(SimpleNamespace(resource=r) for r in resources)
    return SimpleNamespace(text=text, waits=waits, until=until, shorts=shorts)


def sequence_at(record, settings, now=NOON):
    return order(record, now=now, settings=settings)


def listed(record, clock, count):
    """`count` ready items of effort 0001, each its own role, in the order
    the effort lists them (newest first, so the list and the age disagree).
    Returns their numbers, in the list's order."""
    admitted(record)
    numbers = []
    for n in range(count):
        tick(clock)
        numbers.append(item(record, f"c{n}", role=f"r{n}"))
    numbers.reverse()
    record.edit_effort("0001", order={"rule": "listed", "items": numbers})
    return numbers


NEEDS = {"memory": 1, "slots": 1}


def drive(walk, sequence, answers=None, needs=None):
    """The services' loop, as the page gives it. `answers` maps an item
    number to a refusal; any other candidate fits. `needs` maps an item
    number to the needs `check` is given (default: memory and a slot).
    Returns what started: an item's number, else `task N`."""
    answers, needs = answers or {}, needs or {}
    started = []
    for c in sequence.candidates:
        if walk.check(c, needs.get(c.item, NEEDS)):
            continue
        if c.item in answers:
            walk.refused(c, answers[c.item])
        else:
            walk.started(c)
            started.append(c.item if c.item is not None else f"task {c.task}")
    return started


def why_of(walk, number):
    return next(why for c, why in walk.left if c.item == number)


def left_of(walk):
    return [c.item for c, _ in walk.left]


# -- the order before the cut ---------------------------------------------


@over_backends
def test_the_order_is_applied_before_the_per_role_cut(backend, record, clock):
    """templet:3163 -- role alpha has one instance, an old item that unblocks
    nothing and a newer one that effort 0001 lists first: the listed item is
    the one the walk lets start, and the old one is cut once it has."""
    admitted(record)
    old = item(record, "old", role="alpha")
    tick(clock)
    other = item(record, "other role", role="beta")
    tick(clock)
    newer = item(record, "newer, listed first", role="alpha")
    record.edit_effort("0001", order={"rule": "listed", "items": [newer]})
    sequence = sequence_at(record, Settings())
    assert [c.item for c in sequence.candidates] == [newer, old, other]

    walk = Walk(sequence, settings=Settings())
    started = drive(walk, sequence)

    assert started == [newer, other]
    assert why_of(walk, old) == "role alpha: 1 of 1 busy"
    assert left_of(walk) == [old]


@over_backends
def test_the_cut_counts_live_runs_and_the_instances_of_a_role(backend, record, clock):
    """templet:3163 -- a role has as many live runs, plus the starts of this
    walk, as `instances` gives it; a role it does not name has 1; a candidate
    with no role is never cut."""
    admitted(record)
    node(record)
    busy = item(record, "running", role="alpha")
    run_of(record, task_of(record, busy), fence=grant(record, busy))
    first = item(record, "a1", role="alpha")
    tick(clock)
    second = item(record, "a2", role="alpha")
    tick(clock)
    third = item(record, "a3", role="alpha")
    tick(clock)
    lone = item(record, "b1", role="beta")
    tick(clock)
    nobody = job(record)
    tick(clock)
    nobody_too = job(record)
    settings = Settings(instances={"alpha": 3})
    sequence = sequence_at(record, settings)
    assert sequence.counts["roles"] == {"alpha": 1}

    walk = Walk(sequence, settings=settings)
    started = drive(walk, sequence)

    assert started == [first, second, lone, f"task {nobody}", f"task {nobody_too}"]
    assert why_of(walk, third) == "role alpha: 3 of 3 busy"

    again = Walk(sequence, settings=Settings())
    assert drive(again, sequence) == [lone, f"task {nobody}", f"task {nobody_too}"]
    assert why_of(again, first) == "role alpha: 1 of 1 busy"


# -- the landing limit ----------------------------------------------------


def changes(record, repo, count, prefix="feat/c"):
    for n in range(count):
        record.open_or_update_change(repo, f"{prefix}{n}", "h", title="t", by="alpha")


def running(record, repo, role):
    """A live run on a new item in `repo` with no change there yet."""
    node(record)
    n = item(record, f"running in {repo}", role=role, labels=[f"repo:{repo}"])
    run_of(record, task_of(record, n), fence=grant(record, n))
    return n


@over_backends
def test_the_landing_limit_cuts_new_work_at_the_limit(backend, record, clock):
    """templet:3163 -- a repo with 6 unmerged changes (5 open and a live run
    with none yet) cuts new work there, naming the repo and the counts; a
    review, a `resync` task and a post-review resume pass; a resume whose item
    has an open change in that repo passes."""
    admitted(record)
    changes(record, "widgets", 4)
    running(record, "widgets", "r0")
    own_change = item(record, "has its change", role="r1", labels=["repo:widgets"])
    task_of(record, own_change)
    grant(record, own_change)
    record.open_or_update_change(
        "widgets", "feat/own", "h", title="t", number=own_change, by="alpha"
    )
    tick(clock)
    new = item(record, "new work", role="r2", labels=["repo:widgets"])
    tick(clock)
    elsewhere = item(record, "other repo", role="r3", labels=["repo:gadgets"])
    tick(clock)
    unlabelled = item(record, "no repo", role="r4")
    tick(clock)
    review = record.file_item(
        "review",
        "rv",
        "body",
        role="r5",
        effort="0001",
        filed_by="alpha",
        labels=["repo:widgets"],
    )
    tick(clock)
    resync = job(record, flags=["resync"], labels=["repo:widgets"])
    tick(clock)
    sent_back = item(record, "sent back", role="r6", labels=["repo:widgets"])
    sent_task = task_of(record, sent_back)
    grant(record, sent_back)
    record.edit_item(sent_back, review_round=1)
    verdict = record.file_review(sent_back, 1, "pool", author="alpha")
    record.record_verdict(verdict, "beta", "changes", [])
    settings = Settings(landing_limit=6)
    sequence = sequence_at(record, settings)
    assert sequence.counts["repos"] == {"widgets": 6}

    walk = Walk(sequence, settings=settings)
    started = drive(walk, sequence)

    assert why_of(walk, new) == "repo widgets: 6 unmerged of 6"
    assert left_of(walk) == [new]
    assert sorted(started, key=str) == sorted(
        [own_change, elsewhere, unlabelled, review, f"task {resync}", sent_back],
        key=str,
    )
    exempt = {c.key: c.exempt for c in sequence.candidates}
    assert exempt[("item", review)] == "review"
    assert exempt[("task", resync)] == "resync"
    assert exempt[("task", sent_task)] == "post-review"
    assert exempt[("item", new)] is None


@over_backends
def test_an_exempt_start_is_not_counted_against_the_limit(backend, record, clock):
    """templet:3163 -- a start that is exempt from the landing limit adds to
    no repo's count, as a live run of it would not: the next candidate of the
    repo still starts."""
    admitted(record)
    changes(record, "widgets", 1)
    review = record.file_item(
        "review",
        "rv",
        "body",
        role="r1",
        effort="0001",
        filed_by="alpha",
        labels=["repo:widgets"],
    )
    tick(clock)
    resync = job(record, flags=["resync"], labels=["repo:widgets"])
    tick(clock)
    new = item(record, "new work", role="r2", labels=["repo:widgets"])
    tick(clock)
    more = item(record, "more new work", role="r3", labels=["repo:widgets"])
    settings = Settings(landing_limit=2)
    sequence = sequence_at(record, settings)
    walk = Walk(sequence, settings=settings)

    started = drive(walk, sequence)

    assert started == [review, f"task {resync}", new]
    assert why_of(walk, more) == "repo widgets: 2 unmerged of 2"


@over_backends
def test_the_landing_limit_is_the_unmerged_changes_plus_work_in_flight(
    backend, record, clock
):
    """templet:3163 -- 5 open changes alone do not reach a limit of 6; the
    live run with no change yet is the sixth; a dropped change is not
    counted."""
    admitted(record)
    changes(record, "widgets", 5)
    done = record.open_or_update_change(
        "widgets", "feat/done", "h", title="t", by="alpha"
    )
    record.drop_change(done, "alpha", "stale")
    new = item(record, "new work", role="r1", labels=["repo:widgets"])
    settings = Settings(landing_limit=6)

    sequence = sequence_at(record, settings)
    assert sequence.counts["repos"] == {"widgets": 5}
    walk = Walk(sequence, settings=settings)
    assert drive(walk, sequence) == [new]
    assert walk.left == []

    running(record, "widgets", "r0")
    sequence = sequence_at(record, settings)
    assert sequence.counts["repos"] == {"widgets": 6}
    walk = Walk(sequence, settings=settings)
    assert drive(walk, sequence) == []
    assert why_of(walk, new) == "repo widgets: 6 unmerged of 6"


@over_backends
def test_an_item_in_two_repos_is_cut_by_either_and_counts_in_each(
    backend, record, clock
):
    """templet:3163 -- an item in two repos is cut when either is at its
    limit and counts one in each when it starts; a limit of 0 cuts nothing;
    the next sequence reads a changed limit."""
    admitted(record)
    changes(record, "widgets", 1, "feat/w")
    changes(record, "gadgets", 1, "feat/g")
    both = item(record, "both", role="r1", labels=["repo:widgets", "repo:gadgets"])
    tick(clock)
    wide = item(record, "widgets only", role="r2", labels=["repo:widgets"])
    tick(clock)
    gad = item(record, "gadgets only", role="r3", labels=["repo:gadgets"])
    settings = Settings(landing_limit=2)
    sequence = sequence_at(record, settings)
    walk = Walk(sequence, settings=settings)
    assert drive(walk, sequence) == [both]
    assert why_of(walk, wide) == "repo widgets: 2 unmerged of 2"
    assert why_of(walk, gad) == "repo gadgets: 2 unmerged of 2"

    # either repo at its limit cuts it, and the line names that repo only
    changes(record, "widgets", 1, "feat/w2")
    sequence = sequence_at(record, settings)
    walk = Walk(sequence, settings=settings)
    assert drive(walk, sequence) == [gad]
    assert why_of(walk, both) == "repo widgets: 2 unmerged of 2"
    assert why_of(walk, wide) == "repo widgets: 2 unmerged of 2"

    # no limit: nothing is cut for a repo
    free = Settings(landing_limit=0)
    sequence = sequence_at(record, free)
    walk = Walk(sequence, settings=free)
    assert drive(walk, sequence) == [both, wide, gad]
    assert walk.left == []

    # the next sequence reads the limit it is given: widgets 2, gadgets 1
    # take `both` (3 and 2), then cut `wide` (3 of 3) and take `gad` (3)
    three = Settings(landing_limit=3)
    sequence = sequence_at(record, three)
    walk = Walk(sequence, settings=three)
    assert drive(walk, sequence) == [both, gad]
    assert why_of(walk, wide) == "repo widgets: 3 unmerged of 3"

    # both at their limit: the line names both
    changes(record, "gadgets", 1, "feat/g2")
    sequence = sequence_at(record, settings)
    walk = Walk(sequence, settings=settings)
    assert drive(walk, sequence) == []
    assert why_of(walk, both) == (
        "repo gadgets: 2 unmerged of 2; repo widgets: 2 unmerged of 2"
    )


# -- passing, and the holds -----------------------------------------------


@over_backends
def test_a_refused_candidate_is_passed_by_a_later_one_that_fits(backend, record, clock):
    """templet:3163 -- a refused candidate is passed, until it has waited
    `hold_after_s`; its wait starts at the sequence's `at`."""
    first, second, third = listed(record, clock, 3)
    settings = Settings()
    sequence = sequence_at(record, settings)
    walk = Walk(sequence, settings=settings)

    started = drive(walk, sequence, {first: refusal("memory")})

    assert started == [second, third]
    assert walk.waiting == {("item", first): NOON}
    assert why_of(walk, first) == "no room"


@over_backends
def test_past_the_wait_a_candidate_holds_what_needs_its_resource(
    backend, record, clock
):
    """templet:3163 -- past `hold_after_s` a later candidate that needs the
    resource the refused one was short of is held, naming it; one needing
    other resources passes."""
    a, b, c, d = listed(record, clock, 4)
    settings = Settings(hold_after_s=30 * 60)
    since = NOON - 40 * MINUTE
    sequence = sequence_at(record, settings)
    walk = Walk(sequence, settings=settings, waiting={("item", a): since})
    needs = {b: {"memory": 1}, c: {"slots": 1}, d: {"memory": 1, "slots": 1}}

    started = drive(walk, sequence, {a: refusal("memory")}, needs)

    assert started == [c]
    assert why_of(walk, b) == f"held behind #{a}: memory short since 11:20"
    assert why_of(walk, d) == f"held behind #{a}: memory short since 11:20"
    assert walk.waiting == {("item", a): since}


@over_backends
def test_the_hold_begins_at_the_wait_and_not_before(backend, record, clock):
    """templet:3163 -- a candidate that has waited exactly `hold_after_s`
    holds, one millisecond short of it passes; a `hold_after_s` of 0 holds at
    once."""
    a, b = listed(record, clock, 2)
    settings = Settings(hold_after_s=30 * 60)
    sequence = sequence_at(record, settings)

    on = Walk(sequence, settings=settings, waiting={("item", a): NOON - 30 * MINUTE})
    assert drive(on, sequence, {a: refusal("memory")}) == []

    short = Walk(
        sequence, settings=settings, waiting={("item", a): NOON - 30 * MINUTE + 1}
    )
    assert drive(short, sequence, {a: refusal("memory")}) == [b]

    quick = Settings(hold_after_s=0)
    sequence = sequence_at(record, quick)
    now = Walk(sequence, settings=quick)
    assert drive(now, sequence, {a: refusal("memory")}) == []
    assert why_of(now, b) == f"held behind #{a}: memory short since 12:00"


@over_backends
def test_a_short_with_no_resource_holds_every_later_candidate(backend, record, clock):
    """templet:3163 -- a refusal with a short whose `resource` is None (it is
    short of every resource) holds every later candidate, whatever it needs,
    and one whose `needs` is None."""
    a, b, c, d = listed(record, clock, 4)
    settings = Settings()
    sequence = sequence_at(record, settings)
    walk = Walk(sequence, settings=settings, waiting={("item", a): NOON - HOUR})
    needs = {b: {"slots": 1}, c: {}, d: None}

    started = drive(walk, sequence, {a: refusal("memory", None)}, needs)

    assert started == []
    for n in (b, c, d):
        assert why_of(walk, n) == f"held behind #{a}: every resource short since 11:00"


@over_backends
def test_a_candidate_without_needs_waits_behind_any_hold(backend, record, clock):
    """templet:3163 -- `needs` None (a caller that skipped `place`) is held
    behind any hold, and passes when nothing is held."""
    a, b = listed(record, clock, 2)
    settings = Settings()
    sequence = sequence_at(record, settings)
    first, second = sequence.candidates

    free = Walk(sequence, settings=settings)
    assert free.check(first, None) is None
    free.refused(first, refusal("memory"))
    assert free.check(second, None) is None

    held = Walk(sequence, settings=settings, waiting={("item", a): NOON - HOUR})
    assert held.check(first, None) is None
    held.refused(first, refusal("memory"))
    assert held.check(second, None) == f"held behind #{a}: memory short since 11:00"


@over_backends
def test_a_hold_with_an_until_names_it(backend, record, clock):
    """templet:3163 -- a refusal with `until` set names it in the hold's why;
    on another day the date comes with it, and so does the date of the wait."""
    a, b = listed(record, clock, 2)
    settings = Settings()
    sequence = sequence_at(record, settings)
    since = {("item", a): NOON - HOUR}
    needs = {b: {"budget": 1}}

    walk = Walk(sequence, settings=settings, waiting=dict(since))
    drive(walk, sequence, {a: refusal("budget", until=utc(2023, 11, 15, 18))}, needs)
    assert why_of(walk, b) == f"held behind #{a}: budget until 18:00"

    walk = Walk(sequence, settings=settings, waiting=dict(since))
    drive(walk, sequence, {a: refusal("budget", until=utc(2023, 11, 16, 2))}, needs)
    assert why_of(walk, b) == f"held behind #{a}: budget until 2023-11-16 02:00"

    day = {("item", a): NOON - 14 * HOUR}
    walk = Walk(sequence, settings=settings, waiting=day)
    drive(walk, sequence, {a: refusal("memory")})
    assert why_of(walk, b) == f"held behind #{a}: memory short since 2023-11-14 22:00"


@over_backends
def test_a_task_without_an_item_is_named_by_its_task(backend, record, clock):
    """templet:3163 -- a hold names a candidate with no item as `task N`."""
    admitted(record)
    first = job(record)
    tick(clock)
    second = job(record)
    settings = Settings()
    sequence = sequence_at(record, settings)
    a, b = sequence.candidates
    assert (a.task, b.task) == (first, second)
    walk = Walk(sequence, settings=settings, waiting={a.key: NOON - HOUR})

    assert walk.check(a, {"memory": 1}) is None
    walk.refused(a, refusal("memory"))

    assert walk.check(b, {"memory": 1}) == (
        f"held behind task {first}: memory short since 11:00"
    )


@over_backends
def test_a_never_refusal_holds_nothing(backend, record, clock):
    """templet:3163 -- a refusal with `waits` false (it can never fit) is
    left with its text, holds nothing however long ago the candidate was
    first refused, and starts no wait."""
    a, b = listed(record, clock, 2)
    settings = Settings()
    sequence = sequence_at(record, settings)
    never = refusal("memory", waits=False, text="never fits: no node has the label")

    walk = Walk(sequence, settings=settings)
    assert drive(walk, sequence, {a: never}) == [b]
    assert why_of(walk, a) == "never fits: no node has the label"
    assert walk.waiting == {}

    late = Walk(sequence, settings=settings, waiting={("item", a): NOON - HOUR})
    assert drive(late, sequence, {a: refusal(None, waits=False)}) == [b]


@over_backends
def test_the_hold_ends_when_the_held_candidate_starts(backend, record, clock):
    """templet:3163 -- a held candidate that fits starts and its wait is
    dropped; nothing is held after it."""
    a, b, c = listed(record, clock, 3)
    settings = Settings()
    sequence = sequence_at(record, settings)
    held = {("item", a): NOON - HOUR}

    first = Walk(sequence, settings=settings, waiting=dict(held))
    needs = {c: {"slots": 1}}
    assert drive(first, sequence, {a: refusal("memory")}, needs) == [c]
    assert first.waiting == held

    # a retry in one walk: the candidate starts after all, and the hold ends
    retry = Walk(sequence, settings=settings, waiting=dict(held))
    one, two, _ = sequence.candidates
    retry.check(one, NEEDS)
    retry.refused(one, refusal("memory"))
    assert retry.check(two, NEEDS) is not None
    retry.started(one)
    assert retry.check(two, NEEDS) is None
    assert ("item", a) not in retry.waiting

    # the next walk: it fits at once, and then nothing is held
    second = Walk(sequence, settings=settings, waiting=first.waiting)
    assert drive(second, sequence) == [a, b, c]
    assert second.waiting == {}
    assert second.left == []


@over_backends
def test_waiting_drops_what_left_the_sequence_and_keeps_the_first_time(
    backend, record, clock
):
    """templet:3163 -- `waiting` drops a candidate no longer in the sequence;
    a candidate refused again keeps its first time; the caller's map is not
    changed."""
    a, b = listed(record, clock, 2)
    settings = Settings()
    sequence = sequence_at(record, settings)
    kept = {
        ("item", 99_999): NOON - HOUR,
        ("task", 88_888): NOON - HOUR,
        ("item", a): NOON - 5,
    }
    given = dict(kept)

    walk = Walk(sequence, settings=settings, waiting=given)

    assert walk.waiting == {("item", a): NOON - 5}
    assert given == kept
    drive(walk, sequence, {a: refusal("memory")})
    assert walk.waiting == {("item", a): NOON - 5}


@over_backends
def test_the_cut_comes_before_the_hold(backend, record, clock):
    """templet:3163 -- a candidate cut for its role says so, whatever is held
    ahead of it."""
    admitted(record)
    held = item(record, "held", role="beta")
    tick(clock)
    first = item(record, "a1", role="alpha")
    tick(clock)
    second = item(record, "a2", role="alpha")
    record.edit_effort("0001", order={"rule": "listed", "items": [held, first, second]})
    settings = Settings()
    sequence = sequence_at(record, settings)
    walk = Walk(sequence, settings=settings, waiting={("item", held): NOON - HOUR})
    one, two, three = sequence.candidates

    assert walk.check(one, {"memory": 1}) is None
    walk.refused(one, refusal("memory"))
    assert walk.check(two, {"slots": 1}) is None
    walk.started(two)
    assert walk.check(three, {"memory": 1}) == "role alpha: 1 of 1 busy"


@over_backends
def test_the_walk_refuses_settings_with_problems(backend, record, clock):
    """templet:3163 -- `Walk` refuses a `Settings` that has problems."""
    listed(record, clock, 1)
    sequence = sequence_at(record, Settings())
    with pytest.raises(ValueError, match="landing_limit"):
        Walk(sequence, settings=Settings(landing_limit=-1))


# -- the seam with the allocator ------------------------------------------


def test_the_order_imports_nothing_of_the_allocator():
    """templet:3163 -- `quarrylith.order` reads the refusal by attribute name
    and imports nothing of `quarrylith.allocator`."""
    found = []
    for path in sorted(SRC.glob("*.py")):
        for node_ in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node_, ast.Import):
                names = [a.name for a in node_.names]
            elif isinstance(node_, ast.ImportFrom):
                base = "." * node_.level + (node_.module or "")
                names = [base] + [f"{base}.{a.name}" for a in node_.names]
            found += [(path.name, n) for n in names if "allocator" in n]
    assert found == []
