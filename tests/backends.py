# SPDX-License-Identifier: Apache-2.0
"""The conformance pattern: every backend in `BACKENDS`, a way to make a
record's URL on each, and `over_backends` to run a test over all of them. A
backend with no entry in `URLS` skips with that reason, never passes."""

import pytest

from quarrylith.record import contract


def _sqlite(folder):
    folder.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{folder / 'record.sqlite'}"


#: How a test makes a fresh record's URL in a folder, per backend.
URLS = {"sqlite": _sqlite}

#: Run a test once per backend: it takes the `backend` argument, and the
#: `url`, `opener` and `record` fixtures follow it.
over_backends = pytest.mark.parametrize("backend", sorted(contract.BACKENDS))


def url_for(backend, folder):
    make = URLS.get(backend)
    if make is None:
        pytest.skip(f"no fixture makes a record on the {backend} backend")
    return make(folder)


class Clock:
    """The record's clock, which a test moves: milliseconds, by hand."""

    def __init__(self, at=1_700_000_000_000):
        self.at = at

    def __call__(self):
        return self.at

    def step(self, ms=1000):
        self.at += ms
        return self.at


#: The six parts an effort is filed with.
PARTS = {
    "statement": "Do the thing",
    "inputs": {},
    "tools": {},
    "budget": {},
    "evidence": {},
    "gates": {},
}


def admitted(record, id="0001"):
    """File an admitted effort, `id`."""
    return record.file_effort(
        id, f"e{id}", f"Effort {id}", "owner-a", PARTS, standing="admitted"
    )
