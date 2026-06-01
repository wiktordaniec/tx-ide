#!/usr/bin/env bash
# tx-ide tmux fragment. Invoked via `run-shell` from ~/.tx-ide/tmux.conf,
# which is sourced from the user's ~/.tmux.conf by install.sh.
#
# Reads @tx-ide-* options to decide which view features to enable:
#   @tx-ide-popups          on|off   prefix+t (tx), prefix+/ (tx-assistant)
#   @tx-ide-pane-borders    on|off   pane-border-format integration + colors
#   @tx-ide-claude-scroll   on|off   C-u/C-d → PageUp/PageDown in Claude panes
#   @tx-ide-pane-keys       on|off   M-1..9 → select-pane
#   @tx-ide-window-keys     on|off   User0..8 → select-window (terminal must send)
#   @tx-ide-session-labels  on|off   prefix+s shows each session's name + tags (choose-tree)
#   @tx-ide-palette         tokyonight-night|off   color overrides
#
# Composes a tmux.conf fragment and source-file's it, so tmux's own parser
# handles multi-command binds correctly (shell-quoted `;` confuses tmux when
# `bind-key` is invoked directly). Anything the user re-binds later in their
# own tmux.conf wins, since tmux is last-write-wins.
set -u

# The python shim (repo-relative), used by the after-new-window hook to read a session's kind
# from the v2 store. Resolves $TX_IDE_HOME (default ~/.tx-ide) inside the package.
TX="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../bin/tx"
# Repo-relative relabeler the prefix+s bind runs to refresh @tx_name just before choose-tree opens.
RELABEL="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../bin/tmux-session-relabel"

option() {
  tmux show-option -gv "$1" 2>/dev/null || printf '%s' "$2"
}

popups=$(option @tx-ide-popups on)
pane_borders=$(option @tx-ide-pane-borders on)
claude_scroll=$(option @tx-ide-claude-scroll on)
pane_keys=$(option @tx-ide-pane-keys on)
window_keys=$(option @tx-ide-window-keys on)
session_labels=$(option @tx-ide-session-labels on)
palette=$(option @tx-ide-palette tokyonight-night)

CONF=$(mktemp -t tx-ide-bindings.XXXXXX)
trap 'rm -f "$CONF"' EXIT

# --- Popups (prefix+t / prefix+/) ---
# prefix+t opens the tx picker; prefix+/ forwards a line to the tx-assistant. tx attach figures
# out the pane to glue into by asking tmux directly (`display-message -p #{pane_id}`), so the bind
# doesn't need to plumb anything through. There is no mailbox anymore, so prefix+m is left bound to
# its native `select-pane -m` (tx-ide no longer overrides it).
if [ "$popups" = on ]; then
  cat >>"$CONF" <<'EOF'
bind t display-popup -E -w 100 -h 30 -x C -y 1 -T " tx " "tx attach"
bind '/' command-prompt -p "tx-assistant>" {
  set-buffer -b tx-assistant-input "%%"
  run-shell -b "tx-assistant --from-buffer"
}
EOF
fi

# --- Pane borders ---
# Border style + format integrating tmux-pane-session-name. Hex literals
# duplicate shared/palette.sh:
#   ACCENT_HEX       #7aa2f7   (active border)
#   BORDER_DIM_HEX   #3b4261   (inactive border)
if [ "$pane_borders" = on ]; then
  if [ "$palette" = tokyonight-night ]; then
    cat >>"$CONF" <<'EOF'
set -g pane-border-style "fg=#3b4261"
set -g pane-active-border-style "fg=#7aa2f7,bold"
EOF
  fi
  cat >>"$CONF" <<'EOF'
set -g pane-border-lines heavy
set -g pane-border-indicators both
set -g pane-border-status off
set -g pane-border-format " [#P] #(tmux-pane-session-name #D) "
EOF
  cat >>"$CONF" <<EOF
set-hook -g after-new-window "if-shell 'test \"\$($TX _pane-kind \"#{@tx_id}\")\" = view' 'setw pane-border-status top'"
EOF
fi

# --- Session labels in prefix+s (choose-tree) ---
if [ "$session_labels" = on ]; then
  cat >>"$CONF" <<EOF
bind s {
  run-shell "$RELABEL"
  choose-tree -Zs -F '#{?session_format,#{?@tx_name,#{@tx_name}  ,}#{session_windows}w#{?session_attached, (attached),},#{?window_format,#{window_index}: #{window_name},#{pane_current_command}}}'
}
EOF
fi

# --- Claude scroll intercept ---
# Inside a Claude Code pane, C-u/C-d scroll the TUI buffer (PageUp/PageDown).
# Outside Claude, they pass through (default copy-mode-vi behavior). Match
# either the literal "claude" command or a version-string-shaped command
# (Claude Code shows its version while loading: "2.1.138").
if [ "$claude_scroll" = on ]; then
  cat >>"$CONF" <<'EOF'
bind -n C-u if -F '#{||:#{==:#{pane_current_command},claude},#{m:[0-9]*.[0-9]*.[0-9]*,#{pane_current_command}}}' 'send-keys PageUp' 'send-keys C-u'
bind -n C-d if -F '#{||:#{==:#{pane_current_command},claude},#{m:[0-9]*.[0-9]*.[0-9]*,#{pane_current_command}}}' 'send-keys PageDown' 'send-keys C-d'
EOF
fi

# --- Pane keys (M-1..9 → select-pane) ---
# Terminal must produce M-N. Configure your terminal (see setup/iterm.sh).
# Default tmux: prefix M-1..7 trigger layout cycling — unbind so a stray
# prefix+digit doesn't reflow your panes.
if [ "$pane_keys" = on ]; then
  for n in 1 2 3 4 5 6 7 8 9; do
    printf 'bind -n M-%s select-pane -t %s\n' "$n" "$n" >>"$CONF"
  done
  for n in 1 2 3 4 5 6 7; do
    printf 'unbind -T prefix M-%s\n' "$n" >>"$CONF"
  done
fi

# --- Window keys (User0..8 → select-window) ---
# Define user-key escape sequences ESC+'W'+digit, then bind. Terminal must
# emit ESC+'W'+digit on Cmd+Opt+N (or equivalent).
if [ "$window_keys" = on ]; then
  i=0
  for n in 1 2 3 4 5 6 7 8 9; do
    printf 'set -s user-keys[%d] "\\033W%s"\n' "$i" "$n" >>"$CONF"
    printf 'bind -n User%d select-window -t %s\n' "$i" "$n" >>"$CONF"
    i=$((i + 1))
  done
fi

tmux source-file "$CONF"
