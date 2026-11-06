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
#: The most readings the first read of the history returns. A history that
#: fills it is read whole, so no provider's window is lost under the newer
#: readings of others (an unsaturated history, the common case, is one read).
READ_LIMIT = 100
#: `resets_at` is milliseconds, as the record's clock. A number below this is
#: before March 1973 in milliseconds, so it is a time in seconds.
SECONDS_BELOW = 10**11
#: A reset is a time between these, in milliseconds: 2000-01-01 and the last
#: millisecond of year 9999. Any other number is no reset.
FIRST_RESET = 946_684_800_000
LAST_RESET = 253_402_300_799_999

STOP, WARN, ALLOWED, CLEAR = "stop", "warn", "allowed", "clear"
#: The status of a person's clear: a reading in the stopped window, with who
#: and why, that lifts a stop which names no end.
CLEARED = "cleared"
#: The longest a clear's reason may be.
WHY_AT_MOST = 300
#: What a stop that names no end says, in the refusal (a Short's `detail`).
DETAIL = (
    "{provider} is stopped by a reading with no reset time: held until a newer "
    "reading, or until a person clears it (quarrylith alloc clear URL {provider} "
    "--why TEXT)"
)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def providers_of(resources):
    """The providers a set of resources names a share of: `budget.<provider>`
    gives `<provider>`."""
    return [r[len(PREFIX) :] for r in sorted(resources) if unit_of(r) == "budget"]


def _same(named, provider):
    """Whether a reading's `provider` is `provider`, without case or
    surrounding space."""
    return isinstance(named, str) and named.strip().lower() == provider.strip().lower()


def _history(reader, provider):
    """`{window: [reading, ...]}`, `provider`'s readings of each window,
    newest first. The newest `READ_LIMIT` readings of the source are read;
    when they fill it the history is read whole, since the provider's
    newest reading of a window may lie under any number of newer ones."""
    rows = reader.read("readings", source=SOURCE, desc=True, limit=READ_LIMIT)
    if len(rows) >= READ_LIMIT:
        rows = reader.read("readings", source=SOURCE, desc=True)
    windows = {}
    for row in rows:
        value = row["value"]
        if isinstance(value, dict) and _same(value.get("provider"), provider):
            windows.setdefault(row["kind"], []).append(row)
    return windows


def _resets(value):
    """A window's `resets_at` in milliseconds, or None when it names none: a
    number, in seconds when below `SECONDS_BELOW`, that is a time between 2000
    and year 9999. A relative number, a count, microseconds and nanoseconds
    are not times of ours: they name no end."""
    resets = value.get("resets_at")
    if not _number(resets) or not math.isfinite(resets):
        return None
    resets = resets * 1000 if resets < SECONDS_BELOW else resets
    return int(resets) if FIRST_RESET <= resets <= LAST_RESET else None


def _used(value):
    """`(utilization, readable)`: a number, or numeric text, is read (text
    can say `inf`); none given is readable and unused; anything else cannot
    be read."""
    used = value.get("utilization")
    if used is None:
        return None, True
    if isinstance(used, str):
        try:
            used = float(used.strip())
        except ValueError:
            return None, False
    if not _number(used) or math.isnan(used):
        return None, False
    return used, True


def _named(text):
    return isinstance(text, str) and bool(text.strip())


def _state(document, value):
    """`(state, recognised)`: `stop`, `warn`, `allowed` or `clear`, and whether
    the reading is one we know. A `cleared` reading that names who and why is
    a clear; one that does not is not recognised. `rejected`, or a utilization
    from `stop_at`, is a stop; `allowed_warning` is a warning; `allowed` is
    allowed. A status that is not recognised (missing, unknown, of another
    type), and a utilization that cannot be read, are paced as a warning and
    are not recognised. The status is matched without case or surrounding
    space."""
    status = value.get("status")
    status = status.strip().lower() if isinstance(status, str) else None
    if status == CLEARED:
        named = all(_named(value.get(key)) for key in ("by", "why"))
        return (CLEAR, True) if named else (WARN, False)
    used, readable = _used(value)
    if status == "rejected" or (used is not None and used >= document.budget.stop_at):
        return STOP, True
    if readable and status == "allowed_warning":
        return WARN, True
    if readable and status == "allowed":
        return ALLOWED, True
    return WARN, False


def _walk(document, rows):
    """`(paced, anchor, cleared, lifted)` of one window's readings, newest
    first: the newest unrecognised reading above any clear or recognised one;
    the newest recognised one that is not a clear, with its state; the newest
    clear above it; and whether that clear lifts the anchor. A clear lifts a
    stop that names no end and nothing else: not a stop with a readable
    reset, which holds until it, and not a warning."""
    paced = anchor = cleared = None
    for row in rows:
        state, recognised = _state(document, row["value"])
        if state == CLEAR:
            cleared = cleared or row
            continue
        if recognised:
            anchor = row, state
            break
        if paced is None and cleared is None:
            paced = row
    lifted = (
        cleared is not None
        and anchor is not None
        and anchor[1] == STOP
        and _resets(anchor[0]["value"]) is None
    )
    return paced, anchor, cleared, lifted


def _windows(reader, document, provider, now):
    """`(kind, state, resets)` of each guard of `provider` that still holds.
    Per window, the newest recognised reading counts, and an unrecognised
    reading newer than it is paced like a warning without lifting it: only a
    recognised non-stop lifts a stop, or a person's clear, which lifts a stop
    that names no end. A stop holds until its `resets_at`, however old the
    reading, or until a newer recognised reading when it names none; a
    warning, and an unrecognised reading, holds while it is no older than
    `budget.max_age_s` and before its `resets_at`."""
    limit = document.budget.max_age_s * 1000
    found = []
    for kind, rows in _history(reader, provider).items():
        paced, anchor, _, lifted = _walk(document, rows)
        guards = []
        if paced is not None:
            guards.append((paced, WARN))
        if anchor is not None and not lifted:
            guards.append(anchor)
        for row, state in guards:
            resets = _resets(row["value"])
            if resets is not None and now >= resets:
                continue
            if state != STOP and now - row["at"] > limit:
                continue
            found.append((kind, state, resets))
    return found


def report(reader, document, provider, *, now):
    """The stops of `provider` and the clears in force, one dict per window,
    by window name: `state` `stop` with its `resets` (None when it names no
    end), or `cleared` with `by`, `why` and `at` of the clear that lifted a
    stop which names no end. A stop whose reset has passed, and a clear with
    no stop under it, are not listed."""
    out = []
    for kind, rows in sorted(_history(reader, provider).items()):
        _, anchor, cleared, lifted = _walk(document, rows)
        if lifted:
            value = cleared["value"]
            out.append(
                {
                    "provider": provider,
                    "window": kind,
                    "state": "cleared",
                    "resets": None,
                    "by": value["by"],
                    "why": value["why"],
                    "at": cleared["at"],
                }
            )
        elif anchor is not None and anchor[1] == STOP:
            resets = _resets(anchor[0]["value"])
            if resets is None or now < resets:
                out.append(
                    {
                        "provider": provider,
                        "window": kind,
                        "state": "stop",
                        "resets": resets,
                    }
                )
    return out


def clear(record, document, provider, why, *, by, now):
    """Clear `provider`'s stop that names no end: one `cleared` reading, with
    `by` and `why`, in each window whose stop names none. `(cleared, held)`:
    the windows cleared, and the stops that name a future reset, which hold
    until it and are not cleared. Nothing is written for a provider with no
    such stop."""
    provider = provider.strip().lower()
    view = report(record, document, provider, now=now)
    stops = [w for w in view if w["state"] == "stop"]
    cleared = [w["window"] for w in stops if w["resets"] is None]
    for window in cleared:
        value = {"provider": provider, "status": CLEARED, "by": by, "why": why}
        record.record_reading(SOURCE, window, value)
    held = [(w["window"], w["resets"]) for w in stops if w["resets"] is not None]
    return cleared, held


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
                    DETAIL.format(provider=provider) if unended else "",
                )
            )
        return shorts
