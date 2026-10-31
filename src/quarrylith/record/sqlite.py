# SPDX-License-Identifier: Apache-2.0
"""The SQLite backend: one file, one STRICT table per kind of record, and the
journal, written by many processes.

An operation is one `BEGIN IMMEDIATE` transaction: it reads, decides the event,
inserts the journal row (its seq is the row's) and writes each put, then
commits. An error from the `COMMIT` itself is `Unknown`; every earlier error
rolls back and is `Unavailable`. The file runs in WAL with `synchronous =
FULL`, and on macOS `fullfsync` and `checkpoint_fullfsync` too, so a commit is
power-safe when the caller hears of it.

The backstops sit in the store: a `BEFORE DELETE` trigger on every record
table (no record is deleted), partial unique indexes for the uniquenesses the
records name, and an append-only journal. Every put is `INSERT ... ON CONFLICT
DO UPDATE`, never `INSERT OR REPLACE`, whose implicit delete the triggers would
refuse. The schema version is the file's own `user_version`, not a record.
"""

import contextlib
import datetime
import json
import os
import sqlite3
import sys
import urllib.parse
from pathlib import Path

from . import contract, schema
from . import events as ev
from .contract import Record, Refused, Unavailable, Unknown
from .schema import BOOL, INT, JSON, RECORDS, STR

RECORD_SCHEMA_VERSION = 1
SQLITE_MIN = (3, 37, 0)
BUSY_MS = 10_000
MACOS = sys.platform == "darwin"

SQL_TYPES = {INT: "INTEGER", STR: "TEXT", BOOL: "INTEGER", JSON: "TEXT"}

#: The uniquenesses the records name: (index, kind, fields, while).
UNIQUES = (
    ("leases_one_live", "leases", ("number",), "released_at IS NULL"),
    (
        "changes_one_open",
        "changes",
        ("repo", "branch"),
        "merged_at IS NULL AND dropped_at IS NULL",
    ),
)

JOURNAL_SQL = (
    "CREATE TABLE journal (\n"
    "  seq INTEGER PRIMARY KEY AUTOINCREMENT,\n"
    "  at INTEGER NOT NULL,\n"
    "  actor TEXT NOT NULL,\n"
    "  via TEXT NOT NULL,\n"
    "  kind TEXT NOT NULL,\n"
    "  subject TEXT,\n"
    "  data TEXT NOT NULL CHECK (json_valid(data)),\n"
    "  puts TEXT NOT NULL CHECK (json_valid(puts))\n"
    ") STRICT",
    "CREATE INDEX journal_by_subject ON journal(subject, seq)",
    "CREATE TRIGGER journal_no_update BEFORE UPDATE ON journal"
    " BEGIN SELECT RAISE(ABORT, 'the journal is append-only'); END",
    "CREATE TRIGGER journal_no_delete BEFORE DELETE ON journal"
    " BEGIN SELECT RAISE(ABORT, 'the journal is append-only'); END",
)


def _q(name):
    return '"' + name.replace('"', '""') + '"'


def _column(f):
    col = f"{_q(f.name)} {SQL_TYPES[f.type]}"
    if not f.null:
        col += " NOT NULL"
    if f.type == BOOL:
        col += f" CHECK ({_q(f.name)} IN (0, 1))"
    elif f.type == JSON:
        col += f" CHECK (json_valid({_q(f.name)}))"
    return col


def schema_sql():
    """Every statement that makes an empty record."""
    out = []
    for kind in RECORDS.values():
        cols = [_column(f) for f in kind.all_fields]
        cols.append(f"PRIMARY KEY ({', '.join(_q(n) for n in kind.key_names)})")
        out.append(
            f"CREATE TABLE {_q(kind.name)} (\n  " + ",\n  ".join(cols) + "\n) STRICT"
        )
        for name in kind.match:
            out.append(
                f"CREATE INDEX {_q(f'{kind.name}_by_{name}')} "
                f"ON {_q(kind.name)}({_q(name)})"
            )
        out.append(
            f"CREATE TRIGGER {_q(f'{kind.name}_no_delete')} BEFORE DELETE ON "
            f"{_q(kind.name)} BEGIN SELECT RAISE(ABORT, 'no record is deleted'); END"
        )
    for index, kind, fields, where in UNIQUES:
        cols = ", ".join(_q(f) for f in fields)
        out.append(
            f"CREATE UNIQUE INDEX {_q(index)} ON {_q(kind)}({cols}) WHERE {where}"
        )
    out.extend(JOURNAL_SQL)
    return out


def _encode(f, value):
    if value is None:
        return None
    if f.type == BOOL:
        return int(value)
    if f.type == JSON:
        return schema.canonical(value)
    return value


def _decode(f, value):
    if value is None:
        return None
    if f.type == BOOL:
        return bool(value)
    if f.type == JSON:
        return json.loads(value)
    return value


class _Statements:
    """The SQL of one kind, made once."""

    def __init__(self, kind):
        self.kind = kind
        self.fields = kind.all_fields
        names = [_q(f.name) for f in self.fields]
        keys = [_q(n) for n in kind.key_names]
        table = _q(kind.name)
        self.select = f"SELECT {', '.join(names)} FROM {table}"
        self.by_key = " AND ".join(f"{k} = ?" for k in keys)
        self.order = ", ".join(keys)
        self.order_desc = ", ".join(f"{k} DESC" for k in keys)
        rest = [_q(f.name) for f in kind.fields]
        self.upsert = (
            f"INSERT INTO {table} ({', '.join(names)}) "
            f"VALUES ({', '.join('?' for _ in names)}) "
            f"ON CONFLICT ({', '.join(keys)}) DO "
            + (
                "UPDATE SET " + ", ".join(f"{c} = excluded.{c}" for c in rest)
                if rest
                else "NOTHING"
            )
        )

    def row(self, values):
        return {f.name: _decode(f, v) for f, v in zip(self.fields, values)}

    def values(self, record):
        return [_encode(f, record[f.name]) for f in self.fields]


STATEMENTS = {name: _Statements(kind) for name, kind in RECORDS.items()}


class _View:
    """What `events.apply` folds an event into: the tables."""

    def __init__(self, record):
        self.record = record

    def put(self, kind, key, value):
        sql = STATEMENTS[kind]
        self.record._conn.execute(sql.upsert, sql.values(schema.validate(kind, value)))


class SqliteRecord(Record):
    """The record in one SQLite file."""

    backend = "sqlite"

    def __init__(self, path, conn, **kw):
        super().__init__(**kw)
        self.path = path
        self._conn = conn
        self._ready = self._version_check()

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
        synchronous="FULL",
        fullfsync=True,
    ):
        """Open `sqlite:///path`. `synchronous` and `fullfsync` are for tests:
        a record opened with less reads `durable` false."""
        if not url.startswith("sqlite://") or not url[len("sqlite://") :]:
            raise Refused(f"a SQLite record's URL is sqlite:///path, not {url!r}")
        if sqlite3.sqlite_version_info < SQLITE_MIN:
            raise Refused(
                f"SQLite {sqlite3.sqlite_version} is older than 3.37: the record's "
                "tables are STRICT"
            )
        if synchronous not in ("FULL", "EXTRA", "NORMAL", "OFF"):
            raise Refused(f"no synchronous setting {synchronous!r}")
        # sqlite:///abs and sqlite:////abs name the same absolute path
        where = url[len("sqlite://") :]
        while where.startswith("//"):
            where = where[1:]
        path = Path(where)
        try:
            if readonly:
                if not path.exists():
                    raise Refused(f"no record at {path}")
                conn = sqlite3.connect(
                    f"file:{urllib.parse.quote(str(path))}?mode=ro",
                    uri=True,
                    isolation_level=None,
                    timeout=BUSY_MS / 1000,
                    check_same_thread=False,
                )
            else:
                if not path.parent.is_dir():
                    raise Refused(f"no folder {path.parent} for a record")
                conn = sqlite3.connect(
                    str(path),
                    isolation_level=None,
                    timeout=BUSY_MS / 1000,
                    check_same_thread=False,
                )
            conn.execute(f"PRAGMA busy_timeout = {BUSY_MS}")
            conn.execute(f"PRAGMA synchronous = {synchronous}")
            conn.execute("PRAGMA foreign_keys = ON")
            if MACOS:
                flag = "ON" if fullfsync else "OFF"
                conn.execute(f"PRAGMA fullfsync = {flag}")
                conn.execute(f"PRAGMA checkpoint_fullfsync = {flag}")
            if not readonly:
                conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.Error as e:
            raise Unavailable(f"cannot open {path}: {e}") from e
        try:
            return cls(
                path,
                conn,
                actor=actor,
                via=via,
                authorize=authorize,
                readonly=readonly,
                shared=shared,
            )
        except BaseException:
            conn.close()
            raise

    def _version_check(self):
        """True for a record of this schema, False for an empty file; refused
        for a newer or an older schema, and for a file that is not a record."""
        try:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                r[0]
                for r in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        except sqlite3.Error as e:
            raise Unavailable(f"cannot read {self.path}: {e}") from e
        if version == 0 and not tables:
            return False
        if version > RECORD_SCHEMA_VERSION:
            raise Refused(
                f"the record is schema {version}, newer than this quarrylith's "
                f"{RECORD_SCHEMA_VERSION}: update quarrylith"
            )
        if "journal" not in tables:
            raise Refused(f"{self.path} holds tables but no record")
        if version < RECORD_SCHEMA_VERSION:
            raise Refused(
                f"the record is schema {version}, older than this quarrylith's "
                f"{RECORD_SCHEMA_VERSION}: run `quarrylith record migrate`"
            )
        return True

    # -- administration -------------------------------------------------------

    @property
    def initialised(self):
        return self._ready

    def init(self):
        if self.readonly:
            raise Refused("the record is open read-only")
        with self._lock:
            with self._write():
                if self._version_check():
                    raise Refused(f"a record exists at {self.path}")
                for statement in schema_sql():
                    self._conn.execute(statement)
                self._conn.execute(f"PRAGMA user_version = {RECORD_SCHEMA_VERSION}")
            self._ready = True

    def pragmas(self):
        """The connection's settings, read back."""
        names = (
            "synchronous",
            "journal_mode",
            "foreign_keys",
            "busy_timeout",
            "fullfsync",
            "checkpoint_fullfsync",
        )
        with self._lock:
            return {n: self._conn.execute(f"PRAGMA {n}").fetchone()[0] for n in names}

    @property
    def durable(self):
        got = self.pragmas()
        if got["synchronous"] < 2:  # FULL is 2, EXTRA 3
            return False
        if MACOS:
            return got["fullfsync"] == 1 and got["checkpoint_fullfsync"] == 1
        return True

    def backup(self, dir):
        folder = Path(dir)
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        path = folder / f"record-{stamp}.sqlite"
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        target = sqlite3.connect(str(path))
        try:
            with self._lock:
                self._conn.backup(target)
        except sqlite3.Error as e:
            raise Unavailable(f"cannot back up {self.path}: {e}") from e
        finally:
            target.close()
        return path

    def close(self):
        with self._lock:
            self._conn.close()

    def _check_store(self):
        findings = []
        try:
            rows = [r[0] for r in self._conn.execute("PRAGMA integrity_check")]
            if rows != ["ok"]:
                findings += [f"sqlite: {r}" for r in rows]
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version != RECORD_SCHEMA_VERSION:
                findings.append(f"sqlite: schema {version}")
            for kind in RECORDS:
                for record in self._read(kind, {}, None, False, None):
                    try:
                        schema.validate(kind, record)
                    except schema.SchemaError as e:
                        findings.append(f"{kind}: {e}")
        except sqlite3.Error as e:
            raise Unavailable(f"cannot check {self.path}: {e}") from e
        return findings

    # -- the store's primitives -----------------------------------------------

    def _run(self, sql, args=()):
        try:
            return self._conn.execute(sql, args).fetchall()
        except sqlite3.Error as e:
            raise Unavailable(f"{self.path}: {e}") from e

    def _get(self, kind, key):
        sql = STATEMENTS[kind]
        rows = self._run(f"{sql.select} WHERE {sql.by_key}", key)
        return sql.row(rows[0]) if rows else None

    def _read(self, kind, match, live, desc, limit):
        sql = STATEMENTS[kind]
        by_name = {f.name: f for f in sql.fields}
        where, args = [], []
        for name, value in match.items():
            if value is None:
                where.append(f"{_q(name)} IS NULL")
            else:
                where.append(f"{_q(name)} = ?")
                args.append(_encode(by_name[name], value))
        ended = " AND ".join(f"{_q(f)} IS NULL" for f in RECORDS[kind].live)
        if live is True:
            where.append(f"({ended})")
        elif live is False:
            where.append(f"NOT ({ended})")
        text = sql.select
        if where:
            text += " WHERE " + " AND ".join(where)
        text += " ORDER BY " + (sql.order_desc if desc else sql.order)
        if limit is not None:
            text += " LIMIT ?"
            args.append(limit)
        return [sql.row(r) for r in self._run(text, args)]

    def _head(self):
        return self._run("SELECT IFNULL(MAX(seq), 0) FROM journal")[0][0]

    @contextlib.contextmanager
    def _write(self):
        try:
            self._conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as e:
            raise Unavailable(f"{self.path}: cannot begin: {e}") from e
        try:
            yield
        except BaseException as e:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            if isinstance(e, sqlite3.IntegrityError):
                raise Refused(f"the store refused it: {e}") from e
            if isinstance(e, sqlite3.Error):
                raise Unavailable(f"{self.path}: {e}") from e
            raise
        try:
            self._conn.execute("COMMIT")
        except sqlite3.Error as e:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise Unknown(f"{self.path}: the commit failed: {e}") from e

    def _store(self, event, priors):
        if event.seq != self._head() + 1:
            raise Unavailable(f"seq {event.seq} is not next after {self._head()}")
        try:
            self._conn.execute(
                "INSERT INTO journal (seq, at, actor, via, kind, subject, data, puts)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.seq,
                    event.at,
                    event.actor,
                    event.via,
                    event.kind,
                    event.subject,
                    schema.canonical(event.data),
                    schema.canonical([[k, key, v] for k, key, v in event.puts]),
                ),
            )
            ev.apply(_View(self), event)
        except sqlite3.IntegrityError:
            raise
        except sqlite3.Error as e:
            raise Unavailable(f"{self.path}: {e}") from e

    def _events(self, cursor, limit, subject):
        text = (
            "SELECT seq, at, actor, via, kind, subject, data, puts FROM journal"
            " WHERE seq > ?"
        )
        args = [cursor]
        if subject is not None:
            text += " AND subject = ?"
            args.append(subject)
        text += " ORDER BY seq"
        if limit is not None:
            text += " LIMIT ?"
            args.append(limit)
        return [
            ev.Event(
                seq=seq,
                at=at,
                actor=actor,
                via=via,
                kind=kind,
                subject=subj,
                data=json.loads(data),
                puts=tuple((k, key, v) for k, key, v in json.loads(puts)),
            )
            for seq, at, actor, via, kind, subj, data, puts in self._run(text, args)
        ]

    def _changed(self):
        return self._run("PRAGMA data_version")[0][0]

    def _bookkeep(self, kind, row):
        sql = STATEMENTS[kind]
        row = schema.validate(kind, row)
        upsert = sql.upsert
        if kind == "cursors":
            upsert = upsert.replace(
                '"seq" = excluded."seq"', '"seq" = MAX("seq", excluded."seq")'
            )
        self._conn.execute(upsert, sql.values(row))

    @contextlib.contextmanager
    def _snapshot(self):
        try:
            self._conn.execute("BEGIN")
            self._conn.execute("SELECT 1 FROM journal LIMIT 1").fetchall()
        except sqlite3.Error as e:
            raise Unavailable(f"{self.path}: {e}") from e
        try:
            yield
        finally:
            try:
                self._conn.execute("COMMIT")
            except sqlite3.Error:
                pass

    def _empty(self):
        for kind in RECORDS:
            if self._run(f"SELECT 1 FROM {_q(kind)} LIMIT 1"):
                return False
        return not self._run("SELECT 1 FROM journal LIMIT 1")


contract.BACKENDS["sqlite"] = SqliteRecord
