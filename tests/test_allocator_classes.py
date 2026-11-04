# SPDX-License-Identifier: Apache-2.0
"""Run classes and effective needs (templet:3161, build step 1): `classify`
takes the first class whose match holds, and `needs_of` gives the larger of
the class's needs and the task's own. The tests that read labels from a record
run over every backend in BACKENDS."""

from pathlib import Path

from backends import over_backends

from quarrylith.allocator import Refusal, classify, load_document, needs_of

SAMPLE = Path(__file__).resolve().parent / "data" / "allocation-sample.toml"
GIB = 2**30

#: A document with nothing but a node and the two classes below it.
MINI = """
format = 1
node = "box"
drivers = ["harness", "process"]
[capacity]
memory = 1073741824
"slots.alpha" = 4
"budget.alpha" = 4
"slots.beta" = 2
[harness.alpha]
slots = "slots.alpha"
budget = "budget.alpha"
[harness.beta]
slots = "slots.beta"
[budget]
weight = { small = 1, standard = 2, deep = 3 }
warning_max = 2
stop_at = 0.95
max_age_s = 900
[floor]
memory = 1
disk = 1
max_age_s = 60
[drift]
tolerance = 0.1
"""


def sample():
    return load_document(SAMPLE.read_text())


def mini(*classes, lanes=""):
    return load_document(MINI + lanes + "".join(classes))


def klass(name, match, needs):
    return f'[[class]]\nname = "{name}"\nmatch = {match}\nneeds = {needs}\n'


def task(kind="agent", sandbox="harness", labels=(), flags=(), needs=None, id=7):
    """A task as the record holds it, with the fields the policy reads."""
    return {
        "id": id,
        "kind": kind,
        "sandbox": sandbox,
        "labels": sorted(labels),
        "flags": sorted(flags),
        "needs": dict(needs or {}),
    }


def name_of(doc, **kw):
    labels = kw.pop("labels", ())
    answer = classify(doc, task(labels=labels, **kw), sorted(labels))
    return answer if isinstance(answer, Refusal) else answer.name


# -- classify ------------------------------------------------------------------


def test_classify_takes_the_first_class_whose_match_holds():
    """templet:3161 -- over the sample: a smoke VM is `vm.smoke`, a VM is `vm`,
    a container is `container`, a local agent is `local` and not `agent`, an
    agent or a review is `agent`, a gate is `gate`, and the rest is `job`."""
    doc = sample()
    assert name_of(doc, sandbox="vm", flags=["smoke"]) == "vm.smoke"
    assert name_of(doc, sandbox="vm") == "vm"
    assert name_of(doc, sandbox="vm", flags=["smoke:x"]) == "vm"
    assert name_of(doc, sandbox="podman", kind="job") == "container"
    assert name_of(doc, labels=["harness:local"]) == "local"
    assert name_of(doc, labels=["harness:codex"]) == "agent"
    assert name_of(doc, kind="review", labels=["harness:codex"]) == "agent"
    assert name_of(doc, kind="review", labels=["harness:local"]) == "local"
    assert name_of(doc, kind="gate", sandbox="process") == "gate"
    for kind in ("sweep", "job", "cell"):
        assert name_of(doc, kind=kind, sandbox="process") == "job"


def test_classify_is_the_first_in_document_order_not_the_best_fit():
    """templet:3161 -- two classes that both match: the earlier in the
    document wins, whichever is the more specific, and swapping them in the
    document swaps the answer."""
    broad = klass("broad", '{ kind = ["agent"] }', "{ memory = 1 }")
    narrow = klass(
        "narrow",
        '{ kind = ["agent"], labels = ["a:b"], flags = ["f"] }',
        "{ memory = 2 }",
    )
    both = task(labels=["a:b"], flags=["f"])
    assert classify(mini(broad, narrow), both, ["a:b"]).name == "broad"
    assert classify(mini(narrow, broad), both, ["a:b"]).name == "narrow"
    only_kind = task()
    assert classify(mini(narrow, broad), only_kind, []).name == "broad"


def test_classify_matches_on_kind_sandbox_labels_and_flags():
    """templet:3161 -- `kind` and `sandbox` match any of their list, `labels`
    and `flags` need all of theirs carried, a class with several keys needs
    every key to hold, and an empty match matches every task."""
    odd = klass(
        "odd",
        '{ kind = ["gate", "cell"], sandbox = ["vm", "podman"],'
        ' labels = ["a:b", "c:d"], flags = ["f", "g"] }',
        "{ memory = 1 }",
    )
    rest = klass("rest", "{}", "{ memory = 1 }")
    doc = mini(odd, rest)
    every = dict(kind="cell", sandbox="podman")
    full = ["a:b", "c:d", "e:f"]
    assert name_of(doc, labels=full, flags=["f", "g", "h"], **every) == "odd"
    assert (
        name_of(doc, kind="gate", sandbox="vm", labels=full, flags=["f", "g"]) == "odd"
    )
    assert name_of(doc, labels=["a:b"], flags=["f", "g"], **every) == "rest"
    assert name_of(doc, labels=full, flags=["f"], **every) == "rest"
    assert (
        name_of(doc, kind="agent", sandbox="podman", labels=full, flags=["f", "g"])
        == "rest"
    )
    assert (
        name_of(doc, kind="cell", sandbox="process", labels=full, flags=["f", "g"])
        == "rest"
    )
    assert name_of(doc, kind="job", sandbox="process") == "rest"


def test_classify_refuses_a_task_no_class_matches_as_unclassified():
    """templet:3161 -- a task no class matches is refused `unclassified`,
    waits false, with the task, no class, and a one-line text."""
    only = klass("only", '{ kind = ["gate"] }', "{ memory = 1 }")
    doc = mini(only)
    refusal = classify(doc, task(id=12), [])
    assert isinstance(refusal, Refusal)
    assert refusal.task == 12
    assert refusal.run_class is None
    assert [s.cause for s in refusal.shorts] == ["unclassified"]
    assert refusal.shorts[0].waits is False
    assert refusal.waits is False
    assert refusal.until is None
    assert refusal.needs == {}
    assert refusal.text.startswith("allocator: task 12")
    assert "\n" not in refusal.text
    assert "unclassified" in refusal.text


@over_backends
def test_classify_reads_a_tasks_effective_labels(backend, record):
    """templet:3161 -- over every backend: the labels `classify` matches are
    the task's effective labels, a task's own label of a key replacing its
    item's, so an item labelled `harness:local` with a task labelled
    `harness:codex` is an `agent`, and the item's label alone is `local`."""
    doc = sample()
    item = record.file_item(
        "task",
        "t",
        "body",
        filed_by="alpha",
        effort=None,
        role="alpha",
        labels=["harness:local"],
    )
    own = record.file_task(
        "agent", number=item, labels=["harness:codex"], sandbox="harness"
    )
    inherited = record.file_task("agent", number=item, sandbox="harness")
    for id, expect in ((own, "agent"), (inherited, "local")):
        row = record.get("tasks", id)
        answer = classify(doc, row, record.effective_labels(id))
        assert answer.name == expect
    # the stored labels alone would have said otherwise
    assert record.get("tasks", inherited)["labels"] == []


# -- needs_of ------------------------------------------------------------------


def test_needs_of_gives_the_class_needs_with_the_harness_expanded():
    """templet:3161 -- an agent of the claude-code harness needs its class's
    memory and disk, one `slots.claude-code` and one `budget.anthropic`; the
    resources are whole numbers."""
    needs = needs_of(
        sample(), task(labels=["harness:claude-code"]), ["harness:claude-code"]
    )
    assert needs == {
        "memory": 2 * GIB,
        "disk": 4 * GIB,
        "slots.claude-code": 1,
        "budget.anthropic": 1,
    }
    assert all(type(v) is int for v in needs.values())


def test_needs_of_is_the_larger_of_the_class_and_the_task_per_resource():
    """templet:3161 -- per resource the larger of the class's amount and the
    task's own stored needs: a task may ask for more than its class gives and
    never for less, and a resource only the task names is kept."""
    labels = ["harness:claude-code"]
    own = {"memory": 3 * GIB, "disk": 1, "slots.claude-code": 2, "extra": 5}
    needs = needs_of(sample(), task(labels=labels, needs=own), labels)
    assert needs["memory"] == 3 * GIB  # the task's, larger
    assert needs["disk"] == 4 * GIB  # the class's, larger
    assert needs["slots.claude-code"] == 2  # the task's, larger
    assert needs["budget.anthropic"] == 1  # the class's alone
    assert needs["extra"] == 5  # the task's alone
    for resource, amount in own.items():
        assert needs[resource] >= amount


def test_a_harness_need_of_n_is_n_slots_and_n_times_the_weight_of_budget():
    """templet:3161 -- `harness = N` becomes N of the harness's `slots`
    resource and N times the weight of its `budget` resource, the weight
    coming from the task's `class:` label (small 1, standard 2, deep 3 in the
    document); a task without a `class:` label weighs standard."""
    doc = mini(klass("any", "{}", "{ memory = 1, harness = 2 }"))
    expect = {"small": 2, "standard": 4, "deep": 6}
    for model, weight in expect.items():
        labels = ["harness:alpha", f"class:{model}"]
        needs = needs_of(doc, task(labels=labels), labels)
        assert needs["slots.alpha"] == 2, model
        assert needs["budget.alpha"] == weight, model
    labels = ["harness:alpha"]
    assert needs_of(doc, task(labels=labels), labels)["budget.alpha"] == 4


def test_a_harness_with_no_budget_resource_takes_slots_only():
    """templet:3161 -- the local harness has slots and no budget: its runs
    take a slot and no budget, whatever their class label."""
    doc = sample()
    labels = ["harness:local", "class:deep"]
    needs = needs_of(doc, task(labels=labels), labels)
    assert needs == {"memory": 2 * GIB, "disk": 4 * GIB, "slots.local": 1}


def test_a_class_with_harness_and_a_task_without_one_is_unclassified():
    """templet:3161 -- a class with `harness` and a task with no `harness:`
    label, or one that names a harness the document has no row for, or a
    `class:` label that is not small, standard or deep, is refused
    `unclassified`, naming `harness` (or `class`), with waits false."""
    doc = sample()
    for labels, named in (
        ([], "harness"),
        (["class:deep"], "harness"),
        (["harness:nowhere"], "harness"),
        (["harness:codex", "class:huge"], "class"),
    ):
        refusal = needs_of(doc, task(labels=labels, id=3), labels)
        assert isinstance(refusal, Refusal), labels
        assert refusal.task == 3
        assert refusal.run_class == "agent"
        assert refusal.waits is False
        assert [(s.cause, s.resource, s.waits) for s in refusal.shorts] == [
            ("unclassified", named, False)
        ]
        assert named in refusal.text


def test_a_task_without_a_harness_need_needs_no_harness_label():
    """templet:3161 -- a gate, a job and a VM take no `harness:` label."""
    doc = sample()
    assert needs_of(doc, task(kind="gate", sandbox="process"), []) == {
        "memory": 3 * GIB,
        "disk": 4 * GIB,
        "slots.gate": 1,
    }
    assert needs_of(doc, task(kind="sweep", sandbox="process"), []) == {
        "memory": 512 * 2**20,
        "disk": 512 * 2**20,
    }
    assert needs_of(doc, task(sandbox="vm", flags=["smoke"]), [])["lab.smoke"] == 1


def test_needs_of_refuses_what_classify_refuses():
    """templet:3161 -- a task no class matches gets the same Refusal from
    `needs_of`."""
    doc = mini(klass("only", '{ kind = ["gate"] }', "{ memory = 1 }"))
    refusal = needs_of(doc, task(), [])
    assert isinstance(refusal, Refusal)
    assert refusal.shorts[0].cause == "unclassified"


LANES = """
[[lane]]
name = "red"
labels = ["repo:a", "repo:b"]
max = 2
[[lane]]
name = "blue"
labels = ["repo:b", "team:x"]
max = 1
"""


def test_a_task_in_a_lane_takes_one_of_it_beside_its_class_needs():
    """templet:3161 -- a task carrying any of a lane's labels takes 1 of
    `lane.<name>`, one per lane it is in (two lanes: one of each), and a task
    in no lane takes none."""
    doc = mini(klass("any", "{}", "{ memory = 1 }"), lanes=LANES)
    cases = {
        ("repo:a",): {"lane.red": 1},
        ("repo:b",): {"lane.red": 1, "lane.blue": 1},
        ("team:x",): {"lane.blue": 1},
        ("repo:a", "team:x"): {"lane.red": 1, "lane.blue": 1},
        ("repo:c",): {},
        (): {},
    }
    for labels, lanes in cases.items():
        needs = needs_of(doc, task(labels=labels), sorted(labels))
        assert needs == {"memory": 1, **lanes}, labels


def test_a_lane_need_is_at_least_the_tasks_own():
    """templet:3161 -- a task that stores 3 of a lane keeps 3, and the lane's
    1 is added only where the task has less."""
    doc = mini(klass("any", "{}", "{ memory = 1 }"), lanes=LANES)
    labels = ["repo:a"]
    needs = needs_of(doc, task(labels=labels, needs={"lane.red": 3}), labels)
    assert needs["lane.red"] == 3


def test_the_sample_lane_reads_the_repo_label():
    """templet:3161 -- the sample's lane is over `repo:widgets`: a gate in
    that repo takes one `lane.widgets` and a gate in another repo none."""
    doc = sample()
    inside = ["repo:widgets"]
    assert needs_of(doc, task(kind="gate", labels=inside), inside)["lane.widgets"] == 1
    other = ["repo:gadgets"]
    assert "lane.widgets" not in needs_of(doc, task(kind="gate", labels=other), other)
