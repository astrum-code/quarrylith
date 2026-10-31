# SPDX-License-Identifier: Apache-2.0
"""The SQLite backend (templet:3159, build step 2): its settings, its
backstops, its schema version, and the transaction and the journal across
real separate processes."""

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from backends import PARTS, admitted

from quarrylith.record import contract, schema
from quarrylith.record.contract import Refused, Unavailable, Unknown, open_record
from quarrylith.record.schema import RECORDS
from quarrylith.record.sqlite import RECORD_SCHEMA_VERSION, SqliteRecord

PROCS = Path(__file__).resolve().parent / "procs.py"
MACOS = sys.platform == "darwin"


@pytest.fixture
def backend():
    return "sqlite"


def path_of(url):
    return url[len("sqlite://") :]


def raw(url):
    return sqlite3.connect(path_of(url), isolation_level=None)


def run(*args, timeout=60):
    return subprocess.run(
        [sys.executable, str(PROCS), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_sqlite_is_a_backend():
    """templet:3159 -- BACKENDS names the SQLite backend."""
    assert contract.BACKENDS["sqlite"] is SqliteRecord
    assert RECORD_SCHEMA_VERSION == 1


def test_shared_is_refused_on_an_undurable_record(url):
    """templet:3159 -- open_record(..., shared=True) is refused on a backend
    whose durable is false (SQLite opened synchronous=NORMAL), unless
    allow_undurable."""
    record = open_record(url, actor="alpha", via="cli", synchronous="NORMAL")
    assert record.durable is False and record.shared is False
    record.init()
    record.close()
    with pytest.raises(Refused, match="durable"):
        open_record(url, actor="alpha", via="cli", shared=True, synchronous="NORMAL")
    record = open_record(
        url,
        actor="alpha",
        via="cli",
        shared=True,
        allow_undurable=True,
        synchronous="NORMAL",
    )
    assert record.shared is True and record.durable is False
    record.close()
    record = open_record(url, actor="alpha", via="cli", shared=True)
    assert record.shared is True and record.durable is True
    record.close()


def test_every_connection_reads_back_the_power_safe_pragmas(url, opener):
    """templet:3159 -- every connection reads back synchronous FULL and, on
    macOS, fullfsync and checkpoint_fullfsync on; one opened without them
    reads durable false."""
    first = opener()
    first.init()
    second = opener()
    reader = opener(readonly=True)
    for record in (first, second, reader):
        got = record.pragmas()
        assert got["synchronous"] == 2  # FULL
        assert got["journal_mode"] == "wal"
        assert got["foreign_keys"] == 1
        assert got["busy_timeout"] == 10_000
        if MACOS:
            assert got["fullfsync"] == 1 and got["checkpoint_fullfsync"] == 1
        assert record.durable is True
    normal = opener(synchronous="NORMAL")
    assert normal.pragmas()["synchronous"] == 1 and normal.durable is False
    if MACOS:
        flushless = opener(fullfsync=False)
        assert flushless.pragmas()["fullfsync"] == 0
        assert flushless.durable is False


def test_open_refuses_a_newer_schema_and_names_migrate_for_an_older(url, record):
    """templet:3159 -- SQLite: open_record refuses a newer schema_version and
    names migrate for an older one."""
    record.close()
    conn = raw(url)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    conn.execute("PRAGMA user_version = 2")
    conn.close()
    with pytest.raises(Refused, match="newer"):
        open_record(url, actor="alpha", via="cli")
    conn = raw(url)
    conn.execute("PRAGMA user_version = 0")
    conn.close()
    with pytest.raises(Refused, match="quarrylith record migrate"):
        open_record(url, actor="alpha", via="cli")


def test_the_schema_version_is_not_a_record(record):
    """templet:3159 -- the schema version is the file's, not a `meta`
    record, so an initialised record's dump is empty and meets its replay."""
    dump = record.dump()
    assert all(rows == [] for rows in dump["records"].values())
    assert dump["journal"] == []


def test_a_delete_on_a_record_table_is_aborted(url, record):
    """templet:3159 -- SQLite: a DELETE on a record table is aborted by its
    trigger, and the journal is append-only."""
    admitted(record)
    record.file_item("task", "t", "b", filed_by="alpha", effort="0001")
    record.ack("follower", 1)
    conn = raw(url)
    triggers = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
    }
    held = []
    for kind in RECORDS:
        # a row trigger fires on each row deleted: every table has one
        assert f"{kind}_no_delete" in triggers
        if conn.execute(f'SELECT 1 FROM "{kind}" LIMIT 1').fetchall():
            held.append(kind)
            with pytest.raises(sqlite3.IntegrityError, match="deleted"):
                conn.execute(f'DELETE FROM "{kind}"')
    assert {"meta", "efforts", "effort_links", "items", "item_text", "cursors"} <= set(
        held
    )
    for statement in ("DELETE FROM journal", "UPDATE journal SET actor = 'x'"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(statement)
    conn.close()
    assert record.check() == []
    assert len(record.dump()["journal"]) == 2


def test_the_backstops_refuse_what_the_records_name_unique(url, record):
    """templet:3159 -- SQLite: partial unique indexes keep one live lease per
    item and one open change per branch, and every table is STRICT."""
    conn = raw(url)
    lease = (
        "INSERT INTO leases (id, number, holder, fence, branch, machine,"
        " granted_at, renewed_at) VALUES (?, 1, 'a', ?, 'b', 'm', 1, 1)"
    )
    conn.execute(lease, (1, 1))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(lease, (2, 2))
    change = (
        "INSERT INTO changes (id, repo, n, relation, title, branch, head, draft,"
        " opened_at) VALUES (?, 'widgets', ?, 'closes', 't', 'feat/x', 'h', 0, 1)"
    )
    conn.execute(change, (1, 1))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(change, (2, 2))
    with pytest.raises(sqlite3.Error):
        conn.execute("INSERT INTO item_text (number, body) VALUES ('one', 'b')")
    for (sql,) in conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table'"):
        if "sqlite_sequence" not in sql:
            assert sql.rstrip().endswith("STRICT"), sql
    conn.close()


def test_readonly_refuses_every_operation(opener):
    """templet:3159 -- a readonly open reads and refuses every operation."""
    writer = opener()
    writer.init()
    admitted(writer)
    reader = opener(readonly=True)
    assert reader.get("efforts", "0001")["standing"] == "admitted"
    assert reader.dump() == writer.dump()
    for refuse in (
        lambda: admitted(reader, "0002"),
        lambda: reader.ack("follower", 1),
        lambda: reader.init(),
        lambda: reader.load(writer.dump()),
    ):
        with pytest.raises(Refused, match="read-only"):
            refuse()


def test_an_error_before_the_commit_is_unavailable_and_at_it_unknown(record):
    """templet:3159 -- SQLite: an error from the COMMIT itself is Unknown,
    every earlier error rolls back and is Unavailable, raised from the
    backend's own error, and a uniqueness the records name is Refused."""
    admitted(record)
    conn = record._conn

    class Failing:
        def __init__(self, word):
            self.word = word

        def execute(self, sql, *args):
            if sql.lstrip().upper().startswith(self.word):
                raise sqlite3.OperationalError(f"disk I/O error at {self.word}")
            return conn.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(conn, name)

    head, dump = record.head(), record.dump()
    record._conn = Failing('INSERT INTO "ITEMS"')
    with pytest.raises(Unavailable) as failed:
        record.file_item("task", "t", "b", filed_by="alpha", effort="0001")
    assert not isinstance(failed.value, Unknown)
    assert isinstance(failed.value.__cause__, sqlite3.OperationalError)
    record._conn = conn
    assert record.head() == head and record.dump() == dump
    record._conn = Failing("COMMIT")
    with pytest.raises(Unknown) as unknown:
        record.file_item("task", "t", "b", filed_by="alpha", effort="0001")
    assert isinstance(unknown.value, Unavailable)
    assert isinstance(unknown.value.__cause__, sqlite3.OperationalError)
    record._conn = conn
    assert record.check() == []
    n = record.file_item("task", "t", "b", filed_by="alpha", effort="0001")
    assert record.get("items", n)["title"] == "t"


def test_two_writers_in_separate_processes_never_share_a_number(url, record, tmp_path):
    """templet:3159 -- SQLite: two writers in separate processes at once: the
    journal's seqs rise by one with no gap, no number or id is given twice,
    and the record is the replay of its journal."""
    admitted(record)
    gate = tmp_path / "go"
    procs = [
        subprocess.Popen(
            [sys.executable, str(PROCS), "writer", url, tag, "30", str(gate)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for tag in ("alpha", "beta", "gamma")
    ]
    time.sleep(0.5)
    gate.write_text("go")
    got = []
    for proc in procs:
        out, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err
        got.append(json.loads(out.strip().splitlines()[-1])["numbers"])
    numbers = sorted(n for ns in got for n in ns)
    assert numbers == list(range(1, 91))
    assert all(ns == sorted(ns) for ns in got)
    dump = record.dump()
    assert [e["seq"] for e in dump["journal"]] == list(range(1, 182))
    comments = dump["records"]["comments"]
    assert [c["id"] for c in comments] == list(range(1, 91))
    assert sorted(c["number"] for c in comments) == numbers
    assert record.check() == []


def test_a_reader_in_another_process_wakes_within_a_second(url, record):
    """templet:3159 -- SQLite: a reader in another process waiting on events
    returns within a second of a commit."""
    admitted(record)
    head = record.head()
    proc = subprocess.Popen(
        [sys.executable, str(PROCS), "waiter", url, str(head), "5"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout.readline().strip() == "waiting"
    time.sleep(0.6)
    record.file_item("task", "t", "b", filed_by="alpha", effort="0001")
    committed = time.time()
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    got = json.loads(out.strip().splitlines()[-1])
    assert got["seqs"] == [head + 1]
    assert got["at"] - committed < 1.0


def test_a_writer_killed_mid_transaction_leaves_nothing(url, record):
    """templet:3159 -- SQLite: a writer killed inside its transaction, after
    its journal row and puts, leaves no record, no event and no number taken,
    and the next writer is not held up."""
    admitted(record)
    head, dump = record.head(), record.dump()
    done = run("killed", url)
    assert done.returncode == -9, done.stderr
    assert record.head() == head and record.dump() == dump
    assert record.check() == []
    started = time.monotonic()
    n = record.file_item("task", "after", "b", filed_by="alpha", effort="0001")
    assert time.monotonic() - started < 5
    assert n == 1 and record.head() == head + 1
    fresh = open_record(url, actor="alpha", via="cli")
    assert fresh.check() == []
    fresh.close()


def test_a_backup_is_the_same_record(record, tmp_path):
    """templet:3159 -- backup(dir) writes a consistent copy that opens as the
    same record."""
    admitted(record)
    record.file_item("task", "t", "b", filed_by="alpha", effort="0001")
    path = record.backup(tmp_path / "backups")
    copy = open_record(f"sqlite:///{path}", actor="alpha", via="cli", readonly=True)
    assert copy.dump() == record.dump()
    copy.close()
    assert (Path(path).stat().st_mode & 0o077) == 0


def test_a_put_is_an_upsert_never_a_replace(url, record):
    """templet:3159 -- an edit updates the row in place: the delete triggers
    never fire, and the json and bool columns round-trip."""
    admitted(record)
    record.edit_effort("0001", gates={"pace": 1.5, "ok": True}, title="T")
    effort = record.get("efforts", "0001")
    assert effort["gates"] == {"pace": 1.5, "ok": True}
    n = record.file_item("task", "t", "b", filed_by="alpha", effort="0001")
    assert record.get("items", n)["admitted"] is True
    conn = raw(url)
    stored = conn.execute("SELECT gates FROM efforts").fetchone()[0]
    assert stored == schema.canonical({"pace": 1.5, "ok": True})
    conn.close()
    with pytest.raises(Refused, match="parked|standing"):
        record.set_standing("0001", "retired", "no such standing")
    record.file_effort("0002", "b", "B", "owner-a", PARTS)
