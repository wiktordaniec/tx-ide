#!/usr/bin/env bash
# 04-user-untouched — user's ~/.zshrc, ~/.tmux.conf, ~/.claude/settings.json,
# ~/.tx-ide/, ~/.local/bin/tx* must be byte-identical before and after a full
# per-instance install + warm + teardown cycle.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/lib.sh"

prefix=""
snapshot=""
cleanup() {
  [[ -n "$prefix" ]] && nuke_instance "$prefix"
  [[ -n "$snapshot" ]] && rm -f "$snapshot"
}
trap cleanup EXIT

snapshot=$(mktemp -t tx-repro-snap.XXXXXX)
snapshot_user_state "$snapshot"
pass "snapshotted user state"

prefix=$(mktemp_prefix tx-repro-untouched); rm -rf "$prefix"
instance_install "$prefix" || { fail "install failed"; exit 1; }
with_instance "$prefix" tx-assistant --warm >/dev/null 2>&1 || true
with_instance "$prefix" tx teardown >/dev/null 2>&1 || true

if diff_user_state "$snapshot"; then
  pass "user state byte-identical before and after"
else
  fail "user state drifted (diff printed above)"
  exit 1
fi

exit 0
