# SPDX-License-Identifier: Apache-2.0
"""The laptop plugin: the placement for one host, the node the declared
document names. It registers as `host` in the `quarrylith.placement` group.

It declares the document into the record (`declare`), measures the host
(`measure`, `resolve`, `sample_consumer`) and answers from the record's table,
the budget's readings and the host's latest measurement (`place`, `hold`,
`release`, `headroom`). `declare` probes nothing: the figures a "measure"
stands for are given to it, by `resolve`."""

import hashlib
import time
from dataclasses import dataclass, field

from ..record import schema
from ..record.contract import Refused
from .classes import classify, needs_of
from .document import document_from_data, to_data, unit_of
from . import probes as default_probes
from .budget import allowance, budget_shorts, providers_of
from .measure import (
    KIND as MEASURE_KIND,
    SOURCE as MEASURE_SOURCE,
    began,
    drift_entries,
    ended,
    latest,
    measured_room,
    measured_shorts,
    measurement,
)
from .probes import (  # noqa: F401 (the probes are the host plugin's, too)
    ProbeError,
    probe_disk,
    probe_memory,
    probe_process,
    probe_size,
    probe_tree,
)
from .seam import Fit, Placement, Refusal, Short, _size, refuse
from .table import peak, room_short, table_free, table_shorts

#: Where the declared document is kept: a reading of the record.
SOURCE, KIND = "allocator", "document"


@dataclass(frozen=True)
class Declaration:
    """What `declare` did. `wrote`: a line per write. `refusals`: the Refusal
    of each consumer that does not fit, by name, when nothing was written.
    `failed`: `(what, reason)` for each write the record refused after
    writing began (a start that raced in, a capacity below what is reserved);
    a second `declare` finishes it."""

    wrote: tuple = ()
    refusals: dict = field(default_factory=dict)
    failed: tuple = ()

    @property
    def ok(self):
        return not self.refusals and not self.failed


def _digest(data):
    """The fingerprint of the document as the caller wrote it, its "measure"
    kept: it tells `declare` the same document again, and is no check that
    the stored data is what was declared."""
    return hashlib.sha256(schema.canonical(data).encode()).hexdigest()


def _standing(row):
    """Whether a reservation is a standing one: no task's, no run's."""
    return row["task"] is None and row["run"] is None


def _no_node(task, needs=None):
    """The Refusal when no allocation document is declared."""
    short = Short("node", None, None, None, None, None, False, None, [])
    return refuse(task, None, needs or {}, [short], node=None)


def _why(run_class, node, needs, free):
    """One line: the class, the node, and what each need takes of what the
    table had free."""
    parts = [
        f"{resource} {_size(resource, asked)} of {_size(resource, free[resource])} free"
        for resource, asked in sorted(needs.items())
        if resource in free
    ]
    return f"{run_class} on {node}: " + ", ".join(parts)


class HostPlacement(Placement):
    """The placement of one host."""

    name = "host"

    def __init__(self, record, *, probes=None):
        super().__init__(record)
        #: What reads the host: the probes module, or a stand-in with its names.
        self.probes = default_probes if probes is None else probes

    # -- the document in force ----------------------------------------------------

    def _stored(self):
        """`(document, reading value)` of the latest `allocator/document`
        reading, or None before one, and when the latest does not parse."""
        rows = self.record.read(
            "readings", source=SOURCE, kind=KIND, desc=True, limit=1
        )
        if not rows:
            return None
        value = rows[0]["value"]
        try:
            return document_from_data(value["document"]), value
        except (KeyError, TypeError, ValueError):
            # a reading of another shape, or of a format not known: no document
            return None

    def _node_shorts(self, node, sandbox=None):
        """The shorts of a node that is not live (`node`) or does not run
        `sandbox` (`driver`), and whether the node is live."""
        row = self.record.get("nodes", node)
        if row is None or row["retired_at"] is not None:
            short = Short("node", node, None, None, None, None, False, None, [])
            return [short], False
        if sandbox is not None and sandbox not in row["drivers"]:
            short = Short("driver", node, None, None, None, None, False, None, [])
            return [short], True
        return [], True

    # -- place, headroom ----------------------------------------------------------

    def place(self, task, *, now):
        record = self.record
        with record.reading():
            row = record.get("tasks", task)
            if row is None:
                raise Refused(f"no task {task}")
            stored = self._stored()
            if stored is None:
                return _no_node(task)
            document = stored[0]
            node = document.node
            labels = record.effective_labels(task)
            run_class = classify(document, row, labels)
            if isinstance(run_class, Refusal):
                return run_class
            needs = needs_of(document, row, labels)
            if isinstance(needs, Refusal):
                return needs
            shorts, live = self._node_shorts(node, row["sandbox"])
            free = {}
            if live:
                shorts += table_shorts(record, node, needs, now=now)
                shorts += budget_shorts(record, document, node, needs, now=now)
                found = measurement(record, document, node, now=now)
                shorts += measured_shorts(found, document, node, needs)
                free = table_free(record, node, needs, now=now)
        if shorts:
            return refuse(task, run_class.name, needs, shorts, node=node)
        return Fit(
            task,
            node,
            run_class.name,
            needs,
            row["sandbox"],
            _why(run_class.name, node, needs, free),
        )

    def headroom(self, *, now):
        record = self.record
        out = {}
        with record.reading():
            stored = self._stored()
            document = stored[0] if stored else None
            nodes = sorted(record.read("nodes", live=True), key=lambda n: n["name"])
            for node in nodes:
                name = node["name"]
                rows = sorted(
                    record.read("capacity", node=name), key=lambda c: c["resource"]
                )
                out[name] = {
                    row["resource"]: max(
                        record.free(name, row["resource"], from_at=now, until_at=None),
                        0,
                    )
                    for row in rows
                }
                if document is not None and name == document.node:
                    found = measurement(record, document, name, now=now)
                    room = measured_room(found, document) if found else None
                    for resource in ("memory", "disk"):
                        if resource in out[name]:
                            limit = room[resource] if room else 0
                            out[name][resource] = min(out[name][resource], limit)
                    for provider in providers_of(out[name]):
                        room, _ = allowance(record, document, name, provider, now=now)
                        if room is not None:
                            resource = f"budget.{provider}"
                            out[name][resource] = min(out[name][resource], room)
        return out

    # -- hold, release ------------------------------------------------------------

    def hold(self, holder, needs, *, task=None, node=None, measured=True, now):
        """A standing reservation, or a task's own, checked against the table
        as `place` checks it, and against the host's measured room unless
        `measured` is False (on the declared node: the plugin has no
        measurement of another). A `Refusal` when it does not fit, and
        `Refused` for a second live hold of one holder and resource on the
        node."""
        if not isinstance(needs, dict) or not needs:
            raise Refused(f"a hold names its needs, {{resource: amount}}: {needs!r}")
        for resource, amount in needs.items():
            if not schema.is_int(amount) or amount < 1:
                raise Refused(
                    f"hold {resource}: an amount is an int from 1: {amount!r}"
                )
        record = self.record
        with record.reading():
            stored = self._stored()
            if node is None:
                if stored is None:
                    return _no_node(task, needs)
                node = stored[0].node
            shorts, live = self._node_shorts(node)
            if live:
                for r in record.read("reservations", live=True, node=node):
                    if (
                        r["holder"] == holder
                        and r["resource"] in needs
                        and r["run"] is None
                        and r["task"] == task
                    ):
                        raise Refused(
                            f"{holder} holds {r['resource']} on {node} already "
                            f"(reservation {r['id']}): replace it or release it first"
                        )
                shorts += table_shorts(record, node, needs, now=now)
                if measured and (stored is None or stored[0].node == node):
                    document = stored[0] if stored else None
                    found = None
                    if document is not None:
                        found = measurement(record, document, node, now=now)
                    shorts += measured_shorts(found, document, node, needs)
        if shorts:
            return refuse(task, None, needs, shorts, node=node)
        return record.reserve(
            node, needs, task=task, holder=holder, from_at=now, until_at=None
        )

    def release(self, holder, *, node=None, why):
        """Release the holder's reservations that no live run holds, on
        `node`; with none given, on the node the current document names (after
        a rename, the old node is named explicitly), and on every node while
        no document is declared."""
        record = self.record
        with record.reading():
            if node is None:
                stored = self._stored()
                node = stored[0].node if stored else None
            match = {} if node is None else {"node": node}
            ids = [
                r["id"]
                for r in record.read("reservations", live=True, **match)
                if r["holder"] == holder and r["run"] is None
            ]
        for id in ids:
            record.release_reservation(id, why)
        return ids

    # -- measure, drift, resolve, sample_consumer ---------------------------------

    def _memory_table(self, document, now):
        """`(free, capacity)` of the declared node's memory in the table."""
        node = document.node
        row = self.record.get("capacity", [node, "memory"])
        if row is None:
            return 0, None
        free = self.record.free(node, "memory", from_at=now, until_at=None)
        return free, row["amount"]

    def measure(self, *, now):
        """Read the host once and write one `host/measure` reading: the node,
        total and available memory with the method named, the work disk's free
        bytes, each probed consumer's resident memory and the drift from the
        declaration. Returns the value. A probe that fails writes nothing and
        raises `ProbeError` naming it; with no document declared there is
        nothing that names the node, the work disk or the consumers, and it
        refuses. It writes the reading, and a statement when a drift begins or
        ends, and never a capacity or a reservation."""
        record = self.record
        with record.reading():
            stored = self._stored()
            if stored is None:
                raise Refused(
                    "no allocation document is declared: it names the node, "
                    "the consumers and the work disk to measure"
                )
            document = stored[0]
            free, capacity = self._memory_table(document, now)
            row = latest(record)
        before = []
        if row is not None and isinstance(row["value"], dict):
            same = row["value"].get("node") == document.node
            drift = row["value"].get("drift")
            before = drift if same and isinstance(drift, list) else []
        probes = self.probes
        memory = probes.probe_memory()
        value = {
            "node": document.node,
            "memory_total": memory["memory_total"],
            "memory_available": memory["memory_available"],
            "method": memory["method"],
            "disk_free": probes.probe_disk(document.disk_path),
            "consumers": {},
        }
        probed = [c for c in document.consumers if c.probe is not None]
        table = probes.process_table() if probed else []
        for consumer in probed:
            found = probes.resident(consumer.probe, table)
            value["consumers"][consumer.name] = {"memory": found}
        value["drift"] = drift_entries(document, value, free, capacity)
        record.record_reading(MEASURE_SOURCE, MEASURE_KIND, value)
        now_whats = {entry["what"] for entry in value["drift"]}
        before_whats = {entry.get("what") for entry in before}
        for entry in value["drift"]:
            if entry["what"] not in before_whats:
                record.state("observation", began(entry), where="allocator")
        for entry in before:
            if entry.get("what") not in now_whats:
                record.state("observation", ended(entry), where="allocator")
        return value

    def drift(self, *, now):
        """What the latest measurement shows past the declaration, against the
        table as it stands: the list `measure` put in its reading. Reads
        only."""
        with self.record.reading():
            stored = self._stored()
            row = latest(self.record)
            if stored is None or row is None or not isinstance(row["value"], dict):
                return []
            free, capacity = self._memory_table(stored[0], now)
            return drift_entries(stored[0], row["value"], free, capacity)

    def resolve(self, document, *, fresh=False, now):
        """The figures for each "measure" in `document`: the one the latest
        document reading recorded for the same key, or, with none or with
        `fresh`, the probe. `{"capacity": {resource: n}, "consumers": {name:
        {resource: n}}}`. Writes nothing."""
        recorded = {}
        with self.record.reading():
            stored = self._stored()
        if stored is not None and not fresh:
            recorded = stored[1].get("figures") or {}
        old_capacity = recorded.get("capacity") or {}
        old_consumers = recorded.get("consumers") or {}
        probes = self.probes
        figures = {"capacity": {}, "consumers": {}}
        for resource in ("memory", "disk"):
            if document.capacity.get(resource) != "measure":
                continue
            if resource in old_capacity:
                figures["capacity"][resource] = old_capacity[resource]
            elif resource == "memory":
                figures["capacity"][resource] = probes.probe_memory()["memory_total"]
            else:
                held = sum(
                    probes.probe_size(c.path)
                    for c in document.consumers
                    if c.path is not None and "disk" in c.amounts
                )
                free = probes.probe_disk(document.disk_path)
                figures["capacity"][resource] = max(
                    free + held - document.floor.disk, 0
                )
        for consumer in document.consumers:
            if consumer.amounts.get("memory") != "measure":
                continue
            old = (old_consumers.get(consumer.name) or {}).get("memory")
            if old is None:
                old = self.sample_consumer(consumer.name, document=document)
            figures["consumers"][consumer.name] = {"memory": old}
        return figures

    def sample_consumer(
        self,
        name,
        *,
        seconds=600,
        every=10,
        document=None,
        progress=None,
        sleep=time.sleep,
    ):
        """The size to declare for consumer `name`: its probe sampled every
        `every` seconds for `seconds` (the document in `document`, else the
        declared one), its peak resident memory times 1.25, rounded up to a
        whole 256 MiB. `progress(number, total, peak)` is called after each
        sample. A probe that finds nothing resident is a `ProbeError`."""
        if document is None:
            with self.record.reading():
                stored = self._stored()
            document = stored[0] if stored else None
        found = [c for c in (document.consumers if document else ()) if c.name == name]
        if not found or found[0].probe is None:
            raise Refused(f"no probed consumer {name!r} in the allocation document")
        probe = found[0].probe
        total = seconds // every + 1
        top = 0
        for number in range(1, total + 1):
            top = max(top, self.probes.resident(probe, self.probes.process_table()))
            if progress is not None:
                progress(number, total, top)
            if number < total:
                sleep(every)
        if top == 0:
            raise ProbeError(
                f"sample_consumer: nothing is resident under {name}'s probe"
            )
        return default_probes.declared_size(top)

    # -- declare ------------------------------------------------------------------

    def declare(self, document, *, figures, now):
        """Bring the record to `document`, each "measure" taken from
        `figures` and nothing probed (`DocumentError` naming the key of one
        with no figure). The whole document is checked against the record
        first: a consumer that does not fit is a Refusal, and nothing is
        written. Each write is then one operation, refused whole; a refused
        one is in `failed`, the rest go on, and a second `declare` finishes
        it. The document reading is written last, and only when nothing
        failed."""
        figures = {} if figures is None else figures
        data = to_data(document, figures)
        resolved = document_from_data(data)
        digest = _digest(to_data(document))
        node = resolved.node
        with self.record.reading():
            plan = self._plan(resolved, now)
            before = self._stored()
        if plan["refusals"]:
            return Declaration(refusals=plan["refusals"])
        record = self.record
        wrote, failed = [], []

        def write(what, call):
            """`(True, result)`, or `(False, None)` with the record's reason
            in `failed`."""
            try:
                return True, call()
            except Refused as refused:
                failed.append((what, str(refused)))
                return False, None

        if write("node", lambda: record.declare_node(node, resolved.drivers, []))[1]:
            wrote.append(f"node {node}")
        for r, why in plan["release"]:
            what = f"release {r['holder']} {r['resource']}"
            if write(what, lambda: record.release_reservation(r["id"], why))[0]:
                wrote.append(f"{what}: {why}")
        for resource, amount in plan["grow"]:
            self._capacity(node, resource, amount, wrote, write)
        for action in plan["actions"]:
            self._act(node, action, now, wrote, failed, write)
        for resource, amount in plan["shrink"]:
            self._capacity(node, resource, amount, wrote, write)
        if not failed:
            same = (
                before is not None
                and before[1]["sha256"] == digest
                and schema.canonical(before[1]["figures"]) == schema.canonical(figures)
            )
            if not same:
                value = {"sha256": digest, "figures": figures, "document": data}
                record.record_reading(SOURCE, KIND, value)
                wrote.append(f"document {digest[:12]}")
        return Declaration(wrote=tuple(wrote), failed=tuple(failed))

    def _capacity(self, node, resource, amount, wrote, write):
        what = f"capacity {resource}"
        unit = unit_of(resource)

        def call():
            return self.record.set_capacity(node, resource, amount, unit)

        if write(what, call)[1]:
            wrote.append(f"{what} = {amount}")

    def _act(self, node, action, now, wrote, failed, write):
        """One write for a consumer: a hold for a resource it has none of, or
        a replace of the hold it has."""
        kind, name, resource, amount, old = action
        if kind == "replace":
            what = f"replace {name} {resource}"
            why = f"{name} {resource} changed in the allocation document"

            def call():
                return self.record.replace_reservation(old["id"], amount, why)

            if write(what, call)[0]:
                wrote.append(f"{what}: {old['amount']} -> {amount}")
            return
        what = f"hold {name} {resource}"

        def call():
            return self.hold(
                name, {resource: amount}, node=node, measured=False, now=now
            )

        done, made = write(what, call)
        if done and isinstance(made, Refusal):
            failed.append((what, made.text))
        elif done:
            wrote.append(f"{what} {amount}")

    def _plan(self, document, now):
        """What `declare` would do, read from the record: the reservations to
        release, the capacities to raise and to lower, and per consumer its
        replaces and holds; or the Refusal of each consumer that does not
        fit, checked in order against the room the earlier ones leave."""
        record = self.record
        node = document.node
        row = record.get("nodes", node)
        if row is not None and row["retired_at"] is not None:
            raise Refused(f"node {node} is retired")
        before = self._stored()
        past = {
            (c.name, resource)
            for c in (before[0].consumers if before else ())
            for resource in c.amounts
        }
        wanted = {(c.name, r) for c in document.consumers for r in c.amounts}
        capacity = {
            c["resource"]: c["amount"] for c in record.read("capacity", node=node)
        }
        live = record.read("reservations", live=True, node=node) if row else []
        release, kept = [], {}
        for r in sorted(live, key=lambda r: r["id"]):
            pair = (r["holder"], r["resource"])
            if not _standing(r) or pair not in past | wanted:
                continue
            if pair not in wanted:
                release.append((r, "no longer in the allocation document"))
            elif pair in kept:
                release.append((r, "a second hold of one consumer and resource"))
            else:
                kept[pair] = r
        gone = {r["id"] for r, _ in release} | {r["id"] for r in kept.values()}
        working = [dict(r) for r in live if r["id"] not in gone]
        target = {**capacity, **dict(document.capacity)}
        wanted_capacity = sorted(document.capacity.items())
        grow = [(r, a) for r, a in wanted_capacity if a > capacity.get(r, -1)]
        shrink = [(r, a) for r, a in wanted_capacity if a < capacity.get(r, a)]
        # The consumers go back into the room the others leave, the ones that
        # keep or lower their amount first: so a swap of two amounts is not
        # refused for the order they are written in.
        asks = [
            (c, resource, amount, kept.get((c.name, resource)))
            for c in document.consumers
            for resource, amount in sorted(c.amounts.items())
        ]
        lowered = [a for a in asks if a[3] is not None and a[2] <= a[3]["amount"]]
        raised = [a for a in asks if a not in lowered]
        shorts, actions = {}, []
        for consumer, resource, amount, old in lowered + raised:
            cap = target.get(resource, 0)
            mine = [w for w in working if w["resource"] == resource]
            short = room_short(
                node, resource, amount, cap, cap - peak(mine, now), working, now
            )
            if short is not None:
                shorts.setdefault(consumer.name, []).append(short)
                continue
            working.append(
                dict(
                    id=None,
                    resource=resource,
                    amount=amount,
                    holder=consumer.name,
                    run=None,
                    task=None,
                    from_at=now,
                    until_at=None,
                )
            )
            if old is None:
                actions.append(("hold", consumer.name, resource, amount, None))
            elif old["amount"] != amount or old["until_at"] is not None:
                actions.append(("replace", consumer.name, resource, amount, old))
        by_name = {c.name: c for c in document.consumers}
        refusals = {
            name: refuse(None, None, dict(by_name[name].amounts), found, node=node)
            for name, found in shorts.items()
        }
        return {
            "release": release,
            "grow": grow,
            "shrink": shrink,
            "actions": actions,
            "refusals": {n: r for n, r in refusals.items()},
        }
