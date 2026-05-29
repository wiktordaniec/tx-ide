# tx-ide

A tmux + Claude Code "view" — one CLI (`tx`) for picking sessions, opening the mailbox, and spawning Claude Code workers (via the `tx-assistant` popup), plus a repo/branch/model statusline.

Layered as an additive install: nothing you've configured in Claude Code or your shell gets overwritten. Everything goes into files **tx-ide owns** (`~/.tx-ide/`, `~/.claude/settings.local.json`) or into clearly-marked blocks of your existing config. For `~/.tmux.conf` specifically: a fresh machine gets the tx-ide default config, and an existing one is only ever replaced if you confirm (always backed up). `./uninstall` puts it all back.

## What you get

| Command | What it does |
|---|---|
| `tx start` | Set up the tx-ide view: create the `Views` home-base session and warm the `tx-assistant` Claude Code session. `--restart` recreates the assistant. |
| `tx attach` | Fuzzy-pick a tmux session and nest-attach in the current pane. Filters by each session's tags (from its durable record) rendered as chips. Supports local + remote (`tx attach --all`, `tx attach --host`). Bound to `prefix + t` as a centered popup. |
| `tx tag <name> [tags]` | Read or set a session's tags in the durable store — the chips shown in the picker and pane borders. With `tags` (comma-separated): set them; without: print them. The same store is written by `tx spawn --tag` and the picker's Ctrl-T; tags are never `tmux set @tag` options. |
| `tx mailbox` | Curses TUI mailbox. Shows unread Claude Code Stop events + active sessions, grouped by tmux session and tagged. `Enter` jumps to the hosting pane. Bound to `prefix + m`. |
| `prefix + /` | Open a one-line `tx-assistant>` prompt. Whatever you type is forwarded to the persistent `tx-assistant` Claude session (Haiku, low effort) that runs tmux/tx operations and spawns Claude Code workers on your behalf. Fire and forget — attach via `tx attach` (filter `tx-assistant`) to see what it did. |
| `tx` / `tx help` | Show the command summary. |
| Statusline | Two-line Claude Code statusline: repo / worktree / branch · model · tokens · 5h-rate-limit · 7d-rate-limit. |
| Pane border integration | Pane borders show inner attached session name + tag chips (from the durable record). Remote ssh-attached panes are prefixed `(r)`. |
| Claude scroll intercept | `C-u` / `C-d` scroll Claude Code's TUI (PageUp / PageDown). Pass through everywhere else. |
| `M-1..9` / `User0..8` | Pane and window quick-switch. Requires your terminal to emit the matching escape sequences (`setup/iterm.sh` configures iTerm2). |

## Install

```bash
git clone <this-repo> ~/Desktop/Coding/tx-ide
cd ~/Desktop/Coding/tx-ide
./install
```

The installer is **click-through** — it tells you what it will do, asks for one confirm, then runs every step idempotently with backups. Re-run any time to update or to reconcile drift.

The installer reloads tmux's config for you if a server is running. After it finishes:
```bash
tx help     # see available commands
tx start    # create Views + warm the tx-assistant
```

## Uninstall

```bash
./uninstall
```

Reverses every change `./install` made. Leaves your inbox data (`~/.claude/mailbox/`), your role overrides (`~/.tx-ide/user-agents/`), your session metadata (`~/.tx-ide/sessions/`), and the repo itself in place.

## What install actually changes

**Creates / symlinks** (tx-ide owns these — safe to delete by hand):
- `~/.local/bin/{tx,tx-assistant,tmux-pane-for-session,tmux-pane-session-name}` — symlinks to `bin/`
- `~/.claude/hooks/mailbox/` — symlink to `claude/hooks/`
- `~/.claude/statusline.sh` — symlink to `claude/statusline.sh`
- `~/.tx-ide/agents/` — symlink to `agents/` (shipped role files; updates with `git pull`)
- `~/.tx-ide/user-agents/` — empty directory for your role overrides + `.local.md` companions
- `~/.tx-ide/sessions/` — durable per-session metadata, one `<uuid>.json` per tx-created session (created on first spawn, not by install; preserved on uninstall)
- `~/.tx-ide/tmux.conf` — one-line shim that `run-shell`s the view
- `~/.claude/settings.local.json` — Claude Code merges this with your `settings.json`

**Sets up** `~/.tmux.conf` (with backup):
- No existing file → installs the tx-ide default ([`tmux/tmux.conf`](tmux/tmux.conf))
- Existing file → adds a marked block (`# === BEGIN tx-ide ===`, a `source-file` line, `# === END tx-ide ===`), or — with a `[y/N]` confirm — replaces it with the default

**Does not touch**:
- `~/.claude/settings.json` (Claude Code concatenates `hooks` from `settings.local.json` automatically)
- `~/.claude/CLAUDE.md`
- Your existing `~/.tmux.conf`, unless you confirm a replace (`[y/N]`, backed up)
- iTerm preferences if iTerm is currently running (you get a hint to re-run `setup/iterm.sh` later)

## Customizing the view

The plugin reads these tmux user-options. All default `on`. Set in your `~/.tmux.conf` **above** the `source-file ~/.tx-ide/tmux.conf` line:

```tmux
set -g @tx-ide-popups          on             # prefix+t (tx), prefix+m (mx)
set -g @tx-ide-pane-borders    on             # pane-border-format integration + colors
set -g @tx-ide-claude-scroll   on             # C-u/C-d → PageUp/PageDown in Claude panes
set -g @tx-ide-pane-keys       on             # M-1..9 → select-pane
set -g @tx-ide-window-keys     on             # User0..8 → select-window
set -g @tx-ide-palette         tokyonight-night   # or 'off' to skip color overrides
```

tmux's "last write wins" means anything you bind *after* the source-file line wins over tx-ide's defaults.

The installer ships a full default `~/.tmux.conf` ([`tmux/tmux.conf`](tmux/tmux.conf)) — prefix, copy-mode, status bar, splits, resize keys, TPM, all layered over the tx-ide view. It's installed automatically if you have no `~/.tmux.conf`; if you already have one, the installer offers to replace it (`[y/N]`, backed up first) or just appends the `source-file` block.

### Theme

tx-ide assumes your Claude Code theme is `dark-ansi` so terminal colors line up with `tx` / `tx mailbox`. We don't set it for you — change yours via `/config` or `~/.claude/settings.json` if you want the integration to look right.

### Palette

Hex literals in `shared/palette.sh` (tokyonight-night). `install` bundles `setup/tokyonight-night.itermcolors` and imports it into iTerm as a Custom Color Preset; it then asks `[Y/n]` whether to also apply it to your Default profile. Say yes for the full out-of-the-box experience, or pick it manually later from iTerm Settings → Profiles → Colors → Color Presets.

Switching to a different palette is a manual edit: update `shared/palette.sh`, regenerate `setup/tokyonight-<variant>.itermcolors` via `setup/_gen-itermcolors.py`, then re-run `./install`. See the comments at the top of `shared/palette.sh`.

## Orchestration via the tx-assistant

The `tx-assistant` is your orchestration surface. It's a Haiku Claude Code session pinned to a single tmux session named `tx-assistant`, talked to via the `prefix+/` one-line popup. Each line you send is one operation: spawn a worker, kill a session, retag, message a peer.

Run `tx start` once to create the `Views` home-base and warm the assistant. After that, hit `prefix+/` from anywhere in tmux and tell it what to do — e.g. "spawn a coding worker for ~/Code/my-project on the auth-rewrite plan" and it'll launch a worker session with `-c ~/Code/my-project` that follows `agents/DEVELOPER.md`.

### Role files

The shipped roles live in this repo under `agents/` and are exposed at `~/.tx-ide/agents/` via symlink, so `git pull` updates them for every install.

- `agents/COMMON.md` — conventions every session must follow
- `agents/TX-ASSISTANT.md` — assistant role (how to interpret requests, what to spawn, guard rails)
- `agents/DEVELOPER.md` — coding worker role (worktree-first, atomic commits, draft PR)

### Customizing

Drop files in `~/.tx-ide/user-agents/`:

- `~/.tx-ide/user-agents/<ROLE>.local.md` — **extends** the shipped role. Read alongside it; both win.
- `~/.tx-ide/user-agents/<ROLE>.md` — **replaces** the shipped role outright (same filename shadows it).
- `~/.tx-ide/user-agents/<NEW-ROLE>.md` — adds a brand new role you can spawn workers for.

Every prompt template (in `bin/tx-assistant` and `agents/TX-ASSISTANT.md`'s worker-spawn recipe) reads `~/.tx-ide/agents/<ROLE>.md` followed by both `~/.tx-ide/user-agents/<ROLE>.md` and `~/.tx-ide/user-agents/<ROLE>.local.md` if they exist.

## Requirements

- macOS (iTerm2 + `say` + `defaults write` are macOS-only; Linux support is a future PR)
- `tmux`, `fzf`, `python3` (stdlib), `openssl` (for hook entry IDs)

## License

MIT.
