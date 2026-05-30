"""Color palette — the Python mirror of `shared/palette.sh` (stage S1b).

`shared/palette.sh` is the canonical reference; non-bash consumers duplicate the literals and
carry a comment pointing back. This module is the fzf picker's consumer: the same tokyonight-night
semantic colors + the per-tag chip hash, so the Python picker renders identically to the old bash
`cmd_pick`. **When the palette in `shared/palette.sh` changes, change these literals to match.**

Two groups, exactly as in the shell file:
  - Semantic colors (accent / fg / dim / warn / selection) as hex (for fzf `--color`) and as raw
    ANSI escapes (for the rendered rows + headers).
  - The tag-chip palette: a fixed 256-color cube indexed by a stable hash of the tag text, so a tag
    always lands on the same color across the picker, pane borders, and `ls`.
"""

from __future__ import annotations

# ----- raw SGR escapes (mirrors the bash B / R / RFG) --------------------------------------------
BOLD = "\x1b[1m"        # B  — bold on
RESET = "\x1b[0m"       # R  — all attributes off
RESET_FG = "\x1b[39m"   # RFG — default foreground only (keeps bold/bg, used after a colored chip)

# ----- semantic colors (tokyonight-night; see shared/palette.sh) ---------------------------------
# Accent — active pane border + title, fzf selection, the picker's focus-header name.
ACCENT_HEX = "#7aa2f7"
ACCENT_ANSI = "\x1b[38;2;122;162;247m"

# Foreground — terminal base; the focus-header tag chips + the fzf query text.
FG_HEX = "#c0caf5"
FG_ANSI = "\x1b[38;2;192;202;245m"

# Secondary text — softer than FG; the column header, prompt, and STARTED column.
DIM_FG_HEX = "#a9b1d6"
DIM_FG_ANSI = "\x1b[38;2;169;177;214m"

# Staleness signal — the IDLE column + the Ctrl-D kill prompt. tokyonight-night yellow.
WARN_HEX = "#e0af68"
WARN_ANSI = "\x1b[38;2;224;175;104m"

# Deeper-than-bg black, for high-contrast text on accent backgrounds.
BG_DEEP_HEX = "#15161e"

# Selection row background — a 256-color index (fzf `bg+`), matched to the curses TUIs.
SELECTION_BG = "236"

# ----- tag chip palette --------------------------------------------------------------------------
# Fixed 256-color cube; a tag's color is TAG_CUBE[hash(tag) % len]. The hash and cube are a 1:1 port
# of shared/palette.sh so a tag colors identically here, on pane borders, and in `ls`.
TAG_CUBE = (210, 167, 215, 149, 80, 73, 37, 117, 38, 141, 198, 140)


def tag_color_index(tag: str) -> int:
    """Stable index into `TAG_CUBE` for `tag` — the bash `tag_color_index` (a polynomial rolling
    hash over the bytes, mod the 31-bit prime, mod the palette size)."""
    accumulator = 0
    for char in tag:
        accumulator = (accumulator * 31 + ord(char)) % 2147483647
    return accumulator % len(TAG_CUBE)


def tag_cube(tag: str) -> int:
    """The 256-color cube value for `tag`."""
    return TAG_CUBE[tag_color_index(tag)]


def tag_ansi(tag: str) -> str:
    """The SGR foreground escape that colors `tag`'s chip (`\\e[38;5;Nm`)."""
    return f"\x1b[38;5;{tag_cube(tag)}m"


def tag_hex(tag: str) -> str:
    """The tmux `colourN` name for `tag` (for pane-border styling parity)."""
    return f"colour{tag_cube(tag)}"
