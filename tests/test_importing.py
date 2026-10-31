# SPDX-License-Identifier: Apache-2.0
"""The import document: its check and its composition into a dump
(templet:3159, build step 1)."""

import copy
import json
import random

import pytest
from samples import make_doc, sample

from quarrylith.record import importing, schema
from quarrylith.record.events import EVENT_KINDS, Event, MemoryView, apply
from quarrylith.record.importing import (
    IMPORT_FORMAT,
    ImportRefused,
    check_import,
    compose,
)
from quarrylith.record.schema import RECORDS


def refused(doc, match):
    with pytest.raises(ImportRefused, match=match):
        check_import(doc)


def test_the_format_is_named():
    """templet:3159 -- IMPORT_FORMAT is quarrylith-import/1."""
    assert IMPORT_FORMAT == "quarrylith-import/1"
    assert make_doc()["format"] == IMPORT_FORMAT


def test_a_good_document_passes():
    """templet:3159 -- the sample document, with every table named, passes."""
    assert check_import(make_doc()) is None


def test_check_import_refuses_a_table_that_is_neither_pulled_nor_left():
    """templet:3159 -- a legacy table in neither pulled nor left is refused,
    naming the table."""
    doc = make_doc()
    doc["source"]["tables"]["orphans"] = 4
    refused(doc, "orphans")
    doc = make_doc()
    doc["left"] = [r for r in doc["left"] if r["table"] != "ranks"]
    refused(doc, "ranks")


def test_check_import_refuses_a_pulled_row_naming_a_missing_record():
    """templet:3159 -- a pulled row whose record is not in the document is
    refused, naming it; so is one naming a counter that is not there."""
    doc = make_doc()
    doc["pulled"][0]["record_key"] = [99]
    refused(doc, r"items.*\[99\]")
    doc = make_doc()
    doc["pulled"][-1]["record_key"] = ["next:nothing"]
    refused(doc, "next:nothing")
    doc = make_doc()
    doc["pulled"][0]["record"] = "nope"
    refused(doc, "nope")


def test_check_import_collects_every_problem():
    """templet:3159 -- the refusal lists each problem, not the first only."""
    doc = make_doc()
    doc["source"]["tables"]["orphans"] = 4
    doc["pulled"][0]["record_key"] = [99]
    with pytest.raises(ImportRefused) as raised:
        check_import(doc)
    assert len(raised.value.problems) >= 2
    assert "orphans" in str(raised.value) and "99" in str(raised.value)


def test_pulled_and_left_counts_equal_the_rows_read():
    """templet:3159 -- per table, the pulled rows plus the left rows' counts
    (a column is not a row) equal the rows read."""
    doc = make_doc()
    doc["source"]["tables"]["items"] = 3
    refused(doc, "items.*3")
    doc = make_doc()
    doc["left"][0]["count"] = 39
    refused(doc, "journal.*40")
    doc = make_doc()
    doc["left"][3]["count"] = 1  # a column row does not count as rows
    assert check_import(doc) is None
    doc = make_doc()
    doc["pulled"].append(dict(doc["pulled"][0]))
    refused(doc, "twice|duplicate")
    # a negative count cannot balance rows pulled past the rows read
    doc = make_doc()
    doc["source"]["tables"]["items"] = 1
    doc["left"].append({"table": "items", "legacy_key": None, "count": -1, "why": "w"})
    refused(doc, "below 0")
    # a row-keyed left row stands for one row
    doc = make_doc()
    doc["source"]["tables"]["meta"] = 7
    doc["left"][2]["count"] = 5
    refused(doc, "a row's count is 1")
    # a column's count is at most the table's rows read
    doc = make_doc()
    doc["left"][3]["count"] = 3
    refused(doc, "past the 2 rows read")


def test_check_import_refuses_a_malformed_document():
    """templet:3159 -- a wrong format, a missing part, an invalid record, a
    record kind the import must not carry, a duplicate key and a stale counter
    are each refused."""
    refused([], "document")
    doc = make_doc()
    doc["format"] = "quarrylith-import/2"
    refused(doc, "format")
    for part in ("source", "records", "counters", "pulled", "left"):
        doc = make_doc()
        del doc[part]
        refused(doc, part)
    doc = make_doc()
    doc["source"]["last_seq"] = "40"
    refused(doc, "last_seq")
    doc = make_doc()
    doc["records"]["items"][0]["bogus"] = 1
    refused(doc, "items.*bogus")
    doc = make_doc()
    doc["records"]["nope"] = []
    refused(doc, "nope")
    for kind in ("imports", "pulled", "left", "cursors", "meta"):
        doc = make_doc()
        doc["records"][kind] = [sample(kind)]
        refused(doc, kind)
    doc = make_doc()
    doc["records"]["items"].append(copy.deepcopy(doc["records"]["items"][0]))
    refused(doc, "items.*twice|duplicate")
    doc = make_doc()
    doc["counters"]["next:items"] = 2
    refused(doc, "next:items")
    doc = make_doc()
    del doc["counters"]["next:comments"]
    refused(doc, "next:comments")
    doc = make_doc()
    doc["counters"]["next:change:widgets"] = -1
    refused(doc, "next:change:widgets")
    doc = make_doc()
    doc["counters"]["items"] = 3
    refused(doc, "counter")
    doc = make_doc()
    doc["left"][0]["why"] = ""
    refused(doc, "why")


def test_a_counter_is_past_every_change_number_of_its_repo():
    """templet:3159 -- next:change:<repo> is past the highest n of that repo."""
    doc = make_doc()
    doc["records"]["changes"] = [
        sample("changes", id=1, repo="widgets", n=5, branch="b1"),
    ]
    doc["counters"]["next:changes"] = 2
    doc["source"]["tables"]["changes"] = 1
    doc["pulled"].append(
        {"table": "changes", "legacy_key": [1], "record": "changes", "record_key": [1]}
    )
    refused(doc, r"next:change:widgets")
    doc["counters"]["next:change:widgets"] = 6
    assert check_import(doc) is None


def test_compose_gives_the_same_dump_twice():
    """templet:3159 -- compose of one document gives the same dump twice,
    leaves the document as it was, and is the same whatever order the document
    lists its rows in."""
    doc = make_doc()
    before = copy.deepcopy(doc)
    first, second = compose(doc), compose(doc)
    assert first == second
    assert doc == before
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)

    shuffled = copy.deepcopy(doc)
    rng = random.Random(4)
    for rows in shuffled["records"].values():
        rng.shuffle(rows)
    rng.shuffle(shuffled["pulled"])
    rng.shuffle(shuffled["left"])
    shuffled["counters"] = dict(reversed(list(shuffled["counters"].items())))
    assert compose(shuffled) == first


def test_compose_refuses_what_check_import_refuses():
    """templet:3159 -- compose of a bad document raises, and writes nothing."""
    doc = make_doc()
    doc["source"]["tables"]["orphans"] = 4
    with pytest.raises(ImportRefused, match="orphans"):
        compose(doc)


def test_the_dump_has_the_dump_shape():
    """templet:3159 -- format 1; every journaled kind in key order; the journal
    as events; beats in the bookkeeping, not in the records."""
    dump = compose(make_doc())
    assert list(dump) == ["format", "records", "journal", "bookkeeping"]
    assert dump["format"] == 1
    assert list(dump["records"]) == [k for k, v in RECORDS.items() if v.journaled]
    assert "beats" not in dump["records"] and "cursors" not in dump["records"]
    assert [r["number"] for r in dump["records"]["items"]] == [1, 2]
    assert [r["number"] for r in dump["records"]["item_text"]] == [1, 2]
    assert dump["bookkeeping"] == {
        "beats": [sample("beats", run=1, at=2250)],
        "cursors": [],
    }
    assert json.loads(json.dumps(dump)) == dump


def test_the_journal_is_import_events_in_the_page_order():
    """templet:3159 -- the imports record first, then the records in RECORDS
    order, then pulled and left, then the counters as meta.set; every event via
    import, seqs from 1 by one, every event a plain-data Event."""
    dump = compose(make_doc())
    events = [Event.from_dict(e) for e in dump["journal"]]
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert {e.via for e in events} == {"import"}
    assert {e.at for e in events} == {1793371508596}
    assert all(e.kind in EVENT_KINDS for e in events)
    assert events[0].kind == "import"
    assert [p[0] for p in events[0].puts] == ["imports"]
    groups = [e.data["kind"] for e in events if e.kind == "import"]
    carried = [k for k in RECORDS if k in make_doc()["records"] and k != "beats"]
    assert groups == ["imports"] + carried + ["pulled", "left"]
    tail = [e for e in events if e.kind == "meta.set"]
    assert events[-len(tail) :] == tail
    names = [e.data["key"] for e in tail]
    assert names == sorted(names) and "next:imports" in names
    meta = {r["key"]: r["value"] for r in dump["records"]["meta"]}
    assert meta["next:items"] == 3 and meta["next:imports"] == 2
    assert meta["next:change:widgets"] == 5


def test_the_records_are_the_fold_of_the_journal():
    """templet:3159 -- apply over the composed journal, from seq 1 into an empty
    view, gives exactly the records of the dump."""
    dump = compose(make_doc())
    view = MemoryView()
    for raw in dump["journal"]:
        apply(view, Event.from_dict(raw))
    assert view.records() == dump["records"]
    # the beats are bookkeeping: outside the journal, outside the fold
    assert view.bookkeeping() == {"beats": [], "cursors": []}


def test_the_import_leaves_a_record_of_what_was_pulled_and_left():
    """templet:3159 -- one imports record with the counts; a pulled row per
    pulled legacy row and a left row per left one, keyed by the legacy key's
    canonical JSON, * for a table and column:<name> for a column."""
    dump = compose(make_doc())
    (imp,) = dump["records"]["imports"]
    assert imp["id"] == 1 and imp["format"] == IMPORT_FORMAT
    assert imp["source"]["name"] == "legacy-board"
    assert imp["source"]["last_seq"] == 40
    assert imp["started_at"] == imp["finished_at"] == 1793371508596
    assert imp["counts"]["pulled"] == {
        "claims": 1,
        "comments": 1,
        "items": 2,
        "meta": 2,
        "runs": 1,
    }
    assert imp["counts"]["left"] == {"journal": 40, "meta": 1, "ranks": 3}
    assert imp["counts"]["columns"] == {"items.source": 2, "runs.pid": 1}
    pulled = {(r["table"], r["legacy_ref"]): r for r in dump["records"]["pulled"]}
    assert pulled[("claims", "[3]")]["record"] == "leases"
    assert pulled[("claims", "[3]")]["record_key"] == [1]
    assert pulled[("meta", '["next_number"]')]["record_key"] == ["next:items"]
    assert all(r["import"] == 1 for r in dump["records"]["pulled"])
    left = {(r["table"], r["legacy_ref"]): r for r in dump["records"]["left"]}
    assert left[("journal", "*")]["count"] == 40
    assert left[("journal", "*")]["legacy_key"] is None
    assert left[("items", "column:source")]["count"] == 2
    assert left[("meta", '["other"]')]["legacy_key"] == ["other"]
    assert len(dump["records"]["pulled"]) == 7 and len(dump["records"]["left"]) == 5


def test_compose_keeps_every_record_it_was_given():
    """templet:3159 -- each record of the document is in the dump, whole."""
    doc = make_doc()
    dump = compose(doc)
    for kind, rows in doc["records"].items():
        if kind == "beats":
            continue
        assert dump["records"][kind] == sorted(
            rows, key=lambda r: [r[f.name] for f in RECORDS[kind].key]
        ), kind
    assert importing.__doc__


# -- build step 2 ---------------------------------------------------------------


def _old_canon(value):
    """step 1's private encoding, which legacy_ref keys were built from."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def test_canonical_is_byte_identical_to_the_old_encoding():
    """templet:3159 -- schema.canonical gives byte for byte what step 1's
    private encoding gave, over the sample document and every legacy key, so
    the pulled and left keys stay as they were."""
    doc = make_doc()
    assert schema.canonical(doc) == _old_canon(doc)
    for value in [*doc["records"].values(), *doc["pulled"], *doc["left"]]:
        assert schema.canonical(value) == _old_canon(value)
    dump = compose(doc)
    assert schema.canonical(dump) == _old_canon(dump)
    refs = {r["legacy_ref"] for r in dump["records"]["pulled"]}
    assert _old_canon([3]) in refs and _old_canon(["next_number"]) in refs
    assert not hasattr(importing, "_canon")
    assert not hasattr(schema, "_json_bytes")


def test_a_pulled_row_s_record_key_is_checked_as_a_key():
    """templet:3159 -- a pulled row's record_key is a key of its kind: a bool
    or a float that equals a key is refused, not matched."""
    for bad in ([True], [1.0], [1, 2], ["1"]):
        doc = make_doc()
        doc["pulled"][0]["record_key"] = bad
        refused(doc, "no record items")


def _entry(event):
    priors = [[k, key, None] for k, key, _ in event["puts"]]
    return schema.size({"event": event, "priors": priors})


def test_compose_splits_by_the_journal_entry():
    """templet:3159 -- compose splits an import of many small records with
    long keys so that every entry, the event and its null priors by
    schema.size, fits within 768 KiB; the dump is the fold of its journal."""
    doc = make_doc()
    long = "t" * 240
    links = [
        sample("effort_links", effort="0001", kind="plan", target=f"{i:05d}{long}")
        for i in range(3000)
    ]
    doc["records"]["effort_links"] += links
    dump = compose(doc)
    limit = schema.LIMITS["entry_bytes"]
    sizes = [_entry(e) for e in dump["journal"]]
    assert max(sizes) <= limit
    groups = [e for e in dump["journal"] if e["data"].get("kind") == "effort_links"]
    assert len(groups) >= 2
    assert sum(e["data"]["count"] for e in groups) == 3002
    assert sum(len(e["puts"]) for e in groups) == 3002
    assert sum(schema.size(e) for e in groups) > limit  # one event would not fit
    assert [e["seq"] for e in dump["journal"]] == list(
        range(1, len(dump["journal"]) + 1)
    )
    view = MemoryView()
    for raw in dump["journal"]:
        apply(view, Event.from_dict(raw))
    assert view.records() == dump["records"]
    assert compose(doc) == dump


def test_compose_refuses_a_record_too_large_for_one_entry():
    """templet:3159 -- a record whose put alone passes the entry's limit is
    refused, not written past it."""
    doc = make_doc()
    big = "x" * (256 * 1024 - 10)
    doc["records"]["items"][0].update(title=big, filed_by=big, source_ref=big)
    with pytest.raises(ImportRefused, match="entry"):
        compose(doc)
