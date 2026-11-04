# SPDX-License-Identifier: Apache-2.0
"""The Henge backend: the record in four spaces of a running Henge store,
reached with the client alone (`Store.connect("ipc://<folder>/henge.sock")`).

Henge gives one object's compare-and-set and nothing wider, so an operation is
one **journal entry**, put `absent` at the next seq: that put is the commit
point. The event's records, its index chunks and the `.state` keys follow it,
each put with the version the writer expects, then one `sync` of the store,
then `head`. Recovery redoes at most the one entry past `applied`.

    quarrylith.<name>.journal  [seq]                  {"event", "priors"}
    quarrylith.<name>.rec      [kind, *key]           the record, and `_seq`
    quarrylith.<name>.idx      [kind, field, value, n] | [kind, "*", n]
                               | [journal, subject, n] a chunk of sorted keys;
                               [..., "dir"] its directory
    quarrylith.<name>.state    [head] [applied] [writer] [schema] [loading]

**One writer per record**: `open_henge` takes an exclusive `flock` on
`<folder>/quarrylith.<name>.lock`. Correctness does not rest on it. Every put
but the commit put passes **the guard**: its `expect` is the writer's own last
put of the object, or a version read before a check that found
`journal/[high+1]` absent (the commit put is that check on the normal path).
A check that finds the entry means another writer is live: this one puts
nothing more and closes. **One put at a time**: each put is sent once the one
before it is answered, so the store keeps the record's writes in one order and
a power cut leaves a prefix of them. **Only past an answer**: the writer goes
on past a value only once the store has answered a put of it to this writer. A
value it read instead (its commit put's entry read back, a retry or a redo
with nothing left to put, the entry a redo starts from) is put again, the same
value with the version read, before it goes on: on a mirror, an `Unavailable`
put may be on the leader's copy alone, and a read may come from either.

No read lists a space, and no object is ever deleted: what would be a delete
is a versioned put of null (`loading`, at a load's end).
"""

import asyncio
import collections
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import re
import socket
import threading
import time
from bisect import bisect_right
from pathlib import Path

from henge.store.client import AsyncStore, Store
from henge.store.client.api import HengeError

from . import contract, schema
from . import events as ev
from .contract import Record, Refused, Unavailable, Unknown
from .schema import RECORDS

RECORD_SCHEMA_VERSION = 1
#: A chunk holds at most this many keys and this many bytes (`schema.size`);
#: a split cuts it into pieces of at most half of each.
CHUNK_KEYS = 8192
CHUNK_BYTES = 512 * 1024
PIECE_KEYS = 4096
PIECE_BYTES = 256 * 1024
#: A subject's journal chunk is `seq // JOURNAL_CHUNK`: on-disk layout, apart
#: from the bound a record's chunk splits at.
JOURNAL_CHUNK = 8192
#: The most keys one `get_many` takes (henge's `MANY`).
MANY = 1000
#: How long the record waits for the store before it gives an operation up:
#: past the commit point that is `Unknown`, before it `Unavailable`.
TIMEOUT_S = 30.0
#: One try of a client call; the client retries it with the same request id
#: until the record's timeout is spent.
TRY_MS = 2000
#: The first pause before the record tries a refused call again; each pause
#: is the double of the one before, up to `TRY_MS`.
PAUSE_MS = 10
#: The store's refusals for good: not tried again.
FOR_GOOD = ("too_large", "invalid", "forbidden")
#: A follower's catch-up read of `head` after this long with no note.
FOLLOW_IDLE_S = 1.0
#: How many objects the writer keeps in memory, read or put (a journal
#: entry only as it commits it).
KEPT = 100_000
#: A record's name: its spaces are `quarrylith.<name>.*`.
NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
SPACES = ("journal", "rec", "idx", "state")

#: What `put` with `expect` is told when nothing is there.
ABSENT = object()
#: A put function's answer when the object needs no put.
SKIP = object()


class _Conflict(Exception):
    """The store's `Conflict`: the object is not at the version expected."""


class _StoreDown(Exception):
    """Any other error of the store: it may or may not have done it."""


class _ForGood(_StoreDown):
    """A refusal that trying again will not change (`FOR_GOOD`)."""


class _Found(Exception):
    """A check found `journal/[high+1]`: another writer is live."""


class _Tries:
    """The pauses between the tries of a refused call: `PAUSE_MS` first,
    each the double of the one before, up to `TRY_MS`, none past
    `deadline`. `over(e)`: no try is left, the deadline passed or `e` is a
    refusal for good."""

    def __init__(self, deadline):
        self.deadline = deadline
        self.next_s = PAUSE_MS / 1000

    def over(self, e):
        return isinstance(e, _ForGood) or time.monotonic() >= self.deadline

    def pause(self):
        left = self.deadline - time.monotonic()
        if left > 0:
            time.sleep(min(self.next_s, left))
        self.next_s = min(self.next_s * 2, TRY_MS / 1000)


def spaces(name):
    return {s: f"quarrylith.{name}.{s}" for s in SPACES}


def parse_url(url):
    """`henge:<folder>#<name>` -> (folder, name), or Refused."""
    where = url[len("henge:") :] if url.startswith("henge:") else ""
    folder, _, name = where.rpartition("#")
    if not folder or not NAME_RE.fullmatch(name or ""):
        raise Refused(
            f"a Henge record's URL is henge:<folder>#<name>, the name "
            f"[a-z0-9][a-z0-9_-]*, not {url!r}"
        )
    return Path(folder).absolute(), name


def same_entry(a, b):
    """Two journal entries are the same entry: compared by `canonical`,
    since the store gives tuples back as lists."""
    try:
        return schema.canonical(a) == schema.canonical(b)
    except schema.SchemaError:
        return False


def _ckey(key):
    """A key as the writer's memory keeps it: by its JSON, so `True` and `1`
    stay apart."""
    return json.dumps(key, separators=(",", ":"))


def _same(a, b):
    """Two match values are the same value: equal, and both bool or not."""
    return a == b and isinstance(a, bool) == isinstance(b, bool)


# -- the layout's index: chunks and their directory --------------------------


def ix_key(ix, part):
    """The `.idx` key of index `ix` ((kind, field, value), field "*" for
    every key of the kind, kind "journal" for a subject's seqs), at chunk
    `part` (a number, or "dir")."""
    kind, field, value = ix
    if kind == "journal" or field == "*":
        return [kind, field, part]
    return [kind, field, value, part]


def ranges(directory):
    """Chunk number -> (lowest key, the next listed lowest key): chunk 0 has
    no lower bound, the last no upper one."""
    listed = sorted(directory or [], key=lambda e: e[1])
    lows = [low for _, low in listed]
    out = {0: (None, lows[0] if lows else None)}
    for i, (n, low) in enumerate(listed):
        out[n] = (low, lows[i + 1] if i + 1 < len(lows) else None)
    return out


def chunk_of(key, directory):
    """The chunk whose range holds `key`."""
    listed = sorted(directory or [], key=lambda e: e[1])
    i = bisect_right([low for _, low in listed], key)
    return listed[i - 1][0] if i else 0


def in_range(key, rng):
    low, high = rng
    return (low is None or key >= low) and (high is None or key < high)


def filtered(keys, rng):
    """A chunk as read: its keys in its range, sorted, once each. Every other
    key (a split's leftover) is ignored."""
    out = []
    for k in sorted(keys or []):
        if in_range(k, rng) and (not out or out[-1] != k):
            out.append(k)
    return out


def edited(keys, edits):
    """`keys` with each edit applied: key -> True (in) or False (out)."""
    have = {_ckey(k): k for k in keys}
    for c, (k, inside) in edits.items():
        if inside:
            have[c] = k
        else:
            have.pop(c, None)
    return sorted(have.values())


def over(keys):
    """Is a chunk past either bound?"""
    max_keys, max_bytes = CHUNK_KEYS, CHUNK_BYTES
    if len(keys) > max_keys:
        return True
    # a short chunk cannot pass the bytes: measured only once its keys could
    if not keys or len(keys) * (_key_size_max(keys[0]) + 1) + 2 <= max_bytes:
        return False
    return schema.size(keys) > max_bytes


def _key_size_max(key):
    """The most `schema.size` can give a key of `key`'s shape: an `int` part
    20 characters, a `str` part its `key_bytes`, each a control character
    escaped as six, in its quotes; the commas and brackets."""
    text = 2 + 6 * schema.LIMITS["key_bytes"]
    return 1 + len(key) + sum(20 if schema.is_int(p) else text for p in key)


def pieces(keys):
    """A chunk past its bounds cut, in key order, into pieces of at most
    `PIECE_KEYS` keys and `PIECE_BYTES` bytes, each filled before the next
    starts."""
    out, piece, used = [], [], 2
    for k in keys:
        cost = schema.size(k) + (1 if piece else 0)
        if piece and (len(piece) == PIECE_KEYS or used + cost > PIECE_BYTES):
            out.append(piece)
            piece, used = [], 2
            cost = schema.size(k)
        piece.append(k)
        used += cost
    if piece:
        out.append(piece)
    return out


def index_edits(event, priors):
    """The index edits of an event, from its puts and their priors: index ->
    {key's JSON: (key, in)}. A new record joins its kind's `*` and each match
    field's index; a moved match field leaves its old value's and joins the
    new one's. The event's subject indexes its seq. Applying them twice is
    applying them once."""
    out = collections.defaultdict(dict)
    held = {}
    for (kind, key, value), (_, _, prior) in zip(event.puts, priors):
        c = (kind, _ckey(list(key)))
        before = held.get(c, prior)
        k = list(key)
        if before is None:
            out[(kind, "*", None)][_ckey(k)] = (k, True)
        for f in RECORDS[kind].match:
            new = value[f]
            if before is not None:
                if _same(before[f], new):
                    continue
                out[(kind, f, before[f])][_ckey(k)] = (k, False)
            out[(kind, f, new)][_ckey(k)] = (k, True)
        held[c] = {f: value[f] for f in RECORDS[kind].match}
    if event.subject is not None:
        out[("journal", event.subject, None)][str(event.seq)] = (event.seq, True)
    return out


# -- the backend --------------------------------------------------------------


class HengeRecord(Record):
    """The record in a Henge store, written by this one process.

    `HengeRecord(store, name)` opens the record on a connected `Store`: it
    reads the store's report, runs recovery and names its writer. It takes no
    lock: `open_henge` does, and is how a record is opened."""

    backend = "henge"

    def __init__(self, store, name, *, timeout_s=TIMEOUT_S, lock=None, **kw):
        #: The writer lock's descriptor, released by `close()` (open_henge's).
        self._lockfd = lock
        self.store = store
        try:
            super().__init__(**kw)
            if not NAME_RE.fullmatch(name or ""):
                raise Refused(f"a record's name is [a-z0-9][a-z0-9_-]*, not {name!r}")
        except BaseException:
            self._shut()
            raise
        self.name = name
        self.timeout_s = timeout_s
        sp = spaces(name)
        self.J, self.R, self.I, self.S = (sp[s] for s in SPACES)
        #: (space, key's JSON) -> [value or ABSENT, version or None, epoch, own]
        self._kept = collections.OrderedDict()
        #: The highest seq this writer knows to be committed.
        self._high = 0
        #: How many checks have found journal/[high+1] absent: a version read
        #: at an earlier epoch was read before a check.
        self._epoch = 0
        self._closed = None
        self._ready = False
        self._loading = None
        self._report = {}
        self._recheck = False
        try:
            self._start()
        except _StoreDown as e:
            self._shut()
            raise Unavailable(f"the store at {self._where()}: {e}") from e
        except BaseException:
            self._shut()
            raise

    # -- opening ----------------------------------------------------------------

    @classmethod
    def open(
        cls,
        url,
        *,
        readonly=False,
        shared=False,
        actor,
        via,
        authorize=None,
        **settings,
    ):
        folder, name = parse_url(url)
        return open_henge(
            folder,
            name,
            readonly=readonly,
            shared=shared,
            actor=actor,
            via=via,
            authorize=authorize,
            **settings,
        )

    def _start(self):
        self._read_report(at_open=True)
        row = self._fetch(self.S, ["schema"])
        if row[1] is None:
            return
        version = row[0].get("version") if isinstance(row[0], dict) else None
        if not schema.is_int(version):
            raise Refused(f"the record {self.name} has no schema version: {row[0]!r}")
        if version > RECORD_SCHEMA_VERSION:
            raise Refused(
                f"the record is schema {version}, newer than this quarrylith's "
                f"{RECORD_SCHEMA_VERSION}: update quarrylith"
            )
        if version < RECORD_SCHEMA_VERSION:
            raise Refused(
                f"the record is schema {version}, older than this quarrylith's "
                f"{RECORD_SCHEMA_VERSION}: run `quarrylith record migrate`"
            )
        self._ready = True
        self._recover()
        loading = self._fetch(self.S, ["loading"])[0]
        self._loading = loading if isinstance(loading, dict) else None
        writer = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "opened_at": contract.wall_ms(),
            "readonly": bool(self.readonly),
        }
        # row 7: a fresh read, then journal/[head+1], then the put
        self._put(self.S, ["writer"], lambda cur: writer, why="open", fresh=True)

    def _read_report(self, *, at_open=False):
        """The store's report on the record's journal space: `durable` is its
        `power_safe and ordered`. A store without `durability` and `sync`
        cannot run the commit protocol, and is refused at open."""
        missing = [op for op in ("durability", "sync") if not hasattr(self.store, op)]
        if missing:
            raise Refused(
                f"the store has no {' or '.join(missing)}: it cannot run the "
                "record's commit protocol (a single-node store or a mirror can)"
            )
        try:
            report = self.store.durability(self.J)
            if at_open:
                self.store.sync(self.J)
        except HengeError as e:
            if getattr(e, "code", None) == "invalid":
                raise Refused(
                    f"the store refused durability or sync ({e}): it cannot run "
                    "the record's commit protocol"
                ) from e
            raise Unavailable(f"the store at {self._where()}: {e}") from e
        if not isinstance(report, dict):
            raise Refused(f"the store's report is not a report: {report!r}")
        self._report = report
        self._recheck = False

    def _where(self):
        return getattr(self.store, "endpoint", None) or self.name

    # -- the writer's memory and the store's calls ----------------------------

    def _live(self):
        if self._closed is not None:
            raise Unavailable(f"the record {self.name} is closed: {self._closed}")

    def _call(self, op, *args, **kw):
        """One call of the store, its errors as `_Conflict`, `_ForGood` or
        `_StoreDown`."""
        try:
            return getattr(self.store, op)(*args, **kw)
        except HengeError as e:
            code = getattr(e, "code", None)
            if code == "conflict":
                raise _Conflict(str(e)) from e
            if code == "unavailable":
                self._recheck = True
            if code in FOR_GOOD:
                raise _ForGood(f"{op}: {e}") from e
            raise _StoreDown(f"{op}: {e}") from e
        except (OSError, RuntimeError, TimeoutError, asyncio.TimeoutError) as e:
            self._recheck = True
            raise _StoreDown(f"{op}: {e}") from e

    def _keep(self, space, key, value, version, own):
        c = (space, _ckey(key))
        self._kept[c] = [value, version, self._epoch, own]
        self._kept.move_to_end(c)
        while len(self._kept) > KEPT:
            self._kept.popitem(last=False)
        return self._kept[c]

    def _forget(self):
        self._kept.clear()

    def _fetch(self, space, key):
        """A fresh read of one object: [value or ABSENT, version, epoch, own]."""
        try:
            item = self._call("get", space, key)
        except _Conflict as e:
            raise _StoreDown(str(e)) from e
        if item is None:
            return self._keep(space, key, ABSENT, None, False)
        return self._keep(space, key, item.value, item.version, False)

    def _fetch_many(self, space, keys, *, keep=True):
        out = []
        for i in range(0, len(keys), MANY):
            batch = keys[i : i + MANY]
            try:
                items = self._call("get_many", space, batch)
            except _Conflict as e:
                raise _StoreDown(str(e)) from e
            for key, item in zip(batch, items):
                value, version = (
                    (ABSENT, None)
                    if item is None
                    else (
                        item.value,
                        item.version,
                    )
                )
                if keep:
                    out.append(self._keep(space, key, value, version, False))
                else:
                    out.append([value, version, self._epoch, False])
        return out

    def _look(self, space, key):
        got = self._kept.get((space, _ckey(key)))
        return got if got is not None else self._fetch(space, key)

    def _look_many(self, space, keys, *, keep=True):
        found, missing = {}, []
        for key in keys:
            got = self._kept.get((space, _ckey(key)))
            if got is None:
                missing.append(key)
            else:
                found[_ckey(key)] = got
        for key, got in zip(missing, self._fetch_many(space, missing, keep=keep)):
            found[_ckey(key)] = got
        return [found[_ckey(key)] for key in keys]

    def _read_store(self, fn, *args):
        """A read for the contract: the store's error as `Unavailable`."""
        self._live()
        try:
            return fn(*args)
        except _StoreDown as e:
            raise Unavailable(f"the store at {self._where()}: {e}") from e

    # -- the guard and the put --------------------------------------------------

    def _check_journal(self):
        """`get(journal, [high+1])`: absent, the versions read so far may be
        put against; present, another writer is live."""
        got = self._call("get", self.J, [self._high + 1])
        if got is not None:
            raise _Found(f"journal/[{self._high + 1}] is there")
        self._epoch += 1

    def _put(self, space, key, fn, *, why, fresh=False, settle=True):
        """Put `fn(current)` (SKIP: no put) under the guard. A SKIP of a
        value read, not put by this writer, puts that value again with the
        version read (`settle`), so the writer goes on only past a put
        answered to it. On a `Conflict` or any error of the store: a pause, a
        fresh read, a check, and the put again, until the record's timeout (a
        refusal for good at once). `why` names the put path for its failure:
        "op" past the commit point is `Unknown`, anything else `Unavailable`."""
        deadline = time.monotonic() + self.timeout_s
        tries = _Tries(deadline)
        row = self._kept.get((space, _ckey(key)))
        if row is None or (fresh and not row[3]):
            row = self._again(space, key, deadline, why)
        while True:
            new = fn(row[0])
            if new is SKIP:
                if row[3] or row[0] is ABSENT or not settle:
                    return
                # read, not answered to this writer: the same value again
                new = row[0]
            try:
                if not row[3] and row[2] >= self._epoch:
                    # read after the last check: check now, then put
                    self._check_journal()
                expect = {"absent": True} if row[1] is None else {"version": row[1]}
                version = self._call("put", space, key, new, expect=expect)
            except _Found as e:
                self._fail(why, f"{e}: another writer is live", e)
            except (_Conflict, _StoreDown) as e:
                if tries.over(e):
                    self._fail(why, f"the store did not take the put: {e}", e)
                tries.pause()
                row = self._again(space, key, deadline, why)
                continue
            self._keep(space, key, new, version, True)
            return

    def _again(self, space, key, deadline, why):
        """A fresh read of one object, tried again after a pause until
        `deadline`."""
        tries = _Tries(deadline)
        while True:
            try:
                return self._fetch(space, key)
            except _StoreDown as e:
                if tries.over(e):
                    self._fail(why, f"the store did not answer: {e}", e)
                tries.pause()

    def _fail(self, why, message, cause):
        """Stop: past the commit point `Unknown`, else `Unavailable`. A check
        that found another writer's entry, or an operation past its commit
        point, closes the record: it puts nothing more."""
        self._forget()
        if why in ("op", "recover") or isinstance(cause, _Found):
            self._closed = message
            self._shut()
        if why == "op":
            raise Unknown(
                f"the record {self.name}: {message}; the operation may have "
                "happened (read its subject, or events() past the last seq)"
            ) from cause
        raise Unavailable(f"the record {self.name}: {message}") from cause

    def _sync(self, why):
        tries = _Tries(time.monotonic() + self.timeout_s)
        while True:
            try:
                self._call("sync", self.J)
                return
            except (_Conflict, _StoreDown) as e:
                if tries.over(e):
                    self._fail(why, f"the sync did not answer: {e}", e)
                tries.pause()

    # -- the commit protocol ------------------------------------------------------

    def _plan(self, event, priors, *, redo=False):
        """Step 3's puts, in order, read before the commit: (space, key, fn)
        for each record, then each index's edits (a split's new chunks, its
        directory, then its shrunk chunk). A redo also rewrites to its range
        any chunk of a touched index that holds keys outside it: the old chunk
        of a split stopped after its directory put."""
        out = []
        for kind, key, value in event.puts:
            rkey = [kind, *key]
            self._look(self.R, rkey)
            stored = {**value, "_seq": event.seq}
            out.append((self.R, rkey, lambda cur, v=stored: v))
        for ix, edits in sorted(index_edits(event, priors).items(), key=_ix_order):
            out += self._index_plan(ix, edits, redo=redo)
        return out

    def _index_plan(self, ix, edits, *, redo=False):
        if ix[0] == "journal":
            by = collections.defaultdict(dict)
            for c, (seq, inside) in edits.items():
                by[seq // JOURNAL_CHUNK][c] = (seq, inside)
            out = []
            for n, e in sorted(by.items()):
                key = ix_key(ix, n)
                self._look(self.I, key)
                out.append((self.I, key, _chunk_fn((None, None), e)))
            return out
        dkey = ix_key(ix, "dir")
        drow = self._look(self.I, dkey)
        directory = [] if drow[0] in (ABSENT, None) else drow[0]
        rng = ranges(directory)
        by = collections.defaultdict(dict)
        for c, (k, inside) in edits.items():
            by[chunk_of(k, directory)][c] = (k, inside)
        plain, splits = [], []
        for n, e in sorted(by.items(), key=lambda p: rng[p[0]][0] or []):
            key = ix_key(ix, n)
            row = self._look(self.I, key)
            keys = edited(filtered([] if row[0] is ABSENT else row[0], rng[n]), e)
            if over(keys):
                splits.append((n, e, pieces(keys)))
            else:
                plain.append((self.I, key, _chunk_fn(rng[n], e)))
        if redo:
            # a split's leftover: the old chunk, past the directory put
            others = sorted(n for n in rng if n not in by)
            rows = self._look_many(self.I, [ix_key(ix, n) for n in others])
            for n, row in zip(others, rows):
                if row[0] not in (ABSENT, None) and row[0] != filtered(row[0], rng[n]):
                    plain.append((self.I, ix_key(ix, n), _chunk_fn(rng[n], {})))
        if not splits:
            return plain
        top = max([0] + [n for n, _ in directory])
        news, listed, shrunk = [], [], []
        for n, e, cut in splits:
            for piece in cut[1:]:
                top += 1
                key = ix_key(ix, top)
                self._look(self.I, key)
                news.append((self.I, key, lambda cur, p=piece: SKIP if cur == p else p))
                listed.append([top, piece[0]])
        after = ranges(directory + listed)
        for n, e, cut in splits:
            shrunk.append((self.I, ix_key(ix, n), _chunk_fn(after[n], e)))
        dfn = _dir_fn(listed)
        return news + [(self.I, dkey, dfn)] + shrunk + plain

    def _state_up(self, seq):
        """`applied` or `head`, only upward: a value at or past `seq` stays."""
        return lambda cur: SKIP if schema.is_int(cur) and cur >= seq else seq

    def _store(self, event, priors):
        self._live()
        if event.seq != self._high + 1:
            raise Unavailable(f"seq {event.seq} is not next after {self._high}")
        entry = {"event": event.to_dict(), "priors": priors}
        try:
            # decide's reads: every object steps 3 and 4 will put
            plan = self._plan(event, priors)
            for key in (["applied"], ["head"]):
                self._look(self.S, key)
        except _StoreDown as e:
            raise Unavailable(f"the store at {self._where()}: {e}") from e
        self._commit_put(event.seq, entry)
        self._apply(event.seq, plan, "op")

    def _commit_put(self, seq, entry):
        """Step 2: `put(journal, [seq], entry, absent)`, the commit point."""
        version = self._settle(seq, entry, "op")
        if version is None:
            # another writer took this seq: recover, and it did not happen
            self._recover()
            raise Unavailable(
                f"the record {self.name}: seq {seq} was taken by another "
                "writer; the operation did not happen"
            )
        self._high = seq
        self._epoch += 1
        self._keep(self.J, [seq], entry, version, True)

    def _settle(self, seq, entry, why, version=None):
        """Put journal/[seq] = `entry` until the store answers the put to
        this writer: `absent`, or with `version` (as read). On a `Conflict`, a
        fresh read: absent, the put `absent` again; this entry (a put of it
        the store applied but did not answer, or one read), the same entry
        again with the version read; another writer's entry, None. Each try
        after a pause, until the record's timeout. The answer's version."""
        tries = _Tries(time.monotonic() + self.timeout_s)
        expect = {"absent": True} if version is None else {"version": version}
        while True:
            try:
                return self._call("put", self.J, [seq], entry, expect=expect)
            except _Conflict as e:
                row = self._again(self.J, [seq], tries.deadline, why)
                if row[0] is ABSENT:
                    expect = {"absent": True}
                elif same_entry(row[0], entry):
                    expect = {"version": row[1]}
                else:
                    return None
                failed = e
            except _StoreDown as e:
                failed = e
            if tries.over(failed):
                self._fail(why, f"the commit put was not answered: {failed}", failed)
            tries.pause()

    def _apply(self, seq, plan, why):
        """Steps 3 and 4: the plan's puts, `applied`, one sync, `head`."""
        for space, key, fn in plan:
            self._put(space, key, fn, why=why)
        self._put(self.S, ["applied"], self._state_up(seq), why=why)
        self._sync(why)
        self._put(self.S, ["head"], self._state_up(seq), why=why)

    def _recover(self):
        """At open, and after a commit put met another writer's entry: read
        `applied` (a), then journal/[a+1] and journal/[a+2]. None: bring `head`
        up to a. One: redo it whole. Two: another writer is live, and nothing
        is redone."""
        self._forget()
        try:
            applied = self._fetch(self.S, ["applied"])
            self._fetch(self.S, ["head"])
            a = applied[0] if schema.is_int(applied[0]) else 0
            one = self._fetch(self.J, [a + 1])
            two = self._fetch(self.J, [a + 2])
        except _StoreDown as e:
            raise Unavailable(f"the record {self.name}: recovery: {e}") from e
        if two[0] is not ABSENT:
            self._closed = f"recovery found journal/[{a + 1}] and [{a + 2}]"
            self._forget()
            self._shut()
            raise Unavailable(
                f"the record {self.name}: two entries past applied {a}: another "
                "writer is live"
            )
        self._high = a
        if one[0] is ABSENT:
            # the read of journal/[a+1] was the check: head up to a. A head
            # read at or past a stays as read: the next commit puts head
            self._epoch += 1
            self._put(self.S, ["head"], self._state_up(a), why="recover", settle=False)
            return
        try:
            event = ev.Event.from_dict(one[0]["event"])
            priors = one[0]["priors"]
        except (KeyError, TypeError, ValueError) as e:
            raise Unavailable(f"journal/[{a + 1}] is not an entry: {e}") from e
        self._high = a + 1
        # the entry was read, not answered to this writer: put again with
        # the version read, before the redo goes on past it
        if self._settle(a + 1, one[0], "recover", version=one[1]) is None:
            self._fail(
                "recover",
                f"journal/[{a + 1}] changed under recovery",
                _Found(f"journal/[{a + 1}] changed"),
            )
        # fresh reads of every object the redo puts; its first put checks
        # journal/[a+2] after them
        self._forget()
        try:
            plan = self._plan(event, priors, redo=True)
            for key in (["applied"], ["head"]):
                self._fetch(self.S, key)
        except _StoreDown as e:
            raise Unavailable(f"the record {self.name}: recovery: {e}") from e
        self._apply(a + 1, plan, "recover")

    # -- administration -------------------------------------------------------

    @property
    def initialised(self):
        return self._ready

    @property
    def durable(self):
        return self._report.get("power_safe") is True and (
            self._report.get("ordered") is True
        )

    @property
    def report(self):
        """The store's report, as last read."""
        return dict(self._report)

    def init(self):
        if self.readonly:
            raise Refused("the record is open read-only")
        with self._lock:
            self._live()
            try:
                if self._fetch(self.S, ["schema"])[0] is not ABSENT:
                    raise Refused(f"a record {self.name} exists in this store")
                raced = []

                def made(value):
                    def fn(cur):
                        if cur is ABSENT:
                            return value
                        raced.append(cur)
                        return SKIP

                    return fn

                for key, value in (
                    ("applied", 0),
                    ("head", 0),
                    ("schema", {"version": RECORD_SCHEMA_VERSION}),
                ):
                    # row 8: absent, after journal/[1] is checked
                    self._put(self.S, [key], made(value), why="init", fresh=True)
                if raced:
                    # another init made them first: a check that finds its
                    # journal/[1] stops this one
                    try:
                        self._check_journal()
                    except _Found as e:
                        self._fail("init", f"{e}: another writer is live", e)
            except _StoreDown as e:
                raise Unavailable(f"the store at {self._where()}: {e}") from e
            self._ready = True
            self._high = 0

    def backup(self, dir):
        folder = Path(dir)
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        path = folder / f"record-{self.name}-{stamp}.json"
        text = json.dumps(self.dump(), sort_keys=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as out:
            out.write(text)
        return path

    def close(self):
        with self._lock:
            if self._closed is None:
                self._closed = "closed"
            self._shut()

    def _shut(self):
        """Close the client, then release the lock."""
        store, self.store = self.store, _Closed()
        try:
            if not isinstance(store, _Closed):
                store.close()
        except Exception:
            pass
        fd, self._lockfd = self._lockfd, None
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)

    @contextlib.contextmanager
    def _write(self):
        self._live()
        if self._loading is not None:
            raise Refused(
                f"a load into {self.name} is in progress: load the same dump to "
                "finish it"
            )
        if self._recheck:
            self._reread()
        try:
            yield
        except _StoreDown as e:
            raise Unavailable(f"the store at {self._where()}: {e}") from e

    def _reread(self):
        """After an `Unavailable`, the report is read again before the next
        operation. A shared record whose report is no longer power-safe and
        ordered refuses the operation."""
        try:
            self._read_report()
        except Unavailable:
            self._recheck = True
            raise
        if self.shared and not self.durable:
            self._recheck = True
            raise Unavailable(
                f"the store's report is no longer power-safe and ordered: "
                f"{self._report}"
            )

    def load(self, dump):
        """Load a dump into an empty record, resumably: `loading` is put first,
        with the dump's digest; each event goes through the commit protocol;
        `loading` is put to null at the end. A load stopped part-way goes on
        from `head() + 1` when given the same dump, and refuses any other."""
        self._writable()
        journal = contract.check_dump(dump)
        digest = hashlib.sha256(schema.canonical(dump).encode("utf-8")).hexdigest()
        kept = {k for k, v in RECORDS.items() if not v.journaled}
        with self._lock:
            self._not_reading()
            self._live()
            if self._recheck:
                self._reread()
            try:
                if self._loading is None:
                    if not self._empty():
                        raise Refused("a load goes into an empty record")
                    loading = {"digest": digest, "events": len(journal)}

                    def start(cur, v=loading):
                        if cur is ABSENT or cur is None:
                            return v
                        if isinstance(cur, dict) and cur.get("digest") == digest:
                            return SKIP
                        raise _Found("another load started")

                    # row 9: absent, or a fresh read of a null
                    self._put(self.S, ["loading"], start, why="load", fresh=True)
                    self._loading = loading
                elif self._loading.get("digest") != digest:
                    raise Refused(
                        f"a load of another dump ({self._loading.get('digest')}) "
                        "is in progress: give that dump to finish it"
                    )
                for event in journal[self._high :]:
                    priors = contract._priors(self._get, event)
                    self._check_entry(event, priors)
                    self._store(event, priors)
                for kind in sorted(kept):
                    for row in dump["bookkeeping"].get(kind, []):
                        self._bookkeep(kind, schema.validate(kind, row), sync=False)
                for kind, v in RECORDS.items():
                    if v.journaled and self._read(kind, {}, None, False, None) != dump[
                        "records"
                    ].get(kind, []):
                        raise Refused(
                            f"a dump's {kind} are not the fold of its journal"
                        )
                # row 11: null, never deleted
                self._put(
                    self.S,
                    ["loading"],
                    lambda cur: SKIP if cur is None else None,
                    why="load",
                )
                self._loading = None
            except _Found as e:
                self._fail("load", str(e), e)
            except _StoreDown as e:
                raise Unavailable(f"the store at {self._where()}: {e}") from e
        self._wake()

    # -- the store's primitives -----------------------------------------------

    def _get(self, kind, key):
        def get():
            row = self._look(self.R, [kind, *key])
            if row[0] is ABSENT:
                return None
            out = json.loads(json.dumps(row[0]))
            out.pop("_seq", None)
            return out

        return self._read_store(get)

    def _index_keys(self, ix):
        """Every key of index `ix`, in key order: its directory, then its
        chunks, each filtered to its range."""
        drow = self._look(self.I, ix_key(ix, "dir"))
        directory = [] if drow[0] in (ABSENT, None) else drow[0]
        rng = ranges(directory)
        order = sorted(rng, key=lambda n: (rng[n][0] is not None, rng[n][0] or []))
        rows = self._look_many(self.I, [ix_key(ix, n) for n in order])
        out = []
        for n, row in zip(order, rows):
            if row[0] is not ABSENT:
                out += filtered(row[0], rng[n])
        return out

    def _records(self, kind, keys):
        rows = self._look_many(self.R, [[kind, *k] for k in keys])
        out = []
        for row in rows:
            if row[0] is ABSENT:
                continue
            record = json.loads(json.dumps(row[0]))
            record.pop("_seq", None)
            out.append(record)
        return out

    def _read(self, kind, match, live, desc, limit):
        def read():
            if match:
                field = next(iter(match))
                ix = (kind, field, match[field])
            else:
                ix = (kind, "*", None)
            keys = self._index_keys(ix)
            rows = [
                r
                for r in self._records(kind, keys)
                if contract._matches(kind, r, match, live)
            ]
            rows.sort(key=lambda r: schema.key_of(kind, r), reverse=desc)
            return rows if limit is None else rows[:limit]

        return self._read_store(read)

    def _head(self):
        return self._high

    def _events(self, cursor, limit, subject):
        def events():
            top = self._high
            if subject is None:
                last = top if limit is None else min(top, cursor + limit)
                seqs = list(range(cursor + 1, last + 1))
            else:
                ix = ("journal", subject, None)
                chunks = list(range(cursor // JOURNAL_CHUNK, top // JOURNAL_CHUNK + 1))
                rows = self._look_many(self.I, [ix_key(ix, n) for n in chunks])
                seqs = sorted(
                    {
                        s
                        for row in rows
                        if row[0] is not ABSENT
                        for s in row[0]
                        if cursor < s <= top
                    }
                )
                if limit is not None:
                    seqs = seqs[:limit]
            out = []
            rows = self._look_many(self.J, [[s] for s in seqs], keep=False)
            for seq, row in zip(seqs, rows):
                if row[0] is ABSENT:
                    continue
                out.append(ev.Event.from_dict(row[0]["event"]))
            return out

        return self._read_store(events)

    def _changed(self):
        return self._high

    def _bookkeep(self, kind, row, *, sync=True):
        """A cursor or a beat, no event: its `*` index first, then the record,
        each through the guard (an own put, or a fresh read and a check), then
        one sync, as SQLite's commit is, so a cursor never steps back across a
        power cut. A cursor takes the larger seq, a beat the later time. A
        load's rows need no sync of their own: an unsynced one is lost only
        with the load's end, and the next open goes on with the same dump."""
        key = schema.key_of(kind, row)
        rkey = [kind, *key]
        cur = self._kept.get((self.R, _ckey(rkey)))
        if cur is None or not cur[3]:
            cur = self._fetch(self.R, rkey)
        if cur[0] is ABSENT:
            ix = (kind, "*", None)
            edits = {_ckey(key): (key, True)}
            for space, k, fn in self._index_plan(ix, edits):
                self._put(space, k, fn, why="bookkeep", fresh=True)

        def merge(cur):
            if cur is ABSENT:
                return dict(row)
            if kind == "cursors":
                return {**row, "seq": max(cur["seq"], row["seq"])}
            if kind == "beats":
                return {**row, "at": max(cur["at"], row["at"])}
            return dict(row)

        self._put(self.R, rkey, merge, why="bookkeep", fresh=True)
        if sync:
            self._sync("bookkeep")

    @contextlib.contextmanager
    def _snapshot(self):
        # one writer, and every read and operation under the record's lock
        yield

    def _empty(self):
        if self._high:
            return False
        keys = []
        for kind in RECORDS:
            ix = (kind, "*", None)
            keys += [ix_key(ix, "dir"), ix_key(ix, 0)]
        return all(
            row[0] in (ABSENT, None, []) for row in self._look_many(self.I, keys)
        )

    def _check_store(self):
        """The Henge findings: a journal hole below `head`, `applied` behind
        `head`, a record whose `_seq` is past `applied`, and an index chunk
        that disagrees with the records."""

        def check():
            findings = []
            self._forget()
            top = self._high
            seqs = list(range(1, top + 1))
            rows = self._fetch_many(self.J, [[s] for s in seqs], keep=False)
            entries = {}
            for s, row in zip(seqs, rows):
                if row[0] is ABSENT:
                    findings.append(f"henge: journal/[{s}] is absent below head {top}")
                else:
                    entries[s] = row[0]
            state = {
                k: self._fetch(self.S, [k])[0] for k in ("applied", "head", "schema")
            }
            if state["applied"] != state["head"]:
                findings.append(
                    f"henge: applied {state['applied']} and head {state['head']}"
                )
            if state["schema"] != {"version": RECORD_SCHEMA_VERSION}:
                findings.append(f"henge: schema {state['schema']!r}")
            applied = state["applied"] if schema.is_int(state["applied"]) else 0
            values = collections.defaultdict(set)
            written = collections.defaultdict(set)
            subjects = collections.defaultdict(list)
            for s, entry in sorted(entries.items()):
                event = entry.get("event", {})
                if event.get("subject") is not None:
                    subjects[event["subject"]].append(s)
                for kind, key, value in event.get("puts", []):
                    written[kind].add(_ckey(list(key)))
                    for f in RECORDS[kind].match:
                        values[(kind, f)].add(_ckey(value.get(f)))
            for kind, k in RECORDS.items():
                keys = self._index_keys((kind, "*", None))
                raw = self._fetch_many(self.R, [[kind, *key] for key in keys])
                records = {}
                for key, row in zip(keys, raw):
                    if row[0] is ABSENT:
                        if k.journaled:
                            findings.append(
                                f"henge: {kind} {key} is indexed and absent"
                            )
                        continue
                    if k.journaled and row[0].get("_seq", 0) > applied:
                        findings.append(
                            f"henge: {kind} {key} was written by seq "
                            f"{row[0].get('_seq')}, past applied {applied}"
                        )
                    records[_ckey(key)] = row[0]
                if not k.journaled:
                    continue
                for c in sorted(written[kind] - {_ckey(key) for key in keys}):
                    findings.append(
                        f"henge: {kind} {json.loads(c)} is not in its kind's index"
                    )
                for f in k.match:
                    seen = set(values[(kind, f)])
                    seen |= {_ckey(r[f]) for r in records.values()}
                    for c in sorted(seen):
                        value = json.loads(c)
                        want = sorted(
                            schema.key_of(kind, r)
                            for r in records.values()
                            if _same(r[f], value)
                        )
                        have = self._index_keys((kind, f, value))
                        if have != want:
                            findings.append(
                                f"henge: the index of {kind}.{f} = {value!r} "
                                f"disagrees with the records: {have} for {want}"
                            )
            for subject, want in sorted(subjects.items()):
                ix = ("journal", subject, None)
                chunks = range(0, top // JOURNAL_CHUNK + 1)
                rows = self._fetch_many(self.I, [ix_key(ix, n) for n in chunks])
                have = sorted(s for row in rows if row[0] is not ABSENT for s in row[0])
                if have != want:
                    findings.append(
                        f"henge: the journal's index of {subject} disagrees: "
                        f"{have} for {want}"
                    )
            return findings

        return self._read_store(check)


def _ix_order(pair):
    kind, field, value = pair[0]
    return (kind, field, _ckey(value))


def _chunk_fn(rng, edits):
    """A chunk's put: the chunk as read, filtered to `rng`, with the edits
    of the keys in `rng` (a split's other keys are its new chunks')."""
    mine = {c: (k, inside) for c, (k, inside) in edits.items() if in_range(k, rng)}

    def fn(cur):
        keys = edited(filtered([] if cur in (ABSENT, None) else cur, rng), mine)
        return SKIP if cur == keys else keys

    return fn


def _dir_fn(listed):
    def fn(cur):
        have = [] if cur in (ABSENT, None) else [list(e) for e in cur]
        numbers = {n for n, _ in have}
        new = have + [e for e in listed if e[0] not in numbers]
        new.sort(key=lambda e: e[1])
        return SKIP if new == cur else new

    return fn


class _Closed:
    """A closed record's store: every call is refused."""

    endpoint = None

    def __getattr__(self, name):
        def refused(*a, **k):
            raise _StoreDown("the record is closed")

        return refused


def _connect(address, timeout_s):
    return Store.connect(
        address, timeout_ms=TRY_MS, tries=max(1, int(timeout_s * 1000 // TRY_MS))
    )


def _holder(path):
    try:
        text = path.read_text().strip()
    except OSError:
        text = ""
    return text or "unknown"


def open_henge(
    folder,
    name,
    *,
    readonly=False,
    shared=False,
    actor,
    via,
    authorize=None,
    timeout_s=TIMEOUT_S,
):
    """Open the record `name` in the Henge store running in `folder`: take its
    writer lock, `<folder>/quarrylith.<name>.lock` (refused, naming the
    holder's pid, while another open file holds it), connect with
    `Store.connect("ipc://<folder>/henge.sock")`, read the store's report, and
    run recovery. A `readonly` open takes the same lock. `timeout_s` is how
    long the record waits for the store (tests shorten it)."""
    folder = Path(folder).absolute()
    if not NAME_RE.fullmatch(name or ""):
        raise Refused(f"a record's name is [a-z0-9][a-z0-9_-]*, not {name!r}")
    if not folder.is_dir():
        raise Refused(f"no store folder {folder}")
    sock = folder / "henge.sock"
    if not sock.exists():
        raise Unavailable(f"no store is running in {folder}: no {sock.name}")
    path = folder / f"quarrylith.{name}.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise Refused(
            f"the record {name} is held by pid {_holder(path)} ({path})"
        ) from None
    try:
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"{os.getpid()}\n".encode(), 0)
        try:
            store = _connect(f"ipc://{sock}", timeout_s)
        except Exception as e:
            raise Unavailable(f"cannot connect to the store in {folder}: {e}") from e
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(fd)
        raise
    # the record owns the store and the lock from here: a failed open
    # closes both
    return HengeRecord(
        store,
        name,
        lock=fd,
        readonly=readonly,
        shared=shared,
        actor=actor,
        via=via,
        authorize=authorize,
        timeout_s=timeout_s,
    )


class HengeFollower:
    """The record's events from another process: it watches `.state
    ["head"]` (at least once, with a catch-up read after a quiet second) and
    reads `.journal` from its cursor up to `head` with `get_many`. It takes no
    lock and writes nothing; its cursor is its own."""

    def __init__(self, folder, name, *, idle_s=FOLLOW_IDLE_S):
        folder = Path(folder).absolute()
        if not NAME_RE.fullmatch(name or ""):
            raise Refused(f"a record's name is [a-z0-9][a-z0-9_-]*, not {name!r}")
        self.name = name
        sp = spaces(name)
        self.J, self.S = sp["journal"], sp["state"]
        address = f"ipc://{folder}/henge.sock"
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name=f"follow-{name}", daemon=True
        )
        self._thread.start()
        self._store = self._run(self._make(address, f"ipc://{folder}/watch.sock"))
        self._woken = threading.Condition()
        self._notes = 0
        self._watching = asyncio.run_coroutine_threadsafe(
            self._watch(idle_s), self._loop
        )

    @staticmethod
    async def _make(address, watch):
        return AsyncStore(address, role="henge-local", plain=True, watch_endpoint=watch)

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    async def _watch(self, idle_s):
        while True:
            try:
                async for _ in self._store.watch(self.S, ["head"], idle_s=idle_s):
                    with self._woken:
                        self._notes += 1
                        self._woken.notify_all()
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(0.2)

    def head(self):
        try:
            item = self._run(self._store.get(self.S, ["head"]))
        except HengeError as e:
            raise Unavailable(f"the store: {e}") from e
        return item.value if item is not None and schema.is_int(item.value) else 0

    def events(self, cursor, *, limit=500, wait=0.0):
        """The events after `cursor`, in seq order, at most `limit`, up to
        `head`; with `wait`, up to that many seconds for the first one."""
        if not schema.is_int(cursor) or cursor < 0:
            raise Refused(f"a cursor is a seq, from 0, not {cursor!r}")
        end = time.monotonic() + wait
        while True:
            with self._woken:
                seen = self._notes
            top = self.head()
            if top > cursor:
                seqs = list(range(cursor + 1, min(top, cursor + limit) + 1))
                out = []
                for i in range(0, len(seqs), MANY):
                    batch = [[s] for s in seqs[i : i + MANY]]
                    try:
                        items = self._run(self._store.get_many(self.J, batch))
                    except HengeError as e:
                        raise Unavailable(f"the store: {e}") from e
                    for item in items:
                        if item is None:
                            return out
                        out.append(ev.Event.from_dict(item.value["event"]))
                return out
            left = end - time.monotonic()
            if left <= 0:
                return []
            with self._woken:
                if self._notes == seen:
                    self._woken.wait(left)

    def close(self):
        self._watching.cancel()
        with contextlib.suppress(Exception):
            self._run(self._store.close())
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)


contract.BACKENDS["henge"] = HengeRecord
