# SPDX-License-Identifier: Apache-2.0
"""The measurement as policy: the host's latest reading, whether it still
counts, the room it leaves above the floor, and what drifts from the
declaration. The probes that make the reading are `probes`'; this reads
what is in the record and writes nothing."""

from .seam import Short, _size

SOURCE, KIND = "host", "measure"


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def latest(reader):
    """The latest `host/measure` reading's row, or None. One read of one row:
    a record keeps the measurement of the one host its document names, so the
    latest reading is that node's."""
    rows = reader.read("readings", source=SOURCE, kind=KIND, desc=True, limit=1)
    return rows[0] if rows else None


def measurement(reader, document, node, *, now):
    """The value of the latest host reading when it counts: of this `node`, no
    older than `floor.max_age_s`, and with its memory and disk figures as
    numbers. None otherwise, which is `unmeasured`: a missing or stale
    reading is never passed on an assumption."""
    row = latest(reader)
    if row is None:
        return None
    value = row["value"]
    if not isinstance(value, dict) or value.get("node") != node:
        return None
    if now - row["at"] > document.floor.max_age_s * 1000:
        return None
    if not all(_number(value.get(k)) for k in ("memory_available", "disk_free")):
        return None
    return value


def measured_room(value, document):
    """`{"memory": n, "disk": n}`: what a start may still take above the
    floor by `value`, never below 0."""
    return {
        "memory": max(value["memory_available"] - document.floor.memory, 0),
        "disk": max(value["disk_free"] - document.floor.disk, 0),
    }


def measured_shorts(value, document, node, needs):
    """The shorts of `needs` by the measurement: one `unmeasured` (every
    resource) when there is no `value`; else a `measured` short for a memory
    or disk need that would leave less than the floor. All wait: memory frees
    and a reading comes."""
    if value is None:
        return [Short("unmeasured", node, None, None, None, None, True, None, [])]
    room = measured_room(value, document)
    capacity = value.get("memory_total")
    capacity = capacity if _number(capacity) else None
    shorts = []
    for resource in ("memory", "disk"):
        asked = needs.get(resource)
        if asked is not None and asked > room[resource]:
            total = capacity if resource == "memory" else None
            shorts.append(
                Short(
                    "measured",
                    node,
                    resource,
                    asked,
                    room[resource],
                    total,
                    True,
                    None,
                    [],
                )
            )
    return shorts


def drift_entries(document, value, free_memory, capacity_memory):
    """What `value` shows past the declaration, as `{"what", "declared",
    "measured", "over"}`: a probed consumer whose resident memory passes what
    it declared by more than `drift.tolerance`, and measured available memory
    short of the table's free memory by more than the tolerance of the memory
    capacity (use that nothing declared accounts for)."""
    tolerance = document.drift_tolerance
    out = []
    held = value.get("consumers")
    held = held if isinstance(held, dict) else {}
    for consumer in document.consumers:
        declared = consumer.amounts.get("memory")
        found = held.get(consumer.name)
        found = found.get("memory") if isinstance(found, dict) else None
        if not _number(declared) or not _number(found) or declared <= 0:
            continue
        over = (found - declared) / declared
        if over > tolerance:
            out.append(
                {
                    "what": f"consumer {consumer.name} memory",
                    "declared": declared,
                    "measured": found,
                    "over": over,
                }
            )
    available = value.get("memory_available")
    if _number(available) and _number(capacity_memory) and capacity_memory > 0:
        over = (free_memory - available) / capacity_memory
        if over > tolerance:
            out.append(
                {
                    "what": "undeclared use of memory",
                    "declared": free_memory,
                    "measured": available,
                    "over": over,
                }
            )
    return out


def began(entry):
    """The line for a drift that begins."""
    percent = round(entry["over"] * 100)
    measured, declared = entry["measured"], entry["declared"]
    if entry["what"].startswith("consumer "):
        name = entry["what"].split(" ")[1]
        return (
            f"allocator: {name} memory measured {_size('memory', measured)}, "
            f"declared {_size('memory', declared)} (+{percent}%)"
        )
    return (
        f"allocator: undeclared use of memory: {_size('memory', measured)} "
        f"available against {_size('memory', declared)} free in the table "
        f"({percent}% of capacity)"
    )


def ended(entry):
    """The line for a drift that ends."""
    if entry["what"].startswith("consumer "):
        name = entry["what"].split(" ")[1]
        return f"allocator: {name} memory is back within its declaration"
    return "allocator: undeclared use of memory has ended"
