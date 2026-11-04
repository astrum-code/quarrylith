# SPDX-License-Identifier: Apache-2.0
"""The sequence (templet:3163, build step 2), over every backend in BACKENDS:
`Settings`, `effort_order`, `order` and `place`, and `quarrylith order show`.

Each ordering test builds its fixture so that every other order of the keys it
checks gives a different answer. Times are UTC literals, never read from the
module under test."""

import json
from datetime import datetime, timezone

import pytest
from backends import PARTS, admitted, over_backends

from quarrylith import cli
from quarrylith.order.sequence import RULES, effort_order, order, place
from quarrylith.order.settings import DEFAULT, Settings
from quarrylith.record import schema


def utc(year, month, day, hour=0, minute=0):
    moment = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
    return int(moment.timestamp()) * 1000


ROLE_KEY = "role"

# After the default clock (2023-11-14 22:13 UTC), so the record's time only
# moves forward.
NOON = utc(2023, 11, 15, 12)
EVENING = utc(2023, 11, 15, 23)


# -- fixtures -------------------------------------------------------------


def effort(rec, id, *, standing="admitted", rank=None, rule=None, items=None):
    """An effort, `standing`, with its order json set from what is given."""
    rec.file_effort(id, f"e{id}", f"Effort {id}", "owner-a", PARTS, standing=standing)
    given = {}
    if rule is not None:
        given["rule"] = rule
    if rank is not None:
        given["rank"] = rank
    if items is not None:
        given["items"] = items
    if given:
        rec.edit_effort(id, order=given)
    return id


def item(rec, title="t", **kw):
    """An item filed ready (it has a role), under effort 0001."""
    kw.setdefault("filed_by", "alpha")
    kw.setdefault("effort", "0001")
    kw.setdefault("role", "alpha")
    return rec.file_item("task", title, f"body of {title}", **kw)


def task_of(rec, n, kind="agent", **kw):
    kw.setdefault("sandbox", "harness")
    kw.setdefault("needs", {"slots": 1})
    return rec.file_task(kind, number=n, **kw)


def job(rec, kind="job", **kw):
    kw.setdefault("effort", "0001")
    kw.setdefault("sandbox", "process")
    kw.setdefault("needs", {"slots": 1})
    return rec.file_task(kind, **kw)


def node(rec, name="n1", slots=8):
    rec.declare_node(name, ["process", "harness"], ["zone:a"])
    rec.set_capacity(name, "slots", slots, "slots")


def grant(rec, n, holder="alpha"):
    return rec.grant(n, holder, branch=f"feat/{n}", machine="m1")


def run_of(rec, task, *, fence=None):
    return rec.start_run(
        task,
        "alpha",
        node="n1",
        sandbox=rec.get("tasks", task)["sandbox"],
        fence=fence,
        expect_s=60,
    )


def block(rec, blocked, blocker):
    rec.add_dep(blocked, blocker=blocker, why="waits")


def tick(clock):
    return clock.step(60_000)


def keys(sequence):
    return [c.key for c in sequence.candidates]


def sequence_of(rec, clock, **kw):
    return order(rec, now=clock(), settings=kw.pop("settings", DEFAULT))


class Reads:
    """The record as the order may see it: the contract's reads and nothing
    else (no write, no `head`), noting which kinds were read."""

    def __init__(self, rec):
        self._rec = rec
        self.kinds = set()

    def get(self, kind, key, **kw):
        self.kinds.add(kind)
        return self._rec.get(kind, key, **kw)

    def read(self, kind, /, **kw):
        self.kinds.add(kind)
        return self._rec.read(kind, **kw)

    def reading(self):
        return self._rec.reading()

    def ready(self, *, now):
        return self._rec.ready(now=now)

    def effective_labels(self, task_id):
        return self._rec.effective_labels(task_id)


# -- never widens ---------------------------------------------------------


@over_backends
def test_the_order_never_widens(backend, record, clock):
    """templet:3163 -- every candidate of tiers 1 to 3 is in `ready()`; items
    of a parked effort, an unadmitted machine-filed item, a live dep, a live
    hold, a task not before now and a task past its attempts are not."""
    admitted(record)
    effort(record, "0002", standing="parked")
    node(record)
    ok = item(record, "ok")
    ok_task = task_of(record, item(record, "ok with a task"))
    parked = item(record, "parked", effort="0002")
    unadmitted = item(record, "should-fix", machine=True)
    blocked = item(record, "blocked")
    block(record, blocked, ok)
    held = item(record, "held")
    record.hold(number=held, cause="manual", why="wait")
    future = item(record, "future")
    task_of(record, future, not_before=clock() + 3_600_000)
    spent = job(record)
    run = run_of(record, spent)
    record.finish_run(run, "done", "finished", footprint={}, cost={})

    ready = record.ready(now=clock())
    got = sequence_of(record, clock)
    in_ready = {("item", i["number"]) for i in ready["items"]} | {
        ("task", t["id"]) for t in ready["tasks"]
    }
    assert set(keys(got)) <= in_ready
    assert set(keys(got)) == {("item", ok), ("task", ok_task)}
    for out in (parked, unadmitted, blocked, held, future):
        assert ("item", out) not in keys(got)
    assert ("task", spent) not in keys(got)
    assert got.at == clock()
    assert got.seq == record.head()


@over_backends
def test_an_item_with_an_open_task_is_a_candidate_as_its_task(backend, record, clock):
    """templet:3163 -- an item with an open task in the ready set is a
    candidate only as its task; one whose open task is not ready is not a
    candidate; one with no task is a candidate as itself."""
    admitted(record)
    plain = item(record, "plain")
    tasked = item(record, "tasked")
    task = task_of(record, tasked)
    waits = item(record, "waits")
    later = task_of(record, waits, not_before=clock() + 3_600_000)

    got = sequence_of(record, clock)
    assert keys(got) == [("item", plain), ("task", task)]
    plain_c, task_c = got.candidates
    assert (plain_c.task, plain_c.item) == (None, plain)
    assert (task_c.task, task_c.item) == (task, tasked)
    assert ("item", tasked) not in keys(got)
    assert ("item", waits) not in keys(got)
    left = [(entry.task, entry.item) for entry in got.left]
    assert (later, waits) in left


# -- the tiers ------------------------------------------------------------


def tier_fixture(record, clock):
    """Effort A (rank 1) has new work; effort B (rank 2) has two reviews, the
    older first, and a resumed item; one review of A is running."""
    admitted(record, "0001")
    effort(record, "0002", rank=2)
    record.edit_effort("0001", order={"rule": "unblocks-then-age", "rank": 1})
    node(record)
    running = job(record, "review")
    run_of(record, running)
    tick(clock)
    r1 = record.file_item(
        "review", "r1", "body", role="alpha", effort="0002", filed_by="alpha"
    )
    tick(clock)
    r2 = record.file_item(
        "review", "r2", "body", role="alpha", effort="0002", filed_by="alpha"
    )
    tick(clock)
    a1 = item(record, "new work", effort="0001")
    tick(clock)
    resumed = item(record, "resumed", effort="0002")
    resumed_task = task_of(record, resumed)
    grant(record, resumed)
    return r1, r2, a1, resumed_task


@over_backends
def test_the_tiers(backend, record, clock):
    """templet:3163 -- review_slots 2 and one review running: one review is
    floor, then the resumes, then the effort order; the floor review of the
    lower-ranked effort comes before new work of the higher-ranked, and the
    second review waits in the effort order."""
    r1, r2, a1, resumed = tier_fixture(record, clock)

    got = sequence_of(record, clock)
    assert keys(got) == [("item", r1), ("task", resumed), ("item", a1), ("item", r2)]
    assert [c.tier for c in got.candidates] == [1, 2, 3, 3]
    assert [c.rule for c in got.candidates] == [
        "floor",
        "resume",
        "unblocks-then-age",
        "unblocks-then-age",
    ]
    assert [c.effort for c in got.candidates] == ["0002", "0002", "0001", "0002"]


@over_backends
def test_the_floor_is_review_slots_less_the_reviews_running(backend, record, clock):
    """templet:3163 -- a larger floor takes both reviews; a floor already full
    takes none, and they wait in the effort order."""
    r1, r2, a1, resumed = tier_fixture(record, clock)

    wide = sequence_of(record, clock, settings=Settings(review_slots=3))
    assert keys(wide) == [
        ("item", r1),
        ("item", r2),
        ("task", resumed),
        ("item", a1),
    ]
    assert [c.tier for c in wide.candidates] == [1, 1, 2, 3]

    full = sequence_of(record, clock, settings=Settings(review_slots=1))
    assert keys(full) == [
        ("task", resumed),
        ("item", a1),
        ("item", r1),
        ("item", r2),
    ]
    assert [c.tier for c in full.candidates] == [2, 3, 3, 3]
    none = sequence_of(record, clock, settings=Settings(review_slots=0))
    assert [c.tier for c in none.candidates] == [2, 3, 3, 3]


# -- the rules ------------------------------------------------------------


@over_backends
def test_unblocks_then_age(backend, record, clock):
    """templet:3163 -- more unblocks first (counted through a chain, each item
    once), then older, then the lower number; a review counts the unblocks of
    the item it reviews."""
    admitted(record)
    target = item(record, "reviewed", role=None)
    x = item(record, "x")
    tick(clock)
    w = item(record, "w")
    tick(clock)
    y = item(record, "y")
    tick(clock)
    z = item(record, "z")
    tick(clock)
    i1 = item(record, "i1")
    i2 = item(record, "i2")
    tick(clock)
    review = record.file_item(
        "review", "review", "body", role="alpha", effort="0001", filed_by="alpha"
    )
    record.file_review(target, 1, "pool", item=review, author="alpha")
    dependents = {n: item(record, f"after {n}") for n in "pqrstuv"}
    block(record, dependents["p"], y)
    block(record, dependents["q"], y)
    block(record, dependents["q"], dependents["p"])
    block(record, dependents["v"], dependents["p"])
    block(record, dependents["r"], w)
    block(record, dependents["s"], z)
    block(record, dependents["t"], target)
    block(record, dependents["u"], target)

    got = sequence_of(record, clock, settings=Settings(review_slots=0))
    assert keys(got) == [("item", n) for n in (y, review, w, z, x, i1, i2)]
    by_item = {c.item: c for c in got.candidates}
    assert by_item[y].unblocks == tuple(dependents[n] for n in "pqv")
    assert by_item[review].unblocks == (dependents["t"], dependents["u"])
    assert by_item[x].unblocks == ()


@over_backends
def test_listed(backend, record, clock):
    """templet:3163 -- the listed items first, in the list's order, then the
    rest by unblocks-then-age; an `order` with an unknown rule is read as the
    default and a note says why."""
    admitted(record)
    ghost = item(record, "ghost", role=None)
    a = item(record, "a")
    tick(clock)
    b = item(record, "b")
    tick(clock)
    e = item(record, "e")
    tick(clock)
    c = item(record, "c")
    after = item(record, "after b")
    block(record, after, b)
    record.edit_effort("0001", order={"rule": "listed", "items": [c, ghost, a]})
    effort(record, "0002", rule="alphabetical")
    d = item(record, "d", effort="0002")

    got = sequence_of(record, clock)
    assert keys(got)[:4] == [("item", n) for n in (c, a, b, e)]
    by_item = {x.item: x for x in got.candidates}
    assert [by_item[n].rule for n in (c, a, b)] == [
        "listed",
        "listed",
        "unblocks-then-age",
    ]
    assert by_item[c].why.endswith("1st of 3 listed")
    assert by_item[a].why.endswith("3rd of 3 listed")
    assert by_item[d].rule == "unblocks-then-age"
    assert any("0002" in note and "alphabetical" in note for note in got.notes)
    assert not any("0001" in note for note in got.notes)


def test_effort_order_reads_the_shape():
    """templet:3163 -- `effort_order` reads `rule`, `rank` and `items`, and a
    bad shape is read as the default with a note."""
    assert effort_order({"order": {"rule": "unblocks-then-age"}}) == (
        "unblocks-then-age",
        None,
        (),
        None,
    )
    assert effort_order({"order": {"rule": "listed", "items": [3, 1], "rank": 2}}) == (
        "listed",
        2,
        (3, 1),
        None,
    )
    assert effort_order({"order": {"rule": "unblocks-then-age", "rank": 1}})[1] == 1
    for bad in (
        {"rule": "alphabetical"},
        {"rule": "listed"},
        {"rule": "listed", "items": "3"},
        {"rule": "listed", "items": [3, True]},
        {"rule": "listed", "items": [3, "x"]},
        {"rule": "unblocks-then-age", "rank": "1"},
        {"rule": "unblocks-then-age", "rank": True},
        {"rank": 1.5},
        [],
        None,
    ):
        rule, rank, items, note = effort_order({"order": bad})
        assert rule == "unblocks-then-age", bad
        assert items == (), bad
        assert rank is None, bad
        assert isinstance(note, str) and note, bad


@over_backends
def test_efforts_by_rank(backend, record, clock):
    """templet:3163 -- efforts by rank, lower first, the unranked after by
    id; an item linked to two admitted efforts is placed once, under the one
    that comes first; an item with no link of its own is placed under its
    parent's effort."""
    effort(record, "0001", rank=2)
    effort(record, "0002", rank=1)
    effort(record, "0003")
    effort(record, "0004")
    both = item(record, "both", effort="0001")
    record.link("0002", "item", str(both))
    parent = item(record, "parent", effort="0003")
    child = item(record, "child", parent=parent, effort=None)
    record.unlink("0003", "item", str(child))
    unranked = item(record, "four", effort="0004")
    low = item(record, "one", effort="0001")

    got = sequence_of(record, clock)
    assert [(c.item, c.effort) for c in got.candidates] == [
        (both, "0002"),
        (low, "0001"),
        (parent, "0003"),
        (child, "0003"),
        (unranked, "0004"),
    ]
    assert len(keys(got)) == len(set(keys(got)))


# -- schedules and flags --------------------------------------------------


@over_backends
def test_a_schedule_and_the_night_flag_leave_a_task_out(backend, record, clock):
    """templet:3163 -- a task whose schedule does not parse, one outside its
    window, and a `night` task outside the night window (or with none set)
    are left out, each with its reason; inside the window it is a candidate."""
    admitted(record)
    clock.at = NOON
    windowed = job(record, schedule="window 22:00-06:00; tz UTC")
    later = job(record, schedule="window 08:00-09:00; tz UTC")
    broken = job(record, schedule="cron 61 * * * *")
    night_item = item(record, "night work", flags=["night"])
    night_task = task_of(record, night_item)
    night_only = item(record, "night item", flags=["night"])
    plain = job(record)
    nights = Settings(night="window 22:00-06:00; tz UTC")

    def left(got):
        return {(e.task, e.item): e.why for e in got.left}

    got = order(record, now=NOON, settings=DEFAULT)
    why = left(got)
    assert keys(got) == [("task", plain)]
    assert "outside its window (opens 22:00 UTC)" in why[(windowed, None)]
    assert why[(windowed, None)].startswith(f"task {windowed}: ")
    assert "(opens 2023-11-16 08:00 UTC)" in why[(later, None)]
    assert "minute 61" in why[(broken, None)]
    assert "schedule" in why[(broken, None)]
    assert "night" in why[(night_task, night_item)]
    assert "no night window" in why[(night_task, night_item)]
    assert "no night window" in why[(None, night_only)]
    assert why[(None, night_only)].startswith(f"item {night_only}: ")

    got = order(record, now=NOON, settings=nights)
    why = left(got)
    assert "outside the night window (opens 22:00 UTC)" in why[(night_task, night_item)]
    assert (windowed, None) in why

    got = order(record, now=EVENING, settings=nights)
    assert set(keys(got)) == {
        ("task", windowed),
        ("task", night_task),
        ("item", night_only),
        ("task", plain),
    }
    why = left(got)
    assert "(opens 2023-11-16 08:00 UTC)" in why[(later, None)]
    assert (windowed, None) not in why


# -- what a candidate says ------------------------------------------------


@over_backends
def test_every_candidate_names_its_effort_and_rule(backend, record, clock):
    """templet:3163 -- every candidate names an effort and a rule of RULES,
    and its `why` holds both; there is no record kind `ranks`, and the order
    reads none."""
    r1, r2, a1, resumed = tier_fixture(record, clock)
    reads = Reads(record)

    got = order(reads, now=clock(), settings=DEFAULT)
    assert got.candidates
    for c in got.candidates:
        assert c.effort and c.rule in RULES
        assert f"effort {c.effort}" in c.why and c.rule in c.why
        assert "\n" not in c.why
    assert "ranks" not in schema.RECORDS
    assert "ranks" not in reads.kinds


@over_backends
def test_the_why_lines(backend, record, clock):
    """templet:3163 -- the `why` of each rule, with its figure."""
    r1, r2, a1, resumed_task = tier_fixture(record, clock)
    held_at = datetime.fromtimestamp(clock() / 1000, timezone.utc)
    resumed = record.get("tasks", resumed_task)["number"]
    target = item(record, "target", role=None)
    record.file_review(target, 1, "pool", item=r1, author="alpha")
    u = item(record, "u")
    p = item(record, "p")
    q = item(record, "q")
    block(record, p, u)
    block(record, q, u)
    record.edit_effort("0001", order={"rule": "listed", "items": [999, a1, 998]})

    got = {c.item: c for c in sequence_of(record, clock).candidates}
    assert got[r1].why == (
        f"#{r1}: effort 0002, floor -- review 2 of 2, unblocks a merge of #{target}"
    )
    assert got[resumed].why == (
        f"#{resumed} task {resumed_task}: effort 0002, resume -- held by alpha "
        f"since {held_at:%Y-%m-%d %H:%M}"
    )
    assert got[a1].why == f"#{a1}: effort 0001, listed -- 2nd of 3 listed"
    assert got[u].why == (
        f"#{u}: effort 0001, unblocks-then-age -- unblocks 2 (#{p}, #{q}), "
        f"filed {held_at:%Y-%m-%d}"
    )


# -- pure policy ----------------------------------------------------------


@over_backends
def test_the_order_writes_nothing(backend, record, clock):
    """templet:3163 -- `order` leaves the journal's head where it was, reads
    only the contract's reads, and gives the same sequence twice."""
    tier_fixture(record, clock)
    before = record.head()

    first = sequence_of(record, clock)
    second = order(Reads(record), now=clock(), settings=DEFAULT)
    assert record.head() == before
    assert first == second
    assert first.seq == before


@over_backends
def test_a_read_only_handle_orders(backend, record, opener, clock):
    """templet:3163 -- a handle opened read-only gives the same sequence."""
    tier_fixture(record, clock)
    reader = opener(readonly=True)
    assert order(reader, now=clock(), settings=DEFAULT) == sequence_of(record, clock)


@over_backends
def test_the_settings_are_read_at_every_call(backend, record, clock):
    """templet:3163 -- nothing is cached between calls: a changed setting is
    seen by the next sequence."""
    r1, r2, a1, resumed = tier_fixture(record, clock)
    one = sequence_of(record, clock, settings=Settings(review_slots=3))
    two = sequence_of(record, clock, settings=Settings(review_slots=1))
    three = sequence_of(record, clock, settings=Settings(review_slots=3))
    assert keys(one) != keys(two)
    assert one == three


# -- place ----------------------------------------------------------------


@over_backends
def test_place_gives_what_the_order_gives(backend, record, clock):
    """templet:3163 -- `place` gives a task or an item the tier, rule and
    effort `order` gives it, and a `Left` with the same reason for one the
    order leaves out."""
    r1, r2, a1, resumed = tier_fixture(record, clock)
    cut = job(record, schedule="window 08:00-09:00; tz UTC")
    night = item(record, "night", flags=["night"])
    waiting = task_of(record, item(record, "waits"), not_before=clock() + 3_600_000)
    got = sequence_of(record, clock)

    for c in got.candidates:
        if c.task is not None:
            assert place(record, now=clock(), settings=DEFAULT, task=c.task) == c
        if c.item is not None:
            assert place(record, now=clock(), settings=DEFAULT, item=c.item) == c
    assert got.left
    for entry in got.left:
        if entry.task is not None:
            found = place(record, now=clock(), settings=DEFAULT, task=entry.task)
        else:
            found = place(record, now=clock(), settings=DEFAULT, item=entry.item)
        assert found == entry
    assert place(record, now=clock(), settings=DEFAULT, task=cut).why.startswith(
        f"task {cut}:"
    )
    assert place(record, now=clock(), settings=DEFAULT, item=night).item == night
    gone = place(record, now=clock(), settings=DEFAULT, task=waiting)
    assert gone.task == waiting and "not before" in gone.why
    nothing = place(record, now=clock(), settings=DEFAULT, task=9999)
    assert nothing.task == 9999 and "9999" in nothing.why
    with pytest.raises(ValueError):
        place(record, now=clock(), settings=DEFAULT)
    with pytest.raises(ValueError):
        place(record, now=clock(), settings=DEFAULT, task=cut, item=night)


# -- the settings ---------------------------------------------------------


def test_the_default_settings_are_valid():
    """templet:3163 -- the defaults the page gives, and no problems."""
    assert DEFAULT == Settings()
    assert (DEFAULT.review_slots, DEFAULT.landing_limit, DEFAULT.hold_after_s) == (
        2,
        6,
        1800,
    )
    assert DEFAULT.night is None and DEFAULT.instances == {}
    assert DEFAULT.merge_rules == (
        {"name": "pool-approved", "risk_at_most": 4, "reviews": ["pool"]},
    )
    assert DEFAULT.problems() == []
    assert Settings().instances is not Settings().instances


@pytest.mark.parametrize(
    "bad, field",
    [
        ({"review_slots": -1}, "review_slots"),
        ({"review_slots": True}, "review_slots"),
        ({"review_slots": 1.5}, "review_slots"),
        ({"landing_limit": -1}, "landing_limit"),
        ({"hold_after_s": -1}, "hold_after_s"),
        ({"instances": {"alpha": 0}}, "alpha"),
        ({"instances": {"alpha": 1.5}}, "alpha"),
        ({"instances": {"alpha": True}}, "alpha"),
        ({"instances": ["alpha"]}, "instances"),
        ({"night": "window 22-6"}, "night"),
        ({"night": "cron 0 22 * * *"}, "night"),
        ({"night": 5}, "night"),
        ({"merge_rules": ({"risk_at_most": 4, "reviews": ["pool"]},)}, "name"),
        (
            {"merge_rules": ({"name": "x", "risk_at_most": 11, "reviews": ["pool"]},)},
            "risk_at_most",
        ),
        (
            {"merge_rules": ({"name": "x", "risk_at_most": -1, "reviews": ["pool"]},)},
            "risk_at_most",
        ),
        (
            {"merge_rules": ({"name": "x", "risk_at_most": 4, "reviews": ["moot"]},)},
            "moot",
        ),
    ],
)
def test_settings_problems_name_the_field(bad, field):
    """templet:3163 -- `problems()` names each invalid field."""
    problems = Settings(**bad).problems()
    assert len(problems) == 1 and field in problems[0]


def test_settings_problems_name_each_fault():
    """templet:3163 -- every fault is its own line; valid values give none."""
    two = Settings(review_slots=-1, landing_limit=-2).problems()
    assert len(two) == 2
    fine = Settings(
        review_slots=0,
        landing_limit=0,
        hold_after_s=0,
        instances={"alpha": 3},
        night="window 22:00-06:00 mon-fri; window 20:00-08:00 sat,sun; tz UTC",
        merge_rules=(
            {"name": "wide", "risk_at_most": 9, "reviews": ["pool", "second"]},
        ),
    )
    assert fine.problems() == []


@over_backends
def test_order_refuses_settings_with_problems(backend, record, clock):
    """templet:3163 -- `order` and `place` refuse settings that have
    problems."""
    admitted(record)
    item(record)
    bad = Settings(review_slots=-1)
    with pytest.raises(ValueError, match="review_slots"):
        order(record, now=clock(), settings=bad)
    with pytest.raises(ValueError, match="review_slots"):
        place(record, now=clock(), settings=bad, item=1)


# -- what the walk starts from --------------------------------------------


@over_backends
def test_a_candidate_carries_what_the_walk_reads(backend, record, clock):
    """templet:3163 -- role, repos, the repos a start would add a change to,
    and what exempts it from the landing limit."""
    admitted(record)
    # a neutral role label, built from its key: the polish lint's deny rule
    # matches the literal `role:<name>` in any file of a public-bound repo
    label = f"{ROLE_KEY}:alpha"
    beat = item(record, "label beats field", role="beta", labels=[label])
    field = item(record, "field", role="beta")
    both = item(record, "two repos", labels=["repo:widgets", "repo:gadgets"])
    changed = item(record, "has a change", labels=["repo:widgets", "repo:gadgets"])
    record.open_or_update_change(
        "widgets", "feat/c", "h1", title="t", number=changed, by="alpha"
    )
    over = item(record, "task over item", labels=["repo:widgets"])
    over_task = task_of(record, over, labels=["repo:gadgets"])
    review = record.file_item(
        "review", "rv", "body", role="alpha", effort="0001", filed_by="alpha"
    )
    resync_task = job(record, flags=["resync"], labels=["repo:widgets"])
    sent_back = item(record, "sent back", labels=["repo:widgets"])
    sent_task = task_of(record, sent_back)
    grant(record, sent_back)
    record.edit_item(sent_back, review_round=1)
    verdict = record.file_review(sent_back, 1, "pool", author="alpha")
    record.record_verdict(verdict, "beta", "changes", [])
    idle = item(record, "no lease", labels=["repo:widgets"])
    record.edit_item(idle, review_round=1)
    idle_review = record.file_review(idle, 1, "pool", author="alpha")
    record.record_verdict(idle_review, "beta", "changes", [])

    got = sequence_of(record, clock, settings=Settings(review_slots=0))
    by_key = {c.key: c for c in got.candidates}
    assert by_key[("item", beat)].role == "alpha"
    assert by_key[("item", field)].role == "beta"
    c = by_key[("item", both)]
    assert (c.repos, c.adds, c.exempt) == (("gadgets", "widgets"),) * 2 + (None,)
    c = by_key[("item", changed)]
    assert (c.repos, c.adds) == (("gadgets", "widgets"), ("gadgets",))
    c = by_key[("task", over_task)]
    assert (c.repos, c.adds) == (("gadgets",), ("gadgets",))
    assert by_key[("item", review)].exempt == "review"
    assert by_key[("task", resync_task)].exempt == "resync"
    assert by_key[("task", sent_task)].exempt == "post-review"
    assert by_key[("item", idle)].exempt is None
    assert by_key[("item", field)].repos == ()


@over_backends
def test_counts_are_the_figures_the_walk_starts_from(backend, record, clock):
    """templet:3163 -- per role the live runs; per repo the open changes plus
    the live runs that will open one; the live review runs of admitted
    efforts, and the live landing runs."""
    admitted(record, "0001")
    admitted(record, "0002")
    node(record)

    def started(n, task, **kw):
        return run_of(record, task, fence=grant(record, n, **kw))

    w1 = item(record, "w1", labels=["repo:widgets"])
    started(w1, task_of(record, w1))
    w2 = item(record, "w2", labels=["repo:widgets"])
    started(w2, task_of(record, w2))
    record.open_or_update_change(
        "widgets", "feat/w2", "h", title="t", number=w2, by="alpha"
    )
    record.open_or_update_change("gadgets", "feat/g", "h", title="t", by="alpha")
    gone = record.open_or_update_change(
        "gadgets", "feat/old", "h", title="t", by="alpha"
    )
    record.drop_change(gone, "alpha", "stale")
    rv = record.file_item(
        "review",
        "rv",
        "b",
        role="beta",
        effort="0001",
        filed_by="alpha",
        labels=["repo:widgets"],
    )
    started(rv, task_of(record, rv, "review"))
    sync = item(record, "sync", labels=["repo:gadgets"], flags=["resync"])
    started(sync, task_of(record, sync))
    land = record.file_item(
        "review", "land", "b", role="gamma", effort="0002", filed_by="alpha"
    )
    started(land, task_of(record, land, "review"))
    record.set_standing("0002", "parked", "paused")

    counts = sequence_of(record, clock).counts
    assert counts == {
        "roles": {"alpha": 3, "beta": 1, "gamma": 1},
        "repos": {"gadgets": 1, "widgets": 2},
        "reviews": 1,
        "landing": 1,
    }


# -- the command ----------------------------------------------------------


def show(capsys, url, *args):
    code = cli.main(["order", "show", url, *args])
    out, err = capsys.readouterr()
    return code, out, err


@over_backends
def test_order_show_prints_the_sequence(backend, record, url, clock, capsys):
    """templet:3163 -- one line per candidate (its `why`), then what was left
    out with its reason, then the notes and the counts."""
    r1, r2, a1, resumed = tier_fixture(record, clock)
    effort(record, "0003", rule="alphabetical")
    job(record, schedule="window 08:00-09:00; tz UTC")
    now = clock()

    code, out, err = show(capsys, url, "--now", str(now))
    assert code == 0
    got = order(record, now=now, settings=DEFAULT)
    lines = out.splitlines()
    assert lines[: len(got.candidates)] == [c.why for c in got.candidates]
    assert any(line.startswith("left: ") and "window" in line for line in lines)
    assert any(line.startswith("note: ") and "alphabetical" in line for line in lines)
    assert any(line.startswith("count") and "reviews" in line for line in lines)
    assert err == ""


@over_backends
def test_order_show_json_and_settings(backend, record, url, clock, capsys, tmp_path):
    """templet:3163 -- `--json` prints the sequence as one JSON object, and
    `--settings FILE` is a JSON object of `Settings`' fields."""
    r1, r2, a1, resumed = tier_fixture(record, clock)
    now = clock()
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"review_slots": 3}))

    code, out, _ = show(
        capsys, url, "--now", str(now), "--json", "--settings", str(path)
    )
    assert code == 0
    doc = json.loads(out)
    expected = order(record, now=now, settings=Settings(review_slots=3))
    assert [c["key"] for c in doc["candidates"]] == [list(k) for k in keys(expected)]
    assert [c["why"] for c in doc["candidates"]] == [c.why for c in expected.candidates]
    assert doc["counts"] == expected.counts
    assert doc["at"] == now and doc["seq"] == record.head()


@over_backends
def test_order_show_refuses_what_is_not_whole(
    backend, record, url, clock, capsys, tmp_path
):
    """templet:3163 -- exit 2 on settings with problems, an unknown field, or
    a file that is not there."""
    admitted(record)
    item(record)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"review_slots": -1}))
    code, out, err = show(capsys, url, "--settings", str(bad))
    assert (code, out) == (2, "") and "review_slots" in err
    bad.write_text(json.dumps({"night": "window 22-6"}))
    code, out, err = show(capsys, url, "--settings", str(bad))
    assert (code, out) == (2, "") and "night" in err
    bad.write_text(json.dumps({"nope": 1}))
    code, out, err = show(capsys, url, "--settings", str(bad))
    assert (code, out) == (2, "") and "nope" in err
    code, out, err = show(capsys, url, "--settings", str(tmp_path / "none.json"))
    assert (code, out) == (2, "") and "refused" in err


@over_backends
def test_order_show_takes_timing_and_debug(backend, record, url, clock, capsys):
    """templet:3163 -- `--timing` and `--debug` write to stderr and leave
    stdout as it was; a phase past the slow bound is printed with its time."""
    tier_fixture(record, clock)
    now = str(clock())
    _, plain, _ = show(capsys, url, "--now", now)

    code, out, err = show(capsys, url, "--now", now, "--timing", "--debug")
    assert code == 0 and out == plain
    assert "timing:" in err and "settings:" in err


@over_backends
def test_order_show_prints_a_slow_phase(
    backend, record, url, clock, capsys, monkeypatch
):
    """templet:3163 -- a phase that takes longer than the bound is printed
    with its elapsed time."""
    tier_fixture(record, clock)
    monkeypatch.setattr(cli, "SLOW_PHASE_S", 0.0)
    code, out, err = show(capsys, url, "--now", str(clock()))
    assert code == 0
    assert "reading the record" in err and " s" in err
