# COMMON — every session must follow this

These conventions apply to every Claude Code session in this system: the leader, every worker, and any ad-hoc session you spin up inside the orchestration repo.

## Session tags for `tx`

The `tx` picker reads the tmux user-option `@tag` (singular) and renders comma-separated values as chips. **Tag every tmux session you spawn** so it's discoverable in `tx`.

```bash
tmux set -t <session-name> @tag "<tag1>,<tag2>,..."
```

Use `@tag`, not `@tags` — the plural form is silently ignored.

Pick tags that describe the session's purpose (`llm`, `nvim`, `wrangler-p1`, `PR-1840`). A scope tag (`wrangler-p1`) plus a kind tag (`llm` / `nvim`) is the typical pair.

## Nvim companion sessions

During long-running work you may spawn a companion nvim tmux session for viewing diffs or files without cluttering your main session.

**Naming:** give it a human-readable name that says what it's for (e.g. `wrangler-p1-diff`, `auth-review`). Don't try to make the name grep-able — tags handle filtering.

**Tagging rule:** a companion nvim session mirrors its parent dev session's `@tag` **exactly**, swapping `llm` → `nvim`. No extra tags. Example: parent `llm,wrangler-p1,PR-1840` → companion `nvim,wrangler-p1,PR-1840`. This way `tx` filters by scope (`wrangler-p1`, `PR-1840`) surface both, and `nvim` filters surface all viewers.

```bash
PARENT_TAGS=$(tmux show -t <parent-session> -v @tag)
NVIM_TAGS=${PARENT_TAGS//llm/nvim}

tmux new-session -d -s <nvim-session-name> -c <worktree-path> \
  -e COLORTERM=truecolor -e TERM=xterm-256color \
  'nvim +"DiffviewOpen master"'
tmux set -t <nvim-session-name> @tag "$NVIM_TAGS"
```

If diffview.nvim is available, prefer `+"DiffviewOpen master"` to open straight into a diff view. Otherwise just `nvim`.

**Color snag:** `tmux new-session -d` does not propagate iTerm's dark-background OSC11 hint, so nvim's tokyonight auto-mode picks the light variant (tokyonight-day). The `-e COLORTERM=truecolor -e TERM=xterm-256color` flags fix true color but not background detection. If colors look washed out, send this after launch:

```bash
tmux send-keys -t <nvim-session-name> ":set background=dark | colorscheme tokyonight-moon" Enter
```

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
