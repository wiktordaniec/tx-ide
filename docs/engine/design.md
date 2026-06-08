# Engine abstraction — making tx-ide agent-agnostic + the Codex adapter

Status: design agreed; implementation in phases (this doc is canonical for the work).
Scope: make tx-ide agnostic to the coding-agent CLI it drives, and add an OpenAI Codex adapter
with full parity (spawn, hook-driven state, history, fork/handover/rollover, resume). Designed
against a *third* agent (Gemini Antigravity `agy`) so the seam is N-agent, not a 2-way retrofit.

## §0 Concept & naming

Three layers, kept distinct:

- **tx** — the *harness* / session controller. Wraps and drives an agent CLI in a tmux pane.
- **engine** — the agent CLI tx drives: Claude Code, OpenAI Codex (`codex`), Gemini Antigravity
  (`agy`). This is the adapter target — the thing the harness is "put on".
- **model** — the LLM the engine runs (`opus`, `gpt-5.5`). A separate axis, not the engine.

The codebase already anticipated this: `lib/tx/claude.py` is documented as "the one place Claude
specifics live … no provider abstraction (§15)"; `setup/agents/claude.sh` is "the future-agent
seam (§10/§15)"; `D9` keeps no agent field on the record. This work builds the deferred core
abstraction and **revisits D9** (we now store the engine explicitly).

## §1 The `Engine` type + record schema v3

```python
class Engine(str, Enum):       # lib/tx/session.py, alongside Kind / Role / State
    CLAUDE = "claude"
    CODEX  = "codex"
    GEMINI = "gemini"          # not implemented yet; reserved so the seam is proven N-way
```

- `SCHEMA_VERSION` 2 → 3. New field `engine: Engine | None` on **`Session`** and **`ChatRef`**
  (`None` for non-llm sessions — nvim/shell/other; "the session's agent engine, if it has one").
- §9 forbids back-migration (loader refuses non-current versions). So ship a **one-time v2→v3
  migrator** that stamps `engine = CLAUDE` on existing llm records (and `None` otherwise) rather
  than orphaning live in-flight sessions.
- Resolution: an `engine` is set at spawn (from the `--engine` flag / inferred binary) and then
  read off the record — never re-derived from a possibly-reconstructed `cmd`.

## §2 The `Engine` protocol (the adapter surface)

One protocol, one adapter per engine, called by the core. Everything engine-specific lives behind
it; tmux liveness, the durable record, chat-op orchestration + provenance DAG, `TX_SESSION_ID`→
record mapping, peer messaging, and the picker/state machine stay shared and engine-blind.

Capability flags (encode the cross-engine variation):
- `mints_own_id: bool` — Claude `False` (tx pre-mints `--session-id`); Codex/Gemini `True`
  (tx captures the id post-launch). **Two of three engines capture**, so capture is the norm and
  Claude's pre-mint is the exception.
- state source — hook events (Claude, Codex) vs a status poll (a future engine whose CLI has no
  turn-done hook, e.g. Antigravity → statusline `agent_state`).

Methods (illustrative names):
- identity: `binary`, `matches_binary`, `command_declares_chat`,
  `inject_session_id` *(pre-mint engines)* / `capture_session_id(hook_payload | cwd)` *(capture engines)*
- launch/ops: `build_launch_command(model, effort, …)`, `resume_command(id)`, `fork_command(id)`,
  `seed_command(prompt)`, `distiller_command()` — each renders the abstract `(model, effort)` its
  own way (Claude `--effort`, Codex `-c model_reasoning_effort=`, Gemini effort-in-model-id)
- transcript: `resolve_transcript(id, cwd) → Path`, `iter_messages(transcript)`, `bundle(chat)`
- hooks/state: `event_to_state` table + yield signals; capture engines also return id/path from
  the hook payload
- install: `setup/agents/<name>.sh` (the existing seam), driven by one unified installer

A new engine implements ~8 methods + 2 flags + one `setup/agents/<name>.sh`. Everything else is free.

## §3 Cross-engine axis table (the seam holds because every diff is "how this engine renders X")

| Axis | Claude Code | OpenAI Codex (`codex` 0.137) | Gemini Antigravity (`agy`) |
|---|---|---|---|
| session id | **pre-mint** `--session-id` | **capture** (hook payload) | **capture** (disk / statusline) |
| effort → | `--effort high` | `-c model_reasoning_effort=high` | baked into model id |
| transcript | `~/.claude/projects/<munge(cwd)>/<id>.jsonl` | `~/.codex/sessions/<Y/M/D>/rollout-<ts>-<id>.jsonl` | `~/.gemini/antigravity-cli/brain/<id>/…/transcript.jsonl` |
| transcript schema | `{type:"user",message:{role,content}}` (Anthropic) | `{type,timestamp,payload}`; messages = `payload.type=="message"`, role∈{developer,user,assistant}, blocks input_text/output_text (OpenAI Responses items) | JSONL |
| "turn done" → state | `Stop` hook | `Stop` hook | statusline `agent_state` poll (no CLI Stop hook confirmed) |
| hooks install | `~/.claude/settings.json` JSON marker | `~/.codex/config.toml [features]` + tx-owned `~/.codex/hooks.json` | `settings.json` + `.agents/hooks.json` |
| resume / fork | `--resume` / `--fork-session` | `codex resume <id>` / `codex fork <id>` (+positional prompt) | `agy --conversation <id>` / `/fork` |
| sidecar | sibling `<id>/` (subagents/, tool-results/) | none (all inline in the rollout JSONL) | artifacts dir |
| yolo | `--dangerously-skip-permissions` | `--dangerously-bypass-approvals-and-sandbox` + `--dangerously-bypass-hook-trust` | skip-approvals mode |
| statusline | scriptable command (pipes JSON) | **none** — fixed `[tui] status_line` segment enum | scriptable `/statusline` |

Codex hook events (config.toml `[[hooks.EVENT]]` / `hooks.json`, gated by `[features] hooks=true`;
payload = one JSON object on **stdin** carrying `session_id`, `transcript_path`, `cwd`,
`hook_event_name`, `model`): `UserPromptSubmit`→WORKING, `PreToolUse`/`PostToolUse`/`PreCompact`/
`PostCompact`/`SubagentStart`→WORKING, `Stop`→WAITING, `PermissionRequest`→WAITING,
`SessionStart`→chat capture. **No session-end event** → a finished Codex turn rests in WAITING
("needs you"); EXITED still comes from tmux-close→reconcile (already engine-agnostic).

## §4 Decisions (agreed)

1. Store engine explicitly on record + ChatRef; schema v3 + one-time migrator (revisits D9).
2. Full parity for Codex (spawn, state, history, fork/handover/rollover, resume).
3. Default engine = **Claude** on every spawn; **`--engine codex`** is explicit opt-in. tx builds
   the command via the engine (centralizes yolo/model/effort) rather than callers hand-writing it.
4. Codex default model = **`gpt-5.5`** (priority-0 in the local models cache, the documented coding
   default; no `-codex` variant this generation). Distiller/worker reasoning effort = **`high`**
   (second-to-highest; top is `xhigh`). Set via `-m gpt-5.5 -c model_reasoning_effort=high`.
5. yolo = `--dangerously-bypass-approvals-and-sandbox --dangerously-bypass-hook-trust`.
6. Unified installer drives both `setup/agents/claude.sh` + new `setup/agents/codex.sh`.
7. Codex install: tx-owned `~/.codex/hooks.json` + a marked `[features] hooks=true` + the default
   `[tui] status_line = ["model","reasoning","project-name","git-branch","context-used"]` +
   `status_line_use_colors = true` (copied from the known-good manual config), in/next to
   `~/.codex/config.toml` (edited as a marked raw-TOML block; realpath-atomic, since it may be a
   dotfiles symlink). Per-engine hook **shim** sets: `$TX_IDE_HOME/hooks/{claude,codex}/*.sh`;
   Codex shims keep stdin and pass `--engine codex` so `tx hook` reads id/path from the payload.
8. Peer envelope `<from-claude …>` → neutral `<from-agent …>`; **parse both** (live Claude sessions
   exist during rollover).
9. Worktree convention `.claude/worktrees` → `.tx-ide/worktrees`; `CLAUDE_REQUIRE_WORKTREE` →
   `TX_REQUIRE_WORKTREE` + a Codex `PreToolUse` guard-hook equivalent.
10. tx-assistant stays Claude by default; agent selectable later (out of v1 critical path).
11. Usage/rate-limit display is owned cross-engine by the sessions-graph header strip (separate
    `usage-limits` work), fed by Codex's rollout `token_count.rate_limits` (`primary`/`secondary`,
    `used_percent` / `window_minutes` / `resets_at` epoch-secs) + Claude's statusline POST. **Out of
    scope here.** Codex has no scriptable statusline, so usage can't live in Codex's own status line.

## §5 Adapter responsibilities

**`ClaudeEngine`** — pure extraction of today's `lib/tx/claude.py` behind the protocol. Zero
behavior change; all existing tests stay green. This is Phase 1 and proves the seam alone.

**`CodexEngine`** (new):
- launch: `codex -m gpt-5.5 -c model_reasoning_effort=high --dangerously-bypass-approvals-and-sandbox --dangerously-bypass-hook-trust "<seed>"`
- ops: native `codex resume <id> "<seed>"` / `codex fork <id> "<seed>"`; handover = fresh `codex "<seed>"`;
  rollover = respawn pane onto fresh `codex "<seed>"` + catch-up pointer to the predecessor rollout.
  All capture id post-hoc (no pre-mint).
- transcript: glob `~/.codex/sessions/**/rollout-*-<id>.jsonl` (prefer the `transcript_path` the hook
  payload handed us, stored on the ChatRef); Responses-item `iter_messages`; bundle = the rollout
  JSONL alone (no sidecar).
- distiller: `codex -m gpt-5.5 -c model_reasoning_effort=high …` reading the rollout bundle.

## §6 Core generalizations (one-time; benefit every engine)

1. schema v3 + `engine` field + migrator (§1).
2. generalize the **capture path** (today only Claude *forks* capture post-hoc) into the normal flow
   for `mints_own_id` engines — the hook reads `session_id`+`transcript_path` from the payload.
3. `transcript_path` (formula) → `resolve_transcript` (formula for Claude, glob for Codex).
4. per-engine bundle (Codex = rollout JSONL alone).
5. spawn gains `--engine` (default Claude); `infer_role` / `reconcile._is_agent_command` /
   tmux scroll-binding learn `codex`.
6. unified hooks installer + `setup/agents/codex.sh`; envelope + worktree neutralizations (§4.8/4.9).

## §7 Phased plan

0. **Spec + worktree** (this commit).
1. **`Engine` seam, Claude-only** — extract `claude.py` behind `ClaudeEngine`, route all call-sites,
   schema v3 + migrator. Pure refactor; tests green; zero behavior change.
2. **Generalize identity + transcript** — payload-driven id capture; `resolve_transcript`; per-engine
   bundle; generalize the pending-ChatRef backstop.
3. **`CodexEngine` + `setup/agents/codex.sh`** + unified installer + role/reconcile/scroll learn codex.
   (Gated by the §8 spike.)
4. **Neutralize cross-cutting Claude-isms** — `<from-agent>` envelope (+parse both), `.tx-ide/worktrees`,
   `TX_REQUIRE_WORKTREE`, personas/recipes, tx-assistant generalization, tests.
5. **Verify** — live Codex worker: working/waiting transitions, chat capture, history ingest,
   fork/handover/rollover, resume.

## §8 Must-verify spike (before Phase 3 — starts a real Codex session)

Run one isolated Codex session against a sandbox `hooks.json` (temp project `.codex/`, real auth) to
confirm, before building the adapter:
1. `codex "<prompt>"` (and `codex resume/fork <id> "<prompt>"`) **auto-submits** the positional
   prompt in the interactive TUI (the whole seeding/chat-ops model depends on it).
2. `SessionStart` / `codex fork` fires our hook with the **new** `session_id` + `transcript_path`.
3. a `--yolo --dangerously-bypass-hook-trust` codex actually **runs** our hooks headlessly.

## §9 Deferred / open

- Gemini Antigravity (`agy`) adapter — reserved enum member; the seam accommodates it (capture-only
  id like Codex; turn-done via statusline `agent_state`; effort-in-model-id; no config-dir env var).
- tx-assistant engine selection; managed Codex hook pre-trust (`requirements.toml`) vs one-time
  `/hooks` trust for interactive user sessions.
