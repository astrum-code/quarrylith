# SPDX-License-Identifier: Apache-2.0
"""A person's command clears a provider's stop that names no reset
(templet:3161): `quarrylith alloc clear URL PROVIDER --why TEXT` records a
`cleared` reading in the window of each such stop; a stop with a readable
future reset is not cleared by it; a later stop reading holds again; `place`
and `alloc show` name the command. Every test runs over every backend in
BACKENDS."""

import json

import pytest
from backends import one_writer, over_backends
from test_allocator_budget import HOUR, MIN, agents, budget_short, reading, refused
from test_allocator_commands import alloc, head_of, peek
from test_allocator_host import declared, sample

from quarrylith import cli
from quarrylith.allocator import Fit, budget
from quarrylith.allocator.seam import _when

DOCUMENT = sample()


def clear(record, clock, provider="anthropic", kind="five_hour", **more):
    """A `cleared` reading, as the command writes it."""
    value = {
        "provider": provider,
        "status": "cleared",
        "by": "alice",
        "why": "the reader is gone",
    }
    value.update(more)
    return record.record_reading("budget", kind, value)


def allowance_of(record, now, provider="anthropic"):
    return budget.allowance(record, DOCUMENT, "laptop", provider, now=now)


# -- what a clear lifts -------------------------------------------------------------


@over_backends
def test_a_clear_lifts_a_stop_with_no_reset_and_a_later_stop_holds_again(effort, clock):
    """templet:3161 -- a `cleared` reading is a recognised status that lifts a
    stop that names no end; a stop reading after it holds again (a clear is
    not a pass), and a second clear lifts that."""
    record = effort
    placement = declared(record, clock)
    reading(record, clock, status="rejected")
    assert budget_short(refused(placement, record, clock)).until is None
    clock.step(MIN)
    clear(record, clock)
    assert isinstance(refused(placement, record, clock), Fit)
    clock.step(MIN)
    reading(record, clock, status="rejected")
    assert budget_short(refused(placement, record, clock)).until is None
    clock.step(MIN)
    clear(record, clock, by="bob", why="again")
    assert isinstance(refused(placement, record, clock), Fit)


@over_backends
def test_a_clear_does_not_go_stale(effort, clock):
    """templet:3161 -- a clear holds as long as the stop it lifted would
    have: however old, until a later stop reading."""
    record = effort
    placement = declared(record, clock)
    reading(record, clock, status="rejected")
    clear(record, clock)
    clock.step(40 * HOUR)
    assert isinstance(refused(placement, record, clock), Fit)


@over_backends
def test_a_clear_never_lifts_a_stop_with_a_future_reset(effort, clock):
    """templet:3161 -- a stop that names a readable future reset holds until
    it, whatever a clear says: the user's rule is that a stop holds until its
    own reset. After the reset it lifts as ever."""
    record = effort
    placement = declared(record, clock)
    resets = clock() + 6 * HOUR
    reading(record, clock, status="rejected", resets_at=resets)
    clear(record, clock)
    clock.step(MIN)
    clear(record, clock, by="bob")
    answer = refused(placement, record, clock)
    assert budget_short(answer).until == resets
    clock.step(6 * HOUR)
    assert isinstance(refused(placement, record, clock), Fit)


@over_backends
def test_a_clear_lifts_the_window_it_is_for_and_no_other(effort, clock):
    """templet:3161 -- a clear is a reading of one window: the other window's
    stop with a future reset still holds, and says so; another provider's
    clear lifts nothing."""
    record = effort
    placement = declared(record, clock)
    week = clock() + 5 * HOUR
    reading(record, clock, kind="five_hour", status="rejected")
    reading(record, clock, kind="seven_day", status="rejected", resets_at=week)
    clear(record, clock, provider="openai")
    assert budget_short(refused(placement, record, clock)).until is None
    clear(record, clock, kind="seven_day")
    assert budget_short(refused(placement, record, clock)).until is None
    clear(record, clock, kind="five_hour")
    assert budget_short(refused(placement, record, clock)).until == week


@over_backends
def test_a_clear_with_no_stop_before_it_does_nothing(effort, clock):
    """templet:3161 -- a clear with no stop before it lifts nothing: not a
    warning's pacing, and not an unrecognised reading's."""
    record = effort
    placement = declared(record, clock)
    clear(record, clock)
    assert isinstance(refused(placement, record, clock), Fit)
    reading(record, clock, status="allowed_warning")
    agents(record, clock, placement, 2)
    clear(record, clock)
    assert budget_short(refused(placement, record, clock)).until is None
    clock.step(MIN)
    record.record_reading("budget", "five_hour", {"provider": "anthropic"})
    assert budget_short(refused(placement, record, clock)).until is None


@over_backends
@pytest.mark.parametrize(
    "more",
    [{"by": ""}, {"by": None}, {"why": ""}, {"why": 3}, {"by": ["a"]}],
    ids=["no who", "who none", "no why", "why a number", "who a list"],
)
def test_a_clear_that_names_no_one_or_no_reason_lifts_nothing(effort, clock, more):
    """templet:3161 -- a clear says who and why or it is not recognised: it is
    paced like any garbled reading and the stop holds."""
    record = effort
    placement = declared(record, clock)
    reading(record, clock, status="rejected")
    clear(record, clock, **more)
    assert budget_short(refused(placement, record, clock)).until is None
    bare = {"provider": "anthropic", "status": "cleared"}
    record.record_reading("budget", "five_hour", bare)
    assert budget_short(refused(placement, record, clock)).until is None


@over_backends
def test_the_status_of_a_clear_is_matched_without_case_or_space(effort, clock):
    """templet:3161 -- as any status, `Cleared ` is a clear."""
    record = effort
    placement = declared(record, clock)
    reading(record, clock, status="rejected")
    clear(record, clock, status=" Cleared ")
    assert isinstance(refused(placement, record, clock), Fit)


@over_backends
def test_the_refusal_names_the_command_for_a_stop_with_no_reset(effort, clock):
    """templet:3161 -- the refusal of a stop that names no end says the
    command that clears it; a stop with a reset does not."""
    record = effort
    placement = declared(record, clock)
    reading(record, clock, status="rejected")
    answer = refused(placement, record, clock)
    assert "quarrylith alloc clear" in answer.text and "anthropic" in answer.text
    clear(record, clock)
    reading(record, clock, status="rejected", resets_at=clock() + HOUR)
    assert "alloc clear" not in refused(placement, record, clock).text


# -- the command -------------------------------------------------------------------


def stopped(record, clock, **more):
    declared(record, clock)
    reading(record, clock, status="rejected", **more)


def readings(opener):
    return peek(opener, lambda r: r.read("readings", source="budget"))


@over_backends
def test_the_command_records_a_clear_with_who_and_why(
    backend, effort, url, clock, capsys, opener
):
    """templet:3161 -- `alloc clear URL PROVIDER --why TEXT` writes one
    reading of the stopped window, `status` `cleared`, with who (the actor)
    and why, through the shape the budget reads; the provider is lifted, and
    the event is the actor's."""
    record = effort
    stopped(record, clock)
    now = clock()
    one_writer(backend, record)
    code, out, err = alloc(
        capsys,
        *("clear", url, "anthropic", "--why", "the reader is gone"),
        *("--actor", "bob", "--now", str(now)),
    )
    assert (code, err) == (0, "")
    assert "cleared anthropic five_hour" in out
    assert "bob" in out and "the reader is gone" in out
    stop, made = readings(opener)
    assert made["source"] == "budget" and made["kind"] == "five_hour"
    assert made["value"] == {
        "provider": "anthropic",
        "status": "cleared",
        "by": "bob",
        "why": "the reader is gone",
    }
    assert peek(opener, lambda r: allowance_of(r, now)) == (None, None)
    (event,) = [
        e
        for e in peek(opener, lambda r: r.dump()["journal"])
        if e["kind"] == "reading" and e["subject"] == "reading:budget"
    ][-1:]
    assert event["actor"] == "bob" and event["via"] == "cli"
    assert stop["value"]["status"] == "rejected"


@over_backends
def test_the_command_takes_the_callers_name_by_default(
    backend, effort, url, clock, capsys, opener
):
    """templet:3161 -- the actor is the caller's, as the record's actor rule
    and every command have it: no flag, the user the command line names."""
    record = effort
    stopped(record, clock)
    one_writer(backend, record)
    code, out, err = alloc(capsys, "clear", url, "anthropic", "--why", "gone")
    assert code == 0, err
    assert readings(opener)[-1]["value"]["by"] == cli._actor()


@over_backends
def test_the_command_refuses_a_stop_with_a_future_reset_and_names_it(
    backend, effort, url, clock, capsys, opener
):
    """templet:3161 -- a stop with a readable future reset is not cleared: the
    command refuses (exit 2), naming the reset, and writes nothing."""
    record = effort
    resets = clock() + 5 * HOUR
    stopped(record, clock, resets_at=resets)
    now = clock()
    one_writer(backend, record)
    mark = head_of(opener)
    code, out, err = alloc(
        capsys, "clear", url, "anthropic", "--why", "try", "--now", str(now)
    )
    assert code == 2 and out == ""
    assert err.startswith("refused: ") and _when(resets) in err
    assert "anthropic five_hour" in err
    assert head_of(opener) == mark


@over_backends
def test_the_command_says_there_is_nothing_to_clear_and_writes_nothing(
    backend, effort, url, clock, capsys, opener
):
    """templet:3161 -- a provider with no stop (no reading, an `allowed`, a
    warning, a stop whose reset has passed, a stop already cleared, or a
    provider nobody declared) is "nothing to clear", exit 0, and nothing is
    written."""
    record = effort
    declared(record, clock)
    now = clock()
    one_writer(backend, record)
    mark = head_of(opener)

    def nothing(provider="anthropic"):
        code, out, err = alloc(
            capsys, "clear", url, provider, "--why", "x", "--now", str(clock())
        )
        assert (code, err) == (0, "")
        assert out.startswith("nothing to clear") and provider in out
        assert head_of(opener) == mark

    nothing()
    nothing("nobody")

    def add(**kw):
        nonlocal mark
        r = opener()
        reading(r, clock, **kw)
        r.close()
        mark = head_of(opener)

    add()
    nothing()
    add(status="allowed_warning")
    nothing()
    add(status="rejected", resets_at=now + 2 * MIN)
    clock.step(3 * MIN)
    nothing()


@over_backends
def test_the_command_clears_each_window_with_no_reset_and_says_what_still_holds(
    backend, effort, url, clock, capsys, opener
):
    """templet:3161 -- a stop in several windows: each with no reset is
    cleared, one reading each; a window with a future reset still holds, and
    the command says so, naming its reset. With only held windows it refuses.
    Run again it says there is nothing to clear."""
    record = effort
    declared(record, clock)
    week = clock() + 5 * HOUR
    reading(record, clock, kind="five_hour", status="rejected")
    reading(record, clock, kind="seven_day", status="rejected", resets_at=week)
    reading(record, clock, kind="monthly", status="rejected")
    now = clock()
    one_writer(backend, record)
    code, out, err = alloc(
        capsys, "clear", url, "anthropic", "--why", "gone", "--now", str(now)
    )
    assert (code, err) == (0, "")
    assert "cleared anthropic five_hour" in out
    assert "cleared anthropic monthly" in out
    assert f"anthropic seven_day: still held until {_when(week)}" in out
    made = [r for r in readings(opener) if r["value"]["status"] == "cleared"]
    assert sorted(r["kind"] for r in made) == ["five_hour", "monthly"]
    assert peek(opener, lambda r: allowance_of(r, now)) == (0, week)
    code, out, err = alloc(
        capsys, "clear", url, "anthropic", "--why", "gone", "--now", str(now)
    )
    assert code == 2 and "seven_day" in err and _when(week) in err
    assert len([r for r in readings(opener) if r["value"]["status"] == "cleared"]) == 2


@over_backends
def test_a_later_stop_after_the_command_holds_again_and_can_be_cleared(
    backend, effort, url, clock, capsys, opener
):
    """templet:3161 -- the command's clear is no pass: a stop reading after
    it holds, and the command clears that."""
    record = effort
    stopped(record, clock)
    one_writer(backend, record)
    now = str(clock())
    assert alloc(capsys, "clear", url, "anthropic", "--why", "a", "--now", now)[0] == 0
    r = opener()
    reading(r, clock, status="rejected")
    r.close()
    assert peek(opener, lambda r: allowance_of(r, int(now))) == (0, None)
    code, out, err = alloc(
        capsys, "clear", url, "anthropic", "--why", "b", "--now", now
    )
    assert code == 0 and "cleared anthropic five_hour" in out
    assert peek(opener, lambda r: allowance_of(r, int(now))) == (None, None)


@over_backends
def test_the_command_matches_the_provider_without_case_or_space(
    backend, effort, url, clock, capsys, opener
):
    """templet:3161 -- as the readings do."""
    record = effort
    stopped(record, clock)
    one_writer(backend, record)
    code, out, err = alloc(
        capsys, "clear", url, " Anthropic ", "--why", "gone", "--now", str(clock())
    )
    assert code == 0, err
    assert out.startswith("cleared anthropic five_hour (by ")
    assert readings(opener)[-1]["value"]["provider"] == "anthropic"


@over_backends
def test_the_command_needs_a_reason(backend, effort, url, clock, capsys, opener):
    """templet:3161 -- `--why` is required and is neither empty nor longer
    than 300 characters; each refusal writes nothing."""
    stopped(effort, clock)
    one_writer(backend, effort)
    mark = head_of(opener)
    with pytest.raises(SystemExit) as raised:
        cli.main(["alloc", "clear", url, "anthropic"])
    assert raised.value.code == 2
    capsys.readouterr()
    for why in ("", "   ", "x" * 301):
        code, out, err = alloc(capsys, "clear", url, "anthropic", "--why", why)
        assert code == 2 and err.startswith("refused: ") and "--why" in err
    assert head_of(opener) == mark


@over_backends
def test_the_command_with_no_document_refuses(
    backend, record, url, clock, capsys, opener
):
    """templet:3161 -- no document declared, no budget to clear: refused,
    nothing written."""
    one_writer(backend, record)
    mark = head_of(opener)
    code, out, err = alloc(capsys, "clear", url, "anthropic", "--why", "x")
    assert code == 2 and "no allocation document" in err
    assert head_of(opener) == mark


@over_backends
def test_the_command_takes_timing_and_debug(
    backend, effort, url, clock, capsys, opener
):
    """templet:3161 -- `--timing` prints the time per layer on stderr, and
    `--debug` each read and the one write, leaving stdout as the same call
    without them prints."""
    record = effort
    stopped(record, clock)
    one_writer(backend, record)
    now = str(clock())
    argv = ["clear", url, "anthropic", "--why", "x", "--now", now]

    def stop_again():
        r = opener()
        reading(r, clock, status="rejected")
        r.close()

    code, plain, err = alloc(capsys, *argv)
    assert (code, err) == (0, "")
    stop_again()
    code, out, err = alloc(capsys, *argv, "--timing")
    assert code == 0 and out == plain
    assert "timing: store " in err and " probe " in err
    stop_again()
    code, out, err = alloc(capsys, *argv, "--debug")
    assert code == 0 and out == plain
    assert any(x.startswith("read: ") for x in err.splitlines())
    wrote = [x.split("(")[0] for x in err.splitlines() if x.startswith("write: ")]
    assert wrote == ["write: record_reading"]


# -- alloc show ----------------------------------------------------------------------


@over_backends
def test_show_names_the_command_for_a_stop_with_no_reset(
    backend, effort, url, clock, capsys
):
    """templet:3161 -- `alloc show` lists a stop with no reset time and names
    the command that clears it; a stop with a reset shows when it ends, with
    no command."""
    record = effort
    resets = clock() + 5 * HOUR
    declared(record, clock)
    reading(record, clock, kind="five_hour", status="rejected")
    reading(record, clock, kind="seven_day", status="rejected", resets_at=resets)
    now = clock()
    one_writer(backend, record)
    code, out, err = alloc(capsys, "show", url, "--now", str(now))
    assert (code, err) == (0, "")
    lines = [x for x in out.splitlines() if x.startswith("budget ")]
    assert len(lines) == 2
    five = next(x for x in lines if "five_hour" in x)
    assert "stopped, no reset time" in five
    assert "quarrylith alloc clear" in five and "anthropic" in five
    week = next(x for x in lines if "seven_day" in x)
    assert f"stopped until {_when(resets)}" in week and "alloc clear" not in week
    code, out, _ = alloc(capsys, "show", url, "--now", str(now), "--json")
    got = json.loads(out)["budget"]
    assert {(b["provider"], b["window"], b["state"]) for b in got} == {
        ("anthropic", "five_hour", "stop"),
        ("anthropic", "seven_day", "stop"),
    }
    assert {b["resets"] for b in got} == {None, resets}


@over_backends
def test_show_shows_a_clear_in_force_with_who_and_why(
    backend, effort, url, clock, capsys
):
    """templet:3161 -- a clear in force (one that lifted a stop, with none
    after it) shows with who, when and why; a stop after it shows as a stop
    again; a clear with no stop before it, or against a stop with a reset,
    shows nothing."""
    record = effort
    declared(record, clock)
    clear(record, clock, kind="monthly")
    reading(record, clock, status="rejected")
    clear(record, clock, by="bob", why="reader retired")
    now = clock()
    one_writer(backend, record)
    code, out, err = alloc(capsys, "show", url, "--now", str(now))
    lines = [x for x in out.splitlines() if x.startswith("budget ")]
    assert len(lines) == 1, lines
    assert "five_hour" in lines[0] and "cleared by bob" in lines[0]
    assert "reader retired" in lines[0] and _when(now) in lines[0]
    code, out, _ = alloc(capsys, "show", url, "--now", str(now), "--json")
    (entry,) = json.loads(out)["budget"]
    assert (entry["state"], entry["by"], entry["why"]) == (
        "cleared",
        "bob",
        "reader retired",
    )


@over_backends
def test_show_with_no_stop_shows_no_budget_line(backend, effort, url, clock, capsys):
    """templet:3161 -- no stop and no clear in force: no `budget` line, and
    an empty list in the JSON."""
    record = effort
    declared(record, clock)
    reading(record, clock)
    one_writer(backend, record)
    code, out, err = alloc(capsys, "show", url, "--now", str(clock()))
    assert code == 0 and "budget " not in out.replace("budget.", "")
    code, out, _ = alloc(capsys, "show", url, "--now", str(clock()), "--json")
    assert json.loads(out)["budget"] == []
