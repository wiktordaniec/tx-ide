# Multi-engine support for tx — overview

tx today drives exactly one coding agent: Claude Code. This change makes tx **agnostic to the
agent CLI it drives** — adding OpenAI **Codex** now, with **Gemini Antigravity** next — without
special-casing any of them.

This is the high-level picture. The consolidated detailed design is in [`design.md`](./design.md);
focused detail docs (indexed at the bottom) follow.

## Three layers

- **tx — the harness.** Spawns, tracks, and drives agent sessions in tmux; owns the durable
  records, the state machine, history, and chat operations (fork / handover / rollover).
- **engine — the agent CLI tx drives.** Claude Code, Codex (`codex`), Gemini Antigravity (`agy`).
  This is the new adapter target — the thing the harness is put *on*.
- **model — the LLM the engine runs.** `opus`, `gpt-5.5`. A separate axis from the engine.

"Agnostic" means tx calls an **`Engine` adapter**, never `claude` directly. One adapter per agent;
the core (records, state machine, tmux liveness, chat-op orchestration, history) stays engine-blind.

## Why now

tx is hard-coupled to Claude Code across launch flags, the `~/.claude/projects` transcript layout,
the `settings.json` hook wiring, the statusline, and the personas. Codex is installed and capable,
and its hook system maps cleanly onto tx's working/waiting state model — so the cost here is the
abstraction, not the integration.

## What differs across engines

Every cross-engine difference is just "how does this engine render a shared concept":

| Concept | Claude Code | OpenAI Codex | Gemini Antigravity (`agy`) |
|---|---|---|---|
| session id | **pre-mint** `--session-id` | **capture** after launch | **capture** after launch |
| reasoning effort | `--effort` flag | `-c model_reasoning_effort=` | baked into model id |
| transcript | `~/.claude/projects/…/<id>.jsonl` | `~/.codex/sessions/<date>/rollout-…<id>.jsonl` | `~/.gemini/…/brain/<id>/…jsonl` |
| "turn done" → state | `Stop` hook | `Stop` hook | statusline `agent_state` |
| hooks install | `settings.json` (JSON) | `config.toml` + `hooks.json` | `settings.json` + `.agents/hooks.json` |
| resume / fork | native flags | native subcommands | native subcommand / `/fork` |
| statusline | scriptable command | fixed segment list | scriptable command |

The load-bearing insight: **most agents don't let you choose the session id** — Codex and
Antigravity both mint their own, and tx *captures* it after launch (from the hook payload or disk).
Claude's pre-mintable `--session-id` is the exception. Designing capture-first is what makes the
seam fit N agents instead of two.

## Headline decisions

(Full log in [`design.md`](./design.md) §4.)

- The engine is stored **explicitly** on each record (schema v3 + a one-time migrator).
- Spawns default to **Claude**; Codex is opt-in via `--engine codex`, and tx builds the command.
- **Full Codex parity**: hook-driven state, history, fork / handover / rollover, resume.
- A **unified installer** wires both agents' hooks; tx owns `~/.codex/hooks.json` + a marked block.
- Codex defaults: model `gpt-5.5`, effort `high`, yolo via the bypass flags.
- Usage / rate-limit display is owned separately (the sessions-graph header strip) — out of scope.

## Validated against a third agent

Gemini Antigravity ships a real terminal CLI (`agy`, replacing the Gemini CLI in 2026) that is
strikingly close to the Claude/Codex shape — capture-only session id, JSONL transcripts, JSON-on-
stdin hooks, scriptable statusline, native resume/fork. We designed the seam against it (without
implementing it) so multi-engine is N-agent by construction, not a Claude+Codex special case.

## Roadmap

1. **`Engine` seam, Claude-only** — extract `claude.py` behind a `ClaudeEngine`; schema v3 +
   migrator. Pure refactor, zero behavior change, tests stay green.
2. **Generalize identity + transcript** — capture-after-launch id flow; transcript resolution by
   lookup, not formula; per-engine history bundles.
3. **`CodexEngine` + `setup/agents/codex.sh`** + the unified installer (gated by the spike below).
4. **Neutralize cross-cutting Claude-isms** — peer envelope, worktree dir, the require-worktree
   env, personas, tx-assistant.
5. **Verify** — a live Codex worker end-to-end.

A small **spike** (one real Codex session) precedes Phase 3 to confirm positional-prompt
auto-submit and hook firing — see [`design.md`](./design.md) §8.

## Status

Branch `feat/engine-abstraction`. Spec committed; Phase 1 next.

## Documentation set

| Doc | Covers | Status |
|---|---|---|
| `README.md` (this) | the high-level picture | ✅ |
| `design.md` | consolidated detailed design (to be split into the docs below) | ✅ |
| `protocol.md` | the `Engine` adapter interface + capability flags + call-sites | planned |
| `record-and-state.md` | schema v3, the `engine` field, the migrator, the state model | planned |
| `chat-identity.md` | id capture vs pre-mint, transcript resolution, the provenance flow | planned |
| `codex-adapter.md` | `CodexEngine`: commands, rollout parsing, history bundle | planned |
| `hooks-and-install.md` | the unified installer, `claude.sh` + `codex.sh`, trust, statusline | planned |
| `neutralizations.md` | `<from-agent>` envelope, `.tx-ide/worktrees`, require-worktree, personas | planned |
| `verification.md` | the pre-Phase-3 spike + the Phase-5 live verification | planned |
