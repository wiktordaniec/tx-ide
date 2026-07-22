#!/usr/bin/env bash
# tx-ide tmux fragment. Invoked via `run-shell` from ~/.tx-ide/tmux.conf,
# which is sourced from the user's ~/.tmux.conf by install.sh.
#
# Reads @tx-ide-* options to decide which view features to enable:
#   @tx-ide-popups          on|off   prefix+t (tx), prefix+/ (tx-assistant)
#   @tx-ide-pane-borders    on|off   pane-border-format integration + colors
#   @tx-ide-agent-scroll    on|off   C-u/C-d → PageUp/PageDown in agent panes
#   @tx-ide-nav-keys        on|off   C-h/j/k/l seamless nav: nvim splits ↔ panes ↔ nested sessions
#   @tx-ide-pane-keys       on|off   M-1..9 → select-pane
#   @tx-ide-window-keys     on|off   User0..8 → select-window (terminal must send)
#   @tx-ide-session-labels  on|off   prefix+s shows each session's name + tags (choose-tree)
#   @tx-ide-graph-focus     on|off   focus hooks → poke the session-graph dashboard's focus ring
#   @tx-ide-palette         tokyonight-night|off   color overrides
#
# Composes a tmux.conf fragment and source-file's it, so tmux's own parser
# handles multi-command binds correctly (shell-quoted `;` confuses tmux when
# `bind-key` is invoked directly). Anything the user re-binds later in their
# own tmux.conf wins, since tmux is last-write-wins.
set -u

# Repo-relative relabeler the prefix+s bind runs to refresh @tx_name just before choose-tree opens.
RELABEL="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../bin/tmux-session-relabel"
# Repo-relative poke the focus hooks run (backgrounded) to nudge the session-graph dashboard's ring.
FOCUS_POKE="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../bin/tx-graph-focus-poke"
# Repo-relative navigator the nav-keys binds call on a session-edge press (the bubble path).
NAV="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../bin/tmux-nav"

option() {
  tmux show-option -gv "$1" 2>/dev/null || printf '%s' "$2"
}

popups=$(option @tx-ide-popups on)
pane_borders=$(option @tx-ide-pane-borders on)
agent_scroll=$(option @tx-ide-agent-scroll on)
nav_keys=$(option @tx-ide-nav-keys on)
pane_keys=$(option @tx-ide-pane-keys on)
window_keys=$(option @tx-ide-window-keys on)
session_labels=$(option @tx-ide-session-labels on)
graph_focus=$(option @tx-ide-graph-focus on)
palette=$(option @tx-ide-palette tokyonight-night)

CONF=$(mktemp -t tx-ide-bindings.XXXXXX)
trap 'rm -f "$CONF"' EXIT

# --- Popups (prefix+t / prefix+/) ---
# prefix+t opens the tx picker; prefix+/ forwards a line to the tx-assistant. tx attach figures
# out the pane to glue into by asking tmux directly (`display-message -p #{pane_id}`), so the bind
# doesn't need to plumb anything through. There is no mailbox anymore, so prefix+m is left bound to
# its native `select-pane -m` (tx-ide no longer overrides it).
#
# The picker popup is `-w 118`: wide enough for NAME + the 25-col LOCATION + STARTED/IDLE/ROLE + tag
# chips without wrapping, with ~5 cols of extra NAME headroom. Keep it in lockstep with
# `_NAMEW_OVERHEAD` (lib/tx/render.py): that budget assumes LOCATION is 25, and the popup funds those
# columns plus the extra NAME room so the NAME column isn't squeezed.
if [ "$popups" = on ]; then
  cat >>"$CONF" <<'EOF'
bind t display-popup -E -w 118 -h 30 -x C -y 1 -T " tx " "tx attach"
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
  # A new window in a VIEW gets window-top pane borders (so nested panes carry a labelled border).
  # A view is marked by the @tx_view session option, so the hook reads it directly with `if-shell
  # -F` (non-empty/non-zero = true) — no Python launch on the hook path.
  cat >>"$CONF" <<'EOF'
set-hook -g after-new-window "if-shell -F '#{@tx_view}' 'setw pane-border-status top'"
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

# --- Session-graph focus hooks ---
# The terminal → viewer half of the focus link: on every pane/window/session focus change, poke the
# running session-graph dashboard (prototypes/sessions-graph) so it re-rings the node for the session
# in the now-active pane. The poke is backgrounded (run-shell -b — never blocks the switch) and a
# no-op when the dashboard is down (tx-graph-focus-poke exits early when no port file is advertised).
# `focus-events on` is what makes pane-focus-in fire from the terminal. The hook set is deliberately
# broad — the server dedups each poke, so over-firing is free, and the spread covers pane select, the
# active-pane / active-window changes (whatever the cause), session switches, a pane exiting (focus
# auto-reselect), and detach (clears the ring). Each is `set -g` (replace), so re-sourcing is idempotent.
if [ "$graph_focus" = on ]; then
  cat >>"$CONF" <<EOF
set -g focus-events on
set-hook -g pane-focus-in 'run-shell -b "$FOCUS_POKE"'
set-hook -g after-select-pane 'run-shell -b "$FOCUS_POKE"'
set-hook -g after-select-window 'run-shell -b "$FOCUS_POKE"'
set-hook -g window-pane-changed 'run-shell -b "$FOCUS_POKE"'
set-hook -g session-window-changed 'run-shell -b "$FOCUS_POKE"'
set-hook -g client-session-changed 'run-shell -b "$FOCUS_POKE"'
set-hook -g pane-exited 'run-shell -b "$FOCUS_POKE"'
set-hook -g client-detached 'run-shell -b "$FOCUS_POKE"'
EOF
fi

# --- Agent scroll intercept ---
# Inside an agent (Claude Code / Codex) pane, C-u/C-d scroll the TUI buffer (PageUp/PageDown).
# Outside one, they pass through (default copy-mode-vi behavior). Match the literal "claude" or
# "codex" command, or a version-string-shaped command (an agent TUI shows its version while
# loading: "2.1.138"). Kept in LOCKSTEP with the Python agent-pane predicates (spawn.infer_role /
# reconcile._is_agent_command) — same set of engine binaries, enforced by review across the boundary.
if [ "$agent_scroll" = on ]; then
  cat >>"$CONF" <<'EOF'
bind -n C-u if -F '#{||:#{==:#{pane_current_command},claude},#{||:#{==:#{pane_current_command},codex},#{m:[0-9]*.[0-9]*.[0-9]*,#{pane_current_command}}}}' 'send-keys PageUp' 'send-keys C-u'
bind -n C-d if -F '#{||:#{==:#{pane_current_command},claude},#{||:#{==:#{pane_current_command},codex},#{m:[0-9]*.[0-9]*.[0-9]*,#{pane_current_command}}}}' 'send-keys PageDown' 'send-keys C-d'
EOF
fi

# --- Nav keys (C-h/j/k/l: nvim splits ↔ panes ↔ nested sessions) ---
# One root-table bind per direction, evaluated PER CLIENT — and tx nests sessions as a
# client-in-a-pane on the SAME server (`TMUX= tmux attach`), so the same bind re-fires one
# nesting level down whenever the key is forwarded into a nested client. That collapses the
# navigation to two cases and recurses to any depth for free:
#   - pane runs nvim or a nested tmux client → send the key INTO the pane. nvim moves between
#     its splits (lua/config/tmux-nav.lua) and calls bin/tmux-nav itself at the tabpage edge;
#     a nested client re-evaluates this same bind against the inner session.
#   - otherwise → select-pane, or — when already at the session's edge — bin/tmux-nav, which
#     hops to the pane hosting this session's client and continues the walk in the outer
#     session (`#{client_tty}` pins that first hop to the client that pressed the key).
# `pane_current_command` is exact here precisely BECAUSE the nesting is a client-in-a-pane:
# the pane's foreground process IS `tmux` (the nested client) or `nvim` — no ps hackery.
# Root-table binds don't fire in copy-mode or popups, so those keep their keys.
#
# Cost: C-h/j/k/l no longer reach shells or agent TUIs (zsh C-l clear, claude C-j newline…).
# prefix+C-h/j/k/l sends the literal key instead — re-wrapping the prefix per nesting level,
# so each hop unwraps once and the innermost non-tmux pane receives the bare key. The prefix
# is read at compose time (run-shell executes after the user's tmux.conf set it); a `none`
# prefix skips the escape binds. M-1..9 direct pane jumps are unaffected.
if [ "$nav_keys" = on ]; then
  nav_pass='#{||:#{==:#{pane_current_command},nvim},#{==:#{pane_current_command},tmux}}'
  nav_nested='#{==:#{pane_current_command},tmux}'
  nav_prefix=$(tmux show-option -gv prefix 2>/dev/null || echo C-b)
  while read -r key direction edge; do
    cat >>"$CONF" <<EOF
bind -n C-$key if -F '$nav_pass' {
  send-keys C-$key
} {
  if -F '#{$edge}' {
    run-shell -b "$NAV $direction #{pane_id} #{client_tty}"
  } {
    select-pane -$direction
  }
}
EOF
    if [ "$nav_prefix" != none ]; then
      printf "bind C-%s if -F '%s' 'send-keys %s C-%s' 'send-keys C-%s'\n" \
        "$key" "$nav_nested" "$nav_prefix" "$key" "$key" >>"$CONF"
    fi
  done <<'NAVSPEC'
h L pane_at_left
j D pane_at_bottom
k U pane_at_top
l R pane_at_right
NAVSPEC
else
  # A re-source with the option off must CLEAR previously installed binds — last-write-wins
  # would otherwise leave navigation live until a server restart. Unbind only bindings that
  # are recognizably ours (they test pane_current_command; a user's own C-h/j/k/l binds
  # from before the source line won't), so toggling off never strips a personal scheme.
  for key in h j k l; do
    tmux list-keys -T root "C-$key" 2>/dev/null | grep -q 'pane_current_command' &&
      printf 'unbind -n C-%s\n' "$key" >>"$CONF"
    tmux list-keys -T prefix "C-$key" 2>/dev/null | grep -q 'pane_current_command' &&
      printf 'unbind C-%s\n' "$key" >>"$CONF"
  done
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
