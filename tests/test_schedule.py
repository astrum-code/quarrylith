# SPDX-License-Identifier: Apache-2.0
"""The schedule parser (templet:3163, build step 1): `parse`, `Schedule`'s
`within`, `next_open` and `next_fire`, `next_not_before`, and `quarrylith
order schedule`.

The parser reads no record and no clock, so nothing here is run over
`BACKENDS`. Expected instants are written as UTC literals, or built with
`zoneinfo` directly, never with the module under test. A test that needs a
named time zone skips, with the reason, on a host with no zone database."""

import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from quarrylith import cli
from quarrylith.order import schedule as sch
from quarrylith.order.schedule import ScheduleError, next_not_before, parse


def utc(year, month, day, hour=0, minute=0):
    moment = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
    return int(moment.timestamp()) * 1000


def zone(name):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        pytest.skip(f"no zone database entry for {name}")


def local(name, year, month, day, hour=0, minute=0, fold=0):
    """The instant of a wall-clock time in a zone, from `zoneinfo` alone."""
    moment = datetime(year, month, day, hour, minute, tzinfo=zone(name), fold=fold)
    return int(moment.timestamp()) * 1000


def fires(text, after, count):
    got, schedule = [], parse(text)
    for _ in range(count):
        after = schedule.next_fire(after)
        got.append(after)
    return got


REFUSED = [
    ("cron 0 22 * *", "cron: needs five fields, got 4"),
    ("cron 0 22 * * * *", "cron: needs five fields, got 6"),
    ("cron", "cron: needs five fields, got 0"),
    ("cron 60 0 * * *", "cron: minute 60 is past 59"),
    ("cron 0 24 * * *", "cron: hour 24 is past 23"),
    ("cron 0 0 32 * *", "cron: day of month 32 is past 31"),
    ("cron 0 0 0 * *", "cron: day of month 0 is below 1"),
    ("cron 0 0 * 13 *", "cron: month 13 is past 12"),
    ("cron 0 0 * * 8", "cron: day of week 8 is past 7"),
    ("cron 0 0 * * funday", 'cron: day of week "funday" is not a number or a name'),
    ("cron 0 0 * foo *", 'cron: month "foo" is not a number or a name'),
    ("cron */0 0 * * *", "cron: minute step 0 is below 1"),
    ("cron */x 0 * * *", 'cron: minute step "x" is not a number'),
    ("cron 30-10 0 * * *", "cron: minute range 30-10 runs backwards"),
    ("cron 1,,2 0 * * *", "cron: minute has an empty item"),
    ("window 22-6", 'window: "22-6" is not HH:MM-HH:MM'),
    ("window 22:00-6:00", 'window: "22:00-6:00" is not HH:MM-HH:MM'),
    ("window", "window: needs HH:MM-HH:MM"),
    ("window 25:00-06:00", "window: hour 25 is past 23"),
    ("window 22:00-06:61", "window: minute 61 is past 59"),
    ("window 22:00-06:00 someday", 'window: day "someday" is not mon..sun'),
    ("window 22:00-06:00 fri-mon", 'window: days "fri-mon" run backwards'),
    ("tz Mars/Base", 'tz: no zone "Mars/Base"'),
    ("tz ../etc/passwd", 'tz: no zone "../etc/passwd"'),
    ("tz", "tz: needs one zone"),
    ("tz UTC UTC", "tz: needs one zone"),
    ("every day", 'unknown clause "every"'),
    ("cron * * * * *; cron * * * * *", "cron given twice"),
    ("tz UTC; tz UTC", "tz given twice"),
    ("cron * * * * *;", "empty clause"),
    ("; tz UTC", "empty clause"),
]


@pytest.mark.parametrize("text, message", REFUSED)
def test_parse_refuses_naming_the_clause(text, message):
    """templet:3163 -- `parse` refuses a cron of four fields, a minute of 60, a
    day of week of 8, a window `22-6`, an unknown zone, an unknown clause and
    `cron` or `tz` given twice (and more), each naming the clause."""
    with pytest.raises(ScheduleError) as got:
        parse(text)
    assert str(got.value) == message


def test_parse_is_total_over_text_and_refuses_what_is_not_text():
    """templet:3163 -- every input is a `Schedule` or a `ScheduleError`."""
    assert isinstance(ScheduleError("x"), ValueError)
    for junk in (5, b"cron * * * * *", ["cron"], {}):
        with pytest.raises(ScheduleError, match="schedule: not text"):
            parse(junk)


@pytest.mark.parametrize("text", [None, "", "   ", " \t\n"])
def test_an_empty_schedule_is_no_cron_and_no_window(text):
    """templet:3163 -- an empty or null `schedule` is no cron and no window."""
    schedule = parse(text)
    assert schedule.cron is None and schedule.windows == ()
    assert schedule.tz == "UTC"
    assert schedule.within(utc(2026, 1, 1)) is True
    assert schedule.next_open(utc(2026, 1, 1)) == utc(2026, 1, 1)
    assert schedule.next_fire(utc(2026, 1, 1)) is None


def test_the_grammar_reads_names_steps_lists_ranges_and_spacing():
    """templet:3163 -- a field is `*`, a value, a range, a list, or any of
    them with a step; months and days of week have names; sunday is 0 and 7;
    spaces around a clause are ignored."""
    spaced = parse("  cron  */15  0-6/2  1,15  jan-mar,dec  mon-fri ;tz UTC  ")
    minutes, hours, days, months, weekdays = spaced.cron[:5]
    assert minutes == {0, 15, 30, 45}
    assert hours == {0, 2, 4, 6}
    assert days == {1, 15}
    assert months == {1, 2, 3, 12}
    assert weekdays == {1, 2, 3, 4, 5}
    assert parse("cron 5/20 * * * *").cron[0] == {5, 25, 45}
    assert parse("cron * * * * 7").cron[4] == {0}
    assert parse("cron * * * * SUN,Sat").cron[4] == {0, 6}
    assert parse("cron * * * * fri-sun").cron[4] == {5, 6, 0}
    assert parse("cron * * * * 0-7").cron[4] == set(range(7))
    assert parse("cron * * * * *").cron[4] == set(range(7))


def test_a_schedule_is_a_frozen_value():
    """templet:3163 -- `Schedule` is a frozen dataclass: it hashes, compares by
    value, and cannot be changed."""
    one, two = parse("cron 0 22 * * *; tz UTC"), parse("tz UTC;cron 0 22 * * *")
    assert one == two and hash(one) == hash(two)
    with pytest.raises(AttributeError):
        one.tz = "Europe/Berlin"


def test_the_parser_reads_no_clock(monkeypatch):
    """templet:3163 -- the parser is pure: `now` is always passed in."""

    def forbidden(*args, **kw):
        raise AssertionError("the schedule read the clock")

    for name in ("time", "monotonic", "time_ns", "perf_counter"):
        monkeypatch.setattr(time, name, forbidden)
    schedule = parse("cron 0 22 * * mon-fri; window 20:00-23:00; tz UTC")
    assert schedule.next_fire(utc(2026, 1, 9, 21)) == utc(2026, 1, 9, 22)
    assert next_not_before({"schedule": "cron 0 1 * * *"}, finished_at=0)


def test_next_fire_across_a_weekend():
    """templet:3163 -- `cron 0 22 * * mon-fri; tz Europe/Lisbon` from a Friday
    23:00 is Monday 22:00 local, in winter (UTC) and in summer (UTC+1)."""
    text = "cron 0 22 * * mon-fri; tz Europe/Lisbon"
    winter = parse(text).next_fire(local("Europe/Lisbon", 2026, 1, 9, 23))
    assert winter == utc(2026, 1, 12, 22)
    summer = parse(text).next_fire(local("Europe/Lisbon", 2026, 7, 10, 23))
    assert summer == utc(2026, 7, 13, 21)
    # a fire is strictly after: from the fire itself, the next one
    assert parse(text).next_fire(utc(2026, 1, 12, 22)) == utc(2026, 1, 13, 22)
    # and a minute before it, the fire
    assert parse(text).next_fire(utc(2026, 1, 12, 21, 59)) == utc(2026, 1, 12, 22)


@pytest.mark.parametrize(
    "tz, text, after, want",
    [
        # Europe/Berlin springs forward on 2026-03-29 at 02:00 (01:00Z): a fire
        # at 02:30 that day does not exist, and is skipped, not moved
        (
            "Europe/Berlin",
            "cron 30 2 * * *",
            utc(2026, 3, 28, 1, 30),
            [utc(2026, 3, 30, 0, 30), utc(2026, 3, 31, 0, 30)],
        ),
        # every half hour: 02:00 and 02:30 are in the gap, 03:00 CEST follows
        # the half past one
        (
            "Europe/Berlin",
            "cron */30 * * * *",
            utc(2026, 3, 29, 0, 30),
            [utc(2026, 3, 29, 1, 0), utc(2026, 3, 29, 1, 30)],
        ),
        # America/Chicago springs forward on 2026-03-08 at 02:00 (08:00Z)
        (
            "America/Chicago",
            "cron 30 2 * * *",
            utc(2026, 3, 7, 8, 30),
            [utc(2026, 3, 9, 7, 30), utc(2026, 3, 10, 7, 30)],
        ),
        (
            "America/Chicago",
            "cron 0 * * * *",
            utc(2026, 3, 8, 7, 0),
            [utc(2026, 3, 8, 8, 0), utc(2026, 3, 8, 9, 0)],
        ),
    ],
)
def test_next_fire_across_a_spring_forward_gap(tz, text, after, want):
    """templet:3163 -- a fire in a spring-forward gap is skipped."""
    zone(tz)
    assert fires(f"{text}; tz {tz}", after, len(want)) == want


@pytest.mark.parametrize(
    "tz, text, after, want",
    [
        # Europe/Berlin falls back on 2026-10-25 at 03:00 CEST (01:00Z): 02:30
        # happens at 00:30Z and again at 01:30Z, and fires once, at the first
        (
            "Europe/Berlin",
            "cron 30 2 * * *",
            utc(2026, 10, 24, 0, 30),
            [utc(2026, 10, 25, 0, 30), utc(2026, 10, 26, 1, 30)],
        ),
        # every half hour: 02:00 and 02:30 fire once each, at their first
        # instants, and the repeated hour does not fire again; 03:00 CET is next
        (
            "Europe/Berlin",
            "cron */30 * * * *",
            utc(2026, 10, 25, 0, 0),
            [utc(2026, 10, 25, 0, 30), utc(2026, 10, 25, 2, 0)],
        ),
        # America/Chicago falls back on 2026-11-01 at 02:00 CDT (07:00Z)
        (
            "America/Chicago",
            "cron 30 1 * * *",
            utc(2026, 10, 31, 6, 30),
            [utc(2026, 11, 1, 6, 30), utc(2026, 11, 2, 7, 30)],
        ),
        (
            "America/Chicago",
            "cron 0 * * * *",
            utc(2026, 11, 1, 5, 0),
            [utc(2026, 11, 1, 6, 0), utc(2026, 11, 1, 8, 0)],
        ),
    ],
)
def test_next_fire_across_a_fall_back_repeat(tz, text, after, want):
    """templet:3163 -- an ambiguous fire comes once, at its first instant."""
    zone(tz)
    assert fires(f"{text}; tz {tz}", after, len(want)) == want


def test_next_fire_on_a_leap_day_and_the_last_day_of_a_month():
    """templet:3163 -- `29 2` is found years ahead, and the 30th and 31st are
    skipped in the months that lack them."""
    leap = parse("cron 0 0 29 2 *")
    assert fires("cron 0 0 29 2 *", utc(2026, 1, 1), 2) == [
        utc(2028, 2, 29),
        utc(2032, 2, 29),
    ]
    assert leap.next_fire(utc(2028, 2, 29)) == utc(2032, 2, 29)
    assert fires("cron 0 0 31 * *", utc(2026, 1, 31), 6) == [
        utc(2026, 3, 31),
        utc(2026, 5, 31),
        utc(2026, 7, 31),
        utc(2026, 8, 31),
        utc(2026, 10, 31),
        utc(2026, 12, 31),
    ]
    assert fires("cron 0 0 30 * *", utc(2026, 1, 30), 2) == [
        utc(2026, 3, 30),
        utc(2026, 4, 30),
    ]
    assert fires("cron 59 23 31 12 *", utc(2026, 12, 31, 23, 59), 1) == [
        utc(2027, 12, 31, 23, 59)
    ]
    # a leap day in a zone: 18:30 CST is 00:30Z the next day
    zone("America/Chicago")
    text = "cron 30 18 29 2 *; tz America/Chicago"
    assert fires(text, utc(2026, 1, 1), 1) == [utc(2028, 3, 1, 0, 30)]


def test_next_fire_looks_five_years_ahead_and_no_further():
    """templet:3163 -- a cron that never fires (the 30th of February) raises
    `ScheduleError`, as does one that falls beyond five years."""
    with pytest.raises(ScheduleError, match="cron: no fire in 5 years"):
        parse("cron 0 0 30 2 *").next_fire(utc(2026, 1, 1))
    # 2096 is the last leap year before 2104: from just after it, 8 years
    with pytest.raises(ScheduleError, match="cron: no fire in 5 years"):
        parse("cron 0 0 29 2 *").next_fire(utc(2096, 3, 1))


def test_the_day_rule_matches_either_when_both_are_restricted():
    """templet:3163 -- with both day of month and day of week restricted, a day
    matching either one fires; with one starred, both must match."""
    # January 2026: Fridays are the 2nd, 9th, 16th, 23rd and 30th; the 13th
    # is a Tuesday. February's 6th is a Friday.
    either = fires("cron 0 0 13 * fri", utc(2026, 1, 1), 7)
    assert either == [
        utc(2026, 1, 2),
        utc(2026, 1, 9),
        utc(2026, 1, 13),
        utc(2026, 1, 16),
        utc(2026, 1, 23),
        utc(2026, 1, 30),
        utc(2026, 2, 6),
    ]
    assert fires("cron 0 0 13 * *", utc(2026, 1, 1), 2) == [
        utc(2026, 1, 13),
        utc(2026, 2, 13),
    ]
    assert fires("cron 0 0 * * fri", utc(2026, 1, 1), 2) == [
        utc(2026, 1, 2),
        utc(2026, 1, 9),
    ]
    # a step on a day field is a restriction, not a star: every tenth day of
    # the month (the 1st, 11th, 21st, 31st), or a Monday
    stepped = fires("cron 0 0 */10 * mon", utc(2026, 1, 1), 5)
    assert stepped == [
        utc(2026, 1, 5),  # Monday
        utc(2026, 1, 11),  # the 11th is 1 + 10
        utc(2026, 1, 12),  # Monday
        utc(2026, 1, 19),
        utc(2026, 1, 21),  # the 21st
    ]
    # the month still gates both
    assert fires("cron 0 0 13 3 fri", utc(2026, 1, 1), 2) == [
        utc(2026, 3, 6),
        utc(2026, 3, 13),
    ]


def test_a_window_holds_from_its_start_to_its_end_on_the_days_it_opens():
    """templet:3163 -- `window 22:00-06:00 fri` holds Friday 22:00 and Saturday
    05:59, and not Saturday 06:00 nor Thursday 23:00."""
    schedule = parse("window 22:00-06:00 fri")
    assert schedule.within(utc(2026, 1, 9, 22, 0)) is True  # a Friday
    assert schedule.within(utc(2026, 1, 9, 21, 59)) is False
    assert schedule.within(utc(2026, 1, 10, 5, 59)) is True
    assert schedule.within(utc(2026, 1, 10, 5, 59) + 59_999) is True  # 05:59:59
    assert schedule.within(utc(2026, 1, 10, 6, 0)) is False
    assert schedule.within(utc(2026, 1, 8, 23, 0)) is False
    # Friday morning belongs to Thursday's opening, which is not a day it opens
    assert schedule.within(utc(2026, 1, 9, 5, 59)) is False
    day = parse("window 09:00-17:00 mon-fri")
    assert day.within(utc(2026, 1, 5, 9, 0)) and day.within(utc(2026, 1, 5, 16, 59))
    assert not day.within(utc(2026, 1, 5, 17, 0))
    assert not day.within(utc(2026, 1, 10, 12, 0))  # a Saturday
    assert parse("window 00:00-00:00").within(utc(2026, 1, 10, 12, 0))
    assert parse("window 22:00-22:00").within(utc(2026, 1, 10, 21, 59))


def test_two_window_clauses_are_their_union():
    """templet:3163 -- several `window` clauses make one window."""
    schedule = parse("window 09:00-10:00; window 14:00-15:00 mon-fri")
    monday, saturday = (2026, 1, 5), (2026, 1, 10)
    assert schedule.within(utc(*monday, 9, 30))
    assert schedule.within(utc(*monday, 14, 30))
    assert not schedule.within(utc(*monday, 12, 0))
    assert schedule.within(utc(*saturday, 9, 30))
    assert not schedule.within(utc(*saturday, 14, 30))
    assert len(schedule.windows) == 2


def test_next_open_gives_the_next_start():
    """templet:3163 -- `next_open` is the first instant at or after `at` within
    a window: `at` itself when it is inside, else the next start."""
    friday_night = parse("window 22:00-06:00 fri")
    inside = utc(2026, 1, 9, 23, 0)
    assert friday_night.next_open(inside) == inside
    # Saturday 06:00 is outside: the next Friday 22:00
    assert friday_night.next_open(utc(2026, 1, 10, 6, 0)) == utc(2026, 1, 16, 22, 0)
    assert friday_night.next_open(utc(2026, 1, 9, 21, 59)) == utc(2026, 1, 9, 22, 0)
    union = parse("window 09:00-10:00; window 14:00-15:00 mon-fri")
    assert union.next_open(utc(2026, 1, 5, 10, 0)) == utc(2026, 1, 5, 14, 0)
    assert union.next_open(utc(2026, 1, 5, 15, 0)) == utc(2026, 1, 6, 9, 0)
    assert union.next_open(utc(2026, 1, 9, 15, 0)) == utc(2026, 1, 10, 9, 0)


def test_a_window_is_wall_clock_time_in_its_zone():
    """templet:3163 -- a window's times are local: the same schedule opens at
    different instants in winter and summer, and a start in a spring gap
    opens at the gap's end."""
    zone("Europe/Berlin")
    night = parse("window 22:00-06:00; tz Europe/Berlin")
    assert night.next_open(utc(2026, 1, 5, 12)) == utc(2026, 1, 5, 21)
    assert night.next_open(utc(2026, 7, 6, 12)) == utc(2026, 7, 6, 20)
    # Berlin's 29 March is 23 hours long: 02:30 does not exist, 03:00 CEST
    # (01:00Z) is the first wall-clock time past it
    gap = parse("window 02:30-04:00; tz Europe/Berlin")
    assert gap.next_open(utc(2026, 3, 28, 23, 0)) == utc(2026, 3, 29, 1, 0)
    assert gap.within(utc(2026, 3, 29, 1, 0)) and not gap.within(utc(2026, 3, 29, 2, 0))
    # a window wholly inside the gap never opens that day
    inside = parse("window 02:10-02:40; tz Europe/Berlin")
    assert inside.next_open(utc(2026, 3, 28, 23, 0)) == utc(2026, 3, 30, 0, 10)
    # wall-clock time: 05:00Z on the night of the fall back is 06:00 CET
    assert night.within(utc(2026, 10, 25, 3, 59)) and not night.within(
        utc(2026, 10, 25, 5, 0)
    )


def test_a_cron_with_a_window_skips_the_fires_outside_it():
    """templet:3163 -- `cron 0 */6 * * *; window 22:00-06:00` from 12:30 gives
    00:00, not 18:00 and not 22:00, and from 00:30 gives 00:00 the next day (06:00
    is the window's end, excluded); `cron 0 12 * * *; window 22:00-06:00` raises
    `ScheduleError` naming no fire within its window."""
    text = "cron 0 */6 * * *; window 22:00-06:00"
    assert parse(text).next_fire(utc(2026, 1, 5, 12, 30)) == utc(2026, 1, 6, 0, 0)
    assert parse(text).next_fire(utc(2026, 1, 6, 0, 30)) == utc(2026, 1, 7, 0, 0)
    assert parse(text).next_fire(utc(2026, 1, 5, 18, 0)) == utc(2026, 1, 6, 0, 0)
    with pytest.raises(ScheduleError, match="cron: no fire within its window in 5"):
        parse("cron 0 12 * * *; window 22:00-06:00").next_fire(utc(2026, 1, 5))
    # a fire is never moved to the window's start: nothing at 22:00
    every_day = fires("cron 0 */6 * * *; window 22:00-06:00", utc(2026, 1, 5), 3)
    assert every_day == [utc(2026, 1, 6), utc(2026, 1, 7), utc(2026, 1, 8)]
    # the window's days gate the fires too: 02:00 Saturday belongs to Friday
    night = "cron 0 2,23 * * *; window 22:00-06:00 fri"
    assert fires(night, utc(2026, 1, 5), 3) == [
        utc(2026, 1, 9, 23),
        utc(2026, 1, 10, 2),
        utc(2026, 1, 16, 23),
    ]


def test_a_window_gates_a_cron_in_its_zone():
    """templet:3163 -- a fire is judged by its local wall-clock time."""
    zone("Europe/Lisbon")
    text = "cron 30 23 * * *; window 22:00-06:00; tz Europe/Lisbon"
    # 23:30 WEST is 22:30Z
    assert parse(text).next_fire(utc(2026, 7, 6, 12)) == utc(2026, 7, 6, 22, 30)
    text = "cron 30 6 * * *; window 22:00-06:00; tz Europe/Lisbon"
    with pytest.raises(ScheduleError, match="no fire within its window"):
        parse(text).next_fire(utc(2026, 7, 6, 12))


def task(schedule=None, **loop):
    return {"schedule": schedule, "loop": loop or None}


def test_next_not_before_for_a_loop_moves_into_its_window():
    """templet:3163 -- a loop's is the finish plus `interval_s`, moved to its
    window's next opening."""
    looped = task("window 22:00-06:00", max_attempts=5, interval_s=3600)
    # 13:00 is outside: the next opening is 22:00
    assert next_not_before(looped, finished_at=utc(2026, 1, 5, 12)) == utc(
        2026, 1, 5, 22
    )
    # 00:00 is inside: not moved
    assert next_not_before(looped, finished_at=utc(2026, 1, 5, 23)) == utc(2026, 1, 6)
    # 06:30 + 1h is outside again
    assert next_not_before(looped, finished_at=utc(2026, 1, 6, 5, 30)) == utc(
        2026, 1, 6, 22
    )
    plain = task(None, max_attempts=3, interval_s=90)
    assert next_not_before(plain, finished_at=1_000) == 1_000 + 90_000
    assert next_not_before(task(None, max_attempts=2, interval_s=0), finished_at=7) == 7


def test_next_not_before_for_a_cron_is_the_first_fire_after_the_finish():
    """templet:3163 -- a cron task's is the first fire after the finish, so
    fires missed while it could not run give one run, not several."""
    hourly = {**task("cron 0 * * * *"), "not_before": utc(2026, 1, 5, 3)}
    # the run was due at 03:00 and ran at 10:10, seven fires late; it finishes
    # at 10:15 and the next time is 11:00: one run for the seven, never seven
    assert next_not_before(hourly, finished_at=utc(2026, 1, 5, 10, 15)) == utc(
        2026, 1, 5, 11
    )
    nightly = task("cron 0 3 * * *")
    assert next_not_before(nightly, finished_at=utc(2026, 1, 5, 3, 20)) == utc(
        2026, 1, 6, 3
    )
    # a week passes with the machine off: the finish is a week on, the next
    # time is the next 03:00, not a catch-up of the week's
    assert next_not_before(nightly, finished_at=utc(2026, 1, 12, 9)) == utc(
        2026, 1, 13, 3
    )
    # a fire exactly at the finish is not after it
    assert next_not_before(hourly, finished_at=utc(2026, 1, 5, 10, 0)) == utc(
        2026, 1, 5, 11
    )


def test_next_not_before_takes_the_later_of_a_cron_and_a_loop():
    """templet:3163 -- with both, the later of the two; with neither, None."""
    six_hourly = "cron 0 */6 * * *"
    slow = task(six_hourly, max_attempts=9, interval_s=10 * 3600)
    assert next_not_before(slow, finished_at=utc(2026, 1, 5, 1)) == utc(2026, 1, 5, 11)
    quick = task(six_hourly, max_attempts=9, interval_s=3600)
    assert next_not_before(quick, finished_at=utc(2026, 1, 5, 1)) == utc(2026, 1, 5, 6)
    # a cron and a loop and a window: the loop is moved in, the cron is in
    both = task(six_hourly + "; window 22:00-06:00", max_attempts=9, interval_s=60)
    assert next_not_before(both, finished_at=utc(2026, 1, 5, 12)) == utc(2026, 1, 6)
    for neither in (
        task(None),
        {"schedule": None},
        {},
        task("window 22:00-06:00"),
        task("tz Europe/Lisbon"),
        {"schedule": "", "loop": {"max_attempts": 1, "interval_s": 0}},
    ):
        assert next_not_before(neither, finished_at=utc(2026, 1, 5)) is None


def test_next_not_before_refuses_what_parse_refuses():
    """templet:3163 -- a task whose schedule `parse` refuses raises its fault."""
    with pytest.raises(ScheduleError, match="cron: minute 60 is past 59"):
        next_not_before({"schedule": "cron 60 * * * *"}, finished_at=0)


def run(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_order_schedule_prints_the_next_fires_and_openings(capsys):
    """templet:3163 -- `quarrylith order schedule TEXT [--from MS] [--count N]`
    prints the next N fires and window openings, or its `ScheduleError`."""
    text = "cron 0 22 * * mon-fri; window 20:00-23:00; tz UTC"
    start = utc(2026, 1, 9, 23)
    code, out, err = run(
        capsys, "order", "schedule", text, "--from", str(start), "--count", "2"
    )
    assert code == 0 and err == ""
    lines = out.splitlines()
    assert lines == [
        f"fire 2026-01-12T22:00:00+00:00 {utc(2026, 1, 12, 22)}",
        f"fire 2026-01-13T22:00:00+00:00 {utc(2026, 1, 13, 22)}",
        f"open 2026-01-10T20:00:00+00:00 {utc(2026, 1, 10, 20)}",
        f"open 2026-01-11T20:00:00+00:00 {utc(2026, 1, 11, 20)}",
    ]
    code, out, err = run(capsys, "order", "schedule", "cron 60 * * * *")
    assert (code, out) == (2, "")
    assert err == "refused: cron: minute 60 is past 59\n"
    code, out, _ = run(capsys, "order", "schedule", "", "--from", "0")
    assert code == 0 and out.splitlines() == ["no cron", "no window"]
    code, out, _ = run(capsys, "order", "schedule", "cron 0 0 30 2 *", "--from", "0")
    assert code == 2 and out == ""


def test_order_schedule_openings_merge_windows_that_touch(capsys):
    """templet:3163 -- a window that starts where another is open is no new
    opening."""
    text = "window 20:00-22:00; window 21:00-23:00"
    code, out, _ = run(
        capsys,
        "order",
        "schedule",
        text,
        "--from",
        str(utc(2026, 1, 5)),
        "--count",
        "2",
    )
    assert code == 0
    assert out.splitlines() == [
        "no cron",
        f"open 2026-01-05T20:00:00+00:00 {utc(2026, 1, 5, 20)}",
        f"open 2026-01-06T20:00:00+00:00 {utc(2026, 1, 6, 20)}",
    ]


def test_order_schedule_takes_timing_and_debug(capsys):
    """templet:3163 -- `--timing` prints the time per layer and `--debug` what
    was parsed, both on stderr so stdout stays the answer."""
    argv = ["order", "schedule", "cron 0 22 * * *", "--from", "0", "--count", "1"]
    plain = run(capsys, *argv)
    code, out, err = run(capsys, *argv, "--timing", "--debug")
    assert code == 0 and out == plain[1]
    assert "timing: parse" in err and "next" in err
    assert "schedule: Schedule(" in err
    assert "timing" not in plain[2]


def test_order_schedule_shows_local_offsets(capsys):
    """templet:3163 -- an instant is shown in the schedule's zone."""
    zone("Europe/Lisbon")
    code, out, _ = run(
        capsys,
        *("order", "schedule", "cron 0 22 * * *; tz Europe/Lisbon"),
        *("--from", str(utc(2026, 7, 6, 12)), "--count", "1"),
    )
    assert code == 0
    assert (
        out.splitlines()[0] == f"fire 2026-07-06T22:00:00+01:00 {utc(2026, 7, 6, 21)}"
    )


def test_the_module_names_what_the_page_names():
    """templet:3163 -- `Schedule`, `parse`, `ScheduleError` and
    `next_not_before` are the module's interface."""
    for name in ("Schedule", "parse", "ScheduleError", "next_not_before"):
        assert hasattr(sch, name)
    for method in ("within", "next_open", "next_fire"):
        assert callable(getattr(sch.Schedule, method))
