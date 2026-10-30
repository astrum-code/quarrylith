# SPDX-License-Identifier: Apache-2.0
"""The journal's event and the fold.

The record is the fold of its journal. Every operation decides one `Event`
against the record as it stands, and the event carries the whole new value of
every record the operation writes (`puts`). `apply` writes those puts into a
view, in order, by key, and does nothing else: it reads no clock, no counter
and no other record, because every rule was decided before the event existed.
So applying one event twice leaves the record as applying it once did.

`beats` and `cursors` are bookkeeping, outside the fold: written without an
event, never replayed. `MemoryView` is the reference view; a backend brings
its own with the same `put(kind, key, value)`.
"""

import copy
from dataclasses import dataclass

from . import schema
from .schema import RECORDS, SchemaError

#: Who a caller says an operation came through.
VIAS = ("cli", "service", "hook", "import", "load")

#: Every kind of event. A new kind is a change to the record's page.
EVENT_KINDS = (
    "effort.file",
    "effort.edit",
    "effort.standing",
    "effort.conclude",
    "effort.link",
    "effort.unlink",
    "item.file",
    "item.edit",
    "item.state",
    "item.admit",
    "dep.add",
    "dep.lift",
    "lease.grant",
    "lease.renew",
    "lease.release",
    "task.file",
    "task.edit",
    "task.end",
    "node.declare",
    "node.retire",
    "capacity.set",
    "reserve",
    "reserve.release",
    "run.start",
    "run.phase",
    "run.finish",
    "change.open",
    "change.edit",
    "change.drop",
    "check.record",
    "review.file",
    "review.verdict",
    "review.read",
    "merge",
    "comment",
    "question.ask",
    "question.answer",
    "statement",
    "hold.set",
    "hold.lift",
    "reading",
    "meta.set",
    "note",
    "import",
)


@dataclass(frozen=True)
class Event:
    seq: int  # the journal's order, from 1, by one
    at: int  # the operation's one `now`
    actor: str  # who, as the caller names them
    via: str  # VIAS
    kind: str  # EVENT_KINDS
    subject: str | None  # "item:3159", "task:12", "effort:0001", "node:laptop"
    data: dict  # what a consumer needs without a second read
    puts: tuple  # ((kind, key, value), ...): every record written, whole

    def to_dict(self):
        """The event as plain data (lists for tuples), for a dump or a store."""
        return {
            "seq": self.seq,
            "at": self.at,
            "actor": self.actor,
            "via": self.via,
            "kind": self.kind,
            "subject": self.subject,
            "data": copy.deepcopy(self.data),
            "puts": [[k, list(key), copy.deepcopy(v)] for k, key, v in self.puts],
        }

    @classmethod
    def from_dict(cls, raw):
        puts = tuple((k, list(key), copy.deepcopy(v)) for k, key, v in raw["puts"])
        return cls(
            seq=raw["seq"],
            at=raw["at"],
            actor=raw["actor"],
            via=raw["via"],
            kind=raw["kind"],
            subject=raw["subject"],
            data=copy.deepcopy(raw["data"]),
            puts=puts,
        )


def check_event(event):
    """None if `event` is well formed, else a SchemaError: a known kind and
    via, a seq from 1, plain data, and every put a whole valid record of a
    journaled kind under its own key."""
    if not isinstance(event, Event):
        raise SchemaError(f"an event is an Event, not {type(event).__name__}")
    if event.kind not in EVENT_KINDS:
        raise SchemaError(f"event {event.seq}: no kind {event.kind!r}")
    if event.via not in VIAS:
        raise SchemaError(f"event {event.seq}: no via {event.via!r}")
    if not schema.is_int(event.seq) or event.seq < 1:
        raise SchemaError(f"event seq {event.seq!r}: from 1")
    if not schema.is_int(event.at) or event.at < 0:
        raise SchemaError(f"event {event.seq}: at {event.at!r} is not a time")
    if not isinstance(event.actor, str) or not event.actor:
        raise SchemaError(f"event {event.seq}: no actor")
    if event.subject is not None and (
        not isinstance(event.subject, str) or not event.subject
    ):
        raise SchemaError(f"event {event.seq}: subject {event.subject!r}")
    if not isinstance(event.data, dict):
        raise SchemaError(f"event {event.seq}: data is a dict")
    schema.check_plain(event.data, f"event {event.seq} data")
    if not isinstance(event.puts, tuple):
        raise SchemaError(f"event {event.seq}: puts are a tuple")
    for put in event.puts:
        if not isinstance(put, tuple) or len(put) != 3:
            raise SchemaError(f"event {event.seq}: a put is (kind, key, value)")
        kind, key, value = put
        if not schema.kind_of(kind).journaled:
            raise SchemaError(f"event {event.seq}: {kind} is bookkeeping")
        record = schema.validate(kind, value)
        if schema.check_key(kind, key) != schema.key_of(kind, record):
            raise SchemaError(f"event {event.seq}: a {kind} put under another key")


def apply(view, event):
    """Write `event.puts` into `view`, in order, by key. Nothing else."""
    for kind, key, value in event.puts:
        view.put(kind, key, value)


class MemoryView:
    """A record in memory: what `apply` folds a journal into. Each row is
    checked against the declarations, kept as its own copy, and read back as
    one, in the key order."""

    def __init__(self):
        self._rows = {kind: {} for kind in RECORDS}

    def put(self, kind, key, value):
        record = schema.validate(kind, value)
        if schema.check_key(kind, key) != schema.key_of(kind, record):
            raise SchemaError(f"{kind}: a put under {list(key)!r}, not its own key")
        self._rows[kind][tuple(key)] = record

    def get(self, kind, key):
        row = self._rows[schema.kind_of(kind).name].get(tuple(key))
        return copy.deepcopy(row)

    def keys(self, kind):
        return schema.sort_keys(self._rows[schema.kind_of(kind).name])

    def rows(self, kind):
        rows = self._rows[schema.kind_of(kind).name]
        return [copy.deepcopy(rows[tuple(key)]) for key in self.keys(kind)]

    def records(self):
        """Every journaled kind's rows, in key order: what `dump()` holds."""
        return {k: self.rows(k) for k, v in RECORDS.items() if v.journaled}

    def bookkeeping(self):
        return {k: self.rows(k) for k, v in RECORDS.items() if not v.journaled}
