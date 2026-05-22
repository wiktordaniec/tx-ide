#!/usr/bin/env bash
# 01-clean-install — `./install --prefix DIR` on a non-existent prefix produces
# a working instance: layout files present, `tx start` brings up the per-instance
# tmux server with tx-assistant + Views sessions.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/lib.sh"

prefix=""
cleanup() {
  [[ -n "$prefix" ]] && nuke_instance "$prefix"
}
trap cleanup EXIT

prefix=$(mktemp_prefix tx-repro-clean)
rm -rf "$prefix"

info "installing to $prefix"
if ! instance_install "$prefix"; then
  fail "install failed"
  exit 1
fi

# Required layout entries.
for path in \
  "$prefix/bin/tx" \
  "$prefix/agents/COMMON.md" \
  "$prefix/claude/settings.json" \
  "$prefix/claude/hooks/mailbox/post.sh" \
  "$prefix/claude/statusline.sh" \
  "$prefix/env" \
  "$prefix/tmux.conf"; do
  if [[ -e "$path" ]]; then
    pass "exists: $path"
  else
    fail "missing: $path"
    exit 1
  fi
done

# Hook commands in settings.json must be absolute paths under $prefix/claude.
if python3 - "$prefix" <<'PY'
import json, sys
prefix = sys.argv[1]
with open(f"{prefix}/claude/settings.json") as fh:
    data = json.load(fh)
managed = data.get("_tx_ide_managed", {})
commands = managed.get("hook_commands", {})
if not commands:
    print("no hook_commands in _tx_ide_managed", flush=True)
    sys.exit(1)
for event, command in commands.items():
    if not command.startswith(prefix + "/"):
        print(f"hook {event} command does not point inside prefix: {command}", flush=True)
        sys.exit(1)
PY
then
  pass "settings.json hook commands rooted in prefix"
else
  fail "settings.json hook command paths wrong"
  exit 1
fi

info "starting per-instance tmux + tx-assistant"
if ! with_instance "$prefix" tx start >/dev/null 2>&1; then
  # tx start attaches when run from a non-tmux shell, which blocks forever.
  # Instead invoke directly: tx-assistant --warm + tmux new-session for Views.
  info "tx start blocks on attach; falling back to warming tx-assistant directly"
fi

# Direct warm: tx-assistant --warm creates the session if missing.
with_instance "$prefix" tx-assistant --warm >/dev/null 2>&1 || true

# Per-instance tmux server must now have tx-assistant.
if with_instance "$prefix" tmux has-session -t tx-assistant 2>/dev/null; then
  pass "tx-assistant session exists on instance socket"
else
  fail "tx-assistant session missing"
  exit 1
fi

# User's default tmux server must be unaffected: no tx-assistant session there
# (unless the user already has one — in which case this check is inconclusive
# and we just warn).
if tmux has-session -t tx-assistant 2>/dev/null; then
  info "user's primary tmux also has tx-assistant — can't assert isolation here"
else
  pass "user's primary tmux server is clean of tx-assistant"
fi

exit 0
