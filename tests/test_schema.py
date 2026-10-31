# SPDX-License-Identifier: Apache-2.0
"""The record's declarations (templet:3159, build step 1): every kind with its
key, fields, match and live field; what a record may hold; the limits; the key
order; the label and flag grammar."""

import sqlite3

import pytest
from samples import sample

from quarrylith.record import schema
from quarrylith.record.schema import RECORDS, SchemaError

# The page's table of records, held inline: key | fields | match | live.
# `name=type`, and a trailing `?` where the field may be null.
EXPECTED = {
    "meta": ("key=str", "value=json", "", "", True),
    "efforts": (
        "id=str",
        "slug=str title=str standing=str owner=str created_at=int statement=str"
        " inputs=json tools=json budget=json evidence=json gates=json order=json"
        " outcome=str? concluded_at=int?",
        "standing",
        "concluded_at",
        True,
    ),
    "effort_links": (
        "effort=str kind=str target=str",
        "at=int by=str unlinked_at=int?",
        "effort target",
        "unlinked_at",
        True,
    ),
    "items": (
        "number=int",
        "kind=str title=str state=str role=str? tier=int? risk=int? parent=int?"
        " labels=json flags=json admitted=bool filed_by=str must_fix=bool"
        " templet=int? review_round=int overruled_at=int? created_at=int"
        " updated_at=int closed_at=int? source=str source_ref=str?",
        "state kind role parent",
        "closed_at",
        True,
    ),
    "item_text": ("number=int", "body=str", "", "", True),
    "deps": (
        "id=int",
        "number=int blocker=int? change=int? what=str? added_at=int added_by=str"
        " lifted_at=int? lifted_by=str? why=str",
        "number blocker change",
        "lifted_at",
        True,
    ),
    "leases": (
        "id=int",
        "number=int holder=str role=str? fence=int branch=str machine=str"
        " granted_at=int renewed_at=int paused_until=int? expires_at=int?"
        " released_at=int? released_by=str? why=str?",
        "number holder",
        "released_at",
        True,
    ),
    "tasks": (
        "id=int",
        "number=int? effort=str? kind=str labels=json flags=json needs=json"
        " sandbox=str schedule=str? not_before=int? supervision=json loop=json"
        " attempts=int state=str created_at=int ended_at=int?",
        "number state kind effort",
        "ended_at",
        True,
    ),
    "nodes": (
        "name=str",
        "drivers=json labels=json declared_at=int retired_at=int?",
        "",
        "retired_at",
        True,
    ),
    "capacity": (
        "node=str resource=str",
        "amount=int unit=str declared_by=str at=int",
        "node",
        "",
        True,
    ),
    "reservations": (
        "id=int",
        "node=str resource=str amount=int task=int? run=int? holder=str"
        " from_at=int until_at=int? released_at=int? why=str?",
        "node task run",
        "released_at",
        True,
    ),
    "runs": (
        "id=int",
        "task=int attempt=int holder=str fence=int? node=str sandbox=str"
        " branch=str? pgid=int? phase=str phase_at=int expect_until=int?"
        " harness=json footprint=json cost=json outcome=str? reason=str?"
        " started_at=int finished_at=int?",
        "task holder node",
        "finished_at",
        True,
    ),
    "run_text": ("run=int", "result=str", "", "", True),
    "changes": (
        "id=int",
        "repo=str n=int number=int? relation=str title=str branch=str head=str"
        " base=str? position=int? draft=bool url=str? opened_at=int"
        " opened_by=str? pushed_at=int? merged_at=int? dropped_at=int?"
        " dropped_by=str?",
        "number repo branch",
        "merged_at dropped_at",
        True,
    ),
    "change_text": ("change=int", "body=str", "", "", True),
    "checks": (
        "change=int head=str name=str",
        "state=str detail=str? at=int by=str",
        "change",
        "",
        True,
    ),
    "reviews": (
        "id=int",
        "target=int round=int kind=str item=int? author=str heads=json pages=json"
        " filed_by=str? reviewer=str? verdict=str? findings=json filed_at=int"
        " verdict_at=int? read_at=int?",
        "target item",
        "verdict",
        True,
    ),
    "merges": (
        "id=int",
        "change=int sha=str commit=str? by=str rule=str at=int",
        "change",
        "",
        True,
    ),
    "comments": (
        "id=int",
        "number=int? change=int? who=str body=str at=int mark=str? ref=str?"
        " source_ref=str?",
        "number change",
        "",
        True,
    ),
    "questions": (
        "id=int",
        "number=int run=int? who=str kind=str text=str asked_at=int"
        " answered_at=int? answered_by=str? answer=int?",
        "number",
        "answered_at",
        True,
    ),
    "statements": (
        "id=int",
        "type=str effort=str? number=int? text=str who=str at=int where=str?"
        " supersedes=int?",
        "effort number type",
        "",
        True,
    ),
    "holds": (
        "id=int",
        "number=int? who=str? cause=str why=str set_at=int set_by=str"
        " lifted_at=int? lifted_by=str?",
        "number who",
        "lifted_at",
        True,
    ),
    "readings": (
        "id=int",
        "source=str kind=str value=json at=int",
        "source kind",
        "",
        True,
    ),
    "imports": (
        "id=int",
        "source=json format=str started_at=int finished_at=int? counts=json",
        "",
        "finished_at",
        True,
    ),
    "pulled": (
        "import=int table=str legacy_ref=str",
        "record=str record_key=json at=int",
        "import record",
        "",
        True,
    ),
    "left": (
        "import=int table=str legacy_ref=str",
        "legacy_key=json? count=int why=str at=int",
        "import",
        "",
        True,
    ),
    "beats": ("run=int", "at=int", "", "", False),
    "cursors": ("consumer=str", "seq=int at=int", "", "", False),
}


def parse(fields):
    out = []
    for word in fields.split():
        name, _, typ = word.partition("=")
        out.append((name, typ.rstrip("?"), typ.endswith("?")))
    return out


def declared(fields):
    return [(f.name, f.type, f.null) for f in fields]


def test_records_declares_every_kind():
    """templet:3159 -- RECORDS declares every kind of the page's table, with
    its key, fields, match and live field."""
    assert list(RECORDS) == list(EXPECTED)
    for name, (key, fields, match, live, journaled) in EXPECTED.items():
        kind = RECORDS[name]
        assert kind.name == name
        assert declared(kind.key) == parse(key), name
        assert declared(kind.fields) == parse(fields), name
        assert kind.match == tuple(match.split()), name
        assert kind.live == tuple(live.split()), name
        assert kind.journaled is journaled, name
    assert RECORDS["deps"].one_of == ("blocker", "change", "what")
    assert RECORDS["holds"].one_of == ("number", "who")
    assert RECORDS["efforts"].fields[11].default == {"rule": "unblocks-then-age"}


def test_a_kind_is_well_formed():
    """templet:3159 -- a key is int or str and never null; a match field is
    int, str or bool; match and live name declared fields."""
    for kind in RECORDS.values():
        assert kind.key, kind.name
        for f in kind.key:
            assert f.type in (schema.INT, schema.STR) and not f.null, kind.name
        names = {f.name for f in kind.key + kind.fields}
        assert len(names) == len(kind.key + kind.fields), kind.name
        by_name = {f.name: f for f in kind.key + kind.fields}
        for m in kind.match:
            assert by_name[m].type in (schema.INT, schema.STR, schema.BOOL)
        assert set(kind.live) <= names and set(kind.one_of) <= names
    for bad in (schema.JSON, "float", "bytes"):
        with pytest.raises(SchemaError):
            schema.Kind("k", (schema.Field("id", bad),), (), (), ())
    with pytest.raises(SchemaError):
        schema.Kind("k", (schema.Field("id", schema.INT, null=True),), (), (), ())
    with pytest.raises(SchemaError):
        schema.Kind(
            "k",
            (schema.Field("id", schema.INT),),
            (schema.Field("j", schema.JSON),),
            ("j",),
            (),
        )
    with pytest.raises(SchemaError):
        schema.Kind("k", (schema.Field("id", schema.INT),), (), ("nope",), ())


def test_a_sample_of_every_kind_is_valid_and_a_stranger_is_refused():
    """templet:3159 -- a record with an undeclared field, a missing field or a
    wrong type is refused."""
    for name, kind in RECORDS.items():
        rec = sample(name)
        assert schema.validate(name, rec) == rec, name
        with pytest.raises(SchemaError, match="bogus"):
            schema.validate(name, dict(rec, bogus=1))
        for f in kind.key + kind.fields:
            if not f.null and f.default is None:
                gone = {k: v for k, v in rec.items() if k != f.name}
                with pytest.raises(SchemaError, match=f.name):
                    schema.validate(name, gone)
            if not f.null:
                with pytest.raises(SchemaError, match=f.name):
                    schema.validate(name, dict(rec, **{f.name: None}))
            wrong = {
                schema.STR: [1, b"x", ["x"], None],
                schema.INT: ["1", 1.0, True, None],
                schema.BOOL: [0, 1, "no", None],
                schema.JSON: [object(), b"x", {1, 2}],
            }[f.type]
            for value in wrong:
                if value is None and f.null:
                    continue
                with pytest.raises(SchemaError, match=f.name):
                    schema.validate(name, dict(rec, **{f.name: value}))


def test_a_default_fills_a_missing_field_and_one_of_is_exact():
    """templet:3159 -- a field with a default is filled; deps and holds name
    exactly one of their alternatives."""
    rec = sample("efforts")
    del rec["order"]
    assert schema.validate("efforts", rec)["order"] == {"rule": "unblocks-then-age"}
    one = sample("deps")
    assert schema.validate("deps", one)
    none = dict(one, blocker=None)
    with pytest.raises(SchemaError, match="exactly one"):
        schema.validate("deps", none)
    two = dict(one, blocker=1, change=2)
    with pytest.raises(SchemaError, match="exactly one"):
        schema.validate("deps", two)
    assert schema.validate("holds", sample("holds", number=None, who="alpha"))
    with pytest.raises(SchemaError, match="exactly one"):
        schema.validate("holds", sample("holds", number=1, who="alpha"))


def test_validate_returns_a_copy_with_lists_for_tuples():
    """templet:3159 -- tuples become lists in a json field; the input is not
    changed."""
    rec = sample("tasks", needs={"cpu": (1, 2)})
    out = schema.validate("tasks", rec)
    assert out["needs"] == {"cpu": [1, 2]}
    assert isinstance(rec["needs"]["cpu"], tuple)
    assert out is not rec


def test_json_is_plain_data():
    """templet:3159 -- a json field holds dicts with str keys, lists, str, int,
    float, bool and None, and nothing else."""
    good = {"a": [1, 2.5, "x", True, None, {"b": []}]}
    assert schema.validate("readings", sample("readings", value=good))
    for bad in ({1: "x"}, {"a": {2}}, [b"x"], object(), {"a": float}, {None: 1}):
        with pytest.raises(SchemaError):
            schema.validate("readings", sample("readings", value=bad))
    deep = cur = []
    for _ in range(5000):
        nxt = []
        cur.append(nxt)
        cur = nxt
    with pytest.raises(SchemaError):
        schema.validate("readings", sample("readings", value=deep))


@pytest.mark.parametrize(
    "bad", ["plan", "Role:x", "role:", ":x", "a:b:c", "1a:b", "3tier:x", "Repo:x"]
)
def test_a_label_must_be_key_value(bad):
    """templet:3159 -- a label that is not key:value is refused."""
    with pytest.raises(SchemaError):
        schema.check_labels([bad])


def test_label_grammar():
    """templet:3159 -- a second label of one key, an effort: or state: label,
    an unsorted list and more than 64 are refused; the good ones pass."""
    assert schema.check_labels([]) == []
    assert schema.check_labels(["repo:widgets", "team:alpha", "x:a/b.c_d"])
    for good in ("repo:WidgetShop", "repo:MixedCase", "tier:3", "risk:10"):
        assert schema.check_labels([good]) == [good]
    assert schema.check_labels(["repo:WidgetShop", "risk:10", "tier:3"])
    with pytest.raises(SchemaError, match="sorted"):
        schema.check_labels(["b:A", "a:B"])
    with pytest.raises(SchemaError, match="one label per key"):
        schema.check_labels(["repo:WidgetShop", "repo:MixedCase"])
    with pytest.raises(SchemaError, match="one label per key"):
        schema.check_labels(["team:a", "team:b"])
    for barred in ("effort:0001", "effort:alpha", "state:ready"):
        with pytest.raises(SchemaError, match="effort.*state|state.*effort"):
            schema.check_labels([barred])
    with pytest.raises(SchemaError, match="sorted"):
        schema.check_labels(["b:x", "a:x"])
    many = [f"{chr(97 + i // 26)}{chr(97 + i % 26)}:v" for i in range(64)]
    assert schema.check_labels(many)
    with pytest.raises(SchemaError, match="64"):
        schema.check_labels(sorted(many + ["zz:v"]))
    with pytest.raises(SchemaError):
        schema.check_labels("team:a")
    with pytest.raises(SchemaError):
        schema.check_labels([1])
    rec = sample("items", labels=["b:x", "a:x"])
    with pytest.raises(SchemaError, match="labels"):
        schema.validate("items", rec)
    with pytest.raises(SchemaError, match="labels"):
        schema.validate("tasks", sample("tasks", labels=["state:open"]))
    node = sample("nodes", labels=["zone:A1"], drivers=["podman", "process"])
    assert schema.validate("nodes", node)
    for bad in (["zone:a", "rack:b"], ["Zone:a"], ["zone:a", "zone:b"]):
        with pytest.raises(SchemaError, match="nodes.labels"):
            schema.validate("nodes", sample("nodes", labels=bad))


def test_a_nodes_drivers_are_a_sorted_list_of_strs():
    """templet:3159 -- a node's drivers are a sorted list of strs."""
    assert schema.check_names(["podman", "process", "vm"]) == [
        "podman",
        "process",
        "vm",
    ]
    for bad in (["vm", "process"], ["vm", 1], "vm", {"vm": 1}):
        with pytest.raises(SchemaError, match="nodes.drivers"):
            schema.validate("nodes", sample("nodes", drivers=bad))


def test_flag_grammar():
    """templet:3159 -- a flag is a word, optionally with a :qualifier."""
    assert schema.check_flags(["needs:lab", "parallel", "x.y-z"]) == [
        "needs:lab",
        "parallel",
        "x.y-z",
    ]
    for bad in ("Needs", "needs:", "a:b:c", "1x", "", "needs lab"):
        with pytest.raises(SchemaError):
            schema.check_flags([bad])
    with pytest.raises(SchemaError, match="duplicate"):
        schema.check_flags(["a", "a"])
    with pytest.raises(SchemaError, match="sorted"):
        schema.check_flags(["b", "a"])
    many = [f"{chr(97 + i // 26)}{chr(97 + i % 26)}" for i in range(65)]
    with pytest.raises(SchemaError, match="64"):
        schema.check_flags(many)
    with pytest.raises(SchemaError, match="flags"):
        schema.validate("items", sample("items", flags=["B"]))


def test_limits():
    """templet:3159 -- the limits are one set: a str and a json field 256 KiB,
    a key and a match field 256 bytes, an entry 768 KiB, 64 labels and flags."""
    assert schema.LIMITS == {
        "str_bytes": 256 * 1024,
        "json_bytes": 256 * 1024,
        "key_bytes": 256,
        "match_bytes": 256,
        "entry_bytes": 768 * 1024,
        "labels": 64,
        "flags": 64,
    }


def test_a_field_past_its_limit_is_refused():
    """templet:3159 -- a str, a json, a key and a match field past their
    limits are refused, and the limit itself is allowed."""
    limit = 256 * 1024
    ok = sample("item_text", body="x" * limit)
    assert schema.validate("item_text", ok)
    with pytest.raises(SchemaError, match="body"):
        schema.validate("item_text", sample("item_text", body="x" * (limit + 1)))
    # bytes, not characters: 131073 two-byte characters are 262146 bytes
    with pytest.raises(SchemaError, match="body"):
        schema.validate("item_text", sample("item_text", body="é" * (limit // 2 + 1)))
    assert schema.validate("item_text", sample("item_text", body="é" * (limit // 2)))

    # a json value of exactly 256 KiB encoded: a str of limit - 2, quoted
    assert schema.validate("readings", sample("readings", value="x" * (limit - 2)))
    with pytest.raises(SchemaError, match="value"):
        schema.validate("readings", sample("readings", value="x" * (limit - 1)))
    with pytest.raises(SchemaError, match="value"):
        schema.validate("readings", sample("readings", value={"k": ["y" * 1000] * 300}))

    assert schema.validate("meta", sample("meta", key="k" * 256))
    with pytest.raises(SchemaError, match="key"):
        schema.validate("meta", sample("meta", key="k" * 257))
    with pytest.raises(SchemaError, match="key"):
        schema.check_key("meta", ["é" * 129])
    # a match field is at most 256 bytes, though a str field may be longer
    assert schema.validate("items", sample("items", role="r" * 256))
    with pytest.raises(SchemaError, match="role"):
        schema.validate("items", sample("items", role="r" * 257))
    assert schema.validate("items", sample("items", title="t" * 300))


@pytest.mark.parametrize(
    "bad",
    [float("nan"), float("inf"), -float("inf"), {"a": [float("nan")]}],
)
def test_a_float_that_is_not_finite_is_refused(bad):
    """templet:3159 -- a NaN or an infinite float is refused."""
    with pytest.raises(SchemaError):
        schema.validate("readings", sample("readings", value=bad))


def test_an_int_outside_64_bits_is_refused():
    """templet:3159 -- an int outside signed 64 bits is refused, in an int field
    and inside a json one."""
    top, bottom = 2**63 - 1, -(2**63)
    assert schema.validate("items", sample("items", number=top))
    assert schema.validate("items", sample("items", number=bottom))
    for bad in (top + 1, bottom - 1):
        with pytest.raises(SchemaError, match="number"):
            schema.validate("items", sample("items", number=bad))
        with pytest.raises(SchemaError):
            schema.validate("readings", sample("readings", value=[bad]))
        with pytest.raises(SchemaError):
            schema.check_key("items", [bad])


def test_a_lone_surrogate_is_refused():
    """templet:3159 -- a str that is not valid UTF-8 is refused, in a str field,
    a key, a json str and a json key."""
    bad = "ok\ud800"
    with pytest.raises(SchemaError, match="UTF-8"):
        schema.validate("item_text", sample("item_text", body=bad))
    with pytest.raises(SchemaError, match="UTF-8"):
        schema.validate("meta", sample("meta", key=bad))
    with pytest.raises(SchemaError, match="UTF-8"):
        schema.validate("readings", sample("readings", value=[bad]))
    with pytest.raises(SchemaError, match="UTF-8"):
        schema.validate("readings", sample("readings", value={bad: 1}))
    assert schema.validate("item_text", sample("item_text", body="astral \U0001f600"))


def test_a_json_or_null_key_field_is_refused():
    """templet:3159 -- a key is int or str, never json, bool or null."""
    for bad in (None, True, 1.5, {"a": 1}, ["x"], b"x"):
        with pytest.raises(SchemaError):
            schema.validate("items", sample("items", number=bad))
        with pytest.raises(SchemaError):
            schema.check_key("items", [bad])
    with pytest.raises(SchemaError):
        schema.check_key("items", [])
    with pytest.raises(SchemaError):
        schema.check_key("items", [1, 2])
    with pytest.raises(SchemaError):
        schema.check_key("items", "nope")
    with pytest.raises(SchemaError):
        schema.check_key("no-such-kind", [1])
    assert schema.check_key("checks", (3, "abc", "lint")) == [3, "abc", "lint"]
    assert schema.key_of("checks", sample("checks", change=3, head="abc")) == [
        3,
        "abc",
        "x",
    ]


KEYS = [
    [-(2**63)],
    [-5],
    [0],
    [2],
    [10],
    [2**62],
    [2**63 - 1],
]
STRS = [
    "",
    "0001",
    "10",
    "2",
    "A",
    "Z",
    "a",
    "b",
    "é",
    "￿",
    "\U00010000",
    "\U0001f600",
]


def test_key_order_is_numeric_for_ints_and_by_code_point_for_strs():
    """templet:3159 -- ints sort numerically, strs by code point, component by
    component, and SQLite's ORDER BY of the same keys agrees."""
    assert schema.sort_keys([[10], [2], [-5]]) == [[-5], [2], [10]]
    assert schema.sort_keys([["10"], ["2"]]) == [["10"], ["2"]]
    assert schema.sort_keys([["b", 1], ["a", 9], ["a", 2]]) == [
        ["a", 2],
        ["a", 9],
        ["b", 1],
    ]
    # an astral character sorts above U+FFFF: by code point, not UTF-16 unit
    assert schema.sort_keys([["\U00010000"], ["￿"]]) == [
        ["￿"],
        ["\U00010000"],
    ]

    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE a (k INTEGER PRIMARY KEY) STRICT")
    db.execute("CREATE TABLE b (k TEXT PRIMARY KEY) STRICT")
    db.execute("CREATE TABLE c (x TEXT, y INTEGER, PRIMARY KEY (x, y)) STRICT")
    shuffled = KEYS[3:] + KEYS[:3]
    db.executemany("INSERT INTO a VALUES (?)", [(k[0],) for k in shuffled])
    assert [[r[0]] for r in db.execute("SELECT k FROM a ORDER BY k")] == (
        schema.sort_keys(shuffled)
    )
    strs = STRS[5:] + STRS[:5]
    db.executemany("INSERT INTO b VALUES (?)", [(s,) for s in strs])
    assert [[r[0]] for r in db.execute("SELECT k FROM b ORDER BY k")] == (
        schema.sort_keys([[s] for s in strs])
    )
    pairs = [[s, n] for s in STRS[:6] for n in (-1, 0, 7, 70)]
    db.executemany("INSERT INTO c VALUES (?, ?)", pairs[::-1])
    assert [list(r) for r in db.execute("SELECT x, y FROM c ORDER BY x, y")] == (
        schema.sort_keys(pairs)
    )


def test_sort_keys_refuses_a_key_it_cannot_order():
    """templet:3159 -- no bool, float or null in a key, and no tuple back."""
    for bad in ([True], [None], [1.5], [["x"]]):
        with pytest.raises(SchemaError):
            schema.sort_keys([bad])
    out = schema.sort_keys([(2,), (1,)])
    assert out == [[1], [2]] and all(isinstance(k, list) for k in out)


# -- build step 2: the one encoding, and the enumerated values ---------------


def test_canonical_is_sorted_compact_utf8_json():
    """templet:3159 -- schema.canonical is JSON with sorted keys, no spaces,
    UTF-8 (not escaped), tuples as lists; it refuses a NaN."""
    assert schema.canonical({"b": [1, (2, 3)], "a": "é"}) == '{"a":"é","b":[1,[2,3]]}'
    assert schema.canonical(None) == "null" and schema.canonical(True) == "true"
    with pytest.raises(SchemaError):
        schema.canonical([float("nan")])
    with pytest.raises(SchemaError):
        schema.canonical({"a": object()})


def test_size_is_the_canonical_length_with_a_float_as_nine_bytes():
    """templet:3159 -- schema.size is the UTF-8 length of schema.canonical,
    each float counted as 9 bytes."""
    assert schema.size({"a": "é"}) == len('{"a":"é"}'.encode()) == 10
    assert schema.size([1.5]) == 2 + 9
    assert schema.size([0.1, 1e300, -2.5e-7]) == 2 + 2 + 3 * 9
    assert schema.size({"k": [True, None, 3]}) == len('{"k":[true,null,3]}')
    assert schema.size(("x",)) == schema.size(["x"]) == 5


def test_a_json_field_is_measured_by_size():
    """templet:3159 -- a json field's limit is by schema.size, floats as 9
    bytes: a list whose JSON text fits but whose size does not is refused."""
    limit = schema.LIMITS["json_bytes"]
    floats = [1.0] * (limit // 9)
    assert len(schema.canonical(floats)) < limit < schema.size(floats)
    with pytest.raises(SchemaError, match="value"):
        schema.validate("readings", sample("readings", value=floats))
    assert schema.validate("readings", sample("readings", value=[1.0] * 1000))


#: The page's enumerated fields and their values, held inline.
VALUES = {
    ("efforts", "standing"): ("proposed", "admitted", "parked", "concluded"),
    ("efforts", "outcome"): ("complete", "incomplete", "disproved"),
    ("effort_links", "kind"): ("plan", "item", "effort"),
    ("items", "kind"): ("task", "plan", "review", "mission", "ask", "finding", "batch"),
    ("items", "state"): (
        "triage",
        "ready",
        "claimed",
        "blocked",
        "in-review",
        "reviewed",
        "merge-ready",
        "done",
        "dropped",
    ),
    ("items", "source"): ("local", "legacy"),
    ("tasks", "kind"): ("agent", "gate", "review", "cell", "sweep", "job"),
    ("tasks", "state"): ("open", "running", "done", "failed", "cancelled"),
    ("changes", "relation"): ("closes", "part"),
    ("checks", "state"): ("green", "red", "skipped", "pending"),
    ("reviews", "kind"): ("pool", "second", "templet", "conformance", "user"),
    ("reviews", "verdict"): ("approve", "changes"),
    ("questions", "kind"): ("blocked", "pr"),
    ("statements", "type"): (
        "observation",
        "inference",
        "proposal",
        "decision",
        "action",
        "outcome",
        "correction",
    ),
}


def test_an_enumerated_field_takes_only_its_values():
    """templet:3159 -- schema.VALUES names the page's enumerated fields and
    their values, and validate refuses any other value (null stays null where
    the field may be)."""
    assert schema.VALUES == VALUES
    assert schema.ITEM_STATES == VALUES[("items", "state")]
    for (kind, field), values in VALUES.items():
        for value in values:
            assert schema.validate(kind, sample(kind, **{field: value}))[field] == value
        with pytest.raises(SchemaError, match=field):
            schema.validate(kind, sample(kind, **{field: "other"}))
    assert (
        schema.validate("efforts", sample("efforts", outcome=None))["outcome"] is None
    )
