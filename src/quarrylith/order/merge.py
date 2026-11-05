# SPDX-License-Identifier: Apache-2.0
"""Merge classes: which changes may merge without the user.

`classify(record, number, settings=)` reads an item's open changes from the
record and says whether they are `auto` (the merge rule that allows them is
named) or the `user`'s, with every reason it is the user's. It writes nothing,
reads no clock, reads the record only through its contract inside one
`reading()`, and merges nothing: merging an `auto` set and recording the
rule's name are the landing step's, which also checks that the change still
merges cleanly with main, a fact the record does not hold.

The open changes of an item are judged as one set, so a set merges whole or
waits whole. A change qualifies under a configured rule when:

- the item's risk is set and no more than the rule's `risk_at_most`;
- every review of the item's current round, of any kind, approved the
  change's head (a review binds to the heads it saw), and for each kind the
  rule names one of that kind approved;
- every check on that head reads `green` or `skipped`, one at least `green`;
- the change is not a draft.

And whatever the rules say, a change is the user's with a risk of 8 or more,
below 1 or none; a review of the round asking for changes, or without a verdict
yet, and likewise one filed for a round after the item's; a review the item
requires and nobody has filed (a conformance review when it builds to another
item's templet, a second role's when its tier is 5 or more); an item the user
overruled; an open question; a templet (the item holds its own spec, or a
review of kind `templet` judges it); and a live hold.

Today's rule, pool approval at a risk of 4 or less, is the one default in
`Settings.merge_rules`. No rule at all makes every change the user's.
"""

from dataclasses import dataclass

from ..record import contract

#: Whatever the rules say, a risk at or above this is the user's.
NEVER_AUTO_RISK = 8
#: The tier that needs a second role's review, and every tier above it: the
#: record does not bound `tier`, so an unbounded one reads the strict way.
SECOND_ROLE_TIER = 5
#: The risk scale starts here; a risk below it is no score and is the user's.
LOWEST_RISK = 1

AUTO, USER = "auto", "user"


@dataclass(frozen=True)
class MergeClass:
    cls: str  # "auto" or "user"
    rule: str | None  # the merge rule that made it auto
    why: tuple  # every reason it is the user's, one line each


class _Item:
    """What `classify` reads about one item, in one `reading()`."""

    def __init__(self, record, item):
        self.record = record
        self.item = item
        self.number = item["number"]
        self.round = item["review_round"]
        self.changes = record.read("changes", live=True, number=self.number)
        self.reviews = record.read("reviews", target=self.number)
        self.in_round = [r for r in self.reviews if r["round"] == self.round]
        self.approvals = [r for r in self.in_round if r["verdict"] == "approve"]

    def kinds_of_round(self):
        return {r["kind"] for r in self.in_round}

    # -- what is the user's whatever the rules say -------------------------

    def bars(self):
        item, found = self.item, []
        risk = item["risk"]
        if risk is None:
            found.append("no risk set")
        elif risk >= NEVER_AUTO_RISK:
            found.append(f"risk {risk} is {NEVER_AUTO_RISK} or more")
        elif risk < LOWEST_RISK:
            found.append(f"risk {risk} is below {LOWEST_RISK}")
        if item["overruled_at"] is not None:
            found.append("overruled by the user")
        if item["templet"] == self.number:
            found.append("the item holds a templet: the user's merge agrees it")
        for review in self.reviews:
            if review["kind"] == "templet":
                found.append(
                    f"review {review['id']} (templet) judges a templet: "
                    "the user's merge agrees it"
                )
        found.extend(self._questions())
        found.extend(self._holds())
        for review in self.reviews:
            name = f"review {review['id']} ({review['kind']})"
            verdict, at = review["verdict"], review["round"]
            if at > self.round and verdict in ("changes", None):
                found.append(
                    f"{name} is of round {at}, after the item's round {self.round}"
                )
            elif at == self.round and verdict == "changes":
                found.append(f"{name} asked for changes")
            elif at == self.round and verdict is None:
                found.append(f"{name} has no verdict yet")
        found.extend(self._required())
        return found

    def _questions(self):
        asked = self.record.read("questions", live=True, number=self.number)
        return [f"an open question: {q['text']}" for q in asked]

    def _holds(self):
        held = self.record.read("holds", live=True, number=self.number)
        return [f"a live hold ({h['cause']}): {h['why']}" for h in held]

    def _required(self):
        """The reviews the round requires, from the item: a conformance review
        when it builds to another item's templet, a second role's at tier 5.
        A pool approval that lands before the services file one is not
        enough."""
        item, filed, found = self.item, self.kinds_of_round(), []
        spec = item["templet"]
        if spec is not None and spec != self.number and "conformance" not in filed:
            found.append(
                f"it builds to the templet of item {spec}: no conformance "
                f"review filed in round {self.round}"
            )
        tier = item["tier"]
        if tier is not None and tier >= SECOND_ROLE_TIER and "second" not in filed:
            found.append(f"tier {tier}: no second review filed in round {self.round}")
        return found

    # -- what each change must show ------------------------------------------

    def changes_unmet(self):
        if not self.changes:
            return ["no open change"]
        found = []
        if not self.in_round:
            found.append(f"no review filed in round {self.round}")
        for change in self.changes:
            found.extend(self._change(change))
        return found

    def _change(self, change):
        name = f"change {change['id']} ({change['repo']})"
        head, found = change["head"], []
        if change["draft"]:
            found.append(f"{name} is a draft")
        found.extend(self._checks(change, name))
        for review in self.approvals:
            seen = review["heads"].get(str(change["id"]))
            who = f"review {review['id']} ({review['kind']})"
            if seen is None:
                found.append(f"{who} did not judge {name}")
            elif seen != head:
                found.append(f"{who} approved head {seen} of {name}, now at {head}")
        return found

    def _checks(self, change, name):
        head = change["head"]
        checks = [
            c
            for c in self.record.read("checks", change=change["id"])
            if c["head"] == head
        ]
        if not checks:
            return [f"{name} has no check on head {head}"]
        found = [
            f"{name}: check {c['name']} is {c['state']}"
            for c in checks
            if c["state"] not in ("green", "skipped")
        ]
        if not found and not any(c["state"] == "green" for c in checks):
            found.append(f"{name} has no green check on head {head}")
        return found

    # -- what a rule asks beyond that ---------------------------------------

    def unmet_by(self, rule):
        """Why the change does not qualify under `rule` beyond what every
        rule asks."""
        name, found = rule["name"], []
        risk = self.item["risk"]
        if risk is not None and risk > rule["risk_at_most"]:
            found.append(f"rule {name}: risk {risk} is above {rule['risk_at_most']}")
        approved = {r["kind"] for r in self.approvals}
        for kind in rule["reviews"]:
            if kind not in approved:
                found.append(f"rule {name}: no {kind} review has approved the round")
        return found


def classify(record, number, *, settings):
    """The class of item `number`'s open changes, as one set: `auto` with the
    name of the first rule of `settings.merge_rules` they all qualify under,
    else `user` with every reason that applies. Refused for a number the
    record does not hold and for settings that have problems."""
    settings.check()
    with record.reading():
        found = record.get("items", number)
        if found is None:
            raise contract.Refused(f"no item {number}")
        read = _Item(record, found)
        base = read.bars() + read.changes_unmet()
        rules = list(settings.merge_rules)
        unmet = [read.unmet_by(rule) for rule in rules]
    passed = None
    if not base:
        passed = next((r["name"] for r, u in zip(rules, unmet) if not u), None)
    reasons = base + (["no merge rule is set"] if not rules else [])
    reasons += [line for lines in unmet for line in lines]
    if passed is not None:
        return MergeClass(AUTO, passed, ())
    return MergeClass(USER, None, tuple(dict.fromkeys(reasons)))
