# SPDX-License-Identifier: Apache-2.0
"""The record's declarations: every kind of record, once, and what a record
may hold.

A kind is `Kind(name, key, fields, match, live, journaled=True, one_of=())`;
a field is `Field(name, type, null=False, default=None)` of one of four types:
`str`, `int`, `bool` and `json` (plain data: dicts with `str` keys, lists,
`str`, `int`, `float`, `bool` and None). Nothing else exists, so a record fits
SQLite's affinities and Henge's msgpack values alike, and the schema refuses
what the two would take differently: a float that is NaN or infinite, an int
outside signed 64 bits and a `str` that is not valid UTF-8. `VALUES` names the
fields that take one of a few values, and those values.

`canonical(value)` is the one encoding of plain data, and `size(value)` the one
measure of it, on both backends.

Times are `int` milliseconds since the epoch, UTC. No record is ever deleted:
a record ends by its end fields, the kind's `live`, which may name several (a
change is open until it is merged or dropped).
"""

import copy
import json
import re
from dataclasses import dataclass

STR, INT, BOOL, JSON = "str", "int", "bool", "json"
TYPES = (STR, INT, BOOL, JSON)

INT_MIN, INT_MAX = -(2**63), 2**63 - 1

#: One set of limits on both backends. A `str`, a key and a `match` field are
#: measured in UTF-8 bytes; a `json` field and a journal entry (the event and
#: its priors together) by `size`.
LIMITS = {
    "str_bytes": 256 * 1024,
    "json_bytes": 256 * 1024,
    "key_bytes": 256,
    "match_bytes": 256,
    "entry_bytes": 768 * 1024,
    "labels": 64,
    "flags": 64,
}

#: `key:value`: the key lowercase, starting with a letter; the value may hold
#: capitals, may start with a digit, and may also hold `/`.
LABEL_RE = re.compile(r"[a-z][a-z0-9_.-]*:[A-Za-z0-9][A-Za-z0-9_./-]*")
#: A word of the label key's alphabet, optionally with a `:qualifier`
#: (`needs:lab`).
FLAG_RE = re.compile(r"[a-z][a-z0-9_.-]*(:[a-z][a-z0-9_./-]*)?")
#: Effort links are records and the state is a field, so neither is a label.
BARRED_LABEL_KEYS = ("effort", "state")

#: The `json` fields that hold labels, those that hold flags, and those that
#: hold a sorted list of names, by kind.
LABEL_FIELDS = {"items": "labels", "tasks": "labels", "nodes": "labels"}
FLAG_FIELDS = {"items": "flags", "tasks": "flags"}
NAME_FIELDS = {"nodes": "drivers"}

#: An item's states, the legacy board's nine.
ITEM_STATES = (
    "triage",
    "ready",
    "claimed",
    "blocked",
    "in-review",
    "reviewed",
    "merge-ready",
    "done",
    "dropped",
)

#: The fields that take one of a few values, and those values (the page's
#: table of records). `validate` refuses any other; a null stays null where
#: the field may be null.
VALUES = {
    ("efforts", "standing"): ("proposed", "admitted", "parked", "concluded"),
    ("efforts", "outcome"): ("complete", "incomplete", "disproved"),
    ("effort_links", "kind"): ("plan", "item", "effort"),
    ("items", "kind"): ("task", "plan", "review", "mission", "ask", "finding", "batch"),
    ("items", "state"): ITEM_STATES,
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


class SchemaError(ValueError):
    """A value the declarations refuse."""


@dataclass(frozen=True)
class Field:
    name: str
    type: str
    null: bool = False
    default: object = None

    def __post_init__(self):
        if self.type not in TYPES:
            raise SchemaError(f"field {self.name}: no type {self.type!r}")


@dataclass(frozen=True)
class Kind:
    """One kind of record.

    `key` is the key's fields, in key order; `fields` the rest. `match` names
    the fields `read()` may match on, and `live` the fields whose `None` means
    the record is still open (all of them: a change is open until it is
    merged or dropped). `one_of` names fields of which exactly one is set.
    """

    name: str
    key: tuple
    fields: tuple
    match: tuple
    live: tuple
    journaled: bool = True
    one_of: tuple = ()

    def __post_init__(self):
        if not self.key:
            raise SchemaError(f"{self.name}: no key")
        for f in self.key:
            if f.type not in (INT, STR) or f.null or f.default is not None:
                raise SchemaError(
                    f"{self.name}.{f.name}: a key field is int or str, "
                    "never json, bool or null"
                )
        names = [f.name for f in self.key + self.fields]
        if len(set(names)) != len(names):
            raise SchemaError(f"{self.name}: a field is declared twice")
        by_name = {f.name: f for f in self.key + self.fields}
        for name in self.match:
            if name not in by_name or by_name[name].type == JSON:
                raise SchemaError(
                    f"{self.name}.{name}: a match field is a declared int, "
                    "str or bool"
                )
        for name in self.live + self.one_of:
            if name not in by_name or not by_name[name].null:
                raise SchemaError(f"{self.name}.{name}: not a declared null field")

    @property
    def key_names(self):
        return tuple(f.name for f in self.key)

    @property
    def all_fields(self):
        return self.key + self.fields


def _fields(spec, **defaults):
    """Fields from `name=type` words; a trailing `?` lets the field be null."""
    out = []
    for word in spec.split():
        name, _, typ = word.partition("=")
        out.append(
            Field(name, typ.rstrip("?"), typ.endswith("?"), defaults.pop(name, None))
        )
    if defaults:
        raise SchemaError(f"defaults for no field: {sorted(defaults)}")
    return tuple(out)


def _kind(name, key, fields, match="", live="", journaled=True, one_of="", **dflt):
    return Kind(
        name,
        _fields(key),
        _fields(fields, **dflt),
        tuple(match.split()),
        tuple(live.split()),
        journaled,
        tuple(one_of.split()),
    )


def _declare(*kinds):
    return {k.name: k for k in kinds}


#: Every kind, once, in the order a load writes them.
RECORDS = _declare(
    _kind("meta", "key=str", "value=json"),
    _kind(
        "efforts",
        "id=str",
        "slug=str title=str standing=str owner=str created_at=int statement=str"
        " inputs=json tools=json budget=json evidence=json gates=json order=json"
        " outcome=str? concluded_at=int?",
        match="standing",
        live="concluded_at",
        order={"rule": "unblocks-then-age"},
    ),
    _kind(
        "effort_links",
        "effort=str kind=str target=str",
        "at=int by=str unlinked_at=int?",
        match="effort target",
        live="unlinked_at",
    ),
    _kind(
        "items",
        "number=int",
        "kind=str title=str state=str role=str? tier=int? risk=int? parent=int?"
        " labels=json flags=json admitted=bool filed_by=str must_fix=bool"
        " templet=int? review_round=int overruled_at=int? created_at=int"
        " updated_at=int closed_at=int? source=str source_ref=str?",
        match="state kind role parent",
        live="closed_at",
    ),
    _kind("item_text", "number=int", "body=str"),
    _kind(
        "deps",
        "id=int",
        "number=int blocker=int? change=int? what=str? added_at=int added_by=str"
        " lifted_at=int? lifted_by=str? why=str",
        match="number blocker change",
        live="lifted_at",
        one_of="blocker change what",
    ),
    _kind(
        "leases",
        "id=int",
        "number=int holder=str role=str? fence=int branch=str machine=str"
        " granted_at=int renewed_at=int paused_until=int? expires_at=int?"
        " released_at=int? released_by=str? why=str?",
        match="number holder",
        live="released_at",
    ),
    _kind(
        "tasks",
        "id=int",
        "number=int? effort=str? kind=str labels=json flags=json needs=json"
        " sandbox=str schedule=str? not_before=int? supervision=json loop=json"
        " attempts=int state=str created_at=int ended_at=int?",
        match="number state kind effort",
        live="ended_at",
    ),
    _kind(
        "nodes",
        "name=str",
        "drivers=json labels=json declared_at=int retired_at=int?",
        live="retired_at",
    ),
    _kind(
        "capacity",
        "node=str resource=str",
        "amount=int unit=str declared_by=str at=int",
        match="node",
    ),
    _kind(
        "reservations",
        "id=int",
        "node=str resource=str amount=int task=int? run=int? holder=str"
        " from_at=int until_at=int? released_at=int? why=str?",
        match="node task run",
        live="released_at",
    ),
    _kind(
        "runs",
        "id=int",
        "task=int attempt=int holder=str fence=int? node=str sandbox=str"
        " branch=str? pgid=int? phase=str phase_at=int expect_until=int?"
        " harness=json footprint=json cost=json outcome=str? reason=str?"
        " started_at=int finished_at=int?",
        match="task holder node",
        live="finished_at",
    ),
    _kind("run_text", "run=int", "result=str"),
    _kind(
        "changes",
        "id=int",
        "repo=str n=int number=int? relation=str title=str branch=str head=str"
        " base=str? position=int? draft=bool url=str? opened_at=int"
        " opened_by=str? pushed_at=int? merged_at=int? dropped_at=int?"
        " dropped_by=str?",
        match="number repo branch",
        live="merged_at dropped_at",
    ),
    _kind("change_text", "change=int", "body=str"),
    _kind(
        "checks",
        "change=int head=str name=str",
        "state=str detail=str? at=int by=str",
        match="change",
    ),
    _kind(
        "reviews",
        "id=int",
        "target=int round=int kind=str item=int? author=str heads=json pages=json"
        " filed_by=str? reviewer=str? verdict=str? findings=json filed_at=int"
        " verdict_at=int? read_at=int?",
        match="target item",
        live="verdict",
    ),
    _kind(
        "merges",
        "id=int",
        "change=int sha=str commit=str? by=str rule=str at=int",
        match="change",
    ),
    _kind(
        "comments",
        "id=int",
        "number=int? change=int? who=str body=str at=int mark=str? ref=str?"
        " source_ref=str?",
        match="number change",
    ),
    _kind(
        "questions",
        "id=int",
        "number=int run=int? who=str kind=str text=str asked_at=int"
        " answered_at=int? answered_by=str? answer=int?",
        match="number",
        live="answered_at",
    ),
    _kind(
        "statements",
        "id=int",
        "type=str effort=str? number=int? text=str who=str at=int where=str?"
        " supersedes=int?",
        match="effort number type",
    ),
    _kind(
        "holds",
        "id=int",
        "number=int? who=str? cause=str why=str set_at=int set_by=str"
        " lifted_at=int? lifted_by=str?",
        match="number who",
        live="lifted_at",
        one_of="number who",
    ),
    _kind(
        "readings",
        "id=int",
        "source=str kind=str value=json at=int",
        match="source kind",
    ),
    _kind(
        "imports",
        "id=int",
        "source=json format=str started_at=int finished_at=int? counts=json",
        live="finished_at",
    ),
    _kind(
        "pulled",
        "import=int table=str legacy_ref=str",
        "record=str record_key=json at=int",
        match="import record",
    ),
    _kind(
        "left",
        "import=int table=str legacy_ref=str",
        "legacy_key=json? count=int why=str at=int",
        match="import",
    ),
    _kind("beats", "run=int", "at=int", journaled=False),
    _kind("cursors", "consumer=str", "seq=int at=int", journaled=False),
)

#: The kinds whose key is one id from a counter in `meta` (`next:<kind>`).
#: A change's `n` has its own, `next:change:<repo>`.
ID_KINDS = tuple(
    name
    for name, kind in RECORDS.items()
    if kind.key_names in (("id",), ("number",))
    and name not in ("item_text", "run_text", "change_text")
)


def counter_name(kind):
    return f"next:{kind}"


def kind_of(name):
    try:
        return RECORDS[name]
    except (KeyError, TypeError):
        raise SchemaError(f"no kind of record {name!r}") from None


# -- values ---------------------------------------------------------------


def _utf8(value, where):
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError:
        raise SchemaError(f"{where}: not valid UTF-8 (a lone surrogate)") from None


def is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _int(value, where):
    if not is_int(value):
        raise SchemaError(f"{where}: expected int, got {type(value).__name__}")
    if not INT_MIN <= value <= INT_MAX:
        raise SchemaError(f"{where}: {value} is outside signed 64 bits")
    return value


def _str(value, where, limit):
    if not isinstance(value, str):
        raise SchemaError(f"{where}: expected str, got {type(value).__name__}")
    size = len(_utf8(value, where))
    if size > limit:
        raise SchemaError(f"{where}: {size} bytes, past the limit of {limit}")
    return value


def _plain(value, where):
    """`value` as plain data: a copy, with lists for tuples."""
    if value is None or isinstance(value, bool):
        return value
    if is_int(value):
        return _int(value, where)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise SchemaError(f"{where}: a float that is not finite")
        return value
    if isinstance(value, str):
        _utf8(value, where)
        return value
    if isinstance(value, (list, tuple)):
        return [_plain(v, where) for v in value]
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise SchemaError(f"{where}: a dict key is a str, not {k!r}")
            _utf8(k, where)
            out[k] = _plain(v, where)
        return out
    raise SchemaError(f"{where}: {type(value).__name__} is not plain data")


def check_plain(value, where="value"):
    """`value` as plain data (a copy), or a SchemaError."""
    try:
        return _plain(value, where)
    except RecursionError:
        raise SchemaError(f"{where}: nested too deep") from None


def canonical(value):
    """The one encoding of plain data: JSON with sorted keys, no spaces, UTF-8
    text (nothing escaped that need not be), tuples as lists. Two values that
    are equal as plain data encode the same, on either backend."""
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except RecursionError:
        raise SchemaError("nested too deep to encode") from None
    except (TypeError, ValueError) as e:
        raise SchemaError(f"not plain data: {e}") from None


def _floats(value):
    """(how many floats, their JSON text's length) in `value`."""
    count = chars = 0
    stack = [value]
    while stack:
        v = stack.pop()
        if isinstance(v, float):
            count += 1
            chars += len(float.__repr__(v))
        elif isinstance(v, dict):
            stack.extend(v.values())
        elif isinstance(v, (list, tuple)):
            stack.extend(v)
    return count, chars


def size(value):
    """The one measure of plain data on both backends: the UTF-8 length of
    `canonical(value)`, with each float counted as 9 bytes (its msgpack size),
    so it gives the value's msgpack size within a small margin."""
    count, chars = _floats(value)
    return len(canonical(value).encode("utf-8")) - chars + 9 * count


def _json(value, where):
    plain = check_plain(value, where)
    try:
        encoded = size(plain)
    except SchemaError as e:
        raise SchemaError(f"{where}: {e}") from None
    if encoded > LIMITS["json_bytes"]:
        raise SchemaError(
            f"{where}: {encoded} bytes encoded, past the limit of "
            f"{LIMITS['json_bytes']}"
        )
    return plain


# -- labels and flags -----------------------------------------------------


def _words(value, what, limit):
    if not isinstance(value, (list, tuple)):
        raise SchemaError(f"{what} are a list, not {type(value).__name__}")
    if len(value) > limit:
        raise SchemaError(f"{len(value)} {what}, past the limit of {limit}")
    for word in value:
        if not isinstance(word, str):
            raise SchemaError(f"a {what[:-1]} is a str, not {word!r}")
    return list(value)


def check_labels(value):
    """`value` as a list of labels: `key:value`, one label per key, the keys
    `effort` and `state` barred, at most 64, stored sorted."""
    labels = _words(value, "labels", LIMITS["labels"])
    keys = set()
    for label in labels:
        if not LABEL_RE.fullmatch(label):
            raise SchemaError(f"label {label!r} is not key:value")
        key = label.split(":", 1)[0]
        if key in BARRED_LABEL_KEYS:
            raise SchemaError(
                f"label {label!r}: the keys effort and state are not labels "
                "(effort links are records, the state is a field)"
            )
        if key in keys:
            raise SchemaError(f"label {label!r}: one label per key")
        keys.add(key)
    if labels != sorted(labels):
        raise SchemaError("labels are stored sorted")
    return labels


def check_flags(value):
    """`value` as a list of flags: a word, optionally with a `:qualifier`, no
    duplicates, at most 64, stored sorted."""
    flags = _words(value, "flags", LIMITS["flags"])
    for flag in flags:
        if not FLAG_RE.fullmatch(flag):
            raise SchemaError(f"flag {flag!r} is not a word or word:qualifier")
    if len(set(flags)) != len(flags):
        raise SchemaError("a duplicate flag")
    if flags != sorted(flags):
        raise SchemaError("flags are stored sorted")
    return flags


def check_names(value):
    """`value` as a sorted list of names (a node's drivers), each a str."""
    if not isinstance(value, (list, tuple)):
        raise SchemaError(f"names are a list, not {type(value).__name__}")
    for name in value:
        if not isinstance(name, str):
            raise SchemaError(f"a name is a str, not {name!r}")
    if list(value) != sorted(value):
        raise SchemaError("names are stored sorted")
    return list(value)


# -- keys -----------------------------------------------------------------


def _component(value, where):
    if is_int(value):
        return _int(value, where)
    if isinstance(value, str):
        return value
    raise SchemaError(f"{where}: a key component is an int or a str, not {value!r}")


def check_key(kind, key):
    """`key` (a list or tuple) as `kind`'s key: one component per key field, of
    its type, a `str` at most 256 bytes. Returns it as a list."""
    k = kind_of(kind)
    if not isinstance(key, (list, tuple)):
        raise SchemaError(f"{kind}: a key is a list, not {type(key).__name__}")
    if len(key) != len(k.key):
        raise SchemaError(f"{kind}: key {list(key)!r} has {len(k.key)} parts")
    for f, part in zip(k.key, key):
        _key_part(f"{kind}.{f.name}", f, part)
    return list(key)


def _key_part(where, f, value):
    if f.type == INT:
        _int(value, where)
    else:
        _str(value, where, LIMITS["key_bytes"])


def key_of(kind, record):
    """The key of `record`, as a list, in key order."""
    return [record[f.name] for f in kind_of(kind).key]


def sort_keys(keys):
    """`keys` in the one key order, as lists: component by component, an `int`
    numerically and a `str` by code point. SQLite's BINARY collation over UTF-8
    gives the same order, and the Henge backend sorts by the Python tuple."""
    parts = []
    for key in keys:
        if not isinstance(key, (list, tuple)) or not key:
            raise SchemaError(f"a key is a non-empty list, not {key!r}")
        parts.append(tuple(_component(c, "key") for c in key))
    try:
        return [list(k) for k in sorted(parts)]
    except TypeError:
        raise SchemaError("keys of different shapes cannot be ordered") from None


# -- records --------------------------------------------------------------


def _field(kind, f, value):
    where = f"{kind.name}.{f.name}"
    if value is None:
        if not f.null:
            raise SchemaError(f"{where}: null, and the field is not nullable")
        return None
    if f.type == INT:
        value = _int(value, where)
    elif f.type == BOOL:
        if not isinstance(value, bool):
            raise SchemaError(f"{where}: expected bool, got {type(value).__name__}")
    elif f.type == STR:
        limit = LIMITS["str_bytes"]
        if f.name in kind.key_names:
            limit = LIMITS["key_bytes"]
        elif f.name in kind.match:
            limit = LIMITS["match_bytes"]
        value = _str(value, where, limit)
    else:
        value = _json(value, where)
    return value


def validate(kind, record):
    """`record` as a record of `kind`: a copy with every declared field (a
    default or a null filling a missing one), tuples as lists. Refuses an
    undeclared field, a missing one, a wrong type, a value outside `VALUES`
    and every limit."""
    k = kind_of(kind)
    if not isinstance(record, dict):
        raise SchemaError(f"{kind}: a record is a dict, not {type(record).__name__}")
    known = {f.name for f in k.all_fields}
    for name in record:
        if name not in known:
            raise SchemaError(f"{kind}: undeclared field {name!r}")
    out = {}
    for f in k.all_fields:
        if f.name in record:
            value = record[f.name]
        elif f.default is not None:
            value = copy.deepcopy(f.default)
        elif f.null:
            value = None
        else:
            raise SchemaError(f"{kind}.{f.name}: missing")
        out[f.name] = _field(k, f, value)
    for f in k.all_fields:
        values = VALUES.get((kind, f.name))
        if values and out[f.name] is not None and out[f.name] not in values:
            raise SchemaError(
                f"{kind}.{f.name}: {out[f.name]!r} is not one of {', '.join(values)}"
            )
    if k.one_of and sum(out[n] is not None for n in k.one_of) != 1:
        raise SchemaError(f"{kind}: exactly one of {', '.join(k.one_of)} is set")
    for table, check in (
        (LABEL_FIELDS, check_labels),
        (FLAG_FIELDS, check_flags),
        (NAME_FIELDS, check_names),
    ):
        name = table.get(kind)
        if name:
            try:
                out[name] = check(out[name])
            except SchemaError as e:
                raise SchemaError(f"{kind}.{name}: {e}") from None
    return out
