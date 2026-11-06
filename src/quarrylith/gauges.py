# SPDX-License-Identifier: Apache-2.0
"""The gauges: what the record says of its hosts and its work, computed from
the records through the contract's reads, and never stored.

A gauge is `Gauge(name, unit, labels, help, compute)`, and `compute(record, *,
now, window_ms)` gives its series, `[(labels, value)]`: `labels` a dict of the
gauge's labels, the series sorted by them. `GAUGES` is the one list a
dashboard and a node's report to its peers read, and `measure` computes every
gauge at one seq.

- A window is `[now - window_ms, now]`: a run counts in it by its start or its
  finish, as the gauge says, and a review by its filing.
- Durations are seconds. `p50` and `p90` (and a review's `median`) are by
  nearest rank, so each is a value the window holds.
- A series' `effort` is a task's own effort, else its item's efforts (its own
  links, else its parent chain's), one series each; `""` when it has none.
- A run is **alive** while it beats: its latest beat within two of its task's
  `supervision.beat_s`, or within 10 minutes when the task sets none. A live
  run **stalls** when it is alive and has made no phase change past its
  `expect_until` plus 5 minutes, or past 10 minutes with no expectation.
- A record's `json` field (a run's `footprint`, a task's `supervision`, a
  reading's `value`) may hold any JSON, an import's above all: a gauge reads
  one only as a dict, and a figure in it only as a number, so a `beat_s` that
  is not a positive number is none. What it cannot read it skips.
"""

import math
from dataclasses import dataclass

from .record import contract

MINUTE_MS = 60_000
#: How far past its `expect_until` an alive run stalls.
STALL_PAST_MS = 5 * MINUTE_MS
#: How long an alive run with no expectation may go without a phase change,
#: and how long a run whose task sets no `beat_s` may go without a beat.
QUIET_MS = 10 * MINUTE_MS
#: How many beats a run may miss and still be alive.
BEAT_MISSES = 2
HOUR_MS = 60 * MINUTE_MS


@dataclass(frozen=True)
class Gauge:
    name: str
    unit: str
    labels: tuple
    help: str
    compute: object


def _in(t, now, window_ms):
    return t is not None and now - window_ms <= t <= now


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _dict(value):
    """`value` when it is a dict, else `{}`: a record's `json` field may hold
    any JSON, and a gauge reads only the dicts."""
    return value if isinstance(value, dict) else {}


def _quantile(values, q):
    """The nearest-rank `q` quantile of `values`."""
    ordered = sorted(values)
    return ordered[max(math.ceil(q * len(ordered)) - 1, 0)]


def _series(names, values):
    """`{label values: value}` as series, sorted by their labels."""
    return [(dict(zip(names, key)), values[key]) for key in sorted(values)]


def _stats(names, groups, stats):
    """Each group's `stats` (`(name, function)`), as series labelled by the
    group and `stat`."""
    out = {}
    for key, values in groups.items():
        for stat, f in stats:
            out[key + (stat,)] = f(values)
    return _series(names + ("stat",), out)


def _item_efforts(record, number):
    item = record.get("items", number)
    return (contract.efforts_of(record, item) if item else []) or [""]


def _task_efforts(record, task):
    if task["effort"] is not None:
        return [task["effort"]]
    if task["number"] is None:
        return [""]
    return _item_efforts(record, task["number"])


def _live_capacity(record):
    retired = {n["name"] for n in record.read("nodes") if n["retired_at"] is not None}
    return [c for c in record.read("capacity") if c["node"] not in retired]


def _free(record, cap, now):
    return record.free(cap["node"], cap["resource"], from_at=now, until_at=None)


def capacity(record, *, now, window_ms):
    caps = _live_capacity(record)
    return _series(
        ("node", "resource"), {(c["node"], c["resource"]): c["amount"] for c in caps}
    )


def reserved(record, *, now, window_ms):
    caps = _live_capacity(record)
    return _series(
        ("node", "resource"),
        {(c["node"], c["resource"]): c["amount"] - _free(record, c, now) for c in caps},
    )


def free(record, *, now, window_ms):
    caps = _live_capacity(record)
    return _series(
        ("node", "resource"),
        {(c["node"], c["resource"]): _free(record, c, now) for c in caps},
    )


#: The window of readings from which the search for a node's latest reading
#: stops doubling and reads them all once.
FULL_READ_AT = 1024


def measured(record, *, now, window_ms):
    """Per node, resource and holder, the latest `host/measure` reading of
    the node: the host's available memory and free disk (holder `""`), and
    each consumer's figure of a resource (its resident use). A reading that
    names no live node, and a figure that is not a number, are skipped; the
    reading's age is the allocator's to judge, not the gauge's."""
    live = {n["name"] for n in record.read("nodes") if n["retired_at"] is None}
    found = {}
    limit = 2 * len(live)
    while len(found) < len(live):
        # the newest readings first, a window that doubles until every live
        # node is found or the window holds all the readings there are; a
        # node never measured is ordinary, so from FULL_READ_AT one read
        # without a limit ends the search
        rows = record.read(
            "readings", source="host", kind="measure", desc=True, limit=limit
        )
        found = {}
        for reading in rows:
            value = _dict(reading["value"])
            node = value.get("node")
            if isinstance(node, str) and node in live and node not in found:
                found[node] = value
        if limit is None or len(rows) < limit:
            break
        limit = None if limit >= FULL_READ_AT else limit * 2
    out = {}
    for node, value in found.items():
        for resource, key in (("memory", "memory_available"), ("disk", "disk_free")):
            if _number(value.get(key)):
                out[(node, resource, "")] = value[key]
        for holder, figures in _dict(value.get("consumers")).items():
            for resource, amount in _dict(figures).items():
                if _number(amount):
                    out[(node, resource, holder)] = amount
    return _series(("node", "resource", "holder"), out)


def queue_depth(record, *, now, window_ms):
    counts = {}

    def add(of, state, efforts, labels):
        for effort in efforts:
            for label in ["*"] + list(labels):
                key = (of, state, effort, label)
                counts[key] = counts.get(key, 0) + 1

    for item in record.read("items", live=True):
        add(
            "items",
            item["state"],
            _item_efforts(record, item["number"]),
            item["labels"],
        )
    for task in record.read("tasks", live=True):
        add(
            "tasks",
            task["state"],
            _task_efforts(record, task),
            record.effective_labels(task["id"]),
        )
    return _series(("of", "state", "effort", "label"), counts)


def runs_active(record, *, now, window_ms):
    counts = {}
    for run in record.read("runs", live=True):
        key = (run["phase"], run["node"])
        counts[key] = counts.get(key, 0) + 1
    return _series(("phase", "node"), counts)


def run_age(record, *, now, window_ms):
    return _series(
        ("run",),
        {
            (str(run["id"]),): (now - run["started_at"]) / 1000
            for run in record.read("runs", live=True)
        },
    )


def _runs_by(record, keep, measure):
    """The values `measure(run, task)` of the runs `keep(run)` takes, grouped
    by (task kind, effort)."""
    groups = {}
    for run in record.read("runs"):
        if not keep(run):
            continue
        task = record.get("tasks", run["task"])
        for effort in _task_efforts(record, task):
            groups.setdefault((task["kind"], effort), []).append(measure(run, task))
    return groups


QUANTILES = (("p50", lambda v: _quantile(v, 0.5)), ("p90", lambda v: _quantile(v, 0.9)))


def filed_to_start(record, *, now, window_ms):
    def filed(task):
        if task["number"] is None:
            return task["created_at"]
        return record.get("items", task["number"])["created_at"]

    groups = _runs_by(
        record,
        lambda run: run["attempt"] == 1 and _in(run["started_at"], now, window_ms),
        lambda run, task: (run["started_at"] - filed(task)) / 1000,
    )
    return _stats(("kind", "effort"), groups, QUANTILES)


def start_to_done(record, *, now, window_ms):
    groups = _runs_by(
        record,
        lambda run: _in(run["finished_at"], now, window_ms),
        lambda run, task: (run["finished_at"] - run["started_at"]) / 1000,
    )
    return _stats(("kind", "effort"), groups, QUANTILES)


def outcomes(record, *, now, window_ms):
    counts = {}
    for run in record.read("runs"):
        if _in(run["finished_at"], now, window_ms):
            kind = record.get("tasks", run["task"])["kind"]
            key = (kind, run["outcome"] or "")
            counts[key] = counts.get(key, 0) + 1
    return _series(("kind", "outcome"), counts)


def unmerged_changes(record, *, now, window_ms):
    counts = {}
    for change in record.read("changes", live=True):
        counts[(change["repo"],)] = counts.get((change["repo"],), 0) + 1
    return _series(("repo",), counts)


def review_rounds(record, *, now, window_ms):
    rounds, recent = {}, set()
    for review in record.read("reviews"):
        rounds.setdefault(review["target"], set()).add(review["round"])
        if _in(review["filed_at"], now, window_ms):
            recent.add(review["target"])
    groups = {}
    for target in sorted(recent):
        for effort in _item_efforts(record, target):
            groups.setdefault((effort,), []).append(len(rounds[target]))
    return _stats(
        ("effort",), groups, (("median", lambda v: _quantile(v, 0.5)), ("worst", max))
    )


def budget_pace(record, *, now, window_ms):
    """Per provider and window (the reading's kind), the use's rise per hour,
    in percent, across the readings of the latest budget period (its
    `resets_at`) in the window."""
    by = {}
    for reading in record.read("readings", source="budget"):
        value = _dict(reading["value"])
        used = value.get("utilization")
        if not _in(reading["at"], now, window_ms) or not _number(used):
            continue
        provider = value.get("provider")
        key = (provider if isinstance(provider, str) else "", reading["kind"])
        by.setdefault(key, []).append((reading["at"], value.get("resets_at"), used))
    paces = {}
    for key, rows in by.items():
        rows.sort(key=lambda row: row[0])
        period = [row for row in rows if row[1] == rows[-1][1]]
        (first, _, low), (last, _, high) = period[0], period[-1]
        if last > first:
            paces[key] = (high - low) * 100 / ((last - first) / HOUR_MS)
    return _series(("provider", "window"), paces)


def footprint(record, *, now, window_ms):
    """Per task kind and resource, the largest footprint a run that finished
    in the window reported."""
    peaks = {}
    for run in record.read("runs"):
        if not _in(run["finished_at"], now, window_ms):
            continue
        kind = record.get("tasks", run["task"])["kind"]
        for resource, amount in _dict(run["footprint"]).items():
            if _number(amount):
                key = (kind, resource)
                peaks[key] = max(peaks.get(key, amount), amount)
    return _series(("kind", "resource"), peaks)


def _stalled(record, run, now):
    beat = record.get("beats", run["id"])
    if beat is None:
        return False
    task = record.get("tasks", run["task"])
    beat_s = _dict(task["supervision"] if task else None).get("beat_s")
    if _number(beat_s) and beat_s > 0:
        alive_ms = BEAT_MISSES * beat_s * 1000
    else:
        alive_ms = QUIET_MS
    if now - beat["at"] > alive_ms:
        return False
    if run["expect_until"] is not None:
        return now > run["expect_until"] + STALL_PAST_MS
    return now > run["phase_at"] + QUIET_MS


def stalls(record, *, now, window_ms):
    counts = {}
    for run in record.read("runs", live=True):
        key = (run["node"],)
        counts[key] = counts.get(key, 0) + _stalled(record, run, now)
    return _series(("node",), counts)


GAUGES = (
    Gauge(
        "capacity",
        "the resource's",
        ("node", "resource"),
        "a live node's declared capacity of a resource",
        capacity,
    ),
    Gauge(
        "reserved",
        "the resource's",
        ("node", "resource"),
        "the most its live reservations hold at any instant from now",
        reserved,
    ),
    Gauge(
        "free",
        "the resource's",
        ("node", "resource"),
        "capacity less what is reserved, from now on",
        free,
    ),
    Gauge(
        "measured",
        "the resource's",
        ("node", "resource", "holder"),
        "a node's latest measured figure: the host's available (holder empty), "
        "or a holder's use",
        measured,
    ),
    Gauge(
        "queue_depth",
        "items, tasks",
        ("of", "state", "effort", "label"),
        "open items and tasks by state and effort; label * counts all of them",
        queue_depth,
    ),
    Gauge("runs_active", "runs", ("phase", "node"), "live runs", runs_active),
    Gauge("run_age", "s", ("run",), "how long each live run has run", run_age),
    Gauge(
        "filed_to_start",
        "s",
        ("kind", "effort", "stat"),
        "from filing to a first start, for runs started in the window",
        filed_to_start,
    ),
    Gauge(
        "start_to_done",
        "s",
        ("kind", "effort", "stat"),
        "from start to finish, for runs finished in the window",
        start_to_done,
    ),
    Gauge(
        "outcomes",
        "runs",
        ("kind", "outcome"),
        "runs finished in the window, by task kind and outcome",
        outcomes,
    ),
    Gauge(
        "unmerged_changes",
        "changes",
        ("repo",),
        "open changes, neither merged nor dropped",
        unmerged_changes,
    ),
    Gauge(
        "review_rounds",
        "rounds",
        ("effort", "stat"),
        "rounds of review per item reviewed in the window",
        review_rounds,
    ),
    Gauge(
        "budget_pace",
        "%/h",
        ("provider", "window"),
        "how fast a budget is used, over the window's latest period",
        budget_pace,
    ),
    Gauge(
        "footprint",
        "the resource's",
        ("kind", "resource"),
        "the largest footprint of a run finished in the window",
        footprint,
    ),
    Gauge(
        "stalls",
        "runs",
        ("node",),
        "alive runs with no phase change past their margin",
        stalls,
    ),
)


def by_name(name):
    for gauge in GAUGES:
        if gauge.name == name:
            return gauge
    raise KeyError(name)


def measure(record, *, now, window_ms):
    """Every gauge's series, `{name: [(labels, value)]}` in `GAUGES` order, at
    one seq."""
    with record.reading():
        return {g.name: g.compute(record, now=now, window_ms=window_ms) for g in GAUGES}
