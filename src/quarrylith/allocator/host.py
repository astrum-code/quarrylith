# SPDX-License-Identifier: Apache-2.0
"""The laptop plugin: the placement for one host, the node the declared
document names. It registers as `host` in the `quarrylith.placement` group.

It declares the document into the record (`declare`), measures the host
(`measure`, `resolve`, `sample_consumer`) and answers from the record's table,
the budget's readings and the host's latest measurement (`place`, `hold`,
`release`, `headroom`). `declare` probes nothing: the figures a "measure"
stands for are given to it, by `resolve`."""

import copy
import hashlib
import time
from dataclasses import dataclass, field

from ..record import schema
from ..record.contract import Refused
from .classes import classify, needs_of
from .document import MEASURE, document_from_data, to_data, unit_of
from . import probes as default_probes
from .budget import allowance, budget_shorts, providers_of
from .measure import (
    KIND as MEASURE_KIND,
    SOURCE as MEASURE_SOURCE,
    absent_began,
    absent_ended,
    began,
    drift_entries,
    ended,
    latest,
    measured_room,
    measured_shorts,
    measurement,
    pressure_shorts,
    pressure_view,
    reading_state,
    reserved_since,
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


def _drivers_of(document, drivers):
    """`drivers` as a list, or a plain `Refused`: it is a list (or tuple) of
    distinct, non-blank names, at least one (a node with none can run nothing),
    each one the document names."""
    if not isinstance(drivers, (list, tuple)):
        raise Refused(f"drivers is a list of names, not {drivers!r}")
    if not drivers:
        raise Refused("drivers names at least one: a node with none runs nothing")
    for name in drivers:
        if not isinstance(name, str) or not name.strip():
            raise Refused(f"drivers names are text, not {name!r}")
    if len(set(drivers)) != len(drivers):
        raise Refused(f"drivers names each driver once: {list(drivers)!r}")
    unknown = sorted(set(drivers) - set(document.drivers))
    if unknown:
        raise Refused(
            f"drivers {unknown} are not drivers the document names: "
            f"{sorted(document.drivers)}"
        )
    return list(drivers)


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

    def _pressure(self):
        """`(value, why)`: the host's memory pressure read now, live, or `(None,
        why)` when the read fails. It is read where a start is decided, outside
        any reading of the record, and never taken from a stored reading: a
        reading up to `floor.max_age_s` old could not stop what starts in that
        window."""
        try:
            return self.probes.probe_pressure(), ""
        except ProbeError as error:
            return None, str(error)

    def pressure(self):
        """What the host's memory pressure says now, as plain data: `level` (a
        name, or None where the host has none), `swap_used` and `swap_total`
        (bytes, or None), `refused` (what refuses new work, as words) and
        `unread` (why the read failed, else None). Reads the host, not the
        record."""
        return pressure_view(*self._pressure())

    # -- place, headroom ----------------------------------------------------------

    def place(self, task, *, now):
        record = self.record
        pressure = self._pressure()
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
                found, why = reading_state(record, document, node, now=now)
                reserved = (
                    reserved_since(record, document, node, now=now) if found else None
                )
                shorts += measured_shorts(found, document, node, needs, why, reserved)
                shorts += pressure_shorts(*pressure, node)
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
        pressure = self._pressure()
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
                    room = (
                        measured_room(
                            found,
                            document,
                            reserved_since(record, document, name, now=now),
                        )
                        if found
                        else None
                    )
                    for resource in ("memory", "disk"):
                        if resource in out[name]:
                            limit = room[resource] if room else 0
                            out[name][resource] = min(out[name][resource], limit)
                    for provider in providers_of(out[name]):
                        room, _ = allowance(record, document, name, provider, now=now)
                        if room is not None:
                            resource = f"budget.{provider}"
                            out[name][resource] = min(out[name][resource], room)
                    if pressure_shorts(*pressure, name):
                        # new work is refused on this host, so none may start
                        out[name] = dict.fromkeys(out[name], 0)
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
        # only new work meets the pressure: a consumer already running
        # (`measured` False) is in the host's state already
        pressure = self._pressure() if measured else None
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
                    found, why = None, "no allocation document is declared"
                    reserved = None
                    if document is not None:
                        found, why = reading_state(record, document, node, now=now)
                        if found:
                            reserved = reserved_since(record, document, node, now=now)
                    shorts += measured_shorts(
                        found, document, node, needs, why, reserved
                    )
                    shorts += pressure_shorts(*pressure, node)
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
        ends and when a consumer goes absent or comes back, and never a
        capacity or a reservation."""
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
            # the last reservation the record holds as it measures: what is
            # made after it is "since the reading", by the record's order and
            # not by the clocks the callers stamp with
            last = record.read("reservations", desc=True, limit=1)
            through = last[0]["id"] if last else 0
        before, before_absent = [], {}
        if row is not None and isinstance(row["value"], dict):
            same = row["value"].get("node") == document.node
            drift = row["value"].get("drift")
            before = drift if same and isinstance(drift, list) else []
            absent = row["value"].get("absent")
            before_absent = absent if same and isinstance(absent, dict) else {}
        probes = self.probes
        memory = probes.probe_memory()
        value = {
            "node": document.node,
            "memory_total": memory["memory_total"],
            "memory_available": memory["memory_available"],
            "method": memory["method"],
            "disk_free": probes.probe_disk(document.disk_path),
            "reserved_through": through,
            "consumers": {},
            "absent": {},
        }
        probed = [c for c in document.consumers if c.probe is not None]
        table = probes.process_table() if probed else []
        for consumer in probed:
            found, why = probes.resident(consumer.probe, table)
            if found is None:
                value["absent"][consumer.name] = why
            else:
                value["consumers"][consumer.name] = {"memory": found}
        value["drift"] = drift_entries(document, value, free, capacity)
        now_whats = {entry["what"] for entry in value["drift"]}
        before_whats = {entry.get("what") for entry in before}
        # the statements come first: one that fails leaves no reading that
        # holds a drift nobody was told of
        for entry in value["drift"]:
            if entry["what"] not in before_whats:
                record.state("observation", began(entry), where="allocator")
        for entry in before:
            if entry.get("what") not in now_whats:
                record.state("observation", ended(entry), where="allocator")
        # a consumer that goes absent, or comes back, is said once, so a probe
        # that has gone blind is seen as well as a service that is down
        for name, why in value["absent"].items():
            if name not in before_absent:
                record.state("observation", absent_began(name, why), where="allocator")
        for name in before_absent:
            if name not in value["absent"]:
                line = absent_ended(name, name in value["consumers"])
                record.state("observation", line, where="allocator")
        record.record_reading(MEASURE_SOURCE, MEASURE_KIND, value)
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

    def resolve(self, document, *, fresh=False, now, progress=None, sleep=time.sleep):
        """The figures for each "measure" in `document`: the one the latest
        document reading recorded for the same key, or, with none or with
        `fresh`, the probe. `{"capacity": {resource: n}, "consumers": {name:
        {resource: n}}}`. Writes nothing. `progress(name, number, total,
        peak)` is called after each sample of a consumer, and `sleep` waits
        between them."""
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
                old = self.sample_consumer(
                    consumer.name,
                    document=document,
                    sleep=sleep,
                    progress=(
                        None
                        if progress is None
                        else lambda n, total, top, name=consumer.name: progress(
                            name, n, total, top
                        )
                    ),
                )
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
        top, why = 0, ""
        for number in range(1, total + 1):
            found, why = self.probes.resident(probe, self.probes.process_table())
            if number == 1 and found is None:
                # down at the first sample: nothing to wait the window out for
                raise ProbeError(
                    f"sample_consumer: nothing is resident under {name}'s probe"
                    + (f" ({why})" if why else "")
                )
            top = max(top, found or 0)
            if progress is not None:
                progress(number, total, top)
            if number < total:
                sleep(every)
        if top == 0:
            raise ProbeError(
                f"sample_consumer: nothing is resident under {name}'s probe"
                + (f" ({why})" if why else "")
            )
        return default_probes.declared_size(top)

    # -- declare ------------------------------------------------------------------

    def declare(self, document, *, figures, drivers=None, now, dry_run=False):
        """Bring the record to `document`, each "measure" taken from
        `figures` and nothing probed (`DocumentError` naming the key of one
        with no figure). The whole document is checked against the record
        first: a consumer that does not fit is a Refusal, and nothing is
        written. Each write is then one operation, refused whole; a refused
        one is in `failed`, the rest go on, and a second `declare` finishes
        it. The document reading is written last, and only when nothing
        failed. With `dry_run` it writes nothing and returns the lines it
        would write, the ones a real `declare` then writes. `drivers`, when
        given, are what the node declares in place of the document's (the
        drivers the host has, of the ones the document names); the document
        stays whole in its reading, so what is declared and not available can
        be shown."""
        figures = {} if figures is None else figures
        data = to_data(document, figures)
        resolved = document_from_data(data)
        if drivers is not None:
            drivers = _drivers_of(resolved, drivers)
        digest = _digest(to_data(document))
        node = resolved.node
        drivers = sorted(resolved.drivers if drivers is None else drivers)
        with self.record.reading():
            plan = self._plan(resolved, now, drivers)
            before = self._stored()
        if plan["refusals"]:
            return Declaration(refusals=plan["refusals"])
        same = (
            before is not None
            and before[1]["sha256"] == digest
            and schema.canonical(before[1]["figures"]) == schema.canonical(figures)
        )
        if dry_run:
            return Declaration(wrote=tuple(self._lines(plan, node, digest, same)))
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

        if plan["node"]:
            if write("node", lambda: record.declare_node(node, drivers, []))[1]:
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
            if not same:
                value = {"sha256": digest, "figures": figures, "document": data}
                record.record_reading(SOURCE, KIND, value)
                wrote.append(f"document {digest[:12]}")
        return Declaration(wrote=tuple(wrote), failed=tuple(failed))

    @staticmethod
    def _lines(plan, node, digest, same):
        """The lines `declare` writes for `plan`, in the order it writes
        them."""
        lines = [f"node {node}"] if plan["node"] else []
        for r, why in plan["release"]:
            lines.append(f"release {r['holder']} {r['resource']}: {why}")
        lines += [f"capacity {r} = {a}" for r, a in plan["grow"]]
        for kind, name, resource, amount, old in plan["actions"]:
            if kind == "replace":
                lines.append(f"replace {name} {resource}: {old['amount']} -> {amount}")
            else:
                lines.append(f"hold {name} {resource} {amount}")
        lines += [f"capacity {r} = {a}" for r, a in plan["shrink"]]
        if not same:
            lines.append(f"document {digest[:12]}")
        return lines

    def declared(self):
        """`(document, figures)` in force, or None before one: the document as
        it was declared, its "measure" kept, and the figures recorded with it.
        Rebuilt from the reading, and refused when what it rebuilds is not
        what the reading's sha256 fingerprints."""
        with self.record.reading():
            stored = self._stored()
        if stored is None:
            return None
        value = stored[1]
        figures = value.get("figures") or {}
        data = copy.deepcopy(value["document"])
        for resource in figures.get("capacity") or {}:
            data["capacity"][resource] = MEASURE
        for consumer in data.get("consumer", []):
            for resource in (figures.get("consumers") or {}).get(consumer["name"], {}):
                consumer[resource] = MEASURE
        if _digest(data) != value["sha256"]:
            raise Refused(
                "the declared document cannot be told from its record: declare "
                "the document again"
            )
        return document_from_data(data), figures

    def check_resize(self, name):
        """The size of consumer `name` is the record's to set, or `Refused`
        saying why not: no document, no probed consumer of that name, or its
        memory a fixed amount in the declared document."""
        current = self.declared()
        document = current[0] if current else None
        found = [c for c in (document.consumers if document else ()) if c.name == name]
        if not found or found[0].probe is None:
            raise Refused(f"no probed consumer {name!r} in the allocation document")
        if "memory" not in (current[1].get("consumers") or {}).get(name, {}):
            raise Refused(
                f"{name}'s memory is a fixed amount in the declared document, "
                f'not "{MEASURE}": put the size in the document and declare it'
            )

    def resize(self, name, size, *, now):
        """Declare consumer `name`'s measured memory as `size`: the document in
        force declared again with that figure, which replaces its reservation
        in one event and records the figure. The Declaration, a Refusal in it
        when the size does not fit."""
        self.check_resize(name)
        document, figures = self.declared()
        figures = copy.deepcopy(figures)
        figures["consumers"][name]["memory"] = size
        # the node keeps the drivers it was declared with, not the document's
        row = self.record.get("nodes", document.node)
        drivers = row["drivers"] if row and row["retired_at"] is None else None
        return self.declare(document, figures=figures, drivers=drivers, now=now)

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

    def _plan(self, document, now, drivers):
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
            "node": row is None or row["drivers"] != drivers or row["labels"] != [],
            "release": release,
            "grow": grow,
            "shrink": shrink,
            "actions": actions,
            "refusals": {n: r for n, r in refusals.items()},
        }
