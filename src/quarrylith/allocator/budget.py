# SPDX-License-Identifier: Apache-2.0
"""The budget: a provider's account is spent over windows (five hours, seven
days), and its readings narrow what a start may take. They move the allowance
and never the capacity: a capacity cut below what running runs hold is the
record's refusal, and a running run is never preempted."""

import math

from .document import unit_of
from .seam import Short
from .table import holders_of

#: Where a budget reading is kept: `source`, with the window as its `kind`.
SOURCE = "budget"
PREFIX = "budget."
#: The windows read even when the newest readings do not show them, and the
#: most readings one read of the history returns: the newest of the source
#: (to find the windows) and the newest of each window.
WINDOWS = ("five_hour", "seven_day")
READ_LIMIT = 100
#: `resets_at` is milliseconds, as the record's clock. A number below this is
#: before March 1973 in milliseconds, so it is a time in seconds.
SECONDS_BELOW = 10**11

STOP, WARN, ALLOWED = "stop", "warn", "allowed"
#: What a stop that names no end says, in the refusal.
NOTE = (
    "{provider} is stopped by a reading with no reset time: held until a newer reading"
)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def providers_of(resources):
    """The providers a set of resources names a share of: `budget.<provider>`
    gives `<provider>`."""
    return [r[len(PREFIX) :] for r in sorted(resources) if unit_of(r) == "budget"]


def _history(reader, provider):
    """The latest reading of each window of `provider`, newest window first
    as found. The windows are the defaults and those in the newest readings;
    each is read apart, a bounded number of rows deep."""
    newest = reader.read("readings", source=SOURCE, desc=True, limit=READ_LIMIT)
    kinds = dict.fromkeys([*WINDOWS, *(row["kind"] for row in newest)])
    found = []
    for kind in kinds:
        rows = reader.read(
            "readings", source=SOURCE, kind=kind, desc=True, limit=READ_LIMIT
        )
        for row in rows:
            value = row["value"]
            if isinstance(value, dict) and value.get("provider") == provider:
                found.append(row)
                break
    return found


def _resets(value):
    """A window's `resets_at` in milliseconds, or None when it names none: a
    number above zero, in seconds when below `SECONDS_BELOW`."""
    resets = value.get("resets_at")
    if not _number(resets) or not math.isfinite(resets) or resets <= 0:
        return None
    return int(resets * 1000) if resets < SECONDS_BELOW else int(resets)


def _used(value):
    """`(utilization, readable)`: a number, or numeric text, is read; none
    given is readable and unused; anything else cannot be read."""
    used = value.get("utilization")
    if used is None:
        return None, True
    if isinstance(used, str):
        try:
            used = float(used.strip())
        except ValueError:
            return None, False
    if not _number(used) or not math.isfinite(used):
        return None, False
    return used, True


def _state(document, value):
    """`stop`, `warn` or `allowed`. `rejected`, or a utilization from
    `stop_at`, is a stop; `allowed_warning`, a status that is not recognised
    (missing, unknown, of another type) and a utilization that cannot be read
    are paced as a warning; else `allowed`. The status is matched without case
    or surrounding space."""
    status = value.get("status")
    status = status.strip().lower() if isinstance(status, str) else None
    used, readable = _used(value)
    if status == "rejected" or (used is not None and used >= document.budget.stop_at):
        return STOP
    if status == "allowed_warning" or not readable:
        return WARN
    return ALLOWED if status == "allowed" else WARN


def _windows(reader, document, provider, now):
    """`(kind, state, resets)` of each window of `provider` that still guards
    anything, by its latest reading. A stop guards until its `resets_at`,
    however old the reading, or until a newer reading when it names none; a
    warning guards while it is no older than `budget.max_age_s` and before its
    `resets_at`. A window's older readings are never read again once a newer
    one is there."""
    limit = document.budget.max_age_s * 1000
    found = []
    for row in _history(reader, provider):
        value = row["value"]
        resets = _resets(value)
        state = _state(document, value)
        if resets is not None and now >= resets:
            continue
        if state != STOP and now - row["at"] > limit:
            continue
        found.append((row["kind"], state, resets))
    return found


def _judge(reader, document, node, provider, now):
    """`(allowance, until, unended)`: see `allowance`; `unended` is true when
    a stop names no end, so the provider holds until a newer reading."""
    windows = _windows(reader, document, provider, now)
    stops = [resets for _, state, resets in windows if state == STOP]
    if stops:
        unended = None in stops
        return 0, None if unended else max(stops), unended
    if not any(state == WARN for _, state, _ in windows):
        return None, None, False
    resource = PREFIX + provider
    live = sum(
        r["amount"]
        for r in reader.read("reservations", live=True, node=node)
        if r["resource"] == resource and r["run"] is not None
    )
    return max(document.budget.warning_max - live, 0), None, False


def allowance(reader, document, node, provider, *, now):
    """`(allowance, until)` of `provider`'s budget on `node`: how much weight a
    start may still take by the readings, or None when they guard nothing, and
    the time after which it may fit (the latest reset of the windows that
    stop it, when each has one; None at a warning, and None at a stop that
    names no end). Per window: `rejected`, or `utilization` from `stop_at`,
    stops the provider until its `resets_at`, however old the reading;
    `allowed_warning`, and a status that is not recognised, let it take up to
    `warning_max` of live run weight while the reading is fresh; the provider
    is held by the tightest window."""
    room, until, _ = _judge(reader, document, node, provider, now)
    return room, until


def budget_shorts(reader, document, node, needs, *, now):
    """The `budget` shorts of `needs` on `node` at `now`: one for each provider
    whose allowance is below what the task asks of its `budget.<provider>`,
    with the allowance as `free` and the window's reset as `until`. Reads
    only; a harness with no budget resource asks for none."""
    with reader.reading():
        shorts = []
        live = None
        for provider in providers_of(needs):
            resource = PREFIX + provider
            room, until, unended = _judge(reader, document, node, provider, now)
            asked = needs[resource]
            if room is None or asked <= room:
                continue
            if live is None:
                live = reader.read("reservations", live=True, node=node)
            row = reader.get("capacity", [node, resource])
            capacity = row["amount"] if row else None
            holders = holders_of(live, resource)
            shorts.append(
                Short(
                    "budget",
                    node,
                    resource,
                    asked,
                    room,
                    capacity,
                    True,
                    until,
                    holders,
                    NOTE.format(provider=provider) if unended else None,
                )
            )
        return shorts
