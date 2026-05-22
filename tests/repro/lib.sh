#!/usr/bin/env bash
# Shared helpers for the per-instance reproducibility verify harness.
# Sourced by run.sh and the individual 0N-*.sh check scripts.

# REPO_ROOT is two levels up from this file: tests/repro/lib.sh → tests/ → repo.
LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$LIB_DIR/../.." && pwd)"

GREEN=$'\e[32m'
RED=$'\e[31m'
YELLOW=$'\e[33m'
DIM=$'\e[2m'
RESET=$'\e[0m'

pass()  { printf '  %sPASS%s %s\n'  "$GREEN"  "$RESET" "$1"; }
fail()  { printf '  %sFAIL%s %s\n'  "$RED"    "$RESET" "$1" >&2; }
info()  { printf '  %s%s%s\n'       "$DIM"    "$1"     "$RESET"; }

# A unique scratch dir for the current check. Caller is expected to:
#   prefix_dir=$(mktemp_prefix tx-repro-01)
#   instance_install "$prefix_dir"
#   ... checks ...
#   nuke_instance "$prefix_dir"
mktemp_prefix() {
  local tag="${1:-tx-repro}"
  mktemp -d -t "${tag}.XXXXXX"
}

# Run ./install --prefix PREFIX with the agent-safe flags set so it does not
# touch ~/.zshrc, ~/.tmux.conf, or iTerm. --skip-brew assumes tmux + fzf + python3
# are already on PATH (a fair assumption for any machine that has the primary
# install working).
#
# Refuses to run if the install script does not declare --prefix support — until
# Slice 1 lands, today's install would silently ignore the flag and overwrite
# the user's primary install. The guard is intentionally strict: presence of
# the literal "--prefix" token in the install script.
instance_install() {
  local prefix="$1"
  [[ -z "$prefix" ]] && { fail "instance_install: prefix required"; return 1; }
  if ! grep -q -- '--prefix' "$REPO_ROOT/install"; then
    fail "install script does not support --prefix yet (Slice 1 dependency)"
    return 1
  fi
  ( cd "$REPO_ROOT" && yes Y | ./install \
      --prefix "$prefix" \
      --skip-iterm \
      --skip-shell-rc \
      --skip-brew ) >/dev/null 2>&1
}

# Run COMMAND with this instance's env activated. Subshell so env doesn't leak.
with_instance() {
  local prefix="$1"; shift
  ( set +u; . "$prefix/env"; set -u; "$@" )
}

# Capture user-touchable state into a tarball for byte-comparison later.
# Misses: anything outside these paths. settings.json.bak.* are excluded
# because the install script writes new backups on every run.
snapshot_user_state() {
  local dest="$1"
  local stage
  stage=$(mktemp -d)
  mkdir -p "$stage/home"
  for src in "$HOME/.zshrc" "$HOME/.tmux.conf" "$HOME/.claude/settings.json"; do
    [[ -f "$src" ]] && cp "$src" "$stage/home/$(basename "$src")"
  done
  if [[ -d "$HOME/.tx-ide" ]]; then
    cp -R "$HOME/.tx-ide" "$stage/home/.tx-ide"
  fi
  if [[ -d "$HOME/.local/bin" ]]; then
    mkdir -p "$stage/home/local-bin"
    for entry in tx tx-assistant tmux-pane-for-session tmux-pane-session-name; do
      local link="$HOME/.local/bin/$entry"
      if [[ -L "$link" ]]; then
        printf '%s -> %s\n' "$entry" "$(readlink "$link")" >> "$stage/home/local-bin/symlinks.txt"
      fi
    done
  fi
  ( cd "$stage" && tar -cf "$dest" . )
  rm -rf "$stage"
}

# 0 = identical, 1 = drift detected. Diffs are printed to stderr for the
# caller to surface.
diff_user_state() {
  local snapshot="$1"
  local current
  current=$(mktemp)
  snapshot_user_state "$current"
  local a b
  a=$(mktemp -d); b=$(mktemp -d)
  tar -xf "$snapshot" -C "$a"
  tar -xf "$current"  -C "$b"
  # Exclude install's per-run timestamped backups.
  if diff -r --exclude='*.bak.*' "$a" "$b" >/tmp/tx-repro-diff.$$ 2>&1; then
    rm -rf "$a" "$b" "$current" /tmp/tx-repro-diff.$$
    return 0
  fi
  cat /tmp/tx-repro-diff.$$ >&2
  rm -rf "$a" "$b" "$current" /tmp/tx-repro-diff.$$
  return 1
}

# Kill the per-instance tmux server + mx-speaker. Leaves $prefix dir on disk
# (mirrors `tx teardown` semantics — explicit rm is the caller's job).
kill_instance() {
  local prefix="$1"
  [[ -d "$prefix" ]] || return 0
  if [[ -f "$prefix/env" ]]; then
    with_instance "$prefix" tx teardown >/dev/null 2>&1 || true
  fi
  # Defensive — in case `tx teardown` isn't available yet (slice 3 dependency),
  # fall back to direct pidfile + socket-dir cleanup so harness reruns don't
  # leak processes.
  local pid_file="$prefix/claude/mailbox/mx-speaker.pid"
  if [[ -f "$pid_file" ]]; then
    local pid
    pid=$(cat "$pid_file" 2>/dev/null)
    [[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true
    rm -f "$pid_file"
  fi
  if [[ -d "$prefix/tmux" ]]; then
    TMUX_TMPDIR="$prefix/tmux" tmux -f "$prefix/tmux.conf" kill-server 2>/dev/null || true
  fi
}

# kill_instance + rm. Use in teardown of a check.
nuke_instance() {
  local prefix="$1"
  kill_instance "$prefix"
  [[ -n "$prefix" && -d "$prefix" && "$prefix" == /tmp/* ]] && rm -rf "$prefix"
}

# Append a synthetic mailbox entry. Used by 05-hook-routing + 06-speaker.
append_inbox_entry() {
  local inbox="$1" id="$2" session="${3:-test-session}"
  mkdir -p "$(dirname "$inbox")"
  local timestamp
  timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  printf '{"id":"%s","timestamp":"%s","tmux_session":"%s","tmux_tags":"","cwd":"/tmp"}\n' \
    "$id" "$timestamp" "$session" >> "$inbox"
}

# Number of non-empty lines in a file (0 if missing).
file_line_count() {
  local path="$1"
  [[ -f "$path" ]] || { echo 0; return; }
  grep -cve '^[[:space:]]*$' "$path" 2>/dev/null || echo 0
}
