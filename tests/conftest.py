# SPDX-License-Identifier: Apache-2.0
"""The conformance fixtures (templet:3159): a fresh record per backend, a
clock the tests move, and an opener for more handles on the same record."""

import pytest
from backends import Clock, admitted, url_for
from hengestore import guarding, stop_all

from quarrylith.allocator import probes
from quarrylith.record import contract

#: The real reader of the host's memory pressure, kept for the tests that read it.
REAL_PROBE_PRESSURE = getattr(probes, "probe_pressure", None)


def calm_pressure():
    """A host with no pressure and a small swap: what a placement test reads
    unless it says otherwise, so a busy test host cannot fail one."""
    return {
        "level": 1,
        "swap_used": 0,
        "swap_total": 2**30,
        "memory_free_percent": 80,
        "method": "given",
    }


@pytest.fixture(autouse=True)
def calm_host(monkeypatch):
    """The tests read no real memory pressure: the reader of the default probes
    answers calm. A test that wants the real read takes REAL_PROBE_PRESSURE."""
    monkeypatch.setattr(probes, "probe_pressure", calm_pressure, raising=False)


@pytest.fixture(scope="session", autouse=True)
def guarded_henge():
    """Every Henge record of the suite reaches its store through `Guarded`:
    no delete, no listing, no get_many past 1,000 keys. The stores stop at
    the end."""
    henge = getattr(contract, "henge", None)
    if henge is None:
        yield
        return
    connect = henge._connect
    henge._connect = guarding(connect)
    try:
        yield
    finally:
        henge._connect = connect
        stop_all()


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


@pytest.fixture
def effort(record):
    """A fresh record with an admitted effort, for tests that file tasks."""
    admitted(record)
    return record
