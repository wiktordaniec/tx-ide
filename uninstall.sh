#!/usr/bin/env bash
# Reverse everything install.sh did. Leaves user data alone:
#   ~/.claude/mailbox/   (inbox.jsonl, running.d/, mx-speaker.log)
#   any leader directory you scaffolded with init-leader.sh
#   the tx-ide-private repo itself
set -u

REPO="$(cd "$(dirname "$0")" && pwd)"
HOME_DIR="${HOME%/}"
LOCAL_BIN="$HOME_DIR/.local/bin"
CLAUDE_DIR="$HOME_DIR/.claude"
TX_IDE_DIR="$HOME_DIR/.tx-ide"

B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; D=$'\e[2m'; X=$'\e[0m'
ok()    { printf '  %s→%s %-48s %s%s%s\n' "$G" "$X" "$1" "$G" "${2:-removed}" "$X"; }
skip()  { printf '  %s→%s %-48s %s%s%s\n' "$Y" "$X" "$1" "$Y" "$2" "$X"; }

cat <<EOF
${B}=== tx-ide uninstall ===${X}

I will remove:
  - the four CLI symlinks in ~/.local/bin/ (only if they still point at this repo)
  - ~/.claude/hooks/mailbox/ (symlink)
  - ~/.claude/statusline.sh (symlink)
  - the tx-ide block from ~/.tmux.conf (matched by markers)
  - ~/.tx-ide/tmux.conf and the empty ~/.tx-ide/ directory
  - mailbox + statusLine entries from ~/.claude/settings.local.json
  - the mx-speaker daemon (if running) + its PID file

I will NOT touch:
  - ~/.claude/mailbox/ (your inbox + running state)
  - ~/.claude/settings.json
  - ~/.claude/CLAUDE.md
  - any leader directories scaffolded by init-leader.sh
  - the iTerm GlobalKeyMap (run setup/iterm.sh with iTerm quit to clean up)

EOF
read -r -p "Proceed? [y/N] " reply
[[ "$reply" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }

# === Symlinks ===
printf '\n%sCLI symlinks%s\n' "$B" "$X"
for tool in tx mx tmux-pane-for-session tmux-pane-session-name; do
  target="$LOCAL_BIN/$tool"
  if [[ -L "$target" ]] && [[ "$(readlink "$target")" == "$REPO/bin/$tool" ]]; then
    rm "$target"
    ok "$target"
  elif [[ -L "$target" ]]; then
    skip "$target" "points elsewhere, left alone"
  else
    skip "$target" "not present"
  fi
done

printf '\n%sClaude links%s\n' "$B" "$X"
mailbox_link="$CLAUDE_DIR/hooks/mailbox"
if [[ -L "$mailbox_link" ]] && [[ "$(readlink "$mailbox_link")" == "$REPO/claude/hooks" ]]; then
  rm "$mailbox_link"
  ok "$mailbox_link"
elif [[ -L "$mailbox_link" ]]; then
  skip "$mailbox_link" "points elsewhere, left alone"
else
  skip "$mailbox_link" "not present"
fi

statusline_link="$CLAUDE_DIR/statusline.sh"
if [[ -L "$statusline_link" ]] && [[ "$(readlink "$statusline_link")" == "$REPO/claude/statusline.sh" ]]; then
  rm "$statusline_link"
  ok "$statusline_link"
elif [[ -L "$statusline_link" ]]; then
  skip "$statusline_link" "points elsewhere, left alone"
else
  skip "$statusline_link" "not present"
fi

# === ~/.tmux.conf block ===
printf '\n%s~/.tmux.conf block%s\n' "$B" "$X"
USER_TMUX_CONF="$HOME_DIR/.tmux.conf"
BEGIN_MARKER="# === BEGIN tx-ide ==="
END_MARKER="# === END tx-ide ==="
if [[ -f "$USER_TMUX_CONF" ]] && grep -qF "$BEGIN_MARKER" "$USER_TMUX_CONF"; then
  STAMP=$(date +%Y%m%d%H%M%S)
  cp "$USER_TMUX_CONF" "$USER_TMUX_CONF.bak.$STAMP"
  awk -v b="$BEGIN_MARKER" -v e="$END_MARKER" '
    $0==b { skip=1; next }
    $0==e { skip=0; next }
    !skip
  ' "$USER_TMUX_CONF" > "$USER_TMUX_CONF.tmp"
  # Strip trailing blank lines we may have introduced
  awk 'NR==FNR{n=NR} NR<=n{print}' "$USER_TMUX_CONF.tmp" "$USER_TMUX_CONF.tmp" \
    | sed -e :a -e '/^$/{$d;N;ba' -e '}' > "$USER_TMUX_CONF"
  rm "$USER_TMUX_CONF.tmp"
  ok "$USER_TMUX_CONF" "block removed (backup: $USER_TMUX_CONF.bak.$STAMP)"
else
  skip "$USER_TMUX_CONF" "no tx-ide block found"
fi

# === ~/.tx-ide/ ===
printf '\n%s~/.tx-ide/%s\n' "$B" "$X"
if [[ -f "$TX_IDE_DIR/tmux.conf" ]]; then
  rm "$TX_IDE_DIR/tmux.conf"
  ok "$TX_IDE_DIR/tmux.conf"
fi
if [[ -d "$TX_IDE_DIR" ]]; then
  rmdir "$TX_IDE_DIR" 2>/dev/null && ok "$TX_IDE_DIR" "removed (empty)" || skip "$TX_IDE_DIR" "not empty, left alone"
fi

# === settings.local.json ===
printf '\n%sClaude settings.local.json%s\n' "$B" "$X"
SETTINGS_LOCAL="$CLAUDE_DIR/settings.local.json"
if [[ -f "$SETTINGS_LOCAL" ]]; then
  STAMP=$(date +%Y%m%d%H%M%S)
  cp "$SETTINGS_LOCAL" "$SETTINGS_LOCAL.bak.$STAMP"
  SETTINGS_LOCAL="$SETTINGS_LOCAL" python3 - <<'PY'
import json, os

path = os.environ["SETTINGS_LOCAL"]
with open(path) as fh:
    data = json.load(fh)

POST = "$HOME/.claude/hooks/mailbox/post.sh"
PRE  = "$HOME/.claude/hooks/mailbox/pre.sh"
END  = "$HOME/.claude/hooks/mailbox/end.sh"
ours = {POST, PRE, END}

removed_hooks = []
hooks = data.get("hooks", {})
for event, blocks in list(hooks.items()):
    new_blocks = []
    for block in blocks:
        kept = [h for h in block.get("hooks", []) if h.get("command") not in ours]
        if kept:
            block["hooks"] = kept
            new_blocks.append(block)
        else:
            removed_hooks.append(event)
    if new_blocks:
        hooks[event] = new_blocks
    else:
        del hooks[event]
if not hooks:
    data.pop("hooks", None)

removed_status = False
if data.get("statusLine", {}).get("command") == "bash $HOME/.claude/statusline.sh":
    del data["statusLine"]
    removed_status = True

if data:
    with open(path, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    print(f"  \033[32m→\033[0m {path} cleaned")
else:
    os.remove(path)
    print(f"  \033[32m→\033[0m {path} removed (empty)")
print(f"  \033[2mremoved hooks: {','.join(removed_hooks) if removed_hooks else 'none'}\033[0m")
print(f"  \033[2mremoved statusLine: {'yes' if removed_status else 'no'}\033[0m")
PY
  printf '  %sbackup: %s.bak.%s%s\n' "$D" "$SETTINGS_LOCAL" "$STAMP" "$X"
else
  skip "$SETTINGS_LOCAL" "not present"
fi

# === mx-speaker daemon ===
printf '\n%smx-speaker daemon%s\n' "$B" "$X"
PID_FILE="$HOME_DIR/.claude/mailbox/mx-speaker.pid"
if [[ -f "$PID_FILE" ]]; then
  pid=$(cat "$PID_FILE")
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" && ok "mx-speaker" "killed (pid $pid)"
  else
    skip "mx-speaker" "stale PID file (process not running)"
  fi
  rm -f "$PID_FILE"
else
  skip "mx-speaker" "no PID file"
fi

cat <<EOF

${B}=== Done ===${X}

${B}Remaining user data (left in place):${X}
  ${D}~/.claude/mailbox/${X}    inbox, running state, speaker log
  ${D}leader dirs${X}          any directory you scaffolded with init-leader.sh

${B}Tmux:${X} reload to drop the bindings:
  ${D}tmux source-file ~/.tmux.conf${X}
EOF
