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

## Phase-5 live verification (T9)

A **live Codex worker** driven end-to-end through the merged wiring against the **frozen final product**
(`task/T9-e2e-verify` off `feat/engine-abstraction` @ `c3bfd1d`), by the independent verifier `V-T9-e2e`
(no self-grading). Each parity flow was OBSERVED on a real `codex` worker and CAPTURED below with real
artifacts. **Verdict: round-1 FAIL → fixed (`task/fix-d1-d2` @ `d31ad90`) → round-2 re-verify PASS**
(see [Re-verification (round 2)](#re-verification-round-2--d1--d2-fix--d31ad90) at the end). Round 1:
the core paths (spawn / capture / state / history ingest / resume) and the rollover *mechanics* worked
on the real worker, but **`tx fork` and `tx handover` of a Codex session mis-stamped the new record
`engine=claude`**, breaking their engine-routed history ingest (empty bundle), and **`tx rollover`
raised a `TypeError` in its CLI wrapper** (defects D1–D2 below). Round 2 re-ran the three failed flows
against the fix and confirmed all pass. The codex-plan-flagged unseeded-fork capture timing is confirmed
deferred (D3, expected — a separate follow-up, not a fix-blocker).

### Environment

| | |
|---|---|
| codex | `codex-cli 0.138.0`, `/opt/homebrew/bin/codex`, authed (ChatGPT auth) — a **minor bump from the T2 spike's 0.137.0**; the hook payload shape + behaviours below are unchanged across it |
| product under test | `task/T9-e2e-verify` @ `c3bfd1d` (frozen `feat/engine-abstraction`); `bin/tx` resolves its package from the worktree (C9), run with a temp `$TX_IDE_HOME` |
| model | `gpt-5.5`, `model_reasoning_effort=high` (the design defaults the adapter built) |
| host | macOS (darwin 25.3.0), zsh, python3.14 |
| date | 2026-06-09 |

### Method — the DUAL SANDBOX (codex-plan blessed; the live homes are NEVER written)

Two throwaway homes (`mktemp -d`); the live `~/.tx-ide` (v2 crew) and live `~/.codex` (the user is
*actively* using it — its WAL was live during this run) are both untouched, so abort-safety is by
construction (cleanup = `rm -rf` the temp dirs).

- **tx side → temp `$TX_IDE_HOME`.** All v3 records / log / history / hook shims live here.
- **codex side → temp `$CODEX_HOME`.** The user's `auth.json` + `models_cache.json` / `version.json` /
  `installation_id` were copied in **WRITABLE** (codex-plan refinement (a)). T3's installer was run
  **into the temp home** — `TX_IDE_HOME=<temp> CODEX_HOME=<temp> setup/engines/codex.sh install
  --settings <temp>/config.toml` — landing the 4 shims (`$TX_IDE_HOME/hooks/codex/{start,pre,work,post}.sh`),
  the tx-owned `$CODEX_HOME/hooks.json` (9 Codex events → the 4 shims), and the marked `[tui]` block.
  The throwaway worker project was pre-trusted by its **realpath** (`/private/var/...`, since codex
  canonicalises the macOS `/var`→`/private/var` symlink) so the bypass-launched TUI shows no folder modal.
- The worker `codex` got `CODEX_HOME=<temp>` via `tx spawn --env CODEX_HOME=<temp>` (tmux `new-session -e`),
  so every rollout/hook fired against the temp home; `tx hook`'s detached ingest inherits it from the shim.
- **Instrumentation (the T2 "hooks are the instrument" pattern):** the installer-generated shims were
  wrapped with a stdin `tee` to a capture log; the `tx hook <event> --engine codex` invocation each runs
  is **byte-identical** to the installer's (verified by diff). The plain installer shims were also
  exercised first (an un-instrumented spawn drove capture+state correctly), so the integration is proven
  against the unmodified shims and the payloads below are the literal stdin they receive.

> **Live-home integrity (the safety gate).** Snapshotted before the run and re-checked after every
> flow — **byte-identical throughout**:
> `sha256(~/.codex/config.toml) = a2cacd4acaaab023b82cc145c35fdc0bb4f06667f39797f62df4acbff51120e6`,
> `sha256(~/.codex/auth.json)  = c86eadf89fa73de5aa8d7d8f0de61dc08c4ba6f3e139184d435984417b90549e`,
> and **no `~/.codex/hooks.json`** (and no `.bak` files) ever appeared in the live home.
> **Auth-refresh check (codex-plan (a)):** across all five sessions (spawn/fork/handover/rollover/resume)
> codex did **not** rewrite the *temp* `auth.json` (`sha256` unchanged) — i.e. no OAuth refresh fired in
> this run. The WRITABLE copy remains the correct default: a longer-lived multi-turn run *could* refresh,
> and a read-only copy would brick it; the live auth is protected by being a separate untouched file.

---

### Flow 1 — spawn + capture + working→waiting + history ingest  ✅ PASS

```
TX_IDE_HOME=<temp> CODEX_HOME=<temp> tx spawn v-t9-codex --tag v-t9 --cwd <temp/project> \
  --engine codex --prompt "Reply with exactly the word PONG…" --env CODEX_HOME=<temp>
```

`tx` built the launch command via the Codex adapter and stamped `engine=codex` on the record + a
**pending** `original` `ChatRef` (`id=null`, capture-after-launch, no pre-mint):

```
cmd: codex -m gpt-5.5 -c model_reasoning_effort=high --dangerously-bypass-approvals-and-sandbox --dangerously-bypass-hook-trust 'Reply with exactly the word PONG…'
chat: { id: null, role: original, engine: codex, origin.how: spawn }
```

The seeded positional **auto-submitted** in the TUI (0.138, no key sent), and the installed hooks fired
**SessionStart → UserPromptSubmit → Stop** — captured stdin (the real payloads):

```json
{"session_id":"019eac67-aeb7-7cc0-94f9-b8ba439ff5f4","transcript_path":"…/codex-home/sessions/2026/06/09/rollout-2026-06-09T14-42-16-019eac67-…​.jsonl","cwd":"…/project","hook_event_name":"SessionStart","model":"gpt-5.5","permission_mode":"bypassPermissions","source":"startup"}
{"session_id":"019eac67-…","turn_id":"019eac67-af86-…","…","hook_event_name":"UserPromptSubmit","prompt":"Reply with exactly the word PONG…"}
{"session_id":"019eac67-…","turn_id":"019eac67-af86-…","…","hook_event_name":"Stop","stop_hook_active":false,"last_assistant_message":"PONG"}
```

- **Capture (T4):** `SessionStart` (seeded → fires AT spawn) filled the pending `ChatRef` with the real
  `session_id` + `transcript_path` from the payload — **captured, not minted**:
  `chat.id = 019eac67-aeb7-7cc0-94f9-b8ba439ff5f4`, `transcript_path = …/codex-home/sessions/2026/06/09/rollout-2026-06-09T14-42-16-019eac67-….jsonl`.
- **working → waiting (state log, `$TX_IDE_HOME/log.jsonl`, with actor):**

  ```json
  {"actor":"44bc95e7-…","type":"spawn","msg":"v-t9-codex [llm] …/project"}
  {"actor":"44bc95e7-…","type":"state","msg":"v-t9-codex → working"}   ← UserPromptSubmit
  {"actor":"44bc95e7-…","type":"state","msg":"v-t9-codex → waiting"}   ← Stop
  ```
- **history ingest (Codex = rollout ALONE, no sidecar):** the bundle
  `$TX_IDE_HOME/history/44bc95e7-…/019eac67-…/` holds **`transcript.jsonl` only** (+ the coalescing
  `.ingest.lock`) — `29721` bytes, byte-size-identical to the source rollout. No `subagents/` /
  `tool-results/` dirs (confirms `CodexEngine.bundle_sidecars → []`).
- **rollout on disk** (`$CODEX_HOME/sessions/<Y/M/D>/rollout-<ts>-<id>.jsonl`), transcript excerpt:

  ```
  [session_meta] id=019eac67-… originator=codex-tui cli_version=0.138.0 model_provider=openai
  [message role=developer] '<permissions instructions> …'
  [message role=user]      '<environment_context> …'            ← injected first-user turn (expected)
  [message role=user]      'Reply with exactly the word PONG…'
  [message role=assistant] 'PONG'
  ```

---

### Flow 4 — fork  ⚠️ FAIL (D1) + flagged capture-timing CONFIRMED (D3)

```
TX_IDE_HOME=<temp> CODEX_HOME=<temp> tx fork v-t9-codex v-t9-fork
# built cmd (correct — native fork, source persona inherited, identity dropped, no seed):
codex fork 019eac67-aeb7-7cc0-94f9-b8ba439ff5f4 -m gpt-5.5 -c model_reasoning_effort=high --dangerously-bypass-approvals-and-sandbox --dangerously-bypass-hook-trust
```

The forked TUI opened on the full source history (`• Thread forked from 019eac67…`, the prior PONG turn
shown) and sat **idle** at the composer.

- **D3 — unseeded-fork capture timing (codex-plan-flagged): DEFERRED, confirmed.** `tx fork` builds an
  **unseeded** `codex fork <id>` (no positional prompt), so the TUI is idle and **`SessionStart` does NOT
  fire at spawn** — immediately after the fork, no hook had fired and the fork's `ChatRef` was still
  `id=null` (pending). On the **first interaction** (a typed prompt), the deferred
  `SessionStart`(`source:"startup"`)→`UserPromptSubmit`→`Stop` fired, all carrying the fork's **own NEW
  id** `019eac69-b2a1-74d3-b2b7-d938493205be` (divergent from the source `019eac67`), and the capture
  path then filled the pending fork ref. The fork's rollout `session_meta` records the lineage:
  `{"id":"019eac69-…","forked_from_id":"019eac67-…","originator":"codex-tui","cli_version":"0.138.0"}`.
  So capture-for-all still completes — but only once the user interacts. **Follow-up: seed the fork** (or
  inject a no-op first turn) so its id is captured at spawn like a seeded session, per the plan's flag.
  Captured fork `SessionStart` stdin:

  ```json
  {"session_id":"019eac69-b2a1-74d3-b2b7-d938493205be","transcript_path":"…/rollout-2026-06-09T14-44-28-019eac69-….jsonl","cwd":"…/project","hook_event_name":"SessionStart","model":"gpt-5.5","permission_mode":"bypassPermissions","source":"startup"}
  ```

- **D1 — the fork record is mis-stamped `engine=claude` (SUBSTANTIVE BUG).** Although the *command* is a
  correct `codex fork …`, the new record + its fork `ChatRef` are stamped **`engine=claude`**, not
  `codex`:

  ```
  v-t9-fork → record engine: claude     chat: { role: fork, engine: claude, id: 019eac69-…, origin.chat_id: 019eac67-… }
  ```
  Capture still filled the id/transcript_path **only because both adapters read the same payload keys** —
  but the mis-stamp **breaks history ingest**, which is engine-routed: `history.resolve_transcript(chat_id,
  cwd, engine=claude)` runs Claude's munged-cwd formula + a `~/.claude/projects/*/{id}.jsonl` glob, never
  the Codex rollout glob, so it returns `None`. **Result observed: the fork's bundle dir
  `$TX_IDE_HOME/history/ad25da07-…/` is EMPTY**, even though the fork's rollout is on disk in the temp
  `CODEX_HOME` (`rollout-…-019eac69-….jsonl`). A later `tx resume` of a fork would likewise build
  `claude --resume` instead of `codex resume`.
  **Root cause:** `lib/tx/chat.py ChatOps.fork()` builds the command via `get(source.engine).fork_command(...)`
  (it *knows* the source is codex) but constructs `SpawnSpec.for_process(...)` **without** passing
  `engine=source_session.engine`; `service._spawn` then defaults an llm spawn to `Engine.CLAUDE`. (Contrast
  `ResumeCommand`, which *does* pass `engine=record.engine` — Flow 7 — and rollover, which reuses the same
  record — Flow 6.)

---

### Flow 5 — handover  ⚠️ FAIL (D1, same root cause)

```
TX_IDE_HOME=<temp> CODEX_HOME=<temp> tx handover v-t9-codex "say hello then stop" v-t9-handover --self-catch-up
# worker built cmd (correct — fresh codex, source persona inherited, seed appended):
codex -m gpt-5.5 -c model_reasoning_effort=high --dangerously-bypass-approvals-and-sandbox --dangerously-bypass-hook-trust 'You are taking over work via tx handover…'
```

The handover worker launched as a fresh seeded `codex`, auto-submitted, and **captured its own new id**
`019eac6c-805a-7090-a48a-abc164968dc8`. But — **identical D1 defect** — the worker record + its handover
`ChatRef` are stamped **`engine=claude`**, so its history ingest fails the same way: the worker's rollout
exists in the temp `CODEX_HOME` (`rollout-…-019eac6c-805a-….jsonl`) yet its bundle dir
`$TX_IDE_HOME/history/fe0a3801-…/` is **EMPTY**. **Root cause:** `chat.py _finish_handover()` (and
`_spawn_distiller()`) build `SpawnSpec.for_process(...)` without `engine=source.engine` — same omission as
`fork()`.
*(Minor, separate: the self-catch-up seed text hardcodes Claude's `"transcript.jsonl + subagents/ +
tool-results/"` bundle layout, a Claude-ism that leaked past T5 — harmless for Codex, whose bundle is
rollout-only, but a neutralisation follow-up.)*

---

### Flow 6 — rollover  ✅ parity mechanics PASS / ⚠️ FAIL `tx rollover` CLI (D2)

```
TX_IDE_HOME=<temp> CODEX_HOME=<temp> tx rollover v-t9-codex --self-catch-up
```

The **rollover mechanics are correct on the real worker:** the same record stays **`engine=codex`** (it
reuses the record — no new `SpawnSpec`), the pane was `respawn-pane -k`'d onto a **fresh seeded codex**,
the rotated-out chat was closed, and a new `rollover` `ChatRef` captured the successor's **own new id**:

```
v-t9-codex → record engine: codex
  chat: { role: original, engine: codex, id: 019eac67-…, ended_at: set }            ← predecessor closed
  chat: { role: rollover, engine: codex, id: 019eac6c-e66f-7850-97b9-462d6bf11ceb, origin.how: rollover, origin.chat_id: 019eac67-… }
```
The respawned pane carried the **catch-up pointer to the predecessor rollout/bundle** (seeded prompt):
`"Continuing prior work in a fresh chat (rollover). The predecessor bundle is at
…/history/44bc95e7-…/019eac67-…/ … read what you need to resume, then continue."` The successor's rollout
`session_meta` is a fresh start (`forked_from_id: null`) — correct (rollover seeds a fresh chat, not a fork).

- **D2 — `tx rollover` raises `TypeError: 'NoneType' object is not subscriptable`.** `RolloverCommand.run`
  does `new_chat[:8]` but `ChatOps.rollover()` returns `None` (by design — the successor id is captured
  later). The rollover **work still completes** (it is scheduled via a detached `_chat-op-finish` *before*
  the crashing `print`), but the command exits with a traceback. **Engine-agnostic** (would affect a
  Claude rollover too); surfaced here because T9 is the first to drive `tx rollover` end-to-end.

---

### Flow 7 — resume  ✅ PASS (T8's resume-stamp fix holds)

```
TX_IDE_HOME=<temp> CODEX_HOME=<temp> tx resume v-t9-codex --as v-t9-resume
# built cmd (correct — native resume of the same id):
codex resume 019eac67-aeb7-7cc0-94f9-b8ba439ff5f4 --dangerously-bypass-approvals-and-sandbox --dangerously-bypass-hook-trust
```

The resumed record **keeps `engine=codex`** (T8's fix — `ResumeCommand` passes `engine=record.engine`),
the resumed `ChatRef` re-attaches the **same** chat id `019eac67-…` (`role: original`, `origin.how:
resume`), and the TUI **re-attached the prior conversation** (the earlier `PONG` turn shown) continuing the
**same** rollout `rollout-…-019eac67-….jsonl`. ✅

---

### Findings summary (the defects this gate surfaced)

| # | Flow(s) | Defect | Severity | Root cause |
|---|---|---|---|---|
| **D1** | fork, handover | new record/`ChatRef` mis-stamped **`engine=claude`** for a Codex source → engine-routed **history ingest fails (empty bundle)**; a later `tx resume` of it would build `claude --resume` | **substantive** | `chat.py` `fork()` / `_finish_handover()` / `_spawn_distiller()` build `SpawnSpec.for_process(...)` **without `engine=source.engine`**; `_spawn` defaults to Claude. (Fix: thread the source engine into the spec — `ResumeCommand` already does.) |
| **D2** | rollover | **`tx rollover` CLI raises `TypeError`** (`new_chat[:8]` on a `None` return); the rollover work itself completes (detached finish) | medium (CLI crash; engine-agnostic) | `RolloverCommand.run` subscripts `ChatOps.rollover()`'s `None` return |
| **D3** | fork | unseeded `codex fork` **defers `SessionStart`** → fork id stays pending until first interaction | expected (codex-plan-flagged) | idle Codex TUI defers `SessionStart` (T2 spike §"SessionStart timing"). **Follow-up: seed the fork.** |
| minor | handover, rollover | catch-up **seed text** hardcodes Claude's `subagents/ + tool-results/` bundle layout | cosmetic | Claude-ism in `chat.py` seed strings (post-T5 neutralisation follow-up) |

**What PASSES on the real worker:** spawn + capture-after-launch (id+path captured, not minted) ·
working→waiting state from real hook events · history ingest (Codex bundle = rollout JSONL **alone**, no
sidecar) · resume (engine stays codex, re-attaches the rollout) · rollover **mechanics** (same record
stays codex, successor captures its own id, catch-up pointer, predecessor closed). The hook payload shape
matches the T2 field reference **on codex 0.138** (stable across the 0.137→0.138 bump).

### DEFERRED COVERAGE (codex-plan (b) — no silent caps)

> T9 verifies the integration against **real codex in a REDIRECTED home** (temp `CODEX_HOME`/`TX_IDE_HOME`).
> **Install against the user's LIVE `~/.codex`** (the real hand-rolled `[tui]` coexistence) is
> **V-T3-sandbox-validated + a post-T9 user-driven step — NOT covered here.** Do not read T9 as proof of
> real-home adoption. Also not exercised: the **full distiller path** for handover/rollover (this run used
> `--self-catch-up`, which exercises the same `_finish_*` worker-spawn that carries D1; the autonomous opus/
> gpt-5.5 distiller spawn is not separately observed, though `_spawn_distiller` shares the D1 omission by
> inspection), and `PreToolUse`/compaction hook events (the trivial prompts ran no tools — as in T2).

### COST (codex-plan (d))

This run spent **real gpt-5.5 on the user's ChatGPT auth** (the user pays the codex side): ~5 short
single-turn sessions (spawn, fork, handover, rollover successor, resume) plus the auto-run seeds. Prompts
were kept trivial ("reply PONG/FORKED", "say hello then stop") to minimise spend.

### Reproduction

1. Worktree `task/T9-e2e-verify` @ `c3bfd1d`. `SANDBOX=$(mktemp -d)`; temp `TX_IDE_HOME`/`CODEX_HOME`;
   copy `~/.codex/{auth.json,models_cache.json,version.json,installation_id}` in **writable**; pre-trust
   the worker project by **realpath** in `<temp>/config.toml`.
2. `TX_IDE_HOME=<temp> CODEX_HOME=<temp> setup/engines/codex.sh install --settings <temp>/config.toml`;
   `tx _init-home`.
3. `tx spawn … --engine codex --prompt … --env CODEX_HOME=<temp>`; read `tx show`, `log.jsonl`, the
   hook-capture log, the bundle dir, and the on-disk rollout. Repeat for `tx fork` / `tx handover
   --self-catch-up` / `tx rollover --self-catch-up` / `tx resume --as`.
4. Re-check `sha256(~/.codex/config.toml|auth.json)` == the pre-run snapshot after every flow; `rm -rf
   "$SANDBOX"`. The live homes are never written.

---

## Re-verification (round 2) — D1 + D2 fix @ `d31ad90`

Round 1 (above) was a **FAIL** that surfaced two fixable defects. The fix landed on `task/fix-d1-d2`
@ `d31ad90` (off `feat/engine-abstraction`); `V-T9-e2e` re-ran the **three failed flows** live against it,
in a **fresh dual sandbox**, by the same method (worker runs the FIX code; the live `~/.codex` is never
written). **Verdict: PASS — D1 and D2 are fixed; all seven parity flows now pass.**

**The fix (confirmed by diff `c3bfd1d..d31ad90`):**
- **D1** — `lib/tx/chat.py` adds `engine=source.engine` to all three `SpawnSpec.for_process(...)` spawns:
  `fork()` (:178), `_finish_handover()` (:360), `_spawn_distiller()` (:511). So `_spawn` no longer
  defaults the engine to Claude for a Codex-sourced op.
- **D2** — `lib/tx/cli.py` `RolloverCommand` stops subscripting `ChatOps.rollover()`'s `None` return
  (the successor id is captured async), printing a fixed message instead.

**Re-run environment:** worker = the FIX code `task/fix-d1-d2` @ `d31ad90` (a detached worktree; its
`bin/tx` + `setup/engines/codex.sh` + `lib` are the fix — the installed shims bake the fix `lib`), codex
`0.138.0`, fresh temp `$TX_IDE_HOME`/`$CODEX_HOME` (writable auth), worker project realpath-trusted. Live
`~/.codex` re-checked **byte-identical** before/after every flow (`config.toml` `a2cacd4a…`, `auth.json`
`c86eadf8…`; no live `hooks.json`); the temp `auth.json` was again not rewritten (no OAuth refresh this
run). Standing gate at the fix was green (orchestrator: 12 suites / 996 checks; the D1 record-engine test
is red-without-fix).

### D1 — fork: `engine=codex` + NON-EMPTY bundle  ✅ FIXED

```
tx fork rv-codex rv-fork
# cmd: codex fork 019eac8b-92c4-… -m gpt-5.5 -c model_reasoning_effort=high --dangerously-bypass-*
```
- **At spawn:** fork record `engine=codex` and the fork `ChatRef` `engine=codex` (round 1: both `claude`).
- **Capture:** unseeded → deferred `SessionStart` (D3, unchanged); on the first typed turn the fork
  captured its **own** id `019eac8b-bf3c-7ca0-8dc3-7492c255aea2` (rollout `forked_from_id:
  019eac8b-92c4-…`).
- **History ingest (the round-1 break):** the bundle `history/93fefe7d-…/019eac8b-bf3c-…/` now holds
  **`transcript.jsonl` = 52293 bytes** (round 1: **empty**) + `.ingest.lock`, **no sidecar** — i.e. the
  engine-routed resolver now globs the Codex rollout (`rollout-…-019eac8b-bf3c-….jsonl`) instead of
  Claude's projects dir. ✅

### D1 — handover: `engine=codex` + NON-EMPTY bundle  ✅ FIXED

```
tx handover rv-codex "say hello then stop" rv-handover --self-catch-up
# worker cmd: codex -m gpt-5.5 -c model_reasoning_effort=high --dangerously-bypass-* '<seed>'
```
- **At spawn:** handover worker record `engine=codex` and its handover `ChatRef` `engine=codex` (round 1:
  `claude`). It captured its own id `019eac8c-6355-7b40-8e0f-7034b2f2c72a`, ran its seeded task (wrote
  `HANDOVER_BRIEF.md` in the temp project), and reached WAITING.
- **History ingest:** on the worker's `Stop`, the bundle `history/1b83cb87-…/019eac8c-6355-…/` now holds
  **`transcript.jsonl` = 84562 bytes** (round 1: **empty**) + `.ingest.lock`, rollout-alone. ✅

### D2 — `tx rollover`: clean exit + record stays `codex`  ✅ FIXED

```
$ tx rollover rv-codex --self-catch-up
Rollover scheduled (self-catch-up); the same session rotates onto a fresh chat when ready
$ echo $?
0
```
- **No `TypeError`** (round 1 crashed with `'NoneType' object is not subscriptable`); **exit code 0**,
  reworded message.
- The rollover mechanics still hold: same record stays `engine=codex`, the predecessor chat
  `019eac8b-92c4-…` is closed (`ended_at` set), and a fresh `rollover` `ChatRef`
  `019eac8d-79c0-7052-b1ab-35d08cb9b096` (`engine=codex`, `origin.how: rollover`) captured the successor's
  own id. ✅

### Net result

| Defect | Round 1 | Round 2 (fix @ `d31ad90`) |
|---|---|---|
| D1 fork — record engine / bundle | `engine=claude` / empty bundle | **`engine=codex` / 52293-byte bundle** ✅ |
| D1 handover — record engine / bundle | `engine=claude` / empty bundle | **`engine=codex` / 84562-byte bundle** ✅ |
| D2 `tx rollover` CLI | `TypeError` crash | **exit 0, record stays `codex`** ✅ |

**All seven parity flows now PASS** on the real worker (spawn+capture, working→waiting, history ingest
(rollout-alone), fork, handover, rollover, resume). **Remaining follow-ups (NOT fix-blockers):** D3 —
seed the unseeded fork so its id is captured at spawn rather than on first interaction; and the minor
Claude-ism in `chat.py`'s handover/rollover catch-up seed text (`"subagents/ + tool-results/"`, harmless
for Codex's rollout-only bundle). Live homes byte-intact throughout; temp sandbox + the detached fix
worktree torn down after.
