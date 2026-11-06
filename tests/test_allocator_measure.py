# SPDX-License-Identifier: Apache-2.0
"""The allocator's measurement (templet:3161, build step 4): the probes of the
host's memory, disk and processes, `measure` and the one reading it writes,
the measured check of `place` and `hold`, `unmeasured`, drift and its
observations, `sample_consumer` and `resolve`. The probes run for real on the
test host (read-only); the Linux reader is tested on a captured file shape.
Every test that touches a record runs over every backend in BACKENDS."""

import json
import math
import os
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest
from backends import over_backends
from test_allocator_host import (
    FIGURES,
    GIB,
    host_reading,
    make_task,
    sample_text,
)

from quarrylith import gauges
from quarrylith.allocator import Fit, Refusal, load_document
from quarrylith.allocator.host import HostPlacement, ProbeError
from quarrylith.record.contract import Refused

DATA = Path(__file__).resolve().parent / "data"
MIB = 2**20
MIN = 60_000

#: A host with a node, a work folder and two probed consumers. `{tolerance}`
#: and `{consumers}` are filled by `mini`.
MINI = """
format = 1
node = "box"
drivers = ["process"]
disk_path = "{disk_path}"
[capacity]
memory = "measure"
disk = "measure"
[budget]
weight = {{ small = 1, standard = 1, deep = 1 }}
warning_max = 2
stop_at = 0.95
max_age_s = 900
[floor]
memory = "4GiB"
disk = "20GiB"
max_age_s = 180
[drift]
tolerance = {tolerance}
{consumers}
[[class]]
name = "job"
match = {{}}
needs = {{ memory = "1GiB", disk = "1GiB" }}
"""

ME = (
    '[[consumer]]\nname = "me"\nmemory = "4GiB"\n'
    'probe = {{ pid = {pid} }}\nwhy = "test"\n'
)


def mini(tmp_path, consumers="", tolerance=1000):
    return load_document(
        MINI.format(disk_path=tmp_path, tolerance=tolerance, consumers=consumers)
    )


def real(placement, document, now):
    """The figures the real probes give for `document` (no consumer is sampled)."""
    return placement.resolve(document, now=now)


def files(record, kind):
    return record.read("readings", source="host", kind=kind)


def observations(record):
    return [
        s["text"]
        for s in record.read("statements")
        if s["type"] == "observation" and s["where"] == "allocator"
    ]


# -- the probes ---------------------------------------------------------------------


def test_the_parsers_read_captured_samples():
    """templet:3161 -- the parsers read `vm_stat`, `/proc/meminfo` and `ps`
    samples: pages of free, inactive, purgeable and speculative memory at the
    page size the output names; `MemAvailable` in kB; and one row per process
    with its resident size in bytes."""
    from quarrylith.allocator import probes

    text = (DATA / "vm_stat-sample.txt").read_text()
    pages = {
        line.split(":")[0]: int(line.split(":")[1].strip().rstrip("."))
        for line in text.splitlines()[1:]
        if line.split(":")[0]
        in ("Pages free", "Pages inactive", "Pages purgeable", "Pages speculative")
    }
    assert len(pages) == 4
    assert probes.parse_vm_stat(text) == sum(pages.values()) * 16384
    total, available = probes.parse_meminfo((DATA / "meminfo-sample.txt").read_text())
    assert (total, available) == (16318412 * 1024, 11523416 * 1024)
    table = probes.parse_ps((DATA / "ps-sample.txt").read_text())
    assert len(table) == 7
    assert table[3] == probes.Process(102, 100, 10240 * 1024, "/usr/bin/service one")
    assert probes.probe_tree(100, table) == (20480 + 40960 + 10240 + 2048) * 1024
    assert probes.probe_tree(103, table) == 2048 * 1024
    assert probes.probe_tree(999, table) == 0
    assert probes.probe_process("other-daemon", table) == (51200 + 1024) * 1024
    assert probes.probe_process("nothing", table) == 0


def test_the_parsers_refuse_what_they_cannot_read():
    """templet:3161 -- a probe that cannot read its input raises `ProbeError`
    naming the probe, never a figure of zero."""
    from quarrylith.allocator import probes

    for call, text, name in (
        (probes.parse_vm_stat, "nothing here", "vm_stat"),
        (probes.parse_meminfo, "MemTotal: 5 kB\n", "meminfo"),
        (probes.parse_ps, "1 0 notanumber x\n", "ps"),
    ):
        with pytest.raises(ProbeError, match=name):
            call(text)


def test_the_probes_run_for_real_and_read_only():
    """templet:3161 -- on the test host: memory total and available with the
    method named, the work disk's free bytes, a file's allocated size (a
    sparse file counts what it holds), this process's tree and a process by
    name. Each is a figure and nothing else."""
    from quarrylith.allocator import probes

    memory = probes.probe_memory()
    assert set(memory) == {"memory_total", "memory_available", "method"}
    assert memory["method"] == ("vm_stat" if sys.platform == "darwin" else "meminfo")
    assert 0 < memory["memory_available"] <= memory["memory_total"]
    assert probes.probe_disk(".") > 0
    assert probes.probe_tree(os.getpid()) > 10 * MIB
    assert probes.probe_process("python") > 10 * MIB
    assert probes.probe_size("/nonexistent/path/to/nothing") == 0


def test_probe_size_counts_allocated_bytes(tmp_path):
    """templet:3161 -- the size of a sparse file is what it holds, a folder's
    is the sum of its files, and a folder past the bound is a `ProbeError`."""
    from quarrylith.allocator import probes

    sparse = tmp_path / "sparse.img"
    with open(sparse, "wb") as handle:
        handle.truncate(512 * MIB)
        handle.seek(0)
        handle.write(b"x" * 4096)
    assert probes.probe_size(str(sparse)) < 4 * MIB
    folder = tmp_path / "folder"
    folder.mkdir()
    for number in range(3):
        (folder / f"f{number}").write_bytes(b"y" * 100_000)
    assert 300_000 <= probes.probe_size(str(folder)) < 3 * MIB
    with pytest.raises(ProbeError, match="size"):
        probes.probe_size(str(folder), most=2)


def test_no_probe_keeps_a_command_line(tmp_path):
    """templet:3161 -- a process's command line is read by no probe and kept
    nowhere: a child started with a distinctive argument is counted, and the
    rows `ps` gave, the figures and the reading `measure` writes hold nothing
    but numbers and the consumer's own name."""
    from quarrylith.allocator import probes

    marker = "distinct-argument-9f3c1"
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", marker]
    )
    try:
        for _ in range(100):  # until the child is running python
            table = probes.process_table()
            if probes.probe_tree(child.pid, table) > MIB:
                break
            time.sleep(0.05)
        row = next(p for p in table if p.pid == child.pid)
        assert marker not in row.comm
        assert all(marker not in p.comm for p in table)
        assert probes.probe_tree(child.pid, table) > MIB
        assert isinstance(probes.probe_process("python", table), int)
    finally:
        child.kill()
        child.wait()


def test_a_failing_probe_raises_naming_the_probe(tmp_path, monkeypatch):
    """templet:3161 -- a probe that fails raises `ProbeError` naming it: a
    work folder that is not there, a `ps` that fails, and `ps` output past the
    bound."""
    from quarrylith.allocator import probes

    with pytest.raises(ProbeError, match="probe_disk"):
        probes.probe_disk(str(tmp_path / "gone"))
    monkeypatch.setattr(probes, "PS", ["false"])
    with pytest.raises(ProbeError, match="ps"):
        probes.process_table()
    monkeypatch.setattr(probes, "PS", ["ps", "-A", "-o", "pid=,ppid=,rss=,comm="])
    monkeypatch.setattr(probes, "MAX_BYTES", 100)
    with pytest.raises(ProbeError, match="ps"):
        probes.process_table()


# -- measure ------------------------------------------------------------------------


@over_backends
def test_measure_writes_one_reading_of_real_figures(record, clock, tmp_path):
    """templet:3161 -- `measure` writes one reading with the node, total and
    available memory, the method named, the work disk's free bytes and each
    probed consumer's resident size, from the real probes of the test host.
    The reading shows in the `measured` gauge."""
    from quarrylith.allocator import probes

    document = mini(tmp_path, ME.format(pid=os.getpid()))
    placement = HostPlacement(record)
    result = placement.declare(
        document, figures=real(placement, document, clock()), now=clock()
    )
    assert result.ok, result
    head = record.head()
    clock.step()
    value = placement.measure(now=clock())
    assert record.head() == head + 1
    (made,) = files(record, "measure")
    assert made["value"] == value and made["at"] == clock()
    assert value["node"] == "box"
    assert value["method"] == ("vm_stat" if sys.platform == "darwin" else "meminfo")
    assert 0 < value["memory_available"] <= value["memory_total"]
    assert abs(value["disk_free"] - probes.probe_disk(str(tmp_path))) < 8 * GIB
    assert set(value["consumers"]) == {"me"}
    assert value["consumers"]["me"]["memory"] > 10 * MIB
    assert value["drift"] == []
    for found in (value["memory_total"], value["disk_free"]):
        assert isinstance(found, int)
    got = dict(
        ((lab["node"], lab["resource"], lab["holder"]), v)
        for lab, v in gauges.by_name("measured").compute(
            record, now=clock(), window_ms=MIN
        )
    )
    assert got[("box", "memory", "")] == value["memory_available"]
    assert got[("box", "disk", "")] == value["disk_free"]
    assert got[("box", "memory", "me")] == value["consumers"]["me"]["memory"]
    assert "distinct" not in json.dumps(value)


@over_backends
def test_a_failing_probe_writes_no_reading(record, clock, tmp_path):
    """templet:3161 -- a probe that fails writes no reading and raises
    `ProbeError` naming it: the work disk is gone, so `measure` says
    `probe_disk` and the head does not move."""
    document = mini(tmp_path / "work")
    (tmp_path / "work").mkdir()
    placement = HostPlacement(record)
    assert placement.declare(
        document, figures=real(placement, document, clock()), now=clock()
    ).ok
    (tmp_path / "work").rmdir()
    head = record.head()
    with pytest.raises(ProbeError, match="probe_disk"):
        placement.measure(now=clock())
    assert record.head() == head and files(record, "measure") == []


@over_backends
def test_measure_with_no_document_refuses(record, clock):
    """templet:3161 -- before any `declare` nothing names the node, the work
    disk or the consumers, so `measure` refuses (the record's `Refused`) and
    writes nothing."""
    head = record.head()
    with pytest.raises(Refused, match="document"):
        HostPlacement(record).measure(now=clock())
    assert record.head() == head


# -- the measured check -------------------------------------------------------------


@over_backends
def test_a_start_that_would_pass_the_floor_is_refused_measured(effort, clock):
    """templet:3161 -- a start whose need would leave measured available memory
    under `floor.memory` (4 GiB), or measured free disk under `floor.disk`
    (20 GiB), is refused `measured`, with waits true, although the table has
    room. `hold` refuses the same, unless `measured` is False."""
    from test_allocator_host import declared

    record = effort
    placement = declared(record, clock)
    clock.step()
    host_reading(record, clock, available=5 * GIB, disk_free=300 * GIB)
    task = make_task(record)
    answer = placement.place(task, now=clock())
    assert isinstance(answer, Refusal)
    (short,) = answer.shorts
    assert (short.cause, short.resource) == ("measured", "memory")
    assert (short.asked, short.free, short.capacity) == (2 * GIB, GIB, 48 * GIB)
    assert short.waits is True and answer.waits is True and answer.until is None
    assert "[measured]" in answer.text
    clock.step()
    host_reading(record, clock, available=40 * GIB, disk_free=23 * GIB)
    answer = placement.place(task, now=clock())
    (short,) = answer.shorts
    assert (short.cause, short.resource) == ("measured", "disk")
    assert (short.asked, short.free) == (4 * GIB, 3 * GIB)
    refusal = placement.hold("later", {"disk": 4 * GIB}, now=clock())
    assert [(s.cause, s.resource) for s in refusal.shorts] == [("measured", "disk")]
    assert placement.hold("later", {"disk": 4 * GIB}, measured=False, now=clock())
    clock.step()
    host_reading(record, clock, available=40 * GIB, disk_free=30 * GIB)
    assert isinstance(placement.place(task, now=clock()), Fit)
    assert placement.place(task, now=clock()).needs["memory"] == 2 * GIB


@over_backends
def test_headroom_takes_the_measured_room(effort, clock):
    """templet:3161 -- `headroom` of memory and disk on the declared node is
    the least of the table's free and the measured room above the floor, and
    0 while the host is unmeasured."""
    from test_allocator_host import declared

    record = effort
    placement = declared(record, clock, load_document(sample_text(180)))
    table = placement.headroom(now=clock())["laptop"]
    clock.step()
    host_reading(record, clock, available=9 * GIB, disk_free=25 * GIB)
    room = placement.headroom(now=clock())["laptop"]
    assert room["memory"] == 5 * GIB and room["disk"] == 5 * GIB
    assert room["slots.claude-code"] == table["slots.claude-code"]
    clock.step(10 * MIN)
    room = placement.headroom(now=clock())["laptop"]
    assert room["memory"] == 0 and room["disk"] == 0
    assert room["slots.claude-code"] == table["slots.claude-code"]


# -- unmeasured ---------------------------------------------------------------------


@over_backends
def test_with_no_fresh_host_reading_place_refuses_unmeasured(effort, clock):
    """templet:3161 -- with no host reading, one older than `floor.max_age_s`,
    one of another node, or one that is not readable, `place` refuses
    `unmeasured`: one short with `resource` None and waits true, and `hold`
    the same. It never passes on an assumption."""
    record = effort
    text = sample_text(180)
    placement = HostPlacement(record)
    assert placement.declare(load_document(text), figures=FIGURES, now=clock()).ok
    task = make_task(record)

    def causes():
        answer = placement.place(task, now=clock())
        assert isinstance(answer, Refusal)
        (short,) = answer.shorts
        assert (short.cause, short.resource, short.waits) == ("unmeasured", None, True)
        assert short.node == "laptop" and answer.waits is True
        assert "unmeasured" in answer.text
        held = placement.hold("x", {"memory": 1}, now=clock())
        assert [s.cause for s in held.shorts] == ["unmeasured"]

    causes()
    host_reading(record, clock, node="elsewhere")
    causes()
    record.record_reading("host", "measure", {"node": "laptop"})
    causes()
    clock.step()
    host_reading(record, clock)
    assert isinstance(placement.place(task, now=clock()), Fit)
    clock.step(180_000)
    assert isinstance(placement.place(task, now=clock()), Fit)
    clock.step(1)
    causes()


# -- drift --------------------------------------------------------------------------


def stub(memory, resident=None):
    """The probes, with the host's figures given: the memory, a free disk and
    one process (pid 10) of `resident` bytes."""
    from quarrylith.allocator import probes

    table = [] if resident is None else [probes.Process(10, 1, resident, "x")]
    return types.SimpleNamespace(
        probe_memory=lambda: {
            "memory_total": 100 * GIB,
            "memory_available": memory,
            "method": "given",
        },
        probe_disk=lambda path: 500 * GIB,
        probe_size=lambda path: 0,
        process_table=lambda: table,
        resident=probes.resident,
    )


DRIFT = """
[[consumer]]
name = "machine"
memory = "10GiB"
probe = { pid = 10 }
why = "a consumer"
"""


def drifting(record, clock, tmp_path):
    document = mini(tmp_path, DRIFT, tolerance=0.1)
    figures = {"capacity": {"memory": 100 * GIB, "disk": 400 * GIB}, "consumers": {}}
    placement = HostPlacement(record, probes=stub(90 * GIB, 10 * GIB))
    assert placement.declare(document, figures=figures, now=clock()).ok
    return placement


@over_backends
def test_a_consumer_past_its_declaration_and_undeclared_use_are_drift(
    record, clock, tmp_path
):
    """templet:3161 -- a consumer measured past its declaration by more than
    the tolerance, and measured available memory short of the table's free
    beyond it, are each in the reading's `drift`, as `{"what", "declared",
    "measured", "over"}`."""
    placement = drifting(record, clock, tmp_path)
    clock.step()
    value = placement.measure(now=clock())
    assert value["drift"] == []
    clock.step()
    placement.probes = stub(88 * GIB, 12 * GIB)
    value = placement.measure(now=clock())
    (entry,) = value["drift"]
    assert entry["what"] == "consumer machine memory"
    assert (entry["declared"], entry["measured"]) == (10 * GIB, 12 * GIB)
    assert math.isclose(entry["over"], 0.2)
    clock.step()
    placement.probes = stub(70 * GIB, 12 * GIB)
    value = placement.measure(now=clock())
    assert [e["what"] for e in value["drift"]] == [
        "consumer machine memory",
        "undeclared use of memory",
    ]
    undeclared = value["drift"][1]
    assert (undeclared["declared"], undeclared["measured"]) == (90 * GIB, 70 * GIB)
    assert math.isclose(undeclared["over"], 0.2)
    assert placement.drift(now=clock()) == value["drift"]


@over_backends
def test_drift_writes_an_observation_when_it_begins_and_when_it_ends(
    record, clock, tmp_path
):
    """templet:3161 -- each drift writes one observation when it begins and one
    when it ends, nothing while it lasts, and no `measure` writes anything but
    a reading and those statements: the journal holds no `capacity.set` or
    `reserve` that a measure caused."""
    placement = drifting(record, clock, tmp_path)
    mark = record.head()
    for available, resident in (
        (90, 10),
        (88, 12),
        (88, 12),
        (88, 13),
        (88, 10),
        (90, 10),
    ):
        clock.step()
        placement.probes = stub(available * GIB, resident * GIB)
        placement.measure(now=clock())
    texts = observations(record)
    assert len(texts) == 2
    assert texts[0] == (
        "allocator: machine memory measured 12.0 GiB, declared 10.0 GiB (+20%)"
    )
    assert texts[1] == "allocator: machine memory is back within its declaration"
    journal = [e["kind"] for e in record.dump()["journal"] if e["seq"] > mark]
    assert set(journal) == {"reading", "statement"}
    assert journal.count("reading") == 6 and journal.count("statement") == 2
    assert record.check() == []


# -- sample_consumer ----------------------------------------------------------------


CHILD = (
    "import time\n"
    "block = bytearray(b'x') * (64 * 1024 * 1024)\n"
    "print('up', flush=True)\n"
    "time.sleep(60)\n"
)


@over_backends
def test_sample_consumer_gives_the_peak_times_one_quarter_in_whole_blocks(
    record, clock, tmp_path
):
    """templet:3161 -- over a real process tree (a child holding 64 MiB),
    `sample_consumer` gives its peak resident size times 1.25, rounded up to a
    whole 256 MiB; it samples `seconds / every + 1` times, reports each, and a
    consumer with nothing resident is a `ProbeError`."""
    from quarrylith.allocator import probes

    child = subprocess.Popen([sys.executable, "-c", CHILD], stdout=subprocess.PIPE)
    try:
        assert child.stdout.readline().strip() == b"up"
        consumers = (
            f'[[consumer]]\nname = "held"\nmemory = "1GiB"\n'
            f'probe = {{ pid = {child.pid} }}\nwhy = "a child"\n'
            f'[[consumer]]\nname = "idle"\nmemory = "1GiB"\n'
            f'probe = {{ process = "no-such-process-{os.getpid()}" }}\nwhy = "none"\n'
        )
        document = mini(tmp_path, consumers)
        placement = HostPlacement(record)
        assert placement.declare(
            document, figures=real(placement, document, clock()), now=clock()
        ).ok
        peak = probes.probe_tree(child.pid)
        assert 64 * MIB <= peak < 250 * MIB
        seen, slept = [], []
        size = placement.sample_consumer(
            "held",
            seconds=4,
            every=2,
            progress=lambda number, total, top: seen.append((number, total)),
            sleep=slept.append,
        )
        assert size == 256 * MIB
        assert size == math.ceil(peak * 1.25 / (256 * MIB)) * 256 * MIB
        assert seen == [(1, 3), (2, 3), (3, 3)] and slept == [2, 2]
        with pytest.raises(ProbeError, match="idle"):
            placement.sample_consumer("idle", seconds=0, every=1, sleep=slept.append)
        with pytest.raises(Refused, match="nothing"):
            placement.sample_consumer("nothing", seconds=0, every=1)
    finally:
        child.kill()
        child.wait()


# -- resolve ------------------------------------------------------------------------


@over_backends
def test_resolve_probes_once_and_then_gives_the_recorded_figures(
    record, clock, tmp_path
):
    """templet:3161 -- `resolve` of the sample on the test host probes each
    "measure" when the record has no document reading; after a `declare` with
    those figures it gives the recorded figures again, probing nothing, so a
    second `resolve` and `declare` writes nothing (head unchanged); with
    `fresh=True` it probes again."""
    from quarrylith.allocator import probes

    calls = []

    def spy(name, call):
        def spied(*args, **kw):
            calls.append(name)
            return call(*args, **kw)

        return spied

    wrapped = types.SimpleNamespace(
        probe_memory=spy("memory", probes.probe_memory),
        probe_disk=spy("disk", probes.probe_disk),
        probe_size=spy("size", probes.probe_size),
        process_table=spy("table", probes.process_table),
        resident=probes.resident,
    )
    placement = HostPlacement(record, probes=wrapped)
    placement.sample_consumer = lambda name, **kw: (
        calls.append("sample " + name) or 3 * GIB // 2
    )
    image = tmp_path / "disk.img"
    with open(image, "wb") as handle:
        handle.truncate(200 * MIB)
        handle.write(b"x" * (3 * MIB))
    consumers = (
        '[[consumer]]\nname = "yard"\nmemory = "measure"\n'
        'probe = { pidfile = "yard.pid" }\nwhy = "a service"\n'
        f'[[consumer]]\nname = "img"\ndisk = "1GiB"\npath = "{image}"\n'
        'why = "an image"\n'
    )
    document = mini(tmp_path, consumers)
    figures = placement.resolve(document, now=clock())
    assert figures["capacity"]["memory"] == probes.probe_memory()["memory_total"]
    disk = figures["capacity"]["disk"]
    expected = probes.probe_disk(str(tmp_path)) + probes.probe_size(str(image))
    expected = max(expected - 20 * GIB, 0)
    assert isinstance(disk, int) and abs(disk - expected) < 2 * GIB
    assert probes.probe_size(str(image)) < 20 * MIB
    assert figures["consumers"] == {"yard": {"memory": 3 * GIB // 2}}
    assert {"memory", "disk", "size", "sample yard"} <= set(calls)
    assert placement.declare(document, figures=figures, now=clock()).ok
    head, calls[:] = record.head(), []
    clock.step()
    again = placement.resolve(document, now=clock())
    assert again == figures and calls == []
    assert placement.declare(document, figures=again, now=clock()).wrote == ()
    assert record.head() == head
    fresh = placement.resolve(document, fresh=True, now=clock())
    assert {"memory", "disk", "size", "sample yard"} <= set(calls)
    assert fresh["capacity"]["memory"] == figures["capacity"]["memory"]
