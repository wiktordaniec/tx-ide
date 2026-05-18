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
      tx, mx, tmux-pane-for-session, tmux-pane-session-name
  - symlink Claude mailbox hooks dir: ~/.claude/hooks/mailbox/ → repo
  - symlink statusline: ~/.claude/statusline.sh → repo
  - write ~/.tx-ide/tmux.conf (a one-line file I own)
  - add one source-file block to ~/.tmux.conf (with markers + backup)
  - merge hooks + statusLine into ~/.claude/settings.local.json
      (Claude Code concatenates this with your settings.json — main file untouched)
  - configure iTerm keyboard map (only if iTerm + not currently running)
  - start the mx-speaker background daemon

I will NOT touch:
  - ~/.claude/settings.json
  - ~/.claude/CLAUDE.md
  - any tmux config outside the marked source-file block
  - any iTerm preference if iTerm is currently running (you'll get a hint)

Optional after install:
  - scaffold a leader/orchestration directory via init-leader.sh
  - run tx-ide-doctor to verify

EOF

confirm "Proceed?" Y || { echo "Aborted."; exit 0; }

# === 1: CLIs ===
header "CLIs into ~/.local/bin"
mkdir -p "$LOCAL_BIN"
link "$REPO/bin/tx"                      "$LOCAL_BIN/tx"
link "$REPO/bin/mx"                      "$LOCAL_BIN/mx"
link "$REPO/bin/tmux-pane-for-session"   "$LOCAL_BIN/tmux-pane-for-session"
link "$REPO/bin/tmux-pane-session-name"  "$LOCAL_BIN/tmux-pane-session-name"

# === 2: Claude hooks dir ===
header "Claude mailbox hooks"
mkdir -p "$CLAUDE_DIR/hooks"
link_dir "$REPO/claude/hooks" "$CLAUDE_DIR/hooks/mailbox"

# === 3: Statusline ===
header "Statusline"
link "$REPO/claude/statusline.sh" "$CLAUDE_DIR/statusline.sh"

# === 4: ~/.tx-ide/tmux.conf shim ===
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

# === 5: ~/.tmux.conf source-file block ===
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

# === 6: ~/.claude/settings.local.json merge ===
header "Claude settings.local.json"
SETTINGS_LOCAL="$CLAUDE_DIR/settings.local.json"
SETTINGS_MAIN="$CLAUDE_DIR/settings.json"
SETTINGS_LOCAL="$SETTINGS_LOCAL" \
SETTINGS_MAIN="$SETTINGS_MAIN" \
STAMP="$STAMP" \
python3 - <<'PY'
import json, os

local_path = os.environ["SETTINGS_LOCAL"]
main_path = os.environ["SETTINGS_MAIN"]
stamp = os.environ["STAMP"]

# Hooks we want to register. Each lands in its own matcher="" block so
# concatenation with the user's existing matcher blocks in settings.json
# remains additive (Claude Code merges hook arrays across scopes).
def make_entry(cmd):
    return {"type": "command", "command": cmd, "timeout": 10, "async": True}

POST = "$HOME/.claude/hooks/mailbox/post.sh"
PRE  = "$HOME/.claude/hooks/mailbox/pre.sh"
END  = "$HOME/.claude/hooks/mailbox/end.sh"

want = {
    "Stop":              [make_entry(POST)],
    "PermissionRequest": [make_entry(POST)],
    "UserPromptSubmit":  [make_entry(PRE)],
    "SessionEnd":        [make_entry(END)],
}

if os.path.exists(local_path):
    with open(local_path) as fh:
        local = json.load(fh)
    backup_path = f"{local_path}.bak.{stamp}"
    with open(backup_path, "w") as fh:
        json.dump(local, fh, indent=2)
        fh.write("\n")
else:
    local = {}
    backup_path = None

# Read main settings.json (if present) to avoid duplicating commands already
# wired there — hooks concatenate across scopes, so writing the same command
# in both files would fire it twice per event.
main_hooks = {}
if os.path.exists(main_path):
    try:
        with open(main_path) as fh:
            main_data = json.load(fh)
        main_hooks = main_data.get("hooks", {})
    except Exception:
        main_hooks = {}

def existing_commands(hooks_dict, event):
    return {
        h.get("command")
        for block in hooks_dict.get(event, [])
        for h in block.get("hooks", [])
    }

hooks = local.setdefault("hooks", {})
appended = []
skipped_dup = []
for event, entries in want.items():
    already = existing_commands(hooks, event) | existing_commands(main_hooks, event)
    new_entries = [e for e in entries if e["command"] not in already]
    if not new_entries:
        skipped_dup.append(event)
        continue
    bucket = hooks.setdefault(event, [])
    bucket.append({"matcher": "", "hooks": new_entries})
    appended.append(event)

# statusLine: write only if absent in local AND main (override semantics —
# we never silently shadow the user's statusLine in settings.json).
status_msg = ""
if "statusLine" in local:
    status_msg = "already set in settings.local.json"
else:
    main_status = False
    if os.path.exists(main_path):
        try:
            with open(main_path) as fh:
                main = json.load(fh)
            main_status = "statusLine" in main
        except Exception:
            main_status = False
    if main_status:
        status_msg = "skipped (your settings.json already has one)"
    else:
        local["statusLine"] = {
            "type": "command",
            "command": "bash $HOME/.claude/statusline.sh",
        }
        status_msg = "added"

with open(local_path, "w") as fh:
    json.dump(local, fh, indent=2)
    fh.write("\n")

if appended or status_msg == "added":
    state = "merged"
else:
    state = "already current"

print(f"  \033[32m→\033[0m {local_path}    \033[32m{state}\033[0m")
if backup_path:
    print(f"  \033[2mbackup: {backup_path}\033[0m")
if appended:
    print(f"  \033[2mhooks added: {', '.join(appended)}\033[0m")
if skipped_dup:
    print(f"  \033[2mhooks skipped (already in settings.json): {', '.join(skipped_dup)}\033[0m")
print(f"  \033[2mstatusLine: {status_msg}\033[0m")
PY

# === 7: iTerm ===
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

# === 8: mx-speaker daemon ===
header "mx-speaker daemon"
"$REPO/claude/hooks/start-speaker.sh" 2>&1 | sed 's/^/  → /'

# === 9: leader scaffold (optional) ===
header "Leader directory (optional)"
if confirm "Scaffold a leader/orchestration directory now?" N; then
  printf '  Path: '
  read -r leader_path
  if [[ -n "$leader_path" ]]; then
    "$REPO/init-leader.sh" "$leader_path"
  else
    info "(empty path — skipped)"
  fi
else
  info "Skipped. Run later: $REPO/init-leader.sh <path>"
fi

# === Final ===
cat <<EOF

${B}=== Done ===${X}

${B}Reload tmux to pick up the new bindings:${X}
  ${D}tmux source-file ~/.tmux.conf${X}

${B}Verify everything:${X}
  ${D}tx-ide-doctor${X}

${B}Undo:${X}
  ${D}$REPO/uninstall.sh${X}
EOF
