#!/usr/bin/env python3.14
"""Capture-after-launch conformance (task T4 §3).

No pytest in this repo, so this is directly runnable:  python3.14 tests/test_capture.py
Exits non-zero on the first failure; prints "OK — N checks passed" (same convention as the other
gate tests). This is the behavioral gate for T4's identity switch.

Covers the two T4 acceptance checks that need a behavioral assertion:
  1. a synthetic Claude `SessionStart` / `UserPromptSubmit` hook payload fills a PENDING `ChatRef`
     — id + transcript_path captured from the payload, role/origin preserved, idempotent re-fire,
     role-agnostic (a fork ref is filled too), and a hook that can't capture never fails the turn;
  2. the engine-routed `history.resolve_transcript` finds a deterministic CLAUDE transcript fixture
     (`<claude-home>/projects/<munge>/<id>.jsonl`) and returns None when the chat is not on disk.

Hermetic: a temp `$TX_IDE_HOME` (records + log) + a temp `$CLAUDE_CONFIG_DIR` (the transcript
fixture) + a fake Tmux (no live server). No real spawn, no network, no live home touched.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path

# Point the home + Claude-home at temp dirs BEFORE importing tx (storage / claude_home read these).
os.environ["TX_IDE_HOME"] = tempfile.mkdtemp()
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp()

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from tx import hooks  # noqa: E402
from tx.engines import claude as claude_engine  # noqa: E402
from tx.history import resolve_transcript  # noqa: E402
from tx.service import SessionService  # noqa: E402
from tx.session import ChatRef, Engine, Kind, Origin, Role, Session, State  # noqa: E402
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


class FakeTmux:
    """Just enough Tmux for `record_state` on the capture path: `attached_to` → [] (no live server)."""

    def attached_to(self, name):
        return []


class SpawnTmux(FakeTmux):
    """Enough Tmux for `SessionService._spawn` (no live server): name-free, no parent, a stub pid."""

    def has_session(self, name):
        return False

    def current_session_name(self):
        return None

    def new_session(self, name, cwd, command, env):
        return 4242

    def set_tx_id(self, name, session_id):
        pass


class NoReconcile:
    """A reconciler that does nothing — `_spawn`'s `_require_name_free` calls `reconcile()`."""

    def reconcile(self):
        return []


def fresh_service() -> SessionService:
    """A service over a real (temp-home) store + log, with tmux faked out. The capture path only ever
    touches the store; `prompt-submit` additionally drives state, which reads `tmux.attached_to`."""
    return SessionService(store=SessionStore(), tmux=FakeTmux())


def spawn_service() -> SessionService:
    """A service whose `spawn` is hermetic (faked tmux + reconciler) — for the `records_own_chat`
    contract, which lives in `_spawn`."""
    return SessionService(store=SessionStore(), tmux=SpawnTmux(), reconciler=NoReconcile())


def pending_session(txid: str, *, role: str = "original", how: str = "spawn") -> Session:
    """An llm session holding one PENDING `ChatRef` (`id=None`) — exactly what `_spawn` / chat.py
    write ahead of the first hook."""
    cwd = "/Users/me/proj"
    return Session(
        id=txid, name="w", kind=Kind.PROCESS, role=Role.LLM, state=State.IDLE,
        cwd=cwd, cmd="claude --dangerously-skip-permissions", engine=Engine.CLAUDE,
        created_at=time.time(),
        chats=[ChatRef(id=None, role=role, cwd=cwd, transcript_path="",
                       origin=Origin(how=how, session_id=txid, chat_id=None),
                       started_at=time.time(), engine=Engine.CLAUDE)],
    )


def feed(payload: dict | str) -> None:
    """Stage a hook payload on stdin (the shim leaves it connected for the capture events)."""
    sys.stdin = io.StringIO(payload if isinstance(payload, str) else json.dumps(payload))


# ----- 1. a synthetic payload fills a pending ChatRef -------------------------------------------

# (a) SessionStart — the startup capture (id-unknown window closed before the first turn).
svc = fresh_service()
svc.store.save(pending_session("tx-1"))
os.environ["TX_SESSION_ID"] = "tx-1"
feed({"session_id": "CID-ss", "transcript_path": "/x/CID-ss.jsonl", "hook_event_name": "SessionStart"})
check("session-start: dispatch returns 0", hooks.dispatch(svc, ["session-start"]) == 0)
ref = svc.store.load("tx-1").chats[0]
check("session-start: id captured from payload", ref.id == "CID-ss")
check("session-start: transcript_path captured from payload", ref.transcript_path == "/x/CID-ss.jsonl")
check("session-start: role preserved", ref.role == "original")
check("session-start: origin preserved", ref.origin.how == "spawn")

# (b) idempotent re-fire of the same id — no duplicate, the ref is untouched.
feed({"session_id": "CID-ss", "transcript_path": "/x/CID-ss.jsonl"})
hooks.dispatch(svc, ["session-start"])
check("re-fire: still exactly one ChatRef (idempotent)", len(svc.store.load("tx-1").chats) == 1)

# (c) UserPromptSubmit fills a fresh pending session (the first-turn backstop) AND drives WORKING.
svc = fresh_service()
svc.store.save(pending_session("tx-2"))
os.environ["TX_SESSION_ID"] = "tx-2"
feed({"session_id": "CID-ups", "transcript_path": "/x/CID-ups.jsonl"})
check("prompt-submit: dispatch returns 0", hooks.dispatch(svc, ["prompt-submit"]) == 0)
loaded = svc.store.load("tx-2")
check("prompt-submit: id captured from payload", loaded.chats[0].id == "CID-ups")
check("prompt-submit: still drives WORKING", loaded.state == State.WORKING)

# (d) capture is role-agnostic — a pending FORK ref is filled the same way (no env, no snapshot-diff).
svc = fresh_service()
svc.store.save(pending_session("tx-3", role="fork", how="fork"))
os.environ["TX_SESSION_ID"] = "tx-3"
feed({"session_id": "CID-fork", "transcript_path": "/x/CID-fork.jsonl"})
hooks.dispatch(svc, ["session-start"])
fref = svc.store.load("tx-3").chats[0]
check("fork: pending fork ref filled from payload", fref.id == "CID-fork" and fref.role == "fork")

# (e) nothing pending (the ref is already captured) → capture never appends or clobbers.
svc = fresh_service()
already = pending_session("tx-4")
already.chats[0].id = "already"
already.chats[0].transcript_path = "/x/already.jsonl"
svc.store.save(already)
os.environ["TX_SESSION_ID"] = "tx-4"
feed({"session_id": "OTHER", "transcript_path": "/x/OTHER.jsonl"})
hooks.dispatch(svc, ["session-start"])
chats = svc.store.load("tx-4").chats
check("no pending: unchanged (no append, no clobber)", len(chats) == 1 and chats[0].id == "already")

# (f) an unreadable payload must never fail the turn — the ref simply stays pending for the next hook.
svc = fresh_service()
svc.store.save(pending_session("tx-5"))
os.environ["TX_SESSION_ID"] = "tx-5"
feed("")  # empty stdin → engine.capture_session_id raises → dispatch swallows it
check("empty payload: dispatch still returns 0", hooks.dispatch(svc, ["session-start"]) == 0)
check("empty payload: ref stays pending for the next hook", svc.store.load("tx-5").chats[0].id is None)

# (g) D4 — a session this home never recorded is a no-op (hand-started claude).
svc = fresh_service()
os.environ["TX_SESSION_ID"] = "tx-unknown"
feed({"session_id": "X", "transcript_path": "/x/X.jsonl"})
check("untracked id: dispatch returns 0 (D4 no-op)", hooks.dispatch(svc, ["session-start"]) == 0)

# ----- 2. engine-routed resolve_transcript finds a CLAUDE fixture ------------------------------

cwd = "/Users/me/proj2"
chat_id = "FIXT-1"
project = claude_engine.project_dir(cwd)  # <claude-home>/projects/<munge(realpath(cwd))>
project.mkdir(parents=True, exist_ok=True)
(project / f"{chat_id}.jsonl").write_text("{}\n")

resolved = resolve_transcript(chat_id, cwd, Engine.CLAUDE)
check("resolve_transcript: engine-routed fast path finds the deterministic fixture",
      resolved is not None and resolved.name == f"{chat_id}.jsonl")
check("resolve_transcript: a chat not on disk → None",
      resolve_transcript("NOT-ON-DISK", cwd, Engine.CLAUDE) is None)

# ----- 3. records_own_chat gates the auto original ref (the resume orphan-ref guard) ------------
# A plain llm spawn gets a pending `original` ref from `_spawn`; a chat-op that records its own ref
# (fork / handover / resume) passes `records_own_chat=True` so `_spawn` does NOT also write one —
# otherwise resume (whose `_attach_resumed_chat` appends the known-id ref) is left with an orphan
# pending ref the same-id SessionStart never clears (V-T4 finding, cli.py:359).
plain = spawn_service()._spawn(SpawnSpec.for_process(
    name="plain", tags=[], cwd="/p", cmd="claude --dangerously-skip-permissions"))
check("plain llm spawn: exactly one pending original ref",
      len(plain.chats) == 1 and plain.chats[0].id is None and plain.chats[0].role == "original")

owns = spawn_service()._spawn(SpawnSpec.for_process(
    name="owns", tags=[], cwd="/p", cmd="claude --resume X --dangerously-skip-permissions",
    records_own_chat=True))
check("records_own_chat spawn: NO auto original ref (the op records its own — no orphan)",
      owns.chats == [])

# ----- 4. Claude lazy-fork: the guard holds the source id; the divergent id fills the ref --------
# Claude ≥2.1 mints a `--fork-session`'s own id LAZILY (at the first prompt), so the fork's
# `SessionStart` fires carrying the SOURCE id. The guard must keep the fork ref pending then; the
# fork's first `UserPromptSubmit` (carrying claude's now-divergent id) fills it via the normal
# capture path (V-T4 found the live regression; codex-plan ruling B — pure-payload, no snapshot-diff).
SOURCE_ID = "SRC-aaaa"
FORK_ID = "FORK-bbbb"
svc = fresh_service()
svc.store.save(Session(
    id="tx-fork", name="f", kind=Kind.PROCESS, role=Role.LLM, state=State.IDLE,
    cwd="/p", cmd="claude --resume SRC --fork-session", engine=Engine.CLAUDE, created_at=time.time(),
    chats=[ChatRef(id=None, role="fork", cwd="/p", transcript_path="",
                   origin=Origin(how="fork", session_id="tx-src", chat_id=SOURCE_ID),
                   started_at=time.time(), engine=Engine.CLAUDE)]))
os.environ["TX_SESSION_ID"] = "tx-fork"

# SessionStart carrying the SOURCE id (== origin.chat_id) → the guard keeps the fork ref pending.
feed({"session_id": SOURCE_ID, "transcript_path": f"/x/{SOURCE_ID}.jsonl"})
hooks.dispatch(svc, ["session-start"])
held = svc.store.load("tx-fork").chats[0]
check("lazy-fork: SessionStart carrying the source id leaves the fork ref pending (guard)",
      held.id is None and held.role == "fork")

# The fork's first prompt-submit carries claude's divergent new id → it fills the ref.
feed({"session_id": FORK_ID, "transcript_path": f"/x/{FORK_ID}.jsonl"})
hooks.dispatch(svc, ["prompt-submit"])
done = svc.store.load("tx-fork").chats[0]
check("lazy-fork: a later divergent-id payload fills the fork ref with the fork's own id",
      done.id == FORK_ID and done.transcript_path.endswith(f"{FORK_ID}.jsonl"))

# ----- 5. Ownership guard: a captured id already owned by ANOTHER session is refused (crossover) --
# The fix-maker-accounting incident: a leaked / stale TX_SESSION_ID fired a capture hook for the
# victim session carrying a chat id (SHARED) that actually belonged to a DIFFERENT live session.
# Without the guard it cross-bound — the victim's fork ref got stamped with the other session's chat,
# orphaning the fork's real transcript. The guard refuses any captured id already recorded elsewhere,
# leaving the ref pending so the legitimate divergent payload fills it.
SHARED_ID = "SHARED-cccc"   # owned by the orchestrator session below
VICTIM_ID = "VICT-dddd"     # the victim fork's own (legitimate) divergent id
svc = fresh_service()
# (owner) a long-lived session that already owns SHARED_ID as a real, non-pending chat.
svc.store.save(Session(
    id="tx-orch", name="orch", kind=Kind.PROCESS, role=Role.LLM, state=State.IDLE,
    cwd="/p", cmd="claude", engine=Engine.CLAUDE, created_at=time.time(),
    chats=[ChatRef(id=SHARED_ID, role="original", cwd="/p", transcript_path=f"/x/{SHARED_ID}.jsonl",
                   origin=Origin(how="spawn", session_id="tx-orch", chat_id=None),
                   started_at=time.time(), engine=Engine.CLAUDE)]))
# (victim) a fresh fork session whose ref is still pending. origin.chat_id != SHARED, so the lazy-fork
# guard does NOT short-circuit — we genuinely exercise the ownership guard.
svc.store.save(Session(
    id="tx-victim", name="victim", kind=Kind.PROCESS, role=Role.LLM, state=State.IDLE,
    cwd="/p", cmd="claude --resume SRC --fork-session", engine=Engine.CLAUDE, created_at=time.time(),
    chats=[ChatRef(id=None, role="fork", cwd="/p", transcript_path="",
                   origin=Origin(how="fork", session_id="tx-src", chat_id="SRC-other"),
                   started_at=time.time(), engine=Engine.CLAUDE)]))
os.environ["TX_SESSION_ID"] = "tx-victim"

# crossfire: a capture event for the victim carrying the orchestrator's chat → REFUSED.
feed({"session_id": SHARED_ID, "transcript_path": f"/x/{SHARED_ID}.jsonl"})
check("ownership guard: crossfire dispatch still returns 0", hooks.dispatch(svc, ["session-start"]) == 0)
check("ownership guard: victim ref stays pending (crossfire refused)",
      svc.store.load("tx-victim").chats[0].id is None)
check("ownership guard: the owner session's chat is untouched",
      svc.store.load("tx-orch").chats[0].id == SHARED_ID)

# the victim's own divergent id is NOT owned elsewhere → it fills the ref normally.
feed({"session_id": VICTIM_ID, "transcript_path": f"/x/{VICTIM_ID}.jsonl"})
hooks.dispatch(svc, ["prompt-submit"])
check("ownership guard: the victim's own (un-owned) id still fills the ref",
      svc.store.load("tx-victim").chats[0].id == VICTIM_ID)

print(f"OK — {PASSED} checks passed")
