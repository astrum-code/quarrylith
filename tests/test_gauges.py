# SPDX-License-Identifier: Apache-2.0
"""The gauges, over every backend in BACKENDS (templet:3159, build step 4):
each computed from a fixture record through the contract alone, and `stalls`
at its margins."""

import ast
import json
import sys
from pathlib import Path

import pytest
from backends import admitted, over_backends, url_for
from samples import load_sample

from quarrylith import cli, gauges
from quarrylith.gauges import GAUGES, Gauge
from quarrylith.record import contract
from quarrylith.record.importing import ImportRefused, check_import, compose

MIN = 60_000
DAY = 24 * 60 * MIN

#: The page's table of gauges, in its order.
PAGE = (
    "capacity",
    "reserved",
    "free",
    "queue_depth",
    "runs_active",
    "run_age",
    "filed_to_start",
    "start_to_done",
    "outcomes",
    "unmerged_changes",
    "review_rounds",
    "budget_pace",
    "footprint",
    "stalls",
)

#: What a gauge may call on the record: the contract's reads, nothing else.
READS = (
    "get",
    "read",
    "reading",
    "ready",
    "free",
    "lease_view",
    "effective_labels",
    "head",
)


#: The gauges' module, whose source the AST check reads.
GAUGES_PY = Path(gauges.__file__)
#: What `quarrylith.gauges` may import: of the standard library, these; of
#: quarrylith, `.record.contract` alone, and of it only `efforts_of`.
STDLIB = {"math", "dataclasses"}
CONTRACT_NAMES = {"efforts_of"}
#: Names that reach an attribute, or code, by a name in a string.
BY_NAME = {"getattr", "vars", "eval", "exec", "compile", "__import__"}


def contract_only(record):
    """The record's contract reads, and nothing else: the reads are bound
    here, and the object a gauge gets holds no record. Any other attribute, a
    private one or an operation, fails the test."""
    reads = {name: getattr(record, name) for name in READS}

    class ContractOnly:
        __slots__ = ()

        def __getattr__(self, name):
            if name not in reads:
                raise AssertionError(f"a gauge read {name!r}, outside the contract")
            return reads[name]

    return ContractOnly()


def at(t0, minutes):
    return t0 + int(minutes * MIN)


def fixture(record, clock):
    """A record with work in flight and landed: two nodes, three items, five
    tasks and their runs, changes, reviews and budget readings, at minute 60
    of the clock."""
    t0 = clock()
    admitted(record, "0001")
    record.file_effort("0002", "e2", "Parked", "lead", {}, standing="parked")
    record.declare_node("n1", ["harness", "process"], [])
    record.set_capacity("n1", "slots", 4, "slots")
    record.set_capacity("n1", "memory", 4096, "bytes")
    record.declare_node("n2", ["vm"], [])
    record.set_capacity("n2", "slots", 2, "slots")
    record.reserve("n1", {"memory": 1024}, holder="cache", from_at=t0, until_at=None)

    def item(title, role, effort, labels):
        return record.file_item(
            "task",
            title,
            "",
            role=role,
            effort=effort,
            labels=labels,
            filed_by="lead",
        )

    i1 = item("one", "alpha", "0001", ["repo:widgets"])
    i2 = item("two", "beta", "0001", ["repo:gadgets"])
    item("three", None, "0002", [])
    t_a = record.file_task(
        "agent", number=i1, needs={"memory": 512, "slots": 1}, sandbox="harness"
    )
    t_b = record.file_task("agent", number=i2, needs={"slots": 1}, sandbox="harness")

    def job():
        return record.file_task(
            "job", effort="0001", needs={"slots": 1}, sandbox="process"
        )

    j1, j2, j3 = job(), job(), job()

    def start(task, holder, fence, expect_s):
        return record.start_run(
            task,
            holder,
            node="n1",
            sandbox=record.get("tasks", task)["sandbox"],
            fence=fence,
            expect_s=expect_s,
        )

    def finish(run, outcome, footprint):
        record.finish_run(run, outcome, "", footprint=footprint, cost={})

    clock.at = at(t0, 10)
    r1 = start(t_a, "alpha", record.grant(i1, "alpha", branch="b1", machine="m"), 60)
    clock.at = at(t0, 20)
    r2 = start(t_b, "beta", record.grant(i2, "beta", branch="b2", machine="m"), None)
    clock.at = at(t0, 30)
    r3 = start(j1, "gamma", None, 600)
    clock.at = at(t0, 35)
    finish(r3, "done", {"memory": 300})
    clock.at = at(t0, 40)
    r4 = start(j2, "gamma", None, 60)
    clock.at = at(t0, 42)
    r5 = start(j3, "gamma", None, None)
    clock.at = at(t0, 45)
    finish(r5, "failed", {"memory": 700, "note": "not a number"})

    c1 = record.open_or_update_change(
        "widgets", "feat/1", "h1", title="one", number=i1, by="alpha"
    )
    c2 = record.open_or_update_change(
        "widgets", "feat/2", "h2", title="two", number=i2, by="beta"
    )
    record.record_merge(c2, "h2", "user", "reviewed")
    c3 = record.open_or_update_change("gadgets", "feat/3", "h3", title="x", by="beta")
    record.open_or_update_change("gadgets", "feat/4", "h4", title="y", by="beta")
    record.drop_change(c3, "beta", "not needed")
    for target, round_, heads in ((i1, 1, {str(c1): "h1"}), (i1, 2, {}), (i2, 1, {})):
        record.file_review(target, round_, "pool", author="alpha", heads=heads)

    def reading(minutes, used, resets):
        clock.at = at(t0, minutes)
        value = {"provider": "acme", "resets_at": resets, "utilization": used}
        record.record_reading("budget", "five_hour", value)

    reading(20, 0.9, at(t0, 25))
    reading(30, 0.2, at(t0, 300))
    reading(50, 0.3, at(t0, 300))
    record.record_reading("other", "five_hour", {"utilization": 0.99})

    clock.at = at(t0, 50)
    record.beat(r1)
    record.beat(r2)
    clock.at = at(t0, 55)
    record.run_phase(r2, "testing")
    clock.at = at(t0, 60)
    return t0, (r1, r2, r4)


def values(gauge, record, now, window_ms=DAY):
    got = gauge.compute(contract_only(record), now=now, window_ms=window_ms)
    return {tuple(sorted(labels.items())): value for labels, value in got}


def series(**labels):
    return tuple(sorted(labels.items()))


def test_gauges_are_the_pages_list():
    """templet:3159 -- `GAUGES` holds the page's gauges, in its order, each a
    `Gauge` naming its unit, its labels and its help."""
    assert tuple(g.name for g in GAUGES) == PAGE
    for g in GAUGES:
        assert isinstance(g, Gauge)
        assert g.unit and g.help and callable(g.compute)
        assert isinstance(g.labels, tuple) and g.labels
    assert gauges.by_name("stalls").labels == ("node",)


@over_backends
def test_every_gauge_computes_from_a_fixture_through_the_contract(record, clock):
    """templet:3159 -- every gauge in `GAUGES` computes from a fixture record,
    through the contract only, to the expected values."""
    t0, (r1, r2, r4) = fixture(record, clock)
    now = clock()
    want = {
        "capacity": {
            series(node="n1", resource="memory"): 4096,
            series(node="n1", resource="slots"): 4,
            series(node="n2", resource="slots"): 2,
        },
        "reserved": {
            series(node="n1", resource="memory"): 1536,
            series(node="n1", resource="slots"): 3,
            series(node="n2", resource="slots"): 0,
        },
        "free": {
            series(node="n1", resource="memory"): 2560,
            series(node="n1", resource="slots"): 1,
            series(node="n2", resource="slots"): 2,
        },
        "queue_depth": {
            series(of="items", state="claimed", effort="0001", label="*"): 2,
            series(of="items", state="claimed", effort="0001", label="repo:widgets"): 1,
            series(of="items", state="claimed", effort="0001", label="repo:gadgets"): 1,
            series(of="items", state="triage", effort="0002", label="*"): 1,
            series(of="tasks", state="running", effort="0001", label="*"): 3,
            series(of="tasks", state="running", effort="0001", label="repo:widgets"): 1,
            series(of="tasks", state="running", effort="0001", label="repo:gadgets"): 1,
            series(of="tasks", state="open", effort="0001", label="*"): 2,
        },
        "runs_active": {
            series(phase="started", node="n1"): 2,
            series(phase="testing", node="n1"): 1,
        },
        "run_age": {
            series(run=str(r1)): 3000,
            series(run=str(r2)): 2400,
            series(run=str(r4)): 1200,
        },
        "filed_to_start": {
            series(kind="agent", effort="0001", stat="p50"): 600,
            series(kind="agent", effort="0001", stat="p90"): 1200,
            series(kind="job", effort="0001", stat="p50"): 2400,
            series(kind="job", effort="0001", stat="p90"): 2520,
        },
        "start_to_done": {
            series(kind="job", effort="0001", stat="p50"): 180,
            series(kind="job", effort="0001", stat="p90"): 300,
        },
        "outcomes": {
            series(kind="job", outcome="done"): 1,
            series(kind="job", outcome="failed"): 1,
        },
        "unmerged_changes": {
            series(repo="gadgets"): 1,
            series(repo="widgets"): 1,
        },
        "review_rounds": {
            series(effort="0001", stat="median"): 1,
            series(effort="0001", stat="worst"): 2,
        },
        "budget_pace": {
            series(provider="acme", window="five_hour"): 30,
        },
        "footprint": {
            series(kind="job", resource="memory"): 700,
        },
        "stalls": {
            series(node="n1"): 1,
        },
    }
    assert set(want) == set(PAGE)
    for g in GAUGES:
        got = values(g, record, now)
        assert got == pytest.approx(want[g.name]), g.name
    # a window that ends before the runs finished sees none of them
    early = {g.name: values(g, record, at(t0, 33), 3 * MIN) for g in GAUGES}
    assert early["outcomes"] == {} and early["start_to_done"] == {}
    assert early["footprint"] == {}
    assert early["filed_to_start"] == {
        series(kind="job", effort="0001", stat="p50"): 1800,
        series(kind="job", effort="0001", stat="p90"): 1800,
    }
    # the whole set, in one reading, as the command gives it
    every = gauges.measure(contract_only(record), now=now, window_ms=DAY)
    assert list(every) == list(PAGE)
    for name, rows in every.items():
        got = {tuple(sorted(lab.items())): v for lab, v in rows}
        assert got == pytest.approx(want[name]), name


@over_backends
def test_stalls_counts_a_run_past_its_margin_and_not_one_inside_it(record, clock):
    """templet:3159 -- `stalls` counts a live run that is alive (it beats)
    with no phase change past `expect_until` + 5 min, or 10 min with no
    expectation, and not one inside its margin; nor one that stopped
    beating."""
    t0 = clock()
    admitted(record)
    record.declare_node("n1", ["process"], [])
    record.declare_node("n2", ["process"], [])
    for name in ("n1", "n2"):
        record.set_capacity(name, "slots", 9, "slots")

    def run(node, expect_s, beat_s=None):
        task = record.file_task(
            "job",
            effort="0001",
            needs={"slots": 1},
            sandbox="process",
            supervision={"beat_s": beat_s},
        )
        return record.start_run(
            task, "alpha", node=node, sandbox="process", fence=None, expect_s=expect_s
        )

    expecting = run("n1", 60)  # stalls past t0 + 1 min + 5 min
    quiet = run("n1", None)  # stalls past t0 + 10 min
    other = run("n2", None)
    run("n2", None)  # never beats: not alive, never a stall
    beating = run("n2", None, beat_s=30)  # alive only while it beats each 30 s

    def beat_all(minute):
        clock.at = at(t0, minute)
        for r in (expecting, quiet, other, beating):
            record.beat(r)

    def stalls(now):
        return values(gauges.by_name("stalls"), record, now)

    beat_all(5)
    assert stalls(at(t0, 6)) == {series(node="n1"): 0, series(node="n2"): 0}
    assert stalls(at(t0, 6) + 1) == {series(node="n1"): 1, series(node="n2"): 0}
    beat_all(9.5)
    assert stalls(at(t0, 10)) == {series(node="n1"): 1, series(node="n2"): 0}
    assert stalls(at(t0, 10) + 1) == {series(node="n1"): 2, series(node="n2"): 2}
    # a phase change restarts the margin
    clock.at = at(t0, 11)
    record.run_phase(other, "testing", expect_s=120)
    beat_all(11)
    assert stalls(at(t0, 11) + 1) == {series(node="n1"): 2, series(node="n2"): 1}
    # past two missed beats it is not alive, so not a stall
    assert stalls(at(t0, 12)) == {series(node="n1"): 2, series(node="n2"): 1}
    assert stalls(at(t0, 12) + 1) == {series(node="n1"): 2, series(node="n2"): 0}
    # a finished run is no stall
    record.finish_run(quiet, "done", "", footprint={}, cost={})
    assert stalls(at(t0, 12) + 1)[series(node="n1")] == 1


@over_backends
def test_queue_depth_counts_an_item_in_two_repos_under_each(record):
    """templet:3159 -- `queue_depth` counts an item in two repos once under
    each `repo:` label and once under `*`, and its task the same by its
    effective labels."""
    admitted(record)
    n = record.file_item(
        "task",
        "both",
        "",
        role="alpha",
        effort="0001",
        labels=["repo:gadgets", "repo:widgets"],
        filed_by="lead",
    )
    record.file_task("agent", number=n, sandbox="harness")
    got = values(gauges.by_name("queue_depth"), record, now=1)
    for of, state in (("items", "ready"), ("tasks", "open")):
        for label in ("*", "repo:gadgets", "repo:widgets"):
            assert got[series(of=of, state=state, effort="0001", label=label)] == 1
    assert len(got) == 6


@over_backends
def test_the_gauges_compute_over_the_sample_import(record):
    """Every gauge computes over the sample import, whose runs sit on the
    retired node `legacy`."""
    doc = load_sample()
    record.load(compose(doc))
    now = doc["source"]["read_at"]
    every = gauges.measure(contract_only(record), now=now, window_ms=30 * DAY)
    assert list(every) == list(PAGE)
    assert every["capacity"] == []
    # its live run stopped beating an hour before the export
    assert every["stalls"] == [({"node": "legacy"}, 0)]
    assert every["unmerged_changes"] == [({"repo": "widgets"}, 1)]
    assert every["outcomes"] == [
        ({"kind": "agent", "outcome": "failed"}, 1),
        ({"kind": "agent", "outcome": "limited"}, 1),
        ({"kind": "agent", "outcome": "pr"}, 2),
        ({"kind": "agent", "outcome": "review"}, 1),
    ]
    # utilization from 0.2 to 0.82 of the period over four hours
    assert every["budget_pace"] == [
        ({"provider": "", "window": "five_hour"}, pytest.approx(15.5))
    ]


def test_the_gauges_reach_the_record_only_by_the_contract():
    """templet:3159 -- `quarrylith.gauges`, read as source, passes `reaches`:
    it reaches the record by the contract's reads alone."""
    assert reaches(GAUGES_PY.read_text(encoding="utf-8")) == []
    assert "sqlite3" in sys.stdlib_module_names and "sqlite3" not in STDLIB


def reaches(source):
    """What `source` (a module, as text) reaches past the contract, a list of
    `<line>: <what>`; empty when it:

    - imports only `math` and `dataclasses` of the standard library (never
      `sqlite3`), and of quarrylith only `contract` from `.record`;
    - names nothing it imports by another name (no `as`);
    - uses `contract` only as the base of `contract.<name>`, and takes only
      `efforts_of` off it;
    - reads no `_`-prefixed attribute (so nothing private off a gauge's
      `record`, and no `__self__`);
    - names none of `BY_NAME`, nor `__builtins__`, whether it calls it or
      not, so nothing reaches an attribute, or code, by a name in a string.

    It is a lint over plain Python, not a sandbox: a gauge is reviewed code.
    """
    tree = ast.parse(source)
    parents = {
        child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
    }
    found = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.asname is not None:
                    found.append(f"{line}: imports {alias.name} as {alias.asname}")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in STDLIB:
                    found.append(f"{line}: imports {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            names = {a.name for a in node.names}
            if node.level == 0 and node.module in STDLIB:
                continue
            if not (node.level == 1 and node.module == "record"):
                found.append(f"{line}: imports from {node.module}")
            elif names != {"contract"}:
                found.append(f"{line}: imports {names} from .record")
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                found.append(f"{line}: reads {node.attr}")
            base = node.value
            if isinstance(base, ast.Name) and base.id == "contract":
                if node.attr not in CONTRACT_NAMES:
                    found.append(f"{line}: takes contract.{node.attr}")
        elif isinstance(node, ast.Name):
            if node.id in BY_NAME or node.id == "__builtins__":
                found.append(f"{line}: names {node.id}")
            elif node.id == "contract":
                parent = parents.get(node)
                if not (isinstance(parent, ast.Attribute) and parent.value is node):
                    found.append(f"{line}: uses contract other than contract.<name>")
    return found


#: The module's own lines, then a way past the contract and what `reaches`
#: says of it: round 2's two escapes first.
ESCAPES = (
    (
        "def f(record):\n"
        "    ga = getattr\n"
        "    real = ga(record.read, '__se' + 'lf__')\n"
        "    return ga(ga(contract, 'sqlite'), 'sqlite3').connect(real.path)\n",
        "names getattr",
    ),
    (
        "def f(record):\n    return __builtins__['getattr'](record.read, '__self__')\n",
        "names __builtins__",
    ),
    ("c = contract\n", "uses contract other than contract.<name>"),
    ("def f(record):\n    return g(contract)\n", "uses contract other than"),
    ("from .record import contract as c\n", "imports contract as c"),
    ("from math import fsum as add\n", "imports fsum as add"),
    ("def f():\n    return vars\n", "names vars"),
    ("def f(record):\n    return record.read.__self__\n", "reads __self__"),
)


@pytest.mark.parametrize(
    "escape, says",
    ESCAPES,
    ids=[
        "getattr-renamed",
        "builtins",
        "contract-bound",
        "contract-passed",
        "contract-as",
        "import-as",
        "vars-named",
        "self",
    ],
)
def test_the_contract_check_finds_a_way_past_it(escape, says):
    """templet:3159 -- `reaches` finds each way past the contract: `getattr`
    taken under another name, `__builtins__`, `contract` used other than as
    `contract.<name>`, or imported under another name."""
    source = GAUGES_PY.read_text(encoding="utf-8") + "\n\n" + escape
    found = reaches(source)
    assert any(says in f for f in found), found


@over_backends
def test_run_age_lists_its_series_sorted_by_their_labels(record, clock):
    """`run_age`'s series are sorted by their labels, as every gauge's are:
    run `"10"` before run `"9"`."""
    admitted(record)
    record.declare_node("n1", ["process"], [])
    record.set_capacity("n1", "slots", 20, "slots")
    runs = []
    for _ in range(11):
        task = record.file_task(
            "job", effort="0001", needs={"slots": 1}, sandbox="process"
        )
        runs.append(
            record.start_run(
                task, "alpha", node="n1", sandbox="process", fence=None, expect_s=None
            )
        )
        clock.step()
    got = gauges.by_name("run_age").compute(
        contract_only(record), now=clock(), window_ms=DAY
    )
    labels = [lab["run"] for lab, _ in got]
    assert labels == sorted(str(r) for r in runs)
    assert labels[:3] == ["1", "10", "11"]


@over_backends
def test_a_footprint_or_supervision_that_is_not_a_dict_breaks_no_gauge(record, clock):
    """A run's `footprint` is any JSON, so `finish_run` takes one that is not
    a dict: `footprint` skips it, and every gauge still computes."""
    admitted(record)
    record.declare_node("n1", ["process"], [])
    record.set_capacity("n1", "slots", 9, "slots")

    def finished(footprint):
        task = record.file_task(
            "job", effort="0001", needs={"slots": 1}, sandbox="process"
        )
        run = record.start_run(
            task, "alpha", node="n1", sandbox="process", fence=None, expect_s=None
        )
        clock.step()
        record.finish_run(run, "done", "", footprint=footprint, cost={})

    for footprint in ([1, 2], "lots", 7, False, {"memory": 5}):
        finished(footprint)
    every = gauges.measure(contract_only(record), now=clock(), window_ms=DAY)
    assert list(every) == list(PAGE)
    assert every["footprint"] == [({"kind": "job", "resource": "memory"}, 5)]
    assert every["outcomes"] == [({"kind": "job", "outcome": "done"}, 5)]


#: A task's `supervision` no operation writes, which an import refuses and a
#: dump made by hand can carry: none of these is a positive `beat_s`, so each
#: run beats by the 10 minutes.
SUPERVISIONS = (
    [1],
    7,
    "every minute",
    {"beat_s": "soon"},
    {"beat_s": -30},
    {"beat_s": True},
    {"beat_s": 0},
    {"beat_s": [30]},
)


def by_hand(dump, kind, key, field, value):
    """`dump` with one record's `field` set to `value`, in its records and in
    every put of it in the journal: a dump made by hand, which `check_dump`
    takes as the fold of its journal."""
    dump = json.loads(json.dumps(dump))
    for rec in dump["records"][kind]:
        if [rec[n] for n in contract.RECORDS[kind].key_names] == key:
            rec[field] = value
    for event in dump["journal"]:
        for put_kind, put_key, put in event["puts"]:
            if put_kind == kind and put_key == key and put is not None:
                put[field] = value
    return dump


@over_backends
def test_a_footprint_or_supervision_not_a_dict_from_an_import_or_a_dump(
    backend, tmp_path, capsys
):
    """An import carries a run's `footprint` as any JSON, and a footprint that
    is not a dict reports nothing. A task's `supervision` that no operation
    writes is refused by the import, and a dump made by hand can still carry
    one: a `beat_s` that is not a positive number is none (the run alive
    within 10 minutes of a beat). `measure` and `quarrylith record gauges`
    compute every gauge over each."""
    doc = load_sample()
    runs = {r["id"]: r for r in doc["records"]["runs"]}
    runs[1]["footprint"] = [1]
    runs[2]["footprint"] = "much"
    runs[4]["footprint"] = {"memory": 64}
    live = next(r for r in runs.values() if r["finished_at"] is None)
    beat = next(b for b in doc["records"]["beats"] if b["run"] == live["id"])
    # five minutes past the live run's last beat: alive by the 10 minutes
    now = beat["at"] + 5 * MIN
    path = tmp_path / "doc.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    url = url_for(backend, tmp_path / "imported")
    code, _, err = run_cli(capsys, "record", "import", str(path), "--into", url)
    assert code == 0, err
    for i, supervision in enumerate(SUPERVISIONS):
        bad = json.loads(json.dumps(doc))
        for task in bad["records"]["tasks"]:
            if task["id"] == live["task"]:
                task["supervision"] = supervision
        with pytest.raises(ImportRefused, match="supervision"):
            check_import(bad)
        dump = by_hand(
            compose(doc), "tasks", [live["task"]], "supervision", supervision
        )
        url = url_for(backend, tmp_path / f"record-{i}")
        record = contract.open_record(url, actor="alpha", via="cli")
        try:
            record.init()
            record.load(dump)
            assert record.get("tasks", live["task"])["supervision"] == supervision
            every = gauges.measure(contract_only(record), now=now, window_ms=30 * DAY)
        finally:
            record.close()
        assert list(every) == list(PAGE), supervision
        assert every["stalls"] == [({"node": "legacy"}, 1)], supervision
        assert every["footprint"] == [
            ({"kind": "agent", "resource": "memory"}, 64)
        ], supervision
        code, out, err = run_cli(capsys, "record", "gauges", url, "--now", str(now))
        assert code == 0, err
        assert 'stalls{node="legacy"} 1' in out.splitlines(), supervision


def run_cli(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err
