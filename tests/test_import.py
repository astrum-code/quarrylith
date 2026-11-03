# SPDX-License-Identifier: Apache-2.0
"""`quarrylith record import` and the sample import document (templet:3159,
build step 4), over every backend in BACKENDS."""

import json
import re
from datetime import datetime, timezone

import pytest
from backends import over_backends
from samples import SAMPLE, SAMPLE_EFFORT, load_sample

from quarrylith import cli
from quarrylith.record import schema
from quarrylith.record.contract import open_record
from quarrylith.record.importing import (
    OWN_KINDS,
    ImportRefused,
    check_import,
    compose,
)
from quarrylith.record.schema import RECORDS


def run(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def report(out):
    """The import's report: {table: (read, pulled, left)} and {column:
    count}."""
    tables, columns = {}, {}
    for line in out.splitlines():
        words = line.split()
        if len(words) == 4 and words[0] != "table":
            tables[words[0]] = tuple(int(w) for w in words[1:])
        elif len(words) == 3 and words[0] == "dropped":
            columns[words[1]] = int(words[2])
    return tables, columns


def held(doc):
    """What the document holds per legacy table, counted from its own rows:
    {table: (read, pulled, left)} and the dropped columns."""
    tables, columns = {}, {}
    for table, read in doc["source"]["tables"].items():
        pulled = sum(r["table"] == table for r in doc["pulled"])
        left = sum(
            r["count"]
            for r in doc["left"]
            if r["table"] == table and r.get("column") is None
        )
        tables[table] = (read, pulled, left)
    for r in doc["left"]:
        if r.get("column") is not None:
            columns[f"{r['table']}.{r['column']}"] = r["count"]
    return tables, columns


@over_backends
def test_the_imports_report_counts_equal_the_rows_per_table(url, capsys):
    """templet:3159 -- `quarrylith record import` of the sample document
    reports per legacy table the rows read, pulled and left, which equal the
    rows the document holds per table, pulled plus left making the rows read;
    and the dropped columns. The `imports` record holds the same counts."""
    doc = load_sample()
    code, out, err = run(capsys, "record", "import", str(SAMPLE), "--into", url)
    assert code == 0, err
    tables, columns = report(out)
    want_tables, want_columns = held(doc)
    assert tables == want_tables
    assert (
        columns
        == want_columns
        == {
            "budget.source": 3,
            "comments.source": 6,
            "items.source": 10,
            "runs.pid": 4,
        }
    )
    for table, (read, pulled, left) in tables.items():
        assert pulled + left == read, table
    assert tables["items"] == (10, 10, 0) and tables["journal"] == (120, 0, 120)
    assert tables["meta"] == (4, 3, 1) and tables["scratch"] == (1, 0, 1)
    record = open_record(url, actor="alpha", via="cli", readonly=True)
    try:
        counts = record.get("imports", 1)["counts"]
        assert record.check() == []
        assert len(record.read("items")) == 10
    finally:
        record.close()
    assert {t: v for t, v in counts["pulled"].items()} == {
        t: p for t, (_, p, _) in want_tables.items() if p
    }
    assert {t: v for t, v in counts["left"].items()} == {
        t: lf
        for t, (_, _, lf) in want_tables.items()
        if any(r["table"] == t and r.get("column") is None for r in doc["left"])
    }
    assert counts["columns"] == want_columns


@over_backends
def test_an_import_into_a_record_that_holds_records_is_refused(url, capsys):
    """An import goes into an empty record: into one that holds records it is
    refused, and writes nothing."""
    assert run(capsys, "record", "import", str(SAMPLE), "--into", url)[0] == 0
    record = open_record(url, actor="alpha", via="cli", readonly=True)
    before = record.dump()
    record.close()
    code, out, err = run(capsys, "record", "import", str(SAMPLE), "--into", url)
    assert code == 2 and "empty" in err and out == ""
    record = open_record(url, actor="alpha", via="cli", readonly=True)
    assert record.dump() == before
    record.close()


def test_the_sample_is_whole_and_holds_every_kind_an_import_takes():
    """The sample import document passes `check_import`, and holds every kind
    of record an import carries: none of the kinds the import writes itself,
    and none that only the work of the new record makes (capacity and
    reservations)."""
    doc = load_sample()
    check_import(doc)
    takes = {k for k in RECORDS if k not in OWN_KINDS + ("capacity", "reservations")}
    assert set(doc["records"]) == takes
    assert all(doc["records"][k] for k in takes), [
        k for k in takes if not doc["records"][k]
    ]
    assert doc["format"] == "quarrylith-import/1"
    # the effort document it was read from, and the linking pass's result
    efforts = {e["id"]: e for e in doc["records"]["efforts"]}
    assert efforts["unsorted"] == schema.unsorted_effort(
        efforts["unsorted"]["created_at"]
    )
    links = {int(r["target"]): r["effort"] for r in doc["records"]["effort_links"]}
    assert {n for n, e in links.items() if e == "0001"} == {10, 11, 12, 13, 18}
    # nothing closed and nothing reopened: every legacy state stays
    states = {i["number"]: i["state"] for i in doc["records"]["items"]}
    assert states == {
        5: "done",
        10: "ready",
        11: "in-review",
        12: "done",
        13: "claimed",
        14: "triage",
        15: "dropped",
        16: "done",
        17: "ready",
        18: "triage",
    }


def test_check_import_refuses_an_unsorted_that_is_not_the_records():
    """`unsorted` is the record's own effort: a document whose `unsorted`
    differs from `schema.unsorted_effort` in any field but its time is
    refused, naming the field."""
    doc = load_sample()
    for field, value in (
        ("owner", "lead"),
        ("statement", "x"),
        ("standing", "admitted"),
    ):
        bad = json.loads(json.dumps(doc))
        for effort in bad["records"]["efforts"]:
            if effort["id"] == "unsorted":
                effort[field] = value
        with pytest.raises(ImportRefused, match=f"unsorted.*{field}"):
            check_import(bad)


def test_check_import_names_a_link_to_an_effort_the_document_lacks():
    """An effort link to an effort the document does not hold is refused,
    each link named, before the dump finds its items unlinked."""
    doc = load_sample()
    doc["records"]["efforts"] = [
        e for e in doc["records"]["efforts"] if e["id"] != "unsorted"
    ]
    with pytest.raises(ImportRefused) as refused:
        check_import(doc)
    dangling = [p for p in refused.value.problems if "effort_links" in p]
    assert len(dangling) == 5, refused.value.problems
    assert all("'unsorted'" in p and "no such effort" in p for p in dangling)
    assert any("'unsorted', 'item', '14'" in p for p in dangling)


def effort_doc(text):
    """An effort document's parts, by the rules the page gives step 5:
    `{id, title, owner, statement, parts: {part: [lines]}, dated: {section:
    [(date, text)]}}`."""
    heading = re.match(r"# Effort (\d{4}): (.+)\n", text)
    owner = re.search(r"^Owner: (\S+)$", text, re.M)
    sections = dict(re.findall(r"^## (\w+)\n\n(.*?)(?=^## |\Z)", text, re.M | re.S))
    lines = {
        name: re.findall(r"^- (.+)$", body, re.M) for name, body in sections.items()
    }
    return {
        "id": heading[1],
        "title": heading[2],
        "owner": owner[1] if owner else None,
        "statement": " ".join(sections["Statement"].split()),
        "parts": {
            name.lower(): lines[name]
            for name in ("Inputs", "Tools", "Budget", "Evidence", "Gates")
        },
        "dated": {
            name: [tuple(line.split(": ", 1)) for line in lines[name]]
            for name in ("Decisions", "Log")
        },
    }


def midnight(date):
    """A `YYYY-MM-DD` date at midnight UTC, in milliseconds."""
    day = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(day.timestamp() * 1000)


def test_the_samples_effort_follows_its_effort_document():
    """The sample's effort `0001` is its effort document by the page's rules,
    so step 5's export derives every field: the id and title from the
    heading, the slug from the title (lower case, a hyphen between words),
    the owner from its `Owner:` line, `created_at` the export's `read_at`,
    `admitted`, the statement its paragraph, each part `{"lines": [...]}`;
    each Decisions and Log line a statement of the effort by its owner at the
    line's date, midnight UTC."""
    doc = load_sample()
    src = effort_doc(SAMPLE_EFFORT.read_text(encoding="utf-8"))
    efforts = {e["id"]: e for e in doc["records"]["efforts"]}
    effort = efforts[src["id"]]
    assert src["owner"] is not None, "the effort document has an Owner: line"
    slug = re.sub(r"[^a-z0-9]+", "-", src["title"].lower()).strip("-")
    assert {k: effort[k] for k in ("title", "slug", "owner", "created_at")} == {
        "title": src["title"],
        "slug": slug,
        "owner": src["owner"],
        "created_at": doc["source"]["read_at"],
    }
    assert effort["standing"] == "admitted"
    assert effort["statement"] == src["statement"]
    for part, lines in src["parts"].items():
        assert effort[part] == {"lines": lines}, part
    want = [
        ("decision" if section == "Decisions" else "action", midnight(date), text)
        for section, rows in src["dated"].items()
        for date, text in rows
    ]
    got = [
        (s["type"], s["at"], s["text"])
        for s in doc["records"]["statements"]
        if s["effort"] == src["id"]
    ]
    assert sorted(got) == sorted(want)
    assert {
        s["who"] for s in doc["records"]["statements"] if s["effort"] == src["id"]
    } == {src["owner"]}


def test_the_samples_other_choices_are_the_pages_rules():
    """The choices the page gives step 5 beside the effort's: a task's
    `created_at` its first run's `started_at`; the node `legacy` with the
    drivers `["harness"]`, no labels, declared and retired at `read_at`; the
    figures observation by `import` at `read_at`; a `Decided` statement the
    answer comment's text, `who`, `at` and item, `where` `comment:<id>`."""
    doc = load_sample()
    records, read_at = doc["records"], doc["source"]["read_at"]
    first = {}
    for run in sorted(records["runs"], key=lambda r: r["started_at"]):
        first.setdefault(run["task"], run["started_at"])
    for task in records["tasks"]:
        assert task["created_at"] == first[task["id"]], task["id"]
    assert records["nodes"] == [
        {
            "name": "legacy",
            "drivers": ["harness"],
            "labels": [],
            "declared_at": read_at,
            "retired_at": read_at,
        }
    ]
    (figures,) = [s for s in records["statements"] if s["type"] == "observation"]
    assert (figures["who"], figures["at"], figures["where"]) == (
        "import",
        read_at,
        "board figures",
    )
    comments = {c["id"]: c for c in records["comments"]}
    decided = [
        s
        for s in records["statements"]
        if s["type"] == "decision" and s["effort"] is None
    ]
    assert decided
    for s in decided:
        comment = comments[int(s["where"].removeprefix("comment:"))]
        assert comment["mark"] == "answer" and comment["body"].startswith("Decided")
        assert (s["text"], s["who"], s["at"], s["number"]) == (
            comment["body"],
            comment["who"],
            comment["at"],
            comment["number"],
        )


@over_backends
def test_an_open_items_finished_legacy_task_is_open_and_never_ready(record):
    """A legacy task on an open item whose runs all finished is `open`, with
    `attempts == loop.max_attempts`, so `ready()` never gives it: the item is
    ready, and work after the switch takes a new task."""
    doc = load_sample()
    record.load(compose(doc))
    tasks = [t for t in record.read("tasks") if t["state"] == "open"]
    assert len(tasks) == 1
    (task,) = tasks
    item = record.get("items", task["number"])
    assert item["state"] == "ready" and item["closed_at"] is None
    runs = [r for r in record.read("runs") if r["task"] == task["id"]]
    assert runs and all(r["finished_at"] is not None for r in runs)
    assert task["attempts"] == task["loop"]["max_attempts"] == len(runs)
    ready = record.ready(now=doc["source"]["read_at"])
    assert task["number"] in [i["number"] for i in ready["items"]]
    assert ready["tasks"] == []
    assert record.check() == []


def problems_of(doc):
    """The problems `check_import` refuses `doc` with; none when it passes."""
    try:
        check_import(doc)
    except ImportRefused as e:
        return e.problems
    return []


def with_allocation(doc):
    """The sample with a capacity and a live reservation on its node `legacy`,
    held by its live run, so every kind that names another is in it."""
    live = next(r for r in doc["records"]["runs"] if r["finished_at"] is None)
    doc["records"]["capacity"] = [
        {
            "node": "legacy",
            "resource": "slots",
            "amount": 1,
            "unit": "slots",
            "declared_by": "import",
            "at": live["started_at"],
        }
    ]
    doc["records"]["reservations"] = [
        {
            "id": 1,
            "node": "legacy",
            "resource": "slots",
            "amount": 1,
            "task": live["task"],
            "run": live["id"],
            "holder": live["holder"],
            "from_at": live["started_at"],
            "until_at": None,
            "released_at": None,
            "why": None,
        }
    ]
    doc["counters"]["next:reservations"] = 2
    return doc


def test_check_import_refuses_a_run_whose_task_is_missing():
    """templet:3159 -- a run whose task the document lacks is refused, naming
    the run and the task, before four gauges would raise on it."""
    doc = load_sample()
    assert problems_of(doc) == []
    run = next(r for r in doc["records"]["runs"] if r["id"] == 5)
    run["task"] = 99
    assert problems_of(doc) == [
        "records.runs [5]: task 99: no such task in the document"
    ]


def test_check_import_refuses_a_task_whose_item_is_missing():
    """templet:3159 -- a task whose `number` names an item the document lacks
    is refused, naming the task and the item, before `filed_to_start` would
    raise on it."""
    doc = load_sample()
    assert problems_of(doc) == []
    task = next(t for t in doc["records"]["tasks"] if t["id"] == 4)
    task["number"] = 99
    assert problems_of(doc) == [
        "records.tasks [4]: number 99: no such item in the document"
    ]


#: A task's `loop`, `needs` and `supervision` that no operation writes, and
#: what each is refused with.
SHAPES = (
    ("loop", [1], "a task's loop is {max_attempts, interval_s}, not [1]"),
    ("loop", "twice", "a task's loop is {max_attempts, interval_s}"),
    ("loop", {"max_attempts": 1, "every": 5}, "a task's loop is"),
    ("loop", {"max_attempts": 0, "interval_s": 0}, "loop.max_attempts is an int"),
    ("loop", {"max_attempts": True, "interval_s": 0}, "loop.max_attempts is an int"),
    ("loop", {"max_attempts": 1.0, "interval_s": 0}, "loop.max_attempts is an int"),
    ("loop", {"max_attempts": 1}, "an operation writes it as {'max_attempts': 1, "),
    ("loop", {}, "an operation writes it as"),
    ("needs", [1], "needs are {resource: amount}, not [1]"),
    ("needs", {"slots": 0}, "needs slots: an amount is an int from 1"),
    ("needs", {"": 1}, "a resource is a non-empty str"),
    ("supervision", [1], "a task's supervision is {expect_s, beat_s}, not [1]"),
    ("supervision", 7, "a task's supervision is {expect_s, beat_s}, not 7"),
    ("supervision", {"beat_s": "soon"}, "supervision.beat_s is an int from 0"),
    ("supervision", {"beat_s": -30}, "supervision.beat_s is an int from 0"),
    ("supervision", {"beat_s": True}, "supervision.beat_s is an int from 0"),
    ("supervision", {"beat_s": 0}, "an operation writes it as {'expect_s': None, "),
)


@pytest.mark.parametrize("field, value, says", SHAPES)
def test_check_import_refuses_a_task_shaped_as_no_operation_makes_it(
    field, value, says
):
    """templet:3159 -- a task's `loop`, `needs` and `supervision` are shaped
    as `file_task` makes them, every key there and each value of its type, or
    the document is refused, naming the task and the field: a `loop` that is
    not a dict would make `ready()` raise."""
    doc = load_sample()
    assert problems_of(doc) == []
    task = next(t for t in doc["records"]["tasks"] if t["id"] == 4)
    task[field] = value
    (problem,) = problems_of(doc)
    assert problem.startswith(f"records.tasks [4]: {field} {value!r}: "), problem
    assert says in problem, problem


def test_check_import_takes_a_task_shaped_as_file_task_makes_it():
    """templet:3159 -- a task as `file_task` writes it passes: the defaults,
    a positive `beat_s`, and `needs` of several resources."""
    doc = load_sample()
    task = next(t for t in doc["records"]["tasks"] if t["id"] == 4)
    task["loop"] = {"max_attempts": 3, "interval_s": 60}
    task["supervision"] = {"expect_s": 600, "beat_s": 30}
    task["needs"] = {"memory": 512, "slots": 1}
    assert problems_of(doc) == []


#: Every field that names another record, and the kind it names: the
#: page's list, which `schema.REFERENCES` declares.
REFERENCES = {
    ("effort_links", "effort"): "efforts",
    ("items", "parent"): "items",
    ("item_text", "number"): "items",
    ("deps", "number"): "items",
    ("deps", "blocker"): "items",
    ("deps", "change"): "changes",
    ("leases", "number"): "items",
    ("tasks", "number"): "items",
    ("tasks", "effort"): "efforts",
    ("capacity", "node"): "nodes",
    ("reservations", "node"): "nodes",
    ("reservations", "task"): "tasks",
    ("reservations", "run"): "runs",
    ("runs", "task"): "tasks",
    ("runs", "node"): "nodes",
    ("run_text", "run"): "runs",
    ("changes", "number"): "items",
    ("change_text", "change"): "changes",
    ("checks", "change"): "changes",
    ("reviews", "target"): "items",
    ("reviews", "item"): "items",
    ("merges", "change"): "changes",
    ("comments", "number"): "items",
    ("comments", "change"): "changes",
    ("questions", "number"): "items",
    ("questions", "run"): "runs",
    ("questions", "answer"): "comments",
    ("statements", "effort"): "efforts",
    ("statements", "number"): "items",
    ("statements", "supersedes"): "statements",
    ("holds", "number"): "items",
}

#: A value no record of the sample holds, by field type.
MISSING = {schema.INT: 999_999, schema.STR: "nowhere"}


@pytest.mark.parametrize("kind, field", sorted(REFERENCES))
def test_check_import_refuses_every_reference_the_document_lacks(kind, field):
    """templet:3159 -- every field that names another record names one the
    document holds: one that names none is refused, naming the record, the
    field and the kind it names, with every other fault."""
    doc = with_allocation(load_sample())
    assert problems_of(doc) == []
    rows = doc["records"][kind]
    row = next((r for r in rows if r[field] is not None), rows[0])
    (typ,) = [f.type for f in RECORDS[kind].all_fields if f.name == field]
    row[field] = MISSING[typ]
    key = schema.key_of(kind, row)
    target = REFERENCES[(kind, field)]
    expect = (
        f"records.{kind} {key}: {field} {MISSING[typ]!r}: no such "
        f"{target[:-1]} in the document"
    )
    assert expect in problems_of(doc)
    assert schema.REFERENCES[(kind, field)] == target


def test_check_import_lists_every_dangling_reference():
    """templet:3159 -- three dangling references are three problems, in one
    refusal."""
    doc = load_sample()
    doc["records"]["runs"][0]["node"] = "nowhere"
    doc["records"]["comments"][0]["number"] = 99
    doc["records"]["questions"][0]["answer"] = 99
    problems = problems_of(doc)
    assert len(problems) == 3, problems
    assert any("records.runs [1]: node 'nowhere'" in p for p in problems)
    assert any("records.comments [1]: number 99" in p for p in problems)
    assert any("records.questions [1]: answer 99" in p for p in problems)


def test_check_import_resolves_an_effort_links_target_by_its_kind():
    """templet:3159 -- an item link's `target` is the number of an item the
    document holds, and an effort link's the id of an effort it holds; a plan
    link's target is not a record, so it is taken as it is."""
    for kind, target, says in (
        ("item", "99", "target '99': no such item in the document"),
        ("item", "014", "target '014': an item link's target is its number"),
        ("item", "x", "target 'x': an item link's target is its number"),
        ("effort", "0002", "target '0002': no such effort in the document"),
    ):
        doc = load_sample()
        link = doc["records"]["effort_links"][0]
        link["kind"], link["target"] = kind, target
        key = schema.key_of("effort_links", link)
        assert problems_of(doc) == [f"records.effort_links {key}: {says}"]
    doc = load_sample()
    doc["records"]["effort_links"].append(
        {**doc["records"]["effort_links"][0], "kind": "effort", "target": "unsorted"}
    )
    doc["records"]["effort_links"].append(
        {**doc["records"]["effort_links"][0], "kind": "plan", "target": "anything"}
    )
    assert problems_of(doc) == []


def test_check_import_resolves_a_reviews_heads():
    """templet:3159 -- a review's `heads` are `{change id: sha}`, each a change
    the document holds, as `file_review` writes them."""
    for heads, says in (
        ({"9": "abc"}, "heads change '9': no such change in the document"),
        ({"01": "abc"}, "heads {'01': 'abc'}: a review's heads are"),
        ({"1": ""}, "heads {'1': ''}: a review's heads are"),
        ([["1", "abc"]], "heads [['1', 'abc']]: a review's heads are"),
    ):
        doc = load_sample()
        doc["records"]["reviews"][0]["heads"] = heads
        (problem,) = problems_of(doc)
        assert problem.startswith(f"records.reviews [1]: {says}"), problem


#: Fields named like a reference that are not in `schema.REFERENCES`, and
#: why: no operation resolves them, `check_import` resolves them by their own
#: rule, or they name no record.
NOT_REFERENCES = {
    ("efforts", "statement"): "the effort's own statement, a text",
    ("items", "templet"): "edit_item takes any number",
    ("beats", "run"): "a beat is bookkeeping, never refused",
    ("effort_links", "target"): "an item or an effort, by the link's kind",
    ("reviews", "heads"): "keyed by change id",
}


def test_every_field_that_names_a_record_is_a_reference():
    """templet:3159 -- `schema.REFERENCES` is the page's list, and every field
    named for a kind of record, or for an item by its number, is in it or
    named above with why: a kind added later with such a field is resolved,
    or says why not."""
    assert schema.REFERENCES == REFERENCES
    singular = {k[:-1]: k for k in RECORDS if k.endswith("s")}
    names = set(singular) | {
        "number",
        "parent",
        "blocker",
        "supersedes",
        "answer",
        "target",
        "templet",
        "heads",
    }
    found = set()
    for kind, k in RECORDS.items():
        if kind in OWN_KINDS:
            continue
        for f in k.all_fields:
            # an item's own number is its key, not a reference
            if f.name in names and (kind, f.name) != ("items", "number"):
                found.add((kind, f.name))
    assert found == set(REFERENCES) | set(NOT_REFERENCES)
