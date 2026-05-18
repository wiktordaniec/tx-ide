#!/usr/bin/env bash
# Configure iTerm2 GlobalKeyMap for tmux:
#   ⌘0..9    -> ESC + digit    (tmux reads as M-<digit>; selects pane)
#   ⌘⌥1..9   -> ESC + 'W' + N  (tmux user-key; selects window N)
# Also clears legacy ⌃N and ⌥N entries from earlier iterations of this setup.
#
# Must be run with iTerm2 quit, otherwise iTerm overwrites our edits on next quit.
# After running, relaunch iTerm and `tmux attach`.
set -euo pipefail

PLIST="$HOME/Library/Preferences/com.googlecode.iterm2.plist"
PB=/usr/libexec/PlistBuddy

if pgrep -x iTerm2 >/dev/null; then
  echo "iTerm2 is running. Quit it first (⌘Q from the iTerm menu),"
  echo "then re-run this script from Terminal.app."
  exit 1
fi

cp "$PLIST" "${PLIST}.bak.$(date +%s)"

# Disable iTerm's built-in modifier+digit switching. The dropdown tag in
# iTerm's Settings is *not* a bitmask — 0 = Option, 4 = Cmd, 9 = "None".
# We want fully disabled, so use 9.
defaults write com.googlecode.iterm2 SwitchTabModifier    -int 9
defaults write com.googlecode.iterm2 SwitchPaneModifier   -int 9
defaults write com.googlecode.iterm2 SwitchWindowModifier -int 9 2>/dev/null || true

$PB -c "Add :GlobalKeyMap dict" "$PLIST" 2>/dev/null || true

# US ANSI virtual keycode for a top-row digit
keycode_for() {
  case "$1" in
    0) echo 0x1d ;;
    1) echo 0x12 ;;
    2) echo 0x13 ;;
    3) echo 0x14 ;;
    4) echo 0x15 ;;
    5) echo 0x17 ;;
    6) echo 0x16 ;;
    7) echo 0x1a ;;
    8) echo 0x1c ;;
    9) echo 0x19 ;;
  esac
}
CMD=0x100000
OPT=0x80000
CTRL=0x40000  # legacy
CMDOPT=0x180000  # CMD | OPT

set_entry() {
  local key="$1" text="$2"
  $PB -c "Delete :GlobalKeyMap:${key}" "$PLIST" 2>/dev/null || true
  $PB -c "Add :GlobalKeyMap:${key} dict"               "$PLIST"
  $PB -c "Add :GlobalKeyMap:${key}:Action integer 10"  "$PLIST"
  $PB -c "Add :GlobalKeyMap:${key}:Text string ${text}" "$PLIST"
  $PB -c "Add :GlobalKeyMap:${key}:Version integer 1"  "$PLIST"
}

for d in 0 1 2 3 4 5 6 7 8 9; do
  ascii=$(printf '0x%x' $((48 + d)))
  kc=$(keycode_for "$d")
  # Strip legacy ⌃N and ⌥N entries from prior iterations of this setup.
  $PB -c "Delete :GlobalKeyMap:${ascii}-${CTRL}-${kc}" "$PLIST" 2>/dev/null || true
  $PB -c "Delete :GlobalKeyMap:${ascii}-${OPT}-${kc}"  "$PLIST" 2>/dev/null || true
  # ⌘N -> ESC + digit (tmux M-N; ⌘0 stays mapped but is unbound in tmux)
  set_entry "${ascii}-${CMD}-${kc}" "${d}"
done

# ⌘⌥N -> ESC + 'W' + digit (tmux user-key User{N-1}; selects window N).
# Skips ⌘⌥0 since tmux uses 1-based window indexing.
for d in 1 2 3 4 5 6 7 8 9; do
  ascii=$(printf '0x%x' $((48 + d)))
  kc=$(keycode_for "$d")
  set_entry "${ascii}-${CMDOPT}-${kc}" "W${d}"
done

# Strip legacy per-profile Keyboard Map ⌥N entries from any profile.
pidx=0
while $PB -c "Print :'New Bookmarks':${pidx}:Name" "$PLIST" >/dev/null 2>&1; do
  for d in 1 2 3 4 5 6 7 8 9; do
    ascii=$(printf '0x%x' $((48 + d)))
    $PB -c "Delete :'New Bookmarks':${pidx}:'Keyboard Map':${ascii}-${OPT}" \
      "$PLIST" 2>/dev/null || true
  done
  pidx=$((pidx+1))
done

killall cfprefsd 2>/dev/null || true

echo "Done. Backup at ${PLIST}.bak.*"
echo "Relaunch iTerm and run: tmux attach"
