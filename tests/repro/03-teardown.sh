#!/usr/bin/env bash
# 03-teardown — `tx teardown` kills the instance's tmux server + mx-speaker
# cleanly, is idempotent (second run is a no-op), leaves no surviving processes
# associated with the prefix.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/lib.sh"

prefix=""
cleanup() {
  [[ -n "$prefix" ]] && nuke_instance "$prefix"
}
trap cleanup EXIT

prefix=$(mktemp_prefix tx-repro-teardown); rm -rf "$prefix"

instance_install "$prefix" || { fail "install failed"; exit 1; }

# Warm tx-assistant on the instance (creates the per-instance tmux server +
# spawns mx-speaker via install's section 10).
with_instance "$prefix" tx-assistant --warm >/dev/null 2>&1 || true

# tx teardown — first invocation.
if with_instance "$prefix" tx teardown >/dev/null 2>&1; then
  pass "tx teardown #1 exited 0"
else
  fail "tx teardown #1 failed"
  exit 1
fi

# tx teardown — second invocation (idempotent).
if with_instance "$prefix" tx teardown >/dev/null 2>&1; then
  pass "tx teardown #2 exited 0 (idempotent)"
else
  fail "tx teardown #2 failed (not idempotent)"
  exit 1
fi

# pidfile must be gone.
if [[ ! -f "$prefix/claude/mailbox/mx-speaker.pid" ]]; then
  pass "mx-speaker pidfile removed"
else
  fail "mx-speaker pidfile still present"
  exit 1
fi

# Per-instance tmux socket must be unreachable.
if TMUX_TMPDIR="$prefix/tmux" tmux -f "$prefix/tmux.conf" info >/dev/null 2>&1; then
  fail "per-instance tmux server still alive after teardown"
  exit 1
else
  pass "per-instance tmux server dead"
fi

# No mx-speaker process should reference the instance prefix.
if pgrep -f "$prefix" >/dev/null 2>&1; then
  fail "process referencing $prefix still running"
  pgrep -alf "$prefix" >&2 || true
  exit 1
else
  pass "no surviving process references prefix"
fi

exit 0
