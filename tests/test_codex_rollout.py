#!/usr/bin/env python3.14
"""Standalone parse tests for the Codex rollout parser (`lib/tx/engines/codex_rollout.py`, task T7).

No pytest in this repo, so this is directly runnable:  python3.14 tests/test_codex_rollout.py
Exits non-zero on the first failure (the repo convention — see tests/test_messages.py /
tests/test_engine_protocol.py). It parses the committed rollout fixtures under tests/fixtures/codex/
— structurally faithful to the shape the T2 spike captured (docs/engine/verification.md §"Rollout
path + transcript shape") — and asserts the parser's two primitives:
  - `read_session_meta` — session `id` + the `forked_from_id` lineage marker (present only on a fork);
  - `iter_messages`     — ordered turns (roles + text, multi-block concatenation), non-message records
    skipped, and Codex's injected `<environment_context>` first-user turn flagged, not surfaced.

The parser is loaded directly from its file (importlib), not via `tx.engines`, so this test depends
only on the new module — keeping T7 isolated from the engines-package wiring that T4/T6 own.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODULE_PATH = HERE.parent / "lib" / "tx" / "engines" / "codex_rollout.py"
FIXTURES = HERE / "fixtures" / "codex"

_spec = importlib.util.spec_from_file_location("codex_rollout", MODULE_PATH)
codex_rollout = importlib.util.module_from_spec(_spec)
# Register before exec: the parser's `@dataclass` resolves its annotations through
# `sys.modules[__module__]` (under `from __future__ import annotations`), which is unset for a
# module loaded straight from a file unless we register it first (the documented importlib pattern).
sys.modules["codex_rollout"] = codex_rollout
_spec.loader.exec_module(codex_rollout)

read_session_meta = codex_rollout.read_session_meta
iter_messages = codex_rollout.iter_messages
ENVIRONMENT_CONTEXT_TAG = codex_rollout.ENVIRONMENT_CONTEXT_TAG

PASSED = 0


def check(label, condition):
    global PASSED
    if condition:
        PASSED += 1
        return
    print(f"FAIL: {label}")
    sys.exit(1)


def real_turns(messages):
    """The conversation with Codex's injected `<environment_context>` turn dropped — exactly what a
    caller does to recover the typed turns."""
    return [message for message in messages if not message.is_environment_context]


# ----- fresh.jsonl — a fresh single-turn session (no parent) ------------------------------------

fresh_meta = read_session_meta(FIXTURES / "fresh.jsonl")
check("fresh: session id captured", fresh_meta["id"] == "019ea7f9-5334-7221-a09e-f7891025114c")
check("fresh: no forked_from_id on a fresh session", "forked_from_id" not in fresh_meta)
check("fresh: cwd carried on session_meta", fresh_meta["cwd"] == "/Users/dev/project")
check("fresh: cli_version carried on session_meta", fresh_meta["cli_version"] == "0.137.0")

fresh_messages = list(iter_messages(FIXTURES / "fresh.jsonl"))
check("fresh: first turn is the injected environment_context",
      fresh_messages[0].is_environment_context is True)
check("fresh: environment_context is a user item", fresh_messages[0].role == "user")
check("fresh: environment_context text opens with the tag",
      fresh_messages[0].text.startswith(ENVIRONMENT_CONTEXT_TAG))
check("fresh: exactly one environment_context turn",
      sum(message.is_environment_context for message in fresh_messages) == 1)

fresh_real = real_turns(fresh_messages)
check("fresh: two real turns after dropping environment_context", len(fresh_real) == 2)
check("fresh: real turn 0 is the user prompt", fresh_real[0].role == "user")
check("fresh: real user prompt text preserved",
      fresh_real[0].text
      == "Reply with exactly the word PONG and nothing else. Do not run any shell commands or tools.")
check("fresh: real turn 0 not flagged environment_context", fresh_real[0].is_environment_context is False)
check("fresh: real turn 1 is the assistant answer", fresh_real[1].role == "assistant")
check("fresh: assistant output_text captured", fresh_real[1].text == "PONG")


# ----- multi_turn.jsonl — developer turn, multiple turns, non-message records, multi-block --------

multi_meta = read_session_meta(FIXTURES / "multi_turn.jsonl")
check("multi: session id captured", multi_meta["id"] == "019ea801-1111-7000-8000-aaaaaaaaaaaa")
check("multi: no forked_from_id", "forked_from_id" not in multi_meta)

multi_messages = list(iter_messages(FIXTURES / "multi_turn.jsonl"))
check("multi: six message items (reasoning + event_msg records skipped)", len(multi_messages) == 6)
check("multi: roles in order",
      [message.role for message in multi_messages]
      == ["developer", "user", "user", "assistant", "user", "assistant"])
check("multi: only the environment_context turn is flagged",
      sum(message.is_environment_context for message in multi_messages) == 1)
check("multi: the flagged turn is the env block",
      multi_messages[1].is_environment_context is True
      and multi_messages[1].text.startswith(ENVIRONMENT_CONTEXT_TAG))

multi_real = real_turns(multi_messages)
check("multi: five real turns", len(multi_real) == 5)
check("multi: developer turn surfaced first", multi_real[0].role == "developer")
check("multi: developer instructions text preserved", multi_real[0].text.startswith("You are Codex"))
check("multi: multi-block assistant message concatenated in order",
      multi_real[-1].text == "The result is 5.")
check("multi: reasoning record not surfaced as a message",
      all("The user wants a simple sum." not in message.text for message in multi_messages))


# ----- fork.jsonl — a forked session carrying its parent's id -----------------------------------

fork_meta = read_session_meta(FIXTURES / "fork.jsonl")
check("fork: new session id captured", fork_meta["id"] == "019ea7fb-4895-7bd1-b724-83701053474b")
check("fork: forked_from_id names the parent",
      fork_meta["forked_from_id"] == "019ea7f9-5334-7221-a09e-f7891025114c")
check("fork: fork mints a new id distinct from its parent",
      fork_meta["id"] != fork_meta["forked_from_id"])

fork_messages = list(iter_messages(FIXTURES / "fork.jsonl"))
check("fork: exactly one environment_context turn",
      sum(message.is_environment_context for message in fork_messages) == 1)
fork_real = real_turns(fork_messages)
check("fork: the fork's new turn is surfaced",
      fork_real[-1].role == "assistant" and fork_real[-1].text == "FORKED")
check("fork: carried parent history is present",
      any(message.text == "PONG" for message in fork_real))


print(f"OK — {PASSED} checks passed")
