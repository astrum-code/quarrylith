# SPDX-License-Identifier: Apache-2.0
"""The laptop plugin: the placement for one host, the node the declared
document names. It registers as `host` in the `quarrylith.placement` group.

It declares the document into the record (`declare`) and answers from the
record's table (`place`, `hold`, `release`, `headroom`). It probes nothing:
the figures a "measure" stands for are given to `declare`, and the budget and
the host's measured room are not yet part of an answer."""

import hashlib
from dataclasses import dataclass, field

from ..record import schema
from ..record.contract import Refused
from .classes import classify, needs_of
from .document import document_from_data, to_data, unit_of
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

    # -- the document in force ----------------------------------------------------

    def _stored(self):
        """`(document, reading value)` of the latest `allocator/document`
        reading, or None before one."""
        rows = self.record.read(
            "readings", source=SOURCE, kind=KIND, desc=True, limit=1
        )
        if not rows:
            return None
        value = rows[0]["value"]
        return document_from_data(value["document"]), value

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
        return out

    # -- hold, release ------------------------------------------------------------

    def hold(self, holder, needs, *, task=None, node=None, measured=True, now):
        """A standing reservation, or a task's own, checked against the table
        as `place` checks it. `measured` is the host's measured room, which a
        later step reads; the table is checked either way. A `Refusal` when it
        does not fit, and `Refused` for a second live hold of one holder and
        resource on the node."""
        if not isinstance(needs, dict) or not needs:
            raise Refused(f"a hold names its needs, {{resource: amount}}: {needs!r}")
        for resource, amount in needs.items():
            if not schema.is_int(amount) or amount < 1:
                raise Refused(
                    f"hold {resource}: an amount is an int from 1: {amount!r}"
                )
        record = self.record
        with record.reading():
            if node is None:
                stored = self._stored()
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
                    ):
                        raise Refused(
                            f"{holder} holds {r['resource']} on {node} already "
                            f"(reservation {r['id']}): replace it or release it first"
                        )
                shorts += table_shorts(record, node, needs, now=now)
        if shorts:
            return refuse(task, None, needs, shorts, node=node)
        return record.reserve(
            node, needs, task=task, holder=holder, from_at=now, until_at=None
        )

    def release(self, holder, *, node=None, why):
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
        for step in plan["consumers"]:
            self._consumer(node, step, now, wrote, failed, write)
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

    def _consumer(self, node, step, now, wrote, failed, write):
        """One consumer's writes: a replace for each amount that changed, and
        one hold for the resources it has none of."""
        name = step["name"]
        for old, resource, amount in step["replace"]:
            what = f"replace {name} {resource}"
            why = f"{name} {resource} changed in the allocation document"

            def swap(old=old, amount=amount, why=why):
                return self.record.replace_reservation(old["id"], amount, why)

            if write(what, swap)[0]:
                wrote.append(f"{what}: {old['amount']} -> {amount}")
        if not step["hold"]:
            return

        def take():
            return self.hold(name, step["hold"], node=node, measured=False, now=now)

        what = f"hold {name}"
        done, made = write(what, take)
        if done and isinstance(made, Refusal):
            failed.append((what, made.text))
        elif done:
            for resource, amount in sorted(step["hold"].items()):
                wrote.append(f"hold {name} {resource} {amount}")

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
        gone = {r["id"] for r, _ in release}
        working = [dict(r) for r in live if r["id"] not in gone]
        target = {**capacity, **dict(document.capacity)}
        wanted_capacity = sorted(document.capacity.items())
        grow = [(r, a) for r, a in wanted_capacity if a > capacity.get(r, -1)]
        shrink = [(r, a) for r, a in wanted_capacity if a < capacity.get(r, a)]
        refusals, steps = {}, []
        for consumer in document.consumers:
            step = {"name": consumer.name, "replace": [], "hold": {}}
            shorts = []
            for resource, amount in sorted(consumer.amounts.items()):
                old = kept.get((consumer.name, resource))
                if old and old["amount"] == amount and old["until_at"] is None:
                    continue
                others = [w for w in working if old is None or w["id"] != old["id"]]
                cap = target.get(resource, 0)
                mine = [w for w in others if w["resource"] == resource]
                short = room_short(
                    node, resource, amount, cap, cap - peak(mine, now), others, now
                )
                if short is not None:
                    shorts.append(short)
                    continue
                taken = dict(
                    id=None,
                    resource=resource,
                    amount=amount,
                    holder=consumer.name,
                    run=None,
                    task=None,
                    from_at=now,
                    until_at=None,
                )
                working = others + [taken]
                if old is None:
                    step["hold"][resource] = amount
                else:
                    step["replace"].append((old, resource, amount))
            if shorts:
                needs = dict(consumer.amounts)
                refusals[consumer.name] = refuse(None, None, needs, shorts, node=node)
            steps.append(step)
        return {
            "release": release,
            "grow": grow,
            "shrink": shrink,
            "consumers": steps,
            "refusals": refusals,
        }
