# SPDX-License-Identifier: Apache-2.0
"""`quarrylith alloc declare|show|place|measure|consumer measure`: the
allocator's commands over a record. Every one takes `--timing` (the time and
calls of the store and of the probes, on stderr) and `--debug` (each read,
each short and each write, on stderr), and prints its phases with the elapsed
time once one passes the slow bound.

Exit codes are the command line's: 0 done, 1 an error (a probe that failed),
2 refused (a refusal of the allocator or of the record, a document it cannot
read), 3 the store was unavailable. What the commands print carries figures
and names the record holds; a document's free text and a process's command
line are never in it."""

import dataclasses
import json
import re
import sys
import threading
import time
from contextlib import contextmanager

from .allocator import probes as default_probes
from .allocator import show
from .allocator.document import load_document
from .allocator.host import HostPlacement
from .allocator.measure import began
from .allocator.seam import _size, _when
from .record import contract
from .record.contract import Refused

#: The probes the commands read the host with: None for the real ones. A test
#: puts a stand-in with the same names here.
PROBES = None
#: The clock the phase lines are timed by, and what waits between samples.
CLOCK = time.monotonic
SLEEP = time.sleep

#: How often a phase line comes while sampling, once the first has.
EVERY_S = 30.0
#: The record's operations that write; any other call is a read.
WRITES = frozenset(
    (
        "declare_node",
        "retire_node",
        "set_capacity",
        "reserve",
        "release_reservation",
        "replace_reservation",
        "record_reading",
        "state",
        "note",
        "edit_task",
        "start_run",
        "finish_run",
        "end_task",
    )
)
MIB = 2**20


# -- timing and debug ------------------------------------------------------------------


class Layers:
    """The seconds and the calls of each layer: `store` is the record's reads
    and operations, `probe` the host's commands."""

    def __init__(self):
        self.seconds = {"store": 0.0, "probe": 0.0}
        self.calls = {"store": 0, "probe": 0}

    def add(self, layer, seconds):
        self.seconds[layer] += seconds
        self.calls[layer] += 1

    def line(self):
        parts = [
            f"{layer} {self.seconds[layer] * 1000:.1f} ms ({self.calls[layer]} calls)"
            for layer in ("store", "probe")
        ]
        return "timing: " + ", ".join(parts)


def _arg(value):
    """An argument as the debug lines show it: a figure, a name, a short
    string, a map of figures; never the contents of a document."""
    if value is None or isinstance(value, (bool, int, float)):
        return repr(value)
    if isinstance(value, str):
        return repr(value if len(value) <= 100 else value[:97] + "...")
    if isinstance(value, dict):
        if all(
            isinstance(k, str) and (v is None or isinstance(v, (bool, int)))
            for k, v in value.items()
        ):
            return "{" + ", ".join(f"{k}: {v!r}" for k, v in value.items()) + "}"
        return f"{{{len(value)} keys}}"
    if isinstance(value, (list, tuple)):
        if len(value) <= 8 and all(isinstance(v, (str, int)) for v in value):
            return "[" + ", ".join(_arg(v) for v in value) + "]"
        return f"[{len(value)} items]"
    return f"<{type(value).__name__}>"


class _Timed:
    """A context manager whose entering and leaving are counted to a layer."""

    def __init__(self, inner, layers, layer):
        self.inner, self.layers, self.layer = inner, layers, layer

    def __enter__(self):
        started = time.perf_counter()
        try:
            return self.inner.__enter__()
        finally:
            self.layers.add(self.layer, time.perf_counter() - started)

    def __exit__(self, *exc):
        started = time.perf_counter()
        try:
            return self.inner.__exit__(*exc)
        finally:
            self.layers.add(self.layer, time.perf_counter() - started)


class Traced:
    """An object seen through the commands' eyes: each call on it is timed into
    its layer and, with `debug`, said on stderr as a read or a write."""

    def __init__(self, inner, layers, layer, debug):
        self._inner, self._layers, self._layer, self._debug = (
            inner,
            layers,
            layer,
            debug,
        )

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def call(*args, **kw):
            if self._debug:
                kind = "write" if name in WRITES else "read"
                shown = [_arg(a) for a in args] + [
                    f"{k}={_arg(v)}" for k, v in kw.items()
                ]
                print(f"{kind}: {name}({', '.join(shown)})", file=sys.stderr)
            started = time.perf_counter()
            try:
                made = attr(*args, **kw)
            finally:
                self._layers.add(self._layer, time.perf_counter() - started)
            if name == "reading":
                return _Timed(made, self._layers, self._layer)
            return made

        return call


def _say_shorts(refusal):
    """Each short of a refusal, for `--debug`."""
    for short in refusal.shorts:
        what = short.resource or "every resource"
        figures = (
            f"asked {_size(short.resource, short.asked)}, "
            f"free {_size(short.resource, short.free)}, "
            f"capacity {_size(short.resource, short.capacity)}"
        )
        more = f", {short.detail}" if short.detail else ""
        ending = "never" if not short.waits else "waits"
        if short.waits and short.until is not None:
            ending += f" until {_when(short.until)}"
        print(f"short: {short.cause} {what} {figures}{more}, {ending}", file=sys.stderr)


# -- the session -----------------------------------------------------------------------


def _clock(seconds):
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


class Session:
    """A command's record and placement, traced, and its phase times."""

    def __init__(self, args, readonly):
        from . import cli

        self.args = args
        self.layers = Layers()
        self.times = {}
        self.debug = bool(args.debug)
        self.seen = {}
        with cli._phase("opening the record", self.times):
            self.raw = contract.open_record(
                args.url, actor=args.actor, via="cli", readonly=readonly
            )
        self.record = Traced(self.raw, self.layers, "store", self.debug)
        probes = default_probes if PROBES is None else PROBES
        self.probes = Traced(probes, self.layers, "probe", self.debug)
        self.placement = HostPlacement(self.record, probes=self.probes)

    def now(self):
        given = getattr(self.args, "now", None)
        return given if given is not None else self.raw.now_ms()

    @contextmanager
    def phase(self, name):
        """A phase of the command: timed, said when it ends past the slow
        bound (as the order commands do), and said while it runs once it has
        passed the bound, so a call that hangs is not silent."""
        from . import cli

        started = time.perf_counter()

        def running():
            elapsed = time.perf_counter() - started
            print(f"{name}: still running, {elapsed:.1f} s", file=sys.stderr)

        timer = threading.Timer(cli.SLOW_PHASE_S, running)
        timer.daemon = True
        timer.start()
        with cli._phase(name, self.times):
            try:
                yield
            finally:
                timer.cancel()

    def sampling(self, name, number, total, peak):
        """A consumer sampled `number` of `total` times, `peak` bytes at most:
        the first phase line comes once the slow bound has passed, then one
        every 30 seconds."""
        from . import cli

        mine = self.seen.setdefault(
            name, {"started": CLOCK(), "last": None, "number": 0, "peak": 0}
        )
        mine["number"], mine["peak"] = number, peak
        elapsed = CLOCK() - mine["started"]
        if elapsed < cli.SLOW_PHASE_S:
            return
        if mine["last"] is not None and elapsed - mine["last"] < EVERY_S:
            return
        mine["last"] = elapsed
        print(
            f"sampling {name}: {number}/{total}, peak {_size('memory', peak)}, "
            f"{_clock(elapsed)}",
            file=sys.stderr,
        )

    def close(self):
        self.raw.close()
        if self.args.timing:
            print(self.layers.line(), file=sys.stderr)


@contextmanager
def _session(args, readonly):
    session = Session(args, readonly)
    try:
        yield session
    finally:
        session.close()


# -- reading arguments --------------------------------------------------------------


def _read_document(path):
    try:
        with open(path, encoding="utf-8") as source:
            text = source.read()
    except (OSError, ValueError) as error:
        raise Refused(f"cannot read the allocation document {path}: {error}") from None
    return load_document(text)


def _duration(text, what):
    found = re.fullmatch(r"(\d+)([smh]?)", text.strip())
    if found is None:
        raise Refused(f"{what} is a duration such as 90, 30s, 10m or 2h, not {text!r}")
    return int(found[1]) * {"": 1, "s": 1, "m": 60, "h": 3600}[found[2]]


# -- the commands -------------------------------------------------------------------


def _refused(declaration, args):
    """Print what a declaration was refused for. 2 if it was."""
    for name, refusal in declaration.refusals.items():
        print(f"refused {name}: {refusal.text}")
        if args.debug:
            _say_shorts(refusal)
    for what, why in declaration.failed:
        print(f"failed {what}: {why}")
    if declaration.refusals:
        print("nothing written")
    return 2 if declaration.refusals or declaration.failed else 0


def _declare(args):
    document = _read_document(args.doc)
    with _session(args, readonly=args.dry_run) as s:
        with s.phase("resolving"):
            figures = s.placement.resolve(
                document,
                fresh=args.remeasure,
                now=s.now(),
                progress=s.sampling,
                sleep=SLEEP,
            )
        with s.phase("declaring"):
            got = s.placement.declare(
                document, figures=figures, now=s.now(), dry_run=args.dry_run
            )
        with s.phase("printing"):
            if got.refusals or got.failed:
                return _refused(got, args)
            verb = "would write " if args.dry_run else "wrote "
            for line in got.wrote:
                print(verb + line)
            if not got.wrote:
                print("nothing to write")
    return 0


def _show(args):
    with _session(args, readonly=True) as s:
        with s.phase("reading"):
            snap = show.snapshot(s.record, now=s.now(), pressure=s.placement.pressure())
        with s.phase("printing"):
            if args.json:
                print(json.dumps(snap))
            else:
                for line in show.lines(snap):
                    print(line)
    return 0


def _place(args):
    with _session(args, readonly=True) as s:
        with s.phase("placing"):
            got = s.placement.place(args.task, now=s.now())
        refused = hasattr(got, "shorts")
        if refused and args.debug:
            _say_shorts(got)
        with s.phase("printing"):
            if args.json:
                data = got.as_data() if refused else dataclasses.asdict(got)
                print(json.dumps({"refusal" if refused else "fit": data}))
            elif refused:
                print(got.text)
            else:
                needs = ", ".join(
                    f"{r} {_size(r, n)}" for r, n in sorted(got.needs.items())
                )
                print(f"fit: task {got.task} on {got.node} ({got.run_class})")
                print(f"needs: {needs}")
                print(f"why: {got.why}")
    return 2 if refused else 0


def _measure(args):
    with _session(args, readonly=False) as s:
        with s.phase("measuring"):
            value = s.placement.measure(now=s.now())
        with s.phase("printing"):

            def memory(n):
                return _size("memory", n)

            print(
                f"measured {value['node']}: memory {memory(value['memory_available'])}"
                f" available of {memory(value['memory_total'])} ({value['method']}), "
                f"disk {_size('disk', value['disk_free'])} free"
            )
            for name, found in sorted(value["consumers"].items()):
                print(f"consumer {name}: {memory(found['memory'])} resident")
            for name, why in sorted(value["absent"].items()):
                print(f"absent {name}: {why}")
            for entry in value["drift"]:
                print("drift: " + began(entry).removeprefix("allocator: "))
    return 0


def _consumer_measure(args):
    seconds = _duration(args.window, "--for")
    every = _duration(args.every, "--every")
    if every < 1:
        raise Refused("--every is at least 1 second")
    name = args.name
    with _session(args, readonly=not args.declare) as s:
        if args.declare:
            s.placement.check_resize(name)
        with s.phase("sampling"):
            size = s.placement.sample_consumer(
                name,
                seconds=seconds,
                every=every,
                progress=lambda n, total, top: s.sampling(name, n, total, top),
                sleep=SLEEP,
            )
        mine = s.seen.get(name) or {"number": 0, "peak": 0}
        shown = _size("memory", size)
        if size >= 2**30:
            shown += f" ({size // MIB} MiB)"
        done = (
            f"consumer {name}: peak {_size('memory', mine['peak'])} over "
            f"{mine['number']} samples, size {shown}"
        )
        print(done)
        if not args.declare:
            return 0
        with s.phase("declaring"):
            got = s.placement.resize(name, size, now=s.now())
        if got.refusals or got.failed:
            return _refused(got, args)
        print(f"declared {name} memory {_size('memory', size)}")
        print(f'document line: memory = "{size // MIB}MiB"')
    return 0


# -- the parsers -----------------------------------------------------------------------


def add_commands(groups, actor):
    """Add the `alloc` group to the command line's subparsers."""
    alloc = groups.add_parser("alloc", help="the allocator")
    commands = alloc.add_subparsers(dest="command", required=True)

    def command(parent, name, run, help, url=True):
        p = parent.add_parser(name, help=help)
        if url:
            p.add_argument(
                "url", help="sqlite:///path/record.sqlite or henge:<folder>#<name>"
            )
        p.add_argument("--actor", default=actor, help="who, as you name yourself")
        p.add_argument("--timing", action="store_true", help="time per layer")
        p.add_argument("--debug", action="store_true", help="each read, short, write")
        p.set_defaults(run=run)
        return p

    declare = command(
        commands, "declare", _declare, "declare an allocation document", False
    )
    declare.add_argument("doc", help="the allocation document (TOML)")
    declare.add_argument("url", help="the record's URL")
    declare.add_argument(
        "--dry-run", action="store_true", help="say what it would write; write nothing"
    )
    declare.add_argument(
        "--remeasure", action="store_true", help="probe again, not the recorded figures"
    )
    shown = command(commands, "show", _show, "what is declared, held, free, measured")
    shown.add_argument("--json", action="store_true", help="as one JSON object")
    shown.add_argument("--now", type=int, help="the time, in ms (default: the clock)")
    placed = command(commands, "place", _place, "whether and where one task fits, now")
    placed.add_argument("task", type=int, help="the task's id")
    placed.add_argument("--json", action="store_true", help="as one JSON object")
    placed.add_argument("--now", type=int, help="the time, in ms (default: the clock)")
    command(commands, "measure", _measure, "read the host once and record it")
    consumer = commands.add_parser("consumer", help="a standing consumer")
    sub = consumer.add_subparsers(dest="what", required=True)
    measured = command(
        sub, "measure", _consumer_measure, "sample a consumer's size", False
    )
    measured.add_argument("name", help="the consumer's name")
    measured.add_argument("url", help="the record's URL")
    measured.add_argument("--for", dest="window", default="10m", help="the window")
    measured.add_argument("--every", default="10s", help="between samples")
    measured.add_argument(
        "--declare", action="store_true", help="write the size into the record"
    )
