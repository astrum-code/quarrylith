# SPDX-License-Identifier: Apache-2.0
"""The allocation document (templet:3161, build step 1): `load_document` reads
the sample, turns amounts into bytes, and refuses a document with the key
named."""

import math
from pathlib import Path

import pytest

from quarrylith.allocator import (
    Document,
    DocumentError,
    load_document,
    parse_amount,
    unit_of,
)

SAMPLE = Path(__file__).resolve().parent / "data" / "allocation-sample.toml"
GIB = 2**30


def sample_text():
    return SAMPLE.read_text()


def test_load_document_reads_the_sample():
    """templet:3161 -- `load_document` reads the sample: the node, its
    drivers, every capacity (a "measure" kept as it is), the harness rows, the
    budget, floor and drift rules, the consumers, the lane and the classes in
    document order, with every amount in bytes or whole units."""
    doc = load_document(sample_text())
    assert isinstance(doc, Document)
    assert doc.node == "laptop"
    assert doc.drivers == ("harness", "podman", "process", "vm")
    assert doc.disk_path == "."
    assert doc.capacity["memory"] == "measure"
    assert doc.capacity["disk"] == "measure"
    assert doc.capacity["slots.claude-code"] == 4
    assert doc.capacity["slots.local"] == 0
    assert doc.capacity["podman.memory"] == 14 * GIB
    assert doc.capacity["lane.widgets"] == 4
    assert doc.harness["codex"].slots == "slots.codex"
    assert doc.harness["codex"].budget == "budget.openai"
    assert doc.harness["local"].budget is None
    assert doc.budget.weight == {"small": 1, "standard": 1, "deep": 1}
    assert (doc.budget.warning_max, doc.budget.stop_at) == (2, 0.95)
    assert doc.budget.max_age_s == 900
    assert doc.floor.memory == 4 * GIB
    assert doc.floor.disk == 20 * GIB
    assert doc.floor.max_age_s == 180
    assert doc.drift_tolerance == 0.10
    assert [c.name for c in doc.consumers] == ["desktop", "podman-machine", "yard"]
    assert doc.consumers[0].amounts == {"memory": 10 * GIB}
    assert doc.consumers[1].amounts == {
        "memory": math.ceil(15.4 * GIB),
        "disk": 40 * GIB,
    }
    assert doc.consumers[1].probe == {"process": "machine-hypervisor"}
    assert doc.consumers[1].path == "machine/disk.img"
    assert doc.consumers[2].amounts == {"memory": "measure"}
    assert doc.consumers[2].probe == {"pidfile": "yard/supervisor.pid"}
    assert [lane.name for lane in doc.lanes] == ["widgets"]
    assert doc.lanes[0].resource == "lane.widgets"
    assert doc.lanes[0].labels == ("repo:widgets",)
    assert doc.lanes[0].max == 4
    assert [c.name for c in doc.classes] == [
        "vm.smoke",
        "vm",
        "container",
        "local",
        "agent",
        "gate",
        "job",
    ]
    assert doc.classes[0].needs == {
        "memory": 5 * GIB,
        "disk": 10 * GIB,
        "lab.smoke": 1,
    }
    assert doc.classes[3].needs["harness"] == 1
    assert doc.classes[-1].match.is_empty()


def test_amounts_with_units_become_bytes_rounded_up():
    """templet:3161 -- `parse_amount` turns `<number><unit>` into bytes, with
    B, KiB, MiB, GiB and TiB, rounded up and exact (15.4 GiB is not a float's
    guess), and passes a whole number through; a unit on a resource that is
    not counted in bytes, a float, a bool, a space and another unit are
    refused."""
    assert parse_amount("1B", "bytes") == 1
    assert parse_amount("1KiB", "bytes") == 1024
    assert parse_amount("512MiB", "bytes") == 512 * 2**20
    assert parse_amount("14GiB", "bytes") == 14 * GIB
    assert parse_amount("2TiB", "bytes") == 2 * 2**40
    assert parse_amount("15.4GiB", "bytes") == 16535624090  # 16535624089.6 up
    assert parse_amount("0.5B", "bytes") == 1
    assert parse_amount("0GiB", "bytes") == 0
    assert parse_amount("1.0000000001B", "bytes") == 2
    assert parse_amount(7, "bytes") == 7
    assert parse_amount(3, "slots") == 3
    for value, unit in (
        ("2GiB", "slots"),
        ("2GiB", "budget"),
        (1.5, "bytes"),
        (True, "slots"),
        ("1 GiB", "bytes"),
        ("1GB", "bytes"),
        ("-1GiB", "bytes"),
        ("GiB", "bytes"),
        ("", "bytes"),
        (None, "bytes"),
        ([1], "bytes"),
    ):
        with pytest.raises(DocumentError):
            parse_amount(value, unit)


def test_a_resource_is_counted_in_its_unit():
    """templet:3161 -- memory, disk and the resources ending in `.memory` or
    `.disk` are bytes, `budget.<provider>` is budget units, and the rest are
    slots."""
    assert unit_of("memory") == "bytes"
    assert unit_of("disk") == "bytes"
    assert unit_of("podman.memory") == "bytes"
    assert unit_of("budget.anthropic") == "budget"
    assert unit_of("slots.codex") == "slots"
    assert unit_of("lab") == "slots"
    assert unit_of("lane.widgets") == "slots"


def append(extra):
    return lambda text: text + "\n" + extra


def put_first(extra):
    return lambda text: extra + "\n" + text


def swap(old, new):
    def mutate(text):
        assert old in text, old
        return text.replace(old, new)

    return mutate


CONSUMER = '[[consumer]]\nname = "{}"\nwhy = "w"\n{}\n'
CLASS = '[[class]]\nname = "{}"\nmatch = {{}}\nneeds = {}\n'
LANE = '[[lane]]\nname = "{}"\n{}\n'

#: (id, how the sample is changed, the key the refusal names)
REFUSED = [
    ("not-toml", lambda text: "this is [ not toml", "TOML"),
    ("unknown-key", put_first("bogus = 1"), "bogus"),
    (
        "unknown-key-in-budget",
        swap("stop_at = 0.95", "stop_at = 0.95\nbogus = 1"),
        "budget",
    ),
    (
        "unknown-key-in-floor",
        swap("max_age_s = 180", "max_age_s = 180\nbogus = 1"),
        "floor",
    ),
    (
        "unknown-key-in-drift",
        swap("tolerance = 0.10", "tolerance = 0.10\nbogus = 1"),
        "drift",
    ),
    ("unknown-key-in-class", swap('name = "job"', 'name = "job"\nbogus = 1'), "bogus"),
    ("unknown-match-key", swap("match = {}", "match = { colour = [1] }"), "colour"),
    ("unknown-lane-key", swap("max = 4", "max = 4\nbogus = 1"), "lane"),
    ("format", swap("format = 1", "format = 2"), "format"),
    ("no-format", swap("format = 1", ""), "format"),
    ("no-node", swap('node = "laptop"', ""), "node"),
    ("resource-grammar", swap("lab = 2", '"Lab" = 2'), "Lab"),
    ("resource-grammar-digit", swap("lab = 2", '"9lab" = 2'), "9lab"),
    ("resource-grammar-in-class", swap("lab = 1", '"Lab" = 1'), "Lab"),
    ("need-below-one", swap("lab = 1", "lab = 0"), "lab"),
    ("need-negative", swap("lab = 1", "lab = -1"), "lab"),
    ("harness-need-below-one", swap("harness = 1 }", "harness = 0 }"), "harness"),
    (
        "negative-capacity",
        swap('"slots.codex" = 3', '"slots.codex" = -1'),
        "slots.codex",
    ),
    ("float-capacity", swap('"slots.codex" = 3', '"slots.codex" = 1.5'), "slots.codex"),
    ("bool-capacity", swap('"slots.codex" = 3', '"slots.codex" = true'), "slots.codex"),
    ("unit-on-slots", swap("lab = 2", 'lab = "2GiB"'), "lab"),
    (
        "bad-amount",
        swap('"podman.memory" = "14GiB"', '"podman.memory" = "14GB"'),
        "podman.memory",
    ),
    ("two-consumers", append(CONSUMER.format("desktop", "memory = 1")), "desktop"),
    ("two-classes", append(CLASS.format("job", "{ memory = 1 }")), "job"),
    (
        "two-lanes",
        append(LANE.format("widgets", 'labels = ["repo:b"]\nmax = 1')),
        "widgets",
    ),
    ("lane-no-labels", append(LANE.format("gadgets", "max = 1")), "gadgets"),
    (
        "lane-empty-labels",
        append(LANE.format("gadgets", "labels = []\nmax = 1")),
        "gadgets",
    ),
    (
        "lane-bad-label",
        append(LANE.format("gadgets", 'labels = ["repo"]\nmax = 1')),
        "gadgets",
    ),
    ("lane-no-max", append(LANE.format("gadgets", 'labels = ["repo:b"]')), "gadgets"),
    (
        "lane-negative-max",
        append(LANE.format("gadgets", 'labels = ["repo:b"]\nmax = -1')),
        "gadgets",
    ),
    (
        "lane-in-capacity",
        swap("lab = 2", 'lab = 2\n"lane.widgets" = 9'),
        "lane.widgets",
    ),
    ("harness-in-capacity", swap("lab = 2", "lab = 2\nharness = 9"), "harness"),
    ("class-no-capacity", append(CLASS.format("odd", "{ gpu = 1 }")), "gpu"),
    ("consumer-no-capacity", append(CONSUMER.format("odd", "gpu = 1")), "gpu"),
    (
        "harness-row-no-capacity",
        swap('slots = "slots.codex"', 'slots = "slots.nope"'),
        "slots.nope",
    ),
    (
        "harness-budget-no-capacity",
        swap('budget = "budget.openai"', 'budget = "budget.nope"'),
        "budget.nope",
    ),
    ("harness-no-slots", swap('slots = "slots.codex"\n', ""), "codex"),
    ("measure-in-class", swap("lab = 1", 'lab = "measure"'), "lab"),
    ("measure-in-slots-capacity", swap("lab = 2", 'lab = "measure"'), "lab"),
    ("measure-in-consumer-disk", swap('disk = "40GiB"', 'disk = "measure"'), "disk"),
    (
        "measure-consumer-without-probe",
        append(CONSUMER.format("odd", 'memory = "measure"')),
        "odd",
    ),
    ("measure-in-floor", swap('disk = "20GiB"', 'disk = "measure"'), "disk"),
    ("consumer-no-amount", append(CONSUMER.format("odd", "")), "odd"),
    ("consumer-no-why", append('[[consumer]]\nname = "odd"\nmemory = 1\n'), "odd"),
    ("consumer-zero-amount", append(CONSUMER.format("odd", "memory = 0")), "odd"),
    (
        "probe-two-kinds",
        append(
            CONSUMER.format("odd", 'memory = 1\nprobe = { pid = 4, process = "x" }')
        ),
        "odd",
    ),
    (
        "probe-bad-pid",
        append(CONSUMER.format("odd", 'memory = 1\nprobe = { pid = "4" }')),
        "odd",
    ),
    (
        "probe-unknown",
        append(CONSUMER.format("odd", 'memory = 1\nprobe = { socket = "x" }')),
        "odd",
    ),
    ("class-no-needs", append('[[class]]\nname = "odd"\nmatch = {}\n'), "odd"),
    ("class-empty-needs", append(CLASS.format("odd", "{}")), "odd"),
    (
        "class-no-match",
        append('[[class]]\nname = "odd"\nneeds = { memory = 1 }\n'),
        "odd",
    ),
    ("match-empty-list", swap('sandbox = ["podman"]', "sandbox = []"), "sandbox"),
    ("match-unknown-kind", swap('kind = ["gate"]', 'kind = ["wizard"]'), "wizard"),
    (
        "match-bad-label",
        swap('labels = ["harness:local"]', 'labels = ["local"]'),
        "local",
    ),
    ("match-not-a-list", swap('sandbox = ["podman"]', 'sandbox = "podman"'), "sandbox"),
    ("no-classes", lambda text: text[: text.index("[[class]]")], "class"),
    ("negative-weight", swap("small = 1", "small = 0"), "small"),
    (
        "missing-weight",
        swap(
            "weight = { small = 1, standard = 1, deep = 1 }",
            "weight = { small = 1, standard = 1 }",
        ),
        "deep",
    ),
    ("stop-at-above-one", swap("stop_at = 0.95", "stop_at = 1.5"), "stop_at"),
    ("negative-tolerance", swap("tolerance = 0.10", "tolerance = -0.1"), "tolerance"),
    (
        "drivers-not-a-list",
        swap('drivers = ["harness", "podman", "process", "vm"]', 'drivers = "vm"'),
        "drivers",
    ),
    ("class-name-grammar", swap('name = "job"', 'name = "Job"'), "Job"),
]


@pytest.mark.parametrize(
    "mutate, key",
    [pytest.param(m, k, id=i) for i, m, k in REFUSED],
)
def test_load_document_refuses_a_bad_document_naming_the_key(mutate, key):
    """templet:3161 -- a document with an unknown key, a resource outside the
    grammar, a need below 1, a negative capacity, two consumers or two classes
    or two lanes of one name, a lane with no labels, a `[capacity]` key naming
    a `lane.` resource, a class or consumer naming a resource with no
    capacity, or a "measure" where none is allowed is refused, with the key
    named."""
    text = mutate(sample_text())
    if callable(key):  # pragma: no cover - the table holds strings
        key = key(text)
    with pytest.raises(DocumentError) as raised:
        load_document(text)
    assert key in str(raised.value)


def test_a_document_error_is_a_value_error():
    """templet:3161 -- a refused document raises `DocumentError`, a
    `ValueError`, and the sample loads again after one was refused."""
    with pytest.raises(ValueError):
        load_document("format = 2")
    assert load_document(sample_text()).node == "laptop"


def test_a_loaded_document_is_read_only_data():
    """templet:3161 -- the document does not change after it is loaded: its
    dataclasses are frozen."""
    doc = load_document(sample_text())
    with pytest.raises(AttributeError):
        doc.node = "other"
    with pytest.raises(AttributeError):
        doc.classes[0].name = "other"
