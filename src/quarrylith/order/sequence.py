# SPDX-License-Identifier: Apache-2.0
"""The sequence: what may start next, and why.

`order(record, now=, settings=)` takes the record's ready read, narrows it by
schedules and flags, and sorts it into one list of candidates, each naming its
effort, the rule that placed it and a line saying why. It never widens the
ready read, writes nothing, reads no clock and reads the record only through
its contract, inside one `reading()`.

The tiers, in order: the floor (reviews, as many as `review_slots` less the
admitted efforts' reviews running now), the resumes (work with a live lease),
then each admitted effort's own order. Inside a tier, candidates sort by their
effort (`rank`, then `id`), then by the effort's rule, then by key. An item
with an open or running task is a candidate as its task, never beside it.

`place` gives one task or item exactly the place the full order gives it, so a
start named by hand is never placed differently from a round.
"""

import datetime as dt
import re
from dataclasses import dataclass

from ..record import contract, schema
from . import schedule as schedules

#: Every rule a candidate can name: the tiers' own and an effort's. The
#: record takes the same list in a start's `placed`.
RULES = contract.PLACED_RULES

#: The rules an effort's `order` json can name.
EFFORT_RULES = ("unblocks-then-age", "listed")
DEFAULT_RULE = "unblocks-then-age"

_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
_FAR = float("inf")
#: How many unblocked items a `why` names before it counts the rest.
_NAMED = 5


@dataclass(frozen=True)
class Candidate:
    task: int | None  # a ready task, or None: an item with no open task
    item: int | None
    key: tuple  # ("task", id), else ("item", number): the walk's key
    effort: str  # the effort whose order placed it
    rule: str  # one of RULES
    tier: int  # 1 floor, 2 resume, 3 effort order
    why: str  # one line
    unblocks: tuple  # the item numbers, nearest first
    role: str | None
    repos: tuple  # its repo: labels, sorted
    adds: tuple  # the repos where a start would add an unmerged change
    exempt: str | None  # "review", "resync", "post-review": skips the landing limit


@dataclass(frozen=True)
class Left:
    task: int | None
    item: int | None
    why: str  # "task 12: outside its window (opens 22:00 Europe/Lisbon)"


@dataclass(frozen=True)
class Sequence:
    candidates: tuple  # in order
    left: tuple  # what the ready set held that the order left out, and why
    notes: tuple  # an effort's unreadable order, and the like
    counts: dict  # the live figures the walk starts from
    at: int  # now
    seq: int  # the record's seq the reads saw


def _whole(value):
    return isinstance(value, int) and not isinstance(value, bool)


def effort_order(effort):
    """An effort's `order` json read as `(rule, rank, items, note)`. A part
    that is not the shape (an unknown `rule`, `items` not a list of item
    numbers, a `rank` that is not a whole number) is read as the default and
    `note` says why, so a bad edit never stops the order and never hides."""
    given = effort.get("order") if isinstance(effort, dict) else None
    if not isinstance(given, dict):
        return DEFAULT_RULE, None, (), f"order is {given!r}, not an object"
    faults = []
    rule = given.get("rule", DEFAULT_RULE)
    if rule not in EFFORT_RULES:
        faults.append(f"unknown rule {rule!r}")
        rule = DEFAULT_RULE
    rank = given.get("rank")
    if "rank" in given and not _whole(rank):
        faults.append(f"rank {rank!r} is not a whole number")
        rank = None
    items = ()
    if rule == "listed":
        listed = given.get("items")
        if isinstance(listed, list) and all(_whole(n) for n in listed):
            items = tuple(listed)
        else:
            faults.append(
                f"rule listed needs items, a list of item numbers: {listed!r}"
            )
            rule = DEFAULT_RULE
    note = "; ".join(faults) + "; read as the default" if faults else None
    return rule, rank, items, note


def _stamp(at):
    return _EPOCH + dt.timedelta(milliseconds=at)


def _ordinal(n):
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{({1: 'st', 2: 'nd', 3: 'rd'}).get(n % 10, 'th')}"


def _opens(schedule, now):
    """When a window next opens, in the schedule's zone: the time alone when
    it is today, with the date when it is not."""
    try:
        at = schedule.local_iso(schedule.next_open(now))
    except schedules.ScheduleError:
        return "never opens"
    if at[:10] == schedule.local_iso(now)[:10]:
        return f"opens {at[11:16]} {schedule.tz}"
    return f"opens {at[:10]} {at[11:16]} {schedule.tz}"


class _Reading:
    """One sequence's reads: the record, `now`, the settings, and what a
    sequence reads more than once, kept for this call only."""

    def __init__(self, record, now, settings):
        self.record = record
        self.now = now
        self.settings = settings
        self._efforts = {}
        self._unblocks = {}

    # -- efforts ------------------------------------------------------------

    def effort(self, id):
        """An effort's record and its order read, or None when it is not
        there."""
        if id not in self._efforts:
            found = self.record.get("efforts", id)
            self._efforts[id] = None if found is None else (found, effort_order(found))
        return self._efforts[id]

    def admitted_efforts(self, task, item):
        """The admitted efforts a task or item is linked to: an item's own
        links, else its parent chain's; a task with no item has its own."""
        if item is not None:
            ids = contract.efforts_of(self.record, item)
        else:
            ids = [] if task["effort"] is None else [task["effort"]]
        found = [self.effort(e) for e in ids]
        return [
            e[0]["id"]
            for e in found
            if e is not None and e[0]["standing"] == "admitted"
        ]

    def first_effort(self, task, item):
        """The one effort that places it: of several, the first by rank, then
        id."""
        ids = self.admitted_efforts(task, item)
        if not ids:
            return None
        return min(ids, key=self.effort_key)

    def effort_key(self, id):
        _, (_, rank, _, _) = self.effort(id)
        return (rank is None, rank or 0, id)

    # -- what an item says --------------------------------------------------

    def unblocks(self, number):
        """The open items that wait on item `number` through live deps,
        followed through those items' own, each once, nearest first."""
        if number not in self._unblocks:
            seen, queue = [], [number]
            while queue:
                for dep in self.record.read("deps", live=True, blocker=queue.pop(0)):
                    m = dep["number"]
                    if m == number or m in seen:
                        continue
                    waiting = self.record.get("items", m)
                    if waiting is not None and waiting["closed_at"] is None:
                        seen.append(m)
                        queue.append(m)
            self._unblocks[number] = tuple(seen)
        return self._unblocks[number]

    def reviewed(self, number):
        """The item a review item reviews, from its latest review record."""
        found = self.record.read("reviews", item=number)
        if not found:
            return None
        return max(found, key=lambda r: (r["round"], r["id"]))["target"]

    def lease(self, number):
        found = self.record.read("leases", live=True, number=number)
        return found[0] if found else None

    def open_change_repos(self, number):
        return {
            c["repo"] for c in self.record.read("changes", live=True, number=number)
        }

    def labels(self, task, item):
        if task is not None:
            return self.record.effective_labels(task["id"])
        return [] if item is None else item["labels"]

    def role_and_repos(self, task, item):
        labels = self.labels(task, item)
        role = next((x.split(":", 1)[1] for x in labels if x.startswith("role:")), None)
        if role is None and item is not None:
            role = item["role"]
        repos = sorted(x.split(":", 1)[1] for x in labels if x.startswith("repo:"))
        return role, tuple(repos)

    @staticmethod
    def flags(task, item):
        return set(task["flags"] if task else ()) | set(item["flags"] if item else ())

    @staticmethod
    def is_review(task, item):
        return (task is not None and task["kind"] == "review") or (
            item is not None and item["kind"] == "review"
        )

    def exempt(self, task, item, flags, resumed):
        """Why the landing limit skips it, or None: a review, a `resync`, or a
        resume whose item's current round has a review that asked for
        changes."""
        if self.is_review(task, item):
            return "review"
        if "resync" in flags:
            return "resync"
        if resumed and item is not None and item["review_round"]:
            for review in self.record.read("reviews", target=item["number"]):
                if (
                    review["round"] == item["review_round"]
                    and review["verdict"] == "changes"
                ):
                    return "post-review"
        return None

    # -- the gate -----------------------------------------------------------

    def kept_out(self, task, flags):
        """Why a schedule or a flag keeps a task from starting now, or
        None."""
        if task is not None and task["schedule"]:
            try:
                schedule = schedules.parse(task["schedule"])
            except schedules.ScheduleError as e:
                return f"schedule: {e}"
            if not schedule.within(self.now):
                return f"outside its window ({_opens(schedule, self.now)})"
        if "night" in flags:
            if self.settings.night is None:
                return "a night task, and no night window is set"
            night = schedules.parse(self.settings.night)
            if not night.within(self.now):
                return f"outside the night window ({_opens(night, self.now)})"
        return None


def _label(task, item):
    if task is not None and item is not None:
        return f"#{item['number']} task {task['id']}"
    if item is not None:
        return f"#{item['number']}"
    return f"task {task['id']}"


def _plain(why, subject):
    """A reason from the record without the `subject` it begins with (the
    `Left` line names it already), and a `not before` as a time."""
    for lead, put in (
        (f"{subject} is ", ""),
        (f"{subject} has ", "has "),
        (f"{subject}'s ", "its "),
    ):
        if why.startswith(lead):
            why = put + why[len(lead) :]
            break
    return re.sub(
        r"not before (\d+)",
        lambda m: f"not before {_stamp(int(m[1])).strftime('%Y-%m-%d %H:%M')} UTC",
        why,
    )


def _left(task, item, why):
    prefix = f"task {task['id']}" if task is not None else f"item {item['number']}"
    return Left(
        None if task is None else task["id"],
        None if item is None else item["number"],
        f"{prefix}: {why}",
    )


def _sort_key(entry):
    """Inside a tier: the effort (rank, then id), then the effort's rule (a
    listed item by its place, most unblocks, oldest), then the item's
    number, then the task's id."""
    return (
        entry["effort_key"],
        entry["listed"] if entry["listed"] is not None else _FAR,
        -len(entry["unblocks"]),
        entry["age"],
        _FAR if entry["item"] is None else entry["item"]["number"],
        _FAR if entry["task"] is None else entry["task"]["id"],
    )


def _gather(reading, ready):
    """The ready set as (task, item) pairs: an item with an open task is
    represented by that task, one with none by itself. An item whose only
    tasks are running or not ready is left out, as is whatever the schedule
    and the flags keep out."""
    record = reading.record
    by_item = {"open": {}, "running": {}}
    for state, found in by_item.items():
        for task in record.read("tasks", state=state):
            if task["number"] is not None:
                found.setdefault(task["number"], []).append(task)
    pairs, left = [], []
    ready_ids = {t["id"] for t in ready["tasks"]}
    for task in ready["tasks"]:
        item = None if task["number"] is None else record.get("items", task["number"])
        pairs.append((task, item))
    for item in ready["items"]:
        tasks = by_item["open"].get(item["number"], [])
        running = by_item["running"].get(item["number"], [])
        if not tasks and not running:
            pairs.append((None, item))
        elif not any(t["id"] in ready_ids for t in tasks):
            if running:
                left.append(
                    _left(
                        running[0],
                        item,
                        f"running, so item {item['number']} waits",
                    )
                )
                continue
            task = tasks[0]
            why = contract._unready(record, task, reading.now)
            left.append(_left(task, item, _plain(why, f"task {task['id']}")))
    return pairs, left


def _entries(reading, pairs):
    """Each pair read for the order: its effort, its figures, or why it is
    left out."""
    entries, left = [], []
    for task, item in pairs:
        flags = reading.flags(task, item)
        why = reading.kept_out(task, flags)
        if why is None:
            effort = reading.first_effort(task, item)
            if effort is None:
                why = "no admitted effort"
        if why is not None:
            left.append(_left(task, item, why))
            continue
        _, (rule, _, listed, _) = reading.effort(effort)
        number = None if item is None else item["number"]
        review = reading.is_review(task, item)
        target = reading.reviewed(number) if review and number is not None else None
        counted = target if target is not None else number
        lease = None if number is None else reading.lease(number)
        role, repos = reading.role_and_repos(task, item)
        changed = set() if number is None else reading.open_change_repos(number)
        place = None
        if rule == "listed" and number in listed:
            place = listed.index(number)
        entries.append(
            {
                "task": task,
                "item": item,
                "effort": effort,
                "effort_key": reading.effort_key(effort),
                "listed": place,
                "listed_of": len(listed),
                "unblocks": () if counted is None else reading.unblocks(counted),
                "age": (item or task)["created_at"],
                "review": review,
                "target": target,
                "lease": lease,
                "role": role,
                "repos": repos,
                "adds": tuple(r for r in repos if r not in changed),
                "exempt": reading.exempt(task, item, flags, lease is not None),
            }
        )
    return entries, left


def _detail(entry, rule, slot, slots):
    if rule == "floor":
        said = f"review {slot} of {slots}"
        if entry["target"] is not None:
            said += f", unblocks a merge of #{entry['target']}"
        return said
    if rule == "resume":
        lease = entry["lease"]
        since = _stamp(lease["granted_at"]).strftime("%Y-%m-%d %H:%M")
        return f"held by {lease['holder']} since {since}"
    if rule == "listed":
        return f"{_ordinal(entry['listed'] + 1)} of {entry['listed_of']} listed"
    named = ", ".join(f"#{n}" for n in entry["unblocks"][:_NAMED])
    more = len(entry["unblocks"]) - _NAMED
    if more > 0:
        named += f", and {more} more"
    said = f"unblocks {len(entry['unblocks'])}" + (f" ({named})" if named else "")
    created = _stamp(entry["age"]).strftime("%Y-%m-%d")
    return f"{said}, filed {created}"


def _tiers(entries, floor, running, slots):
    """The candidates, in sequence: each entry placed in its tier."""
    entries.sort(key=_sort_key)
    floored = {
        id(e): n for n, e in enumerate([e for e in entries if e["review"]][:floor])
    }
    placed = []
    for entry in entries:
        if id(entry) in floored:
            tier, rule = 1, "floor"
            slot = running + floored[id(entry)] + 1
        elif entry["lease"] is not None:
            tier, rule, slot = 2, "resume", None
        else:
            tier = 3
            rule = "listed" if entry["listed"] is not None else DEFAULT_RULE
            slot = None
        placed.append((tier, rule, slot, entry))
    placed.sort(key=lambda p: p[0])
    out = []
    for tier, rule, slot, entry in placed:
        task, item = entry["task"], entry["item"]
        out.append(
            Candidate(
                task=None if task is None else task["id"],
                item=None if item is None else item["number"],
                key=("task", task["id"]) if task else ("item", item["number"]),
                effort=entry["effort"],
                rule=rule,
                tier=tier,
                why=(
                    f"{_label(task, item)}: effort {entry['effort']}, {rule} -- "
                    f"{_detail(entry, rule, slot, slots)}"
                ),
                unblocks=entry["unblocks"],
                role=entry["role"],
                repos=entry["repos"],
                adds=entry["adds"],
                exempt=entry["exempt"],
            )
        )
    return out


def _counts(reading):
    """The figures the walk starts from, read in the same reading: per role
    the live runs; per repo the open changes plus the live runs that will
    open one; the live review runs of admitted efforts, and the live review
    runs linked to none."""
    record = reading.record
    roles, repos, reviews, landing = {}, {}, 0, 0
    for change in record.read("changes", live=True):
        repos[change["repo"]] = repos.get(change["repo"], 0) + 1
    for run in record.read("runs", live=True):
        task = record.get("tasks", run["task"])
        number = task["number"]
        item = None if number is None else record.get("items", number)
        role, named = reading.role_and_repos(task, item)
        if role is not None:
            roles[role] = roles.get(role, 0) + 1
        if reading.is_review(task, item):
            if reading.admitted_efforts(task, item):
                reviews += 1
            else:
                landing += 1
        flags = reading.flags(task, item)
        if reading.exempt(task, item, flags, number is not None) is not None:
            continue
        changed = set() if number is None else reading.open_change_repos(number)
        for repo in named:
            if repo not in changed:
                repos[repo] = repos.get(repo, 0) + 1
    return {
        "roles": dict(sorted(roles.items())),
        "repos": dict(sorted(repos.items())),
        "reviews": reviews,
        "landing": landing,
    }


def _notes(reading):
    notes = []
    for effort in reading.record.read("efforts", standing="admitted"):
        note = reading.effort(effort["id"])[1][3]
        if note:
            notes.append(f"effort {effort['id']}: order: {note}")
    return notes


def _build(record, now, settings):
    if not schema.is_int(now):
        raise contract.Refused(f"now is a time in milliseconds, not {now!r}")
    settings.check()
    with record.reading() as seq:
        reading = _Reading(record, now, settings)
        ready = record.ready(now=now)
        pairs, left = _gather(reading, ready)
        entries, cut = _entries(reading, pairs)
        counts = _counts(reading)
        floor = max(0, settings.review_slots - counts["reviews"])
        candidates = _tiers(entries, floor, counts["reviews"], settings.review_slots)
        return Sequence(
            candidates=tuple(candidates),
            left=tuple(left + cut),
            notes=tuple(_notes(reading)),
            counts=counts,
            at=now,
            seq=seq,
        )


def order(record, *, now, settings):
    """The sequence at `now`: the ready read narrowed by schedules and flags,
    sorted by the tiers and each effort's rule. Writes nothing."""
    return _build(record, now, settings)


def place(record, *, now, settings, task=None, item=None):
    """One task or item as `order` would place it, a `Candidate`, or a `Left`
    saying why it is not one. An item with an open task is placed as its
    task."""
    if (task is None) == (item is None):
        raise ValueError("place takes a task or an item, one of them")
    with record.reading():
        got = _build(record, now, settings)
        for found in (*got.candidates, *got.left):
            if (found.task == task) if task is not None else (found.item == item):
                return found
        return _unplaced(record, now, task, item)


def _unplaced(record, now, task, item):
    """Why a task or item the sequence did not name is not ready."""
    if task is not None:
        found = record.get("tasks", task)
        if found is None:
            return Left(task, None, f"task {task}: there is no such task")
        why = contract._unready(record, found, now) or "it is not in the ready set"
        return Left(
            task, found["number"], f"task {task}: {_plain(why, f'task {task}')}"
        )
    found = record.get("items", item)
    if found is None:
        return Left(None, item, f"item {item}: there is no such item")
    why = contract._ineligible(record, found)
    if found["state"] != "ready":
        why = f"item {item} is {found['state']}, not ready"
    why = _plain(why or "it is not in the ready set", f"item {item}")
    return Left(None, item, f"item {item}: {why}")
