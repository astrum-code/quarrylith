# SPDX-License-Identifier: Apache-2.0
"""Helpers the tests share: a valid record of any kind, made from its
declaration, and a synthetic import document with neutral names."""

from quarrylith.record import schema
from quarrylith.record.schema import RECORDS


def sample(kind, /, **over):
    """A valid record of `kind`: every field set to a small value of its
    type (null where it may be), `over` on top."""
    k = RECORDS[kind]
    rec = {}
    for f in k.key + k.fields:
        if f.type == schema.STR:
            value = "x"
        elif f.type == schema.INT:
            value = 1
        elif f.type == schema.BOOL:
            value = False
        else:
            value = [] if f.name in ("labels", "flags") else {}
        if f.null and f.name not in k.key_names:
            value = None
        rec[f.name] = value
    if k.one_of:
        first = k.one_of[0]
        rec[first] = {"str": "x", "int": 1}[
            next(f.type for f in k.fields if f.name == first)
        ]
        for other in k.one_of[1:]:
            rec[other] = None
    rec.update(over)
    return rec


def make_doc():
    """A small import document: two items, a lease, a run, a comment, the
    counters, and what was left. The names are neutral on purpose."""
    efforts = [
        sample(
            "efforts",
            id="0001",
            slug="alpha",
            title="Alpha",
            standing="admitted",
            owner="owner-a",
            created_at=1000,
            statement="Do the alpha thing",
            order={"rule": "unblocks-then-age"},
        ),
        sample(
            "efforts",
            id="unsorted",
            slug="unsorted",
            title="Unsorted",
            standing="parked",
            owner="owner-a",
            created_at=1000,
        ),
    ]
    items = [
        sample(
            "items",
            number=n,
            title=f"Item {n}",
            state="ready",
            labels=["repo:widgets", "team:alpha"],
            flags=["needs:lab"],
            admitted=True,
            filed_by="owner-a",
            created_at=2000,
            updated_at=2000,
            source="legacy",
            source_ref=None,
        )
        for n in (1, 2)
    ]
    tables = {
        "items": 2,
        "comments": 1,
        "claims": 1,
        "runs": 1,
        "journal": 40,
        "ranks": 3,
        "meta": 3,
    }
    return {
        "format": "quarrylith-import/1",
        "source": {
            "name": "legacy-board",
            "path": "/data/board.sqlite",
            "schema_version": 1,
            "last_seq": 40,
            "read_at": 1793371508596,
            "tables": tables,
        },
        "records": {
            "efforts": efforts,
            "effort_links": [
                sample(
                    "effort_links",
                    effort="0001",
                    kind="item",
                    target=str(n),
                    at=2000,
                    by="import",
                )
                for n in (1, 2)
            ],
            "items": items,
            "item_text": [
                sample("item_text", number=n, body=f"Body {n}") for n in (2, 1)
            ],
            "comments": [
                sample(
                    "comments",
                    id=1,
                    number=1,
                    who="owner-a",
                    body="Decided: yes",
                    at=2100,
                    mark="answer",
                )
            ],
            "leases": [
                sample(
                    "leases",
                    id=1,
                    number=1,
                    holder="alpha",
                    role="alpha",
                    fence=1,
                    branch="feat/1-x",
                    machine="host-a",
                    granted_at=2200,
                    renewed_at=2200,
                )
            ],
            "tasks": [
                sample(
                    "tasks",
                    id=1,
                    number=1,
                    kind="agent",
                    sandbox="harness",
                    state="done",
                    attempts=1,
                    created_at=2200,
                )
            ],
            "runs": [
                sample(
                    "runs",
                    id=1,
                    task=1,
                    attempt=1,
                    holder="alpha",
                    node="legacy",
                    sandbox="harness",
                    phase="done",
                    phase_at=2300,
                    started_at=2200,
                )
            ],
            "run_text": [sample("run_text", run=1, result="ok")],
            "beats": [sample("beats", run=1, at=2250)],
        },
        "counters": {
            "next:efforts": 2,
            "next:items": 3,
            "next:comments": 2,
            "next:leases": 2,
            "next:tasks": 2,
            "next:runs": 2,
            "next:change:widgets": 5,
        },
        "pulled": [
            {"table": "items", "legacy_key": [1], "record": "items", "record_key": [1]},
            {"table": "items", "legacy_key": [2], "record": "items", "record_key": [2]},
            {
                "table": "comments",
                "legacy_key": [7],
                "record": "comments",
                "record_key": [1],
            },
            {
                "table": "claims",
                "legacy_key": [3],
                "record": "leases",
                "record_key": [1],
            },
            {"table": "runs", "legacy_key": [9], "record": "runs", "record_key": [1]},
            {
                "table": "meta",
                "legacy_key": ["next_number"],
                "record": "meta",
                "record_key": ["next:items"],
            },
            {
                "table": "meta",
                "legacy_key": ["next_change:widgets"],
                "record": "meta",
                "record_key": ["next:change:widgets"],
            },
        ],
        "left": [
            {
                "table": "journal",
                "legacy_key": None,
                "count": 40,
                "why": "the legacy record's own history",
            },
            {"table": "ranks", "legacy_key": None, "count": 3, "why": "retired"},
            {
                "table": "meta",
                "legacy_key": ["other"],
                "count": 1,
                "why": "meta beyond the counters",
            },
            {
                "table": "items",
                "legacy_key": None,
                "column": "source",
                "count": 2,
                "why": "every imported record is legacy",
            },
            {
                "table": "runs",
                "legacy_key": None,
                "column": "pid",
                "count": 1,
                "why": "a process of the legacy host",
            },
        ],
    }
