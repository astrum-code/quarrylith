# SPDX-License-Identifier: Apache-2.0
"""The record's contract: `Record`, the operations written once over it, and
`open_record`.

Every operation decides one event against the record as it stands: it reads,
checks its rules, asks the caller's `authorize`, and puts the whole new value
of every record it writes. The backend stores the event and applies its puts
in one step (SQLite: one transaction), so an operation happens whole or not at
all. A refused one writes nothing: no record, no event, no counter.

A backend brings the store: reads of one record and of a kind, the journal,
one write step, and its own administration. What an operation means is
written here, once, for every backend.
"""

import abc
import contextlib
import copy
import threading
import time

from . import events as ev
from . import schema
from .schema import ITEM_STATES, RECORDS, SchemaError


class Refused(Exception):
    """The operation broke a rule and wrote nothing."""

    def __init__(self, reason):
        self.reason = str(reason)
        super().__init__(self.reason)


class Unavailable(Exception):
    """The store could not be read or written, and the operation did not
    happen. Raised from the backend's own error."""


class Unknown(Unavailable):
    """The store failed at or after the commit point: the operation may have
    happened. A caller resolves it by reading (the event's subject, or
    `events()` past its last seq)."""


CLOSED_STATES = ("done", "dropped")
OPEN_STATES = tuple(s for s in ITEM_STATES if s not in CLOSED_STATES)


def _pairs(froms, tos):
    return {(f, t) for f in froms for t in tos}


#: Every state pair an item may move by: the legacy board's moves, without
#: their who and when, which are the caller's `authorize`.
MOVES = frozenset().union(
    _pairs(["triage"], ["ready"]),
    _pairs(["ready", "blocked"], ["claimed"]),
    _pairs(["claimed"], ["blocked", "in-review", "ready"]),
    _pairs(["ready"], ["blocked"]),
    _pairs(["blocked"], ["ready"]),
    _pairs(["in-review"], ["reviewed"]),
    _pairs(["reviewed"], ["merge-ready"]),
    _pairs(["merge-ready"], ["reviewed"]),
    _pairs(["in-review", "reviewed", "merge-ready"], ["ready", "done"]),
    _pairs(["ready", "claimed", "blocked"], ["done", "dropped"]),
    _pairs(OPEN_STATES, CLOSED_STATES),
    _pairs(CLOSED_STATES, ["ready", "triage"]),
)

#: The effort an item filed with no effort and no parent is linked to.
UNSORTED = "unsorted"
#: The six parts of an effort.
EFFORT_PARTS = ("statement", "inputs", "tools", "budget", "evidence", "gates")
EFFORT_EDITS = ("slug", "title", "owner", "order") + EFFORT_PARTS
ITEM_EDITS = (
    "kind",
    "title",
    "body",
    "role",
    "tier",
    "risk",
    "parent",
    "labels",
    "flags",
    "templet",
    "review_round",
    "overruled_at",
    "must_fix",
)
#: The kinds that carry a text record, read with `get(..., text=True)`.
TEXTS = {"items": ("item_text", "body"), "changes": ("change_text", "body")}


def wall_ms():
    return time.time_ns() // 1_000_000


def _key(kind, key):
    """`key` as a key of `kind`: a list, a scalar standing for a one-field
    key. Refused when it is not one."""
    if not isinstance(key, (list, tuple)):
        key = [key]
    try:
        return schema.check_key(kind, key)
    except SchemaError as e:
        raise Refused(str(e)) from None


def _kind(kind):
    try:
        return schema.kind_of(kind)
    except SchemaError as e:
        raise Refused(str(e)) from None


def _matches(kind, record, match, live):
    for name, value in match.items():
        if record[name] != value or isinstance(record[name], bool) != isinstance(
            value, bool
        ):
            return False
    if live is not None:
        is_live = all(record[f] is None for f in RECORDS[kind].live)
        if is_live != live:
            return False
    return True


def _linked(reader, number):
    """The efforts an item is linked to itself, live."""
    return sorted(
        link["effort"]
        for link in reader.read("effort_links", live=True, target=str(number))
        if link["kind"] == "item"
    )


def _efforts_of(reader, item):
    """The efforts an item is linked to: its own, else its parent's chain's."""
    seen = set()
    while item is not None and item["number"] not in seen:
        seen.add(item["number"])
        own = _linked(reader, item["number"])
        if own:
            return own
        parent = item["parent"]
        item = reader.get("items", [parent]) if parent is not None else None
    return []


def unlinked_items(records):
    """The numbers of the items with no effort link, own or through their
    parent chain, to an effort the records hold (invariant 10)."""
    efforts = {e["id"] for e in records.get("efforts", ())}
    linked = set()
    for link in records.get("effort_links", ()):
        if (
            link["kind"] == "item"
            and link["unlinked_at"] is None
            and link["effort"] in efforts
        ):
            linked.add(link["target"])
    items = {i["number"]: i for i in records.get("items", ())}
    out = []
    for number, item in items.items():
        seen = set()
        while item is not None and item["number"] not in seen:
            seen.add(item["number"])
            if str(item["number"]) in linked:
                break
            item = items.get(item["parent"])
        else:
            out.append(number)
    return out


class _Nothing(Exception):
    """An operation that found nothing to change: it writes nothing."""


class _Op:
    """One operation in progress. Its reads see its own puts, and its puts,
    in order, become one event."""

    def __init__(self, record, name, kind, subject, now):
        self.record = record
        self.name = name
        self.kind = kind
        self.subject = subject
        self.now = now
        self.data = {}
        self.seq = None
        self.empty_ok = False
        self._puts = {}  # (kind, key tuple) -> record, in put order
        self._before = {}  # (kind, key tuple) -> the record before, or None

    def get(self, kind, key):
        key = tuple(_key(kind, key))
        if (kind, key) in self._puts:
            return copy.deepcopy(self._puts[(kind, key)])
        return self.record._get(kind, list(key))

    def need(self, kind, key, what=None):
        found = self.get(kind, key)
        if found is None:
            shown = key[0] if isinstance(key, (list, tuple)) and len(key) == 1 else key
            raise Refused(f"no {what or kind} {shown}")
        return found

    def read(self, kind, /, *, live=None, **match):
        rows = {
            tuple(schema.key_of(kind, r)): r
            for r in self.record._read(kind, match, live, False, None)
        }
        for (k, key), value in self._puts.items():
            if k == kind:
                rows.pop(key, None)
                if _matches(kind, value, match, live):
                    rows[key] = copy.deepcopy(value)
        return [rows[tuple(key)] for key in schema.sort_keys(rows)]

    def put(self, kind, value):
        if not RECORDS[kind].journaled:
            raise AssertionError(f"{kind} is bookkeeping, outside the journal")
        try:
            value = schema.validate(kind, value)
        except SchemaError as e:
            raise Refused(str(e)) from None
        key = tuple(schema.key_of(kind, value))
        if (kind, key) not in self._before:
            self._before[(kind, key)] = self.record._get(kind, list(key))
        self._puts[(kind, key)] = value
        return value

    def take(self, counter):
        """The next number of `counter` (`next:<kind>`), from 1."""
        row = self.get("meta", [counter])
        value = row["value"] if row else 1
        self.put("meta", {"key": counter, "value": value + 1})
        return value

    def past(self, counter, used):
        """Move `counter` past `used`, a number given from outside it."""
        row = self.get("meta", [counter])
        if used >= (row["value"] if row else 1):
            self.put("meta", {"key": counter, "value": used + 1})

    def nothing(self):
        raise _Nothing()

    def event(self, seq, actor, via):
        puts = tuple((k, list(key), v) for (k, key), v in self._puts.items())
        return ev.Event(
            seq=seq,
            at=self.now,
            actor=actor,
            via=via,
            kind=self.kind,
            subject=self.subject,
            data=schema.check_plain(self.data, "event data"),
            puts=puts,
        )

    def priors(self):
        """For each put, the values its `match` fields held before (null for
        a record that did not exist): what makes an index move redoable."""
        out = []
        for k, key in self._puts:
            before = self._before[(k, key)]
            held = None
            if before is not None:
                held = {f: before[f] for f in RECORDS[k].match}
            out.append([k, list(key), held])
        return out

    def before_after(self):
        before = [[k, list(key), self._before[(k, key)]] for k, key in self._puts]
        after = [[k, list(key), copy.deepcopy(v)] for (k, key), v in self._puts.items()]
        return before, after


def entry_size(event, priors):
    """The journal entry's measure: the event and its priors, by `size`."""
    return schema.size({"event": event.to_dict(), "priors": priors})


class Record(abc.ABC):
    """The record behind one contract. A backend implements the store's
    primitives (the abstract methods); the operations, the reads and the
    journal's delivery are written here once."""

    #: The backend's name in `BACKENDS`.
    backend = None

    def __init__(self, *, actor, via, authorize=None, readonly=False, shared=False):
        if not isinstance(actor, str) or not actor:
            raise Refused("an actor is a non-empty str")
        if via not in ev.VIAS:
            raise Refused(f"no via {via!r}: one of {', '.join(ev.VIAS)}")
        self.actor = actor
        self.via = via
        self.authorize = authorize
        self.readonly = readonly
        self._shared = shared
        #: The record's clock: milliseconds since the epoch. Tests move it.
        self.clock = wall_ms
        self._lock = threading.RLock()
        self._reading_depth = 0
        self._woken = threading.Condition()
        self._commits = 0

    # -- the store's primitives, per backend ----------------------------------

    @abc.abstractmethod
    def _get(self, kind, key):
        """The record of `kind` at `key` (a checked list), or None."""

    @abc.abstractmethod
    def _read(self, kind, match, live, desc, limit):
        """The records of `kind` whose fields equal `match`, live or not, in
        key order (reversed when `desc`), at most `limit`."""

    @abc.abstractmethod
    def _head(self):
        """The highest seq in the journal, 0 when it is empty."""

    @abc.abstractmethod
    def _write(self):
        """A context manager: one write step. Its body's writes happen whole
        or not at all; an exception in it writes nothing."""

    @abc.abstractmethod
    def _store(self, event, priors):
        """Inside `_write`: store `event` at its seq, the next, and apply its
        puts."""

    @abc.abstractmethod
    def _events(self, cursor, limit, subject):
        """The events after `cursor`, in seq order, at most `limit` (None:
        all), of `subject` when given."""

    @abc.abstractmethod
    def _changed(self):
        """A token that changes when another process may have committed."""

    @abc.abstractmethod
    def _bookkeep(self, kind, row):
        """Inside `_write`: write a bookkeeping row (a cursor, a beat), no
        event. A cursor takes the larger seq."""

    @abc.abstractmethod
    def _snapshot(self):
        """A context manager inside which every read sees one seq."""

    @abc.abstractmethod
    def _empty(self):
        """True when the record holds no event and no record."""

    @abc.abstractmethod
    def _check_store(self):
        """The backend's own findings, a list of str."""

    @property
    @abc.abstractmethod
    def initialised(self):
        """Has `init()` made the record?"""

    @abc.abstractmethod
    def init(self):
        """Make the record, empty. Refused if one exists."""

    @property
    @abc.abstractmethod
    def durable(self):
        """Does an acknowledged event survive a power cut?"""

    @abc.abstractmethod
    def backup(self, dir):
        """A consistent copy of the record in `dir`; its path."""

    @abc.abstractmethod
    def close(self):
        """Close the record."""

    # -- the common parts -----------------------------------------------------

    @property
    def shared(self):
        """Was the record opened as the one a yard runs on?"""
        return self._shared

    def now_ms(self):
        now = self.clock()
        if not schema.is_int(now) or now < 0:
            raise Refused(f"the clock gave {now!r}, not a time in milliseconds")
        return now

    def _usable(self):
        if not self.initialised:
            raise Refused("no record here yet: init() makes one")

    def _writable(self):
        if self.readonly:
            raise Refused("the record is open read-only")
        self._usable()
        if self._reading_depth:
            raise Refused("no operation runs inside reading()")

    def _wake(self):
        with self._woken:
            self._commits += 1
            self._woken.notify_all()

    @contextlib.contextmanager
    def _operation(self, name, kind, subject=None):
        """One operation: `with self._operation(...) as op`, then the op's
        reads and puts. At the end the event is decided, authorized, measured
        and stored, in the backend's one write step."""
        self._writable()
        op = None
        with self._lock:
            try:
                with self._write():
                    op = _Op(self, name, kind, subject, self.now_ms())
                    yield op
                    if not op._puts and not op.empty_ok:
                        op.nothing()
                    self._commit(op)
            except _Nothing:
                return
        self._wake()

    def _commit(self, op):
        before, after = op.before_after()
        if self.authorize is not None:
            reason = self.authorize(self.actor, op.name, before, after)
            if reason is not None:
                raise Refused(reason)
        event = op.event(self._head() + 1, self.actor, self.via)
        priors = op.priors()
        self._check_entry(event, priors)
        self._store(event, priors)
        op.seq = event.seq

    def _check_entry(self, event, priors):
        limit = schema.LIMITS["entry_bytes"]
        size = entry_size(event, priors)
        if size > limit:
            raise Refused(
                f"a journal entry of {size} bytes (its event "
                f"{schema.size(event.to_dict())}), past the limit of {limit}"
            )

    # -- reads ----------------------------------------------------------------

    def get(self, kind, key, *, text=False):
        """The record of `kind` at `key`, or None. With `text`, an item
        carries its `body` (a change its body too)."""
        _kind(kind)
        key = _key(kind, key)
        if text and kind not in TEXTS:
            raise Refused(f"{kind} has no text")
        with self._lock:
            self._usable()
            found = self._get(kind, key)
            if found is not None and text:
                text_kind, field = TEXTS[kind]
                row = self._get(text_kind, key)
                found[field] = row[field] if row else None
        return found

    def read(self, kind, /, *, live=None, desc=False, limit=None, **match):
        """The records whose `match` fields equal the values given, in key
        order. A match on a field outside the kind's `match` is refused."""
        k = _kind(kind)
        by_name = {f.name: f for f in k.all_fields}
        for name, value in match.items():
            if name not in k.match:
                raise Refused(f"{kind} is not read by {name}: only {list(k.match)}")
            f = by_name[name]
            if value is None:
                if not f.null:
                    raise Refused(f"{kind}.{name} is never null")
                continue
            if f.type == schema.INT and not schema.is_int(value):
                raise Refused(f"{kind}.{name} is an int, not {value!r}")
            if f.type == schema.STR and not isinstance(value, str):
                raise Refused(f"{kind}.{name} is a str, not {value!r}")
            if f.type == schema.BOOL and not isinstance(value, bool):
                raise Refused(f"{kind}.{name} is a bool, not {value!r}")
        if live is not None and not k.live:
            raise Refused(f"{kind} has no end field: it is never live or ended")
        if limit is not None and (not schema.is_int(limit) or limit < 0):
            raise Refused(f"a limit is an int, from 0, not {limit!r}")
        with self._lock:
            self._usable()
            return self._read(kind, match, live, bool(desc), limit)

    @contextlib.contextmanager
    def reading(self):
        """Every read inside sees the record at one seq, which it yields."""
        with self._lock:
            self._usable()
            if self._reading_depth:
                self._reading_depth += 1
                try:
                    yield self._head()
                finally:
                    self._reading_depth -= 1
                return
            with self._snapshot():
                self._reading_depth = 1
                try:
                    yield self._head()
                finally:
                    self._reading_depth = 0

    def ready(self, *, now):
        """The ready read: the items in `ready` that are eligible, and the
        open tasks that are (tasks come with their records, in a later step).
        An item is eligible when it is admitted, an effort it is linked to
        (its own links, else its parent chain's) is admitted, it has no live
        dep and no live hold, and neither its holder nor its role has one."""
        if not schema.is_int(now):
            raise Refused(f"now is a time in milliseconds, not {now!r}")
        with self.reading():
            items = [i for i in self.read("items", state="ready") if self._eligible(i)]
        return {"items": items, "tasks": []}

    def _eligible(self, item):
        n = item["number"]
        if not item["admitted"]:
            return False
        efforts = _efforts_of(self, item)
        if not any(
            (self.get("efforts", e) or {}).get("standing") == "admitted"
            for e in efforts
        ):
            return False
        if self.read("deps", live=True, number=n):
            return False
        if self.read("holds", live=True, number=n):
            return False
        whos = {lease["holder"] for lease in self.read("leases", live=True, number=n)}
        if item["role"] is not None:
            whos.add(item["role"])
        return not any(self.read("holds", live=True, who=who) for who in whos)

    # -- the journal, delivered -----------------------------------------------

    def head(self):
        """The highest seq, 0 when the journal is empty."""
        with self._lock:
            self._usable()
            return self._head()

    def events(self, cursor, *, limit=500, wait=0.0, subject=None):
        """The events after `cursor`, in seq order, at most `limit`. With
        `wait`, up to that many seconds for the first one."""
        if not schema.is_int(cursor) or cursor < 0:
            raise Refused(f"a cursor is a seq, from 0, not {cursor!r}")
        if not schema.is_int(limit) or limit < 1:
            raise Refused(f"a limit is an int, from 1, not {limit!r}")
        if isinstance(wait, bool) or not isinstance(wait, (int, float)) or wait < 0:
            raise Refused(f"wait is seconds, from 0, not {wait!r}")
        end = time.monotonic() + wait
        while True:
            with self._lock:
                self._usable()
                seen = (self._commits, self._changed())
                got = self._events(cursor, limit, subject)
            if got:
                return got
            while True:
                left = end - time.monotonic()
                if left <= 0:
                    return []
                with self._woken:
                    self._woken.wait(min(0.25, left))
                with self._lock:
                    now = (self._commits, self._changed())
                if now != seen:
                    break

    def cursor(self, consumer):
        """A consumer's place in the journal: the last seq it acked, or 0."""
        _key("cursors", [consumer])
        with self._lock:
            self._usable()
            row = self._get("cursors", [consumer])
        return row["seq"] if row else 0

    def ack(self, consumer, seq):
        """Move a consumer's cursor forward to `seq` (an older seq leaves it).
        Bookkeeping: no event."""
        _key("cursors", [consumer])
        self._writable()
        if not schema.is_int(seq) or seq < 0:
            raise Refused(f"a seq is an int, from 0, not {seq!r}")
        with self._lock:
            with self._write():
                if seq > self._head():
                    raise Refused(f"seq {seq} is past the journal's head")
                self._bookkeep(
                    "cursors", {"consumer": consumer, "seq": seq, "at": self.now_ms()}
                )

    # -- administration -------------------------------------------------------

    def dump(self):
        """Every journaled record in key order, the journal, and the
        bookkeeping, at one seq: `{"format", "records", "journal",
        "bookkeeping"}`."""
        with self.reading():
            records = {
                k: self._read(k, {}, None, False, None)
                for k, v in RECORDS.items()
                if v.journaled
            }
            journal = [e.to_dict() for e in self._events(0, None, None)]
            bookkeeping = {
                k: self._read(k, {}, None, False, None)
                for k, v in RECORDS.items()
                if not v.journaled
            }
        return {
            "format": 1,
            "records": records,
            "journal": journal,
            "bookkeeping": bookkeeping,
        }

    def check(self):
        """The findings, a list of str: the replay of the journal against the
        records, the seqs, the items with no effort link, the counters behind
        their ids, then the backend's own. Empty when the record is whole."""
        with self.reading():
            dump = self.dump()
            findings = _findings(dump)
            findings += self._check_store()
        return findings

    def load(self, dump):
        """Write a dump into an empty record: every event with its seq,
        applied in order, then the bookkeeping, in one write step. Refused
        when the record is not empty, or the dump is not whole: its seqs not
        from 1 by one, its records not the fold of its journal, an entry past
        the limit, an item with no effort link."""
        self._writable()
        if not isinstance(dump, dict) or set(dump) != {
            "format",
            "records",
            "journal",
            "bookkeeping",
        }:
            raise Refused("a dump is {format, records, journal, bookkeeping}")
        if dump["format"] != 1:
            raise Refused(f"a dump of format {dump['format']!r}: this reads 1")
        journaled = {k for k, v in RECORDS.items() if v.journaled}
        if not isinstance(dump["records"], dict) or not set(dump["records"]) <= (
            journaled
        ):
            raise Refused("a dump's records are by journaled kind")
        kept = {k for k, v in RECORDS.items() if not v.journaled}
        if (
            not isinstance(dump["bookkeeping"], dict)
            or not set(dump["bookkeeping"]) <= kept
        ):
            raise Refused("a dump's bookkeeping is beats and cursors")
        try:
            journal = [ev.Event.from_dict(raw) for raw in dump["journal"]]
            for event in journal:
                ev.check_event(event)
        except (SchemaError, KeyError, TypeError, ValueError) as e:
            raise Refused(f"a dump's journal: {e}") from None
        for i, event in enumerate(journal, 1):
            if event.seq != i:
                raise Refused(f"a dump's seqs run from 1 by one: {event.seq} at {i}")
        with self._lock:
            with self._write():
                if not self._empty():
                    raise Refused("a load goes into an empty record")
                for event in journal:
                    priors = []
                    for kind, key, _ in event.puts:
                        before = self._get(kind, list(key))
                        held = None
                        if before is not None:
                            held = {f: before[f] for f in RECORDS[kind].match}
                        priors.append([kind, list(key), held])
                    self._check_entry(event, priors)
                    self._store(event, priors)
                for kind in sorted(kept):
                    for row in dump["bookkeeping"].get(kind, []):
                        try:
                            row = schema.validate(kind, row)
                        except SchemaError as e:
                            raise Refused(f"a dump's {kind}: {e}") from None
                        if kind == "cursors" and row["seq"] > len(journal):
                            raise Refused(f"a cursor past the journal: {row}")
                        self._bookkeep(kind, row)
                records = {}
                for kind in sorted(journaled):
                    records[kind] = self._read(kind, {}, None, False, None)
                    if records[kind] != dump["records"].get(kind, []):
                        raise Refused(
                            f"a dump's {kind} are not the fold of its journal"
                        )
                missing = unlinked_items(records)
                if missing:
                    raise Refused(
                        "every item has an effort link: "
                        + ", ".join(f"item {n}" for n in missing)
                        + " has none"
                    )
        self._wake()

    # -- efforts --------------------------------------------------------------

    def file_effort(self, id, slug, title, owner, parts, standing="proposed"):
        """File an effort; `id` None takes the next from `next:efforts`, as
        four digits. Refused for an id taken. Returns the id."""
        if standing not in ("proposed", "admitted", "parked"):
            raise Refused(
                f"an effort is filed proposed, admitted or parked: {standing}"
            )
        if not isinstance(parts, dict) or not set(parts) <= set(EFFORT_PARTS):
            raise Refused(f"an effort's parts are {', '.join(EFFORT_PARTS)}")
        with self._operation("file_effort", "effort.file") as op:
            if id is None:
                id = f"{op.take('next:efforts'):04d}"
                while op.get("efforts", [id]) is not None:
                    id = f"{op.take('next:efforts'):04d}"
            else:
                if not isinstance(id, str) or not id:
                    raise Refused(f"an effort's id is a non-empty str, not {id!r}")
                if op.get("efforts", [id]) is not None:
                    raise Refused(f"effort {id}: the id is taken")
                if id.isascii() and id.isdigit():
                    op.past("next:efforts", int(id))
            record = {
                "id": id,
                "slug": slug,
                "title": title,
                "standing": standing,
                "owner": owner,
                "created_at": op.now,
                "statement": parts.get("statement", ""),
            }
            for part in EFFORT_PARTS[1:]:
                record[part] = parts.get(part, {})
            op.put("efforts", record)
            op.subject = f"effort:{id}"
            op.data = {"id": id, "standing": standing}
        return id

    def edit_effort(self, id, **fields):
        """Edit an effort's slug, title, owner, order or parts. False when
        nothing changed (and nothing is written)."""
        for name in fields:
            if name not in EFFORT_EDITS:
                raise Refused(f"an effort's {name} is not edited: {EFFORT_EDITS}")
        if not fields:
            raise Refused("an edit names a field")
        with self._operation("edit_effort", "effort.edit", f"effort:{id}") as op:
            effort = op.need("efforts", [id], "effort")
            after = {**effort, **fields}
            if after == effort:
                op.nothing()
            op.put("efforts", after)
            op.data = {"id": id, "fields": sorted(fields)}
        return op.seq is not None

    def set_standing(self, id, standing, why):
        """Set an effort's standing (proposed, admitted, parked). Refused for
        a concluded effort; `conclude` concludes."""
        if standing not in ("proposed", "admitted", "parked"):
            raise Refused(
                f"a standing is proposed, admitted or parked, not {standing!r}: "
                "conclude() concludes"
            )
        with self._operation("set_standing", "effort.standing", f"effort:{id}") as op:
            effort = op.need("efforts", [id], "effort")
            if effort["standing"] == "concluded":
                raise Refused(f"effort {id} is concluded")
            if effort["standing"] == standing:
                op.nothing()
            op.put("efforts", {**effort, "standing": standing})
            op.data = {"id": id, "from": effort["standing"], "to": standing, "why": why}
        return op.seq is not None

    def conclude(self, id, outcome, why):
        """Conclude an effort with its outcome. Refused with no outcome, and
        for an effort already concluded."""
        if not outcome:
            raise Refused("an effort concludes with an outcome")
        with self._operation("conclude", "effort.conclude", f"effort:{id}") as op:
            effort = op.need("efforts", [id], "effort")
            if effort["standing"] == "concluded":
                raise Refused(f"effort {id} is concluded already")
            op.put(
                "efforts",
                {
                    **effort,
                    "standing": "concluded",
                    "outcome": outcome,
                    "concluded_at": op.now,
                },
            )
            op.data = {"id": id, "outcome": outcome, "why": why}

    def link(self, effort, kind, target):
        """Link an effort to a plan, an item or another effort. Refused for a
        link already live, or a target that is not there."""
        if not isinstance(target, str) or not target:
            raise Refused(f"a link's target is a non-empty str, not {target!r}")
        with self._operation("link", "effort.link", f"effort:{effort}") as op:
            op.need("efforts", [effort], "effort")
            self._target(op, effort, kind, target)
            found = op.get("effort_links", [effort, kind, target])
            if found is not None and found["unlinked_at"] is None:
                raise Refused(f"effort {effort} is already linked to {kind} {target}")
            op.put(
                "effort_links",
                {
                    "effort": effort,
                    "kind": kind,
                    "target": target,
                    "at": op.now,
                    "by": self.actor,
                    "unlinked_at": None,
                },
            )
            op.data = {"effort": effort, "kind": kind, "target": target}

    @staticmethod
    def _target(op, effort, kind, target):
        if kind == "item":
            if not (target.isascii() and target.isdigit()):
                raise Refused(f"an item link's target is its number, not {target!r}")
            op.need("items", [int(target)], "item")
        elif kind == "effort":
            if target == effort:
                raise Refused("an effort is not linked to itself")
            op.need("efforts", [target], "effort")

    def unlink(self, effort, kind, target):
        """End a live link. Refused when it is not live, or when it is an
        item's last effort link (every item keeps one: invariant 10)."""
        with self._operation("unlink", "effort.unlink", f"effort:{effort}") as op:
            found = op.get("effort_links", [effort, kind, target])
            if found is None or found["unlinked_at"] is not None:
                raise Refused(f"effort {effort} is not linked to {kind} {target}")
            op.put("effort_links", {**found, "unlinked_at": op.now})
            if kind == "item":
                self._keeps_efforts(op, int(target))
            op.data = {"effort": effort, "kind": kind, "target": target}

    @staticmethod
    def _keeps_efforts(op, number):
        """Refused unless the item, and every descendant that has no link of
        its own, still has an effort link."""
        stack, seen = [number], set()
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            item = op.get("items", [n])
            if item is None:
                continue
            if n == number or not _linked(op, n):
                if not _efforts_of(op, item):
                    raise Refused(
                        f"item {n} would have no effort link: link it to another "
                        "effort first"
                    )
                stack.extend(i["number"] for i in op.read("items", parent=n))

    # -- items ----------------------------------------------------------------

    def file_item(
        self,
        kind,
        title,
        body,
        *,
        role=None,
        tier=None,
        risk=None,
        parent=None,
        labels=(),
        flags=(),
        effort=None,
        filed_by,
        machine=False,
        must_fix=False,
        number=None,
    ):
        """File an item, in `triage` without a role and `ready` with one,
        linked to `effort`, else to its parent's efforts, else to `unsorted`
        (made, parked, if it is not there). A machine-filed item is filed
        unadmitted unless it is a must-fix. `number` is taken only by an
        import. Returns the number."""
        if number is not None and self.via != "import":
            raise Refused("an item's number is given only by an import")
        try:
            labels, flags = sorted(labels), sorted(flags)
        except TypeError:
            raise Refused("labels and flags are lists of str") from None
        with self._operation("file_item", "item.file") as op:
            if number is not None:
                number = _key("items", [number])[0]
                if op.get("items", [number]) is not None:
                    raise Refused(f"item {number}: the number is taken")
                op.past("next:items", number)
            else:
                number = op.take("next:items")
            op.subject = f"item:{number}"
            item = {
                "number": number,
                "kind": kind,
                "title": title,
                "state": "ready" if role else "triage",
                "role": role,
                "tier": tier,
                "risk": risk,
                "parent": parent,
                "labels": labels,
                "flags": flags,
                "admitted": not machine or must_fix,
                "filed_by": filed_by,
                "must_fix": must_fix,
                "templet": None,
                "review_round": 0,
                "overruled_at": None,
                "created_at": op.now,
                "updated_at": op.now,
                "closed_at": None,
                "source": "local",
                "source_ref": None,
            }
            op.put("items", item)
            op.put("item_text", {"number": number, "body": body})
            if effort is not None:
                op.need("efforts", [effort], "effort")
                efforts = [effort]
            elif parent is not None:
                efforts = _efforts_of(op, op.need("items", [parent], "parent item"))
            else:
                efforts = [UNSORTED]
            if parent is not None:
                op.need("items", [parent], "parent item")
            if UNSORTED in efforts and op.get("efforts", [UNSORTED]) is None:
                op.put(
                    "efforts",
                    {
                        "id": UNSORTED,
                        "slug": UNSORTED,
                        "title": "Unsorted",
                        "standing": "parked",
                        "owner": self.actor,
                        "created_at": op.now,
                        "statement": "Items filed with no effort",
                        **{part: {} for part in EFFORT_PARTS[1:]},
                    },
                )
            for e in efforts:
                op.put(
                    "effort_links",
                    {
                        "effort": e,
                        "kind": "item",
                        "target": str(number),
                        "at": op.now,
                        "by": self.actor,
                        "unlinked_at": None,
                    },
                )
            op.data = {
                "number": number,
                "kind": kind,
                "state": item["state"],
                "efforts": efforts,
            }
        return number

    def edit_item(self, n, **fields):
        """Edit an item's fields (not its state: `move`; nor `admitted`:
        `admit`). Refused for a cycle of parents. False when nothing changed
        (and nothing is written)."""
        for name in fields:
            if name not in ITEM_EDITS:
                raise Refused(f"an item's {name} is not edited: {ITEM_EDITS}")
        if not fields:
            raise Refused("an edit names a field")
        with self._operation("edit_item", "item.edit", f"item:{n}") as op:
            item = op.need("items", [n], "item")
            text = op.get("item_text", [n])
            after = dict(item)
            for name, value in fields.items():
                if name in ("labels", "flags") and isinstance(value, (list, tuple)):
                    try:
                        value = sorted(value)
                    except TypeError:
                        raise Refused(f"{name} are a list of str") from None
                if name != "body":
                    after[name] = value
            changed = sorted(f for f in fields if f != "body" and after[f] != item[f])
            if "body" in fields and (text or {}).get("body") != fields["body"]:
                changed.append("body")
                op.put("item_text", {"number": n, "body": fields["body"]})
            if not changed:
                op.nothing()
            if "parent" in changed and after["parent"] is not None:
                p = after["parent"]
                seen = set()
                while p is not None and p not in seen:
                    if p == n:
                        raise Refused(f"item {n} under {after['parent']}: a cycle")
                    seen.add(p)
                    p = op.need("items", [p], "parent item")["parent"]
            after["updated_at"] = op.now
            op.put("items", after)
            if "parent" in changed:
                self._keeps_efforts(op, n)
            op.data = {"number": n, "fields": changed}
        return op.seq is not None

    def move(self, n, state, why, *, expect=None):
        """Move an item to `state`. Refused for a pair not in MOVES, and when
        `expect` is not the current state. Done and dropped set `closed_at`;
        a reopen clears it."""
        with self._operation("move", "item.state", f"item:{n}") as op:
            item = op.need("items", [n], "item")
            now = item["state"]
            if expect is not None and now != expect:
                raise Refused(f"item {n} is {now}, not {expect}")
            if (now, state) not in MOVES:
                raise Refused(f"no move from {now} to {state}")
            after = {**item, "state": state, "updated_at": op.now}
            if state in CLOSED_STATES:
                after["closed_at"] = op.now
            elif now in CLOSED_STATES:
                after["closed_at"] = None
            op.put("items", after)
            op.data = {"number": n, "from": now, "to": state, "why": why}

    def admit(self, n, by):
        """Admit a machine-filed item. Refused when it is admitted already."""
        with self._operation("admit", "item.admit", f"item:{n}") as op:
            item = op.need("items", [n], "item")
            if item["admitted"]:
                raise Refused(f"item {n} is admitted already")
            op.put("items", {**item, "admitted": True, "updated_at": op.now})
            op.data = {"number": n, "by": by}

    # -- deps -----------------------------------------------------------------

    def add_dep(self, n, *, blocker=None, change=None, what=None, why):
        """Item `n` waits on exactly one of an item, a change or a `what`.
        Refused for a cycle, or a dep already live. Returns its id."""
        given = [x for x in (blocker, change, what) if x is not None]
        if len(given) != 1:
            raise Refused("a dep waits on exactly one of blocker, change, what")
        with self._operation("add_dep", "dep.add", f"item:{n}") as op:
            op.need("items", [n], "item")
            if blocker is not None:
                op.need("items", [blocker], "blocker item")
                stack, seen = [blocker], set()
                while stack:
                    b = stack.pop()
                    if b == n:
                        raise Refused(f"item {n} on {blocker}: a cycle of deps")
                    if b in seen:
                        continue
                    seen.add(b)
                    stack.extend(
                        d["blocker"]
                        for d in op.read("deps", live=True, number=b)
                        if d["blocker"] is not None
                    )
            if change is not None:
                op.need("changes", [change], "change")
            for dep in op.read("deps", live=True, number=n):
                if (dep["blocker"], dep["change"], dep["what"]) == (
                    blocker,
                    change,
                    what,
                ):
                    raise Refused(f"item {n} has that dep live already ({dep['id']})")
            id = op.take("next:deps")
            op.put(
                "deps",
                {
                    "id": id,
                    "number": n,
                    "blocker": blocker,
                    "change": change,
                    "what": what,
                    "added_at": op.now,
                    "added_by": self.actor,
                    "lifted_at": None,
                    "lifted_by": None,
                    "why": why,
                },
            )
            op.data = {"id": id, "number": n, "blocker": blocker, "change": change}
        return id

    def lift_dep(self, id, by, why):
        """Lift a live dep. Refused when it is lifted already."""
        with self._operation("lift_dep", "dep.lift") as op:
            dep = op.need("deps", [id], "dep")
            op.subject = f"item:{dep['number']}"
            if dep["lifted_at"] is not None:
                raise Refused(f"dep {id} is lifted already")
            op.put("deps", {**dep, "lifted_at": op.now, "lifted_by": by})
            op.data = {"id": id, "number": dep["number"], "why": why}

    # -- text -----------------------------------------------------------------

    def comment(self, number, body, *, change=None, who=None, mark=None, ref=None):
        """A comment on an item, or on a change. Returns its id."""
        if number is None and change is None:
            raise Refused("a comment is on an item or a change")
        subject = f"item:{number}" if number is not None else f"change:{change}"
        with self._operation("comment", "comment", subject) as op:
            if number is not None:
                op.need("items", [number], "item")
            if change is not None:
                op.need("changes", [change], "change")
            id = op.take("next:comments")
            op.put(
                "comments",
                {
                    "id": id,
                    "number": number,
                    "change": change,
                    "who": who or self.actor,
                    "body": body,
                    "at": op.now,
                    "mark": mark,
                    "ref": ref,
                    "source_ref": None,
                },
            )
            op.data = {"id": id, "number": number, "change": change, "mark": mark}
        return id

    def state(
        self, type, text, *, effort=None, number=None, where=None, supersedes=None
    ):
        """A statement (#1228's types), on an effort or an item or neither.
        Returns its id."""
        subject = (
            f"effort:{effort}"
            if effort is not None
            else f"item:{number}" if number is not None else None
        )
        with self._operation("state", "statement", subject) as op:
            if effort is not None:
                op.need("efforts", [effort], "effort")
            if number is not None:
                op.need("items", [number], "item")
            if supersedes is not None:
                op.need("statements", [supersedes], "statement")
            id = op.take("next:statements")
            op.put(
                "statements",
                {
                    "id": id,
                    "type": type,
                    "effort": effort,
                    "number": number,
                    "text": text,
                    "who": self.actor,
                    "at": op.now,
                    "where": where,
                    "supersedes": supersedes,
                },
            )
            op.data = {"id": id, "type": type, "effort": effort, "number": number}
        return id

    # -- holds ----------------------------------------------------------------

    def hold(self, *, number=None, who=None, cause, why):
        """Hold an item, or a holder or role by name. Returns its id."""
        if (number is None) == (who is None):
            raise Refused("a hold is on exactly one of number, who")
        subject = f"item:{number}" if number is not None else f"who:{who}"
        with self._operation("hold", "hold.set", subject) as op:
            if number is not None:
                op.need("items", [number], "item")
            id = op.take("next:holds")
            op.put(
                "holds",
                {
                    "id": id,
                    "number": number,
                    "who": who,
                    "cause": cause,
                    "why": why,
                    "set_at": op.now,
                    "set_by": self.actor,
                    "lifted_at": None,
                    "lifted_by": None,
                },
            )
            op.data = {"id": id, "number": number, "who": who, "cause": cause}
        return id

    def lift_hold(self, id, by):
        """Lift a live hold. Refused when it is lifted already."""
        with self._operation("lift_hold", "hold.lift") as op:
            hold = op.need("holds", [id], "hold")
            if hold["number"] is not None:
                op.subject = f"item:{hold['number']}"
            else:
                op.subject = f"who:{hold['who']}"
            if hold["lifted_at"] is not None:
                raise Refused(f"hold {id} is lifted already")
            op.put("holds", {**hold, "lifted_at": op.now, "lifted_by": by})
            op.data = {"id": id}

    # -- the journal itself ---------------------------------------------------

    def note(self, kind, subject, **data):
        """An event that stands for no record: `note`, with `kind` and the
        data. Returns its seq."""
        with self._operation("note", "note", subject) as op:
            op.empty_ok = True
            op.data = {"kind": kind, **data}
        return op.seq


def _findings(dump):
    """What `check()` finds in a dump, whatever the backend."""
    findings = []
    seqs = [e["seq"] for e in dump["journal"]]
    if seqs != list(range(1, len(seqs) + 1)):
        findings.append("journal: the seqs do not run from 1 by one")
    view = ev.MemoryView()
    try:
        for raw in dump["journal"]:
            ev.apply(view, ev.Event.from_dict(raw))
    except SchemaError as e:
        findings.append(f"journal: an event the declarations refuse: {e}")
    replayed = view.records()
    for kind, rows in dump["records"].items():
        if replayed.get(kind) == rows:
            continue
        want = {tuple(schema.key_of(kind, r)): r for r in replayed.get(kind, [])}
        have = {tuple(schema.key_of(kind, r)): r for r in rows}
        bad = sorted(k for k in want.keys() | have.keys() if want.get(k) != have.get(k))
        shown = ", ".join(str(list(k)) for k in bad[:10])
        findings.append(f"{kind}: not the replay of the journal at {shown}")
    for n in unlinked_items(dump["records"]):
        findings.append(f"item {n}: no effort link")
    meta = {r["key"]: r["value"] for r in dump["records"].get("meta", [])}
    for kind in schema.ID_KINDS:
        name = RECORDS[kind].key_names[0]
        ids = [r[name] for r in dump["records"].get(kind, [])]
        if kind == "efforts":
            ids = [int(i) for i in ids if i.isascii() and i.isdigit()]
        if ids and meta.get(schema.counter_name(kind), 1) <= max(ids):
            findings.append(
                f"meta: {schema.counter_name(kind)} is not past {max(ids)}, the "
                f"highest {kind} id"
            )
    return findings


def open_record(
    url,
    *,
    readonly=False,
    shared=False,
    allow_undurable=False,
    actor,
    via,
    authorize=None,
    **backend,
):
    """Open the record at `url` (`sqlite:///path/record.sqlite`). `shared`
    opens the record a yard runs on, which must be durable unless
    `allow_undurable` (tests and the bench). `backend` passes settings the
    backend takes (tests only: SQLite's `synchronous`, `fullfsync`)."""
    if not isinstance(url, str) or ":" not in url:
        raise Refused(f"a record's URL is <backend>:<where>, not {url!r}")
    name = url.split(":", 1)[0]
    cls = BACKENDS.get(name)
    if cls is None:
        raise Refused(f"no backend {name!r}: one of {', '.join(sorted(BACKENDS))}")
    record = cls.open(
        url,
        readonly=readonly,
        shared=shared,
        actor=actor,
        via=via,
        authorize=authorize,
        **backend,
    )
    if shared and not record.durable and not allow_undurable:
        record.close()
        raise Refused(
            "a shared record is durable, and this one is not (allow_undurable "
            "is for tests and the bench)"
        )
    return record


#: The backends, by the scheme of their URL. Each registers itself.
BACKENDS = {}

from . import sqlite  # noqa: E402,F401  (registers the SQLite backend)
