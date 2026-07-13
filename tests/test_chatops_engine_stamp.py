#!/usr/bin/env python3.14
"""D1 record-engine stamp gate — the chat-op spawn→RECORD path (FIX-D1D2, the T9-FAIL hole).

No pytest in this repo, so this is directly runnable:  python3.14 tests/test_chatops_engine_stamp.py
Exits non-zero on the first failure; prints "OK — N checks passed" (the repo convention).

THE HOLE (codex-plan ruling, T9-FINDINGS D1).  `chat.py` fork / handover / distiller built their
launch COMMAND via the source engine's adapter (so the command was a correct `codex …`) but
constructed `SpawnSpec.for_process(...)` **without `engine=`** — so `service._spawn` defaulted the new
llm record to `Engine.CLAUDE`. A Codex source therefore minted a record stamped `engine=claude`, which
breaks engine-routed history ingest (`resolve_transcript` runs Claude's `~/.claude/projects/*` formula,
never the Codex rollout glob → `None` → an EMPTY bundle) and would later build `claude --resume`.

WHY THE EXISTING SUITES MISSED IT.  `test_chatops_differential` drives the same four sites, but its fake
`spawn()` HARDCODES `engine = Engine.CLAUDE if role == LLM else None` — so by construction it asserts
command-BUILDING, never the record stamp. This gate closes that hole by driving the **REAL**
`SessionService._spawn` (a fake Tmux, no live server, no real process) so `spec.engine` is actually
honored end-to-end: a codex source's fork / handover / distiller mints `record.engine == CODEX` AND a
new `ChatRef.engine == CODEX` (and a claude source still mints CLAUDE — the no-regression). The bonus
(codex-plan): the stamp is load-bearing for ingest — a captured codex fork ingests a NON-EMPTY bundle,
where the old `engine=claude` mis-stamp resolved to `None`.

Hermetic: a temp `$TX_IDE_HOME` (records + history) + a temp `$CODEX_HOME`/`$CLAUDE_CONFIG_DIR` (the
resolvers' homes) + a fake Tmux. No real spawn, no live home touched.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

# Point every home at a temp dir BEFORE importing tx (storage + the engine resolvers read these).
os.environ["TX_IDE_HOME"] = tempfile.mkdtemp()
os.environ["CODEX_HOME"] = tempfile.mkdtemp()
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp()

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from tx import history  # noqa: E402
from tx.chat import ChatOps, ChatOpSpec  # noqa: E402  (imports tx.spawn → registers the adapters)
from tx.history import resolve_transcript  # noqa: E402
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


# The per-engine source launch commands (the adapter shapes — see test_spawn_wiring / verification §5).
CODEX_CMD = ("codex -m gpt-5.6-sol -c model_reasoning_effort=high "
             "--dangerously-bypass-approvals-and-sandbox --dangerously-bypass-hook-trust")
CLAUDE_CMD = "claude --dangerously-skip-permissions --model opus"

CMD_FOR = {Engine.CODEX: CODEX_CMD, Engine.CLAUDE: CLAUDE_CMD}
# An engine-DISTINCTIVE flag present in every one of this engine's op commands (fork / handover /
# distiller) and never the other's — proves we actually drove the source engine's adapter (not just
# defaulted), alongside the record-stamp assertion. Codex carries `-c model_reasoning_effort=`; Claude
# carries `--dangerously-skip-permissions` (Codex uses `--dangerously-bypass-*` instead).
COMMAND_MARK = {Engine.CODEX: "model_reasoning_effort", Engine.CLAUDE: "--dangerously-skip-permissions"}


class StampTmux:
    """Just enough Tmux for `SessionService._spawn` + the chat-op resolution paths — no live server.
    Every method a fork / handover-finish / distiller spawn touches, answering as an empty server."""

    def has_session(self, name):
        return False

    def current_session_name(self):
        return None

    def current_pane_path(self):
        return "/tmp"

    def get_tx_id(self, name):
        return None  # no live @tx_id — records resolve by store name (D7)

    def new_session(self, name, cwd, command, env):
        return 4242  # a stub pid; the launch command is captured on the record (Session.cmd)

    def set_tx_id(self, name, session_id):
        pass

    def attached_to(self, name):
        return []

    def display_message(self, fmt, target=None):
        return "%1"


class NoReconcile:
    """`_unique_name` / `_require_name_free` call `reconcile()`; nothing to reconcile in a fake server."""

    def reconcile(self):
        return []


class FakeLog:
    def append(self, *args):
        pass


def service_over(*sessions):
    """A REAL `SessionService` (so the REAL `_spawn` runs, honoring `spec.engine`) over a temp store
    pre-loaded with the given source records, a fake Tmux, and a no-op reconciler."""
    store = SessionStore()
    for session in sessions:
        store.save(session)
    return SessionService(store=store, tmux=StampTmux(), log=FakeLog(), reconciler=NoReconcile())


def source_session(txid, name, engine, chat_id, cwd):
    """An IDLE llm source with one real (capturable) `original` chat — the thing fork/handover read."""
    now = time.time()
    return Session(
        id=txid, name=name, kind=Kind.PROCESS, role=Role.LLM, state=State.IDLE,
        cwd=cwd, cmd=CMD_FOR[engine], engine=engine, created_at=now, last_activity=now,
        chats=[ChatRef(id=chat_id, role="original", cwd=cwd, transcript_path="",
                       origin=Origin(how="spawn", session_id=txid, chat_id=None),
                       started_at=now, engine=engine)],
    )


def chat_of(session, role):
    return next((chat for chat in session.chats if chat.role == role), None)


WORK = tempfile.mkdtemp()  # a real cwd for the source/derived sessions

# =================================================================================================
# 1. fork / handover / distiller stamp the SOURCE engine on the new record + its new ChatRef.
#    Run for BOTH engines: a codex source → CODEX (the D1 fix); a claude source → CLAUDE (no regress).
# =================================================================================================

for engine in (Engine.CODEX, Engine.CLAUDE):
    tag = engine.value

    # --- fork() — the full chat-op: real `_spawn`, then the fork `ChatRef` (role=fork) ---
    service = service_over(source_session(f"src-fork-{tag}", f"src-fork-{tag}", engine,
                                          f"FORKCHAT-{tag}", WORK))
    forked = ChatOps(service).fork(f"src-fork-{tag}", f"myfork-{tag}")
    check(f"fork [{tag}]: built the command via the SOURCE engine adapter (drove the right path)",
          COMMAND_MARK[engine] in forked.cmd)
    check(f"fork [{tag}]: the NEW record is stamped engine == {tag} (D1 — through the full spawn)",
          forked.engine == engine)
    fork_ref = chat_of(forked, "fork")
    check(f"fork [{tag}]: a pending fork ChatRef exists (id captured later)",
          fork_ref is not None and fork_ref.id is None)
    check(f"fork [{tag}]: the fork ChatRef inherits engine == {tag}", fork_ref.engine == engine)

    # --- _finish_handover() — spawns the fresh seeded worker (the D1 site driven directly) ---
    service = service_over(source_session(f"src-ho-{tag}", f"src-ho-{tag}", engine,
                                          f"HOCHAT-{tag}", WORK))
    handover_spec = ChatOpSpec(
        op_id=f"op-ho-{tag}", kind="handover", source_txid=f"src-ho-{tag}",
        source_chat=f"HOCHAT-{tag}", cwd=WORK, artifact_path="/tmp/brief.md",
        worker_name=f"hw-{tag}",
    )
    ChatOps(service)._finish_handover(handover_spec)
    worker = service.store.find_by_name(f"hw-{tag}")
    check(f"handover [{tag}]: built the worker command via the SOURCE engine adapter",
          worker is not None and COMMAND_MARK[engine] in worker.cmd)
    check(f"handover [{tag}]: the worker record is stamped engine == {tag} (D1)",
          worker.engine == engine)
    handover_ref = chat_of(worker, "handover")
    check(f"handover [{tag}]: a pending handover ChatRef exists",
          handover_ref is not None and handover_ref.id is None)
    check(f"handover [{tag}]: the handover ChatRef inherits engine == {tag}",
          handover_ref.engine == engine)

    # --- _spawn_distiller() — a plain llm spawn; gets a pending `original` ref from `_spawn` ---
    service = service_over()
    distiller = ChatOps(service)._spawn_distiller(f"distill-{tag}", "handover", WORK,
                                                  "summarise this chat", engine)
    check(f"distiller [{tag}]: built the FIXED per-engine distiller command",
          COMMAND_MARK[engine] in distiller.cmd)
    check(f"distiller [{tag}]: the distiller record is stamped engine == {tag} (D1)",
          distiller.engine == engine)
    check(f"distiller [{tag}]: the distiller's original ChatRef inherits engine == {tag}",
          bool(distiller.chats) and distiller.chats[0].engine == engine)

# =================================================================================================
# 2. The stamp is LOAD-BEARING for ingest (codex-plan bonus): a captured codex fork resolves +
#    ingests a NON-EMPTY bundle BECAUSE the record carries engine=CODEX. The old `engine=claude`
#    mis-stamp resolved to None — an empty bundle (the exact T9 D1 symptom). Pure on-disk fixture.
# =================================================================================================

CAPTURED = "019eac69-b2a1-74d3-b2b7-d938493205be"  # a fork's own captured id (verification §Flow-4)
# Plant a real codex rollout the glob `sessions/**/rollout-*-<id>.jsonl` resolves (verification §Rollout).
rollout = (Path(os.environ["CODEX_HOME"]) / "sessions" / "2026" / "06" / "09"
           / f"rollout-2026-06-09T14-44-28-{CAPTURED}.jsonl")
rollout.parent.mkdir(parents=True, exist_ok=True)
rollout.write_text(
    '{"type":"session_meta","payload":{"id":"' + CAPTURED + '","forked_from_id":"019eac67-aeb7",'
    '"originator":"codex-tui","cli_version":"0.138.0","model_provider":"openai"}}\n'
    '{"type":"response_item","payload":{"type":"message","role":"user",'
    '"content":[{"type":"input_text","text":"Reply with exactly the word PONG"}]}}\n'
    '{"type":"response_item","payload":{"type":"message","role":"assistant",'
    '"content":[{"type":"output_text","text":"PONG"}],"phase":"final_answer"}}\n'
)

# The bug locus directly: resolve_transcript routes on the engine. CODEX finds the rollout; CLAUDE
# (the mis-stamp) runs the wrong formula + projects glob → None → empty bundle.
check("ingest-routing: resolve_transcript(id, cwd, CODEX) finds the planted rollout",
      resolve_transcript(CAPTURED, WORK, Engine.CODEX) == rollout)
check("ingest-routing: resolve_transcript(id, cwd, CLAUDE) (the old mis-stamp) returns None — the D1 bug",
      resolve_transcript(CAPTURED, WORK, Engine.CLAUDE) is None)

# End-to-end through the REAL ingest: a codex fork, capture filled, ingests a NON-EMPTY bundle.
service = service_over(source_session("src-bundle", "src-bundle", Engine.CODEX, "ORIGCHAT", WORK))
forked = ChatOps(service).fork("src-bundle", "bundle-fork")
record = service.store.load(forked.id)
captured_ref = chat_of(record, "fork")
captured_ref.id = CAPTURED          # the hook backfills the fork's own id (T4)
captured_ref.transcript_path = ""   # force the by-id glob (exercise the engine routing, not a stored path)
service.store.save(record)

bundles = history.ingest_session(service.store, forked.id, wait=True)
check("ingest-bundle: a CODEX-stamped fork ingests a bundle (non-empty — the D1 fix's payoff)",
      len(bundles) == 1)
bundle_transcript = Path(bundles[0]) / "transcript.jsonl"
check("ingest-bundle: the bundle transcript exists and is byte-size-identical to the source rollout",
      bundle_transcript.is_file()
      and bundle_transcript.stat().st_size == rollout.stat().st_size > 0)

print(f"OK — {PASSED} checks passed")
