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


#: Every state pair an item may move by, without who may move it or when:
#: that is the caller's `authorize`.
MOVES = frozenset().union(
    _pairs(["triage"], ["ready"]),
    _pairs(["ready", "blocked"], ["claimed"]),
    _pairs(["claimed"], ["blocked", "in-review", "ready"]),
    _pairs(["ready", "in-review"], ["blocked"]),
    _pairs(["blocked"], ["ready"]),
    _pairs(["in-review"], ["reviewed"]),
    _pairs(["reviewed"], ["merge-ready"]),
    _pairs(["merge-ready"], ["reviewed"]),
    _pairs(["in-review", "reviewed", "merge-ready"], ["ready", "done"]),
    _pairs(["ready", "claimed", "blocked"], ["done", "dropped"]),
    _pairs(OPEN_STATES, CLOSED_STATES),
    _pairs(CLOSED_STATES, ["ready", "triage"]),
)

#: The effort an item filed with no effort and no parent is linked to
#: (`schema.unsorted_effort` gives it).
UNSORTED = schema.UNSORTED
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
#: How long a lease runs past its last renewal while its item is claimed:
#: three days. Expiry is a report, never a release.
LEASE_MS = 3 * 24 * 3600 * 1000
TASK_EDITS = (
    "labels",
    "flags",
    "needs",
    "sandbox",
    "schedule",
    "not_before",
    "supervision",
    "loop",
)
#: The states a task ends in.
TASK_ENDS = ("done", "failed", "cancelled")
#: A task's loop and supervision when it is filed without them.
LOOP = {"max_attempts": 1, "interval_s": 0}
SUPERVISION = {"expect_s": None, "beat_s": None}


class _Keep:
    """A field not given: an update keeps its value, an open takes its
    default."""

    def __repr__(self):
        return "KEEP"


KEEP = _Keep()
#: A change's fields an open or an update may give, and their defaults.
CHANGE_FIELDS = {
    "title": None,
    "body": "",
    "number": None,
    "relation": "part",
    "base": None,
    "position": None,
    "draft": False,
    "url": None,
}


def wall_ms():
    return time.time_ns() // 1_000_000


def _who(actor, via):
    """Refused unless `actor` and `via` can name an operation."""
    if not isinstance(actor, str) or not actor:
        raise Refused("an actor is a non-empty str")
    if via not in ev.VIAS:
        raise Refused(f"no via {via!r}: one of {', '.join(ev.VIAS)}")


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


def efforts_of(reader, item):
    """The efforts an item is linked to: its own, else its parent's chain's.
    `reader` is anything with the contract's `read` and `get`."""
    seen = set()
    while item is not None and item["number"] not in seen:
        seen.add(item["number"])
        own = _linked(reader, item["number"])
        if own:
            return own
        parent = item["parent"]
        item = reader.get("items", [parent]) if parent is not None else None
    return []


def _ineligible(reader, item):
    """Why an item is not eligible, or None: it is admitted, an effort it is
    linked to (its own links, else its parent chain's) is admitted, it has no
    live dep and no live hold, and neither its holder nor its role has one."""
    n = item["number"]
    if not item["admitted"]:
        return f"item {n} is not admitted"
    if not any(
        (reader.get("efforts", e) or {}).get("standing") == "admitted"
        for e in efforts_of(reader, item)
    ):
        return f"no effort item {n} is linked to is admitted"
    if reader.read("deps", live=True, number=n):
        return f"item {n} has a live dep"
    if reader.read("holds", live=True, number=n):
        return f"item {n} has a live hold"
    whos = {lease["holder"] for lease in reader.read("leases", live=True, number=n)}
    if item["role"] is not None:
        whos.add(item["role"])
    for who in sorted(whos):
        if reader.read("holds", live=True, who=who):
            return f"{who} has a live hold"
    return None


def _unready(reader, task, now):
    """Why an open task is not ready at `now`, or None. Its item, when it has
    one, is ready or claimed and eligible; with no item, its own effort is
    admitted. Its `not_before` has passed and its attempts are under
    `loop.max_attempts`."""
    id = task["id"]
    if task["state"] != "open":
        return f"task {id} is {task['state']}, not open"
    if task["not_before"] is not None and task["not_before"] > now:
        return f"task {id} is not before {task['not_before']}"
    most = task["loop"].get("max_attempts", LOOP["max_attempts"])
    if task["attempts"] >= most:
        return f"task {id} has had {task['attempts']} of its {most} attempts"
    if task["number"] is not None:
        item = reader.get("items", task["number"])
        if item is None:
            return f"task {id}'s item {task['number']} is not there"
        if item["state"] not in ("ready", "claimed"):
            return f"task {id}'s item {task['number']} is {item['state']}"
        return _ineligible(reader, item)
    effort = task["effort"]
    if (
        effort is None
        or (reader.get("efforts", effort) or {}).get("standing") != "admitted"
    ):
        return f"task {id} has no item and no admitted effort"
    return None


def _peak(reservations, from_at, until_at):
    """The largest sum of `reservations` covering any instant of the span
    `[from_at, until_at)` (None: no end). A reservation covers `[its from_at,
    its until_at)`, so the sum can only rise where one starts."""
    over = [
        r
        for r in reservations
        if (until_at is None or r["from_at"] < until_at)
        and (r["until_at"] is None or r["until_at"] > from_at)
    ]
    points = {from_at} | {r["from_at"] for r in over if r["from_at"] > from_at}
    return max(
        sum(
            r["amount"]
            for r in over
            if r["from_at"] <= t and (r["until_at"] is None or t < r["until_at"])
        )
        for t in points
    )


def _free(reader, node, resource, from_at, until_at):
    """A node's capacity of `resource` (0 when none is declared) minus the
    largest sum of its live reservations over the span."""
    cap = reader.get("capacity", [node, resource])
    live = [
        r
        for r in reader.read("reservations", live=True, node=node)
        if r["resource"] == resource
    ]
    return (cap["amount"] if cap else 0) - _peak(live, from_at, until_at)


def _expiry(now, paused_until):
    """A running lease's expiry, renewed at `now`."""
    return max(now, paused_until or 0) + LEASE_MS


def _time(value, what):
    if value is not None and (not schema.is_int(value) or value < 0):
        raise Refused(f"{what} is a time in milliseconds, not {value!r}")
    return value


def _seconds(value, what):
    """`value` seconds as milliseconds (None stays None)."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise Refused(f"{what} is seconds, from 0, not {value!r}")
    return int(value * 1000)


def _needs(needs):
    """`needs` as `{resource: amount}`, an amount an int from 1, by resource."""
    if not isinstance(needs, dict):
        raise Refused(f"needs are {{resource: amount}}, not {needs!r}")
    for resource, amount in needs.items():
        if not isinstance(resource, str) or not resource:
            raise Refused(f"a resource is a non-empty str, not {resource!r}")
        if not schema.is_int(amount) or amount < 1:
            raise Refused(
                f"needs {resource}: an amount is an int from 1, not {amount!r}"
            )
    return dict(sorted(needs.items()))


def _settings(value, defaults, what, least):
    """A task's `loop` or `supervision`: the defaults' keys only, each an int
    from `least[key]` or, where the default is None, None."""
    if value is None:
        return dict(defaults)
    if not isinstance(value, dict) or not set(value) <= set(defaults):
        raise Refused(f"a task's {what} is {{{', '.join(defaults)}}}, not {value!r}")
    out = {**defaults, **value}
    for key, got in out.items():
        if got is None and defaults[key] is None:
            continue
        if not schema.is_int(got) or got < least[key]:
            raise Refused(f"a task's {what}.{key} is an int from {least[key]}: {got!r}")
    return out


def _loop(loop):
    return _settings(loop, LOOP, "loop", {"max_attempts": 1, "interval_s": 0})


def _supervision(supervision):
    return _settings(
        supervision, SUPERVISION, "supervision", {"expect_s": 0, "beat_s": 0}
    )


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


def _priors(get, event):
    """An event's priors from what `get(kind, key)` holds before it: for each
    put, the values its `match` fields held (null for a new record)."""
    out = []
    for kind, key, _ in event.puts:
        before = get(kind, list(key))
        held = None
        if before is not None:
            held = {f: before[f] for f in RECORDS[kind].match}
        out.append([kind, list(key), held])
    return out


class Record(abc.ABC):
    """The record behind one contract. A backend implements the store's
    primitives (the abstract methods); the operations, the reads and the
    journal's delivery are written here once."""

    #: The backend's name in `BACKENDS`.
    backend = None

    def __init__(self, *, actor, via, authorize=None, readonly=False, shared=False):
        _who(actor, via)
        self.actor = actor
        self.via = via
        self.authorize = authorize
        self.readonly = readonly
        self._shared = shared
        #: The record's clock: milliseconds since the epoch. Tests move it.
        self.clock = wall_ms
        self._lock = threading.RLock()
        self._reading_depth = 0
        self._acting_depth = 0
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
        """Was the record opened as the record a deployment runs on, which
        must be durable?"""
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

    def _not_reading(self):
        """Under the lock: another thread's reading() holds the lock, so a
        depth seen here is this thread's own, and a write inside it is
        refused (another thread's write waits for the reading to end)."""
        if self._reading_depth:
            raise Refused("no operation runs inside reading()")

    def _wake(self):
        with self._woken:
            self._commits += 1
            self._woken.notify_all()

    @contextlib.contextmanager
    def acting(self, actor, via):
        """Operations in this span are `actor`'s, through `via`, in place of
        those fixed at open: `authorize` sees them, the event names them, and
        so does whatever an operation stamps with its actor. The span holds
        the record's lock, so another thread's operation waits for it to end
        and is written under its own actor: keep a span to the caller's one
        operation. On one thread, a span must not be left suspended (no
        `await` in it). A span inside a span gives the outer one's back."""
        _who(actor, via)
        with self._lock:
            held = self.actor, self.via
            self.actor, self.via = actor, via
            self._acting_depth += 1
            try:
                yield
            finally:
                self._acting_depth -= 1
                self.actor, self.via = held

    @contextlib.contextmanager
    def _operation(self, name, kind, subject=None):
        """One operation: `with self._operation(...) as op`, then the op's
        reads and puts. At the end the event is decided, authorized, measured
        and stored, in the backend's one write step."""
        self._writable()
        op = None
        with self._lock:
            self._not_reading()
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
        try:
            event = op.event(self._head() + 1, self.actor, self.via)
            ev.check_event(event)
        except SchemaError as e:
            raise Refused(str(e)) from None
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
        """Every read inside sees the record at one seq, which it yields. It
        holds the record's lock: other threads' operations and reads wait
        until the reading ends, so a reading must not wait on one (a join, a
        queue) or it never ends."""
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
        open tasks that are ready, each in key order. An item is eligible
        when it is admitted, an effort it is linked to (its own links, else
        its parent chain's) is admitted, it has no live dep and no live hold,
        and neither its holder nor its role has one. A task is ready when its
        item, if it has one, is ready or claimed and eligible (with no item,
        its own effort is admitted), its `not_before` has passed and its
        attempts are under `loop.max_attempts`."""
        if not schema.is_int(now):
            raise Refused(f"now is a time in milliseconds, not {now!r}")
        with self.reading():
            items = [
                i for i in self.read("items", state="ready") if not _ineligible(self, i)
            ]
            tasks = [
                t
                for t in self.read("tasks", state="open")
                if not _unready(self, t, now)
            ]
        return {"items": items, "tasks": tasks}

    def lease_view(self, number, *, now):
        """The item's live lease, or None, with its `state` (running, paused
        or expired) and its `expiry`: from `renewed_at`, `paused_until` and
        the latest beat of the holder's live run on the item under the
        lease's fence, plus `LEASE_MS`. A lease is paused (no expiry) while
        its item is not claimed. Expiry is a report: an expired lease still
        holds the item."""
        _key("items", [number])
        if not schema.is_int(now):
            raise Refused(f"now is a time in milliseconds, not {now!r}")
        with self.reading():
            live = self.read("leases", live=True, number=number)
            if not live:
                return None
            lease = live[0]
            if lease["expires_at"] is None:
                return {**lease, "state": "paused", "expiry": None}
            since = max(lease["renewed_at"], lease["paused_until"] or 0)
            for run in self.read("runs", live=True, holder=lease["holder"]):
                task = self.get("tasks", run["task"])
                beat = self.get("beats", run["id"])
                if (
                    run["fence"] == lease["fence"]
                    and task
                    and task["number"] == number
                    and beat
                ):
                    since = max(since, beat["at"])
        expiry = since + LEASE_MS
        state = "running" if now < expiry else "expired"
        return {**lease, "state": state, "expiry": expiry}

    def free(self, node, resource, *, from_at, until_at):
        """A node's capacity of `resource` minus the largest sum of its live
        reservations covering any instant of `[from_at, until_at)` (None: no
        end)."""
        _key("capacity", [node, resource])
        _time(from_at, "from_at")
        _time(until_at, "until_at")
        if from_at is None:
            raise Refused("from_at is a time in milliseconds, not None")
        if until_at is not None and until_at <= from_at:
            raise Refused(f"a span ends after it starts: {from_at}, {until_at}")
        with self.reading():
            return _free(self, node, resource, from_at, until_at)

    def effective_labels(self, task_id):
        """A task's effective labels: its item's, then its own, merged by key
        (the task's winning: its labels of a key replace all of the item's of
        that key, one or several), sorted as labels are stored. Never
        stored."""
        with self.reading():
            task = self.get("tasks", task_id)
            if task is None:
                raise Refused(f"no task {task_id}")
            number = task["number"]
            item = self.get("items", number) if number is not None else None
        own = {label.split(":", 1)[0] for label in task["labels"]}
        kept = [
            label
            for label in (item["labels"] if item else [])
            if label.split(":", 1)[0] not in own
        ]
        return sorted(kept + task["labels"])

    # -- the journal, delivered -----------------------------------------------

    def head(self):
        """The highest seq, 0 when the journal is empty."""
        with self._lock:
            self._usable()
            return self._head()

    def events(self, cursor, *, limit=500, wait=0.0, subject=None):
        """The events after `cursor`, in seq order, at most `limit`. With
        `wait`, up to that many seconds for the first one. A wait inside
        `reading()` is refused: the reading holds every other thread's
        operation back and its snapshot pins the read, so it could only end
        empty. A wait inside `acting()` is refused too: the span holds every
        other thread's operation back."""
        if not schema.is_int(cursor) or cursor < 0:
            raise Refused(f"a cursor is a seq, from 0, not {cursor!r}")
        if not schema.is_int(limit) or limit < 1:
            raise Refused(f"a limit is an int, from 1, not {limit!r}")
        if isinstance(wait, bool) or not isinstance(wait, (int, float)) or wait < 0:
            raise Refused(f"wait is seconds, from 0, not {wait!r}")
        if wait > 0:
            with self._lock:
                # another thread's reading holds the lock, so a depth seen
                # here is this thread's own
                if self._reading_depth:
                    raise Refused("events(wait=) does not wait inside reading()")
                if self._acting_depth:
                    raise Refused("events(wait=) does not wait inside acting()")
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
            self._not_reading()
            with self._write():
                if seq > self._head():
                    raise Refused(f"seq {seq} is past the journal's head")
                self._bookkeep(
                    "cursors", {"consumer": consumer, "seq": seq, "at": self.now_ms()}
                )

    def beat(self, run):
        """A run's liveness beat, at the record's clock (an earlier one leaves
        the later). Bookkeeping: no event, and never refused by a rule."""
        _key("beats", [run])
        self._writable()
        with self._lock:
            self._not_reading()
            with self._write():
                self._bookkeep("beats", {"run": run, "at": self.now_ms()})

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
        records, the seqs, each entry against its limit, the items with no
        effort link, the counters behind their ids, a node booked past its
        capacity (invariant 6), a running run without its reservations or a
        finished one holding some (invariant 7), a run's pgid not set once,
        then the backend's own.
        Empty when the record is whole."""
        with self.reading():
            dump = self.dump()
            findings = _findings(dump)
            findings += self._check_store()
        return findings

    def load(self, dump):
        """Write a dump into an empty record: every event with its seq,
        applied in order, then the bookkeeping, in one write step. Refused,
        before anything is written, when the dump is not whole (`check_dump`),
        and when the record is not empty."""
        self._writable()
        journal = check_dump(dump)
        kept = {k for k, v in RECORDS.items() if not v.journaled}
        with self._lock:
            self._not_reading()
            with self._write():
                if not self._empty():
                    raise Refused("a load goes into an empty record")
                for event in journal:
                    priors = _priors(self._get, event)
                    self._check_entry(event, priors)
                    self._store(event, priors)
                for kind in sorted(kept):
                    for row in dump["bookkeeping"].get(kind, []):
                        self._bookkeep(kind, schema.validate(kind, row))
                # the store's fold against the dump's, a backstop to
                # check_dump's in memory
                for kind, v in RECORDS.items():
                    if v.journaled and self._read(kind, {}, None, False, None) != dump[
                        "records"
                    ].get(kind, []):
                        raise Refused(
                            f"a dump's {kind} are not the fold of its journal"
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
            if not (target.isascii() and target.isdigit()) or (
                str(int(target)) != target
            ):
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
                if not efforts_of(op, item):
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
        try:
            labels, flags = sorted(labels), sorted(flags)
        except TypeError:
            raise Refused("labels and flags are lists of str") from None
        with self._operation("file_item", "item.file") as op:
            # read under the lock: a span's via is this thread's own here
            if number is not None and self.via != "import":
                raise Refused("an item's number is given only by an import")
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
                efforts = efforts_of(op, op.need("items", [parent], "parent item"))
                if not efforts:
                    raise Refused(
                        f"parent item {parent} has no effort link: file the item "
                        "with an effort"
                    )
            else:
                efforts = [UNSORTED]
            if parent is not None:
                op.need("items", [parent], "parent item")
            if UNSORTED in efforts and op.get("efforts", [UNSORTED]) is None:
                op.put("efforts", schema.unsorted_effort(op.now))
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
            try:
                after = schema.validate("items", after)
            except SchemaError as e:
                raise Refused(str(e)) from None
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

    def move(self, n, state, why, *, expect=None, fence=None):
        """Move an item to `state`. Refused for a pair not in MOVES, when
        `expect` is not the current state, and for a `fence` (a run's) that
        is not the item's live lease. Done and dropped set `closed_at` and
        release the live lease; a reopen clears `closed_at`. A move out of
        `claimed` pauses the lease, and a move back restarts it."""
        with self._operation("move", "item.state", f"item:{n}") as op:
            item = op.need("items", [n], "item")
            now = item["state"]
            if fence is not None:
                self._live_lease(op, n, fence)
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
            for lease in op.read("leases", live=True, number=n):
                if state in CLOSED_STATES:
                    lease.update(
                        released_at=op.now, released_by=self.actor, why="closed"
                    )
                elif state == "claimed":
                    lease.update(
                        renewed_at=op.now,
                        expires_at=_expiry(op.now, lease["paused_until"]),
                    )
                elif now == "claimed":
                    lease.update(expires_at=None)
                else:
                    continue
                op.put("leases", lease)
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

    # -- leases ---------------------------------------------------------------

    @staticmethod
    def _live_lease(op, n, fence=None):
        """Item `n`'s live lease. Refused when it has none, and when `fence`
        is given and is not the live lease's: a stale fence."""
        live = op.read("leases", live=True, number=n)
        if not live:
            stale = f": fence {fence} is stale" if fence is not None else ""
            raise Refused(f"item {n} has no live lease{stale}")
        lease = live[0]
        if fence is not None and lease["fence"] != fence:
            raise Refused(
                f"fence {fence} is stale: item {n}'s live lease has fence "
                f"{lease['fence']}"
            )
        return lease

    @staticmethod
    def _not_held(op, n, *whos):
        if op.read("holds", live=True, number=n):
            raise Refused(f"item {n} has a live hold")
        for who in whos:
            if who is not None and op.read("holds", live=True, who=who):
                raise Refused(f"{who} has a live hold")

    def grant(self, n, holder, *, role=None, branch, machine, expect=None):
        """Grant item `n` to `holder`, moving it to `claimed` (from a state
        MOVES takes there). The holder's own second grant renews its live
        lease and keeps its fence. Refused for a live lease held by another,
        a live hold (on the item, the holder or the role), and `expect` not
        the state. Returns the fence, from 1 per item."""
        with self._operation("grant", "lease.grant", f"item:{n}") as op:
            item = op.need("items", [n], "item")
            state = item["state"]
            if expect is not None and state != expect:
                raise Refused(f"item {n} is {state}, not {expect}")
            live = op.read("leases", live=True, number=n)
            if live and live[0]["holder"] != holder:
                lease = live[0]
                raise Refused(
                    f"item {n} is held by {lease['holder']} (fence {lease['fence']})"
                )
            self._not_held(op, n, holder, role if role is not None else item["role"])
            if state != "claimed" and (state, "claimed") not in MOVES:
                raise Refused(f"no move from {state} to claimed: item {n}")
            expires_at = _expiry(op.now, live[0]["paused_until"] if live else None)
            if live:
                lease = {**live[0], "renewed_at": op.now, "expires_at": expires_at}
            else:
                fence = max(
                    (lease["fence"] for lease in op.read("leases", number=n)),
                    default=0,
                )
                lease = {
                    "id": op.take("next:leases"),
                    "number": n,
                    "holder": holder,
                    "role": role,
                    "fence": fence + 1,
                    "branch": branch,
                    "machine": machine,
                    "granted_at": op.now,
                    "renewed_at": op.now,
                    "paused_until": None,
                    "expires_at": expires_at,
                    "released_at": None,
                    "released_by": None,
                    "why": None,
                }
            op.put("leases", lease)
            if state != "claimed":
                op.put("items", {**item, "state": "claimed", "updated_at": op.now})
            op.data = {
                "number": n,
                "holder": holder,
                "fence": lease["fence"],
                "from": state,
            }
        return lease["fence"]

    def renew(self, n, holder, fence, *, paused_until=None):
        """Renew the holder's live lease on item `n`; `paused_until` (a usage
        limit's reset) holds its expiry back to then. Refused for a stale
        fence, or a lease another holds."""
        _time(paused_until, "paused_until")
        with self._operation("renew", "lease.renew", f"item:{n}") as op:
            lease = self._live_lease(op, n, fence)
            if lease["holder"] != holder:
                raise Refused(f"item {n}'s lease is held by {lease['holder']}")
            item = op.need("items", [n], "item")
            if paused_until is not None:
                lease["paused_until"] = paused_until
            lease["renewed_at"] = op.now
            if item["state"] == "claimed":
                lease["expires_at"] = _expiry(op.now, lease["paused_until"])
            op.put("leases", lease)
            op.data = {"number": n, "holder": holder, "fence": fence}

    def release(self, n, by, why, *, fence=None):
        """Release item `n`'s live lease, and move the item to `ready` when it
        is `claimed` or `blocked`. Refused when it has none, or for a stale
        `fence`."""
        with self._operation("release", "lease.release", f"item:{n}") as op:
            lease = self._live_lease(op, n, fence)
            op.put(
                "leases",
                {**lease, "released_at": op.now, "released_by": by, "why": why},
            )
            item = op.need("items", [n], "item")
            if item["state"] in ("claimed", "blocked"):
                op.put("items", {**item, "state": "ready", "updated_at": op.now})
            op.data = {"number": n, "holder": lease["holder"], "by": by, "why": why}

    # -- tasks ----------------------------------------------------------------

    def file_task(
        self,
        kind,
        *,
        number=None,
        effort=None,
        labels=(),
        flags=(),
        needs=None,
        sandbox,
        schedule=None,
        not_before=None,
        supervision=None,
        loop=None,
    ):
        """File a task: of an item, or with none (a job), under `effort`.
        `needs` is `{resource: amount}`, reserved by each run. Returns its
        id."""
        try:
            labels, flags = sorted(labels), sorted(flags)
        except TypeError:
            raise Refused("labels and flags are lists of str") from None
        needs = _needs({} if needs is None else needs)
        loop, supervision = _loop(loop), _supervision(supervision)
        _time(not_before, "not_before")
        with self._operation("file_task", "task.file") as op:
            if number is not None:
                op.need("items", [number], "item")
            if effort is not None:
                op.need("efforts", [effort], "effort")
            id = op.take("next:tasks")
            op.subject = f"task:{id}"
            op.put(
                "tasks",
                {
                    "id": id,
                    "number": number,
                    "effort": effort,
                    "kind": kind,
                    "labels": labels,
                    "flags": flags,
                    "needs": needs,
                    "sandbox": sandbox,
                    "schedule": schedule,
                    "not_before": not_before,
                    "supervision": supervision,
                    "loop": loop,
                    "attempts": 0,
                    "state": "open",
                    "created_at": op.now,
                    "ended_at": None,
                },
            )
            op.data = {"id": id, "number": number, "effort": effort, "kind": kind}
        return id

    def edit_task(self, id, **fields):
        """Edit a task's labels, flags, needs, sandbox, schedule, not_before,
        supervision or loop. Refused for an ended task, and for its needs
        while a run holds reservations for them. False when nothing changed
        (and nothing is written)."""
        for name in fields:
            if name not in TASK_EDITS:
                raise Refused(f"a task's {name} is not edited: {TASK_EDITS}")
        if not fields:
            raise Refused("an edit names a field")
        checks = {"needs": _needs, "loop": _loop, "supervision": _supervision}
        for name, check in checks.items():
            if name in fields:
                fields[name] = check(fields[name])
        if "not_before" in fields:
            _time(fields["not_before"], "not_before")
        for name in ("labels", "flags"):
            if name in fields and isinstance(fields[name], (list, tuple)):
                try:
                    fields[name] = sorted(fields[name])
                except TypeError:
                    raise Refused(f"{name} are a list of str") from None
        with self._operation("edit_task", "task.edit", f"task:{id}") as op:
            task = op.need("tasks", [id], "task")
            if task["ended_at"] is not None:
                raise Refused(f"task {id} has ended ({task['state']})")
            after = {**task, **fields}
            if after == task:
                op.nothing()
            if after["needs"] != task["needs"] and op.read("runs", live=True, task=id):
                raise Refused(f"task {id} has a live run, which holds its needs")
            op.put("tasks", after)
            op.data = {
                "id": id,
                "fields": sorted(f for f in fields if after[f] != task[f]),
            }
        return op.seq is not None

    def end_task(self, id, state, why):
        """End a task: done, failed or cancelled, releasing its own live
        reservations (the task's, with no run) in the same event. Refused for
        a task that has ended, and for one with a live run (finish it
        first)."""
        if state not in TASK_ENDS:
            raise Refused(f"a task ends {', '.join(TASK_ENDS)}, not {state!r}")
        with self._operation("end_task", "task.end", f"task:{id}") as op:
            task = op.need("tasks", [id], "task")
            if task["ended_at"] is not None:
                raise Refused(f"task {id} has ended ({task['state']})")
            if op.read("runs", live=True, task=id):
                raise Refused(f"task {id} has a live run: finish it first")
            op.put("tasks", {**task, "state": state, "ended_at": op.now})
            released = []
            for r in op.read("reservations", live=True, task=id):
                if r["run"] is None:
                    op.put(
                        "reservations",
                        {**r, "released_at": op.now, "why": "task ended"},
                    )
                    released.append(r["id"])
            op.data = {"id": id, "state": state, "why": why, "released": released}

    # -- allocation -----------------------------------------------------------

    @staticmethod
    def _live_node(op, name):
        node = op.need("nodes", [name], "node")
        if node["retired_at"] is not None:
            raise Refused(f"node {name} is retired")
        return node

    def declare_node(self, name, drivers, labels):
        """Declare a node, or a live node's drivers and labels anew. Refused
        for a retired node. False when nothing changed (and nothing is
        written)."""
        try:
            drivers, labels = sorted(drivers), sorted(labels)
        except TypeError:
            raise Refused("drivers and labels are lists of str") from None
        with self._operation("declare_node", "node.declare", f"node:{name}") as op:
            node = op.get("nodes", [name])
            if node is not None and node["retired_at"] is not None:
                raise Refused(f"node {name} is retired")
            after = {
                "name": name,
                "drivers": drivers,
                "labels": labels,
                "declared_at": node["declared_at"] if node else op.now,
                "retired_at": None,
            }
            if after == node:
                op.nothing()
            op.put("nodes", after)
            op.data = {"name": name, "drivers": drivers, "labels": labels}
        return op.seq is not None

    def retire_node(self, name):
        """Retire a node. Refused for one retired, or with live reservations."""
        with self._operation("retire_node", "node.retire", f"node:{name}") as op:
            node = self._live_node(op, name)
            if op.read("reservations", live=True, node=name):
                raise Refused(f"node {name} has live reservations")
            op.put("nodes", {**node, "retired_at": op.now})
            op.data = {"name": name}

    def set_capacity(self, node, resource, amount, unit):
        """Set a node's capacity of `resource`. Refused below what its live
        reservations hold at any instant from now. False when nothing changed
        (and nothing is written)."""
        if not schema.is_int(amount) or amount < 0:
            raise Refused(f"a capacity is an int from 0, not {amount!r}")
        subject = f"node:{node}"
        with self._operation("set_capacity", "capacity.set", subject) as op:
            self._live_node(op, node)
            reserved = _peak(
                [
                    r
                    for r in op.read("reservations", live=True, node=node)
                    if r["resource"] == resource
                ],
                op.now,
                None,
            )
            if amount < reserved:
                raise Refused(
                    f"node {node} has {reserved} {resource} reserved from now: "
                    f"{amount} is below it"
                )
            before = op.get("capacity", [node, resource])
            if before and (before["amount"], before["unit"]) == (amount, unit):
                op.nothing()
            op.put(
                "capacity",
                {
                    "node": node,
                    "resource": resource,
                    "amount": amount,
                    "unit": unit,
                    "declared_by": self.actor,
                    "at": op.now,
                },
            )
            op.data = {"node": node, "resource": resource, "amount": amount}
        return op.seq is not None

    def _reserve(self, op, node, needs, *, task, run, holder, from_at, until_at):
        """Reserve each of `needs` on a live node over the span, or refuse
        them all: the reservation invariant. Returns the ids, by resource."""
        self._live_node(op, node)
        short = []
        for resource, amount in needs.items():
            free = _free(op, node, resource, from_at, until_at)
            if free < amount:
                short.append(f"{resource} {amount} asked, {max(free, 0)} free")
        if short:
            raise Refused(f"no room on node {node}: {'; '.join(short)}")
        ids = []
        for resource, amount in needs.items():
            ids.append(op.take("next:reservations"))
            op.put(
                "reservations",
                {
                    "id": ids[-1],
                    "node": node,
                    "resource": resource,
                    "amount": amount,
                    "task": task,
                    "run": run,
                    "holder": holder,
                    "from_at": from_at,
                    "until_at": until_at,
                    "released_at": None,
                    "why": None,
                },
            )
        return ids

    def reserve(self, node, needs, *, task=None, run=None, holder, from_at, until_at):
        """Reserve `needs` (`{resource: amount}`) on a node over `[from_at,
        until_at)` (None: no end), for a task, a run, or a standing consumer
        named `holder`. Refused whole when any resource has no room at any
        instant of the span, and for a task that has ended or a run that is
        finished. Returns the ids, by resource."""
        needs = _needs(needs)
        if not needs:
            raise Refused("a reservation names a resource")
        _time(from_at, "from_at")
        _time(until_at, "until_at")
        if from_at is None:
            raise Refused("from_at is a time in milliseconds, not None")
        if until_at is not None and until_at <= from_at:
            raise Refused(f"a reservation ends after it starts: {from_at}, {until_at}")
        with self._operation("reserve", "reserve", f"node:{node}") as op:
            if task is not None:
                t = op.need("tasks", [task], "task")
                if t["ended_at"] is not None:
                    raise Refused(f"task {task} has ended ({t['state']})")
            if run is not None:
                if op.need("runs", [run], "run")["finished_at"] is not None:
                    raise Refused(f"run {run} is finished")
            ids = self._reserve(
                op,
                node,
                needs,
                task=task,
                run=run,
                holder=holder,
                from_at=from_at,
                until_at=until_at,
            )
            op.data = {"ids": ids, "node": node, "needs": needs, "holder": holder}
        return ids

    def release_reservation(self, id, why):
        """Release a live reservation. Refused when it is released already,
        and when its run is live: `finish_run` releases it, so a running run
        keeps the room it holds (invariant 7)."""
        with self._operation("release_reservation", "reserve.release") as op:
            r = op.need("reservations", [id], "reservation")
            op.subject = f"node:{r['node']}"
            if r["released_at"] is not None:
                raise Refused(f"reservation {id} is released already")
            if r["run"] is not None:
                run = op.get("runs", [r["run"]])
                if run is not None and run["finished_at"] is None:
                    raise Refused(
                        f"reservation {id} is held by live run {r['run']}: "
                        "finish_run releases it"
                    )
            op.put("reservations", {**r, "released_at": op.now, "why": why})
            op.data = {"id": id, "node": r["node"], "why": why}

    def replace_reservation(self, id, amount, why):
        """Replace a live reservation by one of `amount`, in one event: it
        releases the old and reserves `amount` of the same node, resource,
        holder and task from now with no end, with no run. The room is
        checked without the old one, at every instant from now, and refused
        whole when `amount` does not fit. Refused for a reservation released
        already and for one a live run holds (`finish_run` releases it). The
        same amount, open ended, writes nothing. Returns the new id (the
        old, when nothing was written)."""
        if not schema.is_int(amount) or amount < 1:
            raise Refused(f"a reservation's amount is an int from 1, not {amount!r}")
        replaced = id
        with self._operation("replace_reservation", "reserve.replace") as op:
            r = op.need("reservations", [id], "reservation")
            op.subject = f"node:{r['node']}"
            if r["released_at"] is not None:
                raise Refused(f"reservation {id} is released already")
            if r["run"] is not None:
                run = op.get("runs", [r["run"]])
                if run is not None and run["finished_at"] is None:
                    raise Refused(
                        f"reservation {id} is held by live run {r['run']}: "
                        "finish_run releases it"
                    )
            self._live_node(op, r["node"])
            if r["amount"] == amount and r["until_at"] is None:
                op.nothing()
            cap = op.get("capacity", [r["node"], r["resource"]])
            others = [
                o
                for o in op.read("reservations", live=True, node=r["node"])
                if o["resource"] == r["resource"] and o["id"] != id
            ]
            free = (cap["amount"] if cap else 0) - _peak(others, op.now, None)
            if free < amount:
                raise Refused(
                    f"no room on node {r['node']}: {r['resource']} {amount} asked, "
                    f"{max(free, 0)} free without reservation {id}"
                )
            op.put("reservations", {**r, "released_at": op.now, "why": why})
            replaced = op.take("next:reservations")
            op.put(
                "reservations",
                {
                    "id": replaced,
                    "node": r["node"],
                    "resource": r["resource"],
                    "amount": amount,
                    "task": r["task"],
                    "run": None,
                    "holder": r["holder"],
                    "from_at": op.now,
                    "until_at": None,
                    "released_at": None,
                    "why": None,
                },
            )
            op.data = {
                "released": id,
                "id": replaced,
                "node": r["node"],
                "resource": r["resource"],
                "amount": amount,
                "holder": r["holder"],
                "why": why,
            }
        return replaced if op.seq is not None else id

    # -- runs -----------------------------------------------------------------

    def start_run(
        self,
        task,
        holder,
        *,
        node,
        sandbox,
        fence,
        branch=None,
        expect_s,
        harness=None,
    ):
        """Start a run of a ready task, in one event: it checks the task is
        ready, that `fence` is its item's live lease held by `holder` (a task
        with no item has no fence) and that the node is live and runs
        `sandbox`; then it reserves the task's needs on the node from now with
        no end, raises `attempts`, marks the task running and starts the run.
        Refused whole if any reservation has no room: never queued, never
        part-reserved. Returns the run's id."""
        expect_ms = _seconds(expect_s, "expect_s")
        with self._operation("start_run", "run.start") as op:
            t = op.need("tasks", [task], "task")
            why = _unready(op, t, op.now)
            if why:
                raise Refused(why)
            if t["number"] is not None:
                if fence is None:
                    raise Refused(
                        f"task {task}'s item {t['number']} starts under its live "
                        "lease's fence"
                    )
                lease = self._live_lease(op, t["number"], fence)
                if lease["holder"] != holder:
                    raise Refused(
                        f"item {t['number']}'s lease is held by {lease['holder']}, "
                        f"not {holder}"
                    )
            elif fence is not None:
                raise Refused(f"task {task} has no item, so its run has no fence")
            if sandbox not in self._live_node(op, node)["drivers"]:
                raise Refused(f"node {node} does not run {sandbox}")
            id = op.take("next:runs")
            op.subject = f"run:{id}"
            attempt = t["attempts"] + 1
            op.put("tasks", {**t, "attempts": attempt, "state": "running"})
            op.put(
                "runs",
                {
                    "id": id,
                    "task": task,
                    "attempt": attempt,
                    "holder": holder,
                    "fence": fence,
                    "node": node,
                    "sandbox": sandbox,
                    "branch": branch,
                    "pgid": None,
                    "phase": "started",
                    "phase_at": op.now,
                    "expect_until": None if expect_ms is None else op.now + expect_ms,
                    "harness": {} if harness is None else harness,
                    "footprint": {},
                    "cost": {},
                    "outcome": None,
                    "reason": None,
                    "started_at": op.now,
                    "finished_at": None,
                },
            )
            ids = self._reserve(
                op,
                node,
                t["needs"],
                task=task,
                run=id,
                holder=holder,
                from_at=op.now,
                until_at=None,
            )
            op.data = {
                "run": id,
                "task": task,
                "attempt": attempt,
                "holder": holder,
                "node": node,
                "fence": fence,
                "reservations": ids,
            }
        return id

    def run_phase(self, id, phase, *, expect_s=None):
        """A live run's phase, and when it expects the next (None: no
        expectation). Refused for a finished run."""
        expect_ms = _seconds(expect_s, "expect_s")
        with self._operation("run_phase", "run.phase", f"run:{id}") as op:
            run = op.need("runs", [id], "run")
            if run["finished_at"] is not None:
                raise Refused(f"run {id} is finished")
            op.put(
                "runs",
                {
                    **run,
                    "phase": phase,
                    "phase_at": op.now,
                    "expect_until": None if expect_ms is None else op.now + expect_ms,
                },
            )
            op.data = {"run": id, "task": run["task"], "phase": phase}

    def run_pgid(self, id, pgid):
        """Set a live run's process group, known only after its spawn, which
        follows `start_run`. Once: refused when it is set, to this group or
        another, and for a finished run. A `pgid` is an int above 1: to
        `killpg`, 0 is the caller's own group and 1 is init's. It moves nothing
        else of the run, so a group sent late does not touch `phase_at`."""
        if not schema.is_int(pgid) or pgid < 2:
            raise Refused(f"a pgid is a process group's id, above 1, not {pgid!r}")
        with self._operation("run_pgid", "run.pgid", f"run:{id}") as op:
            run = op.need("runs", [id], "run")
            if run["finished_at"] is not None:
                raise Refused(f"run {id} is finished")
            if run["pgid"] is not None:
                raise Refused(f"run {id}'s pgid is set, to {run['pgid']}")
            op.put("runs", {**run, "pgid": pgid})
            op.data = {"run": id, "task": run["task"], "pgid": pgid}

    def finish_run(self, id, outcome, reason, *, footprint, cost, result=None):
        """Finish a run with its outcome, and release its reservations in the
        same event; its task, running, is open again. Never refused for a
        stale fence: it touches nothing of the item. Refused for a run that
        is finished."""
        with self._operation("finish_run", "run.finish", f"run:{id}") as op:
            run = op.need("runs", [id], "run")
            if run["finished_at"] is not None:
                raise Refused(f"run {id} is finished")
            op.put(
                "runs",
                {
                    **run,
                    "outcome": outcome,
                    "reason": reason,
                    "footprint": footprint,
                    "cost": cost,
                    "finished_at": op.now,
                },
            )
            if result is not None:
                op.put("run_text", {"run": id, "result": result})
            released = []
            for r in op.read("reservations", live=True, run=id):
                op.put("reservations", {**r, "released_at": op.now, "why": "finished"})
                released.append(r["id"])
            task = op.get("tasks", [run["task"]])
            if task is not None and task["state"] == "running":
                op.put("tasks", {**task, "state": "open"})
            op.data = {
                "run": id,
                "task": run["task"],
                "outcome": outcome,
                "released": released,
            }

    # -- changes, checks, reviews and merges ---------------------------------

    @staticmethod
    def _open_change(op, id):
        change = op.need("changes", [id], "change")
        for field, what in (("merged_at", "merged"), ("dropped_at", "dropped")):
            if change[field] is not None:
                raise Refused(f"change {id} is {what}, not open")
        return change

    def open_or_update_change(
        self,
        repo,
        branch,
        head,
        *,
        title=KEEP,
        body=KEEP,
        number=KEEP,
        relation=KEEP,
        base=KEEP,
        position=KEEP,
        draft=KEEP,
        url=KEEP,
        by,
    ):
        """Open a change, a branch of a repo offered to merge, with the repo's
        next `n`; or update the open change on that branch, with `head`. A
        field not given keeps its value on an update and takes its default on
        an open (`CHANGE_FIELDS`); an open gives the `title`. `pushed_at`
        moves when the head does. An open change with no `number` takes one by
        an update; one with a number keeps it, so another `number`, or `None`
        given, is a second open change on the branch, and refused. Returns the
        change's id, an update that changes nothing too, and writes nothing
        then."""
        given = {
            name: value
            for name, value in (
                ("title", title),
                ("body", body),
                ("number", number),
                ("relation", relation),
                ("base", base),
                ("position", position),
                ("draft", draft),
                ("url", url),
            )
            if value is not KEEP
        }
        with self._operation("open_or_update_change", "change.open") as op:
            if given.get("number") is not None:
                op.need("items", [given["number"]], "item")
            live = op.read("changes", live=True, repo=repo, branch=branch)
            if live:
                change = live[0]
                id = change["id"]
                op.kind = "change.edit"
                number = change["number"]
                if number is not None and given.get("number", number) != number:
                    raise Refused(
                        f"{repo} has an open change on {branch} already: change "
                        f"{id}, of item {number}"
                    )
                fields = {k: v for k, v in given.items() if k != "body"}
                after = {**change, **fields, "head": head}
                if head != change["head"]:
                    after["pushed_at"] = op.now
                if after != change:
                    op.put("changes", after)
                text = op.get("change_text", [id])
                if "body" in given and (text or {}).get("body") != given["body"]:
                    op.put("change_text", {"change": id, "body": given["body"]})
                if not op._puts:
                    op.nothing()
            else:
                if "title" not in given:
                    raise Refused(f"a change is opened with its title: {repo} {branch}")
                after = {**CHANGE_FIELDS, **given}
                body = after.pop("body")
                id = op.take("next:changes")
                after.update(
                    id=id,
                    repo=repo,
                    n=op.take(f"next:change:{repo}"),
                    branch=branch,
                    head=head,
                    opened_at=op.now,
                    opened_by=by,
                    pushed_at=op.now,
                    merged_at=None,
                    dropped_at=None,
                    dropped_by=None,
                )
                op.put("changes", after)
                op.put("change_text", {"change": id, "body": body})
            op.subject = f"change:{id}"
            op.data = {
                "change": id,
                "repo": repo,
                "n": after["n"],
                "branch": branch,
                "head": head,
                "number": after["number"],
            }
        return id

    def drop_change(self, id, by, why):
        """Drop an open change. Refused for one merged or dropped."""
        with self._operation("drop_change", "change.drop", f"change:{id}") as op:
            change = self._open_change(op, id)
            op.put("changes", {**change, "dropped_at": op.now, "dropped_by": by})
            op.data = {"change": id, "by": by, "why": why}

    def record_check(self, change, head, name, state, detail=None):
        """A change's check `name` at `head` (green, red, skipped, pending),
        by the actor. False when it already reads so (and nothing is
        written)."""
        with self._operation("record_check", "check.record", f"change:{change}") as op:
            op.need("changes", [change], "change")
            before = op.get("checks", [change, head, name])
            if before and (before["state"], before["detail"]) == (state, detail):
                op.nothing()
            op.put(
                "checks",
                {
                    "change": change,
                    "head": head,
                    "name": name,
                    "state": state,
                    "detail": detail,
                    "at": op.now,
                    "by": self.actor,
                },
            )
            op.data = {"change": change, "head": head, "name": name, "state": state}
        return op.seq is not None

    def file_review(
        self,
        target,
        round,
        kind,
        *,
        item=None,
        author,
        heads=None,
        pages=None,
        filed_by=None,
    ):
        """File a review of item `target`'s work in `round`, as item `item`
        when it has one. `heads` (`{change id: sha}`, each a change the record
        holds) is what the review judges: written here, once, and never
        edited. Returns its id."""
        heads = {} if heads is None else heads
        if not isinstance(heads, dict):
            raise Refused(f"a review's heads are {{change id: sha}}, not {heads!r}")
        with self._operation("file_review", "review.file", f"item:{target}") as op:
            op.need("items", [target], "item")
            if item is not None:
                op.need("items", [item], "review item")
            for change, sha in heads.items():
                if not (
                    isinstance(change, str)
                    and change.isascii()
                    and change.isdigit()
                    and str(int(change)) == change
                ):
                    raise Refused(
                        f"a review's heads key a change by its id: {change!r}"
                    )
                if not isinstance(sha, str) or not sha:
                    raise Refused(f"change {change}'s head is a sha, not {sha!r}")
                op.need("changes", [int(change)], "change")
            id = op.take("next:reviews")
            op.put(
                "reviews",
                {
                    "id": id,
                    "target": target,
                    "round": round,
                    "kind": kind,
                    "item": item,
                    "author": author,
                    "heads": dict(sorted(heads.items())),
                    "pages": [] if pages is None else pages,
                    "filed_by": filed_by,
                    "reviewer": None,
                    "verdict": None,
                    "findings": [],
                    "filed_at": op.now,
                    "verdict_at": None,
                    "read_at": None,
                },
            )
            op.data = {"review": id, "target": target, "round": round, "kind": kind}
        return id

    def record_verdict(self, id, reviewer, verdict, findings):
        """A review's verdict (approve, changes) and findings. Refused for a
        review that has one."""
        if verdict not in schema.VALUES[("reviews", "verdict")]:
            raise Refused(f"a verdict is approve or changes, not {verdict!r}")
        with self._operation("record_verdict", "review.verdict") as op:
            review = op.need("reviews", [id], "review")
            op.subject = f"item:{review['target']}"
            if review["verdict"] is not None:
                raise Refused(
                    f"review {id} has its verdict already: {review['verdict']}"
                )
            op.put(
                "reviews",
                {
                    **review,
                    "reviewer": reviewer,
                    "verdict": verdict,
                    "findings": findings,
                    "verdict_at": op.now,
                },
            )
            op.data = {"review": id, "target": review["target"], "verdict": verdict}

    def review_read(self, id):
        """Mark a review read by its author's work. False when it was read
        already (and nothing is written)."""
        with self._operation("review_read", "review.read") as op:
            review = op.need("reviews", [id], "review")
            op.subject = f"item:{review['target']}"
            if review["read_at"] is not None:
                op.nothing()
            op.put("reviews", {**review, "read_at": op.now})
            op.data = {"review": id, "target": review["target"]}
        return op.seq is not None

    def record_merge(self, change, sha, by, rule, *, commit=None):
        """A change merged at `sha`, its head, by `rule` (what allowed it),
        and `merged_at` in the same event. Refused for a change not open, and
        a `sha` that is not its head. Returns the merge's id."""
        with self._operation("record_merge", "merge", f"change:{change}") as op:
            found = self._open_change(op, change)
            if sha != found["head"]:
                raise Refused(f"change {change}'s head is {found['head']}, not {sha!r}")
            id = op.take("next:merges")
            op.put("changes", {**found, "merged_at": op.now})
            op.put(
                "merges",
                {
                    "id": id,
                    "change": change,
                    "sha": sha,
                    "commit": commit,
                    "by": by,
                    "rule": rule,
                    "at": op.now,
                },
            )
            op.data = {"merge": id, "change": change, "sha": sha, "by": by}
        return id

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

    def ask(self, number, text, *, kind, run=None, who=None):
        """A question on an item (blocked, pr), from a run when given. Returns
        its id."""
        with self._operation("ask", "question.ask", f"item:{number}") as op:
            op.need("items", [number], "item")
            if run is not None:
                op.need("runs", [run], "run")
            id = op.take("next:questions")
            op.put(
                "questions",
                {
                    "id": id,
                    "number": number,
                    "run": run,
                    "who": who or self.actor,
                    "kind": kind,
                    "text": text,
                    "asked_at": op.now,
                    "answered_at": None,
                    "answered_by": None,
                    "answer": None,
                },
            )
            op.data = {"question": id, "number": number, "kind": kind}
        return id

    def answer(self, question, comment):
        """Answer a question by a comment on its item, the comment's author
        answering. Refused for a question answered already."""
        with self._operation("answer", "question.answer") as op:
            q = op.need("questions", [question], "question")
            op.subject = f"item:{q['number']}"
            if q["answered_at"] is not None:
                raise Refused(f"question {question} is answered already")
            reply = op.need("comments", [comment], "comment")
            if reply["number"] != q["number"]:
                raise Refused(f"comment {comment} is not on item {q['number']}")
            op.put(
                "questions",
                {
                    **q,
                    "answered_at": op.now,
                    "answered_by": reply["who"],
                    "answer": comment,
                },
            )
            op.data = {"question": question, "number": q["number"], "answer": comment}

    def state(
        self, type, text, *, effort=None, number=None, where=None, supersedes=None
    ):
        """A statement (of a type in `schema.VALUES`), on an effort, an item
        or neither. Returns its id."""
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

    # -- readings -------------------------------------------------------------

    def record_reading(self, source, kind, value):
        """A reading: `source`'s value of `kind` (a budget's use, say), at
        the record's clock. Returns its id."""
        with self._operation("record_reading", "reading", f"reading:{source}") as op:
            id = op.take("next:readings")
            op.put(
                "readings",
                {
                    "id": id,
                    "source": source,
                    "kind": kind,
                    "value": value,
                    "at": op.now,
                },
            )
            op.data = {"reading": id, "source": source, "kind": kind}
        return id

    # -- the journal itself ---------------------------------------------------

    def note(self, kind, subject, **data):
        """An event that stands for no record: `note`, with `kind` and the
        data. Returns its seq."""
        with self._operation("note", "note", subject) as op:
            op.empty_ok = True
            op.data = {"kind": kind, **data}
        return op.seq


def check_dump(dump):
    """The events of a whole dump, or Refused, writing nothing: a dump is
    `{format, records, journal, bookkeeping}`, its seqs run from 1 by one,
    each journal entry is within its limit, its records are the fold of its
    journal, every item has an effort link, and its bookkeeping is valid (no
    cursor past the journal)."""
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
    if not isinstance(dump["records"], dict) or not set(dump["records"]) <= (journaled):
        raise Refused("a dump's records are by journaled kind")
    kept = {k for k, v in RECORDS.items() if not v.journaled}
    if not isinstance(dump["bookkeeping"], dict) or not set(dump["bookkeeping"]) <= (
        kept
    ):
        raise Refused("a dump's bookkeeping is beats and cursors")
    try:
        journal = [ev.Event.from_dict(raw) for raw in dump["journal"]]
        for event in journal:
            ev.check_event(event)
    except (SchemaError, KeyError, TypeError, ValueError) as e:
        raise Refused(f"a dump's journal: {e}") from None
    limit = schema.LIMITS["entry_bytes"]
    view = ev.MemoryView()
    for i, event in enumerate(journal, 1):
        if event.seq != i:
            raise Refused(f"a dump's seqs run from 1 by one: {event.seq} at {i}")
        size = entry_size(event, _priors(view.get, event))
        if size > limit:
            raise Refused(
                f"a journal entry of {size} bytes (event {event.seq}), past the "
                f"limit of {limit}"
            )
        ev.apply(view, event)
    records = view.records()
    for kind in sorted(journaled):
        if records[kind] != dump["records"].get(kind, []):
            raise Refused(f"a dump's {kind} are not the fold of its journal")
    missing = unlinked_items(records)
    if missing:
        raise Refused(
            "every item has an effort link: "
            + ", ".join(f"item {n}" for n in missing)
            + " has none"
        )
    for kind in sorted(kept):
        rows = dump["bookkeeping"].get(kind, [])
        if not isinstance(rows, list):
            raise Refused(f"a dump's {kind} are a list")
        for row in rows:
            try:
                row = schema.validate(kind, row)
            except SchemaError as e:
                raise Refused(f"a dump's {kind}: {e}") from None
            if kind == "cursors" and row["seq"] > len(journal):
                raise Refused(f"a cursor past the journal: {row}")
    return journal


def _capacity_findings(records):
    """Invariant 6: for each node and resource, the live reservations never
    sum past its capacity at any instant from when the capacity was set
    (`set_capacity` is checked from its own time on)."""
    findings = []
    caps = {(c["node"], c["resource"]): c for c in records.get("capacity", ())}
    held = {}
    for r in records.get("reservations", ()):
        if r["released_at"] is None:
            held.setdefault((r["node"], r["resource"]), []).append(r)
    for (node, resource), live in sorted(held.items()):
        cap = caps.get((node, resource))
        if cap is None:
            findings.append(
                f"node {node} {resource}: {_peak(live, 0, None)} reserved, and no "
                "capacity declared"
            )
            continue
        peak = _peak(live, cap["at"], None)
        if peak > cap["amount"]:
            findings.append(
                f"node {node} {resource}: {peak} reserved at once, past its "
                f"capacity {cap['amount']}"
            )
    return findings


def _run_findings(dump):
    """Invariant 7: every running run started in the record (by a
    `run.start`, not brought by an import) holds live reservations for its
    task's needs on its node, and no finished run's reservation is live."""
    findings = []
    records = dump["records"]
    started, seen = set(), set()
    for raw in dump["journal"]:
        for kind, key, _ in raw["puts"]:
            if kind == "runs" and key[0] not in seen:
                seen.add(key[0])
                if raw["kind"] == "run.start":
                    started.add(key[0])
    runs = {r["id"]: r for r in records.get("runs", ())}
    tasks = {t["id"]: t for t in records.get("tasks", ())}
    live = [
        r
        for r in records.get("reservations", ())
        if r["released_at"] is None and r["run"] is not None
    ]
    for id in sorted(started):
        run = runs.get(id)
        if run is None or run["finished_at"] is not None:
            continue
        needs = tasks[run["task"]]["needs"] if run["task"] in tasks else {}
        for resource, amount in sorted(needs.items()):
            have = sum(
                r["amount"]
                for r in live
                if r["run"] == id
                and (r["node"], r["resource"]) == (run["node"], resource)
            )
            if have < amount:
                findings.append(
                    f"run {id} is running on {run['node']} and holds {have} of "
                    f"the {amount} {resource} its task needs"
                )
    for r in live:
        run = runs.get(r["run"])
        if run is not None and run["finished_at"] is not None:
            findings.append(
                f"reservation {r['id']} is live for run {r['run']}, which is "
                "finished"
            )
    return findings


def _pgid_findings(journal):
    """A run's `pgid` is set once: by a `run.pgid`, on a live run, to a group
    above 1. Only a run an import brought comes with a group, and keeps it."""
    findings = []
    held = {}
    for raw in journal:
        for kind, key, value in raw["puts"]:
            if kind != "runs":
                continue
            id, pgid = key[0], value.get("pgid")
            at = f"run {id}, event {raw['seq']}"
            if id not in held and raw["kind"] == "import":
                held[id] = pgid
                if pgid is not None and (not schema.is_int(pgid) or pgid < 2):
                    findings.append(f"{at}: its pgid imported as {pgid!r}")
                continue
            first = id not in held
            was = held.get(id)
            held[id] = pgid
            if raw["kind"] != "run.pgid":
                if pgid != was:
                    findings.append(f"{at}: its pgid changed by a {raw['kind']}")
            elif first and pgid is not None:
                findings.append(f"{at}: its pgid set on a run no run.start wrote")
            elif was is not None:
                findings.append(f"{at}: its pgid set again, {was} to {pgid}")
            elif value.get("finished_at") is not None:
                findings.append(f"{at}: its pgid set when it is finished")
            elif not schema.is_int(pgid) or pgid < 2:
                findings.append(f"{at}: its pgid set to {pgid!r}")
    return findings


def _findings(dump):
    """What `check()` finds in a dump, whatever the backend."""
    findings = []
    seqs = [e["seq"] for e in dump["journal"]]
    if seqs != list(range(1, len(seqs) + 1)):
        findings.append("journal: the seqs do not run from 1 by one")
    limit = schema.LIMITS["entry_bytes"]
    view = ev.MemoryView()
    try:
        for raw in dump["journal"]:
            event = ev.Event.from_dict(raw)
            # measured as load measures it: the priors from before the event
            size = entry_size(event, _priors(view.get, event))
            if size > limit:
                findings.append(
                    f"journal: event {event.seq}'s entry is {size} bytes, past "
                    f"the limit of {limit}"
                )
            ev.apply(view, event)
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
    findings += _capacity_findings(dump["records"])
    findings += _run_findings(dump)
    findings += _pgid_findings(dump["journal"])
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
    """Open the record at `url` (`sqlite:///path/record.sqlite`, or
    `henge:<folder>#<name>` for a record in the Henge store running in
    `folder`). `shared`
    opens the record a deployment runs on, which must be durable unless
    `allow_undurable` (tests and the bench). `backend` passes settings the
    backend takes (tests only: SQLite's `synchronous`, `fullfsync`; Henge's
    `timeout_s`)."""
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
from . import henge  # noqa: E402,F401  (registers the Henge backend)
