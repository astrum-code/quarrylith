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
    document_from_data,
    load_document,
    parse_amount,
    to_data,
    unit_of,
)
from .table import table_shorts
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
    "document_from_data",
    "load_document",
    "needs_of",
    "parse_amount",
    "placement_for",
    "refuse",
    "table_shorts",
    "to_data",
    "unit_of",
]
