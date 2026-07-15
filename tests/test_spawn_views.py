#!/usr/bin/env python3.14
"""Session-record split — spawn fork + views-out-of-store (dev-only gate).

Directly runnable:  python3.14 tests/test_spawn_views.py
Exits non-zero on the first failure; prints "OK — N checks passed".

Covers:
  1. _spawn constructs the right subtype by role — an llm spawn is an LlmSession with its engine,
     a pending `original` ChatRef, an unarmed turn_started_at, and a fresh last_activity; a shell /
     nvim spawn is an OtherSession with none of the llm axis;
  2. spawn_view realizes a live @tx_view tmux session with the view chrome, writes NO store record,
     carries no tags, and logs exactly one event line — and returns the view name;
  3. kill's live-@tx_view fallback ends a view (Q3) and returns None (no record);
  4. _is_view_session reads the @tx_view option, not a record.

Hermetic: a temp $TX_IDE_HOME + a FakeTmux (no live server) + a NoReconcile + a FakeLog.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ["TX_IDE_HOME"] = tempfile.mkdtemp()
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))
sys.path.insert(0, str(HERE))

from _tmux_fakes import FakeTmux, NoReconcile  # noqa: E402

from tx.cli import AttachCommand  # noqa: E402
from tx.service import SessionService  # noqa: E402
from tx.session import Engine, LlmSession, OtherSession, Role, State  # noqa: E402
from tx.spawn import SpawnSpec  # noqa: E402
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


def fresh_service(tmux=None):
    return SessionService(
        store=SessionStore(), tmux=tmux or FakeTmux(), log=FakeLog(), reconciler=NoReconcile()
    )


# ----- 1. _spawn constructs the subtype by role -------------------------------------------------
svc = fresh_service()
llm = svc._spawn(SpawnSpec.for_process(name="auth", tags=["auth"], cwd="/x", cmd="claude foo", engine=Engine.CLAUDE))
check("llm spawn -> LlmSession", isinstance(llm, LlmSession))
check("llm spawn: engine populated", llm.engine == Engine.CLAUDE)
check("llm spawn: one pending original ChatRef", len(llm.chats) == 1 and llm.chats[0].id is None and llm.chats[0].role == "original")
check("llm spawn: turn_started_at unarmed (None)", llm.turn_started_at is None)
check("llm spawn: last_activity set at spawn", llm.last_activity is not None)
check("llm spawn: state IDLE (initial_for llm)", llm.state == State.IDLE)
check("llm spawn: persisted as LlmSession", isinstance(svc.store.load(llm.id), LlmSession))

shell = svc._spawn(SpawnSpec.for_process(name="build", tags=["build"], cwd="/y", cmd="zsh"))
check("shell spawn -> OtherSession", isinstance(shell, OtherSession) and shell.role == Role.SHELL)
check("shell spawn: no last_activity attribute", not hasattr(shell, "last_activity"))
check("shell spawn: no engine attribute", not hasattr(shell, "engine"))
check("shell spawn: chats property is []", shell.chats == [])
check("shell spawn: state ALIVE (initial_for non-llm)", shell.state == State.ALIVE)

nvim = svc._spawn(SpawnSpec.for_nvim(name="nv", tags=["build"], cwd="/y"))
check("nvim spawn -> OtherSession", isinstance(nvim, OtherSession) and nvim.role == Role.NVIM)

# ----- 2. spawn_view: live @tx_view, no record, no tags, one log line ---------------------------
tmux = FakeTmux()
svc = fresh_service(tmux)
records_before = len(svc.store.all())
returned = svc.spawn_view(SpawnSpec.for_view(name="Views", cwd="/repo", cmd="zsh"))
check("spawn_view returns the view name", returned == "Views")
check("spawn_view: live tmux session created", tmux.has_session("Views"))
check("spawn_view: @tx_view marker stamped", tmux.is_view("Views"))
check("spawn_view: view chrome set (status + pane-border)",
      tmux.options.get(("Views", "status")) == "on" and tmux.options.get(("Views", "pane-border-status", "win")) == "top")
check("spawn_view: NO @tx_id stamped on a view", ("Views", "@tx_id") not in tmux.options)
check("spawn_view: writes NO store record", svc.store.find_by_name("Views") is None and len(svc.store.all()) == records_before)
check("spawn_view: exactly one event log line", svc.log.lines == [("spawn-view", "Views /repo")])
# for_view carries no tags at all (Q4)
check("for_view drops tags entirely", SpawnSpec.for_view(name="V", cwd="/", cmd="zsh").tags == [])

# ----- 3. kill's live-@tx_view fallback (Q3) ----------------------------------------------------
killed = svc.kill("Views")
check("kill(view): returns None (no record)", killed is None)
check("kill(view): the live tmux session was killed", "Views" in tmux.killed and not tmux.has_session("Views"))
check("kill(view): logged one kill line", ("kill", "Views") in svc.log.lines)

# ----- 4. _is_view_session reads the @tx_view option --------------------------------------------
tmux = FakeTmux()
svc = fresh_service(tmux)
svc.spawn_view(SpawnSpec.for_view(name="upside-office", cwd="/repo", cmd="zsh"))
attach = AttachCommand(svc)
check("_is_view_session: true for a marked view", attach._is_view_session("upside-office") is True)
check("_is_view_session: false for an unmarked name", attach._is_view_session("not-a-view") is False)
check("_is_view_session: false for the empty string", attach._is_view_session("") is False)

print(f"OK — {PASSED} checks passed")
