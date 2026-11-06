# SPDX-License-Identifier: Apache-2.0
"""The allocator's budget (templet:3161, build step 3): a provider's budget
readings, taken per window, narrow what `place` and `headroom` allow, and
never the capacity. Every test runs over every backend in BACKENDS."""

from backends import over_backends
from test_allocator_host import GIB, SAMPLE, declared, make_task, run

from quarrylith.allocator import Fit, Refusal, load_document

HOUR = 3_600_000
MIN = 60_000


def reading(record, clock, provider="anthropic", kind="five_hour", **value):
    """A budget reading of `provider`'s `kind` window, taken now."""
    value = {"provider": provider, "status": "allowed", "utilization": 0.1, **value}
    return record.record_reading("budget", kind, value)


def agents(record, clock, placement, count, **kw):
    """Start `count` claude-code agent runs."""
    for number in range(count):
        run(record, placement, clock, make_task(record, **kw), holder=f"w{number}")


def refused(placement, record, clock, **kw):
    """The Refusal of one more claude-code agent, or its Fit."""
    return placement.place(make_task(record, **kw), now=clock())


def budget_short(answer):
    assert isinstance(answer, Refusal), answer
    (short,) = answer.shorts
    assert short.cause == "budget" and short.resource == "budget.anthropic"
    return short


# -- the warning -------------------------------------------------------------------


@over_backends
def test_at_a_warning_a_run_is_refused_once_the_providers_weight_passes_the_max(
    effort, clock
):
    """templet:3161 -- with the provider's latest reading at `allowed_warning`,
    a run is refused `budget` once the provider's live weight plus its own
    passes `warning_max` (2), with waits true and `until` None. Another
    provider's runs are unaffected, and `place` writes nothing."""
    record = effort
    placement = declared(record, clock)
    reading(record, clock, status="allowed_warning", resets_at=clock() + HOUR)
    agents(record, clock, placement, 1)
    assert isinstance(refused(placement, record, clock), Fit)
    agents(record, clock, placement, 1)
    head = record.head()
    answer = refused(placement, record, clock)
    assert record.head() == head
    short = budget_short(answer)
    assert (short.asked, short.free, short.node) == (1, 0, "laptop")
    assert short.waits is True and short.until is None
    assert answer.waits is True and answer.until is None
    assert answer.text.endswith("; waits") and "budget.anthropic" in answer.text
    other = refused(placement, record, clock, labels=["harness:codex"])
    assert isinstance(other, Fit) and other.needs["budget.openai"] == 1


@over_backends
def test_a_budget_short_comes_beside_the_others_in_causes_order(effort, clock):
    """templet:3161 -- a budget short is collected with the table's, and the
    Refusal lists them in `CAUSES` order: room first, then budget."""
    record = effort
    placement = declared(record, clock)
    reading(record, clock, status="rejected", resets_at=clock() + HOUR)
    answer = refused(placement, record, clock, needs={"memory": 100 * GIB})
    assert [(s.cause, s.resource) for s in answer.shorts] == [
        ("room", "memory"),
        ("budget", "budget.anthropic"),
    ]
    assert answer.waits is False and answer.until is None


# -- the stop ----------------------------------------------------------------------


@over_backends
def test_at_the_stop_or_a_rejection_a_run_is_refused_until_the_reset(effort, clock):
    """templet:3161 -- at `utilization >= stop_at`, or at `rejected`, a run is
    refused `budget` with `until` equal to the reading's `resets_at`. Past
    `resets_at` the guard lifts with no new reading."""
    record = effort
    placement = declared(record, clock)
    resets = clock() + 4 * MIN
    reading(record, clock, utilization=0.95, resets_at=resets)
    answer = refused(placement, record, clock)
    assert budget_short(answer).until == resets and answer.until == resets
    assert answer.waits is True
    assert "waits until" in answer.text
    clock.step(MIN)
    reading(record, clock, utilization=0.2, status="rejected", resets_at=resets)
    assert budget_short(refused(placement, record, clock)).until == resets
    clock.step(MIN)
    reading(record, clock, utilization=0.94, resets_at=resets)
    assert isinstance(refused(placement, record, clock), Fit)
    clock.step(MIN)
    reading(record, clock, utilization=0.99, resets_at=resets)
    assert budget_short(refused(placement, record, clock)).until == resets
    clock.step(2 * MIN)
    assert clock() >= resets
    assert isinstance(refused(placement, record, clock), Fit)


# -- the windows -------------------------------------------------------------------


@over_backends
def test_the_readings_are_taken_per_window_and_the_tighter_one_counts(effort, clock):
    """templet:3161 -- a provider's latest five-hour reading at `allowed`,
    newer than its latest seven-day reading at `rejected`, still refuses
    `budget`, with `until` the seven-day `resets_at`. With both windows
    stopped, `until` is the later `resets_at`. A window's newer reading
    replaces its older one."""
    record = effort
    placement = declared(record, clock)
    week, five = clock() + 10 * MIN, clock() + 5 * MIN
    reading(record, clock, kind="seven_day", status="rejected", resets_at=week)
    clock.step(MIN)
    reading(record, clock, kind="five_hour", resets_at=five)
    answer = refused(placement, record, clock)
    assert budget_short(answer).until == week and answer.until == week
    clock.step(MIN)
    reading(record, clock, kind="five_hour", status="rejected", resets_at=five)
    assert budget_short(refused(placement, record, clock)).until == week
    clock.step(MIN)
    reading(record, clock, kind="seven_day", utilization=0.1, resets_at=week)
    assert budget_short(refused(placement, record, clock)).until == five
    clock.step(MIN)
    reading(record, clock, kind="five_hour", resets_at=five)
    assert isinstance(refused(placement, record, clock), Fit)


@over_backends
def test_a_stop_beats_a_warning_in_the_reason(effort, clock):
    """templet:3161 -- one window at a warning and another at a stop is one
    `budget` short, whose `until` is the stop's reset."""
    record = effort
    placement = declared(record, clock)
    reading(record, clock, kind="five_hour", status="allowed_warning")
    reading(record, clock, kind="seven_day", status="rejected", resets_at=clock() + MIN)
    short = budget_short(refused(placement, record, clock))
    assert short.until == clock() + MIN


# -- no guard ----------------------------------------------------------------------


@over_backends
def test_an_old_or_missing_reading_guards_nothing_and_a_local_harness_is_never_refused(
    effort, clock
):
    """templet:3161 -- a budget reading older than `budget.max_age_s` (900 s),
    or none, guards nothing; another provider's reading does not count; and a
    harness with no budget resource is never refused `budget`."""
    record = effort
    text = SAMPLE.read_text().replace('"slots.local" = 0', '"slots.local" = 1')
    placement = declared(record, clock, load_document(text))
    assert isinstance(refused(placement, record, clock), Fit)
    reading(record, clock, provider="openai", status="rejected")
    assert isinstance(refused(placement, record, clock), Fit)
    reading(record, clock, status="rejected", resets_at=clock() + 4 * HOUR)
    assert budget_short(refused(placement, record, clock))
    local = refused(placement, record, clock, labels=["harness:local"])
    assert isinstance(local, Fit) and "budget.anthropic" not in local.needs
    clock.step(900 * 1000)
    assert budget_short(refused(placement, record, clock))
    clock.step(1000)
    assert isinstance(refused(placement, record, clock), Fit)
    reading(record, clock, utilization=0.99, status="rejected")
    clock.step(900 * 1000 + 1)
    assert isinstance(refused(placement, record, clock), Fit)


# -- headroom ----------------------------------------------------------------------


@over_backends
def test_headroom_is_the_least_of_the_tables_free_and_the_budget_allowance(
    effort, clock
):
    """templet:3161 -- `headroom` of a budget resource is the least of the
    table's free and the allowance: 0 at a stop, `warning_max` less the live
    runs' weight at a warning, and the table's free with no guard. The
    readings move the allowance, never the capacity."""
    record = effort
    placement = declared(record, clock)
    room = placement.headroom(now=clock())["laptop"]
    assert room["budget.anthropic"] == 4 and room["budget.openai"] == 3
    reading(record, clock, status="allowed_warning")
    assert placement.headroom(now=clock())["laptop"]["budget.anthropic"] == 2
    agents(record, clock, placement, 1)
    room = placement.headroom(now=clock())["laptop"]
    assert room["budget.anthropic"] == 1 and room["budget.openai"] == 3
    agents(record, clock, placement, 1)
    assert placement.headroom(now=clock())["laptop"]["budget.anthropic"] == 0
    clock.step()
    reading(record, clock, utilization=0.96)
    room = placement.headroom(now=clock())["laptop"]
    assert room["budget.anthropic"] == 0 and room["slots.claude-code"] == 2
    assert record.get("capacity", ["laptop", "budget.anthropic"])["amount"] == 4
    clock.step()
    reading(record, clock)
    assert placement.headroom(now=clock())["laptop"]["budget.anthropic"] == 2
