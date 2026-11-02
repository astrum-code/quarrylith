# SPDX-License-Identifier: Apache-2.0
"""`quarrylith record import` and the sample import document (templet:3159,
build step 4), over every backend in BACKENDS."""

import json

import pytest
from backends import over_backends
from samples import SAMPLE, load_sample

from quarrylith import cli
from quarrylith.record import schema
from quarrylith.record.contract import open_record
from quarrylith.record.importing import OWN_KINDS, ImportRefused, check_import
from quarrylith.record.schema import RECORDS


def run(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def report(out):
    """The import's report: {table: (read, pulled, left)} and {column:
    count}."""
    tables, columns = {}, {}
    for line in out.splitlines():
        words = line.split()
        if len(words) == 4 and words[0] != "table":
            tables[words[0]] = tuple(int(w) for w in words[1:])
        elif len(words) == 3 and words[0] == "dropped":
            columns[words[1]] = int(words[2])
    return tables, columns


def held(doc):
    """What the document holds per legacy table, counted from its own rows:
    {table: (read, pulled, left)} and the dropped columns."""
    tables, columns = {}, {}
    for table, read in doc["source"]["tables"].items():
        pulled = sum(r["table"] == table for r in doc["pulled"])
        left = sum(
            r["count"]
            for r in doc["left"]
            if r["table"] == table and r.get("column") is None
        )
        tables[table] = (read, pulled, left)
    for r in doc["left"]:
        if r.get("column") is not None:
            columns[f"{r['table']}.{r['column']}"] = r["count"]
    return tables, columns


@over_backends
def test_the_imports_report_counts_equal_the_rows_per_table(url, capsys):
    """templet:3159 -- `quarrylith record import` of the sample document
    reports per legacy table the rows read, pulled and left, which equal the
    rows the document holds per table, pulled plus left making the rows read;
    and the dropped columns. The `imports` record holds the same counts."""
    doc = load_sample()
    code, out, err = run(capsys, "record", "import", str(SAMPLE), "--into", url)
    assert code == 0, err
    tables, columns = report(out)
    want_tables, want_columns = held(doc)
    assert tables == want_tables
    assert (
        columns
        == want_columns
        == {
            "budget.source": 3,
            "comments.source": 6,
            "items.source": 10,
            "runs.pid": 3,
        }
    )
    for table, (read, pulled, left) in tables.items():
        assert pulled + left == read, table
    assert tables["items"] == (10, 10, 0) and tables["journal"] == (120, 0, 120)
    assert tables["meta"] == (4, 3, 1) and tables["scratch"] == (1, 0, 1)
    record = open_record(url, actor="alpha", via="cli", readonly=True)
    try:
        counts = record.get("imports", 1)["counts"]
        assert record.check() == []
        assert len(record.read("items")) == 10
    finally:
        record.close()
    assert {t: v for t, v in counts["pulled"].items()} == {
        t: p for t, (_, p, _) in want_tables.items() if p
    }
    assert {t: v for t, v in counts["left"].items()} == {
        t: lf
        for t, (_, _, lf) in want_tables.items()
        if any(r["table"] == t and r.get("column") is None for r in doc["left"])
    }
    assert counts["columns"] == want_columns


@over_backends
def test_an_import_into_a_record_that_holds_records_is_refused(url, capsys):
    """An import goes into an empty record: into one that holds records it is
    refused, and writes nothing."""
    assert run(capsys, "record", "import", str(SAMPLE), "--into", url)[0] == 0
    record = open_record(url, actor="alpha", via="cli", readonly=True)
    before = record.dump()
    record.close()
    code, out, err = run(capsys, "record", "import", str(SAMPLE), "--into", url)
    assert code == 2 and "empty" in err and out == ""
    record = open_record(url, actor="alpha", via="cli", readonly=True)
    assert record.dump() == before
    record.close()


def test_the_sample_is_whole_and_holds_every_kind_an_import_takes():
    """The sample import document passes `check_import`, and holds every kind
    of record an import carries: none of the kinds the import writes itself,
    and none that only the work of the new record makes (capacity and
    reservations)."""
    doc = load_sample()
    check_import(doc)
    takes = {k for k in RECORDS if k not in OWN_KINDS + ("capacity", "reservations")}
    assert set(doc["records"]) == takes
    assert all(doc["records"][k] for k in takes), [
        k for k in takes if not doc["records"][k]
    ]
    assert doc["format"] == "quarrylith-import/1"
    # the effort document it was read from, and the linking pass's result
    efforts = {e["id"]: e for e in doc["records"]["efforts"]}
    assert efforts["unsorted"] == schema.unsorted_effort(
        efforts["unsorted"]["created_at"]
    )
    links = {int(r["target"]): r["effort"] for r in doc["records"]["effort_links"]}
    assert {n for n, e in links.items() if e == "0001"} == {10, 11, 12, 13, 18}
    # nothing closed and nothing reopened: every legacy state stays
    states = {i["number"]: i["state"] for i in doc["records"]["items"]}
    assert states == {
        5: "done",
        10: "ready",
        11: "in-review",
        12: "done",
        13: "claimed",
        14: "triage",
        15: "dropped",
        16: "done",
        17: "ready",
        18: "triage",
    }


def test_check_import_refuses_an_unsorted_that_is_not_the_records(tmp_path):
    """`unsorted` is the record's own effort: a document whose `unsorted`
    differs from `schema.unsorted_effort` in any field but its time is
    refused, naming the field."""
    doc = load_sample()
    for field, value in (
        ("owner", "lead"),
        ("statement", "x"),
        ("standing", "admitted"),
    ):
        bad = json.loads(json.dumps(doc))
        for effort in bad["records"]["efforts"]:
            if effort["id"] == "unsorted":
                effort[field] = value
        with pytest.raises(ImportRefused, match=f"unsorted.*{field}"):
            check_import(bad)
