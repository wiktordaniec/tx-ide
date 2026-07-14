#!/usr/bin/env python3.14
"""D2 — `tx rollover` CLI wrapper must not subscript `ChatOps.rollover()`'s None return (FIX-D1D2).

No pytest in this repo, so this is directly runnable:  python3.14 tests/test_rollover_cli.py
Exits non-zero on the first failure; prints "OK — N checks passed" (the repo convention).

THE BUG (T9-FINDINGS D2, a T4-introduced regression).  T4 made the rollover successor's chat id
captured ASYNC — minted by the successor and read off its FIRST hook payload, after launch — so
`ChatOps.rollover()` now returns `None` (the id is genuinely unknown at rollover time). `RolloverCommand`
did `new_chat[:8]` on that return → `TypeError: 'NoneType' object is not subscriptable` on EVERY run.
The rollover WORK still completed (it is scheduled via a detached `_chat-op-finish` BEFORE the crashing
`print`), so only the CLI message tracebacked — which is why no unit caught it until T9 drove the CLI
end-to-end. The fix drops the id from the message (it can't be known yet) and stops subscripting `None`.

This gate asserts the CLI WRAPPER: `tx rollover` returns exit 0 with a sane, traceback-free message for
BOTH the `--self-catch-up` and the default (distiller) paths, and is engine-agnostic (a codex AND a
claude source). Driving the full detached finish is NOT required (codex-plan) — `_detach` is stubbed to
a no-op so the unit asserts the wrapper, not the async finish (no real subprocess, no pane respawn).

Hermetic: a temp `$TX_IDE_HOME` (+ temp engine homes for the distiller path's ingest) + a fake Tmux.
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ["TX_IDE_HOME"] = tempfile.mkdtemp()
os.environ["CODEX_HOME"] = tempfile.mkdtemp()
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp()

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import tx.chat as chat_module  # noqa: E402
from tx.cli import RolloverCommand  # noqa: E402
from tx.service import SessionService  # noqa: E402
from tx.session import ChatRef, Engine, Kind, Origin, Role, Session, State  # noqa: E402
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


CMD_FOR = {
    Engine.CODEX: ("codex -m gpt-5.5 -c model_reasoning_effort=high "
                   "--dangerously-bypass-approvals-and-sandbox --dangerously-bypass-hook-trust"),
    Engine.CLAUDE: "claude --dangerously-skip-permissions --model opus",
}


class RolloverTmux:
    """Just enough Tmux for `rollover()`'s pane resolution + the distiller path's spawn — no live
    server. `display_message` answers a stub pane id so `_resolve_pane` resolves without `$TMUX_PANE`."""

    def has_session(self, name):
        return False

    def current_session_name(self):
        return None

    def current_pane_path(self):
        return "/tmp"

    def get_tx_id(self, name):
        return None

    def new_session(self, name, cwd, command, env):
        return 4242

    def set_tx_id(self, name, session_id):
        pass

    def attached_to(self, name):
        return []

    def display_message(self, fmt, target=None):
        return "%1"


class NoReconcile:
    def reconcile(self):
        return []


class FakeLog:
    def append(self, *args):
        pass


def service_with_source(txid, engine, cwd):
    now = time.time()
    session = Session(
        id=txid, name=txid, kind=Kind.PROCESS, role=Role.LLM, state=State.IDLE,
        cwd=cwd, cmd=CMD_FOR[engine], engine=engine, created_at=now, last_activity=now,
        chats=[ChatRef(id=f"{txid}-chat", role="original", cwd=cwd, transcript_path="",
                       origin=Origin(how="spawn", session_id=txid, chat_id=None),
                       started_at=now, engine=engine)],
    )
    store = SessionStore()
    store.save(session)
    return SessionService(store=store, tmux=RolloverTmux(), log=FakeLog(), reconciler=NoReconcile())


WORK = tempfile.mkdtemp()
subprocess.run(["git", "-C", WORK, "init", "-b", "main"], check=True,
               stdout=subprocess.DEVNULL)
subprocess.run(["git", "-C", WORK, "config", "user.email", "test@example.com"], check=True)
subprocess.run(["git", "-C", WORK, "config", "user.name", "Test User"], check=True)
(Path(WORK) / "README.md").write_text("fixture\n")
subprocess.run(["git", "-C", WORK, "add", "README.md"], check=True)
subprocess.run(["git", "-C", WORK, "commit", "-m", "fixture"], check=True,
               stdout=subprocess.DEVNULL)

# Stub the detached finish: the unit asserts the CLI wrapper, not the async `_chat-op-finish`
# subprocess (codex-plan: "driving the full detached finish isn't required for the unit").
chat_module.ChatOps._detach = lambda self, argv: None

for engine in (Engine.CODEX, Engine.CLAUDE):
    tag = engine.value
    for variant, argv in (("self-catch-up", ["--self-catch-up"]), ("distiller", [])):
        txid = f"src-{tag}-{variant}"
        service = service_with_source(txid, engine, WORK)
        out = io.StringIO()
        # No try/except: an unhandled TypeError here (the D2 bug) must surface as a test crash, not be
        # swallowed — the gate's whole point is that the wrapper raises nothing.
        with contextlib.redirect_stdout(out):
            rc = RolloverCommand(service).run([txid, *argv])
        message = out.getvalue().strip()
        check(f"tx rollover [{tag}/{variant}]: returns exit 0 (no TypeError on the None return)", rc == 0)
        check(f"tx rollover [{tag}/{variant}]: prints a sane scheduled message",
              message.startswith("Rollover scheduled") and "fresh chat" in message)
        check(f"tx rollover [{tag}/{variant}]: prints NO half-truth id (no 'None', no stray '[:8]')",
              "None" not in message and "[:8]" not in message)

print(f"OK — {PASSED} checks passed")
