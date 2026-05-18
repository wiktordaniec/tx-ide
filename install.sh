#!/usr/bin/env bash
# tx-ide installer. Click-through, idempotent, safe to re-run.
# All file modifications back up to <file>.bak.<timestamp> before writing.
set -u

REPO="$(cd "$(dirname "$0")" && pwd)"
HOME_DIR="${HOME%/}"
LOCAL_BIN="$HOME_DIR/.local/bin"
CLAUDE_DIR="$HOME_DIR/.claude"
TX_IDE_DIR="$HOME_DIR/.tx-ide"
STAMP=$(date +%Y%m%d%H%M%S)

B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; RED=$'\e[31m'; D=$'\e[2m'; X=$'\e[0m'
ok()    { printf '  %s→%s %-48s %s%s%s\n' "$G" "$X" "$1" "$G" "${2:-ok}" "$X"; }
warn()  { printf '  %s→%s %-48s %s%s%s\n' "$Y" "$X" "$1" "$Y" "$2" "$X"; }
fail()  { printf '  %s→%s %-48s %s%s%s\n' "$RED" "$X" "$1" "$RED" "$2" "$X"; }
info()  { printf '  %s%s%s\n' "$D" "$*" "$X"; }
header(){ printf '\n%s%s%s\n' "$B" "$*" "$X"; }

confirm() {
  local prompt="$1" default="${2:-N}" reply
  if [[ "$default" == Y ]]; then
    read -r -p "$prompt [Y/n] " reply || reply=""
    [[ -z "$reply" || "$reply" =~ ^[Yy]$ ]]
  else
    read -r -p "$prompt [y/N] " reply || reply=""
    [[ "$reply" =~ ^[Yy]$ ]]
  fi
}

# Symlink src → dst; idempotent; refuses to clobber non-symlinks.
link() {
  local src="$1" dst="$2"
  if [[ -L "$dst" ]]; then
    local current
    current=$(readlink "$dst")
    if [[ "$current" == "$src" ]]; then
      ok "$dst" "already linked"
      return 0
    fi
    rm -f "$dst"
  elif [[ -e "$dst" ]]; then
    fail "$dst" "exists (not a symlink) — refusing to clobber"
    return 1
  fi
  ln -s "$src" "$dst"
  ok "$dst" "linked"
}

# Symlink for a directory target (uses -n to prevent symlink-into-symlink).
link_dir() {
  local src="$1" dst="$2"
  if [[ -L "$dst" ]]; then
    local current
    current=$(readlink "$dst")
    if [[ "$current" == "$src" ]]; then
      ok "$dst" "already linked"
      return 0
    fi
    rm "$dst"
  elif [[ -e "$dst" ]]; then
    fail "$dst" "exists (not a symlink) — refusing to clobber"
    return 1
  fi
  ln -sn "$src" "$dst"
  ok "$dst" "linked"
}

# === Pre-flight ===

cat <<EOF
${B}=== tx-ide install ===${X}

Repo:  $REPO

I will:
  - symlink CLIs into ~/.local/bin/
      tx, mx, tx-leader, tmux-pane-for-session, tmux-pane-session-name
  - symlink Claude mailbox hooks dir: ~/.claude/hooks/mailbox/ → repo
  - symlink statusline: ~/.claude/statusline.sh → repo
  - symlink agents dir: ~/.tx-ide/agents/ → repo (orchestration role files)
  - create ~/.tx-ide/user-agents/ (empty; drop overrides + .local.md here)
  - write ~/.tx-ide/tmux.conf (a one-line file I own)
  - add one source-file block to ~/.tmux.conf (with markers + backup)
  - add hooks + statusLine to ~/.claude/settings.json (with backup)
      (4 mailbox hook entries + statusLine, tracked via a _tx_ide_managed
       marker so uninstall removes only ours and doctor detects drift.
       Your existing hooks, permissions, plugins, theme are untouched.)
  - configure iTerm keyboard map (only if iTerm + not currently running)
  - start the mx-speaker background daemon

I will NOT touch:
  - any settings.json entry I didn't add (permissions, theme, plugins, other hooks)
  - ~/.claude/CLAUDE.md
  - any tmux config outside the marked source-file block
  - any iTerm preference if iTerm is currently running (you'll get a hint)
  - ~/.tx-ide/user-agents/ contents (your overrides survive re-runs)

After install:
  - run tx-leader from any project to start a leader Claude Code session
  - run tx-ide-doctor to verify

EOF

confirm "Proceed?" Y || { echo "Aborted."; exit 0; }

# === 1: CLIs ===
header "CLIs into ~/.local/bin"
mkdir -p "$LOCAL_BIN"
link "$REPO/bin/tx"                      "$LOCAL_BIN/tx"
link "$REPO/bin/mx"                      "$LOCAL_BIN/mx"
link "$REPO/bin/tx-leader"               "$LOCAL_BIN/tx-leader"
link "$REPO/bin/tmux-pane-for-session"   "$LOCAL_BIN/tmux-pane-for-session"
link "$REPO/bin/tmux-pane-session-name"  "$LOCAL_BIN/tmux-pane-session-name"

# === 2: Claude hooks dir ===
header "Claude mailbox hooks"
mkdir -p "$CLAUDE_DIR/hooks"
link_dir "$REPO/claude/hooks" "$CLAUDE_DIR/hooks/mailbox"

# === 3: Statusline ===
header "Statusline"
link "$REPO/claude/statusline.sh" "$CLAUDE_DIR/statusline.sh"

# === 4: Agents (shipped + user override dir) ===
header "Agents"
mkdir -p "$TX_IDE_DIR"
link_dir "$REPO/agents" "$TX_IDE_DIR/agents"
if [[ -d "$TX_IDE_DIR/user-agents" ]]; then
  ok "$TX_IDE_DIR/user-agents" "already present"
else
  mkdir -p "$TX_IDE_DIR/user-agents"
  ok "$TX_IDE_DIR/user-agents" "created (empty)"
fi

# === 5: ~/.tx-ide/tmux.conf shim ===
header "Tmux config shim"
mkdir -p "$TX_IDE_DIR"
TX_IDE_CONF="$TX_IDE_DIR/tmux.conf"
desired_content="# Written by tx-ide install.sh — do not edit, re-run install to update.
# Repo: $REPO
run-shell '$REPO/tmux/tx-ide.tmux'
"
if [[ -f "$TX_IDE_CONF" ]] && [[ "$(cat "$TX_IDE_CONF")" == "$desired_content" ]]; then
  ok "$TX_IDE_CONF" "already current"
else
  if [[ -f "$TX_IDE_CONF" ]]; then
    cp "$TX_IDE_CONF" "$TX_IDE_CONF.bak.$STAMP"
  fi
  printf '%s' "$desired_content" > "$TX_IDE_CONF"
  ok "$TX_IDE_CONF" "written"
fi

# === 6: ~/.tmux.conf source-file block ===
header "~/.tmux.conf source-file"
USER_TMUX_CONF="$HOME_DIR/.tmux.conf"
BEGIN_MARKER="# === BEGIN tx-ide ==="
END_MARKER="# === END tx-ide ==="

if [[ -f "$USER_TMUX_CONF" ]] && grep -qF "$BEGIN_MARKER" "$USER_TMUX_CONF"; then
  ok "$USER_TMUX_CONF" "block present"
else
  if [[ -f "$USER_TMUX_CONF" ]]; then
    cp "$USER_TMUX_CONF" "$USER_TMUX_CONF.bak.$STAMP"
    note="backup: $USER_TMUX_CONF.bak.$STAMP"
  else
    : > "$USER_TMUX_CONF"
    note="file created"
  fi
  cat >> "$USER_TMUX_CONF" <<EOF

$BEGIN_MARKER
source-file $TX_IDE_CONF
$END_MARKER
EOF
  ok "$USER_TMUX_CONF" "block added"
  info "$note"
fi

# === 7: ~/.claude/settings.json merge ===
# We write hooks + statusLine directly into ~/.claude/settings.json (not
# settings.local.json — Claude Code doesn't read settings.local.json at user
# scope, only at project scope). A top-level `_tx_ide_managed` key tracks
# exactly what we added so uninstall removes only those entries and doctor
# detects drift. Claude Code ignores unknown top-level keys.
header "Claude settings.json"
SETTINGS_MAIN="$CLAUDE_DIR/settings.json"
SETTINGS_MAIN="$SETTINGS_MAIN" STAMP="$STAMP" python3 - <<'PY'
import json, os, sys

main_path = os.environ["SETTINGS_MAIN"]
stamp = os.environ["STAMP"]

POST = "$HOME/.claude/hooks/mailbox/post.sh"
PRE  = "$HOME/.claude/hooks/mailbox/pre.sh"
END  = "$HOME/.claude/hooks/mailbox/end.sh"
STATUS = "bash $HOME/.claude/statusline.sh"

# Single source of truth for what tx-ide manages.
managed_hook_commands = {
    "Stop":              POST,
    "PermissionRequest": POST,
    "UserPromptSubmit":  PRE,
    "SessionEnd":        END,
}

def make_entry(cmd):
    return {"type": "command", "command": cmd, "timeout": 10, "async": True}

# Load (or initialize) settings.json.
if os.path.exists(main_path):
    real_path = os.path.realpath(main_path)
    with open(real_path) as fh:
        data = json.load(fh)
    backup_path = f"{real_path}.bak.{stamp}"
    with open(backup_path, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
else:
    real_path = main_path
    data = {}
    backup_path = None

# Append a hook entry for each event if our command isn't already wired.
def event_has_command(hooks_for_event, command):
    return any(
        h.get("command") == command
        for block in hooks_for_event
        for h in block.get("hooks", [])
    )

hooks = data.setdefault("hooks", {})
appended = []
already_present = []
for event, command in managed_hook_commands.items():
    bucket = hooks.setdefault(event, [])
    if event_has_command(bucket, command):
        already_present.append(event)
    else:
        bucket.append({"matcher": "", "hooks": [make_entry(command)]})
        appended.append(event)

# statusLine: add ours if absent. If present and matches ours, leave it
# (idempotent). If present and different, leave the user's untouched and
# print a warning — they have their own; we don't shadow.
status_msg = ""
status_existing = data.get("statusLine", {}).get("command")
if status_existing is None:
    data["statusLine"] = {"type": "command", "command": STATUS}
    status_msg = "added"
elif status_existing == STATUS:
    status_msg = "already current"
else:
    status_msg = f"skipped — user has their own ({status_existing!r}). Move it aside if you want tx-ide's."

# Write the marker. Always overwrite — declares current intent.
data["_tx_ide_managed"] = {
    "version": 1,
    "hook_commands": managed_hook_commands,
    "statusLine_command": STATUS if data.get("statusLine", {}).get("command") == STATUS else None,
}

with open(real_path, "w") as fh:
    json.dump(data, fh, indent=2)
    fh.write("\n")

state = "merged" if appended or status_msg == "added" else "already current"
print(f"  \033[32m→\033[0m {main_path}    \033[32m{state}\033[0m")
if backup_path:
    print(f"  \033[2mbackup: {backup_path}\033[0m")
if appended:
    print(f"  \033[2mhooks added: {', '.join(appended)}\033[0m")
if already_present:
    print(f"  \033[2mhooks already present: {', '.join(already_present)}\033[0m")
print(f"  \033[2mstatusLine: {status_msg}\033[0m")
print(f"  \033[2m_tx_ide_managed marker written (uninstall + doctor use this)\033[0m")
PY

# === 8: iTerm ===
header "iTerm keyboard map"
if [[ "${TERM_PROGRAM:-}" == "iTerm.app" ]]; then
  if pgrep -x iTerm2 >/dev/null 2>&1; then
    warn "GlobalKeyMap" "iTerm is running — quit it, then run $REPO/setup/iterm.sh"
  else
    if "$REPO/setup/iterm.sh" >/dev/null 2>&1; then
      ok "GlobalKeyMap" "configured"
    else
      fail "GlobalKeyMap" "setup script failed; see $REPO/setup/iterm.sh output"
    fi
  fi
else
  warn "GlobalKeyMap" "TERM_PROGRAM='${TERM_PROGRAM:-unknown}' — skipping (iTerm only)"
fi

# === 9: mx-speaker daemon ===
header "mx-speaker daemon"
"$REPO/claude/hooks/start-speaker.sh" 2>&1 | sed 's/^/  → /'

# === Final ===
cat <<EOF

${B}=== Done ===${X}

${B}Reload tmux to pick up the new bindings:${X}
  ${D}tmux source-file ~/.tmux.conf${X}

${B}Start a leader Claude Code session in any project:${X}
  ${D}cd ~/Code/your-project && tx-leader${X}

${B}Customize roles (optional):${X}
  ${D}drop overrides in ~/.tx-ide/user-agents/ (e.g. LEADER.local.md)${X}

${B}Verify everything:${X}
  ${D}tx-ide-doctor${X}

${B}Undo:${X}
  ${D}$REPO/uninstall.sh${X}
EOF
