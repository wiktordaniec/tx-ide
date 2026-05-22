#!/usr/bin/env bash
# 06-speaker-isolation — each instance's mx-speaker watches only its own inbox.
# Appending a synthetic entry to A's inbox makes A's speaker log mention the id;
# B's speaker log does not.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/lib.sh"

prefix_a=""
prefix_b=""
cleanup() {
  [[ -n "$prefix_a" ]] && nuke_instance "$prefix_a"
  [[ -n "$prefix_b" ]] && nuke_instance "$prefix_b"
}
trap cleanup EXIT

prefix_a=$(mktemp_prefix tx-repro-speak-a); rm -rf "$prefix_a"
prefix_b=$(mktemp_prefix tx-repro-speak-b); rm -rf "$prefix_b"

instance_install "$prefix_a" || { fail "install A failed"; exit 1; }
instance_install "$prefix_b" || { fail "install B failed"; exit 1; }

# Disable TTS so checks key off log lines, not audio output. install seeds
# config.json with tts.enabled=true; overwrite both before re-launching the
# speakers under the instance env.
disable_tts() {
  local config="$1/claude/mailbox/config.json"
  mkdir -p "$(dirname "$config")"
  printf '{"tts":{"enabled":false}}\n' >"$config"
}
disable_tts "$prefix_a"
disable_tts "$prefix_b"

# install already launches a speaker (section 10). Kill those so we can
# restart them with the disabled-tts config + correct CLAUDE_CONFIG_DIR.
for prefix in "$prefix_a" "$prefix_b"; do
  pid_file="$prefix/claude/mailbox/mx-speaker.pid"
  [[ -f "$pid_file" ]] && kill "$(cat "$pid_file")" 2>/dev/null || true
  rm -f "$pid_file" "$prefix/claude/mailbox/mx-speaker.log"
done

# Relaunch each speaker with its own env.
CLAUDE_CONFIG_DIR="$prefix_a/claude" bash "$prefix_a/claude/hooks/mailbox/start-speaker.sh" >/dev/null 2>&1
CLAUDE_CONFIG_DIR="$prefix_b/claude" bash "$prefix_b/claude/hooks/mailbox/start-speaker.sh" >/dev/null 2>&1

sleep 0.5

for prefix in "$prefix_a" "$prefix_b"; do
  pid_file="$prefix/claude/mailbox/mx-speaker.pid"
  if [[ -f "$pid_file" ]] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
    pass "speaker alive for $(basename "$prefix")"
  else
    fail "speaker not running for $(basename "$prefix")"
    exit 1
  fi
done

# Append a synthetic, unique entry to A's inbox.
test_id="reprotest$(date +%s)$$"
append_inbox_entry "$prefix_a/claude/mailbox/inbox.jsonl" "$test_id" "speaker-test-session"

# Wait for the poll loop (POLL_INTERVAL_SECONDS=0.5 in mx_speaker.py).
sleep 2

# A's log must mention the new id; B's must not.
log_a="$prefix_a/claude/mailbox/mx-speaker.log"
log_b="$prefix_b/claude/mailbox/mx-speaker.log"

if grep -q "$test_id" "$log_a" 2>/dev/null; then
  pass "A's speaker log mentions test id"
else
  fail "A's speaker log missing test id"
  [[ -f "$log_a" ]] && tail -5 "$log_a" >&2
  exit 1
fi
if grep -q "$test_id" "$log_b" 2>/dev/null; then
  fail "B's speaker log unexpectedly mentions A's test id"
  exit 1
else
  pass "B's speaker log does not mention test id"
fi

exit 0
