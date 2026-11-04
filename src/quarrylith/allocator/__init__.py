# SPDX-License-Identifier: Apache-2.0
"""The allocator: whether and where a task fits on a host. The seam
(`Placement` and its answers), the allocation document, and the run classes
that slice a host's capacity. It reads the record only through its contract."""

from .classes import classify, needs_of
from .document import (
    Consumer,
    Document,
    DocumentError,
    Harness,
    Lane,
    Match,
    RunClass,
    load_document,
    parse_amount,
    unit_of,
)
from .seam import (
    CAUSES,
    PLACEMENT_GROUP,
    Fit,
    Placement,
    Refusal,
    Short,
    placement_for,
    refuse,
)

__all__ = [
    "CAUSES",
    "PLACEMENT_GROUP",
    "Consumer",
    "Document",
    "DocumentError",
    "Fit",
    "Harness",
    "Lane",
    "Match",
    "Placement",
    "Refusal",
    "RunClass",
    "Short",
    "classify",
    "load_document",
    "needs_of",
    "parse_amount",
    "placement_for",
    "refuse",
    "unit_of",
]
