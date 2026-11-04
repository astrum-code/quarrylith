# SPDX-License-Identifier: Apache-2.0
"""A task's schedule: a cron expression, windows and a zone, as text.

    schedule := clause (";" clause)*
    clause   := "cron" MIN HOUR DOM MON DOW
              | "window" HH:MM "-" HH:MM [DAYS]
              | "tz" ZONE

"Once at or after a time" is the record's `not_before` and a loop its
`loop`; this holds what they cannot say. Every time is milliseconds since the
epoch, as the record's are, and the module reads no clock: `now` is always
passed in. A cron and a window are read in wall-clock time in the schedule's
zone. A local time that does not exist (a spring-forward gap) is skipped by a
cron, and one that happens twice (fall-back) fires once, at its first instant.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from typing import NamedTuple
from zoneinfo import ZoneInfo, available_timezones

HORIZON_YEARS = 5  # how far ahead a cron's next fire is looked for

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MS = timedelta(milliseconds=1)
_MINUTE = timedelta(minutes=1)
_DAY = timedelta(days=1)

_NUMBER = re.compile(r"[0-9]+")
_RANGE_OF_TIMES = re.compile(r"([0-9]{2}):([0-9]{2})-([0-9]{2}):([0-9]{2})")

_MONTHS = dict(
    zip("jan feb mar apr may jun jul aug sep oct nov dec".split(), range(1, 13))
)
_DAYS = dict(zip("sun mon tue wed thu fri sat".split(), range(7)))


class ScheduleError(ValueError):
    """A schedule the grammar refuses, or one that never fires; the message
    names the clause and the fault."""


class Cron(NamedTuple):
    """A cron's five fields, expanded to sets (days of week 0-6, Sunday 0),
    and whether the day of month and the day of week were written `*`."""

    minutes: frozenset
    hours: frozenset
    days: frozenset
    months: frozenset
    weekdays: frozenset
    days_any: bool
    weekdays_any: bool

    def matches_day(self, day):
        if day.month not in self.months:
            return False
        of_month = day.day in self.days
        of_week = _weekday(day) in self.weekdays
        if self.days_any or self.weekdays_any:
            return of_month and of_week
        return of_month or of_week


@dataclass(frozen=True)
class Schedule:
    cron: Cron | None = None  # the five fields, expanded to sets
    windows: tuple = ()  # ((start_min, end_min, days), ...), local minutes
    tz: str = "UTC"

    def within(self, at):
        """True when `at` is in a window, or there is no window."""
        if not self.windows:
            return True
        return _in_windows(self.windows, _local(at, _zone(self.tz)))

    def next_open(self, at):
        """The first instant >= `at` within a window, whichever clause it is
        written in: `at` itself when it is inside one, or when there is
        none."""
        if self.within(at):
            return at
        zone = _zone(self.tz)
        day = _local(at, zone).date()
        for _ in range(_DAYS_TO_AN_OPENING):
            opens = [
                _first_instant(datetime.combine(day, _clock(start)), zone)
                for start, _end, days in self.windows
                if _weekday(day) in days
            ]
            opens = [o for o in opens if o >= at and self.within(o)]
            if opens:
                return min(opens)
            day += _DAY
        raise ScheduleError("window: never opens")

    def next_fire(self, after):
        """The first cron minute > `after` that is within a window; None when
        there is no cron."""
        if self.cron is None:
            return None
        zone = _zone(self.tz)
        day = _local(after, zone).date() - _DAY
        last = _years_on(day, HORIZON_YEARS)
        times = [
            (h, m) for h in sorted(self.cron.hours) for m in sorted(self.cron.minutes)
        ]
        while day <= last:
            if self.cron.matches_day(day):
                for hour, minute in times:
                    wall = datetime.combine(day, time(hour, minute))
                    if self.windows and not _in_windows(self.windows, wall):
                        continue
                    fire = _instant(wall, zone)
                    if fire is not None and fire > after:
                        return fire
            day += _DAY
        within = " within its window" if self.windows else ""
        raise ScheduleError(f"cron: no fire{within} in {HORIZON_YEARS} years")

    def local_iso(self, at):
        """The instant `at` as this schedule's zone shows it, ISO 8601."""
        return (_EPOCH + at * _MS).astimezone(_zone(self.tz)).isoformat()

    def next_start(self, after):
        """The first start of a window strictly after `after`, whether or not
        another window is open then; None when there is no window."""
        if not self.windows:
            return None
        zone = _zone(self.tz)
        day = _local(after, zone).date()
        for _ in range(_DAYS_TO_AN_OPENING):
            starts = [
                _first_instant(datetime.combine(day, _clock(start)), zone)
                for start, _end, days in self.windows
                if _weekday(day) in days
            ]
            starts = [s for s in starts if s > after and self.within(s)]
            if starts:
                return min(starts)
            day += _DAY
        raise ScheduleError("window: never opens")


_DAYS_TO_AN_OPENING = 400  # a window with days opens within a week; a margin


def parse(text):
    """The schedule in `text`; `ScheduleError` on any fault. An empty or null
    text is no cron and no window."""
    if text is None:
        return Schedule()
    if not isinstance(text, str):
        raise ScheduleError(f"schedule: not text: {type(text).__name__}")
    if not text.strip():
        return Schedule()
    cron, tz, windows = None, None, []
    for raw in text.split(";"):
        clause = raw.strip()
        if not clause:
            raise ScheduleError("empty clause")
        word, _, rest = clause.partition(" ")
        rest = rest.strip()
        if word == "cron":
            if cron is not None:
                raise ScheduleError("cron given twice")
            cron = _cron(rest)
        elif word == "window":
            windows.append(_window(rest))
        elif word == "tz":
            if tz is not None:
                raise ScheduleError("tz given twice")
            tz = _tz(rest)
        else:
            raise ScheduleError(f'unknown clause "{word}"')
    return Schedule(cron=cron, windows=tuple(windows), tz=tz or "UTC")


def next_not_before(task, *, finished_at):
    """What the services write into a task's `not_before` after a run that
    finished at `finished_at`: a cron's next fire, a loop's finish plus its
    interval moved into the window, the later of the two when it has both, and
    None when it has neither. Fires missed while the task could not run give
    one run: the next one is computed from the finish."""
    schedule = parse(task.get("schedule"))
    times = []
    if schedule.cron is not None:
        times.append(schedule.next_fire(finished_at))
    loop = task.get("loop") or {}
    interval_s = loop.get("interval_s", 0)
    if interval_s > 0 or loop.get("max_attempts", 1) > 1:
        times.append(schedule.next_open(finished_at + interval_s * 1000))
    return max(times) if times else None


def openings(schedule, after, count):
    """The next `count` times the schedule's window opens after `after`: the
    start of a window that is not open already."""
    found = []
    while len(found) < count:
        after = schedule.next_start(after)
        if after is None:
            break
        if not schedule.within(after - 60_000):
            found.append(after)
    return found


# --- clauses ---------------------------------------------------------------


def _cron(rest):
    fields = rest.split()
    if len(fields) != 5:
        raise ScheduleError(f"cron: needs five fields, got {len(fields)}")
    minutes = _field(fields[0], "minute", 0, 59)
    hours = _field(fields[1], "hour", 0, 23)
    days = _field(fields[2], "day of month", 1, 31)
    months = _field(fields[3], "month", 1, 12, _MONTHS)
    weekdays = _field(fields[4], "day of week", 0, 7, _DAYS)
    return Cron(
        minutes,
        hours,
        days,
        months,
        frozenset(day % 7 for day in weekdays),
        days_any=fields[2].startswith("*"),
        weekdays_any=fields[4].startswith("*"),
    )


def _field(text, what, least, most, names=None):
    """The values a cron field names, from `least` to `most`."""
    values = set()
    for item in text.split(","):
        if not item:
            raise ScheduleError(f"cron: {what} has an empty item")
        span, slash, step = item.partition("/")
        if slash:
            step = _value(step, f"{what} step", 1, None)
        if span == "*":
            low, high = least, (6 if what == "day of week" else most)
        else:
            low, dash, high = span.partition("-")
            low = _value(low, what, least, most, names)
            if dash:
                high = _value(high, what, least, most, names, ends_range=True)
                if low > high:
                    raise ScheduleError(f"cron: {what} range {span} runs backwards")
            else:
                high = most if slash else low
        values.update(range(low, high + 1, step or 1))
    return frozenset(values)


def _value(text, what, least, most, names=None, ends_range=False):
    if names and text.lower() in names:
        value = names[text.lower()]
        return 7 if ends_range and what == "day of week" and value == 0 else value
    if not _NUMBER.fullmatch(text):
        named = " or a name" if names else ""
        raise ScheduleError(f'cron: {what} "{text}" is not a number{named}')
    value = int(text)
    if value < least:
        raise ScheduleError(f"cron: {what} {value} is below {least}")
    if most is not None and value > most:
        raise ScheduleError(f"cron: {what} {value} is past {most}")
    return value


def _window(rest):
    first, _, days = rest.partition(" ")
    if not first:
        raise ScheduleError("window: needs HH:MM-HH:MM")
    got = _RANGE_OF_TIMES.fullmatch(first)
    if not got:
        raise ScheduleError(f'window: "{first}" is not HH:MM-HH:MM')
    from_hour, from_minute, to_hour, to_minute = (int(g) for g in got.groups())
    for hour, minute in ((from_hour, from_minute), (to_hour, to_minute)):
        if hour > 23:
            raise ScheduleError(f"window: hour {hour} is past 23")
        if minute > 59:
            raise ScheduleError(f"window: minute {minute} is past 59")
    return (
        from_hour * 60 + from_minute,
        to_hour * 60 + to_minute,
        _window_days(days.replace(" ", "")),
    )


def _window_days(text):
    if not text:
        return frozenset(range(7))
    days = set()
    for item in text.split(","):
        low, dash, high = item.partition("-")
        for name in (low, high) if dash else (low,):
            if name.lower() not in _DAYS:
                raise ScheduleError(f'window: day "{name}" is not mon..sun')
        start = _DAYS[low.lower()]
        end = _DAYS[high.lower()] if dash else start
        end = 7 if dash and end == 0 else end
        if start > end:
            raise ScheduleError(f'window: days "{item}" run backwards')
        days.update(day % 7 for day in range(start, end + 1))
    return frozenset(days)


def _tz(rest):
    names = rest.split()
    if len(names) != 1:
        raise ScheduleError("tz: needs one zone")
    try:
        _zone(names[0])
    except (ValueError, OSError, KeyError):
        raise ScheduleError(f'tz: no zone "{names[0]}"') from None
    return names[0]


# --- time ------------------------------------------------------------------


@lru_cache(maxsize=None)
def _known_zones():
    return available_timezones()


@lru_cache(maxsize=None)
def _zone(name):
    if name == "UTC":
        return timezone.utc
    zone = ZoneInfo(name)  # a missing zone: ZoneInfoNotFoundError, a KeyError
    if name not in _known_zones():  # a filesystem that ignores case
        raise KeyError(name)
    return zone


def _weekday(day):
    """0 for Sunday .. 6 for Saturday, as cron has it."""
    return (day.weekday() + 1) % 7


def _clock(minutes):
    return time(minutes // 60, minutes % 60)


def _local(at, zone):
    """The wall-clock time in `zone` at the instant `at`."""
    return (_EPOCH + at * _MS).astimezone(zone).replace(tzinfo=None)


def _instant(wall, zone):
    """The instant of a wall-clock time in `zone`: its first one when it
    happens twice, None when it does not happen."""
    moment = wall.replace(tzinfo=zone, fold=0).astimezone(timezone.utc)
    if moment.astimezone(zone).replace(tzinfo=None) != wall:
        return None
    return (moment - _EPOCH) // _MS


def _first_instant(wall, zone):
    """The instant of the first wall-clock time from `wall` that happens."""
    while True:
        at = _instant(wall, zone)
        if at is not None:
            return at
        wall += _MINUTE


def _in_windows(windows, wall):
    minute = wall.hour * 60 + wall.minute
    today = _weekday(wall.date())
    yesterday = (today + 6) % 7
    for start, end, days in windows:
        if start < end:
            if start <= minute < end and today in days:
                return True
        elif (minute >= start and today in days) or (
            minute < end and yesterday in days
        ):
            return True
    return False


def _years_on(day, years):
    try:
        return day.replace(year=day.year + years)
    except ValueError:  # the 29th of February
        return date(day.year + years, 3, 1)
