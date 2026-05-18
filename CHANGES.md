# Changes — extraction from claude-server

Design + migration notes for the initial cut of tx-ide. Intended audience: future-self when iterating before the public release.

## What was extracted

Source: `~/Desktop/Coding/claude-server/` (personal dotfile + Claude orchestration repo).

| From claude-server | → | tx-ide-private | Notes |
|---|---|---|---|
| `tmux/tx.sh` | → | `bin/tx` | dropped `.sh` extension (CLI, not a sourced library) |
| `claude-config/hooks/mailbox/mx.py` | → | `bin/mx` | reroutes `paths.py` import to `<repo>/claude/hooks/` |
| `tmux/pane-for-session.sh` | → | `bin/tmux-pane-for-session` | unchanged |
| `tmux/pane-session-name.sh` | → | `bin/tmux-pane-session-name` | palette source path updated |
| `tmux/palette.sh` | → | `shared/palette.sh` | the spine — one source of truth for tokyonight-night hex |
| `claude-config/hooks/mailbox/{pre,post,end}.sh` | → | `claude/hooks/{pre,post,end}.sh` | unchanged |
| `claude-config/hooks/mailbox/{mx_speaker,speak,paths}.{py,sh}` | → | `claude/hooks/...` | unchanged |
| `tmux/setup.sh`'s `mx-speaker` block | → | `claude/hooks/start-speaker.sh` | extracted as its own script |
| `claude-config/statusline-command.sh` | → | `claude/statusline.sh` | renamed |
| (new) | → | `tmux/tx-ide.tmux` | reads `@tx-ide-*` options, emits temp tmux.conf, source-files it |

Plus brand-new pieces: `install.sh`, `uninstall.sh`, `tx-ide-doctor`, `init-leader.sh`, `setup/iterm.sh`, `leader-template/`, `README.md`, `LICENSE`.

## Architecture decisions (and why)

### Three layers, all additive

| Layer | Mechanism | Writes to |
|---|---|---|
| **Scripts** | install.sh symlinks repo → `~/.local/bin/`, `~/.claude/hooks/mailbox/`, `~/.claude/statusline.sh` | absolute symlinks; repo updates propagate instantly |
| **Tmux view** | install.sh adds one `source-file ~/.tx-ide/tmux.conf` block to `~/.tmux.conf` (with markers + backup); the shim file `run-shell`s `tmux/tx-ide.tmux` | three lines in `~/.tmux.conf` |
| **Claude settings** | install.sh writes mailbox hooks + statusLine into `~/.claude/settings.json` (with backup); tracks ownership via a top-level `_tx_ide_managed` marker key | only the entries declared in the marker — everything else is left alone |

Principle the user articulated: **anything tx-ide adds must be overwritable by the user.** Symlinks always reflect repo state (immutable in spirit). tmux's last-write-wins handles bindings. Marker-tracked settings entries are uniquely identifiable so uninstall removes only ours.

### Rejected: TPM plugin distribution

Considered shipping as a TPM plugin (`set -g @plugin 'wiktordaniec/tx-ide'`). Rejected because the user wanted a **single click-through `install.sh`** that wires everything in one step. TPM would require the user to also (a) add the @plugin line, (b) hit prefix+I, (c) separately wire the Claude-side hooks. install.sh does it all.

The repo is *still* a sensible source-file target — `tmux/tx-ide.tmux` can be loaded by any mechanism that calls `tmux source-file` on it. Just not what we ship.

### Rejected: `~/.claude/settings.local.json` for our entries

The original plan was to write tx-ide's hooks + statusLine to `settings.local.json` so we'd never touch the user's `settings.json`. A pre-implementation check claimed user-scope `settings.local.json` composes additively into `settings.json` for hooks. **Wrong empirically** — hooks added there never fired (inbox stayed silent across session restarts). Likely the composition behavior only applies to project-scope `<project>/.claude/settings.local.json`, not user-scope.

Pivot: write directly to `settings.json`, with a `_tx_ide_managed` marker for ownership tracking + drift detection. Net result is similar (we still own only what we add), with slightly more code.

### Leader / worker structure

Orchestration template lives in `leader-template/`. `init-leader.sh <path>` scaffolds it into the target directory, refusing to overwrite existing files.

Structure inside a scaffolded leader directory:
```
<leader>/
├── CLAUDE.md            # short pointer — auto-loaded by Claude Code as project context
├── agents/
│   ├── COMMON.md        # conventions every session must follow (tx tags, peer messaging, …)
│   ├── LEADER.md        # the leader's role + spawn templates
│   └── DEVELOPER.md     # coding-worker role (no code-tour yet — pending nvim PR)
└── start-leader.sh      # spawns the leader tmux session
```

Role files load via spawn prompt: `"Read agents/COMMON.md and agents/<ROLE>.md as your first actions."` Workers spawned into worktrees of the leader repo inherit `agents/COMMON.md` via project context, so tags + peer-messaging conventions are universal without us touching `~/.claude/CLAUDE.md`.

### What tx-ide explicitly does NOT touch

- `~/.claude/CLAUDE.md` — user's voice to Claude across all projects, off-limits
- `~/.claude/settings.json` entries we didn't add (peon-ping, discord-agent-updates, permissions, theme, plugins, …)
- `~/.tmux.conf` lines outside the marked source-file block
- iTerm preferences while iTerm is running (printed hint instead — half-install allowed per the "some people don't use iTerm" call)
- terminal preferences for any terminal that isn't iTerm

## Implementation details worth recording

### `tmux/tx-ide.tmux` — write a temp conf, source-file it

First attempt invoked `tmux bind-key t setenv ... \; display-popup ...` directly from bash. Shell escaping of `\;` happens once (bash sees `;`), and tmux's own command parser also sees `;` as a separator — so `prefix+t` got the `setenv` half but lost `display-popup`. Fix: compose all bindings as a tmux.conf fragment in a `mktemp` file and `tmux source-file` it. Tmux's parser then handles `\;` correctly.

### `_tx_ide_managed` marker shape

```json
{
  "_tx_ide_managed": {
    "version": 1,
    "hook_commands": {
      "Stop": "$HOME/.claude/hooks/mailbox/post.sh",
      "PermissionRequest": "$HOME/.claude/hooks/mailbox/post.sh",
      "UserPromptSubmit": "$HOME/.claude/hooks/mailbox/pre.sh",
      "SessionEnd": "$HOME/.claude/hooks/mailbox/end.sh"
    },
    "statusLine_command": "bash $HOME/.claude/statusline.sh"
  }
}
```

- `version` is for future migrations.
- `hook_commands` maps event → command. Uninstall iterates and removes only entries whose command matches.
- `statusLine_command` records what we set so uninstall removes the `statusLine` block only if it still matches.
- Claude Code ignores unknown top-level keys, so this is safe.

### `tx-ide-doctor` — the drift contract

Read-only, idempotent. Sections:
1. CLIs on PATH (symlinks point at this repo)
2. Claude hooks dir + statusline.sh symlinks
3. `settings.json`: marker present, hooks match marker declaration, statusLine matches
4. tmux config wiring (tx-ide.tmux shim + source-file line in `~/.tmux.conf`)
5. Live tmux bindings (only if a server is running)
6. iTerm GlobalKeyMap (if `$TERM_PROGRAM=iTerm.app`)
7. mx-speaker daemon PID alive

Drift = re-run `install.sh` to reconcile. Doctor never modifies anything.

## Bugs found during dogfooding (and fixed)

| Symptom | Root cause | Fix |
|---|---|---|
| `prefix+t` bound `setenv` only, not `display-popup` | `\;` from bash → tmux's parser saw two commands | Write temp conf + `source-file` it |
| Doctor reported tx/mx bindings "not bound" after reload | `tmux list-keys` uses variable whitespace; regex assumed single spaces | Use `[[:space:]]+` in patterns |
| Hooks added to `settings.local.json` never fired | User-scope `settings.local.json` apparently isn't composed into settings.json (only project-scope is) | Switch to writing `settings.json` directly + marker key |
| Bottom statusline disappeared from all sessions | Cleanup commit removed statusline-command.sh + the statusLine entry from settings.json; settings.local.json that replaced it wasn't honored at user scope | Restored hooks + statusLine in settings.json; settings strategy rewrite (above) |
| Root of repo gained accidental `CLAUDE.md` / `agents/` / `start-leader.sh` | `init-leader.sh` was pointed at the tx-ide repo path itself | Added guard refusing to scaffold into a path that contains `leader-template/` or equals `$REPO` |

## Companion change in claude-server

`extract/tx-ide` branch removes 1753 lines:
- Deleted: `tmux/{tx,palette,pane-for-session,pane-session-name}.sh`, `claude-config/hooks/mailbox/`, `claude-config/statusline-command.sh`
- Trimmed: `install.sh`, `tmux/setup.sh`, `tmux/tmux.conf` (8 view-binding sections moved into tx-ide.tmux), `tmux/README.md`, `claude-config/CLAUDE.md` (path reference)

claude-server's `tmux.conf` keeps its personal opinions (prefix change, splits, resize, copy-mode, Views toggle, TPM); its view layer now comes from tx-ide's source-file block (auto-added by install.sh on first run).

After the cleanup branch landed locally, `~/.claude/settings.json` was missing the mailbox hooks + statusLine (we'd staged removing them on the settings.local.json bet). Restored manually first, then `install.sh` re-stamped the marker. claude-server's cleanup commit will need to be re-cut to leave those entries in place — see "deferred" below.

## Deferred to follow-up PRs

- **nvim / lazyvim layer** — colorscheme alignment, custom shortcuts, agent-review / agent-tour plugins. Once landed, `leader-template/agents/DEVELOPER.md` can have its code-tour section re-added (depends on `agent-tour.nvim`).
- **Cross-platform** — current install assumes macOS (iTerm prefs via `defaults write` / `PlistBuddy`; `say` for mx-speaker). Linux would need: terminal-specific keymap helpers, alternative speaker (`espeak`?), drop iTerm-gated steps.
- **Per-terminal setup** — `setup/iterm.sh` exists. Add `setup/alacritty.md` / `setup/kitty.md` / `setup/ghostty.md` as people ask.
- **Multi-palette** — `@tx-ide-palette` currently accepts only `tokyonight-night` (and `off`). Add other tokyonight variants + gruvbox/catppuccin.
- **Separate role files** — `agents/SCOPING.md` / `PLANNING.md` / `RESEARCH.md` are currently inlined as descriptions in `LEADER.md`. Materialize when behavior diverges enough to warrant.
- **claude-server cleanup commit** — the local `extract/tx-ide` branch removed the mailbox hooks + statusLine from `settings.json`. The actual end-state should leave them in `settings.json` (since `settings.local.json` doesn't compose at user scope) but pointing at the new tx-ide paths. Re-cut that branch before pushing.

## Open before public release

- **Rename** `tx-ide-private` → `tx-ide` (or your final public name).
- **Strip the `wiktordaniec/tx-ide-private` README link** in `leader-template/CLAUDE.md`.
- **README polish** — screenshots, animated demo of tx/mx popups, a one-paragraph "what is this for" pitch above the install section.
- **License + CONTRIBUTING** — MIT is in place; add a brief contributing note (issues welcome, no PRs without discussion, etc.) if you want.
- **Doctor's "version check"** — could detect if the repo's `tx-ide-doctor` declares an `EXPECT_HOOKS` set that doesn't match the installed marker's `hook_commands`, and tell the user to re-run install.sh. Currently catches drift in one direction (file vs repo) but not "I bumped the version and need to re-run install."
- **Telemetry / opt-out** — none currently. Probably stay none.
