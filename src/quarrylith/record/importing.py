# SPDX-License-Identifier: Apache-2.0
"""The import document: what a reader of an older board writes, what
`check_import` refuses, and what `compose` makes of it.

quarrylith never reads the older board itself. A job beside it writes an
import document, plain JSON (`IMPORT_FORMAT`):

    {"format": "quarrylith-import/1",
     "source": {"name", "path", "schema_version", "last_seq", "read_at",
                "tables": {table: rows read}},
     "records": {kind: [record, ...]},      # the records of the new board
     "counters": {"next:items": 3181, "next:change:<repo>": 120},
     "pulled": [{"table", "legacy_key", "record", "record_key"}],
     "left": [{"table", "legacy_key", "count", "why"[, "column"]}]}

Every table of the older board is named in `pulled` or `left`, and per table
the pulled rows plus the left rows' counts equal the rows read: nothing
vanishes unreported. `compose` turns a checked document into a dump, pure: the
same document gives the same dump, the `imports` record first, then the
records in `RECORDS` order, then `pulled` and `left`, then the counters.
"""

import copy

from . import events as ev
from . import schema
from .schema import RECORDS, SchemaError

IMPORT_FORMAT = "quarrylith-import/1"
#: The format of a dump (`dump()` and `load()`).
DUMP_FORMAT = 1

#: The actor of the events a composed import writes.
ACTOR = "import"

PARTS = ("format", "source", "records", "counters", "pulled", "left")
SOURCE_KEYS = ("name", "path", "schema_version", "last_seq", "read_at", "tables")
PULLED_KEYS = ("table", "legacy_key", "record", "record_key")
LEFT_KEYS = ("table", "legacy_key", "count", "why")
#: Kinds the import writes itself, or whose rows are not a document's to carry.
OWN_KINDS = ("meta", "imports", "pulled", "left", "cursors")


class ImportRefused(ValueError):
    """An import document that is not whole; `problems` lists each fault."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("import document refused: " + "; ".join(self.problems))


def _count(value):
    return schema.is_int(value) and 0 <= value <= schema.INT_MAX


def _source(doc, problems):
    src = doc.get("source")
    if not isinstance(src, dict):
        problems.append("source: a dict")
        return None
    for name in src:
        if name not in SOURCE_KEYS:
            problems.append(f"source.{name}: not a part of the source")
    for name in ("name", "path"):
        if not isinstance(src.get(name), str) or not src.get(name):
            problems.append(f"source.{name}: a non-empty str")
    for name in ("schema_version", "last_seq", "read_at"):
        if not _count(src.get(name)):
            problems.append(f"source.{name}: an int, from 0")
    tables = src.get("tables")
    if not isinstance(tables, dict) or not all(
        isinstance(t, str) and _count(n) for t, n in tables.items()
    ):
        problems.append("source.tables: {table: rows read}")
        tables = {}
    return {**copy.deepcopy(src), "tables": dict(tables)}


def _records(doc, problems):
    """The records, validated, each kind in key order."""
    out = {}
    raw = doc.get("records")
    if not isinstance(raw, dict):
        problems.append("records: a dict of kind -> rows")
        return out
    for kind, rows in raw.items():
        if kind not in RECORDS:
            problems.append(f"records.{kind}: no such kind of record")
            continue
        if kind in OWN_KINDS:
            problems.append(f"records.{kind}: the import writes {kind} itself")
            continue
        if not isinstance(rows, list):
            problems.append(f"records.{kind}: a list of records")
            continue
        seen, good = set(), []
        for i, row in enumerate(rows):
            try:
                rec = schema.validate(kind, row)
            except SchemaError as e:
                problems.append(f"records.{kind}[{i}]: {e}")
                continue
            key = tuple(schema.key_of(kind, rec))
            if key in seen:
                problems.append(f"records.{kind}: key {list(key)} twice")
                continue
            seen.add(key)
            good.append(rec)
        keys = schema.sort_keys(schema.key_of(kind, r) for r in good)
        by_key = {tuple(schema.key_of(kind, r)): r for r in good}
        out[kind] = [by_key[tuple(k)] for k in keys]
    return out


def _counters(doc, records, problems):
    raw = doc.get("counters")
    if not isinstance(raw, dict):
        problems.append("counters: a dict of name -> int")
        return {}
    counters = {}
    for name, value in raw.items():
        kind = name[5:] if isinstance(name, str) and name.startswith("next:") else None
        repo = kind[7:] if kind and kind.startswith("change:") else None
        if not (kind in schema.ID_KINDS or repo):
            problems.append(
                f"counters.{name}: not a counter (next:<kind> or next:change:<repo>)"
            )
            continue
        try:
            row = schema.validate("meta", {"key": name, "value": value})
        except SchemaError as e:
            problems.append(f"counters.{name}: {e}")
            continue
        if not _count(value):
            problems.append(f"counters.{name}: an int, from 0")
        else:
            counters[name] = row["value"]
    # a counter is past the highest id its records hold, or the next record
    # filed would take an id twice
    reach = {}
    for kind in schema.ID_KINDS:
        top = 0
        for rec in records.get(kind, ()):
            ident = rec[RECORDS[kind].key_names[0]]
            if kind == "efforts":
                ident = int(ident) if ident.isascii() and ident.isdigit() else 0
            top = max(top, ident)
        if top:
            reach[schema.counter_name(kind)] = top
    for rec in records.get("changes", ()):
        name = f"next:change:{rec['repo']}"
        reach[name] = max(reach.get(name, 0), rec["n"])
    for name, top in reach.items():
        if name not in raw:
            problems.append(f"counters: {name} missing (the records reach {top})")
        elif name in counters and counters[name] <= top:
            problems.append(
                f"counters.{name}: {counters[name]} is not past the {top} "
                "the records hold"
            )
    return counters


def _legacy_ref(row):
    if row.get("column") is not None:
        return f"column:{row['column']}"
    if row["legacy_key"] is None:
        return "*"
    return schema.canonical(row["legacy_key"])


def _pulled(doc, tables, keys, counters, at, problems):
    raw = doc.get("pulled")
    if not isinstance(raw, list):
        problems.append("pulled: a list")
        return []
    out, seen = [], set()
    for i, row in enumerate(raw):
        if not isinstance(row, dict) or set(row) != set(PULLED_KEYS):
            problems.append(f"pulled[{i}]: exactly {', '.join(PULLED_KEYS)}")
            continue
        table, key = row["table"], row["legacy_key"]
        label = f"pulled {table} {key}"
        if not isinstance(table, str):
            problems.append(f"{label}: the table is a str")
            continue
        if table not in tables:
            problems.append(f"{label}: {table} is not a legacy table of the source")
        if not isinstance(key, list) or not key:
            problems.append(f"{label}: the legacy key is a non-empty list")
            continue
        try:
            schema.check_plain(key, label)
        except SchemaError as e:
            problems.append(str(e))
            continue
        kind, rkey = row["record"], row["record_key"]
        if not isinstance(kind, str):
            problems.append(f"{label}: the record is a kind name")
            continue
        if kind == "meta":
            found = (
                isinstance(rkey, list)
                and len(rkey) == 1
                and isinstance(rkey[0], str)
                and rkey[0] in counters
            )
        elif kind in keys:
            # a key of its kind first: True and 1.0 equal 1, and are no key
            try:
                rkey = schema.check_key(kind, rkey)
                found = tuple(rkey) in keys[kind]
            except SchemaError:
                found = False
        else:
            found = False
        if not found:
            what = "no such kind of record" if kind not in RECORDS else "no record"
            problems.append(f"{label}: {what} {kind} {rkey}")
            continue
        rec = {
            "import": 1,
            "table": table,
            "legacy_ref": _legacy_ref(row),
            "record": kind,
            "record_key": rkey,
            "at": at,
        }
        try:
            rec = schema.validate("pulled", rec)
        except SchemaError as e:
            problems.append(f"{label}: {e}")
            continue
        ident = (table, rec["legacy_ref"])
        if ident in seen:
            problems.append(f"{label}: pulled twice")
            continue
        seen.add(ident)
        out.append(rec)
    return out


def _left(doc, tables, at, problems):
    raw = doc.get("left")
    if not isinstance(raw, list):
        problems.append("left: a list")
        return []
    out, seen = [], set()
    for i, row in enumerate(raw):
        keys = set(row) if isinstance(row, dict) else set()
        if not (set(LEFT_KEYS) <= keys <= set(LEFT_KEYS) | {"column"}):
            problems.append(f"left[{i}]: {', '.join(LEFT_KEYS)} and maybe column")
            continue
        table, key, column = row["table"], row["legacy_key"], row.get("column")
        label = f"left {table} {key if column is None else 'column ' + str(column)}"
        if not isinstance(table, str):
            problems.append(f"{label}: the table is a str")
            continue
        if table not in tables:
            problems.append(f"{label}: {table} is not a legacy table of the source")
        if column is not None and (
            not isinstance(column, str) or not column or key is not None
        ):
            problems.append(f"{label}: a column is a name, and has no legacy key")
            continue
        if key is not None and (not isinstance(key, list) or not key):
            problems.append(f"{label}: the legacy key is null or a non-empty list")
            continue
        try:
            schema.check_plain(key, label)
        except SchemaError as e:
            problems.append(str(e))
            continue
        rec = {
            "import": 1,
            "table": table,
            "legacy_ref": _legacy_ref(row),
            "legacy_key": key,
            "count": row["count"],
            "why": row["why"],
            "at": at,
        }
        try:
            rec = schema.validate("left", rec)
        except SchemaError as e:
            problems.append(f"{label}: {e}")
            continue
        if not rec["why"]:
            problems.append(f"{label}: why is empty")
            continue
        count = rec["count"]
        if count < 0:
            problems.append(f"{label}: a count of {count}, below 0")
            continue
        if key is not None and count != 1:
            problems.append(f"{label}: a row's count is 1, not {count}")
            continue
        if column is not None and count > tables.get(table, 0):
            problems.append(
                f"{label}: a count of {count}, past the "
                f"{tables.get(table, 0)} rows read"
            )
            continue
        ident = (table, rec["legacy_ref"])
        if ident in seen:
            problems.append(f"{label}: left twice")
            continue
        seen.add(ident)
        out.append(rec)
    return out


def _counts(tables, pulled, left, problems):
    """Per table: named in `pulled` or `left`, and the rows add up."""
    n_pulled, n_left, columns = {}, {}, {}
    for rec in pulled:
        n_pulled[rec["table"]] = n_pulled.get(rec["table"], 0) + 1
    for rec in left:
        if rec["legacy_ref"].startswith("column:"):
            name = f"{rec['table']}.{rec['legacy_ref'][7:]}"
            columns[name] = rec["count"]
        else:
            n_left[rec["table"]] = n_left.get(rec["table"], 0) + rec["count"]
    named = {r["table"] for r in pulled} | {r["table"] for r in left}
    for table, rows in tables.items():
        if table not in named:
            problems.append(f"table {table}: in neither pulled nor left")
        elif n_pulled.get(table, 0) + n_left.get(table, 0) != rows:
            problems.append(
                f"table {table}: {rows} rows read, {n_pulled.get(table, 0)} pulled "
                f"and {n_left.get(table, 0)} left"
            )
    both = {(r["table"], r["legacy_ref"]) for r in pulled} & {
        (r["table"], r["legacy_ref"]) for r in left
    }
    for table, ref in sorted(both):
        problems.append(f"table {table}: {ref} is both pulled and left")
    return n_pulled, n_left, columns


def _parse(doc):
    """(problems, parsed) of an import document."""
    problems = []
    if not isinstance(doc, dict):
        return ["the document is a dict (a JSON object)"], None
    if doc.get("format") != IMPORT_FORMAT:
        problems.append(f"format: {doc.get('format')!r}, expected {IMPORT_FORMAT!r}")
    for part in PARTS:
        if part not in doc:
            problems.append(f"{part}: missing")
    for part in doc:
        if part not in PARTS:
            problems.append(f"{part}: not a part of an import document")
    source = _source(doc, problems) if "source" in doc else None
    records = _records(doc, problems) if "records" in doc else {}
    counters = _counters(doc, records, problems) if "counters" in doc else {}
    if source is None or "pulled" not in doc or "left" not in doc:
        return problems, None
    at = source["read_at"] if schema.is_int(source.get("read_at")) else 0
    tables = source["tables"]
    # each kind's keys once, so a pulled row is found without a scan
    keys = {
        kind: {tuple(schema.key_of(kind, r)) for r in rows}
        for kind, rows in records.items()
    }
    pulled = _pulled(doc, tables, keys, counters, at, problems)
    left = _left(doc, tables, at, problems)
    n_pulled, n_left, columns = _counts(tables, pulled, left, problems)

    def order(r):
        return [r["table"], r["legacy_ref"]]

    counts = {
        "pulled": dict(sorted(n_pulled.items())),
        "left": dict(sorted(n_left.items())),
        "columns": dict(sorted(columns.items())),
        "records": {k: len(v) for k, v in records.items() if v},
    }
    imported = {
        "id": 1,
        "source": source,
        "format": IMPORT_FORMAT,
        "started_at": at,
        "finished_at": at,
        "counts": counts,
    }
    try:
        imported = schema.validate("imports", imported)
    except SchemaError as e:
        problems.append(f"imports: {e}")
    return problems, {
        "source": source,
        "records": records,
        "counters": dict(sorted(counters.items())),
        "pulled": sorted(pulled, key=order),
        "left": sorted(left, key=order),
        "imported": imported,
        "at": at,
    }


def check_import(doc):
    """None if `doc` is a whole import document, else ImportRefused listing
    every fault: every record valid against `RECORDS`, every `pulled` row
    naming a record the document holds, every legacy table named in `pulled` or
    `left` with the rows adding up, and the counters past the ids in use."""
    problems, _ = _parse(doc)
    if problems:
        raise ImportRefused(problems)


def _shell(kind, count, at):
    """The journal entry of an import event of `count` rows of `kind`, with
    no puts and no priors yet, at the largest seq: what every entry costs
    before its rows."""
    event = ev.Event(
        seq=schema.INT_MAX,
        at=at,
        actor=ACTOR,
        via="import",
        kind="import",
        subject="import:1",
        data={"kind": kind, "count": count},
        puts=(),
    )
    return {"event": event.to_dict(), "priors": []}


def _groups(kind, rows, at):
    """The rows of one kind as the groups that go into one event each. A load
    goes into an empty record, so each put's prior is `[kind, key, null]`, and
    each group's journal entry, the event and its priors by `schema.size`,
    stays within the entry's limit."""
    limit = schema.LIMITS["entry_bytes"]
    base = schema.size(_shell(kind, len(rows), at))
    groups, group, used = [], [], base
    for row in rows:
        key = schema.key_of(kind, row)
        # the put, its prior, and a comma in each list
        cost = schema.size([kind, key, row]) + schema.size([kind, key, None]) + 2
        if base + cost > limit:
            raise ImportRefused(
                [f"records.{kind} {key}: past the journal entry's limit on its own"]
            )
        if group and used + cost > limit:
            groups.append(group)
            group, used = [], base
        group.append(row)
        used += cost
    if group:
        groups.append(group)
    return groups


def compose(doc):
    """The dump an import document makes: pure, so one document gives one
    dump. The `imports` record, then the records in `RECORDS` order, then
    `pulled` and `left`, each kind in events split to keep every journal entry
    within its limit, then the counters as `meta.set`; each event `via`
    "import" at the source's `read_at`. The beats go to the bookkeeping."""
    problems, parsed = _parse(doc)
    if problems:
        raise ImportRefused(problems)
    at = parsed["at"]
    counters = dict(parsed["counters"])
    counters["next:imports"] = max(counters.get("next:imports", 0), 2)
    journal = []

    def add(kind, subject, data, puts):
        journal.append(
            ev.Event(
                seq=len(journal) + 1,
                at=at,
                actor=ACTOR,
                via="import",
                kind=kind,
                subject=subject,
                data=data,
                puts=tuple((k, schema.key_of(k, v), v) for k, v in puts),
            )
        )

    def carry(kind, rows):
        for group in _groups(kind, rows, at):
            data = {"kind": kind, "count": len(group)}
            add("import", "import:1", data, [(kind, r) for r in group])

    carry("imports", [parsed["imported"]])
    for kind in RECORDS:
        if RECORDS[kind].journaled and parsed["records"].get(kind):
            carry(kind, parsed["records"][kind])
    carry("pulled", parsed["pulled"])
    carry("left", parsed["left"])
    for name, value in sorted(counters.items()):
        row = {"key": name, "value": value}
        add("meta.set", f"meta:{name}", {"key": name}, [("meta", row)])

    view = ev.MemoryView()
    for event in journal:
        ev.apply(view, event)
    return {
        "format": DUMP_FORMAT,
        "records": view.records(),
        "journal": [e.to_dict() for e in journal],
        "bookkeeping": {
            "beats": parsed["records"].get("beats", []),
            "cursors": [],
        },
    }
