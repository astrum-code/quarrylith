# SPDX-License-Identifier: Apache-2.0
"""The allocator refuses new work on a host under memory pressure
(templet:3161, the memory-pressure addition): `place` and `hold` read the
host's pressure level and its swap live, and refuse at warn or worse, or
with the swap nearly full. The reads are injected, so a test sets each level;
the real read runs once on the test host, read-only. Every test that touches
a record runs over every backend in BACKENDS."""

import json
import sys

import pytest
from backends import one_writer, over_backends
from conftest import REAL_PROBE_PRESSURE
from test_allocator_commands import FakeHost, alloc
from test_allocator_host import GIB, declared, host_reading, make_task

from quarrylith.allocator import Fit, Refusal
from quarrylith.allocator import probes as real_probes
from quarrylith.allocator.host import HostPlacement, ProbeError
from quarrylith.allocator import measure
from quarrylith.allocator.seam import CAUSES

MIB = 2**20


class Host:
    """The probes of a host whose memory pressure a test sets: every other probe
    is the real one. `reads` counts the reads."""

    def __init__(self, level=1, used=0, total=2 * GIB, fail=None):
        self.level, self.used, self.total, self.fail = level, used, total, fail
        self.reads = 0

    def probe_pressure(self):
        self.reads += 1
        if self.fail:
            raise ProbeError(self.fail)
        return {
            "level": self.level,
            "swap_used": self.used,
            "swap_total": self.total,
            "method": "given",
        }

    def __getattr__(self, name):
        return getattr(real_probes, name)


def setup(record, clock, host):
    """A declared node with a fresh generous host reading, a placement that
    reads `host`, and a task that fits it."""
    declared(record, clock)
    return HostPlacement(record, probes=host), make_task(record)


def pressure_of(answer):
    """The one `pressure` short of a refusal."""
    assert isinstance(answer, Refusal), answer
    (short,) = [s for s in answer.shorts if s.cause == "pressure"]
    return short


# -- the cause ----------------------------------------------------------------------


def test_pressure_is_a_cause_beside_the_others_in_the_pages_order():
    """templet:3161 -- `CAUSES` has `pressure` after `unmeasured` and before
    `budget`."""
    at = CAUSES.index("pressure")
    assert CAUSES[at - 1] == "unmeasured" and CAUSES[at + 1] == "budget"


# -- the level ----------------------------------------------------------------------


@over_backends
def test_place_refuses_at_warn_and_critical_and_names_the_level(effort, clock):
    """templet:3161 -- `place` on a host whose memory pressure is warn (2) or
    critical (4) is refused with one `pressure` short that waits, whose detail
    names the level; at normal (1) the task fits."""
    record = effort
    host = Host()
    placement, task = setup(record, clock, host)
    assert isinstance(placement.place(task, now=clock()), Fit)
    for level, word in ((2, "warn"), (4, "critical")):
        host.level = level
        answer = placement.place(task, now=clock())
        short = pressure_of(answer)
        assert short.detail == f"memory pressure is {word}"
        assert (short.node, short.resource, short.waits) == ("laptop", None, True)
        assert answer.waits is True and short.until is None
        assert f"memory pressure is {word}" in answer.text
        assert "\n" not in answer.text
    host.level = 1
    assert isinstance(placement.place(task, now=clock()), Fit)


@over_backends
def test_pressure_refuses_where_the_measured_room_says_yes(effort, clock):
    """templet:3161 -- the night that began it: 14.4 GiB available against a
    4 GiB floor, and the Mac at warn with 14 GiB of 15 GiB swap in use. The
    measured check passes; `place` refuses on the pressure alone, and on the
    swap alone."""
    record = effort
    host = Host(level=2, used=14 * GIB, total=15 * GIB)
    placement, task = setup(record, clock, host)
    clock.step()
    host_reading(record, clock, available=14 * GIB + 400 * MIB)
    answer = placement.place(task, now=clock())
    assert [s.cause for s in answer.shorts] == ["pressure", "pressure"]
    host.level = 1
    answer = placement.place(task, now=clock())
    assert [s.detail for s in answer.shorts] == ["swap 14.0 GiB of 15.0 GiB"]
    host.used, host.level = 0, 4
    answer = placement.place(task, now=clock())
    assert [s.detail for s in answer.shorts] == ["memory pressure is critical"]


# -- the swap -----------------------------------------------------------------------

#: (used, total, refused): the rule is a share of the total and a floor, and
#: both must hold. 14 of 15 GiB is the night's; the small swaps are macOS's.
SWAPS = [
    (14 * GIB, 15 * GIB, True),
    (8 * GIB, 9 * GIB, True),  # at the floor and past the share
    (8 * GIB - MIB, 9 * GIB, False),  # a byte-ish short of the floor
    (900 * MIB, 1000 * MIB, False),  # 90% of a small swap: no crisis
    (300 * MIB, 330 * MIB, False),
    (7 * GIB, 7 * GIB + 512 * MIB, False),  # 93% below the floor
    (20 * GIB, 40 * GIB, False),  # past the floor, half full: room to grow
    (8 * GIB, 10 * GIB, False),  # at the floor, 80%: under the share
    (17 * GIB // 2, 10 * GIB, True),  # exactly the 85% share
    (0, 0, False),  # no swap at all
]


@pytest.mark.parametrize("used,total,refused", SWAPS)
def test_the_swap_rule_is_a_floor_and_a_share_together(used, total, refused):
    """templet:3161 -- the host is refused when swap used is at least 8 GiB
    and at least 85% of the swap total, and only then: a small swap at 90% is
    no crisis, and a large one half full has room."""
    shorts = measure.pressure_shorts(
        {"level": 1, "swap_used": used, "swap_total": total}, "", "laptop"
    )
    assert bool(shorts) is refused
    if not refused:
        return
    (short,) = shorts
    shown = lambda n: f"{n / GIB:.1f} GiB"  # noqa: E731
    assert short.cause == "pressure"
    assert short.detail == f"swap {shown(used)} of {shown(total)}"
    assert (short.capacity, short.free, short.waits) == (total, total - used, True)


@over_backends
def test_place_refuses_a_nearly_full_swap_and_not_a_small_one(effort, clock):
    """templet:3161 -- through `place`: 14.0 of 15.0 GiB of swap is refused,
    with the detail in the refusal's line; 900 of 1000 MiB is not."""
    record = effort
    host = Host(used=14 * GIB, total=15 * GIB)
    placement, task = setup(record, clock, host)
    answer = placement.place(task, now=clock())
    assert pressure_of(answer).detail == "swap 14.0 GiB of 15.0 GiB"
    assert "swap 14.0 GiB of 15.0 GiB" in answer.text
    host.used, host.total = 900 * MIB, 1000 * MIB
    assert isinstance(placement.place(task, now=clock()), Fit)


# -- hold ---------------------------------------------------------------------------


@over_backends
def test_hold_refuses_under_pressure_but_a_declared_consumer_does_not(effort, clock):
    """templet:3161 -- `hold` of new work (a task's own VM, a standing
    reservation) is refused as `place` is; `hold` with `measured=False`, which
    `declare` uses for a consumer already running, is not, and `declare`
    itself writes under pressure."""
    record = effort
    host = Host(level=2)
    placement, task = setup(record, clock, host)
    held = placement.hold("vm:one", {"memory": GIB}, task=task, now=clock())
    assert isinstance(held, Refusal)
    assert pressure_of(held).detail == "memory pressure is warn"
    assert held.task == task
    assert [
        r for r in record.read("reservations", live=True) if r["holder"] == "vm:one"
    ] == []
    ids = placement.hold("seen", {"memory": GIB}, measured=False, now=clock())
    assert isinstance(ids, list) and ids
    again = HostPlacement(record, probes=host).declare(
        placement._stored()[0],
        figures={"capacity": {"memory": 48 * GIB, "disk": 200 * GIB}, "consumers": {}},
        now=clock(),
    )
    assert again.ok, again
    host.level = 1
    assert isinstance(
        placement.hold("vm:one", {"memory": GIB}, task=task, now=clock()), list
    )


# -- a read that fails, and a host with none ----------------------------------------


@over_backends
def test_a_failed_read_is_refused_unmeasured_naming_the_read(effort, clock):
    """templet:3161 -- a read that fails is never a yes: `place` and `hold`
    refuse `unmeasured` (every resource, waits), saying which read failed, and
    the next good read lets the task fit."""
    record = effort
    host = Host(fail="probe_pressure: sysctl exited 1")
    placement, task = setup(record, clock, host)
    for answer in (
        placement.place(task, now=clock()),
        placement.hold("x", {"memory": 1}, now=clock()),
    ):
        assert isinstance(answer, Refusal)
        (short,) = answer.shorts
        assert (short.cause, short.resource, short.waits) == ("unmeasured", None, True)
        assert "probe_pressure: sysctl exited 1" in short.detail
        assert "probe_pressure: sysctl exited 1" in answer.text
    host.fail = None
    assert isinstance(placement.place(task, now=clock()), Fit)


@over_backends
def test_a_host_with_no_pressure_reading_is_not_refused_for_that(effort, clock):
    """templet:3161 -- a platform with no memory pressure level, or no swap
    figures, gives None for them, and `place` and `hold` do not refuse for the
    missing reading alone."""
    record = effort
    host = Host()
    host.level, host.used, host.total = None, None, None
    placement, task = setup(record, clock, host)
    assert isinstance(placement.place(task, now=clock()), Fit)
    assert isinstance(placement.hold("x", {"memory": 1}, now=clock()), list)
    host.level, host.used, host.total = 2, None, None
    assert pressure_of(placement.place(task, now=clock())).detail.endswith("warn")
    host.level, host.used, host.total = None, 14 * GIB, 15 * GIB
    assert pressure_of(placement.place(task, now=clock())).detail.startswith("swap")


# -- fresh where it decides ---------------------------------------------------------


@over_backends
def test_each_place_and_hold_reads_the_host_afresh(effort, clock):
    """templet:3161 -- the reading is made at each `place` and `hold`, never
    taken from the record: a host reading written a second ago says nothing
    of the pressure, and a change between two calls changes the second's
    answer. `place` makes one read, and writes nothing."""
    record = effort
    host = Host()
    placement, task = setup(record, clock, host)
    host_reading(record, clock)  # a fresh host measurement, calm by itself
    mark = record.head()
    before = host.reads
    assert isinstance(placement.place(task, now=clock()), Fit)
    host.level = 2
    assert isinstance(placement.place(task, now=clock()), Refusal)
    host.level = 1
    assert isinstance(placement.place(task, now=clock()), Fit)
    assert host.reads == before + 3 and record.head() == mark
    assert isinstance(placement.hold("a", {"memory": 1}, now=clock()), list)
    assert host.reads == before + 4
    host.level = 2
    assert isinstance(placement.hold("b", {"memory": 1}, now=clock()), Refusal)
    assert host.reads == before + 5


# -- headroom -----------------------------------------------------------------------


@over_backends
def test_headroom_is_nothing_on_a_node_under_pressure(effort, clock):
    """templet:3161 -- while `place` refuses for pressure (or a failed read),
    `headroom` of the declared node is 0 for every resource; otherwise it is
    what it was."""
    record = effort
    host = Host()
    placement, task = setup(record, clock, host)
    calm = placement.headroom(now=clock())["laptop"]
    assert calm["memory"] > 0 and calm["slots.claude-code"] > 0
    for host_ in (Host(level=2), Host(used=14 * GIB, total=15 * GIB)):
        placement.probes = host_
        room = placement.headroom(now=clock())["laptop"]
        assert set(room) == set(calm) and set(room.values()) == {0}
    placement.probes = Host(fail="probe_pressure: broke")
    assert set(placement.headroom(now=clock())["laptop"].values()) == {0}
    placement.probes = host
    assert placement.headroom(now=clock())["laptop"] == calm


# -- a run already started, and a consumer measured ---------------------------------


@over_backends
def test_pressure_stops_nothing_that_already_runs(effort, clock):
    """templet:3161 -- a run started before the pressure keeps its
    reservation and finishes; `measure` of the host, and the reservation it
    holds, are the same under pressure: it only refuses new work."""
    record = effort
    host = Host()
    placement, task = setup(record, clock, host)
    fit = placement.place(task, now=clock())
    record.edit_task(task, needs=fit.needs)
    run = record.start_run(
        task, "w", node=fit.node, sandbox=fit.sandbox, fence=None, expect_s=60
    )
    held = record.read("reservations", live=True, task=task)
    host.level = 4
    other = make_task(record)
    assert isinstance(placement.place(other, now=clock()), Refusal)
    assert record.read("reservations", live=True, task=task) == held
    assert placement.measure(now=clock())["node"] == "laptop"
    record.finish_run(run, "done", "ok", footprint={}, cost={})
    assert record.read("reservations", live=True, task=task) == []


# -- the commands -------------------------------------------------------------------


@over_backends
def test_alloc_place_and_show_tell_the_pressure(
    backend, effort, url, clock, capsys, monkeypatch
):
    """templet:3161 -- `alloc place` exits 2 with the refusal's text, whose
    reason is the pressure, and `--json` carries the short; `alloc show` has a
    `pressure:` line that says new work is refused, or that the host is calm;
    no output has more than a figure or a name."""
    import quarrylith.alloc_cli as alloc_cli

    fake = FakeHost()
    monkeypatch.setattr(alloc_cli, "PROBES", fake)
    record = effort
    declared(record, clock)
    task = make_task(record)
    now = clock()
    one_writer(backend, record)
    args = ("place", url, str(task), "--now", str(now))
    code, out, err = alloc(capsys, *args)
    assert code == 0, (out, err)
    code, out, err = alloc(capsys, "show", url)
    assert code == 0 and "pressure: normal, swap 0.0 GiB of 2.0 GiB" in out.splitlines()
    fake.level = 2
    code, out, err = alloc(capsys, *args)
    assert code == 2 and err == ""
    assert "memory pressure is warn" in out and out.count("\n") == 1
    code, out, _ = alloc(capsys, *args, "--json")
    shorts = json.loads(out)["refusal"]["shorts"]
    assert [(s["cause"], s["detail"]) for s in shorts] == [
        ("pressure", "memory pressure is warn")
    ]
    code, out, _ = alloc(capsys, "show", url)
    assert "pressure: memory pressure is warn; new work is refused" in out.splitlines()
    code, out, _ = alloc(capsys, "show", url, "--json")
    assert json.loads(out)["pressure"]["refused"] == ["memory pressure is warn"]
    fake.level, fake.swap_used, fake.swap_total = 1, 14 * GIB, 15 * GIB
    code, out, _ = alloc(capsys, *args)
    assert code == 2 and "swap 14.0 GiB of 15.0 GiB" in out
    fake.swap_used = 0
    fake.pressure_fail = "probe_pressure: sysctl exited 1"
    code, out, _ = alloc(capsys, *args)
    assert code == 2 and "unmeasured: " in out and "sysctl exited 1" in out
    code, out, _ = alloc(capsys, "show", url)
    assert any(
        line.startswith("pressure: not read (probe_pressure: sysctl exited 1)")
        for line in out.splitlines()
    ), out


# -- the probe ----------------------------------------------------------------------

SWAPUSAGE = "total = 7168.00M  used = 5913.25M  free = 1254.75M  (encrypted)\n"
PSI = (
    "some avg10=0.00 avg60=0.12 avg300=0.40 total=123456\n"
    "full avg10={full} avg60=0.00 avg300=0.00 total=789\n"
)
MEMINFO = (
    "MemTotal:       16384000 kB\n"
    "SwapTotal:      {total} kB\n"
    "SwapFree:       {free} kB\n"
)


def test_swapusage_is_read_in_bytes():
    """templet:3161 -- `vm.swapusage`'s text gives the total and used swap in
    bytes, in whatever unit macOS wrote (K, M, G or T)."""
    parse = real_probes.parse_swapusage
    assert parse(SWAPUSAGE) == (7168 * MIB, round(5913.25 * MIB))
    assert parse("total = 2.00G  used = 1.50G  free = 0.50G") == (2 * GIB, 3 * GIB // 2)
    assert parse("total = 0.00M  used = 0.00M  free = 0.00M") == (0, 0)
    for bad in ("", "total = x", "total = 5M  free = 1M"):
        with pytest.raises(ProbeError, match="swapusage"):
            parse(bad)


def test_the_pressure_level_is_a_number_of_the_kernels():
    """templet:3161 -- the level is the kernel's own number: 1 normal, 2 warn,
    4 critical; text that is no number is a failed read."""
    assert real_probes.parse_pressure_level("1\n") == 1
    assert real_probes.parse_pressure_level("4") == 4
    for bad in ("", "warn", "-1", "0"):
        with pytest.raises(ProbeError, match="pressure level"):
            real_probes.parse_pressure_level(bad)


def fake_run(answers, calls=None):
    def run(probe, argv):
        if calls is not None:
            calls.append(argv)
        value = answers[tuple(argv)]
        if isinstance(value, Exception):
            raise value
        return value

    return run


def test_probe_pressure_on_macos_asks_sysctl_twice_and_nothing_else(monkeypatch):
    """templet:3161 -- on macOS the read is two `sysctl -n` calls (the level
    and the swap), with nothing from `vm_stat` or `ps`, and a failure of either
    is a ProbeError naming the probe."""
    calls = []
    answers = {
        ("sysctl", "-n", "kern.memorystatus_vm_pressure_level"): "2\n",
        ("sysctl", "-n", "vm.swapusage"): SWAPUSAGE,
    }
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(real_probes, "_run", fake_run(answers, calls))
    got = REAL_PROBE_PRESSURE()
    assert got == {
        "level": 2,
        "swap_used": round(5913.25 * MIB),
        "swap_total": 7168 * MIB,
        "method": "sysctl",
    }
    assert len(calls) == 2
    for key in answers:
        broken = dict(answers)
        broken[key] = real_probes.ProbeError("probe_pressure: sysctl exited 1")
        monkeypatch.setattr(real_probes, "_run", fake_run(broken))
        with pytest.raises(ProbeError, match="probe_pressure"):
            REAL_PROBE_PRESSURE()


@pytest.mark.parametrize(
    "full,level", [("0.00", 1), ("4.99", 1), ("5.00", 2), ("31.50", 2)]
)
def test_probe_pressure_on_linux_reads_psi_and_meminfo(monkeypatch, full, level):
    """templet:3161 -- on Linux, from files of the shape the kernel writes (a
    hand-written fixture, never a captured one): `/proc/pressure/memory`'s
    `full avg10` at 5.0 or more is level 2 (warn), else 1; the swap is
    `SwapTotal` less `SwapFree`."""
    files = {
        "/proc/pressure/memory": PSI.format(full=full),
        "/proc/meminfo": MEMINFO.format(total=4 * 1024 * 1024, free=1024 * 1024),
    }
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(real_probes, "_small", lambda probe, path: files[path])
    assert REAL_PROBE_PRESSURE() == {
        "level": level,
        "swap_used": 3 * GIB,
        "swap_total": 4 * GIB,
        "method": "psi",
    }


def test_probe_pressure_on_linux_without_psi_or_swap_reads_none(monkeypatch):
    """templet:3161 -- a Linux with no `/proc/pressure/memory` (an old kernel,
    or one without PSI) has no level, and a host with no swap has swap 0 of 0;
    neither is a failed read. A file that is there and unreadable is."""
    files = {"/proc/meminfo": MEMINFO.format(total=0, free=0)}

    def small(probe, path):
        if path not in files:
            raise real_probes.Absent(f"{probe}: {path} is not there")
        return files[path]

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(real_probes, "_small", small)
    got = REAL_PROBE_PRESSURE()
    assert (got["level"], got["swap_used"], got["swap_total"]) == (None, 0, 0)

    def unreadable(probe, path):
        raise ProbeError(f"{probe}: {path} is not readable (Permission denied)")

    monkeypatch.setattr(real_probes, "_small", unreadable)
    with pytest.raises(ProbeError, match="not readable"):
        REAL_PROBE_PRESSURE()


def test_probe_pressure_on_another_platform_reads_nothing(monkeypatch):
    """templet:3161 -- a platform with no reader gives no level and no swap, and
    runs nothing: it is not a failed read."""
    monkeypatch.setattr(sys, "platform", "plan9")
    monkeypatch.setattr(real_probes, "_run", fake_run({}))
    got = REAL_PROBE_PRESSURE()
    assert (got["level"], got["swap_used"], got["swap_total"]) == (None, None, None)


def test_the_real_read_on_this_host_gives_figures_and_changes_nothing(
    monkeypatch, capsys
):
    """templet:3161 -- the read for real, here, read-only: on macOS the level is
    1, 2 or 4 and the swap figures are bytes with used within total; on Linux
    the level is 1 or 2, or None where the kernel has no PSI. Nothing is
    allocated to provoke a reading."""
    if not sys.platform.startswith(("darwin", "linux")):
        pytest.skip(f"no real reader for {sys.platform}")
    got = REAL_PROBE_PRESSURE()
    with capsys.disabled():
        print(f"\nREAL READ {sys.platform}: {json.dumps(got)}")
    if sys.platform == "darwin":
        assert got["level"] in (1, 2, 4) and got["method"] == "sysctl"
    else:
        assert got["level"] in (1, 2, None)
    assert 0 <= got["swap_used"] <= got["swap_total"]


def test_a_file_that_is_not_there_is_absent_and_one_not_readable_is_an_error(
    tmp_path,
):
    """templet:3161 -- the small-file reader says `Absent` for a file the host
    does not have (what a kernel without the pressure file gives), and a plain
    `ProbeError`, not `Absent`, for a file that is there and cannot be read."""
    with pytest.raises(real_probes.Absent, match="is not there"):
        real_probes._small("probe_pressure", str(tmp_path / "nothing"))
    locked = tmp_path / "locked"
    locked.write_text("x")
    locked.chmod(0)
    try:
        with pytest.raises(ProbeError, match="not readable") as caught:
            real_probes._small("probe_pressure", str(locked))
        assert not isinstance(caught.value, real_probes.Absent)
    finally:
        locked.chmod(0o600)
