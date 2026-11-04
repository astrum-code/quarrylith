# SPDX-License-Identifier: Apache-2.0
"""quarrylith's command line: `quarrylith record
init|check|dump|load|import|events|gauges URL` and `quarrylith order
schedule TEXT`.

Exit codes: 0 done, 1 `check` found something, 2 refused, 3 the store was
unavailable (or the outcome is unknown). `load` and `import` refuse a dump or
a document before they open the record, so a refused one writes nothing and
makes no file.
"""

import argparse
import getpass
import json
import os
import sys
import time
from pathlib import Path

from . import gauges
from .order import schedule as schedules
from .record import contract, importing


def _open(args, **kw):
    kw.setdefault("via", "cli")
    return contract.open_record(args.url, actor=args.actor, **kw)


def _init(args):
    record = _open(args)
    try:
        record.init()
    finally:
        record.close()
    print(f"initialised {args.url}")
    return 0


def _check(args):
    record = _open(args, readonly=True)
    try:
        findings = record.check()
    finally:
        record.close()
    for finding in findings:
        print(finding)
    if findings:
        return 1
    print("ok")
    return 0


def _write_private(path, text):
    """Write `text` to `path`, replacing it whole: a temporary file created
    0600 beside it, then renamed onto it."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(text)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _dump(args):
    record = _open(args, readonly=True)
    try:
        text = json.dumps(record.dump(), ensure_ascii=False) + "\n"
    finally:
        record.close()
    if args.out:
        _write_private(args.out, text)
    else:
        sys.stdout.write(text)
    return 0


def _read_json(path, what):
    try:
        if path == "-":
            return json.load(sys.stdin)
        with open(path, encoding="utf-8") as source:
            return json.load(source)
    except (OSError, ValueError) as e:
        raise contract.Refused(f"cannot read the {what} {path}: {e}") from None


def _load_into(args, dump, via):
    """Load a whole dump (checked before this opens anything) into the
    record, making it if there is none. Its head."""
    record = _open(args, via=via)
    try:
        if not record.initialised:
            record.init()
        record.load(dump)
        return record.head()
    finally:
        record.close()


def _load(args):
    dump = _read_json(args.file, "dump")
    contract.check_dump(dump)
    head = _load_into(args, dump, "load")
    print(f"loaded {head} events into {args.url}")
    return 0


def _import(args):
    doc = _read_json(args.file, "import document")
    dump = importing.compose(doc)
    contract.check_dump(dump)
    head = _load_into(args, dump, "import")
    (imported,) = dump["records"]["imports"]
    counts, tables = imported["counts"], imported["source"]["tables"]
    print(f"imported {head} events into {args.url}")
    rows = [("table", "read", "pulled", "left")] + [
        (t, n, counts["pulled"].get(t, 0), counts["left"].get(t, 0))
        for t, n in sorted(tables.items())
    ]
    wide = max(len(str(r[0])) for r in rows)
    for name, *numbers in rows:
        print(f"{name:<{wide}}" + "".join(f" {n:>7}" for n in numbers))
    for column, count in counts["columns"].items():
        print(f"dropped {column} {count}")
    return 0


def _quoted(value):
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return '"' + text.replace("\n", "\\n") + '"'


def _gauges(args):
    record = _open(args, readonly=True)
    try:
        now = args.now if args.now is not None else record.now_ms()
        window_ms = int(args.window_h * 3_600_000)
        got = gauges.measure(record, now=now, window_ms=window_ms)
    finally:
        record.close()
    if args.json:
        out = {
            name: [{"labels": labels, "value": value} for labels, value in rows]
            for name, rows in got.items()
        }
        print(json.dumps({"now": now, "window_ms": window_ms, "gauges": out}))
        return 0
    for gauge in gauges.GAUGES:
        print(f"# HELP {gauge.name} {gauge.help}")
        print(f"# UNIT {gauge.name} {gauge.unit}")
        for labels, value in got[gauge.name]:
            shown = ",".join(f"{k}={_quoted(v)}" for k, v in labels.items())
            print(f"{gauge.name}{{{shown}}} {value}")
    return 0


def _events(args):
    record = _open(args, readonly=not args.ack)
    try:
        if args.after is not None:
            cursor = args.after
        elif args.consumer:
            cursor = record.cursor(args.consumer)
        else:
            cursor = 0
        got = record.events(
            cursor, limit=args.limit, wait=args.wait, subject=args.subject
        )
        for event in got:
            sys.stdout.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
        sys.stdout.flush()
        if args.ack and got:
            record.ack(args.consumer, got[-1].seq)
    finally:
        record.close()
    return 0


def _order_schedule(args):
    started = time.perf_counter()
    schedule = schedules.parse(args.text)
    parsed = time.perf_counter()
    if args.debug:
        print(f"schedule: {schedule}", file=sys.stderr)
    start = args.start if args.start is not None else int(time.time() * 1000)
    fires = []
    while schedule.cron is not None and len(fires) < args.count:
        fires.append(schedule.next_fire(fires[-1] if fires else start))
    opens = schedules.openings(schedule, start, args.count)
    done = time.perf_counter()
    if schedule.cron is None:
        print("no cron")
    for at in fires:
        print(f"fire {schedule.local_iso(at)} {at}")
    if not schedule.windows:
        print("no window")
    for at in opens:
        print(f"open {schedule.local_iso(at)} {at}")
    if args.timing:
        print(
            f"timing: parse {(parsed - started) * 1000:.1f} ms, "
            f"next {(done - parsed) * 1000:.1f} ms",
            file=sys.stderr,
        )
    return 0


def _actor():
    try:
        return getpass.getuser()
    except Exception:
        return "cli"


def parser():
    top = argparse.ArgumentParser(prog="quarrylith")
    groups = top.add_subparsers(dest="group", required=True)
    record = groups.add_parser("record", help="the record")
    commands = record.add_subparsers(dest="command", required=True)

    def command(name, run, help):
        p = commands.add_parser(name, help=help)
        p.add_argument("url", help="sqlite:///path/record.sqlite")
        p.add_argument("--actor", default=_actor(), help="who, as you name yourself")
        p.set_defaults(run=run)
        return p

    command("init", _init, "make an empty record")
    command("check", _check, "replay the journal and check the record")
    command("dump", _dump, "the record as JSON").add_argument(
        "--out", help="write it to this file, 0600, replacing it whole"
    )
    command("load", _load, "load a dump into an empty record").add_argument(
        "file", help="the dump, or - for stdin"
    )
    imports = commands.add_parser(
        "import", help="check an import document and load it into an empty record"
    )
    imports.add_argument("file", help="the import document, or - for stdin")
    imports.add_argument("--into", dest="url", required=True, help="the record's URL")
    imports.add_argument("--actor", default=_actor(), help="who, as you name yourself")
    imports.set_defaults(run=_import)
    gauged = command("gauges", _gauges, "every gauge's series, computed now")
    gauged.add_argument("--now", type=int, help="the time, in ms (default: the clock)")
    gauged.add_argument(
        "--window-h", type=float, default=24.0, help="the window, in hours"
    )
    gauged.add_argument("--json", action="store_true", help="as one JSON object")
    events = command("events", _events, "the journal's events, one JSON per line")
    events.add_argument("--after", type=int, help="the seq to start after")
    events.add_argument("--consumer", help="start after this consumer's cursor")
    events.add_argument(
        "--ack", action="store_true", help="then move the consumer's cursor"
    )
    events.add_argument("--limit", type=int, default=500)
    events.add_argument("--wait", type=float, default=0.0, help="seconds to wait")
    events.add_argument("--subject", help="only this subject's events")
    order = groups.add_parser("order", help="the order")
    ordered = order.add_subparsers(dest="command", required=True)
    scheduled = ordered.add_parser(
        "schedule", help="the next fires and window openings of a schedule"
    )
    scheduled.add_argument("text", help='e.g. "cron 0 22 * * mon-fri; tz UTC"')
    scheduled.add_argument(
        "--from", dest="start", type=int, help="the time, in ms (default: the clock)"
    )
    scheduled.add_argument("--count", type=int, default=3, help="how many of each")
    scheduled.add_argument("--timing", action="store_true", help="time per layer")
    scheduled.add_argument("--debug", action="store_true", help="what was parsed")
    scheduled.set_defaults(run=_order_schedule)
    return top


def main(argv=None):
    args = parser().parse_args(argv)
    if getattr(args, "ack", False) and not args.consumer:
        print("refused: --ack needs --consumer", file=sys.stderr)
        return 2
    try:
        return args.run(args)
    except importing.ImportRefused as e:
        print("refused: the import document is not whole:", file=sys.stderr)
        for problem in e.problems:
            print(f"  {problem}", file=sys.stderr)
        return 2
    except schedules.ScheduleError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    except contract.Refused as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    except contract.Unknown as e:
        print(f"unknown: {e}", file=sys.stderr)
        return 3
    except contract.Unavailable as e:
        print(f"unavailable: {e}", file=sys.stderr)
        return 3
