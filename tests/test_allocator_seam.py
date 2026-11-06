# SPDX-License-Identifier: Apache-2.0
"""The allocator's seam (templet:3161, build step 1): `Placement`, the answers
`Fit`, `Short` and `Refusal`, and the entry point group a placement registers
in."""

import dataclasses
import importlib.metadata
import inspect
import json
import tomllib
from pathlib import Path

import pytest

from quarrylith.allocator import (
    CAUSES,
    PLACEMENT_GROUP,
    Fit,
    Placement,
    Refusal,
    Short,
    placement_for,
    refuse,
)
from quarrylith.allocator.host import HostPlacement

GIB = 2**30
ROOT = Path(__file__).resolve().parent.parent


def short(cause="room", **kw):
    fields = dict(
        node="laptop",
        resource="memory",
        asked=2 * GIB,
        free=GIB,
        capacity=48 * GIB,
        waits=True,
        until=None,
        holders=[],
    )
    fields.update(kw)
    return Short(cause, **fields)


# -- placement_for -------------------------------------------------------------


def test_placement_for_host_gives_the_laptop_plugin_through_the_group():
    """templet:3161 -- `placement_for("host")` gives `HostPlacement`, a
    `Placement`, registered in the `quarrylith.placement` group by quarrylith's
    own package metadata."""
    assert PLACEMENT_GROUP == "quarrylith.placement"
    assert placement_for("host") is HostPlacement
    assert issubclass(HostPlacement, Placement)
    assert HostPlacement.name == "host"
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    group = pyproject["project"]["entry-points"][PLACEMENT_GROUP]
    assert group == {"host": "quarrylith.allocator.host:HostPlacement"}


def test_placement_for_an_unknown_name_names_the_group_and_the_names():
    """templet:3161 -- an unknown name raises `LookupError`, naming the group
    and the names registered."""
    with pytest.raises(LookupError) as raised:
        placement_for("nowhere")
    text = str(raised.value)
    assert PLACEMENT_GROUP in text
    assert "nowhere" in text
    assert "host" in text


def test_placement_for_finds_a_plugin_registered_by_another_package(monkeypatch):
    """templet:3161 -- a placement that another package registers in the same
    group is found by its name, and listed in the error for a name that is
    not registered."""
    mine = importlib.metadata.EntryPoint(
        "mesh", "quarrylith.allocator.host:HostPlacement", PLACEMENT_GROUP
    )
    host = importlib.metadata.EntryPoint(
        "host", "quarrylith.allocator.host:HostPlacement", PLACEMENT_GROUP
    )
    asked = []

    def entry_points(**selection):
        asked.append(selection)
        return [host, mine, host]

    monkeypatch.setattr(importlib.metadata, "entry_points", entry_points)
    assert placement_for("mesh") is HostPlacement
    assert asked and all(s == {"group": PLACEMENT_GROUP} for s in asked)
    with pytest.raises(LookupError) as raised:
        placement_for("other")
    assert "host, mesh" in str(raised.value)


# -- the seam ------------------------------------------------------------------


def test_placement_is_an_abstract_class_with_four_methods():
    """templet:3161 -- `Placement` is abstract: `place`, `headroom`, `hold`
    and `release`, with the page's parameters; a class that fills all four is
    made from a record and keeps it."""
    assert Placement.__abstractmethods__ == {"place", "headroom", "hold", "release"}
    with pytest.raises(TypeError):
        Placement(object())

    def parameters(method):
        return list(inspect.signature(method).parameters.values())

    place = parameters(Placement.place)
    assert [p.name for p in place] == ["self", "task", "now"]
    assert place[2].kind is inspect.Parameter.KEYWORD_ONLY
    headroom = parameters(Placement.headroom)
    assert [p.name for p in headroom] == ["self", "now"]
    hold = {p.name: p for p in parameters(Placement.hold)}
    assert list(hold) == ["self", "holder", "needs", "task", "node", "measured", "now"]
    assert hold["task"].default is None and hold["node"].default is None
    assert hold["measured"].default is True
    assert all(
        hold[n].kind is inspect.Parameter.KEYWORD_ONLY
        for n in ("task", "node", "measured", "now")
    )
    release = {p.name: p for p in parameters(Placement.release)}
    assert list(release) == ["self", "holder", "node", "why"]
    assert release["node"].default is None

    class Mine(Placement):
        name = "mine"

        def place(self, task, *, now):
            return None

        def headroom(self, *, now):
            return {}

        def hold(self, holder, needs, *, task=None, node=None, measured=True, now):
            return []

        def release(self, holder, *, node=None, why):
            return []

    record = object()
    assert Mine(record).record is record


# -- the answers ---------------------------------------------------------------


def test_the_answers_are_frozen_with_the_pages_fields():
    """templet:3161 -- `Fit`, `Short` and `Refusal` are frozen dataclasses with
    the page's fields in its order (`Short`'s last, `note`, is optional), and
    `CAUSES` is the page's tuple."""
    assert CAUSES == (
        "room",
        "measured",
        "unmeasured",
        "budget",
        "undeclared",
        "unclassified",
        "driver",
        "node",
    )

    def names(cls):
        return [f.name for f in dataclasses.fields(cls)]

    assert names(Fit) == ["task", "node", "run_class", "needs", "sandbox", "why"]
    assert names(Short) == [
        "cause",
        "node",
        "resource",
        "asked",
        "free",
        "capacity",
        "waits",
        "until",
        "holders",
        "note",
    ]
    plain = Short("node", None, None, None, None, None, False, None, [])
    assert plain.note is None
    assert names(Refusal) == [
        "task",
        "run_class",
        "needs",
        "shorts",
        "waits",
        "until",
        "text",
    ]
    fit = Fit(4, "laptop", "agent", {"memory": 1}, "harness", "agent on laptop")
    with pytest.raises(dataclasses.FrozenInstanceError):
        fit.node = "other"
    with pytest.raises(dataclasses.FrozenInstanceError):
        short().waits = False
    with pytest.raises(dataclasses.FrozenInstanceError):
        refuse(1, "agent", {}, [short()], node="laptop").text = ""
    assert fit == Fit(4, "laptop", "agent", {"memory": 1}, "harness", "agent on laptop")


def test_a_short_has_a_cause_from_causes():
    """templet:3161 -- a `Short` of a cause outside `CAUSES` is refused."""
    for cause in CAUSES:
        assert short(cause).cause == cause
    with pytest.raises(ValueError):
        short("whim")


def test_a_refusal_reads_its_waits_and_until_from_its_shorts():
    """templet:3161 -- the Refusal's shorts come in CAUSES order and then by
    resource; `waits` is true only when every short waits; `until` is the
    latest of the shorts' `until`, and None unless every short has one."""
    a = short("budget", resource="budget.b", waits=True, until=1000)
    b = short("room", resource="disk")
    c = short("room", resource="memory")
    d = short("undeclared", resource="gpu", waits=False)
    e = short("budget", resource="budget.a", waits=True, until=3000)
    refusal = refuse(5, "agent", {"memory": 1}, [d, a, c, e, b], node="laptop")
    assert [(s.cause, s.resource) for s in refusal.shorts] == [
        ("room", "disk"),
        ("room", "memory"),
        ("budget", "budget.a"),
        ("budget", "budget.b"),
        ("undeclared", "gpu"),
    ]
    assert refusal.waits is False
    assert refusal.until is None  # the undeclared one has none

    waits = refuse(5, "agent", {}, [a, e], node="laptop")
    assert waits.waits is True
    assert waits.until == 3000
    assert refuse(5, "agent", {}, [a, e, b], node="laptop").until is None
    assert refuse(5, "agent", {}, [b], node="laptop").until is None
    assert refuse(5, "agent", {}, [b, c], node="laptop").waits is True
    assert refuse(5, "agent", {}, [b, d], node="laptop").waits is False


def test_a_refusal_with_no_shorts_is_refused():
    """templet:3161 -- a Refusal names its reason: with no short there is
    none."""
    with pytest.raises(ValueError, match="at least one short"):
        refuse(5, "agent", {}, [], node="laptop")


def test_the_refusal_text_is_one_line_in_the_form_of_the_disk_guard():
    """templet:3161 -- `text` is the page's one line: the task and class, the
    node, each short resource with what was asked, free and the capacity, the
    largest holders, and whether waiting helps."""
    holders = [
        ("podman-machine", round(15.4 * GIB)),
        ("desktop", 10 * GIB),
        ("run 9", 2 * GIB),
    ]
    over = short(asked=2 * GIB, free=round(1.3 * GIB), holders=holders)
    refusal = refuse(12, "agent", {"memory": 2 * GIB}, [over], node="laptop")
    assert refusal.text == (
        "allocator: task 12 (agent) refused on laptop: memory 2.0 GiB asked, "
        "1.3 GiB free of 48.0 GiB (podman-machine 15.4, desktop 10.0, run 9 2.0); "
        "waits"
    )


def test_the_refusal_text_says_never_until_and_the_slots_of_a_lane():
    """templet:3161 -- a refusal that cannot be helped by waiting ends
    `never`, one with a time ends `waits until <time>`, a resource counted in
    slots shows plain numbers, and a hold with no task says `hold`."""
    lane = short(
        resource="lane.widgets",
        asked=1,
        free=0,
        capacity=4,
        holders=[("a", 2), ("b", 2)],
    )
    text = refuse(3, "gate", {}, [lane], node="laptop").text
    assert text == (
        "allocator: task 3 (gate) refused on laptop: lane.widgets 1 asked, "
        "0 free of 4 (a 2, b 2); waits"
    )
    gone = short(
        "undeclared", resource="gpu", asked=1, free=None, capacity=None, waits=False
    )
    never = refuse(3, "gate", {}, [gone], node="laptop").text
    assert never.endswith("; never")
    assert "gpu" in never and "\n" not in never
    held = short(
        "budget", resource="budget.a", asked=1, free=0, capacity=4, until=1793728800000
    )
    until = refuse(3, "agent", {}, [held], node="laptop").text
    assert until.endswith("; waits until 2026-11-03T18:00Z")
    nothing = refuse(None, None, {"memory": 1}, [short()], node="laptop").text
    assert nothing.startswith("allocator: hold refused on laptop: ")


def test_the_refusal_text_is_one_line_with_every_short_in_it():
    """templet:3161 -- several shorts are one line, each named."""
    many = [
        short(resource="memory"),
        short("measured", resource="disk"),
        short(
            "unmeasured",
            resource=None,
            node="laptop",
            asked=None,
            free=None,
            capacity=None,
        ),
        short(
            "driver", resource=None, asked=None, free=None, capacity=None, waits=False
        ),
    ]
    text = refuse(8, "vm", {"memory": 1}, many, node="laptop").text
    assert "\n" not in text
    for word in ("memory", "disk", "unmeasured", "driver"):
        assert word in text
    assert text.endswith("; never")


def test_a_refusal_is_plain_data_for_a_note_or_a_log():
    """templet:3161 -- `as_data` gives plain data that survives JSON: the
    fields of the Refusal, its shorts as dicts with their holders as lists."""
    over = short(holders=[("desktop", 10 * GIB)])
    refusal = refuse(12, "agent", {"memory": 2 * GIB}, [over], node="laptop")
    data = refusal.as_data()
    assert json.loads(json.dumps(data)) == data
    assert set(data) == {
        "task",
        "run_class",
        "needs",
        "shorts",
        "waits",
        "until",
        "text",
    }
    assert data["task"] == 12 and data["run_class"] == "agent"
    assert data["needs"] == {"memory": 2 * GIB}
    assert data["shorts"][0]["cause"] == "room"
    assert data["shorts"][0]["holders"] == [["desktop", 10 * GIB]]
    assert data["text"] == refusal.text


def test_the_same_inputs_give_equal_refusals():
    """templet:3161 -- a Refusal is a value: the same inputs give equal
    answers, so the services can tell whether a task's refusal changed."""
    one = refuse(1, "agent", {"memory": 1}, [short()], node="laptop")
    two = refuse(1, "agent", {"memory": 1}, [short()], node="laptop")
    assert one == two
    assert one != refuse(2, "agent", {"memory": 1}, [short()], node="laptop")


def test_a_refusal_with_no_node_says_so_instead_of_naming_none():
    """templet:3161 -- a refusal for a record with no document names no node:
    the line says no allocation document is declared."""
    gone = Short("node", None, None, None, None, None, False, None, [])
    text = refuse(4, None, {}, [gone], node=None).text
    assert text == (
        "allocator: task 4 refused: node: no allocation document is declared; never"
    )
    assert "None" not in text


def test_a_line_is_built_for_any_time_a_short_names():
    """templet:3161 -- the line never raises for the time a short names: a
    time past year 9999, or before 1970, is printed as the number."""
    for until in (2**62, 253_402_300_800_000, -(2**62), 0):
        over = short(cause="budget", resource="budget.anthropic", until=until)
        refusal = refuse(1, "agent", {"budget.anthropic": 1}, [over], node="laptop")
        assert refusal.text.startswith("allocator: task 1")
