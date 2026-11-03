# SPDX-License-Identifier: Apache-2.0
"""`quarrylith record init|check|dump|load|import|events|gauges`
(templet:3159, build steps 2 and 4), on the SQLite backend."""

import json
import sqlite3
import subprocess
import sys

from samples import SAMPLE, load_sample, make_doc

from quarrylith import cli
from quarrylith.record.contract import open_record
from quarrylith.record.importing import compose


def run(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_init_load_dump_check_and_events(tmp_path, capsys):
    """templet:3159 -- the command line inits a record, loads a dump into it,
    dumps it back the same, checks it and prints its events."""
    url = f"sqlite:///{tmp_path / 'record.sqlite'}"
    dump = compose(make_doc())
    source = tmp_path / "dump.json"
    source.write_text(json.dumps(dump))

    assert run(capsys, "record", "init", url)[0] == 0
    code, _, err = run(capsys, "record", "init", url)
    assert code == 2 and "exists" in err
    assert run(capsys, "record", "load", url, str(source))[0] == 0
    code, out, _ = run(capsys, "record", "dump", url)
    assert code == 0 and json.loads(out) == dump
    out_file = tmp_path / "out.json"
    assert run(capsys, "record", "dump", url, "--out", str(out_file))[0] == 0
    assert json.loads(out_file.read_text()) == dump
    assert (out_file.stat().st_mode & 0o077) == 0
    code, out, _ = run(capsys, "record", "check", url)
    assert code == 0 and "ok" in out

    code, out, _ = run(capsys, "record", "events", url, "--after", "2", "--limit", "3")
    seqs = [json.loads(line)["seq"] for line in out.splitlines()]
    assert code == 0 and seqs == [3, 4, 5]
    code, out, _ = run(
        capsys,
        "record",
        "events",
        url,
        "--consumer",
        "follower",
        "--limit",
        "2",
        "--ack",
    )
    assert [json.loads(line)["seq"] for line in out.splitlines()] == [1, 2]
    code, out, _ = run(
        capsys, "record", "events", url, "--consumer", "follower", "--limit", "2"
    )
    assert [json.loads(line)["seq"] for line in out.splitlines()] == [3, 4]
    record = open_record(url, actor="alpha", via="cli", readonly=True)
    assert record.cursor("follower") == 2
    record.close()


def test_check_exits_one_with_findings_and_load_refuses_a_bad_dump(tmp_path, capsys):
    """templet:3159 -- check exits 1 when it has findings, and a refused load
    exits 2 and writes nothing: refused before the record is made, it leaves
    no file."""
    url = f"sqlite:///{tmp_path / 'record.sqlite'}"
    path = tmp_path / "record.sqlite"
    dump = compose(make_doc())
    dump["records"]["items"] = dump["records"]["items"][:1]
    source = tmp_path / "bad.json"
    source.write_text(json.dumps(dump))
    code, _, err = run(capsys, "record", "load", url, str(source))
    assert code == 2 and "refused" in err
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bad.json"]
    good = compose(make_doc())
    source.write_text(json.dumps(good))
    assert run(capsys, "record", "load", url, str(source))[0] == 0
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("DROP TRIGGER items_no_delete")
    conn.execute("DELETE FROM items WHERE number = 2")
    conn.close()
    code, out, _ = run(capsys, "record", "check", url)
    assert code == 1 and "items" in out


def test_a_refused_import_leaves_no_file(tmp_path, capsys):
    """templet:3159 -- `record import` refuses before anything is written: a
    document it cannot read, or one `check_import` refuses (exit 2, every
    fault named), leaves no record file."""
    url = f"sqlite:///{tmp_path / 'record.sqlite'}"
    doc = make_doc()
    doc["source"]["tables"]["extra"] = 4
    doc["pulled"][0]["record_key"] = [99]
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(doc))
    garbled = tmp_path / "garbled.json"
    garbled.write_text("{not json")
    code, out, err = run(capsys, "record", "import", str(bad), "--into", url)
    assert code == 2 and out == ""
    assert "extra" in err and "99" in err
    code, out, err = run(capsys, "record", "import", str(garbled), "--into", url)
    assert code == 2 and err.startswith("refused: cannot read the import document")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bad.json", "garbled.json"]


def test_load_exits_two_for_a_dump_it_cannot_read(tmp_path, capsys):
    """A dump file that is not there, or is not JSON, is refused: exit 2 with
    one line, no traceback, and no record made."""
    url = f"sqlite:///{tmp_path / 'record.sqlite'}"
    garbled = tmp_path / "garbled.json"
    garbled.write_text("{not json")
    for source in (tmp_path / "missing.json", garbled):
        code, out, err = run(capsys, "record", "load", url, str(source))
        assert code == 2 and out == ""
        assert err.startswith(f"refused: cannot read the dump {source}: ")
        assert len(err.splitlines()) == 1
    assert not (tmp_path / "record.sqlite").exists()


def test_the_module_runs_as_a_command(tmp_path):
    """templet:3159 -- `python -m quarrylith record init URL` works."""
    url = f"sqlite:///{tmp_path / 'record.sqlite'}"
    done = subprocess.run(
        [sys.executable, "-m", "quarrylith", "record", "init", url],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr


def test_gauges_prints_every_gauge(tmp_path, capsys):
    """templet:3159 -- `record gauges` prints every gauge's series at a given
    time, as text or as JSON."""
    url = f"sqlite:///{tmp_path / 'record.sqlite'}"
    assert run(capsys, "record", "import", str(SAMPLE), "--into", url)[0] == 0
    now = load_sample()["source"]["read_at"]
    code, out, err = run(
        capsys, "record", "gauges", url, "--now", str(now), "--window-h", "720"
    )
    assert code == 0, err
    assert 'unmerged_changes{repo="widgets"} 1' in out.splitlines()
    assert 'outcomes{kind="agent",outcome="pr"} 2' in out.splitlines()
    for name in ("capacity", "stalls", "budget_pace"):
        assert f"# HELP {name} " in out
    code, out, _ = run(
        capsys,
        "record",
        "gauges",
        url,
        "--now",
        str(now),
        "--window-h",
        "720",
        "--json",
    )
    got = json.loads(out)
    assert got["now"] == now and got["window_ms"] == 720 * 3_600_000
    assert got["gauges"]["unmerged_changes"] == [
        {"labels": {"repo": "widgets"}, "value": 1}
    ]
