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


def reading_state(reader, document, node, *, now):
    """`(value, "")` of the latest host reading when it counts, else `(None,
    why)`: it counts when it is of this `node`, no older than
    `floor.max_age_s`, and has its memory and disk figures as numbers. A
    missing or stale reading is never passed on an assumption."""
    row = latest(reader)
    if row is None:
        return None, "no host reading"
    value = row["value"]
    if not isinstance(value, dict) or not isinstance(value.get("node"), str):
        return None, "the host reading names no node"
    if value["node"] != node:
        return None, f"the host reading is of {value['node']}, not {node}"
    age = now - row["at"]
    if age > document.floor.max_age_s * 1000:
        return None, (
            f"the host reading is {-(-age // 1000)} s old, past the "
            f"{document.floor.max_age_s} s it counts for"
        )
    if not all(_number(value.get(k)) for k in ("memory_available", "disk_free")):
        return None, "the host reading has no figures for memory and disk"
    return value, ""


def measurement(reader, document, node, *, now):
    """The value of the latest host reading when it counts, else None, which
    is `unmeasured`."""
    return reading_state(reader, document, node, now=now)[0]


def reserved_since(reader, document, node, *, now):
    """`{"memory": n, "disk": n}`: what was reserved on `node` after the latest
    host reading was taken, still live, which that reading cannot have seen:
    the runs and holds of new work started since. A reservation counts when its
    `from_at` is after the reading's instant (one at the instant itself is
    taken as seen, the record keeping no finer order than the millisecond) and
    it is live now. A consumer the document names is not counted: it was
    running when it was declared, so the reading has it. Released ones are
    gone from the live set. **A limit, named:** a run started before the latest
    reading and still growing is not counted; only the reading sees it."""
    row = latest(reader)
    if row is None:
        return {"memory": 0, "disk": 0}
    standing = {consumer.name for consumer in document.consumers}
    out = {"memory": 0, "disk": 0}
    for r in reader.read("reservations", live=True, node=node):
        if r["resource"] not in out or r["from_at"] <= row["at"]:
            continue
        if r["until_at"] is not None and r["until_at"] <= now:
            continue
        if r["task"] is None and r["run"] is None and r["holder"] in standing:
            continue
        out[r["resource"]] += r["amount"]
    return out


def measured_room(value, document, reserved=None):
    """`{"memory": n, "disk": n}`: what a start may still take above the
    floor by `value`, less what was `reserved` since the reading, never below
    0."""
    reserved = reserved or {}
    return {
        "memory": max(
            value["memory_available"]
            - reserved.get("memory", 0)
            - document.floor.memory,
            0,
        ),
        "disk": max(
            value["disk_free"] - reserved.get("disk", 0) - document.floor.disk, 0
        ),
    }


def measured_shorts(value, document, node, needs, why="", reserved=None):
    """The shorts of `needs` by the measurement: one `unmeasured` (every
    resource, saying `why`) when there is no `value`; else a `measured` short
    for a memory or disk need that would leave less than the floor, with the
    available figure as `free` and the floor in its `detail`. All wait: memory
    frees and a reading comes."""
    if value is None:
        return [Short("unmeasured", node, None, None, None, None, True, None, [], why)]
    reserved = reserved or {}
    room = measured_room(value, document, reserved)
    capacity = value.get("memory_total")
    capacity = capacity if _number(capacity) else None
    floors = {"memory": document.floor.memory, "disk": document.floor.disk}
    shorts = []
    for resource in ("memory", "disk"):
        asked = needs.get(resource)
        if asked is not None and asked > room[resource]:
            held = reserved.get(resource, 0)
            available = max(
                value["memory_available" if resource == "memory" else "disk_free"]
                - held,
                0,
            )
            detail = f"floor {_size(resource, floors[resource])}"
            if held:
                detail += f", {_size(resource, held)} reserved since the reading"
            shorts.append(
                Short(
                    "measured",
                    node,
                    resource,
                    asked,
                    available,
                    capacity if resource == "memory" else None,
                    True,
                    None,
                    [],
                    detail,
                )
            )
    return shorts


#: The kernel's names for the pressure levels; a level it has not named reads
#: as its number.
LEVEL_NAMES = {1: "normal", 2: "warn", 4: "critical"}
#: The lowest level that refuses new work: warn.
REFUSE_LEVEL = 2
#: Swap refuses new work when it is nearly full AND memory is short. Nearly
#: full is used past BOTH a floor and a share of its total. macOS grows its swap
#: on demand, so a share alone reads "nearly full" on a small swap (90% of 1 GiB
#: is no crisis); the floor alone would refuse a large swap that still has room.
#: The floor is twice the memory floor and about a sixth of this host's 48 GiB;
#: the night that began it had 14 of 15 GiB. And memory is short when under
#: `MEMORY_FREE_UNDER_PERCENT` of it is free: macOS leaves old pages in swap for
#: days, so a stock of swap alone would refuse a calm host (9.2 of 10 GiB at 84%
#: free) and only the warn level, or swap with memory short, is struggling now.
SWAP_FLOOR = 8 * 2**30
SWAP_SHARE_PERCENT = 85
MEMORY_FREE_UNDER_PERCENT = 40


def _gib(n):
    return f"{n / 2**30:.1f} GiB"


def swap_refused(used, total, free_percent):
    """Whether swap of `used` of `total` bytes, with `free_percent` of memory
    free (None: not known), refuses new work: the one place the swap rule is
    written. Memory not shown short (a free share of 40% or more, or none known)
    never refuses on swap."""
    return (
        used >= SWAP_FLOOR
        and used * 100 >= total * SWAP_SHARE_PERCENT
        and free_percent is not None
        and free_percent < MEMORY_FREE_UNDER_PERCENT
    )


def pressure_view(value, why=""):
    """What the host's pressure reading says, as plain data and the words that
    would refuse new work: `{"level", "swap_used", "swap_total", "refused",
    "unread"}`. `value` is `probe_pressure`'s dict, or None with `why` for a
    read that failed. `refused` is a list of details, empty when new work may
    start."""
    if value is None:
        return {
            "level": None,
            "swap_used": None,
            "swap_total": None,
            "memory_free_percent": None,
            "refused": [],
            "unread": why or "the pressure reading failed",
        }
    level = value.get("level")
    used, total = value.get("swap_used"), value.get("swap_total")
    free = value.get("memory_free_percent")
    free = free if _number(free) else None
    refused = []
    if _number(level) and level >= REFUSE_LEVEL:
        refused.append(f"memory pressure is {LEVEL_NAMES.get(level, f'level {level}')}")
    if _number(used) and _number(total) and swap_refused(used, total, free):
        refused.append(f"swap {_gib(used)} of {_gib(total)}, memory {free}% free")
    return {
        "level": LEVEL_NAMES.get(level, f"level {level}") if _number(level) else None,
        "swap_used": used if _number(used) else None,
        "swap_total": total if _number(total) else None,
        "memory_free_percent": free,
        "refused": refused,
        "unread": None,
    }


def pressure_shorts(value, why, node):
    """The shorts of the host's memory pressure: one `unmeasured` (every
    resource, saying `why`) when the read failed, and else one `pressure`
    short for a level of warn or worse and one for a swap past its floor and
    its share. All wait: pressure eases. A host that has no such reading
    (None figures) is refused for neither."""
    view = pressure_view(value, why)
    if view["unread"]:
        return [
            Short(
                "unmeasured",
                node,
                None,
                None,
                None,
                None,
                True,
                None,
                [],
                view["unread"],
            )
        ]
    shorts = []
    for detail in view["refused"]:
        swap = detail.startswith("swap")
        shorts.append(
            Short(
                "pressure",
                node,
                None,
                None,
                view["swap_total"] - view["swap_used"] if swap else None,
                view["swap_total"] if swap else None,
                True,
                None,
                [],
                detail,
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


def absent_began(name, why):
    """The line for a consumer that is down, or whose probe sees nothing."""
    return f"allocator: {name} is absent: {why}"


def absent_ended(name, back):
    """The line for a consumer that was absent: back, or no longer probed
    (the document stopped naming a probe for it)."""
    if back:
        return f"allocator: {name} is back"
    return f"allocator: {name} is no longer probed"
