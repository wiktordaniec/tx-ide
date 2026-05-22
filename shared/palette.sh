# Shared color palette — source from any bash script:
#   . "<repo>/shared/palette.sh"
#
# Canonical reference for tx-ide. Non-bash consumers (tmux/tx-ide.tmux,
# lib/tx-mailbox, claude/statusline.sh) duplicate the literals and carry a
# comment pointing back here.
#
# === Switching palettes ===
# Aligned with tokyonight-night. To switch (e.g. to tokyonight-moon):
#   1. Update the hex values below
#   2. Re-apply your terminal's color preset to match (iTerm: open the
#      matching .itermcolors from tokyonight's extras dir, then apply)
#   3. Update the hex literals in: tmux/tx-ide.tmux, lib/tx-mailbox,
#      claude/statusline.sh (grep for the old hex)

# === Semantic colors ===

# Accent — active pane border + title, fzf/curses selection, tx focus name.
# tokyonight-night blue. Selection backgrounds pair this with BG_HEX text.
ACCENT_HEX='#7aa2f7'
ACCENT_ANSI=$'\e[38;2;122;162;247m'

# Muted — inactive pane border + title.
# tokyonight-night dim blue-gray. Tags on inactive panes inherit this via
# pane-border-style (no script override). On active panes, tags override the
# inherited accent with FG_HEX so they read as neutral grayish-white.
BORDER_DIM_HEX='#3b4261'

# Background / foreground — terminal base colors. Used as the text color
# atop ACCENT backgrounds (selection rows, active window status chip), and
# as the explicit tag color when a pane is active (so tags stay neutral
# instead of inheriting the accent blue title color).
BG_HEX='#1a1b26'
# Deeper than BG, for high-contrast text on ACCENT backgrounds (selection rows
# in tx and mx). Matches tokyonight ANSI 0 / curses.COLOR_BLACK exactly, so the
# curses path and the fzf path land on the same color.
BG_DEEP_HEX='#15161e'
FG_HEX='#c0caf5'
FG_ANSI=$'\e[38;2;192;202;245m'

# Secondary text — softer than FG, for muted/secondary content like tag chips
# and informational columns (e.g. tx STARTED). tokyonight-night dim4. Identical
# to ANSI white (\e[37m, COLOR_WHITE) under the iTerm preset, so curses TUIs
# can hit this exact color via curses.COLOR_WHITE.
DIM_FG_HEX='#a9b1d6'
DIM_FG_ANSI=$'\e[38;2;169;177;214m'

# Staleness / time-sensitive signal — tx IDLE column, anything "this getting
# old?". tokyonight-night yellow. Identical to ANSI yellow (\e[33m) under the
# preset, so curses TUIs can hit this via curses.COLOR_YELLOW.
WARN_HEX='#e0af68'
WARN_ANSI=$'\e[38;2;224;175;104m'

# === iTerm tokyonight-night ANSI 8-color reference ===
# Useful when picking colors for curses-based tools that only see COLOR_*:
#   \e[30m black   → #15161e   \e[37m white   → #a9b1d6  (== DIM_FG_HEX)
#   \e[31m red     → #f7768e   \e[34m blue    → #7aa2f7  (== ACCENT_HEX)
#   \e[32m green   → #9ece6a   \e[35m magenta → #bb9af7
#   \e[33m yellow  → #e0af68   \e[36m cyan    → #7dcfff
#   (== WARN_HEX)

# === Tag chip palette ===
# Deterministic color-per-tag-value: a polynomial hash of the tag string picks
# one of four palette indices, so the same tag value (e.g. `llm`) always
# renders in the same color across tx, mx, and the tmux pane border.
#
# The four colors are tokyonight ANSI colors NOT already carrying a semantic
# role (avoiding ACCENT blue, WARN yellow, and FG/DIM_FG white). All four
# hexes match the iTerm tokyonight preset for COLOR_RED, COLOR_GREEN,
# COLOR_MAGENTA, COLOR_CYAN — so curses TUIs hit the same color via the
# COLOR_* constants.
TAG_HEX_0='#f7768e'   # red
TAG_HEX_1='#9ece6a'   # green
TAG_HEX_2='#bb9af7'   # magenta
TAG_HEX_3='#7dcfff'   # cyan

TAG_ANSI_0=$'\e[38;2;247;118;142m'
TAG_ANSI_1=$'\e[38;2;158;206;106m'
TAG_ANSI_2=$'\e[38;2;187;154;247m'
TAG_ANSI_3=$'\e[38;2;125;207;255m'

# Polynomial hash on the tag string → palette index 0..3. Must match the
# Python implementation in lib/tx-mailbox so the same tag stays the same color
# across tx and mx.
tag_color_index() {
  local s="$1" h=0 i c
  for ((i = 0; i < ${#s}; i++)); do
    printf -v c '%d' "'${s:$i:1}"
    h=$(((h * 31 + c) % 2147483647))
  done
  printf '%d' $((h % 4))
}

# 24-bit ANSI escape for the tag value's color. Use in fzf/--ansi contexts.
tag_ansi() {
  case "$(tag_color_index "$1")" in
  0) printf '%s' "$TAG_ANSI_0" ;;
  1) printf '%s' "$TAG_ANSI_1" ;;
  2) printf '%s' "$TAG_ANSI_2" ;;
  3) printf '%s' "$TAG_ANSI_3" ;;
  esac
}

# Bare hex for the tag value's color. Use in tmux #[fg=…] formats.
tag_hex() {
  case "$(tag_color_index "$1")" in
  0) printf '%s' "$TAG_HEX_0" ;;
  1) printf '%s' "$TAG_HEX_1" ;;
  2) printf '%s' "$TAG_HEX_2" ;;
  3) printf '%s' "$TAG_HEX_3" ;;
  esac
}
