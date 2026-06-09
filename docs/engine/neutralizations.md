# Neutralizations — making the cross-cutting surface engine-agnostic (Phase 4 / task T5)

The engine seam (`design.md` §2/§5) put every engine-specific behavior behind an adapter. What this
doc covers is the **cross-cutting** surface that named "Claude" *outside* the adapter — the peer
envelope, the worktree convention, the require-worktree env, a tmux option, and the persona/recipe
prose — neutralized so the harness reads engine-agnostic. tx-assistant stays **Claude-by-default**
(`design.md` §4.10); neutral wording is about not *assuming* Claude in shared text, not about removing
the default.

## 1. Peer envelope — build neutral, parse both (the one behavioral change)

`tx send-message` types an envelope into the recipient's pane; the message reconstructor
(`lib/tx/messages.py`) reads it back out of the transcript by structure alone. Both ends move from the
Claude-named tag to a neutral one:

- **Build** `<from-agent session="X">body</from-agent>` — `messages.build_envelope()`, called by
  `service.send_message()`.
- **Parse BOTH** `<from-agent>` (new) **and** the legacy `<from-claude>` (old) — `messages._PEER`.
  Back-compat is **mandatory and indefinite**: a live Claude session mid-rollover still emits the old
  tag, so the parser must keep accepting it. This is the **only** surviving `from-claude` reference in
  `lib` — the parse path plus its comment, never the builder.
- **Test**: `tests/test_envelope.py` asserts the builder emits `<from-agent>`, the parser classifies
  the old and the new envelope **identically** (kind / via / sender / body) across every sender-role
  case, and build → parse round-trips. `tests/test_messages.py` continues to exercise `<from-claude>`,
  so it doubles as the back-compat regression.

## 2. Worktree convention `.claude/worktrees` → `.tx-ide/worktrees`

The repo-local worktree dir convention (`agents/DEVELOPER.md`) renames to `.tx-ide/worktrees/<name>`.
The only code reference was the illustrative munge example in `lib/tx/engines/claude.py` (cosmetic —
the munge *function* is unchanged; the path in the comment is just kept accurate).

## 3. Require-worktree env `CLAUDE_REQUIRE_WORKTREE` → `TX_REQUIRE_WORKTREE` — and the known gap

This started as `design.md` §4.9 "`CLAUDE_REQUIRE_WORKTREE` → `TX_REQUIRE_WORKTREE` **+ a Codex
`PreToolUse` guard-hook equivalent**". On investigation that premise is **false**, and codex-plan
ruled accordingly (the T5 guard ruling). The verified picture:

- **Nothing in `lib/tx/*.py` reads `REQUIRE_WORKTREE`.** The only Claude-side enforcement is a
  **user-installed** `~/.claude/settings.json` `PreToolUse` hook that blocks Write/Edit until the
  session's cwd is a linked worktree. tx-ide's installer merely *preserves* that interleaved user entry
  (`setup/engines/claude.sh` match-by-marker) — it never writes it. **It is not a tx-ide feature.**
- So the env is a **convention-level rail**, not a hard boundary tx enforces. The rename is therefore a
  **doc + spawn-convention** change. Nothing in the codebase needs to learn the new name.

### Back-compat: the spawn convention sets BOTH names (transitional)

Because the user's existing guard hook reads the **old** name and tx cannot edit a user-owned hook, a
bare rename would **silently break** the guard (the worker would pass `TX_REQUIRE_WORKTREE=1` while the
hook still watches `CLAUDE_REQUIRE_WORKTREE`). Mirroring the envelope parse-both, the coding-worker
spawn convention sets **both** until the user migrates their hook, then drops the old one:

```bash
tx spawn <name> --tag <scope> --cwd <cwd> \
  --env CLAUDE_REQUIRE_WORKTREE=1 --env TX_REQUIRE_WORKTREE=1 \
  --cmd 'claude … "<priming>"'
```

(`--env` is `action="append"` — one `KEY=VALUE` per flag — so the flag is **repeated**, not
space-joined.) `TX_REQUIRE_WORKTREE` is the forward name; `CLAUDE_REQUIRE_WORKTREE` survives **only**
here, in the transitional convention and the docs — the env analog of the surviving `from-claude` parse.

### The gap, stated plainly (no silent cap)

> tx-ide does **not** enforce require-worktree as a hard per-tool block for **either** engine. The env
> is plumbed through spawn; whether it blocks anything depends on a **user-owned** Claude hook. There
> is **no** Codex equivalent — and a Codex `PreToolUse` guard would be **moot** under the yolo flags
> tx-built Codex workers carry (`--dangerously-bypass-approvals-and-sandbox` ⇒
> `permission_mode = bypassPermissions`, which skips per-tool hook denial, exactly as Claude's
> `--dangerously-skip-permissions` does).

### The real future mechanism (NOT-T5)

The correct engine-agnostic enforcement is **not** a per-engine `PreToolUse` hook but a tx
**spawn-time** check: when `TX_REQUIRE_WORKTREE` is set, `tx spawn` **refuses to launch a coding
worker into a non-worktree cwd**. It lives on the shared, engine-blind spawn path; it is **not** moot
under yolo (it gates before the agent starts, not per tool call); and it needs no per-engine hook.
This **supersedes** the §4.9 `PreToolUse` framing and is tracked as a **separate follow-up** — out of
T5's scope.

## 4. tmux option `@tx-ide-claude-scroll` → `@tx-ide-agent-scroll`

The C-u/C-d → PageUp/PageDown intercept already recognized both `claude` and `codex` panes (T8); T5
finishes the job by renaming the user-option itself (`tmux/tx-ide.tmux` set/read sites + the internal
`agent_scroll` variable, and the `README.md` example). The predicate is unchanged.

## 5. Personas read engine-neutral

`agents/COMMON.md` / `DEVELOPER.md` / `TX-ASSISTANT.md`: cross-cutting "Claude Code session/worker"
language becomes "agent session/worker", the envelope and input-box references neutralize, and the
worktree/env conventions track §2/§3. The concrete default spawn command stays `claude …` (Claude is
the default engine, `design.md` §4.3), and **tx-assistant stays Claude-by-default** (§4.10) — engine
selection for it is out of v1 scope.
