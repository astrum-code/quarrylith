# SPDX-License-Identifier: Apache-2.0
"""The budget: a provider's account is spent over windows (five hours, seven
days), and its readings narrow what a start may take. They move the allowance
and never the capacity: a capacity cut below what running runs hold is the
record's refusal, and a running run is never preempted."""

from .document import unit_of
from .seam import Short
from .table import holders_of

#: Where a budget reading is kept: `source`, with the window as its `kind`.
SOURCE = "budget"
PREFIX = "budget."


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def providers_of(resources):
    """The providers a set of resources names a share of: `budget.<provider>`
    gives `<provider>`."""
    return [r[len(PREFIX) :] for r in sorted(resources) if unit_of(r) == "budget"]


def _windows(reader, document, provider, now):
    """The latest reading of each window of `provider` that still guards
    anything: no older than `budget.max_age_s`, and not past its `resets_at`.
    A window's older readings are not read again once a newer one is there."""
    seen, found = set(), []
    limit = document.budget.max_age_s * 1000
    for row in reader.read("readings", source=SOURCE, desc=True):
        value = row["value"]
        if not isinstance(value, dict) or value.get("provider") != provider:
            continue
        if row["kind"] in seen:
            continue
        seen.add(row["kind"])
        resets = value.get("resets_at")
        resets = resets if _number(resets) else None
        if now - row["at"] > limit or (resets is not None and now >= resets):
            continue
        found.append((row["kind"], value, resets))
    return found


def _stopped(document, value):
    used = value.get("utilization")
    over = _number(used) and used >= document.budget.stop_at
    return value.get("status") == "rejected" or over


def allowance(reader, document, node, provider, *, now):
    """`(allowance, until)` of `provider`'s budget on `node`: how much weight a
    start may still take by the readings, or None when they guard nothing, and
    the time after which it may fit (the latest reset of the windows that
    stop it, when each has one; None at a warning). Per window: `rejected`, or
    `utilization` from `stop_at`, stops the provider; `allowed_warning` lets
    it take up to `warning_max` of live run weight; the provider is held by
    the tightest window."""
    windows = _windows(reader, document, provider, now)
    stops = [resets for _, value, resets in windows if _stopped(document, value)]
    if stops:
        until = None if None in stops else max(stops)
        return 0, until
    warned = [v for _, v, _ in windows if v.get("status") == "allowed_warning"]
    if not warned:
        return None, None
    resource = PREFIX + provider
    live = sum(
        r["amount"]
        for r in reader.read("reservations", live=True, node=node)
        if r["resource"] == resource and r["run"] is not None
    )
    return max(document.budget.warning_max - live, 0), None


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
            room, until = allowance(reader, document, node, provider, now=now)
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
                )
            )
        return shorts
