# SPDX-License-Identifier: Apache-2.0
"""The allocator's budget holds a stop (templet:3161): a `rejected` or
over-limit reading holds until its own reset, however old; a stop with no
readable reset holds until a newer reading, and says so; a stale warning
guards nothing; an unrecognised status is paced like a warning; `resets_at` is
in milliseconds. Every test runs over every backend in BACKENDS."""

import pytest
from backends import over_backends
from test_allocator_budget import (
    HOUR,
    MIN,
    agents,
    budget_short,
    reading,
    refused,
)
from test_allocator_host import declared, make_task

from quarrylith.allocator import Fit, budget

MAX_AGE = 900 * 1000


def set_at(clock, at):
    """Move the clock to `at` (forward only)."""
    assert at >= clock()
    clock.step(at - clock())


# -- a stop outlasts a stale reading ---------------------------------------------


@over_backends
@pytest.mark.parametrize(
    "stop",
    [{"status": "rejected"}, {"utilization": 0.95}, {"utilization": 0.99}],
    ids=["rejected", "at stop_at", "over stop_at"],
)
def test_a_stop_holds_until_its_reset_however_old_the_reading(effort, clock, stop):
    """templet:3161 -- a `rejected` reading, or `utilization >= stop_at`, stops
    the provider until its own `resets_at`, however old: past `max_age_s`
    still held, with `until` the reset; at the reset instant it lifts."""
    record = effort
    placement = declared(record, clock)
    resets = clock() + 4 * HOUR
    reading(record, clock, resets_at=resets, **stop)
    clock.step(MAX_AGE + 1000)
    answer = refused(placement, record, clock)
    assert budget_short(answer).until == resets and answer.until == resets
    clock.step(2 * HOUR)
    assert budget_short(refused(placement, record, clock)).until == resets
    set_at(clock, resets - 1)
    assert budget_short(refused(placement, record, clock)).until == resets
    set_at(clock, resets)
    assert isinstance(refused(placement, record, clock), Fit)


@over_backends
def test_a_stale_stop_per_window_and_the_later_reset(effort, clock):
    """templet:3161 -- each window's stale stop holds alone, the later reset is
    `until`, and a window whose stop has passed its reset no longer counts."""
    record = effort
    placement = declared(record, clock)
    week, five = clock() + 6 * HOUR, clock() + 2 * HOUR
    reading(record, clock, kind="seven_day", status="rejected", resets_at=week)
    reading(record, clock, kind="five_hour", status="rejected", resets_at=five)
    clock.step(HOUR)
    assert budget_short(refused(placement, record, clock)).until == week
    set_at(clock, five)
    assert budget_short(refused(placement, record, clock)).until == week
    set_at(clock, week)
    assert isinstance(refused(placement, record, clock), Fit)


@over_backends
def test_a_newer_reading_that_is_not_a_stop_lifts_it(effort, clock):
    """templet:3161 -- a newer reading of that provider and window that is not
    a stop lifts a stale stop; one of another window or provider does not; a
    newer stop replaces its `until`."""
    record = effort
    placement = declared(record, clock)
    resets = clock() + 6 * HOUR
    reading(record, clock, status="rejected", resets_at=resets)
    clock.step(HOUR)
    reading(record, clock, kind="seven_day", resets_at=resets)
    reading(record, clock, provider="openai", resets_at=resets)
    assert budget_short(refused(placement, record, clock)).until == resets
    reading(record, clock, status="rejected", resets_at=resets + HOUR)
    assert budget_short(refused(placement, record, clock)).until == resets + HOUR
    clock.step(MIN)
    reading(record, clock, utilization=0.2, resets_at=resets)
    assert isinstance(refused(placement, record, clock), Fit)


# -- a stop with no readable reset --------------------------------------------------

NO_RESET = [
    "absent",
    None,
    "soon",
    True,
    "1700000000000",
    0,
    -5,
    [1],
    # a "time" before 2000 is no reset: a relative number, a count, seconds
    # since some start
    1,
    86_400,
    5 * 10**11,
    # past year 9999 in milliseconds: microseconds, nanoseconds, a huge int
    253_402_300_800_000,
    1_700_000_000_000_000,
    1_700_000_000_000_000_000,
    2**62,
]


@over_backends
@pytest.mark.parametrize("resets", NO_RESET, ids=[repr(r) for r in NO_RESET])
def test_a_stop_with_no_readable_reset_holds_until_a_newer_reading(
    effort, clock, resets
):
    """templet:3161 -- a stop whose `resets_at` is absent or not a time can't
    name its end: it holds, however old, with waits true and `until` unset,
    and the Refusal says why and names the provider; a newer reading that is
    not a stop lifts it."""
    record = effort
    placement = declared(record, clock)
    fields = {} if resets == "absent" else {"resets_at": resets}
    reading(record, clock, status="rejected", **fields)
    for age in (0, MAX_AGE + 1000, 30 * HOUR):
        clock.step(age)
        answer = refused(placement, record, clock)
        short = budget_short(answer)
        assert short.until is None and short.waits is True
        assert answer.until is None and answer.waits is True
        assert short.note and "anthropic" in short.note
        assert "no reset time" in answer.text and "newer reading" in answer.text
        assert "anthropic" in answer.text
    reading(record, clock)
    assert isinstance(refused(placement, record, clock), Fit)


@over_backends
def test_a_stop_without_a_reset_beside_one_with_one_holds_without_an_end(effort, clock):
    """templet:3161 -- when one stopped window has a reset and the other has
    none, the provider has no end to name: `until` is unset."""
    record = effort
    placement = declared(record, clock)
    reading(record, clock, kind="five_hour", status="rejected")
    reading(
        record, clock, kind="seven_day", status="rejected", resets_at=clock() + HOUR
    )
    answer = refused(placement, record, clock)
    assert budget_short(answer).until is None and "newer reading" in answer.text


@over_backends
def test_a_stop_with_a_reset_has_no_note(effort, clock):
    """templet:3161 -- the plain words are for a stop with no reset only: a
    stop with a reset, and a warning, carry none."""
    record = effort
    placement = declared(record, clock)
    reading(record, clock, status="rejected", resets_at=clock() + HOUR)
    assert budget_short(refused(placement, record, clock)).note is None
    reading(record, clock, status="allowed_warning")
    agents(record, clock, placement, 2)
    assert budget_short(refused(placement, record, clock)).note is None


# -- a warning that is stale guards nothing -------------------------------------


@over_backends
def test_a_stale_warning_guards_nothing(effort, clock):
    """templet:3161 -- the decision covers a stop only: a warning older than
    `max_age_s` still guards nothing, and one inside it does."""
    record = effort
    placement = declared(record, clock)
    reading(record, clock, status="allowed_warning", resets_at=clock() + 4 * HOUR)
    agents(record, clock, placement, 2)
    assert budget_short(refused(placement, record, clock))
    clock.step(MAX_AGE)
    assert budget_short(refused(placement, record, clock))
    clock.step(1000)
    assert isinstance(refused(placement, record, clock), Fit)


@over_backends
def test_a_warning_lifts_at_its_reset(effort, clock):
    """templet:3161 -- a fresh warning lifts at the exact instant of its
    `resets_at`, with no new reading."""
    record = effort
    placement = declared(record, clock)
    resets = clock() + 5 * MIN
    reading(record, clock, status="allowed_warning", resets_at=resets)
    agents(record, clock, placement, 2)
    set_at(clock, resets - 1)
    assert budget_short(refused(placement, record, clock))
    set_at(clock, resets)
    assert isinstance(refused(placement, record, clock), Fit)


# -- an unrecognised status ------------------------------------------------------

UNKNOWN = ["absent", None, "blocked", "", 3, ["rejected"], True]


@over_backends
@pytest.mark.parametrize("status", UNKNOWN, ids=[repr(s) for s in UNKNOWN])
def test_an_unrecognised_status_is_paced_like_a_warning(effort, clock, status):
    """templet:3161 -- a status that is missing, unknown or of the wrong type
    is paced like `allowed_warning`: the provider takes up to `warning_max`
    of live weight and no more, with waits true and `until` unset; stale, it
    guards nothing."""
    record = effort
    placement = declared(record, clock)
    value = {"provider": "anthropic", "utilization": 0.1}
    if status != "absent":
        value["status"] = status
    record.record_reading("budget", "five_hour", value)
    agents(record, clock, placement, 2)
    answer = refused(placement, record, clock)
    short = budget_short(answer)
    assert short.until is None and short.waits is True and answer.until is None
    clock.step(MAX_AGE + 1000)
    assert isinstance(refused(placement, record, clock), Fit)


@over_backends
@pytest.mark.parametrize(
    "status", ["REJECTED", " rejected ", "Rejected\n", "\trejected"]
)
def test_the_status_is_matched_without_case_or_surrounding_space(effort, clock, status):
    """templet:3161 -- `REJECTED` and `rejected ` are stops."""
    record = effort
    placement = declared(record, clock)
    resets = clock() + HOUR
    reading(record, clock, status=status, resets_at=resets)
    assert budget_short(refused(placement, record, clock)).until == resets


@over_backends
def test_the_other_statuses_are_matched_alike(effort, clock):
    """templet:3161 -- `ALLOWED_WARNING ` paces as a warning, and ` Allowed `
    is allowed: it guards nothing."""
    record = effort
    placement = declared(record, clock)
    reading(record, clock, status=" Allowed ")
    agents(record, clock, placement, 2)
    assert isinstance(refused(placement, record, clock), Fit)
    clock.step(MIN)
    reading(record, clock, status="ALLOWED_WARNING ")
    assert budget_short(refused(placement, record, clock)).until is None


# -- the units --------------------------------------------------------------------


@over_backends
def test_resets_at_is_milliseconds_and_a_seconds_value_is_read_as_seconds(
    effort, clock
):
    """templet:3161 -- `resets_at` is in milliseconds, as the record's clock.
    A value no millisecond time of ours can be (before March 1973) is read as
    seconds, so a stop given in seconds doesn't read as long past and lift:
    `until` is the instant in milliseconds, and the stop lifts there."""
    record = effort
    placement = declared(record, clock)
    resets_ms = clock() + 3 * HOUR
    reading(record, clock, status="rejected", resets_at=resets_ms // 1000)
    clock.step(MAX_AGE + 1000)
    answer = refused(placement, record, clock)
    assert budget_short(answer).until == (resets_ms // 1000) * 1000
    set_at(clock, (resets_ms // 1000) * 1000)
    assert isinstance(refused(placement, record, clock), Fit)


@over_backends
def test_a_warning_given_a_reset_in_seconds_lifts_at_that_instant(effort, clock):
    """templet:3161 -- a warning's reset in seconds is read in seconds too: it
    does not lift at once as a past millisecond time would."""
    record = effort
    placement = declared(record, clock)
    resets = (clock() + 5 * MIN) // 1000
    reading(record, clock, status="allowed_warning", resets_at=resets)
    agents(record, clock, placement, 2)
    assert budget_short(refused(placement, record, clock))
    set_at(clock, resets * 1000)
    assert isinstance(refused(placement, record, clock), Fit)


@over_backends
@pytest.mark.parametrize(
    "used,stops",
    [
        ("0.99", True),
        (" 0.95 ", True),
        ("0.2", False),
        ("1e-1", False),
        ("inf", True),
        ("Infinity", True),
        ("1e999", True),
        ("-inf", False),
    ],
)
def test_a_utilization_given_as_text_is_read(effort, clock, used, stops):
    """templet:3161 -- a numeric `utilization` as text is not ignored: it is
    read as a number against `stop_at`."""
    record = effort
    placement = declared(record, clock)
    resets = clock() + HOUR
    reading(record, clock, utilization=used, resets_at=resets)
    answer = refused(placement, record, clock)
    if stops:
        assert budget_short(answer).until == resets
    else:
        assert isinstance(answer, Fit)


@over_backends
@pytest.mark.parametrize("used", ["high", True, [0.99], "nan", {"a": 1}, "99%"])
def test_a_utilization_that_cannot_be_read_is_paced_like_a_warning(effort, clock, used):
    """templet:3161 -- a `utilization` that is neither a number nor numeric
    text is not ignored as `allowed`: the reading is paced like a warning."""
    record = effort
    placement = declared(record, clock)
    reading(record, clock, utilization=used)
    agents(record, clock, placement, 2)
    assert budget_short(refused(placement, record, clock)).until is None


# -- the read escalates until the provider's windows are seen -------------------------


def readings_read(record, monkeypatch):
    """The sizes of the reads of the readings, as the record answers them."""
    returned = []
    real = record.read

    def spy(kind, /, **kw):
        rows = real(kind, **kw)
        if kind == "readings":
            returned.append(len(rows))
        return rows

    monkeypatch.setattr(record, "read", spy)
    return returned


@over_backends
def test_a_short_history_is_read_within_the_bound(effort, clock, monkeypatch):
    """templet:3161 -- with fewer than `READ_LIMIT` readings, the common
    case, one read of at most `READ_LIMIT` rows sees them all."""
    record = effort
    placement = declared(record, clock)
    resets = clock() + 6 * HOUR
    reading(record, clock, kind="seven_day", status="rejected", resets_at=resets)
    for _ in range(budget.READ_LIMIT // 2):
        reading(record, clock, provider="openai", kind="five_hour")
    returned = readings_read(record, monkeypatch)
    assert budget_short(refused(placement, record, clock)).until == resets
    assert returned == [budget.READ_LIMIT // 2 + 1]


@over_backends
@pytest.mark.parametrize("newer", [1, 2, 3], ids=["under", "at", "over"])
def test_a_stop_under_many_newer_readings_of_another_provider_is_found(
    effort, clock, monkeypatch, newer
):
    """templet:3161 -- a stop is never lost for the readings that come after
    it: under `READ_LIMIT` - 1, `READ_LIMIT` and twice as many newer readings
    of its own window by another provider (whose reader is alive while the
    stopped provider's is quiet), it holds until its reset. When the newest
    `READ_LIMIT` readings hold none of the provider's, the read goes on."""
    record = effort
    placement = declared(record, clock)
    resets = clock() + 6 * HOUR
    reading(record, clock, status="rejected", resets_at=resets)
    count = {1: budget.READ_LIMIT - 1, 2: budget.READ_LIMIT, 3: 2 * budget.READ_LIMIT}
    for _ in range(count[newer]):
        reading(record, clock, provider="openai", kind="five_hour")
    returned = readings_read(record, monkeypatch)
    answer = refused(placement, record, clock)
    assert budget_short(answer).until == resets
    if newer > 1:
        assert max(returned) > budget.READ_LIMIT
    reading(record, clock, utilization=0.1)
    assert isinstance(refused(placement, record, clock), Fit)


@over_backends
def test_a_stop_with_no_reset_under_many_newer_readings_is_found(effort, clock):
    """templet:3161 -- a stop that names no end holds until a newer reading
    of its own, however many readings of others are written after it."""
    record = effort
    placement = declared(record, clock)
    reading(record, clock, status="rejected")
    for _ in range(budget.READ_LIMIT + 5):
        reading(record, clock, provider="openai")
    answer = refused(placement, record, clock)
    assert budget_short(answer).until is None and "newer reading" in answer.text


@over_backends
def test_a_window_with_any_name_is_found(effort, clock):
    """templet:3161 -- a window need not be `five_hour` or `seven_day`: a stop
    of a `monthly` window, absent from the newest `READ_LIMIT` readings of the
    source, is found."""
    record = effort
    placement = declared(record, clock)
    resets = clock() + 6 * HOUR
    reading(record, clock, kind="monthly", status="rejected", resets_at=resets)
    for _ in range(budget.READ_LIMIT + 5):
        reading(record, clock, kind="five_hour", provider="openai")
    assert budget_short(refused(placement, record, clock)).until == resets


@over_backends
def test_a_provider_with_no_reading_pays_for_a_full_read_and_is_never_refused(
    effort, clock, monkeypatch
):
    """templet:3161 -- a provider none of whose readings is among the newest
    `READ_LIMIT` is looked for through the whole history before it is said to
    have none; with none, nothing is guarded."""
    record = effort
    placement = declared(record, clock)
    for _ in range(budget.READ_LIMIT + 5):
        reading(record, clock, provider="openai", status="rejected")
    returned = readings_read(record, monkeypatch)
    assert isinstance(refused(placement, record, clock), Fit)
    assert max(returned) == budget.READ_LIMIT + 5


@over_backends
@pytest.mark.parametrize("name", ["Anthropic", " anthropic ", "ANTHROPIC\n"])
def test_the_provider_is_matched_without_case_or_surrounding_space(effort, clock, name):
    """templet:3161 -- the provider is matched as the status is: a reading
    of `Anthropic` or `anthropic ` is the provider's."""
    record = effort
    placement = declared(record, clock)
    resets = clock() + HOUR
    reading(record, clock, provider=name, status="rejected", resets_at=resets)
    assert budget_short(refused(placement, record, clock)).until == resets


# -- what the warning counts (the live runs of the asked node only) -------------------


@over_backends
def test_a_warning_counts_only_runs_not_holds_on_the_asked_node(effort, clock):
    """templet:3161 -- a warning's live weight is the provider's live runs on
    the node asked: a standing hold or a task's own reservation of the
    provider's budget resource is no run, and a run on another node is not
    this node's."""
    record = effort
    placement = declared(record, clock)
    reading(record, clock, status="allowed_warning")
    room = placement.headroom(now=clock())["laptop"]["budget.anthropic"]
    assert room == 2
    placement.hold("standing", {"budget.anthropic": 1}, now=clock())
    own = make_task(record)
    placement.hold("worker", {"budget.anthropic": 1}, task=own, now=clock())
    other_run(record, clock)
    now = placement.headroom(now=clock())["laptop"]["budget.anthropic"]
    assert now == 2


def other_run(record, clock):
    """A live run of budget weight 1 on another node."""
    record.declare_node("elsewhere", ["harness"], [])
    record.set_capacity("elsewhere", "budget.anthropic", 4, "budget")
    task = make_task(record, needs={"budget.anthropic": 1})
    return record.start_run(
        task, "remote", node="elsewhere", sandbox="harness", fence=None, expect_s=60
    )


# -- only a recognised non-stop lifts a stop -------------------------------------------


def stop(record, clock, **kw):
    resets = clock() + 6 * HOUR
    reading(record, clock, status="rejected", resets_at=resets, **kw)
    return resets


GARBLED = [
    {"status": "blocked"},
    {"status": None},
    {"status": 3},
    {"status": ["allowed"]},
    {"status": "allowed", "utilization": "high"},
    {"status": "allowed", "utilization": True},
    {"status": "allowed_warning", "utilization": [1]},
    {},
]


@over_backends
@pytest.mark.parametrize("value", GARBLED, ids=[repr(v) for v in GARBLED])
def test_a_newer_unrecognised_reading_does_not_lift_a_stop(effort, clock, value):
    """templet:3161 -- an unrecognised or garbled reading is paced like a
    warning and does not lift a stop: the older stop holds until its reset,
    and a recognised `allowed` after it lifts it."""
    record = effort
    placement = declared(record, clock)
    resets = stop(record, clock)
    clock.step(MIN)
    record.record_reading("budget", "five_hour", {"provider": "anthropic", **value})
    answer = refused(placement, record, clock)
    assert budget_short(answer).until == resets
    clock.step(HOUR)
    assert budget_short(refused(placement, record, clock)).until == resets
    reading(record, clock)
    assert isinstance(refused(placement, record, clock), Fit)


@over_backends
def test_a_recognised_warning_below_the_stop_lifts_a_stop(effort, clock):
    """templet:3161 -- `allowed_warning` below `stop_at` is recognised and
    lifts a stop (and then paces); one at `stop_at` is a stop of its own."""
    record = effort
    placement = declared(record, clock)
    stop(record, clock)
    clock.step(MIN)
    reading(record, clock, status="allowed_warning", utilization=0.5)
    assert isinstance(refused(placement, record, clock), Fit)
    agents(record, clock, placement, 2)
    assert budget_short(refused(placement, record, clock)).until is None
    resets = clock() + HOUR
    reading(record, clock, status="allowed_warning", utilization=0.95, resets_at=resets)
    assert budget_short(refused(placement, record, clock)).until == resets


@over_backends
def test_an_unrecognised_reading_over_an_allowed_one_is_paced(effort, clock):
    """templet:3161 -- an unrecognised reading newer than a recognised
    `allowed` is paced like a warning while it is fresh; stale, the `allowed`
    under it is all there is, and guards nothing."""
    record = effort
    placement = declared(record, clock)
    reading(record, clock)
    clock.step(MIN)
    record.record_reading("budget", "five_hour", {"provider": "anthropic"})
    agents(record, clock, placement, 2)
    assert budget_short(refused(placement, record, clock)).until is None
    clock.step(MAX_AGE + 1000)
    assert isinstance(refused(placement, record, clock), Fit)
