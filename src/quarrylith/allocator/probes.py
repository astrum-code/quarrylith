# SPDX-License-Identifier: Apache-2.0
"""The host probes: how much memory is available, how much disk is free, how
large a file or folder is on disk and how much memory a process tree holds.
They only read (`vm_stat`, `sysctl`, `/proc/meminfo`, `ps` and `statvfs`),
give figures and nothing else, and keep no process's command line. A probe
that cannot read raises `ProbeError`, naming itself."""

import errno
import math
import os
import re
import subprocess
import sys
from collections import namedtuple

#: The one `ps` call: pid, parent, resident size in KiB and the executable's
#: name. No column carries arguments.
PS = ["ps", "-A", "-o", "pid=,ppid=,rss=,comm="]
#: Where macOS keeps `ps`, run by its path so a PATH without `/bin` is no failure.
PS_DARWIN = "/bin/ps"
#: What a probe reads at most: the bytes of `ps`'s output and the rows of its
#: table, the files of a folder it sizes, and the bytes of a small file.
MAX_BYTES = 8 * 2**20
MAX_ROWS = 200_000
MAX_FILES = 100_000
MAX_SMALL = 65_536
#: How long a command may take, in seconds.
TIMEOUT = 10

Process = namedtuple("Process", "pid ppid rss comm")


class ProbeError(Exception):
    """A probe could not read what it measures; the message names the probe."""


def _run(probe, argv, timeout=TIMEOUT):
    """The output of `argv`, bounded and timed, or a `ProbeError` naming
    `probe`. It runs in the `C` locale, so that a figure is the kernel's and not
    a locale's (a comma decimal reads as a smaller number, not as an error)."""
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            env={**os.environ, "LC_ALL": "C"},
            timeout=timeout,
            text=True,
            errors="replace",
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ProbeError(f"{probe}: {argv[0]} did not run ({error})") from None
    if done.returncode != 0:
        raise ProbeError(f"{probe}: {argv[0]} exited {done.returncode}")
    if len(done.stdout) > MAX_BYTES:
        raise ProbeError(f"{probe}: {argv[0]} gave more than {MAX_BYTES} bytes")
    return done.stdout


class Absent(ProbeError):
    """A reading that this host does not have (a file its kernel does not
    write), as against one it has and could not read."""


#: Errors that say a file is not on this host: not there, or not supported.
NOT_THERE = (errno.ENOENT, errno.ENOTSUP, errno.EOPNOTSUPP)


def _small(probe, path):
    try:
        with open(path, "rb") as handle:
            return handle.read(MAX_SMALL).decode("utf-8", "replace")
    except OSError as error:
        if error.errno in NOT_THERE:
            raise Absent(f"{probe}: {path} is not there") from None
        raise ProbeError(
            f"{probe}: {path} is not readable ({error.strerror})"
        ) from None


# -- memory -------------------------------------------------------------------------

PAGE_SIZE = re.compile(r"page size of (\d+) bytes")
#: The pages `vm_stat` counts that the system gives back on demand.
AVAILABLE_PAGES = (
    "Pages free",
    "Pages inactive",
    "Pages purgeable",
    "Pages speculative",
)


def parse_vm_stat(text):
    """Available bytes in `vm_stat`'s output: the free, inactive, purgeable
    and speculative pages at the page size it names."""
    size = PAGE_SIZE.search(text)
    if size is None:
        raise ProbeError("vm_stat: the page size is not in its output")
    pages = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        if name in AVAILABLE_PAGES and rest.strip().rstrip(".").isdigit():
            pages[name] = int(rest.strip().rstrip("."))
    if set(pages) != set(AVAILABLE_PAGES):
        raise ProbeError("vm_stat: a count of free, inactive, purgeable or speculative")
    return sum(pages.values()) * int(size[1])


def parse_meminfo(text):
    """`(total, available)` bytes in `/proc/meminfo`'s text (kB)."""
    found = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if name in ("MemTotal", "MemAvailable") and parts and parts[0].isdigit():
            found[name] = int(parts[0]) * 1024
    if set(found) != {"MemTotal", "MemAvailable"}:
        raise ProbeError("meminfo: MemTotal or MemAvailable is not in the file")
    return found["MemTotal"], found["MemAvailable"]


def probe_memory():
    """`{"memory_total", "memory_available", "method"}` of this host. On macOS
    available is `vm_stat`'s free, inactive, purgeable and speculative pages
    (the memory the system gives back); on Linux `MemAvailable`."""
    if sys.platform == "darwin":
        total = _run("probe_memory", [SYSCTL, "-n", "hw.memsize"]).strip()
        if not total.isdigit():
            raise ProbeError("probe_memory: sysctl hw.memsize is not a number")
        available = parse_vm_stat(_run("probe_memory", [VM_STAT]))
        return {
            "memory_total": int(total),
            "memory_available": available,
            "method": "vm_stat",
        }
    if sys.platform.startswith("linux"):
        total, available = parse_meminfo(_small("probe_memory", "/proc/meminfo"))
        return {
            "memory_total": total,
            "memory_available": available,
            "method": "meminfo",
        }
    raise ProbeError(f"probe_memory: no reader for {sys.platform}")


# -- memory pressure ----------------------------------------------------------------

#: The kernel's numbers for `kern.memorystatus_vm_pressure_level`.
LEVELS = {1: "normal", 2: "warn", 4: "critical"}
#: On Linux, `/proc/pressure/memory`'s `full avg10` (the share of the last ten
#: seconds in which every task stalled on memory) at or above which the level is
#: warn: 5%.
PSI_FULL_WARN = 5.0
#: `sysctl` by its path (`/usr/sbin` is not on every PATH), and how long each of
#: the two pressure reads may take: a few seconds, so one stuck `sysctl` cannot
#: cost a placement long.
SYSCTL = "/usr/sbin/sysctl"
VM_STAT = "/usr/bin/vm_stat"
PRESSURE_TIMEOUT_S = 3
#: The share of memory that is free, in whole percent, on macOS.
MEMORY_LEVEL = "kern.memorystatus_level"
UNITS = {"K": 2**10, "M": 2**20, "G": 2**30, "T": 2**40}
#: The whole of `vm.swapusage`, as the kernel writes it (`total = 7168.00M  used
#: = 5913.25M  free = 1254.75M  (encrypted)`): a figure, a point and a capital
#: unit each, and nothing else. A figure off this is a failed read, never bytes.
_FIGURE = r"([0-9]+(?:\.[0-9]+)?)([KMGT])"
SWAPUSAGE = re.compile(
    rf"\s*total = {_FIGURE}\s+used = {_FIGURE}\s+free = {_FIGURE}"
    r"(?:\s+\([a-z ]+\))?\s*"
)
PSI_FULL = re.compile(r"^full\b.*?\bavg10=([0-9]+(?:\.[0-9]+)?)", re.MULTILINE)


def parse_pressure_level(text):
    """The kernel's pressure level in `sysctl -n`'s output: 1, 2 or 4."""
    found = text.strip()
    if re.fullmatch(r"[0-9]+", found) is None or int(found) < 1:
        raise ProbeError("probe_pressure: the pressure level is not a number")
    return int(found)


def parse_memory_level(text):
    """The free share of memory in `kern.memorystatus_level`'s output: a whole
    number of percent, 0 to 100. Anything else is a `ProbeError`."""
    found = text.strip()
    if re.fullmatch(r"[0-9]{1,3}", found) is None or int(found) > 100:
        raise ProbeError("probe_pressure: the memory level is not a percent")
    return int(found)


def parse_swapusage(text):
    """`(total, used)` bytes in `vm.swapusage`'s text. The whole text must be
    the kernel's format (a figure with no unit, a lowercase unit, a comma or
    anything else is a `ProbeError`)."""
    found = SWAPUSAGE.fullmatch(text)
    if found is None:
        raise ProbeError("probe_pressure: vm.swapusage is not in the kernel's format")
    total, used, _ = (round(float(found[n]) * UNITS[found[n + 1]]) for n in (1, 3, 5))
    return total, used


def parse_psi(text):
    """The `full avg10` figure of `/proc/pressure/memory`."""
    found = PSI_FULL.search(text)
    if found is None:
        raise ProbeError("probe_pressure: no full avg10 in the memory pressure file")
    return float(found[1])


def parse_swap_meminfo(text):
    """`(total, used)` bytes of swap in `/proc/meminfo` (kB)."""
    found = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if name in ("SwapTotal", "SwapFree") and parts and parts[0].isdigit():
            found[name] = int(parts[0]) * 1024
    if set(found) != {"SwapTotal", "SwapFree"}:
        raise ProbeError("probe_pressure: SwapTotal or SwapFree is not in meminfo")
    return found["SwapTotal"], max(found["SwapTotal"] - found["SwapFree"], 0)


def probe_pressure():
    """`{"level", "swap_used", "swap_total", "memory_free_percent", "method"}` of
    this host: the memory pressure level (the kernel's 1 normal, 2 warn, 4
    critical), the swap in bytes and the share of memory that is free, in whole
    percent (macOS: `kern.memorystatus_level`; Linux: `MemAvailable` over
    `MemTotal`, rounded down). A figure the host does not have is None, and is
    not a failed read; one it has and cannot read raises `ProbeError`. On macOS
    each is one `sysctl -n`; on Linux the level is 2 when
    `/proc/pressure/memory`'s `full avg10` is `PSI_FULL_WARN` or more, else 1,
    and the swap and the free share come from `/proc/meminfo`."""
    if sys.platform == "darwin":
        level = parse_pressure_level(
            _run(
                "probe_pressure",
                [SYSCTL, "-n", "kern.memorystatus_vm_pressure_level"],
                PRESSURE_TIMEOUT_S,
            )
        )
        total, used = parse_swapusage(
            _run(
                "probe_pressure",
                [SYSCTL, "-n", "vm.swapusage"],
                PRESSURE_TIMEOUT_S,
            )
        )
        free = parse_memory_level(
            _run(
                "probe_pressure",
                [SYSCTL, "-n", MEMORY_LEVEL],
                PRESSURE_TIMEOUT_S,
            )
        )
        return {
            "level": level,
            "swap_used": used,
            "swap_total": total,
            "memory_free_percent": free,
            "method": "sysctl",
        }
    if sys.platform.startswith("linux"):
        try:
            full = parse_psi(_small("probe_pressure", "/proc/pressure/memory"))
            level = 2 if full >= PSI_FULL_WARN else 1
        except Absent:
            level = None
        meminfo = _small("probe_pressure", "/proc/meminfo")
        total, used = parse_swap_meminfo(meminfo)
        memory_total, available = parse_meminfo(meminfo)
        if memory_total < 1:
            raise ProbeError("probe_pressure: MemTotal in meminfo is 0")
        return {
            "level": level,
            "swap_used": used,
            "swap_total": total,
            "memory_free_percent": min(available * 100 // memory_total, 100),
            "method": "psi" if level is not None else "meminfo",
        }
    return {
        "level": None,
        "swap_used": None,
        "swap_total": None,
        "memory_free_percent": None,
        "method": "none",
    }


# -- disk ---------------------------------------------------------------------------


def probe_disk(path):
    """The free bytes of the filesystem `path` is on."""
    try:
        found = os.statvfs(path)
    except OSError as error:
        raise ProbeError(f"probe_disk: {path} ({error.strerror})") from None
    return found.f_bavail * found.f_frsize


def probe_size(path, most=MAX_FILES):
    """The bytes `path` occupies on disk: the allocated blocks, so a sparse
    file counts what it holds, and for a folder those of every file under it
    (at most `most` of them). 0 for a path that is not there."""
    try:
        first = os.lstat(path)
    except FileNotFoundError:
        return 0
    except OSError as error:
        raise ProbeError(f"probe_size: {path} ({error.strerror})") from None
    if not os.path.isdir(path) or os.path.islink(path):
        return first.st_blocks * 512
    total, seen = first.st_blocks * 512, 0
    for here, folders, names in os.walk(path):
        for name in names:
            seen += 1
            if seen > most:
                raise ProbeError(f"probe_size: {path} holds more than {most} files")
            try:
                total += os.lstat(os.path.join(here, name)).st_blocks * 512
            except OSError:
                continue
    return total


# -- processes ----------------------------------------------------------------------


def parse_ps(text):
    """The `Process` rows of `ps -A -o pid=,ppid=,rss=,comm=`'s output, the
    resident size in bytes."""
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split(None, 3)
        if len(parts) < 3 or not all(p.isdigit() for p in parts[:3]):
            raise ProbeError("ps: a row that is not pid, ppid and rss")
        rows.append(
            Process(
                int(parts[0]),
                int(parts[1]),
                int(parts[2]) * 1024,
                parts[3] if len(parts) > 3 else "",
            )
        )
        if len(rows) > MAX_ROWS:
            raise ProbeError(f"ps: more than {MAX_ROWS} processes")
    return rows


def process_table():
    """The processes of this host, from one `ps`."""
    argv = list(PS)
    if sys.platform == "darwin" and argv[0] == "ps":
        argv[0] = PS_DARWIN
    return parse_ps(_run("ps", argv))


def probe_tree(pid, table=None):
    """The resident bytes of process `pid` and all its descendants (0 when it
    is not running)."""
    table = process_table() if table is None else table
    children, own = {}, {}
    for row in table:
        children.setdefault(row.ppid, []).append(row.pid)
        own[row.pid] = own.get(row.pid, 0) + row.rss
    total, stack, seen = 0, [pid], set()
    while stack:
        here = stack.pop()
        if here in seen:
            continue
        seen.add(here)
        total += own.get(here, 0)
        stack.extend(children.get(here, []))
    return total


def probe_process(match, table=None):
    """The resident bytes of every process whose executable's name contains
    `match`."""
    table = process_table() if table is None else table
    return sum(row.rss for row in table if match in row.comm)


def _pid_of(path):
    """`(pid, None)` read from the pid file `path`, or `(None, why)`: a service
    that is down has no file, or one that holds no number."""
    try:
        with open(path, "rb") as handle:
            text = handle.read(MAX_SMALL).decode("utf-8", "replace").strip()
    except FileNotFoundError:
        return None, f"pid file {path} is missing"
    except OSError:
        return None, f"pid file {path} is not readable"
    if not text.isdigit():
        return None, f"pid file {path} does not hold a process number"
    return int(text), None


def resident(probe, table):
    """`(bytes, None)`: the resident bytes a consumer's `probe` (`{pidfile:
    PATH}`, `{pid: N}` or `{process: TEXT}`) finds in `table`; or `(None,
    why)` when the consumer is not there: no pid file, no number in it, no
    such process. A consumer that is down is no failed probe."""
    kind, value = next(iter(probe.items()))
    if kind == "process":
        found = [row.rss for row in table if value in row.comm]
        if not found:
            return None, f"no process named {value}"
        return sum(found), None
    if kind == "pidfile":
        pid, why = _pid_of(value)
        if pid is None:
            return None, why
    else:
        pid = value
    if not any(row.pid == pid for row in table):
        return None, f"no process {pid}" + (
            f" (pid file {value})" if kind == "pidfile" else ""
        )
    return probe_tree(pid, table), None


def declared_size(peak, block=256 * 2**20):
    """`peak` bytes times 1.25, rounded up to a whole `block`."""
    return math.ceil(peak * 1.25 / block) * block
