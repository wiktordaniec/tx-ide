#!/usr/bin/env python3.14
"""Unit tests for `CodexEngine` (`lib/tx/engines/codex.py`, task T6).

No pytest in this repo, so this is directly runnable:  python3.14 tests/test_codex_engine.py
Exits non-zero on the first failure; prints "OK — N checks passed" (the repo convention — see
tests/test_messages.py / tests/test_engine_protocol.py). The shared protocol-conformance test
(tests/test_engine_protocol.py) already runs Codex through the contract + asserts the dual bundle
layout; this is the engine-specific behavior the conformance gate checks only structurally:

  - command building — every launch/op builder's exact argv (fresh / resume / fork / seed /
    distiller), the `-m gpt-5.5 -c model_reasoning_effort=high` rendering + model/effort overrides,
    and both yolo + hook-trust bypass flags on every op (design §5; verification.md Evidence 1/3);
  - identity — `matches_binary` (basename of a full path), `capture_session_id` off a hook payload;
  - transcript — `resolve_transcript` globs the rollout by id under a temp `$CODEX_HOME` (date-nested
    AND directly under sessions/), and `iter_messages` delegates to T7's `codex_rollout` parser over
    the committed fixtures, normalizing each turn to an engine-neutral dict;
  - hooks/state — the full `event_to_state` table + the `state_source` capability flag (design §3).

Hermetic: a temp `$CODEX_HOME` for the glob (the rollout fixtures are copied in); the parse fixtures
are read straight from tests/fixtures/codex/. No real spawn, no network, no live home touched.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

# A temp CODEX_HOME before importing the adapter — `resolve_transcript` globs under it (read at call
# time, but set up front so nothing can glob the live ~/.codex).
os.environ["CODEX_HOME"] = tempfile.mkdtemp()

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from tx.engines import StateSource, codex as codex_module, get  # noqa: E402
from tx.session import Engine, State  # noqa: E402

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures" / "codex"

engine = get(Engine.CODEX)

PASSED = 0


def check(label, condition):
    global PASSED
    if condition:
        PASSED += 1
        return
    print(f"FAIL: {label}")
    sys.exit(1)


def real_turns(messages):
    """The conversation with Codex's injected `<environment_context>` turn dropped — what the messages
    layer does to recover the typed turns (the engine yields the flag, the caller filters)."""
    return [message for message in messages if not message["is_environment_context"]]


# ----- registration --------------------------------------------------------------------------

check("register(Engine.CODEX) resolves via get()", engine is get(Engine.CODEX))
check("the adapter is a CodexEngine", type(engine).__name__ == "CodexEngine")


# ----- identity ------------------------------------------------------------------------------

check("binary == 'codex'", engine.binary == "codex")
check("matches_binary('codex') is True", engine.matches_binary("codex") is True)
check("matches_binary(full path) matches on the basename",
      engine.matches_binary("/opt/homebrew/bin/codex resume X") is True)
check("matches_binary('claude …') is False", engine.matches_binary("claude --x") is False)
check("matches_binary('') is False", engine.matches_binary("") is False)

cid, tpath = "019ea7f9-5334-7221-a09e-f7891025114c", "/private/var/…/rollout-…-019ea7f9.jsonl"
captured = engine.capture_session_id(
    {"session_id": cid, "transcript_path": tpath, "hook_event_name": "SessionStart", "model": "gpt-5.5"})
check("capture_session_id returns (session_id, transcript_path) off the payload",
      captured == (cid, tpath))


# ----- launch / ops: exact argv shapes (design §5) -------------------------------------------

fresh = engine.build_launch_command(initial_prompt="seed me")
check("launch: full shape -m gpt-5.5 -c model_reasoning_effort=high + both bypass flags + prompt",
      fresh == ["codex", "-m", "gpt-5.5", "-c", "model_reasoning_effort=high",
                "--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust",
                "seed me"])
check("launch: bare (no prompt) drops the positional tail",
      engine.build_launch_command() == ["codex", "-m", "gpt-5.5", "-c", "model_reasoning_effort=high",
                                        "--dangerously-bypass-approvals-and-sandbox",
                                        "--dangerously-bypass-hook-trust"])
override = engine.build_launch_command(model="gpt-5.5-codex", effort="xhigh")
check("launch: model/effort are rendered, not hardcoded",
      override[1:5] == ["-m", "gpt-5.5-codex", "-c", "model_reasoning_effort=xhigh"])

check("resume: codex resume <id> + both bypass flags",
      engine.resume_command("ABC") == ["codex", "resume", "ABC",
                                       "--dangerously-bypass-approvals-and-sandbox",
                                       "--dangerously-bypass-hook-trust"])
check("fork: codex fork <id> + both bypass flags",
      engine.fork_command("ABC") == ["codex", "fork", "ABC",
                                     "--dangerously-bypass-approvals-and-sandbox",
                                     "--dangerously-bypass-hook-trust"])
check("seed: a fresh codex carrying the seed as its positional prompt",
      engine.seed_command("brief") == engine.build_launch_command(initial_prompt="brief"))
check("seed: the seed is the positional tail", engine.seed_command("brief")[-1] == "brief")
check("distiller: a fresh codex at the gpt-5.5/high default, no prompt",
      engine.distiller_command() == engine.build_launch_command())

# Every op keeps --dangerously-bypass-hook-trust (Evidence 3: it is what fires our hooks headlessly).
for op_name, argv in [
    ("launch", fresh), ("resume", engine.resume_command("X")), ("fork", engine.fork_command("X")),
    ("seed", engine.seed_command("p")), ("distiller", engine.distiller_command()),
]:
    check(f"{op_name}: --dangerously-bypass-hook-trust present (hooks fire headlessly)",
          "--dangerously-bypass-hook-trust" in argv)
    check(f"{op_name}: binary 'codex' first", argv[0] == "codex")


# ----- transcript: resolve_transcript globs the rollout by id --------------------------------

def place_rollout(chat_id: str, *date_parts: str) -> Path:
    """Drop a rollout fixture at <CODEX_HOME>/sessions/<date…>/rollout-<ts>-<id>.jsonl."""
    target = codex_module.sessions_root().joinpath(*date_parts)
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"rollout-2026-06-08T18-03-15-{chat_id}.jsonl"
    shutil.copy(FIXTURES / "fresh.jsonl", path)
    return path


nested = place_rollout("NESTED-ID", "2026", "06", "08")
resolved = engine.resolve_transcript("NESTED-ID", "/irrelevant/cwd")
check("resolve_transcript: finds a date-nested rollout by id", resolved == nested)
check("resolve_transcript: the resolved path exists", resolved.exists())

flat = place_rollout("FLAT-ID")  # ** matches zero intermediate dirs (directly under sessions/)
check("resolve_transcript: finds a rollout directly under sessions/ (** matches zero dirs)",
      engine.resolve_transcript("FLAT-ID", "/x") == flat)

missing = engine.resolve_transcript("NO-SUCH-ID", "/x")
check("resolve_transcript: a chat not on disk → a non-existent sentinel path",
      missing.exists() is False)
check("resolve_transcript: cwd is irrelevant — same rollout regardless of cwd",
      engine.resolve_transcript("NESTED-ID", "/totally/different") == nested)


# ----- transcript: iter_messages delegates to codex_rollout, normalizing to dicts ------------

fresh_msgs = list(engine.iter_messages(FIXTURES / "fresh.jsonl"))
check("iter_messages: each turn is an engine-neutral dict (role/text/is_environment_context)",
      all(set(message) == {"role", "text", "is_environment_context"} for message in fresh_msgs))
check("iter_messages/fresh: first turn is the flagged environment_context",
      fresh_msgs[0]["is_environment_context"] is True and fresh_msgs[0]["role"] == "user")
fresh_real = real_turns(fresh_msgs)
check("iter_messages/fresh: two real turns after dropping environment_context", len(fresh_real) == 2)
check("iter_messages/fresh: user prompt preserved, assistant answer is PONG",
      fresh_real[0]["role"] == "user" and fresh_real[1]["text"] == "PONG")

multi_msgs = list(engine.iter_messages(FIXTURES / "multi_turn.jsonl"))
check("iter_messages/multi: six message items (reasoning + token_count records skipped)",
      len(multi_msgs) == 6)
check("iter_messages/multi: roles in order",
      [message["role"] for message in multi_msgs]
      == ["developer", "user", "user", "assistant", "user", "assistant"])
check("iter_messages/multi: exactly one environment_context turn flagged",
      sum(message["is_environment_context"] for message in multi_msgs) == 1)
check("iter_messages/multi: multi-block assistant message concatenated in order",
      real_turns(multi_msgs)[-1]["text"] == "The result is 5.")
check("iter_messages/multi: reasoning text never surfaces as a message",
      all("The user wants a simple sum." not in message["text"] for message in multi_msgs))

fork_real = real_turns(list(engine.iter_messages(FIXTURES / "fork.jsonl")))
check("iter_messages/fork: the fork's new turn is surfaced (assistant FORKED)",
      fork_real[-1]["role"] == "assistant" and fork_real[-1]["text"] == "FORKED")
check("iter_messages/fork: carried parent history is present (PONG)",
      any(message["text"] == "PONG" for message in fork_real))


# ----- bundle layout: Codex has no sidecar (design §6.4) -------------------------------------

check("bundle_sidecars: Codex names no sidecar dirs (the rollout JSONL is the whole bundle)",
      engine.bundle_sidecars(FIXTURES / "fresh.jsonl", "ANY-ID") == [])


# ----- hooks / state (design §3) -------------------------------------------------------------

check("state_source is HOOK_EVENTS (Codex emits a Stop hook)",
      engine.state_source is StateSource.HOOK_EVENTS)
working = {"UserPromptSubmit", "PreToolUse", "PostToolUse", "PreCompact", "PostCompact", "SubagentStart"}
check("event_to_state: the whole working family → WORKING",
      all(engine.event_to_state[name] == State.WORKING for name in working))
check("event_to_state: Stop → WAITING", engine.event_to_state["Stop"] == State.WAITING)
check("event_to_state: PermissionRequest → WAITING",
      engine.event_to_state["PermissionRequest"] == State.WAITING)
check("event_to_state: SessionStart is capture-only (not a state edge)",
      "SessionStart" not in engine.event_to_state)
check("event_to_state: no session-end edge (Codex has no session-end event; EXITED comes from tmux)",
      "SessionEnd" not in engine.event_to_state)


print(f"OK — {PASSED} checks passed")
