# SPDX-License-Identifier: Apache-2.0
"""The placement seam: `Placement`, the base class a placement fills, the
answers it gives (`Fit`, `Refusal` and its `Short`s) and the entry point group
a placement registers in. A placement decides whether and where one task fits,
and writes nothing."""

import abc
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import metadata

from .document import unit_of

#: The entry point group: name -> a `Placement` class.
PLACEMENT_GROUP = "quarrylith.placement"

#: Why a task does not fit, in the order a refusal lists them.
CAUSES = (
    "room",
    "measured",
    "unmeasured",
    "pressure",
    "budget",
    "undeclared",
    "unclassified",
    "driver",
    "node",
)

GIB = 2**30
MIB = 2**20


@dataclass(frozen=True)
class Fit:
    """The task fits `node`, in `run_class`, and `start_run` must reserve
    `needs`."""

    task: int
    node: str
    run_class: str  # the class that matched
    needs: dict  # {resource: amount}
    sandbox: str
    why: str  # one line


@dataclass(frozen=True)
class Short:
    """One reason a task does not fit."""

    cause: str  # one of CAUSES
    node: str | None
    resource: str | None  # None for driver, node, unmeasured: every resource
    asked: int | None
    free: int | None  # what the table, the measurement or the allowance had
    capacity: int | None
    waits: bool  # can waiting alone ever make it fit?
    until: int | None  # a time after which it may fit, else None
    holders: list  # the largest live holders of the resource: [(holder, amount)]
    detail: str = ""  # one line for a cause the figures do not say: why, or the floor

    def __post_init__(self):
        if self.cause not in CAUSES:
            raise ValueError(f"cause {self.cause!r} is not one of {CAUSES}")

    def as_data(self):
        """Plain data, for a note or a log."""
        data = {name: getattr(self, name) for name in self.__dataclass_fields__}
        data["holders"] = [list(holder) for holder in self.holders]
        return data


@dataclass(frozen=True)
class Refusal:
    """The task does not fit: every short, whether waiting can help, until
    when, and one line for a person."""

    task: int | None  # None for a hold with no task
    run_class: str | None
    needs: dict
    shorts: tuple  # every Short, in CAUSES order, then by resource
    waits: bool  # all(s.waits for s in shorts)
    until: int | None  # the latest `until` of the shorts, when every one has one
    text: str  # one line

    def as_data(self):
        """Plain data, for a note or a log."""
        data = {name: getattr(self, name) for name in self.__dataclass_fields__}
        data["needs"] = dict(self.needs)
        data["shorts"] = [short.as_data() for short in self.shorts]
        return data


def refuse(task, run_class, needs, shorts, *, node):
    """The `Refusal` of `task` (None for a hold) in `run_class` for `shorts`
    on `node`: the shorts in CAUSES order and then by resource, `waits` and
    `until` read from them, and the line."""
    if not shorts:
        raise ValueError("a refusal has a reason: at least one short")
    shorts = tuple(
        sorted(shorts, key=lambda s: (CAUSES.index(s.cause), s.resource or ""))
    )
    waits = all(short.waits for short in shorts)
    untils = [short.until for short in shorts]
    until = max(untils) if all(at is not None for at in untils) else None
    return Refusal(
        task,
        run_class,
        dict(needs),
        shorts,
        waits,
        until,
        _line(task, run_class, shorts, waits, until, node),
    )


def _size(resource, amount, unit=True):
    """`amount` of `resource` for a person: bytes in GiB (MiB below one)."""
    if amount is None:
        return "?"
    if unit_of(resource or "") != "bytes":
        return str(amount)
    if amount >= GIB or not unit:
        return f"{amount / GIB:.1f}" + (" GiB" if unit else "")
    return f"{amount / MIB:.1f} MiB"


def _part(short):
    """One short as words."""
    what, node = short.resource, short.node
    if short.cause == "measured":
        part = (
            f"{what} {_size(what, short.asked)} asked, "
            f"{_size(what, short.free)} available"
        )
        return f"{part}, {short.detail} [measured]" if short.detail else part
    if short.cause in ("room", "budget"):
        part = (
            f"{what} {_size(what, short.asked)} asked, {_size(what, short.free)} free"
        )
        if short.capacity is not None:
            part += f" of {_size(what, short.capacity)}"
        if short.holders:
            held = ", ".join(f"{h} {_size(what, n, False)}" for h, n in short.holders)
            part += f" ({held})"
        if short.cause == "room":
            return part
        part = f"{part} [{short.cause}]"
        return f"{part} -- {short.detail}" if short.detail else part
    if short.cause == "pressure":
        return f"pressure: {short.detail}"
    if short.cause == "unmeasured":
        return f"unmeasured: {short.detail or 'the host reading is missing or stale'}"
    if short.cause == "undeclared":
        return f"undeclared: {what} has no capacity on {node}"
    if short.cause == "unclassified":
        if what is None:
            return "unclassified: no class matches"
        return f"unclassified: no usable {what} label"
    if short.cause == "driver":
        return f"driver: {node} has no driver for the task's sandbox"
    if node is None:
        return "node: no allocation document is declared"
    return f"node: {node} is not declared"


def _when(ms):
    """A time as `YYYY-MM-DDTHH:MMZ`; a number no date holds is printed as it
    is, so a line is built for any time a short names."""
    try:
        when = datetime.fromtimestamp(ms / 1000, timezone.utc)
        return when.strftime("%Y-%m-%dT%H:%MZ")
    except (ValueError, OverflowError, OSError):
        return str(ms)


def _line(task, run_class, shorts, waits, until, node):
    who = "hold" if task is None else f"task {task}"
    if task is not None and run_class:
        who += f" ({run_class})"
    if not waits:
        ending = "never"
    elif until is not None:
        ending = f"waits until {_when(until)}"
    else:
        ending = "waits"
    reasons = "; ".join(_part(short) for short in shorts)
    where = "" if node is None else f" on {node}"
    return f"allocator: {who} refused{where}: {reasons}; {ending}"


class Placement(abc.ABC):
    """Whether and where one task fits. A placement reads the record and
    writes nothing but the standing reservations `hold` and `release` make."""

    name: str  # the name it registers under: "host"

    def __init__(self, record):
        self.record = record  # a Record, or a reader

    @abc.abstractmethod
    def place(self, task, *, now):
        """A `Fit` or a `Refusal` for one task, now. Writes nothing."""

    @abc.abstractmethod
    def headroom(self, *, now):
        """{node: {resource: amount}}: what a start may still take on each
        live node, the least of the table's free, the budget allowance and
        the measured room. Writes nothing."""

    @abc.abstractmethod
    def hold(self, holder, needs, *, task=None, node=None, measured=True, now):
        """A standing reservation (task None) or a task's own, checked as
        `place` checks, and against the measured room unless `measured` is
        False. Returns the reservation ids, or the `Refusal`. A second live
        hold of one holder and resource (a standing one, or the task's own for
        the same task) is the caller's error, not a refusal for want of room:
        it raises the record's `Refused`, since a change is a replace."""

    @abc.abstractmethod
    def release(self, holder, *, node=None, why):
        """Release every live reservation of `holder` on the node that no
        live run holds. Returns their ids."""


def placement_for(name):
    """The `Placement` class registered under `name` in `PLACEMENT_GROUP`;
    `LookupError` naming the group and the names it has."""
    found = {}
    for point in metadata.entry_points(group=PLACEMENT_GROUP):
        found.setdefault(point.name, point)
    if name not in found:
        names = ", ".join(sorted(found)) or "none"
        raise LookupError(
            f"no placement {name!r} in the {PLACEMENT_GROUP} group; registered: {names}"
        )
    return found[name].load()
