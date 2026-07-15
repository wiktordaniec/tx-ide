#!/usr/bin/env python3.14
"""Session-record split — reconcile C5 clock + render (dev-only gate).

Directly runnable:  python3.14 tests/test_reconcile_render.py
Exits non-zero on the first failure; prints "OK — N checks passed".

Covers:
  1. the C5 stuck-WORKING demote now reads the llm-only turn_started_at, not last_activity:
     a WORKING LlmSession past the threshold under a non-agent pane is demoted; one left WORKING
     across the split (turn_started_at=None) is skipped; and a non-llm OtherSession is never touched
     (the state!=WORKING guard short-circuits before the llm-only field would be read — no crash);
  2. render_ls is a single PROCESSES listing (no VIEWS section), newest-activity first;
  3. the IDLE cell shows a real last-turn age for an llm row and `—` for a non-llm row, in both
     `tx ls` (_idle_cell) and the picker feed;
  4. both listings sort by the base activity_at property.

Hermetic: a temp $TX_IDE_HOME + a FakeTmux + a FakeLog. render is pure (no I/O).
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["TX_IDE_HOME"] = tempfile.mkdtemp()
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))
sys.path.insert(0, str(HERE))

from _tmux_fakes import FakeTmux  # noqa: E402

from tx.reconcile import Reconciler, _Live  # noqa: E402
from tx.render import _idle_cell, picker_display_rows, render_ls  # noqa: E402
from tx.session import Engine, LlmSession, OtherSession, Role, State  # noqa: E402
from tx.storage import ensure_home  # noqa: E402
from tx.store import SessionStore  # noqa: E402

ensure_home()
PASSED = 0


def check(label, condition):
    global PASSED
    if condition:
        PASSED += 1
        return
    print(f"FAIL: {label}")
    sys.exit(1)


class FakeLog:
    def __init__(self):
        self.lines = []

    def append(self, event, detail):
        self.lines.append((event, detail))


NOW = time.time()
THRESHOLD = 600.0


def reconciler():
    return Reconciler(SessionStore(), FakeTmux(), FakeLog())


def working_llm(turn_started_at):
    return LlmSession(
        id="w", name="w", state=State.WORKING, engine=Engine.CLAUDE,
        created_at=NOW - 5000, last_activity=NOW - 5000, turn_started_at=turn_started_at,
    )


# ----- 1. C5 clock reads turn_started_at --------------------------------------------------------
rec = reconciler()
stuck = working_llm(NOW - 5000)  # armed long ago
check("C5: stuck WORKING (old turn_started_at, non-agent pane) is demoted to IDLE",
      rec._demote_if_stuck(stuck, _Live(name="w", command="zsh"), THRESHOLD) is True and stuck.state == State.IDLE)

fresh = working_llm(NOW)  # just armed
check("C5: a recent turn (within threshold) is not demoted",
      rec._demote_if_stuck(fresh, _Live(name="w", command="zsh"), THRESHOLD) is False)

unarmed = working_llm(None)  # left WORKING across the split
check("C5: turn_started_at=None (pre-split) is skipped",
      rec._demote_if_stuck(unarmed, _Live(name="w", command="zsh"), THRESHOLD) is False)

agent_up = working_llm(NOW - 5000)
check("C5: an agent still up in the pane is not demoted",
      rec._demote_if_stuck(agent_up, _Live(name="w", command="claude"), THRESHOLD) is False)

# The safety property: a non-llm OtherSession has no turn_started_at, but the state!=WORKING guard
# short-circuits before that field is read — so this must return False, not AttributeError.
shell = OtherSession(id="s", name="s", state=State.ALIVE, role=Role.SHELL, created_at=NOW - 5000)
check("C5: a non-llm session is never touched (no crash on the missing llm field)",
      rec._demote_if_stuck(shell, _Live(name="s", command="zsh"), THRESHOLD) is False)

# ----- 2/3/4. render_ls + picker: processes-only, activity_at sort, IDLE cells -------------------
llm = LlmSession(id="l", name="auth", state=State.WAITING, cwd="/x", initial_cmd="claude",
                 engine=Engine.CLAUDE, tags=["auth"], created_at=NOW - 300, last_activity=NOW - 30)
old_shell = OtherSession(id="o", name="build", state=State.ALIVE, cwd="/y", initial_cmd="zsh",
                         role=Role.SHELL, tags=["build"], created_at=NOW - 900)

ls = render_ls([old_shell, llm], now=NOW)
check("render_ls: single PROCESSES section, no VIEWS", ls.startswith("PROCESSES") and "VIEWS" not in ls)
check("render_ls: lists both sessions", "auth" in ls and "build" in ls)
# newest-activity first: llm (30s ago) before shell (spawned 900s ago)
check("render_ls: sorted by activity_at (llm before older shell)", ls.index("auth") < ls.index("build"))

check("_idle_cell: llm shows a real age", _idle_cell(llm, NOW) == "30s")
check("_idle_cell: non-llm shows —", _idle_cell(old_shell, NOW) == "—")

rows = picker_display_rows([old_shell, llm], namew=18, now=NOW).splitlines()
check("picker: one row per session", len(rows) == 2)
check("picker: activity_at order (llm row first)", "auth" in rows[0] and "build" in rows[1])
check("picker: llm row shows a real idle age", "30s" in rows[0])
check("picker: non-llm row shows — for idle", "—" in rows[1])

print(f"OK — {PASSED} checks passed")
