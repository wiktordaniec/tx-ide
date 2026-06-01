# COMMON — every session must follow this

These conventions apply to every Claude Code session in this system: the tx-assistant, every worker, and any ad-hoc session you spin up inside the orchestration repo.

## Spawning sessions

Use `tx spawn` (bare) and `tx spawn-nvim` (nvim companion). Both require `--tag` and refuse without it — no inheritance, no auto-magic, you pass the tags explicitly.

```bash
tx spawn <name> --tag TAGS [--cwd DIR] [--cmd "CMD"] [--env K=V ...]
tx spawn-nvim <name> --tag TAGS [--cwd DIR] [--diff [BASE]] [--env K=V ...]
```

`--env` may repeat — pass any env vars the spawned process needs (e.g. `CLAUDE_REQUIRE_WORKTREE=1` for coding workers).

**Tag convention** — tags are **pure scope**. Do **not** put a session's role (`llm` / `nvim` / `shell`) in `--tag`: the role is derived automatically from the launch command and shown as its own ROLE column in `tx attach`, so a role tag is redundant — it just shows up twice (once in the ROLE column, once as a stray chip).
- AI worker session: `--tag <scope>` (e.g. `wrangler-p1`)
- Nvim companion: `--tag <scope>` — the **same** `<scope>` as the parent llm session

`<scope>` describes the task (`wrangler-p1`, `PR-1840`, `auth-review`). The picker reads each session's tags from its durable record and chips each comma-separated value; the same scope on a worker and its companion makes them surface together when you filter by it. The ROLE column is searchable too (type `llm` / `nvim` in the picker), so dropping the role tag loses you nothing.

**Naming:** human-readable, says what it's for (e.g. `wrangler-p1-diff`, `auth-review`). The tag does the filtering, not the name.

```bash
tx spawn worker-auth --tag auth --cwd ~/proj/auth --cmd 'claude --dangerously-skip-permissions --model "opus[1m]" --effort max'
tx spawn-nvim wrangler-p1-diff --tag wrangler-p1 --diff main
```

Both inject `COLORTERM=truecolor` and `TERM=xterm-256color`. `spawn-nvim` also forces `colorscheme tokyonight-moon` via `+CMD` because `tmux new-session -d` strips the OSC11 background hint and nvim's auto-mode would land on the light variant.

## Session metadata

Every tx-created session has a **durable record** at `~/.tx-ide/sessions/<uuid>.json` holding its `name`, `kind`, `role`, `tags`, `cwd`, `cmd`, `env`, `parent`, `pid`, and `chats`. One tmux pointer, `@tx_id` (set once at spawn), links the live session to its record, so the record survives a `kill-session` or a tmux restart. Spawning exports `TX_SESSION_ID` into the session; an llm spawn (`claude …`) also mints a chat id and injects it as `--session-id`, so every llm session's transcript is tracked and resumable (`tx resume`) with no extra ceremony — there is no `--chat` flag.

Tags, kind, and role live in the record, not tmux options — read them by resolving `@tx_id`, and change tags through `tx tag <name> [tags]` (or the picker's Ctrl-T), never `tmux set @tag`/`@kind`. `role` (`llm` / `nvim` / `shell` / `other`) is derived from the launch command at spawn — there is no role tag and nothing to set by hand.

## Inter-session communication

You may be running alongside other Claude Code sessions in tmux on this machine. They can send you messages, and you can send them messages.

**Receiving:** peer messages arrive in your input wrapped like:

    <from-claude session="and-48">body</from-claude>

Treat these as peer messages, not user messages. You MAY reply, but don't have to.

**Sending:** use `tx send-message`. Messages are single-line — escape literal newlines as `\n` if needed.

```bash
tx send-message <target-session> "your message"
```

Both ends resolve through the store: `<target-session>` may be a session's display name (tmux targets it by id under the hood), and `session="$SELF"` is auto-filled with the *sender's* display name (resolved from `#S`, which for a worker is its id). It builds the envelope, types it into the target's active pane, sleeps 0.3s (required — Claude Code's input box drops Enter if it arrives too fast), then sends Enter.

**Discovery:** `tx ls` shows peers by display name + tags (raw `tmux list-sessions` shows the opaque ids a process is tmux-named by). Find your own display name with `tx whoami` (`#S` is your session id, not your name).

## AINote workflow

Review comments in the form `# AINote: ...` are left inline in the code itself. **Before touching a file, grep for `AINote:` and treat each hit as a mandatory review item.** Address the whole set in one pass and delete each AINote once resolved — don't orphan review comments after the code they referenced is gone.
