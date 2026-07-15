#!/usr/bin/env python3.14
"""Session-record split — hooks narrow to LlmSession (dev-only gate).

Directly runnable:  python3.14 tests/test_hooks_narrow.py
Exits non-zero on the first failure; prints "OK — N checks passed".

hooks.dispatch now loads the firing record and narrows to LlmSession before the capture + state
arms. This gate pins the behavior — including the latent bug the narrow closes:
  1. a non-llm (OtherSession) record that fires a state-driving hook (a hand-run `claude` inside a
     tx SHELL session that leaked its TX_SESSION_ID) is a no-op — it must NOT be driven to WORKING;
  2. an llm (LlmSession) record IS driven to WORKING and its turn_started_at clock is armed;
  3. the capture path still fills the pending ChatRef from the payload for an llm session;
  4. session-closed stays role-agnostic (it just triggers a reconcile).

Hermetic: a temp $TX_IDE_HOME + a FakeTmux + a NoReconcile. No live server, no network.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ["TX_IDE_HOME"] = tempfile.mkdtemp()
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))
sys.path.insert(0, str(HERE))

from _tmux_fakes import FakeTmux, NoReconcile  # noqa: E402

from tx import hooks  # noqa: E402
from tx.service import SessionService  # noqa: E402
from tx.session import Engine, State  # noqa: E402
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


def fresh_service():
    return SessionService(store=SessionStore(), tmux=FakeTmux(), reconciler=NoReconcile())


def feed(payload):
    sys.stdin = io.StringIO(payload if isinstance(payload, str) else json.dumps(payload))


# ----- 1. a non-llm record is never driven to WORKING (the closed bug) --------------------------
svc = fresh_service()
shell = svc._spawn(SpawnSpec.for_process(name="sh", tags=["x"], cwd="/y", cmd="zsh"))
os.environ["TX_SESSION_ID"] = shell.id
feed({})
check("shell: prompt-submit dispatch returns 0", hooks.dispatch(svc, ["prompt-submit"]) == 0)
check("shell: record NOT driven to WORKING (narrow closes the leak)", svc.store.load(shell.id).state == State.ALIVE)

# A record id the home never tracked is likewise a no-op (D4 — load returns None -> not LlmSession).
os.environ["TX_SESSION_ID"] = "not-a-record"
check("unknown id: dispatch is a no-op", hooks.dispatch(svc, ["prompt-submit"]) == 0)

# ----- 2/3. an llm record is driven to WORKING, clock armed, chat captured ----------------------
llm = svc._spawn(SpawnSpec.for_process(name="w", tags=["x"], cwd="/x", cmd="claude foo", engine=Engine.CLAUDE))
os.environ["TX_SESSION_ID"] = llm.id
feed({"session_id": "CID", "transcript_path": "/x/CID.jsonl"})
check("llm: prompt-submit dispatch returns 0", hooks.dispatch(svc, ["prompt-submit"]) == 0)
reloaded = svc.store.load(llm.id)
check("llm: driven to WORKING", reloaded.state == State.WORKING)
check("llm: turn_started_at armed by the WORKING transition", reloaded.turn_started_at is not None)
check("llm: last_activity bumped", reloaded.last_activity is not None)
check("llm: capture filled the pending ChatRef from the payload", reloaded.chats[0].id == "CID")

# ----- 4. session-closed stays role-agnostic (reconcile only) -----------------------------------
check("session-closed: dispatch returns 0 (role-agnostic reconcile arm)", hooks.dispatch(svc, ["session-closed"]) == 0)

print(f"OK — {PASSED} checks passed")
