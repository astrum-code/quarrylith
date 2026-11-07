# SPDX-License-Identifier: Apache-2.0
"""What the services found they need of the allocator and the record
(templet:3161 and templet:3159): `declare` with the drivers the node has, the
measured room net of what was reserved since the reading, and a public way to
ask whether a record has closed. Every test that touches a record runs over
every backend in BACKENDS."""

import re

import pytest
from backends import over_backends
from test_allocator_host import (
    FIGURES,
    GIB,
    host_reading,
    make_task,
    sample_text,
    start,
)
from test_allocator_measure import stub

from quarrylith.allocator import Fit, Refusal, load_document, show
from quarrylith.allocator.host import HostPlacement
from quarrylith.record.contract import Refused

# -- declare with drivers -----------------------------------------------------------


def sample_document():
    return load_document(sample_text())


def node_drivers(record):
    return record.get("nodes", "laptop")["drivers"]


@over_backends
def test_declare_with_drivers_declares_the_node_with_them_and_keeps_the_document(
    record, clock
):
    """templet:3161 -- `declare(document, figures=, drivers=, now=)` declares
    the node with `drivers` in place of the document's, and the reading of the
    document holds the document whole, all its drivers, so `alloc show` can
    tell what is declared from what is available."""
    document = sample_document()
    assert document.drivers == ("harness", "podman", "process", "vm")
    placement = HostPlacement(record)
    got = placement.declare(
        document, figures=FIGURES, drivers=["harness", "process"], now=clock()
    )
    assert got.ok, got
    assert node_drivers(record) == ["harness", "process"]
    stored, _ = placement._stored()
    assert tuple(stored.drivers) == document.drivers
    assert placement.declared()[0].drivers == document.drivers


@over_backends
def test_declare_with_the_same_drivers_again_writes_nothing_and_none_reverts(
    record, clock
):
    """templet:3161 -- a second `declare` with the same document, figures and
    drivers writes nothing (head unchanged); a `declare` with no drivers
    brings the node back to the document's."""
    document = sample_document()
    placement = HostPlacement(record)
    drivers = ["harness", "process"]
    assert placement.declare(document, figures=FIGURES, drivers=drivers, now=clock()).ok
    mark = record.head()
    clock.step()
    again = placement.declare(document, figures=FIGURES, drivers=drivers, now=clock())
    assert again.ok and again.wrote == () and record.head() == mark
    dry = placement.declare(
        document, figures=FIGURES, drivers=drivers, dry_run=True, now=clock()
    )
    assert dry.wrote == (), "a node with those drivers has nothing to write"
    clock.step()
    back = placement.declare(document, figures=FIGURES, now=clock())
    assert back.ok and "node laptop" in back.wrote
    assert node_drivers(record) == sorted(document.drivers)


@over_backends
def test_a_dry_run_with_drivers_says_the_node_write_and_writes_nothing(record, clock):
    """templet:3161 -- `dry_run` with `drivers` returns the node line a real
    `declare` writes, and writes nothing."""
    placement = HostPlacement(record)
    mark = record.head()
    got = placement.declare(
        sample_document(),
        figures=FIGURES,
        drivers=["process"],
        dry_run=True,
        now=clock(),
    )
    assert "node laptop" in got.wrote and record.head() == mark
    assert record.get("nodes", "laptop") is None


@over_backends
def test_a_task_whose_driver_the_node_lacks_is_refused_driver(effort, clock):
    """templet:3161 -- with `vm` left out of the node's drivers, a task of
    sandbox `vm` is refused `driver` and never waits; one of `harness` fits."""
    record = effort
    placement = HostPlacement(record)
    assert placement.declare(
        sample_document(),
        figures=FIGURES,
        drivers=["harness", "podman", "process"],
        now=clock(),
    ).ok
    host_reading(record, clock)
    vm = make_task(record, sandbox="vm")
    answer = placement.place(vm, now=clock())
    assert isinstance(answer, Refusal) and answer.waits is False
    assert [s.cause for s in answer.shorts] == ["driver"]
    assert isinstance(placement.place(make_task(record), now=clock()), Fit)


@over_backends
def test_alloc_show_says_what_is_declared_but_not_available(effort, clock):
    """templet:3161 -- the snapshot of a node declared with fewer drivers than
    the document names lists the ones left out, and the text says so; a node
    with all of them says nothing of it."""
    record = effort
    placement = HostPlacement(record)
    assert placement.declare(
        sample_document(),
        figures=FIGURES,
        drivers=["harness", "podman", "process"],
        now=clock(),
    ).ok
    snap = show.snapshot(record, now=clock())
    assert snap["nodes"][0]["unavailable"] == ["vm"]
    text = show.lines(snap)
    assert (
        "node laptop: drivers harness, podman, process; "
        "declared but not available: vm" in text
    )
    clock.step()
    assert placement.declare(sample_document(), figures=FIGURES, now=clock()).ok
    snap = show.snapshot(record, now=clock())
    assert snap["nodes"][0]["unavailable"] == []
    assert not any("not available" in line for line in show.lines(snap))


# -- the measured room, net of what was reserved since the reading ------------------


def floor_of(floor_memory="1GiB"):
    """The sample with the memory floor lowered, so a test sets the room by the
    reading's available memory alone."""
    text = sample_text()
    text, n = re.subn(r'(\[floor\]\nmemory = )"4GiB"', rf'\1"{floor_memory}"', text)
    assert n == 1
    return load_document(text)


def ready(record, clock, available, disk_free=190 * GIB, floor="1GiB"):
    """A declared node, one host reading of `available` memory taken now, and the
    clock a second on, so what is reserved from here on is after the reading."""
    document = floor_of(floor)
    placement = HostPlacement(record)
    assert placement.declare(document, figures=FIGURES, now=clock()).ok
    host_reading(record, clock, available=available, disk_free=disk_free)
    clock.step()
    return placement


def starts_of(record, placement, clock, count=4):
    """How many of `count` tasks (each wanting 2 GiB of memory and 4 GiB of
    disk) start in one round: each is placed, and a Fit is started."""
    started = 0
    for _ in range(count):
        task = make_task(record)
        answer = placement.place(task, now=clock())
        if isinstance(answer, Fit):
            start(record, task, answer)
            started += 1
    return started


#: (available, starts): the floor is 1 GiB and each task wants 2, so the room
#: is available less 1. At equality it starts; a hair under it does not.
MEMORY = [
    (5 * GIB, 2),  # room 4.0: two
    (5 * GIB - 1, 1),  # room 4.0 less a byte: one
    (4 * GIB + GIB // 2, 1),  # 4.5 GiB: one, not two
    (3 * GIB, 1),  # room 2.0: one
    (3 * GIB - 1, 0),  # room 2.0 less a byte: none
    (2 * GIB, 0),
    (9 * GIB, 4),  # room 8.0: all four
]


@over_backends
@pytest.mark.parametrize("available,starts", MEMORY)
def test_a_round_starts_only_what_the_room_left_by_its_own_starts_holds(
    effort, clock, available, starts
):
    """templet:3161 -- in one round, the measured room is net of what was
    reserved since the reading: with a 1 GiB floor and four candidates of 2 GiB,
    5.0 GiB available starts 2, 4.99 starts 1, 4.5 starts 1, 3.0 starts 1 and
    2.99 starts 0."""
    placement = ready(effort, clock, available)
    assert starts_of(effort, placement, clock) == starts


@over_backends
@pytest.mark.parametrize(
    "disk_free,starts", [(28 * GIB, 2), (28 * GIB - 1, 1), (24 * GIB, 1), (23 * GIB, 0)]
)
def test_the_disk_room_is_net_of_what_was_reserved_since_the_reading_too(
    effort, clock, disk_free, starts
):
    """templet:3161 -- the same for disk: with a 20 GiB floor and tasks of 4
    GiB, 28.0 GiB free starts 2, 27.99 starts 1, 24 starts 1, 23 starts 0."""
    placement = ready(effort, clock, 40 * GIB, disk_free=disk_free)
    assert starts_of(effort, placement, clock) == starts


@over_backends
def test_the_refusal_names_the_floor_and_what_was_reserved_since_the_reading(
    effort, clock
):
    """templet:3161 -- the `measured` short of a start held back by what was
    reserved since the reading has the net room as `free` and a `detail` that
    says the floor and the amount reserved; with nothing reserved it says the
    floor only, as before."""
    record = effort
    placement = ready(record, clock, 5 * GIB - 1)
    first = make_task(record)
    refusal = placement.place(first, now=clock())
    assert isinstance(refusal, Fit)  # room 3.99 holds one
    start(record, first, refusal)
    second = placement.place(make_task(record), now=clock())
    assert isinstance(second, Refusal)
    (short,) = [s for s in second.shorts if s.cause == "measured"]
    assert short.resource == "memory" and short.waits is True
    assert short.free == 5 * GIB - 1 - 2 * GIB
    assert short.detail == "floor 1.0 GiB, 2.0 GiB reserved since the reading"
    assert "2.0 GiB reserved since the reading" in second.text
    # nothing reserved: the floor alone
    bare = ready(record, clock, GIB)
    alone = bare.place(make_task(record, needs={"memory": GIB}), now=clock())
    assert isinstance(alone, Refusal)
    (short,) = [s for s in alone.shorts if s.cause == "measured"]
    assert short.detail == "floor 1.0 GiB"


@over_backends
def test_a_reservation_released_is_not_counted(effort, clock):
    """templet:3161 -- the run that finishes after the reading gives its room
    back: with 3.0 GiB available (room 2) one starts and a second is refused;
    once the first finishes, the second fits."""
    record = effort
    placement = ready(record, clock, 3 * GIB)
    one, two = make_task(record), make_task(record)
    fit = placement.place(one, now=clock())
    run = start(record, one, fit)
    assert isinstance(placement.place(two, now=clock()), Refusal)
    record.finish_run(run, "done", "ok", footprint={}, cost={})
    clock.step()
    assert isinstance(placement.place(two, now=clock()), Fit)
    held = placement.hold("vm:x", {"memory": 2 * GIB}, now=clock())
    assert isinstance(held, list)
    assert isinstance(placement.place(make_task(record), now=clock()), Refusal)
    for r in record.read("reservations", live=True):
        if r["holder"] == "vm:x":
            record.release_reservation(r["id"], "gone")
    assert isinstance(placement.place(make_task(record), now=clock()), Fit)


@over_backends
def test_a_run_the_new_reading_already_sees_is_not_counted_twice(effort, clock):
    """templet:3161 -- a run started after the first reading is in the second
    one's available memory: with 5.0 GiB then 3.0 GiB read after it started
    (room 2), the next candidate fits; counted twice it would not."""
    record = effort
    placement = ready(record, clock, 5 * GIB)
    one = make_task(record)
    start(record, one, placement.place(one, now=clock()))
    clock.step()
    host_reading(record, clock, available=3 * GIB)  # the run now holds its 2
    clock.step()
    assert isinstance(placement.place(make_task(record), now=clock()), Fit)
    # a run started after the second reading is counted again
    two = make_task(record)
    start(record, two, placement.place(two, now=clock()))
    assert isinstance(placement.place(make_task(record), now=clock()), Refusal)


@over_backends
def test_a_reservation_made_at_the_instant_of_the_reading_is_taken_as_seen(
    effort, clock
):
    """templet:3161 -- the named edge: a reservation whose `from_at` is the
    reading's own instant is taken as seen by it (the record keeps no finer
    order than the millisecond), and one a millisecond later is counted."""
    record = effort
    document = floor_of()
    placement = HostPlacement(record)
    assert placement.declare(document, figures=FIGURES, now=clock()).ok
    task = make_task(record)
    host_reading(record, clock, available=3 * GIB)
    start(record, task, placement.place(task, now=clock()))  # at the instant
    assert isinstance(placement.place(make_task(record), now=clock()), Fit)
    clock.step(1)
    other = make_task(record)
    start(record, other, placement.place(other, now=clock()))
    assert isinstance(placement.place(make_task(record), now=clock()), Refusal)


@over_backends
def test_a_consumer_the_document_names_is_not_counted_but_a_new_hold_is(effort, clock):
    """templet:3161 -- a standing consumer of the document held again after the
    reading is already in what the host measured, so it is not taken off the
    room; a hold of new work (a VM held for a task or with no task) is."""
    record = effort
    placement = ready(record, clock, 3 * GIB)
    placement.release("yard", why="resized")
    assert isinstance(
        placement.hold("yard", {"memory": 3 * GIB // 2}, measured=False, now=clock()),
        list,
    )
    assert isinstance(placement.place(make_task(record), now=clock()), Fit)
    assert isinstance(placement.hold("vm:y", {"memory": 2 * GIB}, now=clock()), list)
    assert isinstance(placement.place(make_task(record), now=clock()), Refusal)


@over_backends
def test_a_reservation_that_has_ended_is_not_counted(effort, clock):
    """templet:3161 -- a reservation made after the reading that ends before
    now holds nothing now: with 3.0 GiB available (room 2), a 2 GiB reservation
    over a span that is past leaves the next start its room, and the same span
    still running does not."""
    record = effort
    placement = ready(record, clock, 3 * GIB)
    t = clock()
    record.reserve(
        "laptop", {"memory": 2 * GIB}, holder="vm:t", from_at=t, until_at=t + 2000
    )
    assert isinstance(placement.place(make_task(record), now=clock()), Refusal)
    clock.step(3000)
    assert isinstance(placement.place(make_task(record), now=clock()), Fit)


@over_backends
def test_hold_and_headroom_are_net_of_what_was_reserved_since_the_reading(
    effort, clock
):
    """templet:3161 -- `hold` is checked against the measured room less what was
    reserved since the reading, as `place` is, and `headroom` of memory is the
    least of the table's free and that room."""
    record = effort
    placement = ready(record, clock, 5 * GIB)
    assert placement.headroom(now=clock())["laptop"]["memory"] == 4 * GIB
    one = make_task(record)
    start(record, one, placement.place(one, now=clock()))
    assert placement.headroom(now=clock())["laptop"]["memory"] == 2 * GIB
    refused = placement.hold("vm:z", {"memory": 2 * GIB + 1}, now=clock())
    assert isinstance(refused, Refusal)
    assert [s.cause for s in refused.shorts] == ["measured"]
    assert isinstance(placement.hold("vm:z", {"memory": 2 * GIB}, now=clock()), list)


# -- a record that has closed -------------------------------------------------------


@over_backends
def test_a_record_says_whether_it_has_closed(backend, record, clock):
    """templet:3159 -- `Record.closed` is None while the record is open and a
    line saying why once it is closed, on SQLite and on Henge, and asking
    never raises. `close()` makes it "closed"."""
    assert record.closed is None
    record.close()
    assert record.closed == "closed"
    record.close()  # closing twice is no error and changes nothing
    assert record.closed == "closed"


def test_a_henge_record_that_closed_itself_says_why(tmp_path):
    """templet:3159 -- a Henge record closed by a failure, not by `close()`,
    gives the reason it closed (what it said to the caller): the property is
    the public form of the private field a caller used to read."""
    from quarrylith.record import henge

    assert henge.HengeRecord.closed.fget is not None
    fake = object.__new__(henge.HengeRecord)
    fake._closed = "recovery found journal/[3] and [4]"
    assert fake.closed == "recovery found journal/[3] and [4]"
    fake._closed = None
    assert fake.closed is None


@over_backends
def test_a_resize_keeps_the_drivers_the_node_was_declared_with(effort, clock):
    """templet:3161 -- `resize` declares the document in force again with the
    new figure, and the node keeps the drivers it has, not the document's."""
    record = effort
    placement = HostPlacement(record)
    document = sample_document()
    got = placement.declare(
        document,
        figures=FIGURES,
        drivers=["harness", "process"],
        now=clock(),
    )
    assert got.ok, got
    clock.step()
    sized = placement.resize("yard", 2 * GIB, now=clock())
    assert sized.ok, sized
    assert node_drivers(record) == ["harness", "process"]


# -- what a hold is stamped with, and the order the record keeps --------------------


def measured_round(record, clock, available, floor="4GiB"):
    """A declared node, then a round as the services run it: `now` is taken, the
    host is measured (a reading written at the record's clock, a second on),
    and the holds are made with the `now` taken before the reading. Returns the
    placement and that earlier `now`."""
    placement = HostPlacement(record, probes=stub(available))
    assert placement.declare(floor_of(floor), figures=FIGURES, now=clock()).ok
    earlier = clock()
    clock.step()
    placement.measure(now=clock())
    return placement, earlier


@over_backends
def test_holds_stamped_before_the_reading_are_counted_by_the_records_order(
    effort, clock
):
    """templet:3161 -- a round takes `now`, measures, then holds with that `now`,
    which is earlier than the reading's instant. What was reserved since the
    reading is decided by the record's order, not by two clocks: with a 4 GiB
    floor and 8 GiB available, three holds of 4 GiB made with the earlier `now`
    leave the first granted and the second and third refused."""
    record = effort
    placement, earlier = measured_round(record, clock, 8 * GIB)
    granted = [
        placement.hold(f"vm:{n}", {"memory": 4 * GIB}, now=earlier) for n in "abc"
    ]
    assert isinstance(granted[0], list)
    for refusal in granted[1:]:
        assert isinstance(refusal, Refusal)
        assert [s.cause for s in refusal.shorts] == ["measured"]
        assert "4.0 GiB reserved since the reading" in refusal.text


@over_backends
def test_the_reading_names_the_last_reservation_it_could_see(effort, clock):
    """templet:3161 -- `measure` writes `reserved_through`, the id of the last
    reservation the record held when it measured, and a reservation made
    before the reading but stamped after it is not counted (the order decides),
    where one made after it is, however it is stamped."""
    record = effort
    placement = HostPlacement(record, probes=stub(8 * GIB))
    assert placement.declare(floor_of("4GiB"), figures=FIGURES, now=clock()).ok
    t = clock()
    (early,) = record.reserve(
        "laptop",
        {"memory": 2 * GIB},
        holder="vm:early",
        from_at=t + 60_000,
        until_at=None,
    )
    clock.step()
    value = placement.measure(now=clock())
    assert value["reserved_through"] == max(
        r["id"] for r in record.read("reservations", live=True)
    )
    assert early <= value["reserved_through"]
    # made before the reading: seen by it, though stamped after it
    assert isinstance(
        placement.hold("vm:one", {"memory": 4 * GIB}, now=clock() + 120_000), list
    )
    # made after it: counted, however stamped
    refused = placement.hold("vm:two", {"memory": 1}, now=clock())
    assert isinstance(refused, Refusal)


# -- drivers are validated ----------------------------------------------------------

BAD_DRIVERS = {
    "a string": ("vm", "list of names"),
    "an empty list": ([], "at least one"),
    "a driver the document does not name": (["harness", "docker"], "not drivers"),
    "duplicates": (["harness", "harness"], "each driver once"),
    "a name that is not text": (["harness", 3], "names are text"),
    "a blank name": (["harness", ""], "names are text"),
    "not a list": ({"harness": 1}, "list of names"),
}


@over_backends
@pytest.mark.parametrize("why", sorted(BAD_DRIVERS))
def test_drivers_that_are_not_a_list_of_the_documents_names_are_refused_whole(
    record, clock, why
):
    """templet:3161 -- `drivers` is a list of distinct names the document
    names, and at least one: a string (which would be declared by its
    letters), a driver the document does not name, a duplicate, a blank or
    non-text name and an empty list (a node nothing can run on) are each
    refused plainly with nothing written, a dry run included."""
    placement = HostPlacement(record)
    mark = record.head()
    bad, said = BAD_DRIVERS[why]
    for dry_run in (False, True):
        with pytest.raises(Refused, match=f"drivers.*{said}|{said}"):
            placement.declare(
                sample_document(),
                figures=FIGURES,
                drivers=bad,
                dry_run=dry_run,
                now=clock(),
            )
    assert record.head() == mark and record.get("nodes", "laptop") is None


@over_backends
def test_drivers_may_be_a_tuple_a_subset_or_all_of_the_documents(record, clock):
    """templet:3161 -- a tuple of some of the document's drivers declares them,
    and all of them is the document's own."""
    placement = HostPlacement(record)
    assert placement.declare(
        sample_document(), figures=FIGURES, drivers=("process", "harness"), now=clock()
    ).ok
    assert node_drivers(record) == ["harness", "process"]
    clock.step()
    assert placement.declare(
        sample_document(),
        figures=FIGURES,
        drivers=["vm", "podman", "process", "harness"],
        now=clock(),
    ).ok
    assert node_drivers(record) == ["harness", "podman", "process", "vm"]


# -- the two rules a mutant showed untested -----------------------------------------


@over_backends
def test_only_the_documents_node_lists_drivers_it_lacks(effort, clock):
    """templet:3161 -- the drivers declared but not available are those of the
    node the document names: another node of the record, declared with few
    drivers, lists none."""
    record = effort
    placement = HostPlacement(record)
    assert placement.declare(sample_document(), figures=FIGURES, now=clock()).ok
    record.declare_node("elsewhere", ["harness"], [])
    nodes = {n["name"]: n for n in show.snapshot(record, now=clock())["nodes"]}
    assert nodes["elsewhere"]["unavailable"] == []
    assert nodes["laptop"]["unavailable"] == []
    assert not any(
        "not available" in line
        for line in show.lines(show.snapshot(record, now=clock()))
    )


@over_backends
def test_a_task_hold_under_a_consumers_name_is_still_counted(effort, clock):
    """templet:3161 -- only a standing hold of a consumer the document names is
    left off the room: a hold made for a task, whatever its holder is called,
    is new work and is counted."""
    record = effort
    placement = ready(record, clock, 3 * GIB)
    task = make_task(record)
    held = placement.hold("yard", {"memory": 2 * GIB}, task=task, now=clock())
    assert isinstance(held, list)
    assert isinstance(placement.place(make_task(record), now=clock()), Refusal)


# -- the reason a record closed -----------------------------------------------------


def test_a_closed_henge_records_refusal_writes_its_reason_on_one_line(tmp_path):
    """templet:3159 -- the reason a record closed can hold newlines and paths from
    the store's exceptions; where the record says it is closed, in the
    `Unavailable` a call gets, it is written out on one line, and `closed`
    itself keeps the reason as it was."""
    from quarrylith.record import henge
    from quarrylith.record.contract import Unavailable

    fake = object.__new__(henge.HengeRecord)
    fake.name = "q"
    fake._closed = "put failed\nat /tmp/x/store.py\x1b[31m red"
    with pytest.raises(Unavailable) as caught:
        fake._live()
    text = str(caught.value)
    assert "\n" not in text and "\x1b" not in text
    assert "put failed\\nat /tmp/x/store.py\\x1b[31m red" in text
    assert fake.closed == "put failed\nat /tmp/x/store.py\x1b[31m red"
