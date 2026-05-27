# COMMON — every session must follow this

These conventions apply to every Claude Code session in this system: the tx-assistant, every worker, and any ad-hoc session you spin up inside the orchestration repo.

## Spawning sessions

Use `tx spawn` (bare) and `tx spawn-nvim` (nvim companion). Both require `--tag` and refuse without it — no inheritance, no auto-magic, you pass the tags explicitly.

```bash
tx spawn <name> --tag TAGS [--cwd DIR] [--cmd "CMD"] [--chat] [--env K=V ...]
tx spawn-nvim <name> --tag TAGS [--cwd DIR] [--diff [BASE]] [--env K=V ...]
```

`--env` may repeat — pass any env vars the spawned process needs (e.g. `CLAUDE_REQUIRE_WORKTREE=1` for coding workers).

**Tag convention** — a scope tag plus a kind tag (`llm` / `nvim`) is the pair:
- AI worker session: `--tag llm,<scope>` (e.g. `llm,wrangler-p1`)
- Nvim companion: `--tag nvim,<scope>` (use the same `<scope>` as the parent llm session)

`<scope>` describes the task (`wrangler-p1`, `PR-1840`, `auth-review`). The picker reads each session's tags from its durable record and chips each comma-separated value; same scope on both makes them surface together when you filter by it.

**Naming:** human-readable, says what it's for (e.g. `wrangler-p1-diff`, `auth-review`). The tag does the filtering, not the name.

```bash
tx spawn worker-auth --tag llm,auth --cwd ~/proj/auth --cmd 'claude --dangerously-skip-permissions --model "opus[1m]" --effort max'
tx spawn-nvim wrangler-p1-diff --tag nvim,wrangler-p1 --diff main
```

Both inject `COLORTERM=truecolor` and `TERM=xterm-256color`. `spawn-nvim` also forces `colorscheme tokyonight-moon` via `+CMD` because `tmux new-session -d` strips the OSC11 background hint and nvim's auto-mode would land on the light variant.

## Session metadata

Every tx-created session has a **durable record** at `~/.tx-ide/sessions/<uuid>.json` — the single source of truth for its `name`, `kind`, `tags`, `cwd`, `cmd`, `env`, `parent`, `pid`, and `chats`. The session is linked to its record by one opaque tmux pointer, `@tx_id`, set once at spawn; the record outlives a `kill-session` and a tmux restart. Spawning also exports `TX_SESSION_ID` (the record uuid) into the session, plus `TX_CHAT_ID` when you pass `--chat` to `tx spawn` — that mints a conversation uuid so the command can resume its transcript (e.g. `claude --session-id "$TX_CHAT_ID"`).

Tags and kind are **not** tmux options. Don't `tmux set @tag`/`@kind` — change tags through `tx` (the picker's Ctrl-T writes the record); reading them means resolving the session's `@tx_id` to its record.

### Handover restart

Refresh a session's context without losing its identity: have it write a handover note, `tmux kill-session -t <name>`, then `tx restart <name> --handover <path>`. Restart re-spawns from the stored tags/cwd/cmd/kind, reuses the **same** `@tx_id`, records a fresh chat incarnation, and injects `TX_HANDOVER_FILE=<path>` so the new process picks up where the last one left off.

## Inter-session communication

You may be running alongside other Claude Code sessions in tmux on this machine. They can send you messages, and you can send them messages.

**Receiving:** peer messages arrive in your input wrapped like:

    <from-claude session="and-48">body</from-claude>

Treat these as peer messages, not user messages. You MAY reply, but don't have to.

**Sending:** use `tx send-message`. Messages are single-line — escape literal newlines as `\n` if needed.

```bash
tx send-message <target-session> "your message"
```

It auto-fills `session="$SELF"` from `tmux display-message -p '#S'`, builds the envelope, types it into `<target>`'s active pane, sleeps 0.3s (required — Claude Code's input box drops Enter if it arrives too fast), then sends Enter.

**Discovery:** `tmux list-sessions` shows peers. Find your own name with `tmux display-message -p '#S'`.

## AINote workflow

Review comments in the form `# AINote: ...` are left inline in the code itself. **Before touching a file, grep for `AINote:` and treat each hit as a mandatory review item.** Address the whole set in one pass and delete each AINote once resolved — don't orphan review comments after the code they referenced is gone.
