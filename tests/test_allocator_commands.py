# SPDX-License-Identifier: Apache-2.0
"""The allocator's commands (templet:3161, build step 5): `quarrylith alloc
declare|show|place|measure|consumer measure`, with `--timing`, `--debug` and
the phase lines. Every test that touches a record runs over every backend in
BACKENDS; the host is a stand-in whose figures a test moves, except where a
test says it probes for real."""

import dataclasses
import json
import os
import subprocess
import sys
import time

import pytest
from backends import one_writer, over_backends
from test_allocator_host import (
    GIB,
    STANDING,
    declared,
    host_reading,
    make_task,
    sample_text,
    start,
)
from test_allocator_measure import ME, MINI

from quarrylith import cli
from quarrylith.allocator import Fit, Refusal, load_document
from quarrylith.allocator import probes as real_probes
from quarrylith.allocator.host import ProbeError

MIB = 2**20
SECRET = "WHYTEXT-77"


class FakeHost:
    """The probes of a host whose figures a test moves."""

    def __init__(self, total=48 * GIB, available=40 * GIB, disk=200 * GIB, rss=GIB):
        self.total, self.available, self.disk, self.rss = total, available, disk, rss
        self.fail = None

    def probe_memory(self):
        if self.fail:
            raise ProbeError(self.fail)
        return {
            "memory_total": self.total,
            "memory_available": self.available,
            "method": "given",
        }

    def probe_disk(self, path):
        return self.disk

    def probe_size(self, path):
        return 0

    def process_table(self):
        if self.rss is None:
            return []
        return [real_probes.Process(10, 1, self.rss, "x")]

    def resident(self, probe, table):
        if not table:
            return None, "no process 10"
        return table[0].rss, None


@pytest.fixture
def alloc_cli():
    import quarrylith.alloc_cli as module

    return module


@pytest.fixture
def host(alloc_cli, monkeypatch):
    """A fake host answers every probe, and nothing really sleeps."""
    fake = FakeHost()
    monkeypatch.setattr(alloc_cli, "PROBES", fake)
    monkeypatch.setattr(alloc_cli, "SLEEP", lambda seconds: None)
    return fake


def alloc(capsys, *argv):
    code = cli.main(["alloc", *argv])
    out, err = capsys.readouterr()
    return code, out, err


def peek(opener, read):
    """What `read` makes of the record, on a handle that is closed after."""
    record = opener()
    try:
        return read(record)
    finally:
        record.close()


def head_of(opener):
    return peek(opener, lambda r: r.head())


def write_doc(tmp_path, text=None, name="allocation.toml"):
    path = tmp_path / name
    path.write_text(sample_text() if text is None else text)
    return str(path)


def kinds_since(record, mark):
    return [e["kind"] for e in record.dump()["journal"] if e["seq"] > mark]


def lines_of(out, prefix):
    return [line[len(prefix) :] for line in out.splitlines() if line.startswith(prefix)]


def declare_sample(capsys, tmp_path, url, *more):
    doc = write_doc(tmp_path)
    code, out, err = alloc(capsys, "declare", doc, url, *more)
    assert code == 0, (out, err)
    return doc, out


# -- declare ------------------------------------------------------------------------


@over_backends
def test_declare_writes_what_it_says_and_a_second_declare_writes_nothing(
    backend, record, url, host, capsys, tmp_path, opener
):
    """templet:3161 -- `alloc declare DOC URL` resolves the figures and
    declares: it prints each write, the record then holds the node, the
    capacities, the consumers' reservations and the document reading, and a
    second `declare` of the same document prints that there is nothing to
    write and leaves the head unchanged."""
    one_writer(backend, record)
    doc, out = declare_sample(capsys, tmp_path, url)
    wrote = lines_of(out, "wrote ")
    assert "node laptop" in wrote
    assert f"capacity memory = {48 * GIB}" in wrote
    assert f"capacity disk = {180 * GIB}" in wrote
    assert any(line.startswith("hold desktop memory ") for line in wrote)
    assert any(line.startswith("hold yard memory ") for line in wrote)
    assert any(line.startswith("document ") for line in wrote)
    assert "nothing to write" not in out

    def read(rec):
        held = {
            (r["holder"], r["resource"]): r["amount"]
            for r in rec.read("reservations", live=True, node="laptop")
        }
        (reading,) = rec.read("readings", source="allocator", kind="document")
        return held, reading["value"], rec.head(), rec.check()

    held, value, head, problems = peek(opener, read)
    assert held[("desktop", "memory")] == 10 * GIB
    assert held[("yard", "memory")] == 1280 * MIB  # 1 GiB x 1.25 -> whole 256 MiB
    assert value["figures"]["capacity"] == {"memory": 48 * GIB, "disk": 180 * GIB}
    assert problems == []
    code, out, err = alloc(capsys, "declare", doc, url)
    assert code == 0 and "nothing to write" in out and "wrote " not in out
    assert head_of(opener) == head


@over_backends
def test_dry_run_writes_nothing_and_says_what_a_declare_would_write(
    backend, record, url, host, capsys, tmp_path, opener
):
    """templet:3161 -- `--dry-run` prints `would write` for each write a
    declare would make and writes nothing, shown on the record's head and its
    tables; the lines are the ones the real declare then writes. Over a
    declared record with a consumer changed and one removed it names the
    replace and the release."""
    one_writer(backend, record)
    doc = write_doc(tmp_path)
    mark = head_of(opener)
    code, out, err = alloc(capsys, "declare", doc, url, "--dry-run")
    assert code == 0, err
    would = lines_of(out, "would write ")
    assert "node laptop" in would and "wrote " not in out
    assert head_of(opener) == mark
    assert peek(opener, lambda r: (r.read("nodes"), r.read("readings"))) == ([], [])
    code, out, err = alloc(capsys, "declare", doc, url)
    assert code == 0 and lines_of(out, "wrote ") == would
    # the declared record, the sample with the machine larger and desktop gone
    text = sample_text().replace('memory = "15.4GiB"', 'memory = "16GiB"')
    gone = (
        '[[consumer]]\nname = "desktop"\nmemory = "10GiB"\n'
        'why = "the operating system and the user\'s own applications"\n'
    )
    assert gone in text
    changed = write_doc(tmp_path, text.replace(gone, ""), "changed.toml")
    mark = head_of(opener)
    code, out, err = alloc(capsys, "declare", changed, url, "--dry-run")
    assert code == 0, err
    would = lines_of(out, "would write ")
    assert any(line.startswith("replace podman-machine memory") for line in would)
    assert any(line.startswith("release desktop memory") for line in would)
    assert head_of(opener) == mark
    code, out, err = alloc(capsys, "declare", changed, url)
    assert code == 0 and lines_of(out, "wrote ") == would


@over_backends
def test_declare_refuses_what_does_not_fit_with_exit_two(
    backend, record, url, host, capsys, tmp_path, opener
):
    """templet:3161 -- a document whose consumers do not fit is refused before
    any write: exit 2, a line per consumer with the allocator's own refusal,
    the record unchanged; `--dry-run` says the same and exits 2."""
    one_writer(backend, record)
    doc = write_doc(tmp_path)
    host.total = 11 * GIB
    mark = head_of(opener)
    for more in ((), ("--dry-run",)):
        code, out, err = alloc(capsys, "declare", doc, url, *more)
        assert code == 2, err
        refused = lines_of(out, "refused ")
        assert [line.split(":")[0] for line in refused] == ["podman-machine", "yard"]
        assert "allocator: hold refused on laptop: memory" in refused[0]
        assert "wrote " not in out and "would write " not in out
        assert head_of(opener) == mark


@over_backends
def test_declare_refuses_a_document_it_cannot_read(
    backend, record, url, host, capsys, tmp_path, opener
):
    """templet:3161 -- a document with a key it does not know, or a file that
    is not there, is refused with exit 2 naming the problem, and writes
    nothing."""
    one_writer(backend, record)
    mark = head_of(opener)
    bad = write_doc(tmp_path, sample_text() + "\nbogus = 1\n", "bad.toml")
    code, out, err = alloc(capsys, "declare", bad, url)
    assert (code, out) == (2, "") and err.startswith("refused: ") and "bogus" in err
    code, out, err = alloc(capsys, "declare", str(tmp_path / "none.toml"), url)
    assert (code, out) == (2, "")
    assert err.startswith("refused: cannot read the allocation document")
    assert len(err.splitlines()) == 1
    assert head_of(opener) == mark


@over_backends
def test_remeasure_probes_again_and_without_it_the_recorded_figures_stay(
    backend, record, url, host, capsys, tmp_path, opener
):
    """templet:3161 -- a second `declare` keeps the recorded figures although
    the host moved (nothing to write); `--remeasure` probes again and writes
    the new figure."""
    one_writer(backend, record)
    doc, _ = declare_sample(capsys, tmp_path, url)
    host.disk = 150 * GIB
    mark = head_of(opener)
    code, out, err = alloc(capsys, "declare", doc, url)
    assert code == 0 and "nothing to write" in out
    assert head_of(opener) == mark
    code, out, err = alloc(capsys, "declare", doc, url, "--remeasure")
    assert code == 0, err
    assert f"capacity disk = {130 * GIB}" in lines_of(out, "wrote ")


def test_a_dry_run_declare_names_the_writes_and_makes_none(record, clock):
    """templet:3161 -- `HostPlacement.declare(..., dry_run=True)` returns what
    a declare would write, line for line, and writes nothing; the real declare
    then writes those lines."""
    from test_allocator_host import FIGURES

    placement = declared(record, clock)
    text = sample_text().replace('memory = "15.4GiB"', 'memory = "16GiB"')
    changed = load_document(
        text.replace('"podman.memory" = "14GiB"', '"podman.memory" = "15GiB"')
    )
    clock.step()
    mark = record.head()
    dry = placement.declare(changed, figures=FIGURES, now=clock(), dry_run=True)
    assert dry.ok and dry.wrote
    assert record.head() == mark
    real = placement.declare(changed, figures=FIGURES, now=clock())
    assert real.wrote == dry.wrote
    again = placement.declare(changed, figures=FIGURES, now=clock(), dry_run=True)
    assert again.wrote == ()


# -- show ---------------------------------------------------------------------------


def busy(record, clock):
    """The sample declared, a host reading with drift and a down consumer, a
    run, a task's own hold and a hold the document does not name."""
    placement = declared(record, clock)
    task = make_task(record)
    fit = placement.place(task, now=clock())
    run_id = start(record, task, fit, "worker-1")
    own = make_task(record)
    placement.hold("vm:lab-1", {"memory": 4 * GIB, "lab": 1}, task=own, now=clock())
    placement.hold("old-thing", {"memory": GIB}, now=clock(), measured=False)
    clock.step(125_000)
    machine = 18 * GIB + 3 * GIB // 10
    host_reading(
        record,
        clock,
        consumers={"podman-machine": {"memory": machine}},
        absent={"yard": "pid file yard/supervisor.pid is missing"},
        drift=[
            {
                "what": "consumer podman-machine memory",
                "declared": STANDING[("podman-machine", "memory")],
                "measured": machine,
                "over": 0.19,
            }
        ],
    )
    record.state(
        "observation",
        "allocator: podman-machine memory measured 18.3 GiB, declared 15.4 GiB (+19%)",
        where="allocator",
    )
    clock.step(5000)
    return task, own, run_id


def gib(n):
    return f"{n / GIB:.1f} GiB"


@over_backends
def test_show_gives_per_resource_the_capacity_consumers_runs_free_measured_drift(
    backend, effort, url, clock, capsys
):
    """templet:3161 -- `alloc show` prints, per node and resource, the
    capacity, the standing consumers (the one the document does not name
    marked, a down one marked absent, a measured one with its figure), the
    tasks' own holds, the runs, what is free, the measured figure and the
    drift; then the document's sha256 and age, the host reading's age, the
    absent consumers and the allocator's observations."""
    record = effort
    task, own, run_id = busy(record, clock)
    now = clock()
    free = record.free("laptop", "memory", from_at=now, until_at=None)
    disk_free = record.free("laptop", "disk", from_at=now, until_at=None)
    (reading,) = record.read("readings", source="allocator", kind="document")
    digest = reading["value"]["sha256"]
    one_writer(backend, record)

    code, out, err = alloc(capsys, "show", url, "--now", str(now))
    assert (code, err) == (0, "")
    lines = out.splitlines()
    assert f"document: sha256 {digest}, age 2 m 10 s" in lines
    assert "host reading: node laptop, given, age 5 s" in lines
    assert "node laptop: drivers harness, podman, process, vm" in lines
    assert f"  memory: capacity {gib(48 * GIB)}, free {gib(free)}, " in " ".join(lines)
    assert (
        f"  memory: capacity {gib(48 * GIB)}, free {gib(free)}, measured "
        + (gib(47 * GIB) + " available")
        in lines
    )
    assert (
        f"  disk: capacity {gib(200 * GIB)}, free {gib(disk_free)}, "
        + ("measured " + gib(190 * GIB) + " free")
        in lines
    )
    assert (
        "    standing: desktop 10.0 GiB, old-thing 1.0 GiB (not in the document), "
        "podman-machine 15.4 GiB (measured 18.3 GiB), yard 1.5 GiB (absent)"
    ) in lines
    assert f"    tasks: vm:lab-1 4.0 GiB (task {own})" in lines
    assert f"    runs: worker-1 2.0 GiB (task {task}, run {run_id})" in lines
    assert (
        "    drift: podman-machine memory measured 18.3 GiB, declared 15.4 GiB (+19%)"
    ) in lines
    assert "  slots.claude-code: capacity 4, free 3" in lines
    assert f"    runs: worker-1 1 (task {task}, run {run_id})" in lines
    assert "absent: yard: pid file yard/supervisor.pid is missing" in lines
    observed = [line for line in lines if line.startswith("  20")]
    assert observed and "podman-machine memory measured 18.3 GiB" in observed[-1]


@over_backends
def test_show_json_agrees_with_the_text(backend, effort, url, clock, capsys):
    """templet:3161 -- `show --json` is one JSON object of the same figures:
    the document's sha256 and age, per resource the capacity, free, the
    holders by kind and the measured figure."""
    record = effort
    task, own, run_id = busy(record, clock)
    now = clock()
    (reading,) = record.read("readings", source="allocator", kind="document")
    one_writer(backend, record)

    code, out, err = alloc(capsys, "show", url, "--now", str(now), "--json")
    assert (code, err) == (0, "")
    got = json.loads(out)
    assert got["document"]["sha256"] == reading["value"]["sha256"]
    assert got["document"]["age_s"] == 130
    (node,) = got["nodes"]
    assert node["name"] == "laptop"
    memory = {r["resource"]: r for r in node["resources"]}["memory"]
    assert memory["capacity"] == 48 * GIB
    assert memory["measured"] == 47 * GIB
    assert {h["holder"] for h in memory["standing"]} == {
        "desktop",
        "old-thing",
        "podman-machine",
        "yard",
    }
    assert [h["holder"] for h in memory["runs"]] == ["worker-1"]
    assert [(h["holder"], h["task"]) for h in memory["tasks"]] == [("vm:lab-1", own)]
    assert memory["drift"] and memory["drift"][0]["what"].endswith("memory")
    code, text, _ = alloc(capsys, "show", url, "--now", str(now))
    assert f"free {gib(memory['free'])}" in text


@over_backends
def test_show_of_a_record_with_no_document(backend, record, url, capsys):
    """templet:3161 -- `show` of a record nothing is declared in says so and
    exits 0: no document, no host reading, no node."""
    one_writer(backend, record)
    code, out, err = alloc(capsys, "show", url)
    assert (code, err) == (0, "")
    assert "document: none declared" in out and "host reading: none" in out
    assert "node " not in out


# -- place --------------------------------------------------------------------------


@over_backends
def test_place_prints_a_fit_or_the_refusal_and_writes_nothing(
    backend, effort, url, clock, capsys, opener
):
    """templet:3161 -- `alloc place URL TASK` prints the Fit (node, class,
    needs, why) and exits 0, or the Refusal's text and exits 2; `--json` is
    the Fit or the Refusal as data; a task that is not there is refused with
    exit 2. It writes nothing."""
    record = effort
    placement = declared(record, clock)
    fits = make_task(record)
    huge = make_task(record, needs={"memory": 60 * GIB})
    now = clock()
    expected = placement.place(fits, now=now)
    refusal = placement.place(huge, now=now)
    assert isinstance(expected, Fit) and isinstance(refusal, Refusal)
    mark = record.head()
    one_writer(backend, record)

    code, out, err = alloc(capsys, "place", url, str(fits), "--now", str(now))
    assert (code, err) == (0, "")
    assert f"fit: task {fits} on laptop (agent)" in out.splitlines()
    assert (
        "needs: "
        + ", ".join(
            f"{r} {gib(n) if r in ('memory', 'disk') else n}"
            for r, n in sorted(expected.needs.items())
        )
        in out.splitlines()
    )
    assert f"why: {expected.why}" in out.splitlines()
    code, out, err = alloc(capsys, "place", url, str(huge), "--now", str(now))
    assert code == 2 and out.strip() == refusal.text and err == ""
    code, out, _ = alloc(capsys, "place", url, str(fits), "--now", str(now), "--json")
    assert code == 0 and json.loads(out) == {"fit": dataclasses.asdict(expected)}
    code, out, _ = alloc(capsys, "place", url, str(huge), "--now", str(now), "--json")
    assert code == 2
    assert json.loads(out) == {"refusal": json.loads(json.dumps(refusal.as_data()))}
    code, out, err = alloc(capsys, "place", url, "999", "--now", str(now))
    assert (code, out) == (2, "") and "no task 999" in err
    assert head_of(opener) == mark


# -- measure ------------------------------------------------------------------------


def mini_doc(tmp_path):
    return write_doc(
        tmp_path,
        MINI.format(
            disk_path=tmp_path, tolerance=1000, consumers=ME.format(pid=os.getpid())
        ),
        "mini.toml",
    )


@over_backends
def test_measure_prints_the_figures_and_writes_one_reading(
    backend, record, url, capsys, tmp_path, opener
):
    """templet:3161 -- `alloc measure URL` runs the probes for real on the test
    host, prints the host's memory and disk and each consumer's resident size,
    and writes one `host/measure` reading."""
    one_writer(backend, record)
    doc = mini_doc(tmp_path)
    code, out, err = alloc(capsys, "declare", doc, url)
    assert code == 0, err
    mark = head_of(opener)
    code, out, err = alloc(capsys, "measure", url)
    assert code == 0, err
    first = out.splitlines()[0]
    assert first.startswith("measured box: memory ") and " available of " in first
    assert ", disk " in first and first.endswith(" free")
    assert any(line.startswith("consumer me: ") for line in out.splitlines())

    def read(rec):
        rows = rec.read("readings", source="host", kind="measure")
        return rec.head(), rows

    head, rows = peek(opener, read)
    assert head == mark + 1 and len(rows) == 1
    assert rows[0]["value"]["node"] == "box" and "me" in rows[0]["value"]["consumers"]


@over_backends
def test_measure_with_no_document_refuses_and_a_failing_probe_is_an_error(
    backend, record, url, host, capsys, tmp_path, opener
):
    """templet:3161 -- `measure` with nothing declared is refused (exit 2) and
    writes nothing; a probe that fails is an error (exit 1) naming the probe,
    and writes no reading."""
    one_writer(backend, record)
    mark = head_of(opener)
    code, out, err = alloc(capsys, "measure", url)
    assert (code, out) == (2, "") and "no allocation document" in err
    assert head_of(opener) == mark
    declare_sample(capsys, tmp_path, url)
    mark = head_of(opener)
    host.fail = "probe_memory: vm_stat exited 1"
    code, out, err = alloc(capsys, "measure", url)
    assert (code, out) == (1, "")
    assert err.startswith("error: probe_memory: vm_stat exited 1")
    assert head_of(opener) == mark


@over_backends
def test_measure_prints_an_absent_consumer_and_the_drift(
    backend, record, url, host, capsys, tmp_path, opener
):
    """templet:3161 -- `measure` names a consumer that is down with why, and
    each drift in the words the observation uses."""
    one_writer(backend, record)
    declare_sample(capsys, tmp_path, url)
    host.rss = None
    host.available = 10 * GIB
    code, out, err = alloc(capsys, "measure", url)
    assert code == 0, err
    lines = out.splitlines()
    assert any(line.startswith("absent yard: no process 10") for line in lines)
    assert any(
        line.startswith("drift: undeclared use of memory: ") for line in lines
    ), out


# -- consumer measure -----------------------------------------------------------------


@over_backends
def test_consumer_measure_prints_the_size_and_writes_nothing(
    backend, record, url, host, capsys, tmp_path, opener
):
    """templet:3161 -- `alloc consumer measure NAME URL --for 30s --every 10s`
    samples the consumer's probe, prints its peak and the size to declare
    (the peak x 1.25 in whole 256 MiB), and writes nothing."""
    one_writer(backend, record)
    declare_sample(capsys, tmp_path, url)
    host.rss = 2 * GIB
    mark = head_of(opener)
    code, out, err = alloc(
        capsys, "consumer", "measure", "yard", url, "--for", "30s", "--every", "10s"
    )
    assert code == 0, err
    assert "consumer yard: peak 2.0 GiB over 4 samples, size 2.5 GiB (2560 MiB)" in (
        out.splitlines()
    )
    assert head_of(opener) == mark


@over_backends
def test_consumer_measure_declare_replaces_the_hold_and_prints_the_line(
    backend, record, url, host, capsys, tmp_path, opener
):
    """templet:3161 -- `--declare` writes the size into the record by a
    replace (the reservation and the recorded figure, in one declare), prints
    the document line to copy, and a later `declare` of the document finds it
    declared. It refuses a consumer whose amount the document fixes, before
    sampling, and a size that does not fit, with the allocator's refusal."""
    one_writer(backend, record)
    doc, _ = declare_sample(capsys, tmp_path, url)
    (reading,) = peek(
        opener, lambda r: r.read("readings", source="allocator", kind="document")
    )
    mark = head_of(opener)
    host.rss = 2 * GIB
    code, out, err = alloc(
        capsys,
        "consumer",
        "measure",
        "yard",
        url,
        "--for",
        "10s",
        "--every",
        "10s",
        "--declare",
    )
    assert code == 0, err
    assert 'document line: memory = "2560MiB"' in out.splitlines()
    assert "declared yard memory 2.5 GiB" in out.splitlines()

    def read(rec):
        held = {
            (r["holder"], r["resource"]): r["amount"]
            for r in rec.read("reservations", live=True, node="laptop")
        }
        (made,) = rec.read("readings", source="allocator", kind="document")[-1:]
        return kinds_since(rec, mark), held, made["value"]

    kinds, held, value = peek(opener, read)
    assert kinds == ["reserve.replace", "reading"]
    assert held[("yard", "memory")] == 2560 * MIB
    assert value["figures"]["consumers"] == {"yard": {"memory": 2560 * MIB}}
    assert value["sha256"] == reading["value"]["sha256"]
    mark = head_of(opener)
    code, out, err = alloc(capsys, "declare", doc, url)
    assert code == 0 and "nothing to write" in out
    assert head_of(opener) == mark
    # the machine's amount is fixed in the document: refused before sampling
    code, out, err = alloc(
        capsys, "consumer", "measure", "podman-machine", url, "--declare"
    )
    assert code == 2 and "fixed" in err and "podman-machine" in err
    code, out, err = alloc(capsys, "consumer", "measure", "desktop", url)
    assert code == 2 and "no probed consumer" in err
    # 60 GiB x 1.25 does not fit beside the others
    host.rss = 60 * GIB
    code, out, err = alloc(
        capsys,
        "consumer",
        "measure",
        "yard",
        url,
        "--for",
        "0s",
        "--every",
        "10s",
        "--declare",
    )
    assert code == 2
    assert "allocator: hold refused on laptop: memory" in out
    assert head_of(opener) == mark


@over_backends
def test_consumer_measure_of_a_consumer_that_is_down_is_an_error(
    backend, record, url, host, capsys, tmp_path, opener
):
    """templet:3161 -- a consumer with nothing resident has no size: exit 1,
    naming the consumer, and nothing is written."""
    one_writer(backend, record)
    declare_sample(capsys, tmp_path, url)
    host.rss = None
    mark = head_of(opener)
    code, out, err = alloc(
        capsys, "consumer", "measure", "yard", url, "--for", "0s", "--every", "10s"
    )
    assert (code, out) == (1, "")
    assert err.startswith("error: ") and "yard" in err
    assert head_of(opener) == mark


@over_backends
def test_consumer_measure_over_five_seconds_prints_a_phase_line(
    backend, record, url, host, alloc_cli, monkeypatch, capsys, tmp_path
):
    """templet:3161 -- a `consumer measure` that runs past five seconds prints
    at least one phase line, with the samples done, the peak and the elapsed
    time, before it ends; one that ends sooner prints none. It really waits."""
    one_writer(backend, record)
    declare_sample(capsys, tmp_path, url)
    monkeypatch.setattr(alloc_cli, "SLEEP", time.sleep)
    began = time.monotonic()
    code, out, err = alloc(
        capsys, "consumer", "measure", "yard", url, "--for", "7s", "--every", "1s"
    )
    assert code == 0 and time.monotonic() - began >= 7
    phases = [line for line in err.splitlines() if line.startswith("sampling yard: ")]
    assert phases, err
    assert phases[0].startswith("sampling yard: ") and "/8, peak 1.0 GiB, 0:0" in (
        phases[0]
    )
    assert "consumer yard: peak 1.0 GiB" in out
    code, out, err = alloc(
        capsys, "consumer", "measure", "yard", url, "--for", "2s", "--every", "1s"
    )
    assert code == 0 and "sampling" not in err


@over_backends
def test_consumer_measure_phase_lines_come_every_thirty_seconds(
    backend, record, url, host, alloc_cli, monkeypatch, capsys, tmp_path
):
    """templet:3161 -- past five seconds the first phase line comes, then one
    every 30 seconds: `sampling yard: 5/31, peak 1.0 GiB, 0:40`."""
    one_writer(backend, record)
    declare_sample(capsys, tmp_path, url)
    clock = [0.0]

    def sleep(seconds):
        clock[0] += seconds

    monkeypatch.setattr(alloc_cli, "SLEEP", sleep)
    monkeypatch.setattr(alloc_cli, "CLOCK", lambda: clock[0])
    code, out, err = alloc(
        capsys, "consumer", "measure", "yard", url, "--for", "5m", "--every", "10s"
    )
    assert code == 0, err
    phases = [line for line in err.splitlines() if line.startswith("sampling yard: ")]
    assert len(phases) == 10
    assert phases[0] == "sampling yard: 2/31, peak 1.0 GiB, 0:10"
    assert phases[1] == "sampling yard: 5/31, peak 1.0 GiB, 0:40"
    assert phases[-1] == "sampling yard: 29/31, peak 1.0 GiB, 4:40"


@over_backends
def test_declare_says_its_phases_and_the_sampling_it_waits_for(
    backend, record, url, host, alloc_cli, monkeypatch, capsys, tmp_path
):
    """templet:3161 -- `declare` prints each of its phases with the elapsed
    time once one passes the slow bound, and the yard's sampling as phase
    lines while resolve waits for it."""
    one_writer(backend, record)
    clock = [0.0]

    def sleep(seconds):
        clock[0] += seconds

    monkeypatch.setattr(alloc_cli, "SLEEP", sleep)
    monkeypatch.setattr(alloc_cli, "CLOCK", lambda: clock[0])
    monkeypatch.setattr(cli, "SLOW_PHASE_S", 0.0)
    code, out, err = alloc(capsys, "declare", write_doc(tmp_path), url)
    assert code == 0, out
    assert "sampling yard: 2/61, peak 1.0 GiB, 0:10" in err.splitlines()
    for phase in ("opening the record: ", "resolving: ", "declaring: "):
        assert phase in err


# -- --timing and --debug -------------------------------------------------------------


def run_each(capsys, url, doc, task, now):
    """Every command once, with its flags: `(name, argv)`."""
    return [
        ("declare", ["declare", doc, url, "--dry-run"]),
        ("show", ["show", url, "--now", now]),
        ("place", ["place", url, task, "--now", now]),
        ("measure", ["measure", url]),
        (
            "consumer measure",
            ["consumer", "measure", "yard", url, "--for", "10s", "--every", "10s"],
        ),
    ]


@over_backends
def test_every_command_takes_timing_and_leaves_its_output_as_it_was(
    backend, effort, url, host, clock, capsys, tmp_path
):
    """templet:3161 -- every `alloc` command takes `--timing`, which prints on
    stderr the time and the calls of the store and of the probes, and leaves
    stdout as it was; a command that reads the store shows its calls, and
    `measure` the probes'."""
    record = effort
    declared(record, clock)
    task = str(make_task(record))
    now = str(clock())
    doc = write_doc(tmp_path)
    one_writer(backend, record)
    for name, argv in run_each(capsys, url, doc, task, now):
        code, plain, _ = alloc(capsys, *argv)
        code2, out, err = alloc(capsys, *argv, "--timing")
        assert code2 == code, (name, err)
        assert out == plain, name
        line = next(x for x in err.splitlines() if x.startswith("timing: "))
        assert " store " in line and " probe " in line and "ms" in line, (name, line)
        if name == "measure":
            assert "probe 0.0 ms (0 calls)" not in line
        assert "store 0.0 ms (0 calls)" not in line, (name, line)
    code, out, err = alloc(capsys, "declare", doc, url, "--timing")
    assert code == 0 and "timing: store " in err


@over_backends
def test_every_command_takes_debug_and_prints_reads_shorts_and_writes(
    backend, effort, url, host, clock, capsys, tmp_path, opener
):
    """templet:3161 -- `--debug` prints on stderr each read the command makes
    (`read: `), each short of a refusal (`short: `) and each write (`write: `),
    and leaves stdout as it was. A read-only command prints no write; a
    declare prints one write line for each event it makes."""
    record = effort
    declared(record, clock)
    ok = str(make_task(record))
    huge = str(make_task(record, needs={"memory": 60 * GIB, "disk": 300 * GIB}))
    now = str(clock())
    doc = write_doc(tmp_path)
    one_writer(backend, record)
    for name, argv in run_each(capsys, url, doc, ok, now):
        _, plain, _ = alloc(capsys, *argv)
        code, out, err = alloc(capsys, *argv, "--debug")
        assert out == plain, name
        assert any(x.startswith("read: ") for x in err.splitlines()), name
        assert not any(x.startswith("write: ") for x in err.splitlines()), name
    code, out, err = alloc(capsys, "place", url, huge, "--now", now, "--debug")
    assert code == 2
    shorts = [x for x in err.splitlines() if x.startswith("short: ")]
    assert any("room memory asked 60.0 GiB" in x for x in shorts)
    assert any("room disk asked 300.0 GiB" in x for x in shorts)
    assert all(x.endswith(("waits", "never")) for x in shorts)


@over_backends
def test_debug_prints_one_write_line_for_each_event_a_command_makes(
    backend, record, url, host, capsys, tmp_path, opener
):
    """templet:3161 -- a declare with `--debug` prints one `write:` line for
    each event it makes, naming the operation, and `measure` and `consumer
    measure --declare` print theirs."""
    one_writer(backend, record)
    doc = write_doc(tmp_path)
    mark = head_of(opener)
    code, out, err = alloc(capsys, "declare", doc, url, "--debug")
    assert code == 0, err
    writes = [x for x in err.splitlines() if x.startswith("write: ")]
    assert len(writes) == head_of(opener) - mark
    assert writes[0].startswith("write: declare_node(")
    assert any(x.startswith("write: set_capacity(") for x in writes)
    assert any(x.startswith("write: reserve(") for x in writes)
    assert writes[-1].startswith("write: record_reading(")
    mark = head_of(opener)
    code, out, err = alloc(capsys, "measure", url, "--debug")
    assert code == 0
    assert [x.split("(")[0] for x in err.splitlines() if x.startswith("write: ")] == [
        "write: record_reading"
    ]
    assert head_of(opener) == mark + 1
    mark = head_of(opener)
    code, out, err = alloc(
        capsys,
        "consumer",
        "measure",
        "yard",
        url,
        "--for",
        "0s",
        "--every",
        "10s",
        "--declare",
        "--debug",
    )
    assert code == 0, err
    writes = [x.split("(")[0] for x in err.splitlines() if x.startswith("write: ")]
    assert writes == ["write: replace_reservation", "write: record_reading"]
    assert head_of(opener) == mark + 2


@over_backends
def test_no_output_carries_more_than_a_figure_or_a_name(
    backend, record, url, host, capsys, tmp_path, opener
):
    """templet:3161 -- no output of any command, with or without `--debug`
    and `--timing`, carries a consumer's free text from the document."""
    one_writer(backend, record)
    text = sample_text().replace(
        "the yard: its supervisor, its store and its services", SECRET
    )
    assert SECRET in text
    doc = write_doc(tmp_path, text)
    seen = []
    for argv in (
        ["declare", doc, url, "--dry-run", "--debug", "--timing"],
        ["declare", doc, url, "--debug", "--timing"],
        ["measure", url, "--debug", "--timing"],
        ["show", url, "--debug", "--timing"],
        ["show", url, "--json"],
        ["place", url, "1", "--debug"],
        ["consumer", "measure", "yard", url, "--for", "0s", "--debug", "--declare"],
    ):
        _, out, err = alloc(capsys, *argv)
        seen.append(out + err)
    assert all(seen[:5]) and not any(SECRET in shown for shown in seen)


def test_a_real_measure_shows_no_command_line(tmp_path, capsys):
    """templet:3161 -- the real probes behind `measure`, through the command
    on a scratch record: a process with a distinctive argument, probed by name,
    shows its resident size and nothing of its command line."""
    needle = "needle-4c1d"
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", needle]
    )
    try:
        url = f"sqlite:///{tmp_path / 'record.sqlite'}"
        assert cli.main(["record", "init", url]) == 0
        consumers = (
            '[[consumer]]\nname = "snooze"\nmemory = "4GiB"\n'
            f'probe = {{ pid = {child.pid} }}\nwhy = "test"\n'
        )
        doc = write_doc(
            tmp_path,
            MINI.format(disk_path=tmp_path, tolerance=1000, consumers=consumers),
            "mini.toml",
        )
        capsys.readouterr()
        for argv in (["declare", doc, url], ["measure", url, "--debug"], ["show", url]):
            code = cli.main(["alloc", *argv])
            out, err = capsys.readouterr()
            assert code == 0, err
            assert needle not in out + err
        assert "snooze" in out
    finally:
        child.kill()
        child.wait()
