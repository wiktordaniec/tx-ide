# COMMON — every session must follow this

These conventions apply to every agent session in this system: the tx-assistant, every worker, and any ad-hoc session you spin up inside the orchestration repo.

## Spawning sessions

Use `tx spawn` (bare) and `tx spawn-nvim` (nvim companion). Both require `--tag` and refuse without it — no inheritance, no auto-magic, you pass the tags explicitly.

```bash
tx spawn <name> --tag TAGS [--cwd DIR] [--cmd "CMD"] [--env K=V ...]
tx spawn-nvim <name> --tag TAGS [--cwd DIR] [--diff [BASE]] [--env K=V ...]
```

`--env` may repeat — pass any env vars the spawned process needs (e.g. the require-worktree env vars for coding workers — see **§ Spawning workers**).

**Tag convention** — tags are **pure scope**. Do **not** put a session's role (`llm` / `nvim` / `shell`) in `--tag`: the role is derived automatically from the launch command and shown as its own ROLE column in `tx attach`, so a role tag is redundant — it just shows up twice (once in the ROLE column, once as a stray chip).
- AI worker session: `--tag <scope>` (e.g. `wrangler-p1`)
- Nvim companion: `--tag <scope>` — the **same** `<scope>` as the parent llm session

`<scope>` describes the task (`wrangler-p1`, `PR-1840`, `auth-review`). The picker reads each session's tags from its durable record and chips each comma-separated value; the same scope on a worker and its companion makes them surface together when you filter by it. The ROLE column is searchable too (type `llm` / `nvim` in the picker), so dropping the role tag loses you nothing.

**Naming:** human-readable, says what it's for (e.g. `wrangler-p1-diff`, `auth-review`). The tag does the filtering, not the name.

```bash
tx spawn build-watch --tag wrangler-p1 --cmd 'npm run watch'   # an ad-hoc process
tx spawn-nvim wrangler-p1-diff --tag wrangler-p1 --diff main   # an nvim companion
```

An agent **worker** is also a `tx spawn`, but it needs a priming prompt in `--cmd` — see **§ Spawning workers** below. A bare agent CLI with no priming never reads these conventions.

Both inject `COLORTERM=truecolor` and `TERM=xterm-256color`. `spawn-nvim` also forces `colorscheme tokyonight-moon` via `+CMD` because `tmux new-session -d` strips the OSC11 background hint and nvim's auto-mode would land on the light variant.

## Spawning workers

When you need to delegate work — coding, scoping, planning, or research/exploration — spawn an agent worker. The mechanics are `tx spawn` above; what turns a bare agent CLI into a *worker* is the **priming prompt** passed through `--cmd`. Spawn one with no priming and it never reads these conventions — it has no role, no standards, no worktree discipline.

```bash
tx spawn <name> --tag <scope> --cwd <cwd> \
  --cmd 'claude --dangerously-skip-permissions --model "opus[1m]" --effort max "<priming>"'
```

- `<name>` — short, descriptive (`orchestrator-cleanup`, `auth-review`).
- `<scope>` — the single work-scope tag (`wrangler-p1`, `PR-1840`, `cleanup`); no role. An nvim companion takes the **same** scope.
- `<cwd>` — the project root the worker operates in.
- Model + effort: `--model "opus[1m]"` and `--effort max` are the defaults; don't downgrade unless asked.
- Keep `<priming>` short and single-line — long, quoted, special-char-laden prompts crash tmux input.

For **coding workers**, also pass `TX_REQUIRE_WORKTREE=1`. It trips a PreToolUse hook (a user-installed `~/.claude/settings.json` guard — **not** a tx-ide feature) that blocks Write/Edit until the worker `cd`s into a linked worktree:

```bash
tx spawn <name> --tag <scope> --cwd <cwd> \
  --env TX_REQUIRE_WORKTREE=1 \
  --cmd 'claude --dangerously-skip-permissions --model "opus[1m]" --effort max "<priming>"'
```

### Worker priming

`<priming>` **opens with the role-file read instruction** so the worker self-loads these conventions, then a short imperative telling it what to do. For a coding worker:

```
Read ~/.tx-ide/agents/COMMON.md and ~/.tx-ide/agents/DEVELOPER.md as your first actions. Then, if they exist, also read ~/.tx-ide/user-agents/COMMON.md, ~/.tx-ide/user-agents/COMMON.local.md, ~/.tx-ide/user-agents/DEVELOPER.md, and ~/.tx-ide/user-agents/DEVELOPER.local.md (any user-agents/X.md replaces the shipped one; any user-agents/X.local.md extends it). Follow all of these for the duration of this session.
```

`DEVELOPER.md` is the only role tx-ide ships today. If you've dropped another role file under `~/.tx-ide/user-agents/`, swap its name in for `DEVELOPER`; a named role with no shipped or user-agent file is unknown — don't guess.

Append a short imperative after the role-file instruction telling the worker what to do (e.g., `Then implement the plan at ~/Code/foo/.claude/plans/auth-rewrite.md.`).

## Session metadata

Every tx-created session has a **durable record** at `~/.tx-ide/sessions/<uuid>.json` holding its `name`, `kind`, `role`, `tags`, `cwd`, `cmd`, `env`, `parent`, `pid`, and `chats`. One tmux pointer, `@tx_id` (set once at spawn), links the live session to its record, so the record survives a `kill-session` or a tmux restart. Spawning exports `TX_SESSION_ID` into the session; an llm spawn also records a chat id for the session, so every llm session's transcript is tracked and resumable (`tx resume`) with no extra ceremony — there is no `--chat` flag.

Tags, kind, and role live in the record, not tmux options — read them by resolving `@tx_id`, and change tags through `tx tag <name> [tags]` (or the picker's Ctrl-T), never `tmux set @tag`/`@kind`. `role` (`llm` / `nvim` / `shell` / `other`) is derived from the launch command at spawn — there is no role tag and nothing to set by hand.

## Inter-session communication

You may be running alongside other agent sessions in tmux on this machine. They can send you messages, and you can send them messages.

**Receiving:** peer messages arrive in your input wrapped like:

    <from-agent session="and-48">body</from-agent>

Treat these as peer messages, not user messages. You MAY reply, but don't have to.

**Sending:** use `tx send-message`. Messages are single-line — escape literal newlines as `\n` if needed.

```bash
tx send-message <target-session> "your message"
```

Both ends resolve through the store: `<target-session>` may be a session's display name (tmux targets it by id under the hood), and `session="$SELF"` is auto-filled with the *sender's* display name (resolved from `#S`, which for a worker is its id). It builds the envelope, types it into the target's active pane, sleeps 0.3s (required — the agent's input box drops Enter if it arrives too fast), then sends Enter.

**Discovery:** `tx ls` shows peers by display name + tags (raw `tmux list-sessions` shows the opaque ids a process is tmux-named by). Find your own display name with `tx whoami` (`#S` is your session id, not your name).

## AINote workflow

Review comments in the form `# AINote: ...` are left inline in the code itself. **Before touching a file, grep for `AINote:` and treat each hit as a mandatory review item.** Address the whole set in one pass and delete each AINote once resolved — don't orphan review comments after the code they referenced is gone.
