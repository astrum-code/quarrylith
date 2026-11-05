# SPDX-License-Identifier: Apache-2.0
"""The walk: the per-role cut, the landing limit and the holds behind a
refused candidate, applied to a sequence in its own order.

The caller walks the sequence, asks the allocator about each candidate and
tells the walk what came of it:

    walk = Walk(sequence, settings=settings, waiting=kept)
    for c in sequence.candidates:
        answer = placement.place(c.task, now=now)
        if walk.check(c, answer.needs):
            continue                       # walk.left has why
        if fits(answer):
            start(c)
            walk.started(c)
        else:
            walk.refused(c, answer)
    kept = walk.waiting

A cut candidate is cut only for its own role's or repo's count, never for
where it stands in its role's list: the sequence has already put the effort's
order first. The walk reads the allocator's refusal by attribute name
(`text`, `waits`, `until`, and `shorts[].resource`) and imports nothing of
the allocator. It reads no clock (`at` is the sequence's) and writes nothing
to the record: the first-refused times are the caller's map.
"""

import datetime as dt

_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
#: What a hold says when the refused candidate was short of every resource.
_EVERY = "every resource"


def _stamp(ms):
    return _EPOCH + dt.timedelta(milliseconds=ms)


def _when(ms, at):
    """A time as `HH:MM` UTC, with its date when it is not the day of `at`."""
    when, today = _stamp(ms), _stamp(at)
    if when.date() == today.date():
        return when.strftime("%H:%M")
    return when.strftime("%Y-%m-%d %H:%M")


def _named(candidate):
    if candidate.item is not None:
        return f"#{candidate.item}"
    return f"task {candidate.task}"


class _Hold:
    """A refused candidate that has waited long enough: what it was short
    of, since when, and until when it may be, if the allocator said."""

    def __init__(self, candidate, refusal, since):
        self.key = candidate.key
        self.name = _named(candidate)
        self.since = since
        self.until = refusal.until
        resources = {short.resource for short in refusal.shorts}
        self.every = None in resources
        self.resources = frozenset(r for r in resources if r is not None)

    def holds(self, needs):
        """The resources a candidate with `needs` would take from it, as
        text, or None when it takes none of them. `needs` None is a caller
        that skipped the allocator: held behind any hold."""
        if self.every:
            return _EVERY
        if needs is None:
            return ", ".join(sorted(self.resources))
        taken = self.resources.intersection(needs)
        return ", ".join(sorted(taken)) if taken else None


class Walk:
    """One walk over a `Sequence`: the live counts it starts from, the starts
    and refusals of this walk, and what it left."""

    def __init__(self, sequence, *, settings, waiting=None):
        settings.check()
        self.sequence = sequence
        self.settings = settings
        self.left = []  # [(candidate, why)], in the order checked
        known = {c.key for c in sequence.candidates}
        #: {candidate key: first refused at}; the caller keeps it. A
        #: candidate that is not in the sequence loses its wait.
        self.waiting = {k: v for k, v in (waiting or {}).items() if k in known}
        self._roles = dict(sequence.counts["roles"])
        self._repos = dict(sequence.counts["repos"])
        self._holds = []  # the held candidates of this walk, in order

    # -- the questions ------------------------------------------------------

    def check(self, candidate, needs=None):
        """Why `candidate` may not start now, or None: its role is at its
        instances, a repo it adds a change to is at the landing limit, or a
        candidate ahead of it is held on a resource it needs. Tested in that
        order; a reason is also put in `left`."""
        why = (
            self._role(candidate)
            or self._limit(candidate)
            or self._held(candidate, needs)
        )
        if why:
            self.left.append((candidate, why))
        return why

    def _role(self, candidate):
        role = candidate.role
        if role is None:
            return None
        limit = self.settings.instances.get(role, 1)
        busy = self._roles.get(role, 0)
        if busy >= limit:
            return f"role {role}: {busy} of {limit} busy"
        return None

    def _limit(self, candidate):
        limit = self.settings.landing_limit
        if not limit or candidate.exempt:
            return None
        full = [
            f"repo {repo}: {self._repos.get(repo, 0)} unmerged of {limit}"
            for repo in candidate.adds
            if self._repos.get(repo, 0) >= limit
        ]
        return "; ".join(full) or None

    def _held(self, candidate, needs):
        for hold in self._holds:
            short = hold.holds(needs)
            if short is None:
                continue
            if hold.until is not None:
                return (
                    f"held behind {hold.name}: {short} until "
                    f"{_when(hold.until, self.sequence.at)}"
                )
            return (
                f"held behind {hold.name}: {short} short since "
                f"{_when(hold.since, self.sequence.at)}"
            )
        return None

    # -- the answers --------------------------------------------------------

    def started(self, candidate):
        """The candidate started: it is counted against its role and, unless
        it is exempt from the landing limit (as a live run of it would be),
        against each repo it adds a change to; its wait and its hold end."""
        if candidate.role is not None:
            self._roles[candidate.role] = self._roles.get(candidate.role, 0) + 1
        if not candidate.exempt:
            for repo in candidate.adds:
                self._repos[repo] = self._repos.get(repo, 0) + 1
        self.waiting.pop(candidate.key, None)
        self._holds = [h for h in self._holds if h.key != candidate.key]

    def refused(self, candidate, refusal):
        """The allocator refused the candidate. It is left with the refusal's
        text. A `never` refusal (`waits` false) holds nothing. Any other
        starts its wait, if it has none, and once it has waited
        `hold_after_s` no later candidate that needs what it was short of may
        start."""
        self.left.append((candidate, refusal.text))
        if not refusal.waits:
            return
        since = self.waiting.setdefault(candidate.key, self.sequence.at)
        self._holds = [h for h in self._holds if h.key != candidate.key]
        if self.sequence.at - since >= self.settings.hold_after_s * 1000:
            self._holds.append(_Hold(candidate, refusal, since))
