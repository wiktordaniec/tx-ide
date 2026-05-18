#!/usr/bin/env bash
# tx-ide tmux fragment. Invoked via `run-shell` from ~/.tx-ide/tmux.conf,
# which is sourced from the user's ~/.tmux.conf by install.sh.
#
# Reads @tx-ide-* options to decide which view features to enable:
#   @tx-ide-popups          on|off   prefix+t (tx), prefix+m (mx)
#   @tx-ide-pane-borders    on|off   pane-border-format integration + colors
#   @tx-ide-claude-scroll   on|off   C-u/C-d → PageUp/PageDown in Claude panes
#   @tx-ide-pane-keys       on|off   M-1..9 → select-pane
#   @tx-ide-window-keys     on|off   User0..8 → select-window (terminal must send)
#   @tx-ide-palette         tokyonight-night|off   color overrides
#
# All settings tmux sees are emitted via `tmux <cmd>`, so user's later
# `bind` / `set` lines in tmux.conf override ours (last-write-wins).
set -u

option() {
  tmux show-option -gv "$1" 2>/dev/null || printf '%s' "$2"
}

popups=$(option @tx-ide-popups on)
pane_borders=$(option @tx-ide-pane-borders on)
claude_scroll=$(option @tx-ide-claude-scroll on)
pane_keys=$(option @tx-ide-pane-keys on)
window_keys=$(option @tx-ide-window-keys on)
palette=$(option @tx-ide-palette tokyonight-night)

# --- Popups (prefix+t / prefix+m) ---
# prefix+t: set TX_ORIGIN_PANE so tx can respawn the invoking pane after
# attach; then display-popup with the tx CLI. prefix+m: same shape for mx.
# prefix+M re-homes the default `select-pane -m` that prefix+m used to do.
if [ "$popups" = on ]; then
  tmux bind-key t setenv -gF TX_ORIGIN_PANE '#{pane_id}' \; \
    display-popup -E -w 100 -h 30 -x C -y 1 -T ' tx ' 'tx'
  tmux bind-key m display-popup -E -w 100 -h 30 -x C -y 1 -T ' mx ' 'mx'
  tmux bind-key M select-pane -m
fi

# --- Pane borders ---
# Border style + format integrating tmux-pane-session-name. Hex literals
# duplicate shared/palette.sh:
#   ACCENT_HEX       #7aa2f7   (active border)
#   BORDER_DIM_HEX   #3b4261   (inactive border)
if [ "$pane_borders" = on ]; then
  if [ "$palette" = tokyonight-night ]; then
    tmux set-option -g  pane-border-style        'fg=#3b4261'
    tmux set-option -g  pane-active-border-style 'fg=#7aa2f7,bold'
  fi
  tmux set-option -g  pane-border-lines       heavy
  tmux set-option -g  pane-border-indicators  both
  tmux set-option -g  pane-border-status      off
  tmux set-option -g  pane-border-format \
    ' [#P] #(tmux-pane-session-name #D) '
fi

# --- Claude scroll intercept ---
# Inside a Claude Code pane, C-u/C-d scroll the TUI buffer (PageUp/PageDown).
# Outside Claude, they pass through (default copy-mode-vi behavior). Match
# either the literal "claude" command or a version-string-shaped command
# (Claude Code shows its version while loading: "2.1.138").
if [ "$claude_scroll" = on ]; then
  tmux bind-key -n C-u if-shell -F \
    '#{||:#{==:#{pane_current_command},claude},#{m:[0-9]*.[0-9]*.[0-9]*,#{pane_current_command}}}' \
    'send-keys PageUp' 'send-keys C-u'
  tmux bind-key -n C-d if-shell -F \
    '#{||:#{==:#{pane_current_command},claude},#{m:[0-9]*.[0-9]*.[0-9]*,#{pane_current_command}}}' \
    'send-keys PageDown' 'send-keys C-d'
fi

# --- Pane keys (M-1..9 → select-pane) ---
# Terminal must produce M-N. Configure your terminal (see setup/iterm.sh).
# Default tmux: prefix M-1..7 trigger layout cycling — unbind so a stray
# prefix+digit doesn't reflow your panes.
if [ "$pane_keys" = on ]; then
  for n in 1 2 3 4 5 6 7 8 9; do
    tmux bind-key -n "M-$n" select-pane -t "$n"
  done
  for n in 1 2 3 4 5 6 7; do
    tmux unbind-key -T prefix "M-$n" 2>/dev/null || true
  done
fi

# --- Window keys (User0..8 → select-window) ---
# Define user-key escape sequences ESC+'W'+digit, then bind. Terminal must
# emit ESC+'W'+digit on Cmd+Opt+N (or equivalent).
if [ "$window_keys" = on ]; then
  i=0
  for n in 1 2 3 4 5 6 7 8 9; do
    tmux set-option -s "user-keys[$i]" "\033W$n"
    tmux bind-key -n "User$i" select-window -t "$n"
    i=$((i + 1))
  done
fi
