#!/usr/bin/env bash
# Configure iTerm2 for tx-ide:
#   1. GlobalKeyMap for tmux:
#      ⌘0..9    -> ESC + digit    (tmux reads as M-<digit>; selects pane)
#      ⌘⌥1..9   -> ESC + 'W' + N  (tmux user-key; selects window N)
#      Also clears legacy ⌃N and ⌥N entries from earlier iterations.
#   2. Imports tokyonight-night.itermcolors as a Custom Color Preset.
#   3. Optionally applies it to the Default profile (--apply-colors).
#
# Must be run with iTerm2 quit, otherwise iTerm overwrites our edits on next quit.
# After running, relaunch iTerm and `tmux attach`.
#
# Usage:
#   setup/iterm.sh                  Keymap + import preset (no apply).
#   setup/iterm.sh --apply-colors   Keymap + import preset + apply to Default profile.
set -euo pipefail

APPLY_COLORS=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply-colors) APPLY_COLORS=1; shift ;;
    -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) printf 'iterm.sh: unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PRESET_FILE="$SCRIPT_DIR/tokyonight-night.itermcolors"
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

# === Color preset (always imported; optionally applied) ===
# Python is more reliable than PlistBuddy for dict-of-dicts merging.
if [[ -f "$PRESET_FILE" ]]; then
  PLIST="$PLIST" PRESET_FILE="$PRESET_FILE" APPLY="$APPLY_COLORS" python3 - <<'PY'
import os, plistlib

plist_path = os.environ["PLIST"]
preset_path = os.environ["PRESET_FILE"]
apply = os.environ["APPLY"] == "1"

with open(preset_path, "rb") as fh:
    preset = plistlib.load(fh)

with open(plist_path, "rb") as fh:
    plist = plistlib.load(fh)

# Always import the preset (additive, named).
plist.setdefault("Custom Color Presets", {})["tokyonight-night"] = preset

# Optionally overwrite the Default profile's colors with the preset's values.
# Default profile is New Bookmarks[0]. We touch only color keys.
if apply:
    bookmarks = plist.get("New Bookmarks") or []
    if bookmarks:
        for color_name, color_value in preset.items():
            bookmarks[0][color_name] = color_value

with open(plist_path, "wb") as fh:
    plistlib.dump(plist, fh)

print(f"  → tokyonight-night preset imported into Custom Color Presets")
if apply:
    print(f"  → Default profile colors applied (tokyonight-night)")
else:
    print(f"  → Skipped applying to Default profile (run with --apply-colors)")
PY
else
  echo "  ! preset file not found: $PRESET_FILE — skipping color setup"
fi

killall cfprefsd 2>/dev/null || true

echo "Done. Backup at ${PLIST}.bak.*"
echo "Relaunch iTerm and run: tmux attach"
