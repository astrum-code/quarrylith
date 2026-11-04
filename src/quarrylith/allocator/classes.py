# SPDX-License-Identifier: Apache-2.0
"""Run classes and effective needs: which class of the document a task is in,
and what that slices off the host for it."""

from .document import HARNESS, MODEL_CLASSES
from .seam import Refusal, Short, refuse


def _unclassified(document, task, run_class, resource):
    """The Refusal of a task the document cannot place in a class (`resource`
    None) or whose class cannot be filled for want of a label `resource`."""
    short = Short(
        "unclassified", document.node, resource, None, None, None, False, None, []
    )
    return refuse(task["id"], run_class, {}, [short], node=document.node)


def classify(document, task, labels):
    """The first class, in the document's order, whose match holds for `task`
    (a task record) with its effective `labels`: its kind, sandbox, labels and
    flags. A `Refusal` (`unclassified`, never) when none does."""
    for run_class in document.classes:
        if run_class.match.holds(task, labels):
            return run_class
    return _unclassified(document, task, None, None)


def _value(labels, key):
    """The value of the first label of `key`, or None."""
    prefix = f"{key}:"
    for label in labels:
        if label.startswith(prefix):
            return label[len(prefix) :]
    return None


def _harness(document, task, run_class, labels, count):
    """What `harness = count` is for this task: `count` of its harness's slots
    resource and `count` times the weight of its budget resource. A `Refusal`
    when the task names no harness the document has, or a model class it does
    not weigh."""
    row = document.harness.get(_value(labels, HARNESS))
    if row is None:
        return _unclassified(document, task, run_class.name, HARNESS)
    needs = {row.slots: count}
    if row.budget is not None:
        model = _value(labels, "class") or "standard"
        if model not in MODEL_CLASSES:
            return _unclassified(document, task, run_class.name, "class")
        needs[row.budget] = count * document.budget.weight[model]
    return needs


def _raise(needs, resource, amount):
    needs[resource] = max(needs.get(resource, 0), amount)


def needs_of(document, task, labels):
    """The effective needs of `task`: per resource, the larger of its class's
    amount and the task's own stored needs, so never less than the task asked.
    A class's `harness = N` becomes N of the task's harness's slots and N times
    its model class's weight of its budget, and the task takes 1 of every lane
    it is in. A `Refusal` when the task cannot be classified."""
    run_class = classify(document, task, labels)
    if isinstance(run_class, Refusal):
        return run_class
    needs = {}
    for resource, amount in run_class.needs.items():
        if resource != HARNESS:
            _raise(needs, resource, amount)
            continue
        expanded = _harness(document, task, run_class, labels, amount)
        if isinstance(expanded, Refusal):
            return expanded
        for slot, count in expanded.items():
            _raise(needs, slot, count)
    for lane in document.lanes:
        if any(label in labels for label in lane.labels):
            _raise(needs, lane.resource, 1)
    for resource, amount in task["needs"].items():
        _raise(needs, resource, amount)
    return needs
