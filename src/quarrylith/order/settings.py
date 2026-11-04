# SPDX-License-Identifier: Apache-2.0
"""The order's live settings: what the caller reads from its configuration
and passes at every call. Nothing here caches a value, so a change takes
effect at the next sequence and never touches a running task.

`Settings.problems()` names each invalid field; `order` refuses settings that
have problems (`SettingsError`, a `ValueError`). Keeping the last good
settings is the caller's."""

from dataclasses import dataclass, field

from ..record import schema
from . import schedule as schedules

_REVIEW_KINDS = schema.VALUES[("reviews", "kind")]


class SettingsError(ValueError):
    """Settings with problems: the message names each one."""


def _whole(value, least):
    return isinstance(value, int) and not isinstance(value, bool) and value >= least


@dataclass(frozen=True)
class Settings:
    review_slots: int = 2  # the floor's size, and the landing tier's cap
    landing_limit: int = 6  # unmerged changes per repo; 0 = no limit
    instances: dict = field(default_factory=dict)  # {role: n}; a role not named: 1
    night: str | None = None  # window clauses, a `tz` clause allowed; no cron
    hold_after_s: int = 1800  # how long a refused candidate may be passed
    merge_rules: tuple = (
        {"name": "pool-approved", "risk_at_most": 4, "reviews": ["pool"]},
    )

    def problems(self):
        """Each fault, one line naming the field; [] when every field is
        valid."""
        found = []
        for name in ("review_slots", "landing_limit", "hold_after_s"):
            if not _whole(getattr(self, name), 0):
                found.append(
                    f"{name}: a whole number from 0, not {getattr(self, name)!r}"
                )
        found.extend(self._instances())
        found.extend(self._night())
        found.extend(self._rules())
        return found

    def _instances(self):
        if not isinstance(self.instances, dict):
            return [f"instances: a {{role: n}} map, not {self.instances!r}"]
        return [
            f"instances: role {role!r} has {n!r}, a whole number from 1 is needed"
            for role, n in self.instances.items()
            if not _whole(n, 1)
        ]

    def _night(self):
        if self.night is None:
            return []
        if not isinstance(self.night, str):
            return [f"night: window clauses as text, not {self.night!r}"]
        try:
            parsed = schedules.parse(self.night)
        except schedules.ScheduleError as e:
            return [f"night: {e}"]
        if parsed.cron is not None:
            return ["night: window clauses only, a cron is not a window"]
        return []

    def _rules(self):
        if not isinstance(self.merge_rules, (tuple, list)):
            return [f"merge_rules: a list of rules, not {self.merge_rules!r}"]
        found = []
        for i, rule in enumerate(self.merge_rules):
            if not isinstance(rule, dict):
                found.append(f"merge_rules[{i}]: a rule is an object, not {rule!r}")
                continue
            label = f"merge_rules[{i}]"
            if not isinstance(rule.get("name"), str) or not rule.get("name"):
                found.append(f"{label}: a rule needs a name")
            if not (_whole(rule.get("risk_at_most"), 0) and rule["risk_at_most"] <= 10):
                found.append(
                    f"{label}: risk_at_most is a whole number from 0 to 10, "
                    f"not {rule.get('risk_at_most')!r}"
                )
            reviews = rule.get("reviews")
            if not isinstance(reviews, (list, tuple)):
                found.append(f"{label}: reviews is a list of review kinds")
                continue
            for kind in reviews:
                if kind not in _REVIEW_KINDS:
                    found.append(
                        f"{label}: review kind {kind!r} is not one of "
                        f"{', '.join(_REVIEW_KINDS)}"
                    )
        return found

    def check(self):
        """Refuse settings that have problems."""
        problems = self.problems()
        if problems:
            raise SettingsError("settings: " + "; ".join(problems))


DEFAULT = Settings()
