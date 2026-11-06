# SPDX-License-Identifier: Apache-2.0
"""What the allocator has made of a record, for a person: per node and
resource the capacity, who stands on it, which tasks and runs hold it, what is
free, what the host measured and where the measurement drifts from the
declaration. `snapshot` is plain data; `lines` writes it as text. Both read
the record only, and carry figures and names the record holds, never the
document's free text."""

from .document import document_from_data
from .measure import KIND as MEASURE_KIND
from .measure import SOURCE as MEASURE_SOURCE
from .measure import _number, began
from .seam import _size, _when

DOCUMENT = ("allocator", "document")
#: Which reading figure measures a resource, and how it is worded.
MEASURED = {"memory": ("memory_available", "available"), "disk": ("disk_free", "free")}
#: How many of the allocator's latest observations are shown.
OBSERVATIONS = 5


def _latest(record, source, kind):
    rows = record.read("readings", source=source, kind=kind, desc=True, limit=1)
    return rows[0] if rows else None


def _document(row):
    """The document in a document reading, or None when it does not parse."""
    try:
        return document_from_data(row["value"]["document"])
    except (KeyError, TypeError, ValueError):
        return None


def _age(ms):
    seconds = max(ms, 0) // 1000
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} m {seconds % 60} s"
    if seconds < 86400:
        return f"{seconds // 3600} h {seconds % 3600 // 60} m"
    return f"{seconds // 86400} d {seconds % 86400 // 3600} h"


def _resource_of(entry):
    """The resource a drift entry is about: the last word of what it names."""
    return str(entry.get("what", "")).rsplit(" ", 1)[-1]


def snapshot(record, *, now, pressure=None):
    """Plain data: the document in force and its age, the host reading and its
    age, the host's memory pressure when the caller read it (`pressure`, from
    `HostPlacement.pressure`), per live node its resources, the consumers that
    are absent, the drift and the allocator's latest observations."""
    with record.reading():
        stored = _latest(record, *DOCUMENT)
        measure = _latest(record, MEASURE_SOURCE, MEASURE_KIND)
        document = _document(stored) if stored else None
        value = (
            measure["value"] if measure and isinstance(measure["value"], dict) else {}
        )
        held_by = {c.name for c in document.consumers} if document else None
        nodes = []
        for node in sorted(record.read("nodes", live=True), key=lambda n: n["name"]):
            nodes.append(_node(record, node, now, value, held_by))
        said = record.read("statements", type="observation", desc=True, limit=50)
    drift = value.get("drift") if isinstance(value.get("drift"), list) else []
    absent = value.get("absent") if isinstance(value.get("absent"), dict) else {}
    said = [s for s in said if s["where"] == "allocator"][:OBSERVATIONS]
    return {
        "at": now,
        "document": (
            None
            if stored is None
            else {
                "sha256": stored["value"].get("sha256"),
                "node": document.node if document else None,
                "declared_at": stored["at"],
                "age_s": max(now - stored["at"], 0) // 1000,
            }
        ),
        "reading": (
            None
            if not value
            else {
                "node": value.get("node"),
                "method": value.get("method"),
                "at": measure["at"],
                "age_s": max(now - measure["at"], 0) // 1000,
            }
        ),
        "pressure": pressure,
        "nodes": nodes,
        "absent": dict(absent),
        "drift": list(drift),
        "observations": [{"at": s["at"], "text": s["text"]} for s in reversed(said)],
    }


def _node(record, node, now, value, held_by):
    name = node["name"]
    live = sorted(
        record.read("reservations", live=True, node=name),
        key=lambda r: (r["resource"], r["holder"], r["id"]),
    )
    here = value.get("node") == name
    consumers = (
        value.get("consumers") if isinstance(value.get("consumers"), dict) else {}
    )
    absent = value.get("absent") if isinstance(value.get("absent"), dict) else {}
    drift = value.get("drift") if isinstance(value.get("drift"), list) else []
    resources = []
    for row in sorted(record.read("capacity", node=name), key=lambda c: c["resource"]):
        resource = row["resource"]
        out = {
            "resource": resource,
            "unit": row["unit"],
            "capacity": row["amount"],
            "free": record.free(name, resource, from_at=now, until_at=None),
            "measured": None,
            "standing": [],
            "tasks": [],
            "runs": [],
            "drift": [e for e in drift if _resource_of(e) == resource],
        }
        figure = MEASURED.get(resource)
        if here and figure and _number(value.get(figure[0])):
            out["measured"] = value[figure[0]]
        for r in live:
            if r["resource"] != resource:
                continue
            if r["run"] is not None:
                out["runs"].append(
                    {
                        "holder": r["holder"],
                        "amount": r["amount"],
                        "task": r["task"],
                        "run": r["run"],
                    }
                )
            elif r["task"] is not None:
                out["tasks"].append(
                    {"holder": r["holder"], "amount": r["amount"], "task": r["task"]}
                )
            else:
                standing = {
                    "holder": r["holder"],
                    "amount": r["amount"],
                    "in_document": True if held_by is None else r["holder"] in held_by,
                }
                found = consumers.get(r["holder"])
                if here and resource == "memory" and isinstance(found, dict):
                    if _number(found.get("memory")):
                        standing["measured"] = found["memory"]
                if here and resource == "memory" and r["holder"] in absent:
                    standing["absent"] = True
                out["standing"].append(standing)
        resources.append(out)
    return {
        "name": name,
        "drivers": list(node["drivers"]),
        "labels": list(node["labels"]),
        "resources": resources,
    }


def _pressure_lines(pressure):
    """The line for the host's memory pressure: what refuses new work, or that
    nothing does, or why it was not read."""
    if pressure is None:
        return []
    if pressure["unread"]:
        return [f"pressure: not read ({pressure['unread']})"]
    if pressure["refused"]:
        line = "pressure: " + "; ".join(pressure["refused"]) + "; new work is refused"
        if any(r.startswith("swap") for r in pressure["refused"]):
            # swap in use is a stock: it stays until the pages are touched
            line += (
                "; swap in use falls only when its pages are read back "
                "or the host restarts"
            )
        return [line]
    parts = []
    if pressure["level"] is not None:
        parts.append(pressure["level"])
    if pressure["swap_total"] is not None:
        used, total = pressure["swap_used"], pressure["swap_total"]
        parts.append(f"swap {used / 2**30:.1f} GiB of {total / 2**30:.1f} GiB")
    return ["pressure: " + (", ".join(parts) or "no reading on this host")]


def lines(snap):
    """`snap` as the text `alloc show` prints."""
    out = []
    doc = snap["document"]
    if doc is None:
        out.append("document: none declared")
    else:
        age = _age(doc["age_s"] * 1000)
        out.append(f"document: sha256 {doc['sha256']}, age {age}")
    reading = snap["reading"]
    if reading is None:
        out.append("host reading: none")
    else:
        age = _age(reading["age_s"] * 1000)
        out.append(
            f"host reading: node {reading['node']}, {reading['method']}, age {age}"
        )
    out.extend(_pressure_lines(snap.get("pressure")))
    for node in snap["nodes"]:
        out.append(f"node {node['name']}: drivers {', '.join(node['drivers'])}")
        for r in node["resources"]:
            resource = r["resource"]

            def size(n, resource=resource):
                return _size(resource, n)

            head = (
                f"  {resource}: capacity {size(r['capacity'])}, free {size(r['free'])}"
            )
            if r["measured"] is not None:
                word = MEASURED[resource][1]
                head += f", measured {size(r['measured'])} {word}"
            out.append(head)
            if r["standing"]:
                parts = []
                for s in r["standing"]:
                    notes = []
                    if s.get("absent"):
                        notes.append("absent")
                    elif "measured" in s:
                        notes.append(f"measured {size(s['measured'])}")
                    if not s["in_document"]:
                        notes.append("not in the document")
                    part = f"{s['holder']} {size(s['amount'])}"
                    parts.append(part + (f" ({', '.join(notes)})" if notes else ""))
                out.append("    standing: " + ", ".join(parts))
            if r["tasks"]:
                parts = [
                    f"{t['holder']} {size(t['amount'])} (task {t['task']})"
                    for t in r["tasks"]
                ]
                out.append("    tasks: " + ", ".join(parts))
            if r["runs"]:
                parts = [
                    f"{x['holder']} {size(x['amount'])} "
                    f"(task {x['task']}, run {x['run']})"
                    for x in r["runs"]
                ]
                out.append("    runs: " + ", ".join(parts))
            for entry in r["drift"]:
                text = began(entry)
                out.append("    drift: " + text.removeprefix("allocator: "))
    for name, why in sorted(snap["absent"].items()):
        out.append(f"absent: {name}: {why}")
    if snap["observations"]:
        out.append("observations:")
        for said in snap["observations"]:
            out.append(f"  {_when(said['at'])} {said['text']}")
    return out
