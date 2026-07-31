#!/usr/bin/env python3.14
"""Checks for callgraph.py — the cross-file resolution rules.

Directly runnable, no pytest: builds a small fake package in a temp dir,
analyzes it as if its files were nvim buffers, and asserts on the edges.

    python3.14 prototypes/nvim-buffer-graph/test_callgraph.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import callgraph

CHECKS = 0


def check(condition, label):
    global CHECKS
    CHECKS += 1
    if not condition:
        raise SystemExit(f"FAIL: {label}")


FILES = {
    "pkg/__init__.py": "",
    "pkg/guard.py": """
class RunGuard:
    def may_write(self):
        return True

    def may_persist(self):
        return True


def guard_helper():
    return None
""",
    "pkg/handler.py": """
from pkg.guard import RunGuard
from pkg import util


class Handler:
    def __init__(self, guard: RunGuard):
        self.guard = guard

    def capture(self):
        self.guard.may_write()
        util.log_event("captured")
""",
    "pkg/manager.py": """
from .guard import RunGuard, guard_helper
from .handler import Handler
import pkg.util as helpers


class Manager(Handler):
    worker: RunGuard

    def __init__(self):
        self.own_guard = RunGuard()

    def run(self):
        local = RunGuard()
        local.may_persist()
        guard_helper()
        helpers.log_event("ran")
        self.own_guard.missing_method()
""",
    "pkg/util.py": """
def log_event(message):
    print(message)
""",
    "notes.md": "not python\n",
    "broken.py": "def broken(:\n",
}


def main():
    root = Path(tempfile.mkdtemp(prefix="nbg-test-"))
    entries = []
    for relative, source in FILES.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
        entries.append({"path": str(path), "changed": 0, "lastused": 0})

    graph = callgraph.build_graph(entries, root)
    edges = {(e["from_file"], e["from_symbol"], e["to_file"], e["to_symbol"], e["kind"])
             for e in graph["edges"]}

    # Files and symbols.
    by_rel = {f["rel"]: f for f in graph["files"]}
    check(by_rel["notes.md"]["language"] == "md", "non-python passthrough")
    check(by_rel["broken.py"]["error"] is not None, "syntax error surfaced")
    guard_symbols = {s["id"] for s in by_rel["pkg/guard.py"]["symbols"]}
    check(guard_symbols == {"RunGuard", "guard_helper"}, "top-level symbols only")
    methods = {c["id"] for s in by_rel["pkg/guard.py"]["symbols"] if s["kind"] == "class"
               for c in s["children"]}
    check(methods == {"RunGuard.may_write", "RunGuard.may_persist"}, "methods nested")

    # Typed-parameter DI: self.guard = guard (param annotated RunGuard).
    check(("pkg/handler.py", "Handler", "pkg/guard.py", "RunGuard", "has_instance")
          in edges, "has_instance via constructor injection")
    check(("pkg/handler.py", "Handler.capture", "pkg/guard.py", "RunGuard.may_write", "calls")
          in edges, "self.attr.method() via injected type")

    # Module alias imported via ``from pkg import util``.
    check(("pkg/handler.py", "Handler.capture", "pkg/util.py", "log_event", "calls")
          in edges, "module-member call via from-import of submodule")

    # Relative imports + inheritance + direct construction.
    check(("pkg/manager.py", "Manager", "pkg/handler.py", "Handler", "inherits")
          in edges, "inherits across files (relative import)")
    check(("pkg/manager.py", "Manager", "pkg/guard.py", "RunGuard", "has_instance")
          in edges, "has_instance via annotation and construction")
    check(("pkg/manager.py", "Manager.__init__", "pkg/guard.py", "RunGuard", "instantiates")
          in edges, "instantiates in __init__")
    check(("pkg/manager.py", "Manager.run", "pkg/guard.py", "RunGuard", "instantiates")
          in edges, "instantiates local var")
    check(("pkg/manager.py", "Manager.run", "pkg/guard.py", "RunGuard.may_persist", "calls")
          in edges, "constructor-typed local var method call")
    check(("pkg/manager.py", "Manager.run", "pkg/guard.py", "guard_helper", "calls")
          in edges, "imported function call (relative import)")
    check(("pkg/manager.py", "Manager.run", "pkg/util.py", "log_event", "calls")
          in edges, "import-as module alias call")
    # Unknown method on a known class anchors on the class row.
    check(("pkg/manager.py", "Manager.run", "pkg/guard.py", "RunGuard", "calls")
          in edges, "missing method anchors on class")

    # Nothing points at files outside the buffer set, and no self-loops.
    for e in graph["edges"]:
        check(e["from_file"] != e["to_file"], "no intra-file edges")
        check(e["to_file"] in by_rel, "edges stay inside buffer set")

    print(f"OK — {CHECKS} checks passed")


if __name__ == "__main__":
    main()
