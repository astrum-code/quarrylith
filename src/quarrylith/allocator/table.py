# SPDX-License-Identifier: Apache-2.0
"""The table check: whether a node's capacity less its live reservations has
room for what a task needs, and who holds the rest. The check `start_run`
makes, asked in advance and writing nothing."""

from .seam import Short

#: How many holders a short names.
HOLDERS = 3


def peak(reservations, from_at):
    """The largest sum of `reservations` (rows with `amount`, `from_at` and
    `until_at`) covering any instant from `from_at` on. The record's own sweep:
    a reservation covers `[its from_at, its until_at)`, so the sum can only
    rise where one starts."""
    over = [r for r in reservations if r["until_at"] is None or r["until_at"] > from_at]
    points = {from_at} | {r["from_at"] for r in over if r["from_at"] > from_at}
    return max(
        (
            sum(
                r["amount"]
                for r in over
                if r["from_at"] <= t and (r["until_at"] is None or t < r["until_at"])
            )
            for t in points
        ),
        default=0,
    )


def holders_of(reservations, resource):
    """The largest live holders of `resource` among `reservations`:
    `[(holder, amount)]`, a holder's reservations summed, the largest first."""
    held = {}
    for r in reservations:
        if r["resource"] == resource:
            held[r["holder"]] = held.get(r["holder"], 0) + r["amount"]
    ranked = sorted(held.items(), key=lambda pair: (-pair[1], pair[0]))
    return ranked[:HOLDERS]


def room_short(node, resource, asked, capacity, free, live, now):
    """The `room` short for `asked` of `resource` when the table has `free` of
    `capacity` for it, `live` being the node's live reservations; None when it
    fits. It waits when `asked` fits within the capacity less the reservations
    no run holds (standing consumers and tasks' own): then the finish of runs
    alone makes room."""
    if free >= asked:
        return None
    mine = [r for r in live if r["resource"] == resource]
    standing = [r for r in mine if r["run"] is None]
    waits = asked <= capacity - peak(standing, now)
    holders = holders_of(mine, resource)
    return Short(
        "room", node, resource, asked, max(free, 0), capacity, waits, None, holders
    )


def undeclared_short(node, resource, asked):
    """The short of a resource the node has no capacity row for."""
    return Short("undeclared", node, resource, asked, None, None, False, None, [])


def table_free(reader, node, needs, *, now):
    """`{resource: free}` of each resource in `needs` the node has a capacity
    for, as the record's `free` gives it from `now` with no end."""
    return {
        resource: reader.free(node, resource, from_at=now, until_at=None)
        for resource in sorted(needs)
        if reader.get("capacity", [node, resource]) is not None
    }


def table_shorts(reader, node, needs, *, now):
    """Every short of `needs` (`{resource: amount}`) on `node` at `now`: `room`
    for a resource without room, `undeclared` for one the node has no capacity
    row for, by resource. Empty when the table has room for all of it. Reads
    only."""
    with reader.reading():
        live = reader.read("reservations", live=True, node=node)
        free = table_free(reader, node, needs, now=now)
        shorts = []
        for resource in sorted(needs):
            asked = needs[resource]
            if resource not in free:
                shorts.append(undeclared_short(node, resource, asked))
                continue
            capacity = reader.get("capacity", [node, resource])["amount"]
            short = room_short(
                node, resource, asked, capacity, free[resource], live, now
            )
            if short is not None:
                shorts.append(short)
        return shorts
