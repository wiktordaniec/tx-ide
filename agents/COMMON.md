# COMMON — every session must follow this

These conventions apply to every Claude Code session in this system: the tx-assistant, every worker, and any ad-hoc session you spin up inside the orchestration repo.

## Spawning sessions

Use `tx spawn` (bare) and `tx spawn-nvim` (nvim companion). Both require `--tag` and refuse without it — no inheritance, no auto-magic, you pass the tags explicitly.

```bash
tx spawn <name> --tag TAGS [--cwd DIR] [--cmd "CMD"] [--env K=V ...]
tx spawn-nvim <name> --tag TAGS [--cwd DIR] [--diff [BASE]] [--env K=V ...]
```

`--env` may repeat — pass any env vars the spawned process needs (e.g. `CLAUDE_REQUIRE_WORKTREE=1` for coding workers).

**Tag convention** — a scope tag plus a kind tag (`llm` / `nvim`) is the pair:
- AI worker session: `--tag llm,<scope>` (e.g. `llm,wrangler-p1`)
- Nvim companion: `--tag nvim,<scope>` (use the same `<scope>` as the parent llm session)

`<scope>` describes the task (`wrangler-p1`, `PR-1840`, `auth-review`). The picker reads the `@tag` user-option and chips each comma-separated value; same scope on both makes them surface together when you filter by it.

**Naming:** human-readable, says what it's for (e.g. `wrangler-p1-diff`, `auth-review`). The tag does the filtering, not the name.

```bash
tx spawn worker-auth --tag llm,auth --cwd ~/proj/auth --cmd 'claude --dangerously-skip-permissions --model "opus[1m]" --effort max'
tx spawn-nvim wrangler-p1-diff --tag nvim,wrangler-p1 --diff main
```

Both inject `COLORTERM=truecolor` and `TERM=xterm-256color`. `spawn-nvim` also forces `colorscheme tokyonight-moon` via `+CMD` because `tmux new-session -d` strips the OSC11 background hint and nvim's auto-mode would land on the light variant.

## Inter-session communication

You may be running alongside other Claude Code sessions in tmux on this machine. They can send you messages, and you can send them messages.

**Receiving:** peer messages arrive in your input wrapped like:

    <from-claude session="and-48">body</from-claude>

Treat these as peer messages, not user messages. You MAY reply, but don't have to.

**Sending:** use the same envelope. Messages are single-line — escape literal newlines as `\n` if needed.

```bash
SELF=$(tmux display-message -p '#S')
tmux send-keys -t <target-session> "<from-claude session=\"$SELF\">your message</from-claude>"
sleep 0.3
tmux send-keys -t <target-session> Enter
```

The sleep is required — Claude Code's input box drops Enter if it arrives too fast.

**Discovery:** `tmux list-sessions` shows peers. Find your own name with `tmux display-message -p '#S'`.

## AINote workflow

Review comments in the form `# AINote: ...` are left inline in the code itself. **Before touching a file, grep for `AINote:` and treat each hit as a mandatory review item.** Address the whole set in one pass and delete each AINote once resolved — don't orphan review comments after the code they referenced is gone.
