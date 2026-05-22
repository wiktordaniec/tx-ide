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
# one of TAG_PALETTE_SIZE palette indices, so the same tag value (e.g. `llm`)
# always renders in the same color across tx, mx, and the tmux pane border.
#
# The palette uses 12 tokyonight named colors NOT already carrying a semantic
# role (avoiding ACCENT blue, WARN yellow, and FG/DIM_FG white). Bash and tmux
# emit the 24-bit hex directly; the curses TUI in lib/tx-mailbox calls
# `init_color()` against these RGB values so all three surfaces hit the same
# pixel color. Keep TAG_PALETTE_SIZE in sync with the Python constant of the
# same name in lib/tx-mailbox.
TAG_PALETTE_SIZE=12

TAG_HEX_0='#f7768e'   # red
TAG_HEX_1='#db4b4b'   # red1
TAG_HEX_2='#ff9e64'   # orange
TAG_HEX_3='#9ece6a'   # green
TAG_HEX_4='#73daca'   # green1
TAG_HEX_5='#41a6b5'   # green2
TAG_HEX_6='#1abc9c'   # teal
TAG_HEX_7='#7dcfff'   # cyan
TAG_HEX_8='#2ac3de'   # blue1
TAG_HEX_9='#bb9af7'   # magenta
TAG_HEX_10='#ff007c'  # magenta2
TAG_HEX_11='#9d7cd8'  # purple

TAG_ANSI_0=$'\e[38;2;247;118;142m'   # red
TAG_ANSI_1=$'\e[38;2;219;75;75m'     # red1
TAG_ANSI_2=$'\e[38;2;255;158;100m'   # orange
TAG_ANSI_3=$'\e[38;2;158;206;106m'   # green
TAG_ANSI_4=$'\e[38;2;115;218;202m'   # green1
TAG_ANSI_5=$'\e[38;2;65;166;181m'    # green2
TAG_ANSI_6=$'\e[38;2;26;188;156m'    # teal
TAG_ANSI_7=$'\e[38;2;125;207;255m'   # cyan
TAG_ANSI_8=$'\e[38;2;42;195;222m'    # blue1
TAG_ANSI_9=$'\e[38;2;187;154;247m'   # magenta
TAG_ANSI_10=$'\e[38;2;255;0;124m'    # magenta2
TAG_ANSI_11=$'\e[38;2;157;124;216m'  # purple

# Polynomial hash on the tag string → palette index. Must match the Python
# implementation in lib/tx-mailbox so the same tag stays the same color
# across tx and mx.
tag_color_index() {
  local s="$1" h=0 i c
  for ((i = 0; i < ${#s}; i++)); do
    printf -v c '%d' "'${s:$i:1}"
    h=$(((h * 31 + c) % 2147483647))
  done
  printf '%d' $((h % TAG_PALETTE_SIZE))
}

# Lookup helpers — indirect-expand TAG_ANSI_$idx / TAG_HEX_$idx by index so
# they don't have to be touched when TAG_PALETTE_SIZE changes.
tag_ansi() {
  local var="TAG_ANSI_$(tag_color_index "$1")"
  printf '%s' "${!var}"
}

tag_hex() {
  local var="TAG_HEX_$(tag_color_index "$1")"
  printf '%s' "${!var}"
}
