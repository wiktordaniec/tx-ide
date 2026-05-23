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
# Selection highlight bg — tokyonight bg_highlight. Neutral enough to not
# shift chip colors layered on top.
BG_HIGHLIGHT_HEX='#292e42'
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
TAG_PALETTE_SIZE=12

# Colors emit as xterm-256 cube indices so tx, mx, and the pane border all
# land on the same pixel — mx's curses path can't do init_color() under tmux,
# so we meet it at the cube. TAG_HEX_<i> matches the cube index used by
# TAG_PALETTE_XTERM in lib/tx-mailbox.
TAG_HEX_0='colour210'   # red       (cube #ff8787)
TAG_HEX_1='colour167'   # red1      (cube #d75f5f)
TAG_HEX_2='colour215'   # orange    (cube #ffaf5f)
TAG_HEX_3='colour149'   # green     (cube #afd75f)
TAG_HEX_4='colour80'    # green1    (cube #5fd7d7)
TAG_HEX_5='colour73'    # green2    (cube #5fafaf)
TAG_HEX_6='colour37'    # teal      (cube #00afaf)
TAG_HEX_7='colour117'   # cyan      (cube #87d7ff)
TAG_HEX_8='colour38'    # blue1     (cube #00afd7)
TAG_HEX_9='colour141'   # magenta   (cube #af87ff)
TAG_HEX_10='colour198'  # magenta2  (cube #ff0087)
TAG_HEX_11='colour140'  # purple    (cube #af87d7)

TAG_ANSI_0=$'\e[38;5;210m'
TAG_ANSI_1=$'\e[38;5;167m'
TAG_ANSI_2=$'\e[38;5;215m'
TAG_ANSI_3=$'\e[38;5;149m'
TAG_ANSI_4=$'\e[38;5;80m'
TAG_ANSI_5=$'\e[38;5;73m'
TAG_ANSI_6=$'\e[38;5;37m'
TAG_ANSI_7=$'\e[38;5;117m'
TAG_ANSI_8=$'\e[38;5;38m'
TAG_ANSI_9=$'\e[38;5;141m'
TAG_ANSI_10=$'\e[38;5;198m'
TAG_ANSI_11=$'\e[38;5;140m'

# Must match tag_color_index() in lib/tx-mailbox.
tag_color_index() {
  local s="$1" h=0 i c
  for ((i = 0; i < ${#s}; i++)); do
    printf -v c '%d' "'${s:$i:1}"
    h=$(((h * 31 + c) % 2147483647))
  done
  printf '%d' $((h % TAG_PALETTE_SIZE))
}

tag_ansi() {
  local var="TAG_ANSI_$(tag_color_index "$1")"
  printf '%s' "${!var}"
}

tag_hex() {
  local var="TAG_HEX_$(tag_color_index "$1")"
  printf '%s' "${!var}"
}
