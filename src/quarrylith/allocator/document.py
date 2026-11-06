# SPDX-License-Identifier: Apache-2.0
"""The allocation document: a host's capacity, the consumers that stand on it,
its harness rows, lanes and run classes, read from TOML by `load_document`
and checked whole. A document that breaks a rule is refused with the key
named; a loaded one is read-only data."""

import math
import re
import tomllib
from dataclasses import dataclass, field
from fractions import Fraction
from types import MappingProxyType

from ..record import schema

FORMAT = 1
#: What stands in for a figure the host is asked for, where the document
#: allows it: a capacity of memory or disk, and a consumer's memory.
MEASURE = "measure"
#: The pseudo-resource of a class's needs, expanded through the task's
#: `harness:` label and the document's harness row.
HARNESS = "harness"
#: The model classes a budget weighs.
MODEL_CLASSES = ("small", "standard", "deep")

#: A unit's size in bytes.
SIZES = {"B": 1, "KiB": 2**10, "MiB": 2**20, "GiB": 2**30, "TiB": 2**40}
AMOUNT_RE = re.compile(r"(\d+(?:\.\d+)?)(" + "|".join(SIZES) + ")")
RESOURCE_RE = re.compile(r"[a-z][a-z0-9_.-]*")
#: A harness's name is a label's value and a TOML table's key.
HARNESS_RE = re.compile(r"[a-z][a-z0-9_-]*")

#: The keys a table may hold.
TOP = (
    "format node drivers disk_path capacity harness budget floor drift"
    " consumer lane class"
).split()
#: The tables a document may leave out.
OPTIONAL = ("harness", "consumer", "lane")
MATCH_KEYS = ("kind", "sandbox", "labels", "flags")
CONSUMER_KEYS = ("name", "why", "probe", "path")
PROBES = ("pidfile", "pid", "process")


class DocumentError(ValueError):
    """The document breaks a rule; the message names the key."""


def unit_of(resource):
    """The unit a resource is counted in: bytes for memory and disk (and what
    ends in `.memory` or `.disk`), budget units for `budget.<provider>`, slots
    for the rest."""
    if resource in ("memory", "disk") or resource.endswith((".memory", ".disk")):
        return "bytes"
    if resource.startswith("budget."):
        return "budget"
    return "slots"


def parse_amount(value, unit):
    """`value` as a whole number of `unit`. A whole number stands as it is. A
    string `<number><unit>` (B, KiB, MiB, GiB or TiB) is bytes, rounded up
    and exact, and only for a resource counted in bytes."""
    if isinstance(value, bool):
        raise DocumentError(f"{value!r} is not an amount")
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        raise DocumentError(f"{value!r} is not an amount: a whole number or text")
    found = AMOUNT_RE.fullmatch(value)
    if not found:
        raise DocumentError(
            f"{value!r} is not an amount: <number><unit> with B, KiB, MiB, GiB or TiB"
        )
    if unit != "bytes":
        raise DocumentError(f"{value!r}: a size is for bytes, not {unit}")
    return math.ceil(Fraction(found[1]) * SIZES[found[2]])


def _ro(mapping):
    return MappingProxyType(dict(mapping))


@dataclass(frozen=True)
class Match:
    """What a class holds a task to: `kind` and `sandbox` any of their list;
    `labels` and `flags` all of theirs. Empty matches every task."""

    kind: tuple = ()
    sandbox: tuple = ()
    labels: tuple = ()
    flags: tuple = ()

    def is_empty(self):
        return not (self.kind or self.sandbox or self.labels or self.flags)

    def holds(self, task, labels):
        """Whether `task` (a task record) with its effective `labels`
        matches."""
        return (
            (not self.kind or task["kind"] in self.kind)
            and (not self.sandbox or task["sandbox"] in self.sandbox)
            and all(label in labels for label in self.labels)
            and all(flag in task["flags"] for flag in self.flags)
        )


@dataclass(frozen=True)
class RunClass:
    """A row of `[[class]]`: the needs it slices off the host for the tasks
    its match holds for. A need `harness` is the pseudo-resource."""

    name: str
    match: Match
    needs: MappingProxyType


@dataclass(frozen=True)
class Harness:
    """A row of `[harness.<name>]`: the resource counting its runs at once and
    the one holding its provider's budget share (None for a local one)."""

    name: str
    slots: str
    budget: str | None


@dataclass(frozen=True)
class BudgetRules:
    weight: MappingProxyType  # model class -> weight of one run
    warning_max: int  # weight live on a provider at a warning
    stop_at: float  # utilization from which a provider is stopped
    max_age_s: int  # an older reading guards nothing


@dataclass(frozen=True)
class Floor:
    memory: int  # measured available memory kept free, bytes
    disk: int  # measured free disk kept, bytes
    max_age_s: int  # a host reading older than this is unmeasured


@dataclass(frozen=True)
class Consumer:
    """A standing consumer: what it holds on the host, with a probe for its
    resident size and the path it occupies on disk, where it has them."""

    name: str
    why: str
    amounts: MappingProxyType  # resource -> amount, or MEASURE for memory
    probe: MappingProxyType | None
    path: str | None


@dataclass(frozen=True)
class Lane:
    """A ceiling over the tasks carrying any of its labels."""

    name: str
    labels: tuple
    max: int

    @property
    def resource(self):
        return f"lane.{self.name}"


@dataclass(frozen=True)
class Document:
    node: str
    drivers: tuple
    disk_path: str
    capacity: MappingProxyType  # resource -> amount or MEASURE, lanes included
    harness: MappingProxyType  # name -> Harness
    budget: BudgetRules
    floor: Floor
    drift_tolerance: float
    consumers: tuple = field(default=())
    lanes: tuple = field(default=())
    classes: tuple = field(default=())


# -- reading the document --------------------------------------------------------


def _join(path, key):
    return f"{path}.{key}" if path else str(key)


def _table(path, value, allowed, required=()):
    """`value` as a table of only the `allowed` keys, with the `required`."""
    if not isinstance(value, dict):
        raise DocumentError(f"{path}: a table, not {type(value).__name__}")
    for key in value:
        if key not in allowed:
            raise DocumentError(f"{_join(path, key)}: unknown key")
    for key in required:
        if key not in value:
            raise DocumentError(f"{_join(path, key)}: required")
    return value


def _keys(value):
    """The keys of `value`, any, when it is a table."""
    return value if isinstance(value, dict) else ()


def _text(path, value):
    if not isinstance(value, str) or not value:
        raise DocumentError(f"{path}: a non-empty text, not {value!r}")
    return value


def _name(path, value, pattern=RESOURCE_RE):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise DocumentError(f"{path}: {value!r} is not a name ({pattern.pattern})")
    return value


def _whole(path, value, low, high=None):
    if isinstance(value, bool) or not isinstance(value, int) or value < low:
        raise DocumentError(f"{path}: a whole number from {low}, not {value!r}")
    if high is not None and value > high:
        raise DocumentError(f"{path}: at most {high}, not {value!r}")
    return value


def _amount(path, value, unit, low):
    """An amount of `unit`, from `low`, with the key named on a refusal."""
    try:
        amount = parse_amount(value, unit)
    except DocumentError as error:
        raise DocumentError(f"{path}: {error}") from None
    if amount < low:
        raise DocumentError(f"{path}: from {low}, not {value!r}")
    return amount


def _number(path, value, low, high=None, *, above=False):
    ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    if ok and (value > low if above else value >= low):
        if high is None or value <= high:
            return value
    bound = f"above {low}" if above else f"from {low}"
    tail = f" to {high}" if high is not None else ""
    raise DocumentError(f"{path}: a number {bound}{tail}, not {value!r}")


def _words(path, value, check):
    """A non-empty list of text, each passing `check`."""
    if not isinstance(value, list) or not value:
        raise DocumentError(f"{path}: a non-empty list, not {value!r}")
    out = []
    for word in value:
        if not isinstance(word, str):
            raise DocumentError(f"{path}: {word!r} is not text")
        check(path, word)
        out.append(word)
    return tuple(out)


def _label(path, word):
    if not schema.LABEL_RE.fullmatch(word):
        raise DocumentError(f"{path}: {word!r} is not a key:value label")


def _flag(path, word):
    if not schema.FLAG_RE.fullmatch(word):
        raise DocumentError(f"{path}: {word!r} is not a flag")


def _kind(path, word):
    if word not in schema.VALUES[("tasks", "kind")]:
        known = ", ".join(schema.VALUES[("tasks", "kind")])
        raise DocumentError(f"{path}: {word!r} is not a task kind ({known})")


def _anything(path, word):
    _text(path, word)


def _capacity(raw):
    path = "capacity"
    out = {}
    for resource, value in _table(path, raw, _keys(raw)).items():
        here = _join(path, resource)
        _name(here, resource)
        if resource == HARNESS:
            raise DocumentError(f"{here}: harness is the pseudo-resource of a class")
        if resource.startswith("lane."):
            raise DocumentError(
                f"{here}: a lane's capacity is its max, set by the lane"
            )
        if value == MEASURE and resource in ("memory", "disk"):
            out[resource] = MEASURE
            continue
        out[resource] = _amount(here, value, unit_of(resource), 0)
    return out


def _needs(path, raw, capacity, *, harness=False):
    """A table of resource -> amount from 1, each with a capacity (and the
    pseudo-resource `harness`, where `harness` is true)."""
    if not isinstance(raw, dict) or not raw:
        raise DocumentError(f"{path}: a table of needs, at least one")
    out = {}
    for resource, value in raw.items():
        here = _join(path, resource)
        _name(here, resource)
        if resource == HARNESS and harness:
            out[resource] = _whole(here, value, 1)
            continue
        if resource not in capacity:
            raise DocumentError(f"{here}: {resource} has no capacity")
        out[resource] = _amount(here, value, unit_of(resource), 1)
    return out


def _harness_rows(raw, capacity):
    out = {}
    for name, row in _table("harness", raw, _keys(raw)).items():
        here = _join("harness", name)
        _name(here, name, HARNESS_RE)
        _table(here, row, ("slots", "budget"), ("slots",))
        for key in ("slots", "budget"):
            if key in row and _text(_join(here, key), row[key]) not in capacity:
                raise DocumentError(f"{_join(here, key)}: {row[key]} has no capacity")
        out[name] = Harness(name, row["slots"], row.get("budget"))
    return out


def _budget(raw):
    _table(
        "budget",
        raw,
        ("weight", "warning_max", "stop_at", "max_age_s"),
        ("weight", "warning_max", "stop_at", "max_age_s"),
    )
    weight = _table("budget.weight", raw["weight"], MODEL_CLASSES, MODEL_CLASSES)
    return BudgetRules(
        _ro({m: _whole(f"budget.weight.{m}", weight[m], 1) for m in MODEL_CLASSES}),
        _whole("budget.warning_max", raw["warning_max"], 0),
        _number("budget.stop_at", raw["stop_at"], 0, 1, above=True),
        _whole("budget.max_age_s", raw["max_age_s"], 1),
    )


def _floor(raw):
    keys = ("memory", "disk", "max_age_s")
    _table("floor", raw, keys, keys)
    return Floor(
        _amount("floor.memory", raw["memory"], "bytes", 0),
        _amount("floor.disk", raw["disk"], "bytes", 0),
        _whole("floor.max_age_s", raw["max_age_s"], 1),
    )


def _probe(path, raw):
    if not isinstance(raw, dict) or len(raw) != 1 or next(iter(raw)) not in PROBES:
        raise DocumentError(f"{path}: exactly one of {', '.join(PROBES)}")
    kind, value = next(iter(raw.items()))
    if kind == "pid":
        _whole(f"{path}.pid", value, 1)
    else:
        _text(f"{path}.{kind}", value)
    return _ro(raw)


def _consumers(raw, capacity):
    if not isinstance(raw, list):
        raise DocumentError("consumer: a list of tables")
    out, seen = [], set()
    for row in raw:
        if not isinstance(row, dict) or "name" not in row:
            raise DocumentError("consumer: a table with a name")
        name = _name("consumer.name", row["name"])
        here = _join("consumer", name)
        if name in seen:
            raise DocumentError(f"{here}: two consumers of one name")
        seen.add(name)
        _table(here, row, row, ("name", "why"))
        _text(_join(here, "why"), row["why"])
        probe = row.get("probe")
        if probe is not None:
            probe = _probe(_join(here, "probe"), probe)
        path = row.get("path")
        if path is not None:
            _text(_join(here, "path"), path)
        amounts = {}
        for key, value in row.items():
            if key in CONSUMER_KEYS:
                continue
            where = _join(here, key)
            _name(where, key)
            if key not in capacity:
                raise DocumentError(f"{where}: {key} has no capacity")
            if value == MEASURE and key == "memory":
                if probe is None:
                    raise DocumentError(f"{where}: {MEASURE} needs a probe")
                amounts[key] = MEASURE
                continue
            amounts[key] = _amount(where, value, unit_of(key), 1)
        if not amounts:
            raise DocumentError(f"{here}: at least one amount (memory, disk, ...)")
        out.append(Consumer(name, row["why"], _ro(amounts), probe, path))
    return tuple(out)


def _lanes(raw):
    if not isinstance(raw, list):
        raise DocumentError("lane: a list of tables")
    out, seen = [], set()
    for row in raw:
        if not isinstance(row, dict) or "name" not in row:
            raise DocumentError("lane: a table with a name")
        name = _name("lane.name", row["name"])
        here = _join("lane", name)
        if name in seen:
            raise DocumentError(f"{here}: two lanes of one name")
        seen.add(name)
        _table(here, row, ("name", "labels", "max"), ("labels", "max"))
        labels = _words(_join(here, "labels"), row["labels"], _label)
        out.append(Lane(name, labels, _whole(_join(here, "max"), row["max"], 0)))
    return tuple(out)


def _match(path, raw):
    _table(path, raw, MATCH_KEYS)
    checks = {"kind": _kind, "sandbox": _anything, "labels": _label, "flags": _flag}
    return Match(**{k: _words(_join(path, k), raw[k], checks[k]) for k in raw})


def _classes(raw, capacity):
    if not isinstance(raw, list) or not raw:
        raise DocumentError("class: at least one class")
    out, seen = [], set()
    for row in raw:
        if not isinstance(row, dict) or "name" not in row:
            raise DocumentError("class: a table with a name")
        name = _name("class.name", row["name"])
        here = _join("class", name)
        if name in seen:
            raise DocumentError(f"{here}: two classes of one name")
        seen.add(name)
        _table(here, row, ("name", "match", "needs"), ("match", "needs"))
        match = _match(_join(here, "match"), row["match"])
        needs = _needs(_join(here, "needs"), row["needs"], capacity, harness=True)
        out.append(RunClass(name, match, _ro(needs)))
    return tuple(out)


def load_document(text):
    """The allocation document in `text` (TOML), checked whole. Raises
    `DocumentError` naming the key that breaks a rule."""
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise DocumentError(f"not a TOML document: {error}") from None
    return document_from_data(raw)


def document_from_data(raw):
    """The document in `raw`, plain data shaped as the TOML reads (what
    `to_data` gives, once through JSON), checked whole. Amounts may be whole
    numbers, and a "measure" only where the sample shows it."""
    _table(
        "",
        raw,
        TOP,
        ("format", "node", "drivers", "capacity", "class")
        + ("budget", "floor", "drift"),
    )
    if raw["format"] != FORMAT or isinstance(raw["format"], bool):
        raise DocumentError(f"format: {FORMAT}, not {raw['format']!r}")
    node = _name("node", raw["node"])
    drivers = _words("drivers", raw["drivers"], _anything)
    disk_path = _text("disk_path", raw.get("disk_path", "."))
    capacity = _capacity(raw["capacity"])
    lanes = _lanes(raw.get("lane", []))
    for lane in lanes:
        capacity[lane.resource] = lane.max
    harness = _harness_rows(raw.get("harness", {}), capacity)
    consumers = _consumers(raw.get("consumer", []), capacity)
    classes = _classes(raw["class"], capacity)
    drift = _table("drift", raw["drift"], ("tolerance",), ("tolerance",))
    return Document(
        node=node,
        drivers=drivers,
        disk_path=disk_path,
        capacity=_ro(capacity),
        harness=_ro(harness),
        budget=_budget(raw["budget"]),
        floor=_floor(raw["floor"]),
        drift_tolerance=_number("drift.tolerance", drift["tolerance"], 0),
        consumers=consumers,
        lanes=lanes,
        classes=classes,
    )


# -- the document as plain data ----------------------------------------------------


def _figure(where, value, figure):
    """`value`, or for a "measure" its `figure` (a whole number from 0), with
    the key named when there is none."""
    if value != MEASURE:
        return value
    if figure is None:
        raise DocumentError(f"{where}: {MEASURE} has no figure")
    if isinstance(figure, bool) or not isinstance(figure, int) or figure < 0:
        raise DocumentError(
            f"{where}: a figure is a whole number from 0, not {figure!r}"
        )
    return figure


def to_data(document, figures=None):
    """The document as plain data shaped as the TOML reads it, every amount a
    whole number: JSON-safe, and what `document_from_data` loads back. With
    `figures` (`{"capacity": {resource: amount}, "consumers": {name:
    {resource: amount}}}`) each "measure" is its figure, and a "measure" with
    none is refused (`DocumentError`, naming the key); without, it stays."""
    capacity = figures.get("capacity", {}) if figures is not None else None
    consumers = figures.get("consumers", {}) if figures is not None else None

    def measured(where, value, found):
        return value if figures is None else _figure(where, value, found)

    data = {
        "format": FORMAT,
        "node": document.node,
        "drivers": list(document.drivers),
        "disk_path": document.disk_path,
        "capacity": {
            resource: measured(
                _join("capacity", resource),
                value,
                (capacity or {}).get(resource),
            )
            for resource, value in document.capacity.items()
            if not resource.startswith("lane.")
        },
        "harness": {
            row.name: {
                "slots": row.slots,
                **({"budget": row.budget} if row.budget is not None else {}),
            }
            for row in document.harness.values()
        },
        "budget": {
            "weight": dict(document.budget.weight),
            "warning_max": document.budget.warning_max,
            "stop_at": document.budget.stop_at,
            "max_age_s": document.budget.max_age_s,
        },
        "floor": {
            "memory": document.floor.memory,
            "disk": document.floor.disk,
            "max_age_s": document.floor.max_age_s,
        },
        "drift": {"tolerance": document.drift_tolerance},
        "consumer": [
            _consumer_data(c, consumers, measured) for c in document.consumers
        ],
        "lane": [
            {"name": lane.name, "labels": list(lane.labels), "max": lane.max}
            for lane in document.lanes
        ],
        "class": [
            {
                "name": run_class.name,
                "match": {
                    key: list(getattr(run_class.match, key))
                    for key in MATCH_KEYS
                    if getattr(run_class.match, key)
                },
                "needs": dict(run_class.needs),
            }
            for run_class in document.classes
        ],
    }
    for key in OPTIONAL:
        if not data[key]:
            del data[key]
    return data


def _consumer_data(consumer, figures, measured):
    row = {"name": consumer.name, "why": consumer.why}
    for resource, value in consumer.amounts.items():
        found = ((figures or {}).get(consumer.name) or {}).get(resource)
        where = _join(_join("consumer", consumer.name), resource)
        row[resource] = measured(where, value, found)
    if consumer.probe is not None:
        row["probe"] = dict(consumer.probe)
    if consumer.path is not None:
        row["path"] = consumer.path
    return row
