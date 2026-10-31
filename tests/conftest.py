# SPDX-License-Identifier: Apache-2.0
"""The conformance fixtures (templet:3159): a fresh record per backend, a
clock the tests move, and an opener for more handles on the same record."""

import pytest
from backends import Clock, url_for

from quarrylith.record import contract


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def url(backend, tmp_path):
    return url_for(backend, tmp_path / "record")


@pytest.fixture
def opener(url, clock):
    """`opener(**kw)` opens the test's record (actor alpha, via cli unless
    given) on the test's clock; every handle is closed at the end."""
    opened = []

    def open_(**kw):
        kw.setdefault("actor", "alpha")
        kw.setdefault("via", "cli")
        rec = contract.open_record(url, **kw)
        rec.clock = clock
        opened.append(rec)
        return rec

    yield open_
    for rec in opened:
        rec.close()


@pytest.fixture
def record(opener):
    """A fresh, initialised record."""
    rec = opener()
    rec.init()
    return rec
