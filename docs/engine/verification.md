# Engine verification — Codex spike (T2) + live verification

This doc holds the **empirical** evidence behind the engine work: the pre-Phase-3 **Codex spike**
(this file's body — T2) and, later, the Phase-5 **live verification** (T9, placeholder at the end).

The spike answers the must-verify questions from [`design.md`](./design.md) §8 (plus a 4th, hook
trust, added by codex-plan) against **one real, isolated Codex session** — concrete captured stdin
and observed TUI behaviour, not claims read from docs — **before** anyone builds `CodexEngine` (T6)
or the installer (T3).

## Environment

| | |
|---|---|
| codex | `codex-cli 0.137.0`, `/opt/homebrew/bin/codex` (Caskroom `0.137.0`), authed (ChatGPT auth) |
| model | `gpt-5.5` (design default; priority-0 in the local models cache), `model_reasoning_effort=high` |
| host | macOS (darwin 25.3.0), zsh |
| date | 2026-06-08 |
| `hooks` feature | `stable`, effective `true` by default (`codex features list`); no `[features] hooks=true` gate needed at 0.137. (`plugin_hooks` is `removed` — the live system is `hooks`.) |

## Method — isolation (the live environment is never touched)

Codex honours `CODEX_HOME` (`os.environ.get("CODEX_HOME", "~/.codex")`). The spike runs against a
**throwaway `CODEX_HOME`** so the real `~/.codex` — config, trust state, sessions — is never written:

- `SANDBOX=$(mktemp -d)`; `CODEX_HOME=$SANDBOX/codex-home`, authed by a **read-only copy** of
  `~/.codex/auth.json` (+ `models_cache.json`, `version.json`, `installation_id`). The temp home's
  own `config.toml` marks the temp project `trust_level = "trusted"` (so no folder modal).
- Throwaway git project `$SANDBOX/project` carrying a **project-local** `.codex/hooks.json` — the
  exact path T3's installer targets — whose four hooks each append their **raw stdin** to a log:

  ```json
  { "hooks": {
      "SessionStart":     [ { "hooks": [ { "type": "command", "command": ".../cap_sessionstart.sh" } ] } ],
      "UserPromptSubmit": [ { "hooks": [ { "type": "command", "command": ".../cap_userpromptsubmit.sh" } ] } ],
      "PreToolUse":       [ { "hooks": [ { "type": "command", "command": ".../cap_pretooluse.sh" } ] } ],
      "Stop":             [ { "hooks": [ { "type": "command", "command": ".../cap_stop.sh" } ] } ]
  } }
  ```
- The interactive TUI is driven in a detached `tmux` pane (`tmux new-session -d -s t2spike`); the
  hook log is read back from disk. **No key is sent into the TUI for the seed turns** — only the
  shell command line is typed — so any submission is Codex's own doing.

**The hooks are the instrument.** `UserPromptSubmit` firing with our exact prompt — with no Enter
sent into the composer — *is* the proof of positional auto-submit; `Stop` with `last_assistant_message`
*is* the proof the turn ran.

> Result of the isolation: at the end, `sha256(~/.codex/config.toml)` is **byte-identical** to the
> pre-spike snapshot (`9e3e3b0024f583e30c5eaa0a7cbfa193c18647d4cc8709ed10c10e8b55177d89`), and no
> `~/.codex/hooks.json` / `[hooks.state]` leaked into the live home. (Codex *does* rewrite its
> `CODEX_HOME/config.toml` during a run — see §4 — which is exactly why the temp-home indirection,
> rather than snapshot+restore of the real file, was used.)

Common launch command (the shape T6's `CodexEngine` will build):

```
CODEX_HOME=$HOME codex -C $PROJECT \
  --dangerously-bypass-approvals-and-sandbox --dangerously-bypass-hook-trust \
  -m gpt-5.5 -c model_reasoning_effort=high "<seed prompt>"
```

---

## Evidence 1 — positional prompt auto-submits (fresh / resume / fork)

**Question.** Does `codex "<prompt>"` — and `codex resume <id> "<prompt>"` / `codex fork <id>
"<prompt>"` — auto-submit the positional prompt in the interactive TUI (vs. leaving it unsent in the
composer)? The whole seeding / chat-ops model depends on this.

**Verdict: YES for all three.** In every case the positional prompt was submitted with **no key sent
into the composer**, `UserPromptSubmit` fired carrying the prompt verbatim, and `Stop` returned the
model's answer.

### 1a. Fresh — `codex "<prompt>"`

```
CODEX_HOME=… codex -C …/project --dangerously-bypass-approvals-and-sandbox \
  --dangerously-bypass-hook-trust -m gpt-5.5 -c model_reasoning_effort=high \
  'Reply with exactly the word PONG and nothing else. Do not run any shell commands or tools.'
```

TUI (captured): the prompt appears already submitted (`› Reply with…` then `• PONG`). Hooks fired in
order **SessionStart → UserPromptSubmit → Stop**:

```json
{"session_id":"019ea7f9-5334-7221-a09e-f7891025114c","turn_id":"019ea7f9-5404-7a50-b701-ee2730d115d9","transcript_path":"…/sessions/2026/06/08/rollout-2026-06-08T18-03-15-019ea7f9-5334-7221-a09e-f7891025114c.jsonl","cwd":"…/project","hook_event_name":"UserPromptSubmit","model":"gpt-5.5","permission_mode":"bypassPermissions","prompt":"Reply with exactly the word PONG and nothing else. Do not run any shell commands or tools."}
{"…","hook_event_name":"Stop","…","stop_hook_active":false,"last_assistant_message":"PONG"}
```

### 1b. Resume — `codex resume <id> "<prompt>"`

```
CODEX_HOME=… codex resume 019ea7f9-5334-7221-a09e-f7891025114c -C …/project \
  --dangerously-bypass-approvals-and-sandbox --dangerously-bypass-hook-trust \
  -m gpt-5.5 -c model_reasoning_effort=high 'Reply with exactly the word PINGBACK and nothing else. No shell, no tools.'
```

Auto-submitted. `SessionStart.source == "resume"`; the **same** `session_id` and `transcript_path`
are continued, with a new `turn_id`:

```json
{"session_id":"019ea7f9-5334-7221-a09e-f7891025114c","hook_event_name":"SessionStart","model":"gpt-5.5","permission_mode":"bypassPermissions","source":"resume", …}
{"session_id":"019ea7f9-5334-7221-a09e-f7891025114c","turn_id":"019ea7fa-b28a-7ab1-abe5-6a90a00b3668","hook_event_name":"UserPromptSubmit","prompt":"Reply with exactly the word PINGBACK and nothing else. No shell, no tools.", …}
{"…","hook_event_name":"Stop","last_assistant_message":"PINGBACK"}
```

### 1c. Fork — `codex fork <id> "<prompt>"`

```
CODEX_HOME=… codex fork 019ea7f9-5334-7221-a09e-f7891025114c -C …/project \
  --dangerously-bypass-approvals-and-sandbox --dangerously-bypass-hook-trust \
  -m gpt-5.5 -c model_reasoning_effort=high 'Reply with exactly the word FORKED and nothing else. No shell, no tools.'
```

Auto-submitted, and forking minted a **new** identity (see Evidence 2). `last_assistant_message ==
"FORKED"`.

> `codex resume`/`codex fork` accept `-C/--cd` and both `--dangerously-bypass-*` flags (verified via
> `--help`), so T6 can pass the same flag set on every op.

---

## Evidence 2 — `SessionStart` (and `fork`) fire the hook with the **new** identity

**Question.** Does `SessionStart` (and `codex fork`) fire our hook with the new `session_id` +
`transcript_path` on stdin?

**Verdict: YES.** The session id is **captured** from the payload, never chosen by us — exactly the
capture-for-all path the design relies on (README "the session id is captured, not chosen").

`SessionStart` payload (fresh run 1a):

```json
{"session_id":"019ea7f9-5334-7221-a09e-f7891025114c","transcript_path":"/private/var/folders/…/codex-home/sessions/2026/06/08/rollout-2026-06-08T18-03-15-019ea7f9-5334-7221-a09e-f7891025114c.jsonl","cwd":"…/project","hook_event_name":"SessionStart","model":"gpt-5.5","permission_mode":"bypassPermissions","source":"startup"}
```

Identity across the three ops:

| op | `session_id` | continuity |
|---|---|---|
| fresh | `019ea7f9-…5114c` | new |
| resume | `019ea7f9-…5114c` | **same** id + same `transcript_path`, new `turn_id` |
| fork | `019ea7fb-4895-7bd1-b724-83701053474b` | **new** id + **new** `transcript_path` |

The fork's rollout `session_meta` records its parent explicitly — lineage tx can read directly for
the fork/handover/rollover provenance DAG:

```json
{"type":"session_meta","payload":{"id":"019ea7fb-4895-7bd1-b724-83701053474b","forked_from_id":"019ea7f9-5334-7221-a09e-f7891025114c","cwd":"…/project","originator":"codex-tui","cli_version":"0.137.0","source":"cli","thread_source":"user","model_provider":"openai", …}}
```

> Note: a fork's `SessionStart.source` is **`"startup"`**, not a distinct `"fork"` — a fork is
> indistinguishable from a fresh start *by `source` alone*; the **new `session_id` + `forked_from_id`
> in `session_meta`** are what mark it. (`source` enum observed/typed: `startup | resume | clear |
> compact`.)

---

## Evidence 3 — hooks run under yolo + `--dangerously-bypass-hook-trust`

**Question.** Does a `--dangerously-bypass-approvals-and-sandbox --dangerously-bypass-hook-trust`
codex actually run our hooks?

**Verdict: YES.** Runs 1a–1c each ran all three configured non-tool hooks
(`SessionStart`/`UserPromptSubmit`/`Stop`) with our (untrusted) project-local `hooks.json`, with
`permission_mode == "bypassPermissions"`. The TUI shows the banner:

```
⚠ `--dangerously-bypass-hook-trust` is enabled. Enabled hooks may run without review for this invocation.
```

The bypass is **independently required**: see Evidence 4's A/B — yolo **alone** (no
`--dangerously-bypass-hook-trust`) does **not** run untrusted hooks; it stops at the trust gate.

> **`codex exec` does NOT run lifecycle hooks.** A headless `codex exec … --dangerously-bypass-*`
> turn completed (`PONG`) and wrote a rollout, but fired **zero** hooks. Hooks are an **interactive
> (TUI) session** feature — which matches the design: tx drives the interactive `codex` in a tmux
> pane (per `CodexEngine` §5), not `codex exec`. T6/T9 must rely on the **TUI**, not `exec`, for
> state.

`PreToolUse` was configured but **not exercised** — the trivial verification prompts intentionally
used no tools, so no tool-use turn occurred. (It is wired and would fire on a tool call; the design's
`PreToolUse → WORKING` mapping is unexercised here, flagged for the T9 live verification.)

---

## Evidence 4 — hook-trust mechanism (the 4th item; directly feeds T3)

**Question.** How does hook trust work for a tx-owned `hooks.json` — project `.codex/` vs. user
`~/.codex/`; what does `--dangerously-bypass-hook-trust` actually do; and what would a non-bypass
*trusted* install look like?

**Observed, end to end:**

**(a) Untrusted hooks are gated, not silently run.** Launching with yolo but **without**
`--dangerously-bypass-hook-trust` (everything else equal to run 1a) stopped at an interactive gate at
startup; no hook fired:

```
Hooks need review
4 hooks are new or changed.
Hooks can run outside the sandbox after you trust them.
› 1. Review hooks
  2. Trust all and continue
  3. Continue without trusting (hooks won't run)
```

**(b) `--dangerously-bypass-hook-trust` = run enabled hooks for *this invocation* without persisted
trust** (and without persisting any). Confirmed by the A/B: with the flag → hooks fire (Evidence 3);
without it → the gate above. Trust state is **not** written when bypassing.

**(c) Trusting persists a per-hook hash.** Choosing *"Trust all and continue"* wrote, to the
**user-level** `CODEX_HOME/config.toml` (i.e. `~/.codex/config.toml` in production — *not* the
project file), one entry per hook, keyed `"<hooks-file-path>:<event_snake>:<index>:<index>"`:

```toml
[hooks.state]

[hooks.state."…/project/.codex/hooks.json:session_start:0:0"]
trusted_hash = "sha256:49b4440b8e26c0359a9b842cb2c732e174052c4c3c8008fecd1dc100caa23753"

[hooks.state."…/project/.codex/hooks.json:user_prompt_submit:0:0"]
trusted_hash = "sha256:f8d159534d84c14297ae73689bff95ea54a27597b3411c1e6b00afdc3545e3de"

[hooks.state."…/project/.codex/hooks.json:pre_tool_use:0:0"]
trusted_hash = "sha256:e92f13092115297810b9c72eda51811d7ed6d28c80e25e61b59af09df4227e77"

[hooks.state."…/project/.codex/hooks.json:stop:0:0"]
trusted_hash = "sha256:e9f919fa0380f5ac5518a36130da24a59e5639247b4af15af1425b6ba0d32ce9"
```

Trust is recorded **against the hook's current hash**, so a later edit re-trips the gate ("new or
changed").

**(d) Once trusted, hooks run with no bypass flag.** A subsequent fresh session in the same project
**without** `--dangerously-bypass-hook-trust` showed **no gate** and fired all three hooks
(`SessionStart`/`UserPromptSubmit`/`Stop`, `last_assistant_message == "TRUSTED"`). This is the
non-bypass trusted-install end state.

**(e) Project vs. user — same rule.** A user-level `~/.codex/hooks.json` (a new, non-managed hook)
hit the **same** gate ("1 hook is new or changed"). So the trust axis is **managed vs. not**, *not*
project-vs-user: a tx-owned `~/.codex/hooks.json` is non-managed and therefore needs trust just like
a project hook. Per the docs, **managed** hooks (system / MDM / `requirements.toml`) are
**pre-trusted**.

### Implication for T3 (the installer)

A tx-owned `~/.codex/hooks.json` is non-managed → it needs trust before it runs. Three viable paths,
in preference order for an automated worker:

1. **Pre-seed trust** — T3 writes the `[hooks.state."~/.codex/hooks.json:<event>:0:0"]
   trusted_hash = "sha256:<hash-of-each-hook>"` block into `~/.codex/config.toml` (the marked,
   realpath-atomic TOML edit the design already calls for in §4.7). Then tx spawns **without**
   `--dangerously-bypass-hook-trust` and hooks run clean. Must recompute the hash whenever the shim
   changes (it's hashed per hook).
2. **Yolo bypass** — keep `--dangerously-bypass-hook-trust` on every tx-built Codex command (design
   §4.5 / §5). Simplest; runs untrusted hooks every invocation; nothing persisted. Fine for
   tx-vetted shims, but it is the "DANGEROUS … only for automation that already vets hook sources"
   path.
3. **Interactive user onboarding** — a human runs the startup gate's *"Trust all and continue"* (or
   `/hooks`) once. Good for the interactive-user install; not viable for headless workers.

(A managed/`requirements.toml` install — pre-trusted by construction — is the cleanest long-term
option and is noted in design §9 as open.)

---

## Hook payload field reference (observed on stdin)

One JSON object per hook on **stdin**. Fields observed at 0.137 (yolo → `permission_mode =
"bypassPermissions"`):

| field | SessionStart | UserPromptSubmit | Stop |
|---|---|---|---|
| `session_id` | ✓ | ✓ | ✓ |
| `transcript_path` | ✓ | ✓ | ✓ |
| `cwd` | ✓ | ✓ | ✓ |
| `hook_event_name` | ✓ | ✓ | ✓ |
| `model` | ✓ | ✓ | ✓ |
| `permission_mode` | ✓ | ✓ | ✓ |
| `source` (`startup`/`resume`/`clear`/`compact`) | ✓ | — | — |
| `turn_id` | — | ✓ | ✓ |
| `prompt` | — | ✓ | — |
| `stop_hook_active`, `last_assistant_message` | — | — | ✓ |

Event → state (design §3), corroborated where exercised: `SessionStart` → chat capture (carries
id+path); `UserPromptSubmit` → WORKING; `Stop` → WAITING (carries `last_assistant_message`).
`PreToolUse`/`PostToolUse`/compaction events not exercised (no tool/compaction turns) — deferred to
T9.

## Rollout path + transcript shape (feeds T7 fixtures)

Real on-disk shape (production root `~/.codex`; the spike's is identical under the temp home):

```
~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<YYYY-MM-DDTHH-MM-SS>-<session-id>.jsonl
e.g. rollout-2026-06-08T18-03-15-019ea7f9-5334-7221-a09e-f7891025114c.jsonl
```

Matches the design's glob `~/.codex/sessions/**/rollout-*-<id>.jsonl`. The hook payload's
`transcript_path` is the canonical realpath (`/private/var/…` on macOS) and should be **preferred**
over re-deriving the glob (store it on the `ChatRef`, per §2).

Transcript = newline-delimited JSON; first line is `session_meta` (see Evidence 2, incl.
`forked_from_id`). Messages are OpenAI Responses items:

```json
{"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"…"}]}}
{"type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"PONG"}],"phase":"final_answer"}}
```

`iter_messages` = `payload.type == "message"`, `role ∈ {developer, user, assistant}`, text blocks
`input_text` / `output_text` — as design §3 states. (The first `user` item is an injected
`<environment_context>` block, not user-typed input — the parser/bundle should expect it.)

## Other findings worth carrying forward

- **Codex rewrites its `CODEX_HOME/config.toml` during normal runs** (adds `personality`, a
  realpath-canonical `[projects."…"]` trust entry, and `[hooks.state]`). T3's TOML edit must be a
  marked, realpath-atomic block (already in §4.7) and tolerate Codex re-normalising the file around it.
- **TUI composer drops a too-fast Enter** (same race COMMON.md notes for Claude's input box): typing
  a prompt then immediately sending `Enter` left it unsent. When tx types into a **live** Codex
  composer (e.g. mid-session chat-ops), it needs the same ~0.3 s pre-Enter delay it uses for Claude.
  The positional-prompt **auto-submit** (Evidence 1) sidesteps this for the *initial* seed — one more
  reason the seed goes through the positional arg, not typed keys.
- **`SessionStart` timing**: with an auto-submitted positional prompt it fires immediately at spawn
  (so the capture path is satisfied at spawn). An *idle* TUI (launched with no prompt) defers
  `SessionStart` until the first interaction — not a concern for tx, which always seeds with a prompt.

## Acceptance checklist (T2)

- [x] **All four** evidence items answered with reproducible commands + captured stdin/observed TUI
      (above), concrete not asserted.
- [x] Real `~/.codex/config.toml` **unmodified** — `sha256 = 9e3e3b00…55177d89`, identical to the
      pre-spike snapshot; no `~/.codex/hooks.json` / `[hooks.state]` leaked (CODEX_HOME isolation, no
      snapshot/restore of the real file needed).
- [x] Baseline green: `python3.14 tests/test_messages.py` → `OK — 31 checks passed`.
- [x] No unresolved `AINote:` introduced.

## Reproduction

1. `SANDBOX=$(mktemp -d)`; build `$SANDBOX/codex-home` (copy `~/.codex/auth.json` + caches;
   `config.toml` trusting the temp project) and `$SANDBOX/project/.codex/hooks.json` (four hooks, each
   appending stdin to a log).
2. Launch the interactive TUI in tmux with the common launch command above and a trivial single-turn
   prompt; read the hook log + `tmux capture-pane`.
3. Repeat for `codex resume <id> "<p>"`, `codex fork <id> "<p>"`, and the trust A/B (with/without
   `--dangerously-bypass-hook-trust`, and after *"Trust all and continue"*).
4. `rm -rf "$SANDBOX"`. The real `~/.codex` is never written.

---

## Phase-5 live verification (T9) — placeholder

To be filled by T9: a **live Codex worker** end-to-end — working/waiting transitions from real hook
events, chat capture onto the record, history ingest from the rollout, fork / handover / rollover,
and resume — observed, with evidence, against the built `CodexEngine`. (This spike de-risks that work;
it does not stand in for it.)
