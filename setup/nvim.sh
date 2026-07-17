#!/usr/bin/env bash
# setup/nvim.sh — provision (or remove) tx-ide's default nvim config.
#
# tx-ide ships a complete LazyVim config under <repo>/nvim (tokyonight, diffview.nvim,
# gitsigns inline-diff keymaps, keylog telemetry — everything `tx spawn-nvim` relies on).
# This script points ~/.config/nvim at it via symlink. The repo stays the source of
# truth: `git pull` updates the config everywhere it's linked.
#
#   setup/nvim.sh install   symlink $XDG_CONFIG_HOME/nvim → <repo>/nvim
#                           (a pre-existing real config is moved to nvim.bak.<stamp>,
#                            never deleted; a foreign symlink is replaced, its target
#                            untouched)
#   setup/nvim.sh remove    remove the symlink IF it points at this repo; restore the
#                           most recent nvim.bak.<stamp> if one exists
#   setup/nvim.sh status    show what ~/.config/nvim currently is
#
# The per-machine toggle is simply whether this ran: `install` opts the machine in,
# `remove` opts it out. The installer asks; you can re-run either verb any time.
set -euo pipefail

REPO="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$REPO/nvim"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}"
DST="$CONFIG_DIR/nvim"
STAMP="$(date +%Y%m%d%H%M%S)"

G=$'\e[32m'; Y=$'\e[33m'; RED=$'\e[31m'; D=$'\e[2m'; X=$'\e[0m'
ok()   { printf '  %s→%s %-48s %s%s%s\n' "$G" "$X" "$1" "$G" "${2:-ok}" "$X"; }
warn() { printf '  %s→%s %-48s %s%s%s\n' "$Y" "$X" "$1" "$Y" "$2" "$X"; }
fail() { printf '  %s→%s %-48s %s%s%s\n' "$RED" "$X" "$1" "$RED" "$2" "$X"; }

status() {
  if [[ -L "$DST" ]]; then
    local target; target="$(readlink "$DST")"
    if [[ "$target" == "$SRC" ]]; then
      ok "$DST" "→ tx-ide ($SRC)"
    else
      warn "$DST" "symlink → $target (not tx-ide)"
    fi
  elif [[ -d "$DST" ]]; then
    warn "$DST" "real directory (not managed by tx-ide)"
  elif [[ -e "$DST" ]]; then
    warn "$DST" "exists, not a directory"
  else
    warn "$DST" "absent"
  fi
}

install_() {
  [[ -d "$SRC" ]] || { fail "$SRC" "missing from repo"; exit 1; }
  mkdir -p "$CONFIG_DIR"
  if [[ -L "$DST" ]]; then
    if [[ "$(readlink "$DST")" == "$SRC" ]]; then
      ok "$DST" "already linked"
      return 0
    fi
    # foreign symlink: replacing the link is safe — its target is untouched
    warn "$DST" "was a symlink → $(readlink "$DST"); replacing link only"
    rm "$DST"
  elif [[ -e "$DST" ]]; then
    mv "$DST" "$DST.bak.$STAMP"
    ok "backup" "$DST.bak.$STAMP"
  fi
  ln -s "$SRC" "$DST"
  ok "$DST" "→ $SRC"
  printf '  %sfirst nvim launch will bootstrap plugins (lazy.nvim)%s\n' "$D" "$X"
}

remove_() {
  if [[ -L "$DST" && "$(readlink "$DST")" == "$SRC" ]]; then
    rm "$DST"
    ok "$DST" "unlinked"
    local latest
    latest="$(ls -d "$DST".bak.* 2>/dev/null | sort | tail -1 || true)"
    if [[ -n "$latest" ]]; then
      mv "$latest" "$DST"
      ok "restored" "$latest → $DST"
    else
      printf '  %sno backup to restore — ~/.config/nvim is now absent%s\n' "$D" "$X"
    fi
  else
    warn "$DST" "not a tx-ide symlink — leaving untouched"
  fi
}

case "${1:-status}" in
install) install_ ;;
remove) remove_ ;;
status) status ;;
*)
  sed -n '2,20p' "$0"
  exit 2
  ;;
esac
