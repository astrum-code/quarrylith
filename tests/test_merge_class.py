# SPDX-License-Identifier: Apache-2.0
"""The merge classes (templet:3163, build step 6), over every backend in
BACKENDS: `classify` and `MergeClass`, and `quarrylith order merge`.

Every fixture starts from one change that qualifies (`ready_to_merge`) and
each test spoils exactly one fact of it, so a rule that is missing shows as a
test that fails, never as a fixture that happened to pass. Times are plain
integers; nothing here reads a clock."""

import json

import pytest
from backends import admitted, over_backends

from quarrylith import cli
from quarrylith.order.merge import MergeClass, classify
from quarrylith.order.settings import DEFAULT, Settings
from quarrylith.record import contract


class Merge:
    """What `ready_to_merge` filed: the item, its change and head, and its
    pool review."""

    def __init__(self, rec, clock, number, change, head, review):
        self.rec = rec
        self.clock = clock
        self.n = number
        self.change = change
        self.head = head
        self.review = review

    def add_review(self, kind, verdict="approve", heads=None):
        """A review of the item's current round, of `kind`, judging this
        change's head unless `heads` says otherwise; with a `verdict` it is
        given, with None it is still live."""
        round_ = self.rec.get("items", self.n)["review_round"]
        heads = {str(self.change): self.head} if heads is None else heads
        id = self.rec.file_review(
            self.n, round_, kind, author="alpha", heads=heads, filed_by="beta"
        )
        if verdict is not None:
            self.rec.record_verdict(id, "gamma", verdict, [])
        return id


def item(rec, title="t", **kw):
    kw.setdefault("filed_by", "alpha")
    kw.setdefault("effort", "0001")
    kw.setdefault("role", "alpha")
    return rec.file_item("task", title, f"body of {title}", **kw)


def ready_to_merge(rec, clock, *, risk=4, tier=None, head="a1", also=None, both=True):
    """An admitted effort and one item at `risk` with a change at `head`,
    green on its one check, and a pool review of review round 1 that
    approved that head: the change `pool-approved` would merge. With `also`
    (a repo) the item has a second change there, green too, which the pool
    review judges unless `both` is false."""
    admitted(rec)
    n = item(rec, "work", risk=risk, tier=tier)
    rec.edit_item(n, review_round=1)
    change = rec.open_or_update_change(
        "widgets",
        f"feat/{n}",
        head,
        number=n,
        title="work",
        body="the work",
        relation="closes",
        by="alpha",
    )
    rec.record_check(change, head, "test", "green")
    merge = Merge(rec, clock, n, change, head, None)
    heads = {str(change): head}
    if also is not None:
        merge.second = rec.open_or_update_change(
            also, f"feat/{n}", "b1", number=n, title="more", body="b", by="alpha"
        )
        rec.record_check(merge.second, "b1", "test", "green")
        if both:
            heads[str(merge.second)] = "b1"
    merge.review = merge.add_review("pool", heads=heads)
    return merge


def judged(rec, clock, **kw):
    return classify(rec, ready_to_merge(rec, clock, **kw).n, settings=DEFAULT)


def has(got, text):
    return any(text in line for line in got.why)


class Reads:
    """The record as the order may see it: the contract's reads and nothing
    else (no write, no `head`)."""

    def __init__(self, rec):
        self._rec = rec

    def get(self, kind, key, **kw):
        return self._rec.get(kind, key, **kw)

    def read(self, kind, /, **kw):
        return self._rec.read(kind, **kw)

    def reading(self):
        return self._rec.reading()


# -- the class ------------------------------------------------------------


@over_backends
def test_a_pool_approval_of_the_head_with_green_checks_is_auto(backend, record, clock):
    """templet:3163 -- a pool approval of the change's head in the current
    round, checks green, risk 4, gives `auto` with the rule
    `pool-approved` and no reason."""
    got = judged(record, clock)
    assert got == MergeClass("auto", "pool-approved", ())
    assert got.cls == "auto" and got.rule == "pool-approved" and got.why == ()


@over_backends
def test_a_skipped_check_beside_a_green_one_is_auto(backend, record, clock):
    """templet:3163 -- every check reads `green` or `skipped`, and one at
    least reads `green`."""
    m = ready_to_merge(record, clock)
    record.record_check(m.change, m.head, "docs", "skipped")
    assert classify(record, m.n, settings=DEFAULT).cls == "auto"


def _risk(value):
    return lambda m: m.rec.edit_item(m.n, risk=value)


def _move_head(m):
    """The change moves to a new head, with a green check on it, so the one
    fact that is wrong is the review's head."""
    m.rec.open_or_update_change("widgets", f"feat/{m.n}", "a2", number=m.n, by="alpha")
    m.rec.record_check(m.change, "a2", "test", "green")


def _check(state, name="lint"):
    return lambda m: m.rec.record_check(m.change, m.head, name, state)


def _hold(m):
    m.rec.hold(number=m.n, cause="manual", why="wait")


def _question(m):
    m.rec.ask(m.n, "merge after the other?", kind="pr")


def _overruled(m):
    m.rec.edit_item(m.n, overruled_at=m.clock())


def _draft(m):
    m.rec.open_or_update_change(
        "widgets", f"feat/{m.n}", m.head, number=m.n, draft=True, by="alpha"
    )


#: Each spoils one fact of a change that would be `auto`, and the text its
#: reason must hold.
USER_CASES = {
    "risk 5 under a rule allowing 4": (_risk(5), "risk 5"),
    "no risk": (_risk(None), "no risk"),
    "a changes verdict in the round": (
        lambda m: m.add_review("second", "changes"),
        "asked for changes",
    ),
    "an approval of an older head": (
        _move_head,
        "head",
    ),
    "a red check": (_check("red"), "check lint is red"),
    "a pending check": (_check("pending"), "check lint is pending"),
    "a draft": (_draft, "draft"),
    "overruled": (_overruled, "overruled"),
    "an open question": (_question, "question"),
    "a templet review": (
        lambda m: m.add_review("templet"),
        "templet",
    ),
    "an item whose templet names itself": (
        lambda m: m.rec.edit_item(m.n, templet=m.n),
        "templet",
    ),
    "a live hold": (_hold, "hold"),
    "a conformance review still live": (
        lambda m: m.add_review("conformance", None),
        "no verdict",
    ),
    "a second role's review of an older head": (
        lambda m: m.add_review("second", heads={str(m.change): "a0"}),
        "head",
    ),
    "a tier-5 item with only the pool's approval": (
        lambda m: m.rec.edit_item(m.n, tier=5),
        "second",
    ),
    "an item building to another item's templet, with only the pool's": (
        lambda m: m.rec.edit_item(m.n, templet=item(m.rec, "spec")),
        "conformance",
    ),
    "a review that did not judge this change": (
        lambda m: m.add_review("second", heads={}),
        "did not judge change",
    ),
}


@over_backends
@pytest.mark.parametrize("name", sorted(USER_CASES))
def test_a_spoiled_fact_gives_the_user_with_its_reason(backend, record, clock, name):
    """templet:3163 -- risk 5, no risk, a `changes` verdict, an approval of an
    older head, a red or pending check, a draft, `overruled_at`, an open
    question, a `templet` review, an item whose `templet` names itself, a
    live hold, a conformance review still live, a second role's review of an
    older head, a tier-5 item with only the pool's approval and a build with
    only the pool's approval each give `user` with that reason among
    `why`."""
    spoil, reason = USER_CASES[name]
    m = ready_to_merge(record, clock)
    spoil(m)
    got = classify(record, m.n, settings=DEFAULT)
    assert got.cls == "user" and got.rule is None, got
    assert has(got, reason), got.why


@over_backends
def test_risk_8_is_the_users_under_a_rule_allowing_9(backend, record, clock):
    """templet:3163 -- a risk of 8 or more is never auto, whatever the rules
    say."""
    m = ready_to_merge(record, clock, risk=8)
    loose = Settings(
        merge_rules=({"name": "loose", "risk_at_most": 9, "reviews": ["pool"]},)
    )
    got = classify(record, m.n, settings=loose)
    assert got.cls == "user" and has(got, "risk 8")
    for risk in (9, 10):
        record.edit_item(m.n, risk=risk)
        assert classify(record, m.n, settings=loose).cls == "user"
    record.edit_item(m.n, risk=7)
    assert classify(record, m.n, settings=loose).cls == "auto"


@over_backends
def test_a_templet_is_told_by_the_item_before_its_review_is_filed(
    backend, record, clock
):
    """templet:3163 -- an item whose `templet` names itself is the user's
    with no review of kind `templet` filed yet, and not because of its files;
    once the owner's review is filed it is the user's still."""
    m = ready_to_merge(record, clock)
    record.edit_item(m.n, templet=m.n)
    got = classify(record, m.n, settings=DEFAULT)
    assert got.cls == "user" and has(got, "templet")
    assert not [r for r in record.read("reviews", target=m.n) if r["kind"] == "templet"]
    m.add_review("templet")
    assert classify(record, m.n, settings=DEFAULT).cls == "user"


@over_backends
def test_a_templet_review_of_an_earlier_round_still_marks_a_templet(
    backend, record, clock
):
    """templet:3163 -- an item that has a review of kind `templet` is a
    templet, in whatever round the review was filed."""
    m = ready_to_merge(record, clock)
    old = record.file_review(m.n, 0, "templet", author="alpha", filed_by="beta")
    record.record_verdict(old, "gamma", "approve", [])
    got = classify(record, m.n, settings=DEFAULT)
    assert got.cls == "user" and has(got, "templet")


@over_backends
def test_a_build_with_its_conformance_review_approved_is_auto(backend, record, clock):
    """templet:3163 -- an item that builds to another item's templet is auto
    when the conformance review of the round is filed and approved too; a
    tier-5 item likewise with the second role's."""
    m = ready_to_merge(record, clock)
    record.edit_item(m.n, templet=item(record, "spec"))
    assert classify(record, m.n, settings=DEFAULT).cls == "user"
    m.add_review("conformance")
    assert classify(record, m.n, settings=DEFAULT).cls == "auto"
    record.edit_item(m.n, tier=5)
    assert classify(record, m.n, settings=DEFAULT).cls == "user"
    m.add_review("second")
    assert classify(record, m.n, settings=DEFAULT).rule == "pool-approved"


@over_backends
def test_only_the_current_round_counts(backend, record, clock):
    """templet:3163 -- the reviews of the item's current round decide: a
    `changes` verdict or a live review of an earlier round does not hold the
    change, and an approval of an earlier round does not approve it."""
    m = ready_to_merge(record, clock)
    old = record.file_review(
        m.n, 0, "second", author="alpha", heads={str(m.change): "a0"}
    )
    live = record.file_review(m.n, 0, "conformance", author="alpha", filed_by="b")
    stale = record.file_review(m.n, 0, "pool", author="alpha", filed_by="b")
    record.record_verdict(old, "gamma", "changes", [])
    assert live and stale
    assert classify(record, m.n, settings=DEFAULT).cls == "auto"
    record.edit_item(m.n, review_round=2)
    got = classify(record, m.n, settings=DEFAULT)
    assert got.cls == "user" and has(got, "pool")


@over_backends
def test_every_review_of_the_round_must_approve_not_only_the_named_kinds(
    backend, record, clock
):
    """templet:3163 -- a review of a kind the rule does not name, in the
    round, holds the change as `status:reviewed` does today."""
    m = ready_to_merge(record, clock)
    m.add_review("user", "changes")
    got = classify(record, m.n, settings=DEFAULT)
    assert got.cls == "user" and has(got, "(user)")


@over_backends
def test_the_rule_names_a_kind_that_must_be_among_the_approvals(backend, record, clock):
    """templet:3163 -- for each review kind the rule names, one of that kind
    is among the approvals: no pool review, only a second role's, is the
    user's; a rule naming two kinds needs both."""
    admitted(record)
    n = item(record, risk=4)
    c = record.open_or_update_change(
        "widgets", "feat/x", "a1", number=n, title="t", body="b", by="alpha"
    )
    record.record_check(c, "a1", "test", "green")
    record.file_review(n, 0, "second", author="alpha", heads={str(c): "a1"})
    record.record_verdict(1, "gamma", "approve", [])
    assert [r["kind"] for r in record.read("reviews", target=n)] == ["second"]
    got = classify(record, n, settings=DEFAULT)
    assert got.cls == "user" and has(got, "no pool review")
    both = Settings(
        merge_rules=(
            {"name": "both", "risk_at_most": 4, "reviews": ["pool", "second"]},
        )
    )
    assert classify(record, n, settings=both).cls == "user"
    pool = record.file_review(n, 0, "pool", author="alpha", heads={str(c): "a1"})
    record.record_verdict(pool, "gamma", "approve", [])
    assert classify(record, n, settings=both) == MergeClass("auto", "both", ())


@over_backends
def test_without_a_green_check_it_is_the_users(backend, record, clock):
    """templet:3163 -- every check reads `green` or `skipped`, and at least
    one reads `green`: none, or only skipped, is the user's."""
    admitted(record)
    n = item(record, risk=1)
    c = record.open_or_update_change(
        "widgets", "feat/n", "a1", number=n, title="t", body="b", by="alpha"
    )
    review = record.file_review(n, 0, "pool", author="a", heads={str(c): "a1"})
    record.record_verdict(review, "gamma", "approve", [])
    got = classify(record, n, settings=DEFAULT)
    assert got.cls == "user" and has(got, "no check")
    record.record_check(c, "a1", "docs", "skipped")
    got = classify(record, n, settings=DEFAULT)
    assert got.cls == "user" and has(got, "no green check")
    record.record_check(c, "a1", "test", "green")
    assert classify(record, n, settings=DEFAULT).cls == "auto"


@over_backends
def test_a_check_of_an_older_head_does_not_count(backend, record, clock):
    """templet:3163 -- the checks that count are those on `(change, head)`:
    a red check of an earlier head is no bar, and a green one of an earlier
    head is no pass."""
    m = ready_to_merge(record, clock)
    record.record_check(m.change, "old", "test", "red")
    assert classify(record, m.n, settings=DEFAULT).cls == "auto"
    _move_head(m)
    # the review still judges a1; the new head has its own green check, so
    # the one fault is the review's head
    got = classify(record, m.n, settings=DEFAULT)
    assert got.cls == "user" and not has(got, "check")


@over_backends
def test_a_set_of_changes_is_auto_only_whole(backend, record, clock):
    """templet:3163 -- an item's open changes are classified as one set: auto
    only when every one qualifies, so a set with one member not qualifying is
    the user's whole."""
    m = ready_to_merge(record, clock, also="gadgets")
    assert classify(record, m.n, settings=DEFAULT).cls == "auto"
    record.record_check(m.second, "b1", "lint", "red")
    got = classify(record, m.n, settings=DEFAULT)
    assert got.cls == "user" and has(got, f"change {m.second}")
    assert not has(got, f"change {m.change} ")


@over_backends
def test_a_review_that_judges_only_part_of_the_set_does_not_approve_it(
    backend, record, clock
):
    """templet:3163 -- the approval binds to the heads the review judged: a
    change it did not judge is not approved by it."""
    m = ready_to_merge(record, clock, also="gadgets", both=False)
    got = classify(record, m.n, settings=DEFAULT)
    assert got.cls == "user"
    assert has(got, f"did not judge change {m.second}")


@over_backends
def test_a_merged_or_dropped_change_is_not_in_the_set(backend, record, clock):
    """templet:3163 -- only the item's open changes are classified."""
    m = ready_to_merge(record, clock, also="gadgets", both=False)
    record.drop_change(m.second, "beta", "not needed")
    assert classify(record, m.n, settings=DEFAULT).cls == "auto"


@over_backends
def test_why_lists_every_reason_not_the_first(backend, record, clock):
    """templet:3163 -- every reason that applies is in `why`."""
    m = ready_to_merge(record, clock, risk=9)
    _draft(m)
    _hold(m)
    _question(m)
    _overruled(m)
    m.add_review("second", "changes")
    got = classify(record, m.n, settings=DEFAULT)
    assert got.cls == "user" and got.rule is None
    for text in ("risk 9", "draft", "hold", "question", "overruled", "changes"):
        assert has(got, text), (text, got.why)
    assert len(set(got.why)) == len(got.why) >= 6


@over_backends
def test_no_merge_rules_makes_everything_the_users(backend, record, clock):
    """templet:3163 -- empty `merge_rules` gives `user` for all (the
    rollback: `auto_merge_risk = 0`)."""
    m = ready_to_merge(record, clock)
    got = classify(record, m.n, settings=Settings(merge_rules=()))
    assert got.cls == "user" and got.rule is None
    assert has(got, "no merge rule")


@over_backends
def test_the_first_rule_that_qualifies_names_the_class(backend, record, clock):
    """templet:3163 -- the class's `rule` is the name of the first rule the
    change qualifies under; one it does not qualify under is passed."""
    m = ready_to_merge(record, clock, risk=4)
    rules = (
        {"name": "tiny", "risk_at_most": 2, "reviews": ["pool"]},
        {"name": "needs-user", "risk_at_most": 4, "reviews": ["user"]},
        {"name": "pool-approved", "risk_at_most": 4, "reviews": ["pool"]},
        {"name": "also", "risk_at_most": 10, "reviews": ["pool"]},
    )
    got = classify(record, m.n, settings=Settings(merge_rules=rules))
    assert got == MergeClass("auto", "pool-approved", ())
    got = classify(record, m.n, settings=Settings(merge_rules=rules[:2]))
    assert got.cls == "user"
    assert has(got, "tiny") and has(got, "needs-user")


# -- cases the page leaves to the build ----------------------------------


@over_backends
def test_an_item_with_no_open_change_is_the_users(backend, record, clock):
    """templet:3163 -- there is nothing to merge: not `auto` (an empty set is
    not whole), and the reason says so."""
    admitted(record)
    n = item(record, risk=1)
    got = classify(record, n, settings=DEFAULT)
    assert got.cls == "user" and has(got, "no open change")


@over_backends
def test_a_round_with_no_review_is_the_users(backend, record, clock):
    """templet:3163 -- a rule that names no review kind still needs the
    round to have been reviewed."""
    admitted(record)
    n = item(record, risk=1)
    c = record.open_or_update_change(
        "widgets", "feat/n", "a1", number=n, title="t", body="b", by="alpha"
    )
    record.record_check(c, "a1", "test", "green")
    open_rule = Settings(
        merge_rules=({"name": "open", "risk_at_most": 4, "reviews": []},)
    )
    got = classify(record, n, settings=open_rule)
    assert got.cls == "user" and has(got, "no review")


@over_backends
def test_an_item_that_is_not_there_is_refused(backend, record, clock):
    """templet:3163 -- a number the record does not hold is refused, not
    classified."""
    admitted(record)
    with pytest.raises(contract.Refused, match="no item 99"):
        classify(record, 99, settings=DEFAULT)


# -- pure policy ----------------------------------------------------------


@over_backends
def test_classify_refuses_settings_with_problems(backend, record, clock):
    """templet:3163 -- `classify` refuses a `Settings` with problems
    (`ValueError`), naming each."""
    m = ready_to_merge(record, clock)
    bad = Settings(merge_rules=({"risk_at_most": 11, "reviews": ["nobody"]},))
    with pytest.raises(ValueError, match="merge_rules"):
        classify(record, m.n, settings=bad)
    with pytest.raises(ValueError, match="review_slots"):
        classify(record, m.n, settings=Settings(review_slots=-1))


@over_backends
def test_classify_writes_nothing_and_reads_only_the_contract(backend, record, clock):
    """templet:3163 -- the journal's head stays where it was, only the
    contract's reads are used, and the same record and settings give the same
    class twice; a changed setting is seen by the next call."""
    m = ready_to_merge(record, clock)
    before = record.head()
    first = classify(record, m.n, settings=DEFAULT)
    second = classify(Reads(record), m.n, settings=DEFAULT)
    assert record.head() == before
    assert first == second == MergeClass("auto", "pool-approved", ())
    strict = Settings(
        merge_rules=({"name": "strict", "risk_at_most": 3, "reviews": ["pool"]},)
    )
    assert classify(record, m.n, settings=strict).cls == "user"
    assert classify(record, m.n, settings=DEFAULT) == first


@over_backends
def test_a_read_only_handle_classifies(backend, record, opener, clock):
    """templet:3163 -- a handle opened read-only gives the same class."""
    m = ready_to_merge(record, clock)
    reader = opener(readonly=True)
    assert classify(reader, m.n, settings=DEFAULT) == classify(
        record, m.n, settings=DEFAULT
    )


# -- the command ----------------------------------------------------------


def merge(capsys, url, *args):
    code = cli.main(["order", "merge", url, *args])
    out, err = capsys.readouterr()
    return code, out, err


@over_backends
def test_order_merge_prints_auto_with_its_rule(backend, record, url, clock, capsys):
    """templet:3163 -- `order merge URL NUMBER` prints the item's class, and
    for `auto` the rule."""
    m = ready_to_merge(record, clock)
    code, out, err = merge(capsys, url, str(m.n))
    assert code == 0 and err == ""
    assert out.splitlines() == [f"#{m.n}: auto (rule pool-approved)"]


@over_backends
def test_order_merge_prints_every_reason(backend, record, url, clock, capsys):
    """templet:3163 -- for `user`, one `why:` line per reason."""
    m = ready_to_merge(record, clock, risk=6)
    _draft(m)
    code, out, err = merge(capsys, url, str(m.n))
    assert code == 0 and err == ""
    lines = out.splitlines()
    got = classify(record, m.n, settings=DEFAULT)
    assert lines[0] == f"#{m.n}: user"
    assert lines[1:] == [f"why: {line}" for line in got.why]
    assert len(lines) >= 3


@over_backends
def test_order_merge_reads_the_settings_file(
    backend, record, url, clock, capsys, tmp_path
):
    """templet:3163 -- `--settings FILE` is a JSON object of `Settings`'
    fields, its `merge_rules` a list of rules."""
    m = ready_to_merge(record, clock, risk=6)
    path = tmp_path / "settings.json"
    rules = [{"name": "wider", "risk_at_most": 7, "reviews": ["pool"]}]
    path.write_text(json.dumps({"merge_rules": rules}))
    code, out, _ = merge(capsys, url, str(m.n), "--settings", str(path))
    assert code == 0 and out.splitlines() == [f"#{m.n}: auto (rule wider)"]
    path.write_text(json.dumps({"merge_rules": []}))
    code, out, _ = merge(capsys, url, str(m.n), "--settings", str(path))
    assert code == 0 and "why: no merge rule" in out


@over_backends
def test_order_merge_refuses_what_is_not_whole(
    backend, record, url, clock, capsys, tmp_path
):
    """templet:3163 -- exit 2, and nothing on stdout, on settings with
    problems, an unknown field, a file that is not there, and an item the
    record does not hold."""
    m = ready_to_merge(record, clock)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"merge_rules": [{"name": "x", "risk_at_most": 11}]}))
    code, out, err = merge(capsys, url, str(m.n), "--settings", str(bad))
    assert (code, out) == (2, "") and "merge_rules" in err
    bad.write_text(json.dumps({"nope": 1}))
    code, out, err = merge(capsys, url, str(m.n), "--settings", str(bad))
    assert (code, out) == (2, "") and "nope" in err
    code, out, err = merge(
        capsys, url, str(m.n), "--settings", str(tmp_path / "none.json")
    )
    assert (code, out) == (2, "") and "refused" in err
    code, out, err = merge(capsys, url, "99")
    assert (code, out) == (2, "") and "no item 99" in err


@over_backends
def test_order_merge_takes_timing_and_debug(backend, record, url, clock, capsys):
    """templet:3163 -- `--timing` and `--debug` write to stderr and leave
    stdout as it was."""
    m = ready_to_merge(record, clock)
    _, plain, _ = merge(capsys, url, str(m.n))
    code, out, err = merge(capsys, url, str(m.n), "--timing", "--debug")
    assert code == 0 and out == plain
    assert "settings:" in err and "class:" in err
    timing = next(line for line in err.splitlines() if line.startswith("timing:"))
    for phase in ("opening the record", "classifying", "printing"):
        assert phase in timing


@over_backends
def test_order_merge_prints_a_slow_phase(
    backend, record, url, clock, capsys, monkeypatch
):
    """templet:3163 -- a phase that takes longer than the bound is printed
    with its elapsed time."""
    m = ready_to_merge(record, clock)
    monkeypatch.setattr(cli, "SLOW_PHASE_S", 0.0)
    code, out, err = merge(capsys, url, str(m.n))
    assert code == 0
    assert "opening the record: " in err and "classifying: " in err and " s" in err
