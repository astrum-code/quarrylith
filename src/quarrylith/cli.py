# SPDX-License-Identifier: Apache-2.0
"""quarrylith's command line: `quarrylith record init|check|dump|load|events
URL`.

Exit codes: 0 done, 1 `check` found something, 2 refused, 3 the store was
unavailable (or the outcome is unknown).
"""

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

from .record import contract


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


def _load(args):
    if args.file == "-":
        dump = json.load(sys.stdin)
    else:
        with open(args.file, encoding="utf-8") as source:
            dump = json.load(source)
    record = _open(args, via="load")
    try:
        if not record.initialised:
            record.init()
        record.load(dump)
        head = record.head()
    finally:
        record.close()
    print(f"loaded {head} events into {args.url}")
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
    events = command("events", _events, "the journal's events, one JSON per line")
    events.add_argument("--after", type=int, help="the seq to start after")
    events.add_argument("--consumer", help="start after this consumer's cursor")
    events.add_argument(
        "--ack", action="store_true", help="then move the consumer's cursor"
    )
    events.add_argument("--limit", type=int, default=500)
    events.add_argument("--wait", type=float, default=0.0, help="seconds to wait")
    events.add_argument("--subject", help="only this subject's events")
    return top


def main(argv=None):
    args = parser().parse_args(argv)
    if getattr(args, "ack", False) and not args.consumer:
        print("refused: --ack needs --consumer", file=sys.stderr)
        return 2
    try:
        return args.run(args)
    except contract.Refused as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    except contract.Unknown as e:
        print(f"unknown: {e}", file=sys.stderr)
        return 3
    except contract.Unavailable as e:
        print(f"unavailable: {e}", file=sys.stderr)
        return 3
