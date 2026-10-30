# SPDX-License-Identifier: Apache-2.0
"""What src/quarrylith may import (templet:3159, build step 1): the standard
library and quarrylith, and nothing else. The one exception is the client of a
store, from the one module that reaches it."""

import ast
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
ROOT = "quarrylith"
#: The one module that may import the store's client, and what of it.
STORE_MODULE = "quarrylith/record/henge.py"
STORE_CLIENT = "henge.store.client"


def imported_names(tree):
    """(line, dotted name) of every import: for `from m import n`, both `m`
    and `m.n`. A relative import is quarrylith's own."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom) and not node.level:
            yield node.lineno, node.module
            for alias in node.names:
                yield node.lineno, f"{node.module}.{alias.name}"


def dynamic_imports(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name in ("__import__", "import_module"):
                yield node.lineno, name


def violations(path, source):
    """The imports of `source`, at `path` (relative to src/), that the
    allow-list refuses, one line each."""
    out = []
    for line, name in imported_names(ast.parse(source)):
        top = name.split(".")[0]
        if top == ROOT or top in sys.stdlib_module_names:
            continue
        if top == "henge" and path == STORE_MODULE:
            # henge, henge.store and henge.store.client[.*] only
            parts, client = name.split("."), STORE_CLIENT.split(".")
            if parts == client[: len(parts)] or parts[: len(client)] == client:
                continue
        out.append(f"{path}:{line}: imports {name}")
    for line, name in dynamic_imports(ast.parse(source)):
        out.append(f"{path}:{line}: calls {name}")
    return out


def test_every_module_imports_only_the_standard_library_and_quarrylith():
    """templet:3159 -- every module under src/quarrylith imports only the
    standard library and quarrylith, and henge.store.client only from
    quarrylith/record/henge.py, never henge.store.serve."""
    files = sorted((SRC / ROOT).rglob("*.py"))
    assert files
    found = []
    for path in files:
        rel = path.relative_to(SRC).as_posix()
        found += violations(rel, path.read_text())
    assert found == []


def test_the_allow_list_refuses_what_it_should():
    """templet:3159 -- the check itself: a third-party module, the store's
    client outside its module, the store's server anywhere, and a dynamic
    import are each found; the client in its module, relative imports and the
    standard library are not."""
    other = "quarrylith/record/other.py"
    assert not violations(other, "import json, sqlite3\nfrom quarrylith import x")
    assert not violations(other, "from .schema import RECORDS\nfrom .. import y")
    assert violations(other, "import yaml") == [f"{other}:1: imports yaml"]
    assert violations(other, "import msgpack as m")
    assert violations(other, "from henge.store.client import Store")
    assert violations(other, "import henge.store.client")
    assert violations(STORE_MODULE, "import yaml")
    ok = (
        "from henge.store.client import Store\n"
        "import henge.store.client as c\n"
        "from henge.store import client\n"
    )
    assert violations(STORE_MODULE, ok) == []
    for serve in (
        "import henge.store.serve",
        "from henge.store.serve import local",
        "from henge.store import serve",
        "from henge import store, core",
    ):
        assert violations(STORE_MODULE, serve), serve
    assert violations(other, "x = __import__('yaml')")
    assert violations(other, "import importlib\nimportlib.import_module('yaml')")
