# SPDX-License-Identifier: Apache-2.0
"""Hardening of what the services' needs added (templet:3161 and
templet:3159): a host reading's `reserved_through` that cannot be true is no
figure, and the message of a Henge record's first failure is written out on
one line. Every test that touches a record runs over every backend in
BACKENDS."""

import pytest
from backends import over_backends
from test_allocator_host import FIGURES, GIB, host_reading, make_task
from test_allocator_needs import floor_of

from quarrylith.allocator.host import HostPlacement
from quarrylith.record.contract import Unavailable, Unknown


def highest(record):
    """The highest reservation id the record holds."""
    return max(r["id"] for r in record.read("reservations"))


def declared_node(record, clock):
    placement = HostPlacement(record)
    assert placement.declare(floor_of("1GiB"), figures=FIGURES, now=clock()).ok
    return placement


def verdict(record, placement, clock):
    """Whether a task of 2 GiB fits now: the room is what the reading holds less
    what was reserved since it."""
    answer = placement.place(make_task(record), now=clock())
    return type(answer).__name__


@over_backends
def test_a_figure_above_the_highest_reservation_is_no_figure(effort, clock):
    """templet:3161 -- a `reserved_through` larger than any reservation id the
    record holds cannot have been seen: it counts as no figure and the clocks
    decide. With 3 GiB available (room 2), a 2 GiB hold made after the reading
    and stamped after it takes the room whatever huge figure the reading
    names."""
    record = effort
    placement = declared_node(record, clock)
    host_reading(record, clock, available=3 * GIB, reserved_through=10**18)
    clock.step()
    assert isinstance(placement.hold("vm:a", {"memory": 2 * GIB}, now=clock()), list)
    assert verdict(record, placement, clock) == "Refusal"


@over_backends
def test_the_figure_may_be_the_highest_reservation_and_no_more(effort, clock):
    """templet:3161 -- the figure is a figure up to the highest reservation id
    the record holds, and one more is not. A 2 GiB reservation made before the
    readings and stamped after them is seen by the record's order, so with the
    figure at its id the room is whole; with the figure one above it, which no
    reading could have named, the clocks decide and they count it."""
    record = effort
    placement = declared_node(record, clock)
    t = clock()
    record.reserve(
        "laptop",
        {"memory": 2 * GIB},
        holder="vm:early",
        from_at=t + 60_000,
        until_at=None,
    )
    clock.step()
    host_reading(record, clock, available=3 * GIB, reserved_through=highest(record))
    clock.step()
    assert verdict(record, placement, clock) == "Fit"
    host_reading(record, clock, available=3 * GIB, reserved_through=highest(record) + 1)
    clock.step()
    assert verdict(record, placement, clock) == "Refusal"


@over_backends
@pytest.mark.parametrize("figure", [True, False])
def test_a_bool_is_no_figure(effort, clock, figure):
    """templet:3161 -- JSON's `true` and `false` are not reservation ids, though
    Python counts them as 1 and 0: a reading that names one is a reading with no
    figure and the clocks decide. `false` as 0 would count a hold made before
    the reading; `true` as 1 would count one made after it and stamped before
    its instant."""
    record = effort
    placement = declared_node(record, clock)
    t = clock()
    clock.step()
    if figure is False:
        placement.hold("vm:early", {"memory": 2 * GIB}, now=t)  # before, seen
    host_reading(record, clock, available=3 * GIB, reserved_through=figure)
    if figure is True:
        placement.hold("vm:late", {"memory": 2 * GIB}, now=t)  # after, stamped before
    clock.step()
    assert verdict(record, placement, clock) == "Fit"


# -- the first failure's message ----------------------------------------------------

RAW = "put failed\nat /tmp/x/store.py\x1b[31m red"
ONE_LINE = "put failed\\nat /tmp/x/store.py\\x1b[31m red"


@pytest.mark.parametrize("why,error", [("op", Unknown), ("check", Unavailable)])
def test_the_first_failures_message_is_written_out_on_one_line(why, error):
    """templet:3159 -- the `Unknown` and the `Unavailable` a Henge record raises
    as it fails carry the store's exception text, which can hold newlines and
    escapes: they are written out on one line, as the refusals after it are, and
    `closed` keeps the reason as it was."""
    from quarrylith.record import henge

    fake = object.__new__(henge.HengeRecord)
    fake.name = "q"
    fake._closed = None
    fake._forget = lambda: None
    fake._shut = lambda: None
    with pytest.raises(error) as caught:
        fake._fail(why, RAW, RuntimeError("boom"))
    text = str(caught.value)
    assert "\n" not in text and "\x1b" not in text
    assert ONE_LINE in text
    if why == "op":
        assert fake.closed == RAW
