---
title: The order
owner: workbench
mechanism: order
templet: 3163
status: built
base:
  quarrylith: bd0420d
  benchLith: 2a8a78e5
pins:
  - repo: quarrylith
    symbols:
      - src/quarrylith/record/contract.py::Record.ready
      - src/quarrylith/record/contract.py::Record.landable
      - src/quarrylith/record/contract.py::Record.start_run
      - src/quarrylith/record/contract.py::Record.effective_labels
      - src/quarrylith/record/contract.py::efforts_of
      - src/quarrylith/record/contract.py::_ineligible
      - src/quarrylith/record/contract.py::_unready
      - src/quarrylith/record/schema.py::LABEL_RE
      - src/quarrylith/record/schema.py::FLAG_RE
      - src/quarrylith/record/schema.py::MULTI_VALUED_LABELS
      - src/quarrylith/record/schema.py::check_labels
    paths: [src/quarrylith/order/]
    at: f7f4338
  - repo: benchLith
    symbols:
      - bin/lith::launch_order
      - bin/lith::ranked_ready
      - bin/lith::unblocks
      - bin/lith::ranks_first
      - bin/lith::set_rank
      - bin/lith::launch_lanes
      - bin/lith::SCORE_POLICY
      - bin/lith::auto_merge_risk
      - bin/lith::sweep_merge_holds
    at: 2a8a78e5
---

# The order

Plan #3157, effort 0001, step 4: the spec of #3163. The order decides
**what may start next, and why**. It takes the record's ready read, narrows
it by schedules and flags, sorts it by one rule set (floor reviews,
resumes, then each admitted effort's own order), and hands the services a
sequence of candidates, each naming its effort and the rule that placed it.
A walk over that sequence applies the per-role cut and the per-repo landing
limit, and decides what happens behind a candidate the allocator refuses.
The page also specs the schedule parser and the classes of change that may
merge without the user.

**Statement (#3157, user, 2026-10-29):** memLith schedules a mixed workload
on one node, in the yard, under allocation tables, with its record on both
backends behind one contract, and our own development runs on it. This page
is the "schedules" and the "one order" of that sentence. It is policy only:
pure functions over the record's reads. It reserves nothing, starts
nothing, stops nothing and merges nothing.

**What exists at the base commits.**

- quarrylith `bd0420d`: the record (templet 3159, steps 1 to 5 built). The
  ready read `Record.ready(*, now)` gives the items in `ready` and the open
  tasks that are eligible: admitted, linked (directly or through the parent
  chain) to an `admitted` effort, no live dep, no live hold, `not_before`
  passed, attempts under `loop.max_attempts`. The record's page says of it:
  "Step 4 orders this set and does not widen it." Efforts carry `standing`
  and an `order` json, "step 4's rule", default `{"rule":
  "unblocks-then-age"}`. Tasks carry `schedule` (text, unparsed), `labels`,
  `flags`, `loop` (`max_attempts`, `interval_s`) and `supervision`. The
  label grammar (`LABEL_RE`, `FLAG_RE`) and its rule (quarrylith#9, merged
  2026-11-03): one label per key on a record, except the keys of
  `MULTI_VALUED_LABELS`, `repo` and `legacy`; no label twice; a task's
  labels of a key replace all its item's labels of that key in
  `effective_labels(task)`.
- benchLith `2a8a78e5`: today's order, `launch_order`. Each role offers its
  best ready issues, one per free instance (`ranked_ready`: held first,
  then most unblocking, then oldest). Only then are the roles' offers
  sorted together: the floor reviews (`review_slots`), the resumes, the
  ranks table (`ranks_first`), the most unblocking, the oldest. So the
  ranks are applied **after** the per-role cut, and a ranked item that was
  not its role's first never started (#3157's figures). The lanes
  (`launch_lanes`, templet 1376) slice the pool's slots. The sweep merges
  a pool-approved change at risk <= `SCORE_POLICY["auto_merge_risk"]` (4)
  when `sweep_merge_holds` finds nothing.

```mermaid
flowchart LR
  R[("the record")] -->|"ready(now):<br/>admitted work only"| F["schedules and flags:<br/>window, night"]
  R -->|"landable(now):<br/>parked reviews (needed)"| T
  F -->|"in its window"| T["the tiers: floor reviews,<br/>resumes, effort order"]
  E["efforts: standing,<br/>order json (rank, rule)"] -->|"rank, rule"| T
  D["live deps"] -->|"unblocks"| T
  T -->|"one sequence,<br/>each with effort + rule"| W["the walk: per-role cut,<br/>landing limit, holds"]
  S["live settings"] -->|"review_slots, landing_limit,<br/>instances, night, hold_after"| W
  W -->|"next candidate"| A["the allocator (step 3):<br/>place: Fit or Refusal"]
  A -->|"Refusal: shorts, waits, until"| W
  A -->|"Fit: node, needs"| SR["start_run (step 5):<br/>one event, with its reason"]
  SR -->|"run.start"| R
  R -->|"changes, reviews,<br/>checks, questions"| M["merge classes:<br/>auto or the user's"]
  M -->|"class + rule"| L["landing (step 6)"]
```

## Interfaces

### The package

The order is `quarrylith.order`, in quarrylith, standard library only
(`zoneinfo` for time zones). It imports `quarrylith.record` and nothing of
a yard, a harness, a role or a repo host. Who the roles are, how many
instances each has, and the live settings' values are the caller's,
passed in.

| module | holds |
|---|---|
| `quarrylith.order.schedule` | `Schedule`, `parse(text)`, `ScheduleError`, `next_not_before(task, *, finished_at)` |
| `quarrylith.order.settings` | `Settings`, `Settings.problems()`, `DEFAULT` |
| `quarrylith.order.sequence` | `Candidate`, `Left`, `Sequence`, `RULES`, `order(record, *, now, settings)`, `place(record, *, now, settings, task=None, item=None)`, `effort_order(effort)` |
| `quarrylith.order.walk` | `Walk` |
| `quarrylith.order.merge` | `MergeClass`, `classify(record, number, *, settings)` |
| `quarrylith.cli` | `quarrylith order show|schedule|merge` |

Every function reads the record only through its contract (`get`, `read`,
`reading`, `ready`, `effective_labels`, `contract.efforts_of`), inside one
`reading()`, and takes `now` from its caller. None writes.

### Schedules

A task runs **once at or after a time**, **on a cron expression**, or
**looped with an interval**, and any of these may be kept to a **window**.
Each is said once, in the field that holds it:

| what | where | meaning |
|---|---|---|
| once, at or after a time | `tasks.not_before` (the record's) | not before this ms; `ready()` already reads it |
| a loop | `tasks.loop` (the record's): `max_attempts`, `interval_s` | up to `max_attempts` runs, each at least `interval_s` after the last one finished |
| a cron expression, a window, a zone | `tasks.schedule`, text, parsed here | below |

**The grammar of `tasks.schedule`.** Clauses joined by `;`, spaces around
them ignored, each clause once except `window`:

```
schedule := clause (";" clause)*
clause   := "cron" MIN HOUR DOM MON DOW
          | "window" HH:MM "-" HH:MM [DAYS]
          | "tz" ZONE
```

- **`cron`**: five fields, as cron has them: minute 0-59, hour 0-23, day of
  month 1-31, month 1-12 or `jan`..`dec`, day of week 0-7 (0 and 7 are
  Sunday) or `sun`..`sat`. Each field is `*`, a value, a range `a-b`, a
  list `a,b,...`, or any of them with a step `/n`. When both day of month
  and day of week are restricted (neither starts with `*`), a day matches if
  **either** does; otherwise it must match both, as classic cron does. So
  `*/10` is unrestricted: `0 9 */10 * 1` is every 10th day that is also a
  Monday.
- **`window`**: a start and an end in local time, start included, end
  excluded. An end at or before the start crosses midnight (`22:00-06:00`).
  `DAYS` names the days the window **opens** on: `mon-fri`, `sat,sun`,
  `mon,wed`, a range or a list of `mon`..`sun`; none means every day. Several
  `window` clauses make one window, their union.
- **`tz`**: an IANA zone (`Europe/Lisbon`), read with `zoneinfo`; `UTC` when
  none is given. Local times are wall-clock times in that zone. A local
  time that does not exist (a spring-forward gap) is skipped, and one that
  happens twice (fall-back) counts once, at its first instant. A window is
  judged by the wall-clock time of the instant, so one whose start falls in
  a gap opens at the gap's end (`02:30-04:00` opens at 03:00 on the day 02:30
  does not exist), and one that lies wholly inside a gap does not open that
  day.
- Nothing else is a clause. An empty or null `schedule` is no cron and no
  window: the task runs on its `not_before` and its `loop` alone.

```python
class ScheduleError(ValueError): ...   # names the clause and the fault

@dataclass(frozen=True)
class Schedule:
    cron: tuple | None       # a NamedTuple: the five fields expanded to sets
                             #   (days of week 0-6, Sunday 0), then whether
                             #   day of month and day of week were written *
    windows: tuple           # ((start_min, end_min, days), ...) in local minutes
    tz: str                  # "UTC" when none is given

    def within(self, at: int) -> bool: ...          # at in a window, or no window
    def next_open(self, at: int) -> int: ...        # the first instant >= at within a window
    def next_fire(self, after: int) -> int | None:  # the first cron minute > after
        ...                                         #   within a window; None: no cron
    def next_start(self, after: int) -> int | None: ...  # the next window start
                                                    #   after `after`; None: no window
    def local_iso(self, at: int) -> str: ...        # `at` in the zone, ISO 8601

def parse(text: str | None) -> Schedule: ...        # ScheduleError on any fault
def next_not_before(task: dict, *, finished_at: int) -> int | None: ...
def openings(schedule, after: int, count: int) -> list[int]: ...  # the next
                                                    #   window openings, for the command
```

- `parse` is total over its grammar: every input is a `Schedule` or a
  `ScheduleError` naming what is wrong (`cron: minute 61 is past 59`,
  `window: "22-6" is not HH:MM-HH:MM`, `tz: no zone "Mars/Base"`, `unknown
  clause "every"`, `cron given twice`).
- **A cron with a window**: the fires outside the window are **skipped**,
  and `next_fire` takes the next fire inside it. A fire is never moved to
  the window's start: the cron says when, the window only where a fire is
  allowed. `cron 0 */6 * * *; window 22:00-06:00` from 12:30 fires at 00:00,
  not at 22:00. `next_fire` looks at most 5 years ahead (so `29 2` is
  found); with no fire inside a window in that span it raises
  `ScheduleError` (`cron: no fire within its window in 5 years`; with no
  window, `cron: no fire in 5 years`).
- **`next_not_before(task, *, finished_at)`** is what the services write
  into the task's `not_before` after each run (step 5 calls it; the record
  stores it). For a task with a cron: the next fire after `finished_at`.
  For a loop (`interval_s` above 0, or `max_attempts` above 1):
  `finished_at + interval_s`, moved to the next instant within its window. Both: the later of the two. With neither: `None` (nothing to
  set). **Missed fires collapse**: a cron task whose fires passed while it
  could not run (the machine off, the task held) runs once at its next
  chance, never once per missed fire, because `not_before` holds one time
  and the next one is computed from the run's finish.
- **A window gates starts only.** A task outside its window is left out of
  the sequence (below). A run that is inside its window when it starts is
  never stopped by this page when the window ends: whatever happens to a
  run at a window's end is supervision's, step 5's.
- **A task's attempts are its allowance.** A recurring task runs while
  `attempts < loop.max_attempts` (the record's rule); when they are spent it
  is no longer ready. Whoever files a recurring task (the night document's
  loader, a later step) sets the allowance and files the next task.

### Labels and flags: what the order reads

The grammar is the record's. The order reads these, and nothing else of a
record's labels and flags; the allocator (step 3) reads its own.

| read | from | used for |
|---|---|---|
| role | a task's effective `role:` label, else its item's `role` field (so an item's `role:` label beats its own `role` field) | the per-role cut |
| repos | effective `repo:` labels (several on one record) | the landing limit |
| `resync` (flag) | the task's or its item's flags | exempt from the landing limit: work that brings an open change up to date with main |
| `night` (flag) | the task's or its item's flags | the task starts only within `Settings.night` |
| kind | the task's `kind` field, the item's `kind` field | a review is either one's `review` |

**A repo is named the same way** in a `repo:` label and in a change's
`changes.repo`: the same string, the repo's own name. The landing limit
joins the two by that string, so a filer or an import that writes one
writes the other alike (the legacy import takes both from the board, so
they agree today).

Flags are an open list (#3157: supervised, the sandbox class, looped,
night, long, needs:lab, local-ok, ...). A flag this page does not read is
passed on untouched: `long`, `needs:lab`, `local-ok` and the sandbox class
are capacity, the allocator's.

### The settings

```python
@dataclass(frozen=True)
class Settings:
    review_slots: int = 2          # the floor's size, and the landing tier's cap
    landing_limit: int = 6         # unmerged changes per repo; 0 = no limit
    instances: dict = field(default_factory=dict)  # {role: n}; a role not named has 1
    night: str | None = None       # window clauses, e.g. "window 22:00-06:00 mon-fri;
                                   #   window 20:00-08:00 sat,sun; tz Europe/Lisbon"
    hold_after_s: int = 1800       # how long a refused candidate may be passed
    merge_rules: tuple = ({"name": "pool-approved", "risk_at_most": 4,
                           "reviews": ["pool"]},)

    def problems(self) -> list[str]: ...   # [] when every field is valid
```

- **These are live settings** (#3100): the caller reads the yard's
  effective values and passes them at **every** call. Nothing here caches a
  setting, so a change takes effect at the next sequence, a scheduling
  boundary, and never touches a running task.
- `problems()` names each fault: a count below 0, an `instances` value that
  is not a whole number from 1, a `night` that `parse` refuses or that has a
  `cron`, a merge rule without a `name`, a `risk_at_most` outside 0-10, a
  review kind the record does not know. (A rule may name a risk above 7;
  the never-auto bars below still keep risk 8 and up the user's.) `order`, `Walk` and `classify`
  refuse a `Settings` with problems (`ValueError`). Keeping the last good
  settings is the caller's (#1376's last-good rule, #3100).
- `night` set to `None` means no night window: a `night` task is left out,
  with that reason.

### An effort's order

The effort's `order` json is this page's. Its shape:

```json
{"rule": "unblocks-then-age"}
{"rule": "listed", "items": [3163, 3161, 3165]}
{"rule": "unblocks-then-age", "rank": 1}
```

- **`rule`**: `unblocks-then-age` (the default) or `listed`.
  - `unblocks-then-age`: the candidate that unblocks the most open items
    first, then the oldest, then key order.
  - `listed`: the items `items` names first, in that order; every other
    candidate after them, by `unblocks-then-age`. This is how an effort
    says "these first". It replaces the ranks table, scoped to one effort.
- **`rank`**: the effort's place among admitted efforts, lower first. An
  effort with no `rank` comes after every ranked one, and efforts tie by
  `id`.
- `effort_order(effort) -> (rule, rank, items, note)` reads it. An `order`
  that is not this shape (an unknown `rule`, `items` not a list of item
  numbers, a `rank` that is not an int) is read as the default, and `note`
  says why, so a bad edit never stops the order and never hides.

**Unblocks**: the open items that wait on an item through live item deps
(`deps.blocker` = the item, `lifted_at` null), followed through those items'
own, counted once each. A task's unblocks are its item's (none with no
item). A review's are the unblocks of the item it reviews (its `reviews`
record's `target`), since its approval is what lets that merge. **Age** is
the item's `created_at`, or the task's with no item.

### The sequence

```python
RULES = ("floor", "resume", "unblocks-then-age", "listed", "landing")

@dataclass(frozen=True)
class Candidate:
    task: int | None          # a ready task, or None: an item with no open task
    item: int | None
    key: tuple                # ("task", id), else ("item", number): the walk's key
    effort: str               # the effort whose order placed it
    rule: str                 # one of RULES
    tier: int                 # 1 floor, 2 resume, 3 effort order, 4 landing
    why: str                  # one line, below
    unblocks: tuple           # the item numbers, nearest first
    role: str | None
    repos: tuple              # its repo: labels, sorted
    adds: tuple               # the repos where a start would add an unmerged change
    exempt: str | None        # "review", "resync", "post-review": the landing limit skips it

@dataclass(frozen=True)
class Left:
    task: int | None
    item: int | None
    why: str                  # "task 12: outside its window (opens 22:00 Europe/Lisbon)"

@dataclass(frozen=True)
class Sequence:
    candidates: tuple         # in order
    left: tuple               # what the ready set held that the order left out, and why
    notes: tuple              # an effort's unreadable order, and the like
    counts: dict              # the live figures the walk starts from (below)
    at: int                   # now
    seq: int                  # the record's seq the reads saw

def order(record, *, now, settings, never=None) -> Sequence: ...
def place(record, *, now, settings, task=None, item=None) -> Candidate | Left: ...
```

**`order(record, *, now, settings, never=None)`**, inside one `reading()`.
`never` is `{candidate key: the allocator's refusal text}`, the candidates
this round's walk saw refused `never` (below, "The landing tier"); each is
left out, `Left` with "never fits: " and that text. None leaves nothing out.

1. **The ready set.** `record.ready(now=now)`. An item with an open or
   running task is represented by its task: the task is a candidate when it
   is in the ready set, and the item alone is never one (else it would start
   beside its task). An item with neither is a candidate as itself (`task`
   None; the services file its task before they start it, step 5).
2. **Schedules and flags narrow it.** A task whose `schedule` `parse`
   refuses is left out, with the fault. A task outside its schedule's window,
   or flagged `night` and outside `Settings.night`, is left out, with when
   its window next opens. Nothing is added.
3. **Each candidate's effort.** For an item (or a task with an item), the
   item's efforts by `contract.efforts_of`, the admitted ones; with several,
   the one that comes first by `rank`, then `id`. For a task with no item,
   its own `effort`. Every candidate of the ready set has one, since the
   ready read gives only an admitted effort's work.
4. **The tiers**, in this order:
   1. **floor**: reviews (a task or an item of kind `review`), as many as
      `review_slots` less the admitted efforts' reviews running now (never
      below 0), taken in the effort order below. The rest of the reviews go
      on to tier 2 or 3. A landing run counts only against the landing
      tier's cap, never against the floor, so live landing reviews never
      take the floor from admitted ones.
   2. **resume**: a candidate whose item has a live lease (`leases`,
      `released_at` null): work in flight, held by someone, to take up again.
   3. **the effort order**: everything else, rule `listed` or
      `unblocks-then-age`, as the effort's `order` says.
   4. **landing**: below.
5. **Inside a tier**, candidates sort by their effort (`rank`, then `id`),
   then by the effort's rule (a listed item by its place in `items`, then
   most unblocks, then oldest), then by key: an item's number, then the
   task's id. So the sequence is a total order, the same on every backend
   for the same record, `now` and settings.
6. **The landing tier** comes only when tiers 1 to 3 hold no candidate that
   the walk's cuts would let start (below, "The landing tier").

**`why`**, one line per candidate, names the candidate, its effort, its
rule and the rule's figure:

```
#3169 task 41: effort 0001, floor -- review 1 of 2, unblocks a merge of #3163
#3163: effort 0001, resume -- held by alpha since 2026-11-03 14:02
#3165 task 44: effort 0001, listed -- 2nd of 3 listed
#3170: effort 0001, unblocks-then-age -- unblocks 2 (#3171, #3172), filed 2026-10-29
#3088 task 9: effort unsorted, landing -- review of a parked effort's change
```

The services carry it, with the effort and the rule, into the run's start
(below, "Needs from the record"), so **every start names its effort and the
rule that placed it**.

**`counts`**: the live figures read in the same `reading()`, from which the
walk starts: per role, the live runs; per repo, the unmerged changes the
landing limit counts; the live review runs of admitted efforts (the
floor's figure); the live landing runs (review runs whose item or task is
linked to no admitted effort: the landing cap's figure, and only its).

**`place(record, *, now, settings, task=None, item=None)`** gives one task
or item as `order` would place it, with its tier, rule, effort and `why`,
or a `Left` with why it is not a candidate. A start the user names by hand
(step 5's `--task N`) goes through `place` and then the same walk, so a
named start is never placed differently from a full round (templet 1376,
round 6: a named start that skipped the round's rules).

### The walk: the per-role cut, the landing limit, the holds

```python
class Walk:
    def __init__(self, sequence, *, settings, waiting=None): ...
    def check(self, candidate, needs=None) -> str | None: ...
    def started(self, candidate) -> None: ...
    def refused(self, candidate, refusal) -> None: ...
    left: list                      # [(candidate, why)] in the order checked
    waiting: dict                   # {candidate key: first refused at}; the caller keeps it
    never: dict                     # {candidate key: refusal text}: refused `never` this walk
    only_never: bool                # nothing started, and every candidate that
                                    #   passed rules 1 to 3 (one at least) was
                                    #   refused `never`
```

**The refusal is the allocator's.** The walk has no refusal type of its own:
`refused(candidate, refusal)` takes the allocator's `Refusal` (its page,
`quarrylith.allocator.Refusal`, with `Short`) and reads four of its
attributes **by name**, so `quarrylith.order` does not import
`quarrylith.allocator` and the walk's build step need not wait for the
allocator's:

| attribute | the walk reads it as |
|---|---|
| `refusal.text` | the `why` it leaves the candidate with |
| `refusal.waits` | `False`: the refusal is **never** (it can never fit, short of a declaration change); `True`: waiting may help |
| `refusal.shorts[].resource` | the resources the candidate was short of, for its hold; a short with `resource` None (`unmeasured`, waits true) is short of **every** resource |
| `refusal.until` | named in the hold's `why` when it is set ("held behind #3170: budget until 18:00") |

The services (step 5) compose the order, the walk and the allocator's
`Placement`, on the seam as the allocator's page has it:

```
sequence = order(record, now=now, settings=settings)
walk = Walk(sequence, settings=settings, waiting=kept)
for c in sequence.candidates:
    # an item with no open task (c.task None): its task is filed first
    answer = placement.place(c.task, now=now)   # Fit or Refusal; both carry needs
    why = walk.check(c, answer.needs)
    if why: continue                            # walk.left has it
    if isinstance(answer, Fit): start_run(...); walk.started(c)
    else: walk.refused(c, answer)
if walk.only_never:                             # the landing tier, below
    sequence = order(record, now=now, settings=settings, never=walk.never)
    walk = Walk(sequence, settings=settings, waiting=walk.waiting)
    ... the same loop, once ...
kept = walk.waiting
```

**`check(candidate, needs)`** returns why the candidate may not start now,
or None, testing in this order:

1. **The per-role cut.** Its role has as many live runs, plus starts in
   this walk, as `instances` gives it (1 when not named): "role alpha: 1
   of 1 busy". A candidate with no role is not cut.
2. **The landing limit.** For each repo in `adds`: that repo's unmerged
   changes, plus starts in this walk that add one there, are at
   `landing_limit` or past it: "repo widgets: 6 unmerged of 6" (every repo
   at its limit is named, joined with "; "). Skipped when
   `landing_limit` is 0 or the candidate is `exempt`.
3. **The landing tier's cap.** A `landing` candidate, while live landing
   runs plus this walk's landing starts reach `review_slots`.
4. **A hold ahead.** An earlier candidate of this walk is **held** (below)
   and short of a resource that `needs` names, or short of every resource
   (a short with `resource` None), or `needs` is None (the allocator's `Fit`
   and `Refusal` always carry a dict, so none arises only from a caller that
   skips `place`): "held behind #3170: memory short since 14:02", or, with
   the refusal's `until` set, "held behind #3170: budget until 18:00".

**`started(candidate)`** adds the candidate to the walk's counts (its role,
each repo of `adds` unless the candidate is `exempt`, a landing run) and drops
its `waiting` entry. An exempt start adds to no repo, as a live run of it is
not in the count either, so the next candidate of that repo is cut by the
same figure in this walk as in the next round.

**`refused(candidate, refusal)`** records the allocator's refusal. A
`never` refusal (`waits` false) is left with its `text`, holds nothing, and
goes into `never`. Any other sets
`waiting[key]` to the sequence's `at` if it has no entry. The candidate is
**held** when it has waited `hold_after_s` or longer (`at` −
`waiting[key]`): from then on, in this walk, no later candidate that needs
what it was short of may start (rule 4). The `waiting` map is the caller's
to keep between walks: `Walk` drops an entry whose candidate is not in the
sequence, so a candidate that left the ready set loses its wait.

**The rule behind a refused candidate** (the seam with step 3, which this
page decides): **a later candidate that fits may pass a refused one, until
the refused one has waited `hold_after_s`; from then on, nothing that needs
a resource it was short of passes it.** Candidates that need none of those
resources still pass. As runs finish and nothing new takes the short
resources, the held candidate fits, at the latest when every run ahead of
it that holds them has finished -- **for a `room` refusal, and for a
`budget` one once its `until` has passed. Not for a `measured` one**: when
memory is short because of something outside the queue (a manual session,
the container machine's drift), the queue's runs finishing may never free
it. Every class needs `memory`, so 30 minutes after a `measured` refusal
the hold stops every start until the host frees memory. That is safe, as an
`unmeasured` refusal is. A running task is never preempted to make room.
(Decided by the user, 2026-11-03, with `hold_after_s` 30 minutes, and the
`measured` consequence kept by the user, 2026-11-04: Rationale.)

**Unmerged changes, counted** (the landing limit's figure, per repo): the
record's open changes in that repo (`changes` with `merged_at` and
`dropped_at` null), plus the live runs whose task's effective `repo:` labels
name it, whose item has no open change there, and which are not `exempt`.
The second term counts work in flight that will open a change, so a
limit of 6 is not passed by six starts at once.

**`adds`**: the candidate's repos where its item has no open change. A
candidate that already has a change open in a repo adds none there (a
resume that pushes to its own change). An item in two repos (`repo:a`,
`repo:b`) adds one in each, is cut if either is at its limit, and counts
one in each when it starts.

**`exempt`**, by #3157's three: `review` (a review task or item);
`resync` (the `resync` flag); `post-review` (a resume whose item's current
round, `review_round`, has a review with verdict `changes`).

### The landing tier

A free slot with nothing admitted to start may review a parked effort's
built change, to land it, and never resume or start work there.

- **Its source** is the record's **`landable(*, now)`** (needed from the
  record, below): the reviews (a task or an item of kind `review`) that are
  eligible by every rule of the ready read but the effort's, linked to at
  least one `parked` effort and to none `admitted`, whose reviewed item has
  an open change. Nothing else of a parked effort is ever a candidate.
- **When**: only when tiers 1 to 3 hold no candidate that passes the walk's
  cuts (rules 1 to 3 of `check`, with no starts yet). A candidate the
  allocator refuses for a while (`waits` true) is still admitted work to
  start, so it does not open the landing tier (the user, 2026-11-03). The
  sequence then holds the landing candidates, rule `landing`, sorted as
  tier 3 is (their parked efforts by `rank` and `id`).
- **A `never` refusal does not keep it shut.** A candidate that can never
  fit is not admitted work to start. `order` decides tier 4 before any
  allocator answer exists, so the walk says it: when a walk started nothing
  and every candidate that passed rules 1 to 3 was refused `never`
  (`walk.only_never`), the services call `order` once more in the same
  round with `never=walk.never`. Those candidates are left out, "never
  fits: ..." with the allocator's text, tiers 1 to 3 then hold none that
  passes the cuts, and tier 4 opens. The second walk keeps the first's
  `waiting`. `never` is not kept between rounds: the next round asks the
  allocator again, so a declaration change is seen at once.
- **Capped at the review slots**: at most `review_slots` landing runs live
  at once (the walk's rule 3). Landing runs count only against this cap,
  never against the floor (tier 1).
- **Started by `start_run` with `placed["rule"]` `landing`** (needed from
  the record, below): the record's start accepts a task `landable(now)`
  gives, checked in the same event, and nothing else of a parked effort.

### Merge classes

The configured gate rules classify which changes may merge without the
user. Today's one rule: **pool-approved at risk <= 4**. The class is read
from the record; merging is the landing step's (step 6), which records the
class's rule name as the merge's `rule`.

```python
@dataclass(frozen=True)
class MergeClass:
    cls: str            # "auto" or "user"
    rule: str | None    # the merge rule that made it auto
    why: tuple          # every reason it is the user's, one line each

def classify(record, number, *, settings) -> MergeClass: ...
```

`classify(record, number)` classifies an item's open changes **as one
set**: `auto` only when every one of them qualifies, so a set merges whole
or waits whole. A change qualifies under a rule when all hold:

- the item's `risk` is set and at most the rule's `risk_at_most`;
- **every** review filed on the item (`target` = the item) in its current
  round (`round` = `review_round`), of any kind, has verdict `approve` and
  `heads[change]` equal to the change's `head`: the approval binds to the
  head it saw (templet 2710). So a conformance review, a second role's
  review or any other review of the round holds the change as today's
  `status:reviewed` does, not only the kinds the rule names;
- among them, for each review kind the rule names, one of that kind;
- every check on `(change, head)` reads `green` or `skipped`, and at least
  one reads `green`;
- the change is not a draft.

Three cases the rules above leave open are read the safe way: an item with
**no open change** is `user` (an empty set is not whole; the reason is "no
open change"); a round with **no review filed** is `user` whatever kinds the
rule names (a rule that names none still needs the round reviewed); and a
review whose `heads` do not name a change has **not judged** it, so it does
not approve it. `classify` refuses (`Refused`) a number the record does not
hold.

And none of these, whatever the rules say (**never auto**, #3044's
non-waivable list and today's sweep rule):

- a `risk` of 8 or more, below 1, or none;
- a review in the item's current round with verdict `changes`;
- a review in the item's current round with no verdict yet (live), of any
  kind;
- a review of a round **after** the item's (`round` > `review_round`) with
  verdict `changes` or none: the filing path may file it before it moves the
  item's round, and a verdict already filed must not be ignored ("review N is
  of round R, after the item's round");
- a review the round requires and nobody has filed: the required kinds come
  from the item, which the record holds, not from what is filed:
  `conformance` when the item's `templet` names another item (it builds to
  an agreed spec), and `second` when its `tier` is 5 or more (the record
  does not bound `tier`, so a tier above the scale reads the strict way, as
  does a `risk` below 1). A pool approval that
  lands before the services file one of these is not enough, so the rule
  holds even when the filing path does not file every review of a round in
  one event;
- the item `overruled_at` set;
- an unanswered question on the item (`questions`, `answered_at` null);
- a templet: the item's `templet` field names the item itself (it holds the
  spec it builds to: it writes a templet), or the item has a review of kind
  `templet` (the owner's review of a templet, which the user's merge
  agrees). The first is set right after the item is filed (`file_item`
  takes no `templet`, so the filer follows it with an `edit_item`), before
  any review of the item can exist, so a templet is known before its
  review is filed, and a pool approval that lands before the owner's review
  is not enough. The filer sets it: the services' filing path (step 5,
  #3165) sets it on every spec item it files, and step 5 tests that. **A
  spec item's `templet` is its own number; a build item's is the spec
  item's.** (Not the page front matter's `templet: N`, which names the
  build issue.) Until the switch, the legacy landing mode, which reads the
  files under `docs/templets/`, does the merging;
- a live hold on the item.

`why` lists every reason that applies, not the first. `merge_rules` empty
classifies everything `user` (today's rollback: `auto_merge_risk = 0`).
Whether the change still merges cleanly with main is the landing step's to
check at the merge, since the record does not hold it.

### Needs from the record

The order reads the record as it is, but for three things. Each is named
here for the coordinator, and is made by a build step (below) that proposes
it on the record's page with a `proposed:` History line, as a change to an
agreed templet is made.

1. **`landable(*, now)`**, a read beside `ready()`, for the landing tier:
   `{"items": [...], "tasks": [...]}`, the reviews `ready()` leaves out
   only because no effort they are linked to is admitted, linked to at
   least one `parked` effort, whose reviewed item has an open change; in key
   order. `ready()` stays as it is.
2. **`start_run(..., placed=None)`**: `placed` is `{"effort", "rule",
   "why"}` (strings, `rule` one of `RULES`), carried in the `run.start`
   event's `data`, so the record says why each run started. No new field
   and no schema change: the event is the record. Step 5 passes it.
3. **`start_run` starts a landing review.** Today `start_run` begins with
   `_unready` (contract.py, `start_run`), which, for a task with an item,
   refuses "no effort item N is linked to is admitted": every landing
   candidate is linked to none, so without this the landing tier could
   never start a run. With `placed["rule"]` equal to `landing`, `start_run`
   accepts a task that `landable(now)` gives (the task itself, or the task
   of an item it gives), **checked in the same event**, instead of the
   ready check; the lease, fence, node and reservation checks are as they
   are. Nothing else of a parked effort starts: any other `placed`, or
   none, is the ready check as today, and a parked effort's task that
   `landable` does not give is refused with `placed["rule"]` `landing` too.
   The record's invariant 9 gains a sentence of its own: "`start_run`
   starts no task of a parked effort either, except a review `landable`
   gives when its `placed` names the rule `landing`."

### The commands

`quarrylith order show URL [--now MS] [--settings FILE] [--json]` prints
the sequence: one line per candidate (its `why`), then the left-out with
their reasons, then the notes and the counts. `quarrylith order schedule
TEXT [--from MS] [--count N]` prints the next N fires and window openings
of a schedule, or its `ScheduleError`. `quarrylith order merge URL NUMBER
[--settings FILE]` prints an item's class and every reason. A settings
file is a JSON object of `Settings`' fields. Each prints its phases with
elapsed time past five seconds, and takes `--timing` (the store layer) and
`--debug`, as the CLI rule asks.

## Reuse

- quarrylith `src/quarrylith/record/contract.py::Record.ready` -- the set
  the order sorts and never widens; `_ineligible` and `_unready` -- its
  rules, which `landable` reuses with the effort's standing turned the
  other way.
- quarrylith `contract.py::efforts_of` -- a candidate's efforts, its own
  links else its parent chain's, so unlinked work is placed under its
  parent's effort.
- quarrylith `contract.py::Record.effective_labels` and
  `schema.py::MULTI_VALUED_LABELS` -- a task's role and repos, several
  `repo:` labels on one record.
- quarrylith `gauges.py::unmerged_changes` -- the per-repo count of open
  changes the landing limit starts from (the gauge stays the dashboard's
  figure; the limit adds work in flight to it).
- benchLith `bin/lith::unblocks` -- the transitive count, taken as written:
  breadth first, nearest first, each item once.
- benchLith `bin/lith::launch_order` -- the floor (`review_slots`), resumes
  before new work, a review counting its target's unblocks, and the reason
  line (`order_lines`). The order keeps these and moves the cut after the
  sort.
- benchLith `bin/lith::sweep_merge_holds` and `auto_merge_risk` -- every
  condition of today's auto-merge, as the merge class's bars.
- Templet 1376's trilithon holds and its "a hold never takes a guarantee"
  -- the ancestor of the walk's holds, without writing any state into the
  record.

What looked reusable but was not:

- **The ranks table** (`ranks_first`, `set_rank`): one global list, applied
  after the per-role cut. Retired (#3157); an effort's `listed` rule does
  the same inside one effort, before the cut. The import leaves the table
  behind.
- **The lanes** (`launch_lanes`, templet 1376's slot plan, shares,
  reserves, the slow start): capacity, not order (user, 2026-10-29, on
  #3044: lanes stay per repo and harness and schedule by capacity; an
  effort's priority orders work inside a lane). Ceilings are the
  allocator's resources; floors, reserves, share entitlements and the slow
  start are dropped, and would be this page's if they return (Rationale).
- **`night_in_force`**: a settings window read by one function. A window is
  now a schedule clause, and `Settings.night` is one, so the night is
  parsed by the same parser as any task's window.
- **`ranked_ready`**: a role's own list, cut before the sort. The order has
  one list.

## Invariants

1. **The order narrows; it never widens.** Every candidate of tiers 1 to 3
   is in `ready(now=now)`, and every landing candidate is in
   `landable(now=now)`. Nothing else is ever a candidate.
2. **Admitted work comes first.** No landing candidate is in a sequence that
   holds a tier 1 to 3 candidate passing the cuts, and a landing candidate
   is a review, never a resume or new work. A candidate refused `never` is
   not admitted work to start, so it never keeps the landing tier shut; one
   refused for a while (`waits` true) does.
3. **Every candidate names one effort and one rule** of `RULES`, and its
   `why` says both. Every start the services make from a sequence carries
   them (`placed`).
4. **The order is applied before the cut.** The sequence is one list over
   every role; the per-role cut and the landing limit walk it in order, and
   a candidate is cut only for its own role's or repo's count, never for
   where it stands in its role's list.
5. **The landing limit holds.** A start never brings a repo's unmerged
   changes past `landing_limit`, except an `exempt` one.
6. **Nothing starves behind a passing rule.** A candidate refused for want
   of a resource (not `never`) is, once it has waited `hold_after_s`, never
   passed by a candidate that needs that resource.
7. **Pure policy.** `order`, `place`, `Walk` and `classify` write nothing to
   the record, read it only through the contract inside one `reading()`,
   read no clock (`now` is given), and give the same result for the same
   record, `now`, settings and `waiting` on every backend.
8. **No preemption.** Nothing on this page stops, pauses or signals a run.
   A window, a schedule and a hold gate starts only.
9. **Settings are live.** Every call reads the settings it is given, and
   nothing is cached between calls.
10. **Never auto** for a change at risk >= 8 or unscored, flagged by a
    `changes` verdict in its round, with a review of its round not yet
    given or not yet filed, overruled, with a question open, a templet, under a live hold,
    approved for another head by any review of its round, or with a check
    not green; and a set is `auto` only whole. `classify` is never looser
    than today's rule: every review of the round approves the head.
11. **quarrylith names nothing internal** (the record's invariant 15): no
    role, repo host or yard in `src/`, and the imports on its allow-list.

## Acceptance

Each item is a test marked `templet:3163`, run over every backend in
`BACKENDS` where it reads a record. An ordering test builds its fixture so
that **every other order of the keys it checks gives a different answer**
(templet 1376, round 3: a fixture that passed with the keys swapped). A test
that needs a named time zone skips with the reason on a host with no zone
database, never passes.

- [ ] `parse` refuses, each with a message naming the clause: a cron of four
  fields, a minute of 60, a day of week of 8, a window `22-6`, an unknown
  zone, an unknown clause, `cron` or `tz` given twice
- [ ] `next_fire` of `cron 0 22 * * mon-fri; tz Europe/Lisbon` from a
  Friday 23:00 is Monday 22:00 local; across a spring-forward gap a fire in
  the gap is skipped, and across a fall-back an ambiguous fire comes once
- [ ] with both day of month and day of week restricted (neither starts
  with `*`), a day matching either one fires; `*/10` with a weekday needs both
- [ ] `window 22:00-06:00 fri` holds Friday 22:00 and Saturday 05:59, and
  not Saturday 06:00 nor Thursday 23:00; two `window` clauses are their
  union; `next_open` gives the next start
- [ ] a cron with a window skips the fires outside it:
  `cron 0 */6 * * *; window 22:00-06:00` from 12:30 gives 00:00, not 18:00
  and not 22:00, and from 00:30 gives 00:00 the next day (06:00 is the
  window's end, excluded); `cron 0 12 * * *; window 22:00-06:00` raises
  `ScheduleError` naming no fire within its window
- [ ] `next_not_before`: a loop's is the finish plus `interval_s`, moved to
  its window's next opening; a cron task's is the first fire after the
  finish, so fires missed while it could not run give one run, not several
- [ ] the order never widens: over a fixture with items of a parked effort,
  an unadmitted machine-filed should-fix item, an item with a live dep, one
  with a live hold, a task not before now and a task past its attempts,
  every candidate of tiers 1 to 3 is in `ready()` and every landing
  candidate in `landable()`
- [ ] an item with an open task in the ready set is a candidate only as its
  task, and an item with an open task that is not ready is not a candidate
- [ ] the tiers: with `review_slots` 2 and one review running, one review is
  floor, then the resumes, then the effort order; a floor review of a
  lower-ranked effort comes before new work of a higher-ranked one, and the
  second review waits in the effort order
- [ ] `unblocks-then-age`: more unblocks first (counted through a chain, each
  item once), then older, then the lower number; a review counts the
  unblocks of the item it reviews
- [ ] `listed`: the listed items first, in the list's order, then the rest
  by `unblocks-then-age`; an `order` with an unknown rule is read as the
  default and a note says why
- [ ] efforts by `rank`, lower first, unranked after by `id`; an item linked
  to two admitted efforts is placed once, under the one that comes first;
  an item with no link of its own is placed under its parent's effort
- [ ] a task with a schedule `parse` refuses, one outside its window, and a
  `night` task outside `Settings.night` (or with no night set) are left out,
  each with its reason; inside the window, it is a candidate
- [ ] every candidate names an effort and a rule of `RULES`, and its `why`
  holds both; `"ranks"` is no record kind and no read of the order's
- [ ] the order writes nothing (`head()` unchanged), and the same record,
  `now` and settings give the same sequence twice and on every backend
- [ ] `place` gives a task the same tier, rule and effort that `order` gives
  it, and a `Left` with the same reason for one the order leaves out
- [ ] the order before the cut: role alpha with one instance has an old item
  that unblocks nothing and a newer item effort 0001 lists first; the listed
  item is the candidate the walk lets start, and the old one is cut with
  "role alpha: 1 of 1 busy" once it starts
- [ ] the landing limit: a repo with 6 unmerged changes (5 open and a live
  run with none yet) cuts new work there, naming the repo and the counts; a
  review, a `resync` task and a post-review resume pass; a resume whose item
  has an open change in that repo passes; an item in two repos is cut when
  either is at its limit and counts one in each when it starts; a limit of 0
  cuts nothing, and the next sequence reads a changed limit
- [ ] passing: a refused candidate is passed by a later one that fits;
  past `hold_after_s` a later candidate needing the resource it was short of
  is held, naming it, while one needing other resources passes; a refusal
  with a short whose `resource` is None holds every later candidate; a
  refusal with `until` set names it in the hold's `why`; a `never` refusal
  (`waits` false) holds nothing and is in `walk.never`; when the held
  candidate starts, the hold ends; `waiting` drops a candidate no longer in
  the sequence. The refusals are given as objects with only the allocator's
  attribute names (`text`, `waits`, `shorts[].resource`, `until`), and
  `quarrylith.order` imports nothing of `quarrylith.allocator`
- [ ] the landing tier: with no admitted candidate passing the cuts, a
  parked effort's review of an open change is a candidate, rule `landing`;
  never a parked effort's resume or new work, nor a review whose item has no
  open change; at most `review_slots` landing runs at once, and live
  landing runs leave the floor's `review_slots` whole; with an admitted
  candidate refused by the allocator with `waits` true, no landing
  candidate; with every candidate that passed the cuts refused `never`,
  `walk.only_never` is true and `order(..., never=walk.never)` leaves them
  out ("never fits: ...") and holds the landing candidates; and
  `start_run` with `placed` rule `landing` starts the landing review, while
  a parked effort's non-review task is still refused, with `placed` rule
  `landing` or without
- [ ] `Settings.problems()` names each invalid field, and `order`, `Walk`
  and `classify` refuse settings that have problems
- [ ] `classify`: a pool approval of the change's head in the current round,
  checks green, risk 4, gives `auto` with the rule `pool-approved`; each of
  risk 5, risk 8 under a rule allowing 9, no risk, a `changes` verdict in the
  round, an approval of an older head, a red or pending check, a draft,
  `overruled_at`, an open question, a `templet` review, an item whose
  `templet` names itself (with no review of kind `templet` filed yet) and a
  live hold gives `user` with that reason among `why`; so does a
  conformance review of the round still live (no verdict), and a second
  role's review of the round that approved an older head, each with the pool
  approval in place; and so does a tier-5 item with only the pool's
  approval, and an item building to another item's templet with only the
  pool's approval (the required review not filed); a set with one member not qualifying is `user` whole;
  empty `merge_rules` gives `user` for all
- [ ] `quarrylith order show`, `schedule` and `merge` print as the page says,
  exit 2 on a refused schedule or settings, and take `--timing` and
  `--debug`

## Build steps

In merge order. Each is one PR in quarrylith that merges alone and passes
its gate without any later step; its tests carry `templet:3163`. The last
step's PR sets `status: built` and the pins, and #3163 closes with it.

1. **The schedule parser** -- `quarrylith.order.schedule`: `parse`,
   `Schedule`, `next_not_before`; `quarrylith order schedule`.
   Acceptance: `parse` refuses; `next_fire` across a weekend and DST; day
   of month or week; `window`; a cron with a window; `next_not_before`.
   Aim: tier 3.
2. **The sequence** -- `quarrylith.order.settings` and
   `quarrylith.order.sequence`: `Settings`, `effort_order`, `order`,
   `place`, the tiers 1 to 3, schedules and flags, `why`, `counts`;
   `quarrylith order show`.
   Acceptance: never widens; an item as its task; the tiers;
   `unblocks-then-age`; `listed`; efforts by rank; schedules and flags;
   effort and rule named; writes nothing; `place`; `Settings.problems()`.
   Aim: tier 3.
3. **The walk** -- `quarrylith.order.walk`: `Walk`, the per-role cut, the
   landing limit and its count, the holds, read from the allocator's
   `Refusal` by attribute name (so it needs none of the allocator's steps).
   Acceptance: the order before the cut; the landing limit; passing.
   Aim: tier 3.
4. **What the record gains for the order** -- quarrylith's record:
   `landable(*, now)`, `start_run(..., placed=)`, and `start_run` starting a
   task `landable` gives when `placed` names the rule `landing` (with
   invariant 9's sentence), each proposed on the record's page
   (`3159-the-record.md`) with a `proposed:` History line, which the
   record's conformance review answers. No acceptance item of this page:
   its tests are the record's, marked `templet:3159`.
   Aim: tier 3.
5. **The landing tier** -- tier 4 in `order`, from `landable`; `never` in
   `order` and `Walk.never`, `Walk.only_never`; the walk's landing cap.
   Acceptance: the landing tier (its `start_run` half over step 4's
   record).
   Aim: tier 3.
6. **Merge classes** -- `quarrylith.order.merge`: `classify`, `MergeClass`;
   `quarrylith order merge`. It needs only the record, so it may merge
   before steps 2 to 5; whichever merges last sets `built`.
   Acceptance: `classify`; the commands.
   Aim: tier 3.

**Which step builds what this page leaves:**

- the services that call `order`, the walk and the allocator, write
  `not_before` from `next_not_before`, keep `waiting`, re-run `order` with
  `never` when `walk.only_never`, file an item's task, pass `placed`, and
  set a spec item's `templet` to its own number when filing it -- step 5
  (#3165);
- **one open item per effort per shared hand-written page** (a page several
  efforts edit, edited through one issue per effort) -- step 5 (#3165), the
  services' filing path: it files an effort's edit of the page into that
  effort's open item for it, or a new one when there is none;
- **the regeneration of generated pages at merge** (`docs/ATLAS.md`,
  `PLAN.md` status: regenerated at merge, never in a PR) -- step 6 (#3167),
  the GitHub connector, which performs the merges: it files a `job` task
  per merge that needs one, in the merged change's effort, and this order
  places it like any task;
- merging the `auto` class and recording its `rule` -- the landing step:
  the legacy landing mode until the switch, then step 6;
- the dashboard's view of the sequence and its reasons -- step 7 (#3169).

## Boundaries

- What the build may touch: quarrylith's `src/quarrylith/order/`,
  `src/quarrylith/cli.py` (the `order` commands), `tests/`, `changes/`; and,
  for step 4 only, `src/quarrylith/record/` and the record's page, by the
  `proposed:` lines above.
- No other repo. benchLith's order (`launch_order`, the ranks, the lanes,
  the sweep's merge) stays as it is until the switch (#3170), which points
  bin/lith at the scheduler; this page edits nothing of bin/lith.
- No services, no socket, no process, no yard code: step 5's (#3165).
- No allocation: needs, nodes, capacity, reservations, standing consumers,
  lanes and long jobs are the allocator's (step 3, #3161). The walk reads
  its answer and nothing of how it was reached.
- No writes to the record from `quarrylith.order`; no merge, no push, no
  GitHub: the landing step's.
- No roles named in `src/`: the role in a label is a value, and `instances`
  comes from the caller. Nothing internal in `src/` or `tests/`
  (quarrylith is public-bound): fixtures use neutral roles and repos
  (`role:alpha`, `repo:widgets`). This page lives in `docs/`, which the
  mirror leaves out.

## Rationale

**Decided by the user, 2026-11-03** (the six choices this page put to the
user, each taken as recommended):

1. **Behind a refused candidate**: pass, then hold after `hold_after_s`.
2. **Across efforts**: strict rank.
3. **The landing tier stays shut** while an admitted candidate is only
   refused (for a while: a `never` refusal does not keep it shut, the
   owner's review of this page; "The landing tier").
4. **`hold_after_s` 30 minutes and `review_slots` 2**, both live settings.
5. **The regeneration of generated pages**: step 6.
6. **Lanes are capacity, the allocator's** (with the user's #3044
   decision).

**For the user's eyes: holds make a harness's slots first in, first out.**
`slots.<harness>` is a resource of the allocator's. So after
`hold_after_s`, a candidate waiting for a harness slot holds every later
candidate that needs the same harness's slot: the slots go first in, first
out, in the sequence's order. That is a change from today, where each
role's own list decides who takes its next slot. The owner's review agrees
it is right (the sequence's order is the effort's order), and it is named
here so it is not a surprise.

**For the user's eyes: a `measured` hold can stop every start.** The hold's
guarantee (that the held candidate fits once the runs ahead of it finish)
holds for `room`, and for `budget` once `until` passes, but not for
`measured`: memory short because of something outside the queue may never
be freed by the queue's own runs. Every class needs `memory`, so 30 minutes
after a `measured` refusal the hold stops every start until the host frees
memory. Decided by the user, 2026-11-04: it stays ("safe, and say so"); it
is safe, as an `unmeasured` refusal is, and the allocator's drift
observation says why memory can be short.

**Passing a refused candidate, with a hold after a wait.** The seam leaves
this page to say what happens behind a candidate the allocator refuses.
Chosen: a later one that fits may pass it, until it has waited
`hold_after_s` (30 minutes, a live setting); from then on nothing that
needs a resource it was short of passes it. Rejected:

- **strictly in order** (the refused one holds the rest): one lab cell
  waiting for a VM's memory would idle every harness slot for as long, on a
  machine with room for other work. #3157's figures were too little
  progress, and an idle pool is that;
- **pass always**: a large task (a lab cell, a trilithon of #1376) waits
  forever behind a stream of small ones, which is what #1376's holds were
  made against;
- **reserve ahead** (a future reservation for the refused one): the order
  reserves nothing (the seam), and the time it would fit is not known
  until runs finish.

The hold names its resources from the allocator's refusal (its shorts'
`resource`); a short with none named (`unmeasured`) holds everything behind
it, which is strict ordering, safe but idle, and right, since every start
would be refused `unmeasured` too. The walk reads the allocator's `Refusal`
rather than a type of its own: the allocator is the producer, its type is
the richer one, and a mesh placement implements it, so a second type would
need an adapter the services own. It reads four attributes by name rather
than importing the allocator, so the walk's build step waits on none of the
allocator's. It keeps no state in the record: the
first-refused times are the caller's map, and a scheduler restart restarts
the clock, which costs at most one more `hold_after_s` of passing.

**The order before the cut, as one sequence.** Today each role offers its
best item first and the ranks sort only what was offered, so a ranked item
that was not its role's best never started. One sequence over every role,
cut in its order, makes the effort's order the only thing that decides,
and a cut candidate waits only for its own role's run to end.

**Floor and resumes before the effort order.** A review is the last step
before a merge; ranked last it starved behind new work (#945, #1275), and
the floor keeps `review_slots` for it. A resume holds a claim, and its
changes are out (#3157). Both are for admitted efforts only, since they come
from the ready read; a parked effort's review is the landing tier's.

**An effort's order inside the effort, the efforts by rank.** The ranks
table was one list for everything, applied too late. `listed` gives an
effort the same power over its own work. Across efforts, strict rank: the
user admits efforts and ranks them to say what matters, and #3157 is "align
behind efforts and have control over what we run and why". Interleaving the
efforts (a fair share) is the rejected alternative: it would start a
lower-ranked effort's work ahead of a higher one's whenever the turn came
round. The user decided strict rank (2026-11-03). Its risk is a
lower-ranked effort that never starts; the dashboard (step 7) shows each
admitted effort's ready-but-unstarted count, so that is seen, not silent.

**The landing tier from a record read.** The record's ready read is the one
place admission lives (its invariant 9), and this page must not widen it.
A parked effort's review is outside it by design. So the record gains a
second, narrow read, `landable`, which gives only reviews of open changes,
rather than the order re-deriving eligibility from the record's private
rules. The tier opens only when no admitted candidate passes the cuts: a
candidate the allocator refuses for a while is still admitted work, so a
parked review does not take its place. A candidate refused `never` is not:
left counted, one task that no node can run would shut the tier for good.
The second `order` call with `never` keeps tier 4 decided in one place,
`order`, rather than the walk adding candidates the sequence did not hold;
it costs one more pure read, only in a round where nothing else can start.
The record's start must then accept the landing review, since its own
ready check refuses a parked effort's work: `placed` rule `landing` names
the one exception, and the record checks it against `landable` in the same
event, so the exception is the record's, not the order's word.

**Merges never looser than today.** Today's sweep merges only a
`status:reviewed` change, which the board sets when every review of the
round approved (the pool's, the conformance review when a templet is
attached, the second role's). So `classify` asks every review of the
round, not only the kinds a rule names, bars a review still live, and bars
a round whose required review (a conformance review for a build of a
templet, a second role's at tier 5, both read from the item) is not filed.
Marking a templet by the item's own `templet` field, set at filing, rather
than only by its `templet` review, closes the gap where the pool approves
before the owner's review is filed.

**The landing limit counts work in flight.** The record's open changes
alone would let six starts at once each open a seventh. Counting live runs
that have no change yet in the repo closes that. A resume of an item whose
change is open adds nothing, so it is not cut; #3157's three exemptions
(reviews, resyncs, post-review resumes) are the work that lands changes.

**Schedules in the record's fields, the parser here.** `not_before` already
says "once at or after", and `loop` says "how often, how many", so the text
holds only what they cannot: a cron, a window, a zone. One way to say each.
The record keeps `schedule` as text and refuses nothing in it (its
Boundaries), and a schedule the parser refuses is left out with its fault,
so the record need not import the order. Rejected: an `every` clause
(it would say `loop.interval_s` twice), and catching up missed fires one
by one (a machine back after a week would run a nightly seven times). A
cron's fires outside its window are skipped, not moved to the window's
start: moving them would make fires the cron never named (a noon cron
running at 22:00), and a cron that fires inside its window is the way to
say "at the window's start". A loop's interval is a least gap, so moving
its next time into the window is no new fire.

**Windows gate starts, never runs.** #3157: a running task is never
preempted. #731's window end ("killed unless `may_run_over`", later
"progress, not time, decides") is supervision of a running task, step 5's,
and this page does not decide it.

**Merge classes over the record's facts.** Today's rule is
`sweep_merge_holds` over GitHub's facts. The record holds the same facts
(reviews with their heads, checks per head, questions, holds, the drafts),
so the class is a read, and the landing step merges what it says. A
templet is told by the item's `templet` field naming itself, or by its
review of kind `templet`, not by its files, which the record does not
hold. Mergeability against main is checked where the
merge happens.

**Left to later steps, or to other pages:**

- **Lanes** (shares, reserves, profiles, the slow start of long jobs,
  trilithons): capacity, by the user's decision on #3044. Lane ceilings are
  the allocator's resources (step 3's page). Floors, reserves, share
  entitlements and the slow start choose between candidates, which is
  ordering: they are dropped, and would be this page's if they return. The
  order gives one sequence that any lane would take in order.
- **Budget pacing and harness fallback** (#2172, #3100's chain): which
  harness and whether its budget allows a start are the allocator's and the
  services'.
- **The night document** (#731): its entries become tasks with a cron, a
  window and the `night` flag; its loader, its spend ledger and its overrun
  question are later steps'. This page parses what it writes.
- **A shared hand-written page edited through one issue per effort**: a
  rule of filing (one open item per effort per page), not of order. Step 5
  (#3165), the services' filing path, owns it (Build steps), and it needs
  nothing of this page.
- **The regeneration of generated pages**: step 6 (Build steps).

## History

- 2026-11-03 agreed (merge of this PR, user)
- 2026-11-03 changed: a window judged by wall-clock time, so a start in a
  spring-forward gap opens at the gap's end (approved: the conformance review
  of quarrylith#15)
- 2026-11-03 changed: `next_not_before` reads a loop as `interval_s` above 0
  or `max_attempts` above 1, so the record's default loop is none (approved:
  the conformance review of quarrylith#15)
- 2026-11-03 changed: `Schedule.next_start`, `Schedule.local_iso` and
  `openings`, which `quarrylith order schedule` prints from; the shape of
  `cron`; and the no-window wording of the five-year error (approved: the
  conformance review of quarrylith#15)
- 2026-11-04 changed: a day field that starts with `*` (`*`, `*/10`) is
  unrestricted, as in classic cron (user decision, 2026-11-04) (approved: the
  conformance review of quarrylith#15)
- 2026-11-04 changed: "The ready set" reads an item's task as open or
  running, so an item moved back to `ready` while its task runs is left out
  (approved: the conformance review of quarrylith#17)
- 2026-11-04 changed: `started` adds an exempt candidate to no repo's count,
  as the count of live runs leaves an exempt run out (approved: the
  conformance review of quarrylith#18)
- 2026-11-04 changed: a candidate cut by the landing limit names every repo
  at its limit, joined with "; " (approved: the conformance review of
  quarrylith#18)
- 2026-11-04 changed: `classify` reads an item with no open change, a round
  with no review filed, and a review whose `heads` omit a change as the
  user's, and refuses a number the record does not hold (approved: the
  conformance review of quarrylith#19)
- 2026-11-04 changed: a review of a round after the item's, with verdict
  `changes` or none, bars the merge; a tier above 5 needs the second role's
  review, and a risk below 1 is the user's (approved: the conformance review
  of quarrylith#19)
- 2026-11-06 changed: `order` refuses a `never` that is not a map from
  `("task", id)` or `("item", number)` to the refusal's text; the walk's third
  check reads "landing reviews: N of M busy" (N live plus started, M
  `review_slots`); and `place` says "a landing review waits while admitted
  work can start" for a landing review the sequence does not hold because
  tiers 1 to 3 can start work (approved: the conformance review of quarrylith#24)
- 2026-11-06 changed: what a landing candidate is left out for (a schedule,
  a flag, `never`) is in the sequence's `left`, as for any candidate; and the
  landing tier is decided by a `Walk` over tiers 1 to 3 inside `order`, with
  no starts, so the sequence and the walk's first three checks cannot disagree
  (approved: the conformance review of quarrylith#24)
