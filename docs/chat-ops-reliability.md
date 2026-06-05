# Chat-ops reliability + initial-prompt seeding

## Problem (reproduced)

`tx handover` / `tx rollover` "almost never work properly." Live repro + code audit found:

1. **Readiness marker is wrong.** `chat.py` `READINESS_MARKER = "❯ "` is `U+276F` + an ASCII
   space (`U+0020`), but claude 2.1.x renders the empty prompt as `❯` + `U+00A0` (non-break
   space) + placeholder. So `_await_ready` *never* matches → it always burns the full 20 s
   (`40 × 0.5`) and `_seed` then fires **blind**. Net: a mandatory ~20 s stall per seed *and* a
   silent drop whenever claude isn't input-ready by the 20 s mark. Same broken check in
   `bin/tx-assistant`.
2. **Fragile finish handoff.** The distiller (an LLM) must echo a ~200-char exact shell command
   verbatim to trigger the worker spawn / pane respawn. Any deviation → silent stall.
3. **No tests** on any of chat-ops.

## Design (agreed with the user)

Keep the **distiller → fresh-continuation** architecture: the new session starts clean from a
distilled brief/note, never the origin's full history ("no baggage"). Changes:

1. **Seed via claude's initial-prompt argument.** A positional prompt auto-submits in interactive
   mode (verified), and `inject_session_id` already preserves a priming-prompt tail. So bake the
   seed into the launch command for all three sites (distiller, handover worker, rollover
   successor) and delete the entire `_seed` / `_await_ready` / `READINESS_*` send-keys path. This
   removes root cause #1 for chat-ops outright.
2. **Op-spec + idempotent finish.** The parent writes a small op-spec under an op-id; the distiller
   triggers completion with one short verb `tx _chat-op-finish <op-id>` (not a long verbatim
   command). The finish is idempotent (keyed on op-id), so it is safe to call twice.
3. **Watchdog fallback.** The parent also fires a detached watcher that polls for the brief/note
   file; if it appears but the op is not finished within a grace window (the distiller flaked), the
   watcher runs the same idempotent finish. Distiller-driven happy path, deterministic safety net.
4. **Opus distiller.** `claude --model opus --effort medium` (was sonnet).
5. **Rollover catch-up link.** Re-ingest the origin's *final* transcript at finish (closes the
   snapshot→respawn gap), store a durable `catch_up` pointer on the rollover `ChatRef`, and seed the
   successor to read it on demand if the note looks truncated. No-baggage by default, nothing lost.
6. **Drive-by:** fix the `bin/tx-assistant` readiness marker.

## Continuation seeds (initial prompt, auto-submitted)

- **handover** → new tx process: `"Your brief is at <brief>. Read it and begin. Fuller history if
  needed: <bundle>/."`
- **rollover** → respawn same pane onto fresh chat: `"Continuing prior work in a fresh chat. Note:
  <note>. If it looks truncated, catch up from the predecessor transcript: <catch_up>. Then
  continue."`

## Files

- `lib/tx/chat.py` — core refactor (initial-prompt seeding, op-spec, finish, watchdog, catch-up)
- `lib/tx/cli.py` — `_chat-op-finish` / `_chat-op-watch` verbs (replace `_handover-finish` /
  `_rollover-finish`)
- `lib/tx/storage.py` — `chat_ops_dir()` for op-specs
- `bin/tx-assistant` — readiness marker fix

(No `session.py` change in the end: the rollover→predecessor catch-up link is the successor's
existing `origin.chat_id` + the deterministic bundle path, so no `ChatRef` field / schema bump.)

## Checklist

- [x] plan doc
- [x] `bin/tx-assistant` marker drive-by
- [x] op-spec store + `_chat-op-finish` (idempotent) + `_chat-op-watch`
- [x] handover → initial-prompt + op-spec
- [x] rollover → initial-prompt + op-spec + catch-up link
- [x] opus distiller
- [x] delete dead `_seed` / `_await_ready` / `READINESS_*`
- [x] QA end-to-end (inline) — handover + rollover validated on the new code (no committed unit tests)
- [x] draft PR + nvim diff companion

## QA notes

End-to-end validation in the real `~/.tx-ide` home (worktree `bin/tx` → worktree lib; the distiller's
finish runs the baked worktree path, the watchdog inherits it) surfaced one bug, now fixed:

- **`_strip_identity` carried the source's baked initial prompt forward.** With the seed now a
  positional, the worker got TWO positionals and claude ran the *source's* prompt (the worker did the
  wrong task). Real sources have baked prompts (the COMMON.md worker recipe), so this was a true bug.
  Fix: `_strip_identity` now keeps flags+values (via claude's `<…>` value-flag set) and drops
  positionals.

Confirmed after the fix: handover seeds the worker with the *brief* and it does the right task;
rollover respawns the pane onto a fresh `--session-id` seeded with the *note + catch-up pointer*;
lineage correct (`handover←src`, `rollover←src` on the same record, old chat closed); op-specs and
distillers torn down by the watchdog; no ~20s send-keys stalls (initial-prompt auto-submits).
