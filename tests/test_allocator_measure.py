# SPDX-License-Identifier: Apache-2.0
"""The allocator's measurement (templet:3161, build step 4): the probes of the
host's memory, disk and processes, `measure` and the one reading it writes,
the measured check of `place` and `hold`, `unmeasured`, drift and its
observations, `sample_consumer` and `resolve`. The probes run for real on the
test host (read-only); the Linux reader is tested on a hand-written file of
the shape `/proc/meminfo` has, not on a captured one, and never run.
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
    """templet:3161 -- the parsers read a `vm_stat` sample captured on the
    test host and hand-written `/proc/meminfo` and `ps` samples: pages of
    free, inactive, purgeable and speculative memory at the page size the
    output names; `MemAvailable` in kB; and one row per process
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
    assert (short.asked, short.free, short.capacity) == (2 * GIB, 5 * GIB, 48 * GIB)
    assert short.waits is True and answer.waits is True and answer.until is None
    assert "[measured]" in answer.text
    assert "5.0 GiB available" in answer.text and "floor 4.0 GiB" in answer.text
    assert short.detail == "floor 4.0 GiB"
    clock.step()
    host_reading(record, clock, available=40 * GIB, disk_free=23 * GIB)
    answer = placement.place(task, now=clock())
    (short,) = answer.shorts
    assert (short.cause, short.resource) == ("measured", "disk")
    assert (short.asked, short.free) == (4 * GIB, 23 * GIB)
    assert short.detail == "floor 20.0 GiB"
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

    def causes(said):
        answer = placement.place(task, now=clock())
        assert isinstance(answer, Refusal)
        (short,) = answer.shorts
        assert (short.cause, short.resource, short.waits) == ("unmeasured", None, True)
        assert short.node == "laptop" and answer.waits is True
        assert "unmeasured" in answer.text and said in answer.text, answer.text
        held = placement.hold("x", {"memory": 1}, now=clock())
        assert [s.cause for s in held.shorts] == ["unmeasured"]

    causes("no host reading")
    host_reading(record, clock, node="elsewhere")
    causes("is of elsewhere, not laptop")
    record.record_reading("host", "measure", {"node": "laptop"})
    causes("has no figures for memory and disk")
    clock.step()
    host_reading(record, clock)
    assert isinstance(placement.place(task, now=clock()), Fit)
    clock.step(180_000)
    assert isinstance(placement.place(task, now=clock()), Fit)
    clock.step(1)
    causes("181 s old, past the 180 s")


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


@over_backends
def test_sample_consumer_takes_the_peak_not_the_last_sample(record, clock, tmp_path):
    """templet:3161 -- the size is from the largest resident size of any
    sample, times 1.25: 100, 220 then 150 MiB gives 275 MiB, rounded up to
    512 MiB (the last sample alone would give 256, and the peak alone 256)."""
    from quarrylith.allocator import probes

    sizes = iter([100 * MIB, 220 * MIB, 150 * MIB])
    given = types.SimpleNamespace(
        process_table=lambda: [probes.Process(10, 1, next(sizes), "x")],
        resident=probes.resident,
    )
    document = mini(tmp_path, DRIFT)
    placement = HostPlacement(record, probes=given)
    size = placement.sample_consumer(
        "machine", seconds=2, every=1, document=document, sleep=lambda s: None
    )
    assert size == 512 * MIB


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
        for _ in range(512):
            handle.write(b"x" * MIB)
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
    assert isinstance(disk, int) and abs(disk - expected) < 200 * MIB
    # the image's 512 MiB are counted in: free space alone would be less
    assert disk == 0 or disk - max(probes.probe_disk(str(tmp_path)) - 20 * GIB, 0) > (
        256 * MIB
    )
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


# -- a consumer that is down --------------------------------------------------------


def stale_pid():
    """A process number that was a process and is not now."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


@over_backends
def test_a_consumer_that_is_down_reads_as_absent_and_the_host_is_still_measured(
    record, clock, tmp_path
):
    """templet:3161 -- a consumer whose pid file is missing, holds no number, or
    names a process that is gone, whose pid is not running, or whose name no
    process has, is down, not a failed probe: `measure` still writes the host's
    figures, the consumer has no resident figure, and the reading says in
    `absent` why. A pid file that is read and finds a running process is
    measured."""
    gone = tmp_path / "gone.pid"
    junk = tmp_path / "junk.pid"
    junk.write_text("not a number")
    dead = tmp_path / "dead.pid"
    dead.write_text(f"{stale_pid()}\n")
    live = tmp_path / "live.pid"
    live.write_text(f"{os.getpid()}\n")
    consumers = "".join(
        f'[[consumer]]\nname = "{name}"\nmemory = "1GiB"\n'
        f'probe = {{ {probe} }}\nwhy = "a consumer"\n'
        for name, probe in (
            ("missing", f'pidfile = "{gone}"'),
            ("junk", f'pidfile = "{junk}"'),
            ("dead", f'pidfile = "{dead}"'),
            ("alive", f'pidfile = "{live}"'),
            ("nopid", f"pid = {stale_pid()}"),
            ("noname", f'process = "no-such-process-{os.getpid()}"'),
        )
    )
    document = mini(tmp_path, consumers)
    placement = HostPlacement(record)
    assert placement.declare(
        document, figures=real(placement, document, clock()), now=clock()
    ).ok
    head = record.head()
    value = placement.measure(now=clock())
    # the reading, and one observation for each consumer that is down
    assert record.head() == head + 1 + 5
    assert value["memory_available"] > 0 and value["disk_free"] > 0
    assert set(value["consumers"]) == {"alive"}
    assert set(value["absent"]) == {"missing", "junk", "dead", "nopid", "noname"}
    assert "missing" in value["absent"]["missing"]
    assert "pid file" in value["absent"]["junk"]
    assert "no process" in value["absent"]["dead"]
    assert "no process" in value["absent"]["nopid"]
    assert "no-such-process" in value["absent"]["noname"]
    (made,) = files(record, "measure")
    assert made["value"]["absent"] == value["absent"]
    assert value["drift"] == []


@over_backends
def test_a_consumer_going_absent_and_returning_is_observed_once_each(
    record, clock, tmp_path
):
    """templet:3161 -- a consumer that becomes absent is said once, with why,
    as an observation, and one that returns is said once; nothing is written
    while it stays absent or stays present, so a probe gone blind is seen as
    well as a service that is down."""
    placement = drifting(record, clock, tmp_path)
    mark = record.head()
    for resident in (10, 10, None, None, None, 10, 10):
        clock.step()
        placement.probes = stub(90 * GIB, None if resident is None else GIB)
        placement.measure(now=clock())
    assert observations(record) == [
        "allocator: machine is absent: no process 10",
        "allocator: machine is back",
    ]
    journal = [e["kind"] for e in record.dump()["journal"] if e["seq"] > mark]
    assert journal.count("reading") == 7 and journal.count("statement") == 2
    assert record.check() == []


@over_backends
def test_a_consumer_absent_at_the_first_measure_and_one_taken_out_are_observed(
    record, clock, tmp_path
):
    """templet:3161 -- a consumer absent at the very first measurement is said
    at once; one that is absent and then leaves the document is said to be no
    longer probed, once."""
    placement = drifting(record, clock, tmp_path)
    placement.probes = stub(90 * GIB, None)
    clock.step()
    placement.measure(now=clock())
    assert observations(record) == ["allocator: machine is absent: no process 10"]
    clock.step()
    placement.measure(now=clock())
    assert len(observations(record)) == 1
    document = mini(tmp_path, tolerance=0.1)
    figures = {"capacity": {"memory": 100 * GIB, "disk": 400 * GIB}, "consumers": {}}
    assert placement.declare(document, figures=figures, now=clock()).ok
    clock.step()
    placement.measure(now=clock())
    clock.step()
    placement.measure(now=clock())
    assert observations(record) == [
        "allocator: machine is absent: no process 10",
        "allocator: machine is no longer probed",
    ]


@over_backends
def test_sample_consumer_of_a_consumer_that_is_down_says_why(record, clock, tmp_path):
    """templet:3161 -- a consumer that is down has nothing to size: the
    `ProbeError` names it and says why (not a raw error)."""
    consumers = (
        f'[[consumer]]\nname = "svc"\nmemory = "1GiB"\n'
        f'probe = {{ pidfile = "{tmp_path / "gone.pid"}" }}\nwhy = "a service"\n'
    )
    placement = HostPlacement(record)
    document = mini(tmp_path, consumers)
    with pytest.raises(ProbeError, match="svc.*missing"):
        placement.sample_consumer(
            "svc", seconds=0, every=1, document=document, sleep=lambda s: None
        )


def test_a_name_that_is_not_utf8_does_not_stop_the_probe(monkeypatch):
    """templet:3161 -- a process name that is not UTF-8 (Linux names are any
    bytes) is read with a replacement character: the table still comes."""
    from quarrylith.allocator import probes

    monkeypatch.setattr(probes, "PS", ["printf", "1 0 5 /bin/x\\377y\\n 2 1 7 ok\\n"])
    table = probes.process_table()
    assert [(p.pid, p.rss) for p in table] == [(1, 5 * 1024), (2, 7 * 1024)]
    assert table[0].comm.startswith("/bin/x") and table[0].comm.endswith("y")


# -- the line says why ----------------------------------------------------------------


@over_backends
def test_a_failing_statement_leaves_no_reading_and_the_drift_is_said_next_time(
    record, clock, tmp_path
):
    """templet:3161 -- the observations are written before the reading, so a
    `state` that fails leaves no reading that holds a drift nobody was told of:
    `measure` raises, and the next `measure` says the drift once."""

    class Failing:
        """A record whose statements are refused."""

        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def state(self, *args, **kw):
            raise Refused("the record would not take a statement")

    placement = drifting(record, clock, tmp_path)
    clock.step()
    placement.probes = stub(88 * GIB, 12 * GIB)
    head = record.head()
    with pytest.raises(Refused, match="statement"):
        HostPlacement(Failing(record), probes=placement.probes).measure(now=clock())
    assert record.head() == head and files(record, "measure") == []
    assert observations(record) == []
    placement.measure(now=clock())
    assert len(observations(record)) == 1 and len(files(record, "measure")) == 1
    clock.step()
    placement.measure(now=clock())
    assert len(observations(record)) == 1


# -- the edges ------------------------------------------------------------------------


@over_backends
def test_a_drift_exactly_at_the_tolerance_is_not_a_drift(record, clock, tmp_path):
    """templet:3161 -- the tolerance is how far past is allowed: a consumer
    exactly 10% past its declaration, and available memory exactly 10% of the
    capacity short of the table's free, are no drift; one byte more is."""
    placement = drifting(record, clock, tmp_path)
    for resident, available, drift in (
        (11 * GIB, 90 * GIB, False),
        (11 * GIB + 1, 90 * GIB, True),
        (10 * GIB, 80 * GIB, False),
        (10 * GIB, 80 * GIB - 1, True),
    ):
        clock.step()
        placement.probes = stub(available, resident)
        assert bool(placement.measure(now=clock())["drift"]) is drift, (
            resident,
            available,
        )


@over_backends
def test_hold_on_another_node_does_not_check_the_measured_room(effort, clock):
    """templet:3161 -- the plugin measures the node its document names: with the
    host unmeasured, `hold` there is refused `unmeasured`, and `hold` on another
    node checks the table alone."""
    from test_allocator_host import declared

    record = effort
    placement = declared(record, clock, load_document(sample_text(180)))
    record.declare_node("other", ["process"], [])
    record.set_capacity("other", "memory", 4 * GIB, "bytes")
    clock.step(10 * MIN)
    refusal = placement.hold("a", {"memory": GIB}, now=clock())
    assert [s.cause for s in refusal.shorts] == ["unmeasured"]
    assert placement.hold("a", {"memory": GIB}, node="other", now=clock())
    refusal = placement.hold("b", {"memory": 8 * GIB}, node="other", now=clock())
    assert [s.cause for s in refusal.shorts] == ["room"]


@over_backends
def test_sample_consumer_of_a_consumer_that_is_down_stops_at_the_first_sample(
    record, clock, tmp_path
):
    """templet:3161 -- a consumer with nothing resident is a `ProbeError` at
    the first sample, naming why, without sleeping through the window; one that
    is up at the first sample is sampled to the end."""
    document = mini(tmp_path, DRIFT, tolerance=0.1)
    placement = HostPlacement(record, probes=stub(90 * GIB, None))
    slept = []
    with pytest.raises(ProbeError, match="machine.*no process 10"):
        placement.sample_consumer(
            "machine", seconds=600, every=10, document=document, sleep=slept.append
        )
    assert slept == []
    placement.probes = stub(90 * GIB, GIB)
    placement.sample_consumer(
        "machine", seconds=30, every=10, document=document, sleep=slept.append
    )
    assert slept == [10, 10, 10]
