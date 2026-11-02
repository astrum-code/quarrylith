# SPDX-License-Identifier: Apache-2.0
"""The landing records, over every backend in BACKENDS (templet:3159, build
step 4): changes and their text, checks, reviews and their heads, merges,
questions and readings; `unsorted` as the record makes it; and what `check()`
finds of invariants 6 and 7."""

import pytest
from backends import URLS, admitted, over_backends, url_for
from samples import load_sample

from quarrylith.record import contract, schema
from quarrylith.record.contract import Refused, open_record
from quarrylith.record.events import Event, MemoryView, apply
from quarrylith.record.importing import compose


def item(rec, title="t", **kw):
    kw.setdefault("filed_by", "alpha")
    kw.setdefault("effort", "0001")
    kw.setdefault("role", "alpha")
    return rec.file_item("task", title, f"body of {title}", **kw)


def change(rec, number, branch=None, head="a1", repo="widgets", **kw):
    kw.setdefault("title", f"change of {number}")
    kw.setdefault("body", f"body of the change of {number}")
    kw.setdefault("by", "alpha")
    return rec.open_or_update_change(
        repo, branch or f"feat/{number}", head, number=number, **kw
    )


def unchanged(rec):
    """What a refused operation must leave as it was."""
    return rec.head(), rec.dump()


def landing(rec, clock):
    """A run of mixed operations over every kind step 4 writes."""
    admitted(rec, "0001")
    a, b = item(rec, "a"), item(rec, "b")
    c1 = change(rec, a, relation="closes", url="https://git.example/w/pull/1")
    clock.step()
    change(rec, a, head="a2", draft=True)
    c2 = change(rec, b, repo="gadgets", relation="part", position=1)
    rec.record_check(c1, "a2", "test", "pending")
    clock.step()
    rec.record_check(c1, "a2", "test", "green", "12 passed")
    rec.record_check(c1, "a2", "docs", "skipped")
    review = rec.file_review(
        a, 1, "pool", item=b, author="alpha", heads={str(c1): "a2"}, filed_by="beta"
    )
    rec.record_verdict(review, "gamma", "approve", [{"note": "fine"}])
    rec.review_read(review)
    merge = rec.record_merge(c1, "a2", "user", "reviewed", commit="m1")
    q = rec.ask(a, "merge after b?", kind="pr")
    answer = rec.comment(a, "yes", who="user", mark="answer")
    rec.answer(q, answer)
    rec.drop_change(c2, "beta", "not needed")
    rec.record_reading("budget", "five_hour", {"utilization": 0.25})
    return a, b, c1, c2, review, merge, q


# -- the acceptance items -----------------------------------------------------


@over_backends
def test_a_second_open_change_on_a_branch_and_a_merge_off_its_head_are_refused(
    record, clock
):
    """templet:3159 -- a second open change on one branch is refused, and
    `record_merge` of a sha that is not the change's head is refused; each
    writes nothing."""
    admitted(record)
    a, b = item(record, "a"), item(record, "b")
    c = change(record, a, branch="feat/shared", head="h1")
    before = unchanged(record)
    with pytest.raises(Refused, match="open change"):
        change(record, b, branch="feat/shared", head="h9")
    assert unchanged(record) == before
    # the same item's change on the branch is the one change, updated
    clock.step()
    assert change(record, a, branch="feat/shared", head="h2") == c
    assert record.get("changes", c)["head"] == "h2"
    # another repo's branch of the same name is another change
    other = change(record, b, branch="feat/shared", repo="gadgets")
    assert other != c

    before = unchanged(record)
    for sha in ("h1", "h3", ""):
        with pytest.raises(Refused, match="head"):
            record.record_merge(c, sha, "user", "reviewed")
        assert unchanged(record) == before
    merge = record.record_merge(c, "h2", "user", "reviewed", commit="m1")
    got = record.get("changes", c)
    assert got["merged_at"] == clock()
    assert record.get("merges", merge) == {
        "id": merge,
        "change": c,
        "sha": "h2",
        "commit": "m1",
        "by": "user",
        "rule": "reviewed",
        "at": clock(),
    }
    # the merge and merged_at are one event
    (event,) = record.events(record.head() - 1)
    assert event.kind == "merge"
    assert {k for k, _, _ in event.puts} >= {"changes", "merges"}
    # a merged change is not open: no second merge, and the branch is free
    before = unchanged(record)
    with pytest.raises(Refused, match="not open"):
        record.record_merge(c, "h2", "user", "reviewed")
    assert unchanged(record) == before
    again = change(record, b, branch="feat/shared", head="h4")
    assert again not in (c, other)
    assert record.check() == []


@over_backends
def test_record_verdict_once_and_no_operation_edits_a_reviews_heads(record, clock):
    """templet:3159 -- `record_verdict` on a review that has one is refused,
    and no operation edits a review's `heads`: every put of a review in the
    journal carries the heads it was filed with."""
    a, b, c1, c2, review, _, _ = landing(record, clock)
    before = unchanged(record)
    for verdict in ("approve", "changes"):
        with pytest.raises(Refused, match="verdict"):
            record.record_verdict(review, "delta", verdict, [])
        assert unchanged(record) == before
    other = record.file_review(
        b, 1, "second", author="beta", heads={str(c2): "a1"}, filed_by="user"
    )
    record.record_verdict(other, "delta", "changes", [{"must": "fix"}])
    assert record.review_read(other) is True
    assert record.review_read(other) is False
    filed = {}
    puts = 0
    for event in record.events(0, limit=10_000):
        for kind, key, value in event.puts:
            if kind == "reviews":
                puts += 1
                filed.setdefault(key[0], value["heads"])
                assert value["heads"] == filed[key[0]], (event.seq, event.kind)
    assert puts >= 6
    assert filed == {review: {str(c1): "a2"}, other: {str(c2): "a1"}}
    # nothing takes heads to edit, and a review's heads name its changes
    with pytest.raises(TypeError):
        record.record_verdict(review, "delta", "approve", [], heads={})
    for bad in ({"99": "x"}, {"one": "x"}, {str(c1): 3}, [str(c1)]):
        before = unchanged(record)
        with pytest.raises(Refused):
            record.file_review(a, 2, "pool", author="alpha", heads=bad)
        assert unchanged(record) == before


# -- the rest of step 4 -------------------------------------------------------


@over_backends
def test_changes_are_opened_updated_and_dropped(record, clock):
    """A change is opened with its text and its repo's next `n`, updated on its
    branch (`pushed_at` moves with its head), and dropped; an update that
    changes nothing writes nothing."""
    admitted(record)
    a = item(record, "a")
    c = change(record, a, head="h1", url="https://git.example/w/pull/1")
    got = record.get("changes", c, text=True)
    assert got["n"] == 1 and got["repo"] == "widgets" and got["number"] == a
    assert got["opened_at"] == got["pushed_at"] == clock()
    assert got["opened_by"] == "alpha" and got["relation"] == "part"
    assert got["body"] == f"body of the change of {a}"
    assert record.get("meta", ["next:change:widgets"])["value"] == 2
    head = record.head()
    assert change(record, a, head="h1", url="https://git.example/w/pull/1") == c
    assert record.head() == head
    opened = clock()
    clock.step()
    change(record, a, head="h1", title="better", url="https://git.example/w/pull/1")
    got = record.get("changes", c)
    assert got["title"] == "better" and got["pushed_at"] == opened
    assert record.events(head)[0].kind == "change.edit"
    clock.step()
    change(record, a, head="h2", body="new body", by="beta")
    got = record.get("changes", c, text=True)
    assert got["head"] == "h2" and got["pushed_at"] == clock()
    assert got["opened_by"] == "alpha" and got["body"] == "new body"
    # each repo counts its own `n`
    assert record.get("changes", change(record, a, repo="gadgets"))["n"] == 1
    assert record.get("changes", change(record, a, branch="feat/x"))["n"] == 2
    for bad in (
        lambda: change(record, a, branch="feat/bad", relation="both"),
        lambda: change(record, a, branch="feat/bad", draft="yes"),
        lambda: change(record, 999, branch="feat/bad"),
        lambda: record.open_or_update_change("widgets", "feat/new", "h", by="a"),
    ):
        before = unchanged(record)
        with pytest.raises(Refused):
            bad()
        assert unchanged(record) == before
    record.drop_change(c, "beta", "superseded")
    got = record.get("changes", c)
    assert got["dropped_at"] == clock() and got["dropped_by"] == "beta"
    for refused in (
        lambda: record.drop_change(c, "beta", "again"),
        lambda: record.record_merge(c, "h2", "user", "reviewed"),
        lambda: record.drop_change(999, "beta", "none"),
    ):
        before = unchanged(record)
        with pytest.raises(Refused):
            refused()
        assert unchanged(record) == before
    assert record.read("changes", repo="widgets", live=True) == [
        record.get("changes", 3)
    ]
    assert record.check() == []


@over_backends
def test_checks_are_kept_per_change_head_and_name(record, clock):
    """A check is recorded per (change, head, name), by the actor; recording
    the same state and detail again writes nothing."""
    admitted(record)
    c = change(record, item(record))
    assert record.record_check(c, "a1", "test", "pending") is True
    clock.step()
    assert record.record_check(c, "a1", "test", "green", "9 passed") is True
    head = record.head()
    assert record.record_check(c, "a1", "test", "green", "9 passed") is False
    assert record.head() == head
    record.record_check(c, "a0", "test", "red")
    assert [(r["head"], r["name"], r["state"]) for r in record.read("checks")] == [
        ("a0", "test", "red"),
        ("a1", "test", "green"),
    ]
    assert record.get("checks", [c, "a1", "test"]) == {
        "change": c,
        "head": "a1",
        "name": "test",
        "state": "green",
        "detail": "9 passed",
        "at": clock(),
        "by": "alpha",
    }
    for bad in (
        lambda: record.record_check(c, "a1", "test", "amber"),
        lambda: record.record_check(999, "a1", "test", "green"),
    ):
        before = unchanged(record)
        with pytest.raises(Refused):
            bad()
        assert unchanged(record) == before


@over_backends
def test_questions_are_asked_and_answered_once(record, clock):
    """A question is asked on an item and answered by a comment on it, once."""
    admitted(record)
    a, b = item(record, "a"), item(record, "b")
    q = record.ask(a, "which list?", kind="blocked")
    assert record.read("questions", live=True, number=a)[0]["id"] == q
    wrong = record.comment(b, "elsewhere")
    reply = record.comment(a, "the sorted one", who="user", mark="answer")
    for bad in (
        lambda: record.answer(q, wrong),
        lambda: record.answer(q, 999),
        lambda: record.ask(a, "?", kind="maybe"),
        lambda: record.ask(999, "?", kind="pr"),
        lambda: record.ask(a, "?", kind="pr", run=999),
    ):
        before = unchanged(record)
        with pytest.raises(Refused):
            bad()
        assert unchanged(record) == before
    clock.step()
    record.answer(q, reply)
    got = record.get("questions", q)
    assert (got["answered_at"], got["answered_by"], got["answer"]) == (
        clock(),
        "user",
        reply,
    )
    assert record.read("questions", live=True, number=a) == []
    before = unchanged(record)
    with pytest.raises(Refused, match="answered"):
        record.answer(q, reply)
    assert unchanged(record) == before


@over_backends
def test_readings_are_recorded(record, clock):
    """A reading is a source's value of a kind, at the record's clock."""
    r = record.record_reading("budget", "five_hour", {"utilization": 0.5})
    assert record.get("readings", r) == {
        "id": r,
        "source": "budget",
        "kind": "five_hour",
        "value": {"utilization": 0.5},
        "at": clock(),
    }
    assert record.read("readings", source="budget", kind="five_hour")[0]["id"] == r
    before = unchanged(record)
    with pytest.raises(Refused):
        record.record_reading("budget", "five_hour", float("nan"))
    assert unchanged(record) == before


@over_backends
def test_the_replay_holds_over_the_landing_records_and_a_dump_loads(
    record, clock, tmp_path
):
    """templet:3159 -- after a run of step 4's operations, the replay of the
    journal equals the records, and a dump loads into every backend and dumps
    the same."""
    landing(record, clock)
    dump = record.dump()
    view = MemoryView()
    for raw in dump["journal"]:
        apply(view, Event.from_dict(raw))
    assert view.records() == dump["records"]
    for kind in (
        "changes",
        "change_text",
        "checks",
        "reviews",
        "merges",
        "questions",
        "readings",
    ):
        assert dump["records"][kind], kind
    kinds = {e["kind"] for e in dump["journal"]}
    assert kinds >= {
        "change.open",
        "change.edit",
        "change.drop",
        "check.record",
        "review.file",
        "review.verdict",
        "review.read",
        "merge",
        "question.ask",
        "question.answer",
        "reading",
    }
    assert record.check() == []
    for name in sorted(contract.BACKENDS):
        if name not in URLS:
            continue
        target = open_record(
            url_for(name, tmp_path / f"into-{name}"), actor="loader", via="load"
        )
        try:
            target.init()
            target.load(dump)
            assert target.dump() == dump
            assert target.check() == []
        finally:
            target.close()


@over_backends
def test_unsorted_is_the_records_own_whoever_files_first(opener, clock):
    """The `unsorted` effort `file_item` makes is `schema.unsorted_effort`,
    the same whoever files first: its owner is not the filer's."""
    made = []
    for actor in ("alpha", "beta"):
        rec = opener(actor=actor)
        if not rec.initialised:
            rec.init()
        made.append(rec)
    first = made[1].file_item("task", "loose", "", filed_by="beta")
    got = made[1].get("efforts", "unsorted")
    assert got == schema.unsorted_effort(clock())
    assert got["owner"] == "record" and got["standing"] == "parked"
    clock.step()
    made[0].file_item("task", "loose too", "", filed_by="alpha")
    assert made[0].get("efforts", "unsorted") == got
    assert made[0].read("effort_links", target=str(first))[0]["effort"] == "unsorted"


def _with_event(dump, kind, puts):
    """`dump` with one more event, its puts folded in: a dump made by hand."""
    last = dump["journal"][-1]
    event = Event(
        seq=last["seq"] + 1,
        at=last["at"],
        actor="hand",
        via="load",
        kind=kind,
        subject=None,
        data={},
        puts=tuple((k, schema.key_of(k, v), v) for k, v in puts),
    )
    view = MemoryView()
    for raw in dump["journal"]:
        apply(view, Event.from_dict(raw))
    apply(view, event)
    return {
        **dump,
        "journal": dump["journal"] + [event.to_dict()],
        "records": view.records(),
    }


def _check_loaded(backend, folder, dump):
    """Load `dump` into a fresh record in `folder`: it loads, and gives the
    findings of its `check()`."""
    rec = open_record(url_for(backend, folder), actor="loader", via="load")
    try:
        rec.init()
        rec.load(dump)
        return rec.check()
    finally:
        rec.close()


def _started(record):
    """A node of one slot, and a run on it."""
    admitted(record)
    record.declare_node("n1", ["process"], [])
    record.set_capacity("n1", "slots", 1, "slots")
    task = record.file_task("job", effort="0001", needs={"slots": 1}, sandbox="process")
    run = record.start_run(
        task, "alpha", node="n1", sandbox="process", fence=None, expect_s=None
    )
    return task, run


@over_backends
def test_check_finds_a_node_booked_past_its_capacity(backend, record, tmp_path):
    """Invariant 6 in `check()`: a dump made by hand whose reservations pass a
    node's capacity loads (its records are the fold of its journal), and
    `check()` names the node and the resource."""
    _started(record)
    dump = record.dump()
    (held,) = dump["records"]["reservations"]
    extra = {**held, "id": held["id"] + 1, "task": None, "run": None, "holder": "cache"}
    meta = {"key": "next:reservations", "value": held["id"] + 2}
    bad = _with_event(dump, "reserve", [("reservations", extra), ("meta", meta)])
    findings = _check_loaded(backend, tmp_path / "hand", bad)
    assert len(findings) == 1, findings
    assert "n1" in findings[0] and "slots" in findings[0]
    assert "2" in findings[0] and "capacity 1" in findings[0]


@over_backends
def test_check_finds_a_running_run_with_no_live_reservation(backend, record, tmp_path):
    """Invariant 7 in `check()`: a dump made by hand that releases a running
    run's reservation loads, and `check()` names the run; so does one that
    keeps a finished run's reservation live."""
    _, run = _started(record)
    dump = record.dump()
    (held,) = dump["records"]["reservations"]
    released = {**held, "released_at": dump["journal"][-1]["at"], "why": "by hand"}
    bad = _with_event(dump, "reserve.release", [("reservations", released)])
    findings = _check_loaded(backend, tmp_path / "hand-released", bad)
    assert len(findings) == 1, findings
    assert f"run {run}" in findings[0] and "slots" in findings[0]
    # a finished run whose reservation stays live
    record.finish_run(run, "done", "ok", footprint={}, cost={})
    dump = record.dump()
    finished = [r for r in dump["records"]["reservations"] if r["run"] == run][0]
    kept = {**finished, "released_at": None, "why": None}
    bad = _with_event(dump, "reserve", [("reservations", kept)])
    findings = _check_loaded(backend, tmp_path / "hand-kept", bad)
    assert len(findings) == 1, findings
    assert f"run {run}" in findings[0] and "finished" in findings[0]


@over_backends
def test_check_takes_imported_runs_as_holding_nothing(record):
    """Runs an import brings sit on the retired node `legacy` and hold no
    reservation (invariant 7's exception): a live one is no finding."""
    doc = load_sample()
    assert [r for r in doc["records"]["runs"] if r["finished_at"] is None]
    record.load(compose(doc))
    assert record.check() == []


def test_the_unsorted_effort_is_declared_once():
    """`unsorted_effort` is a valid effort, parked, and the page's fields."""
    got = schema.unsorted_effort(5)
    assert got == schema.validate("efforts", got)
    assert (got["id"], got["slug"], got["title"], got["standing"]) == (
        "unsorted",
        "unsorted",
        "Unsorted",
        "parked",
    )
    assert got["owner"] == "record" and got["created_at"] == 5
    assert got["statement"] == "Items filed with no effort"
    assert contract.UNSORTED == schema.UNSORTED == "unsorted"
