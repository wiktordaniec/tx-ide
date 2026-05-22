#!/usr/bin/env bash
# 05-hook-routing — running A's post.sh hook with CLAUDE_CONFIG_DIR=A writes to
# A's inbox.jsonl only. B's inbox is untouched. The user's ~/.claude/mailbox/
# inbox is also untouched.
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

prefix_a=$(mktemp_prefix tx-repro-hook-a); rm -rf "$prefix_a"
prefix_b=$(mktemp_prefix tx-repro-hook-b); rm -rf "$prefix_b"

instance_install "$prefix_a" || { fail "install A failed"; exit 1; }
instance_install "$prefix_b" || { fail "install B failed"; exit 1; }

# Snapshot user's primary inbox line count to assert no leak.
user_inbox="$HOME/.claude/mailbox/inbox.jsonl"
user_before=$(file_line_count "$user_inbox")

# Fire A's hook with A's CLAUDE_CONFIG_DIR.
CLAUDE_CONFIG_DIR="$prefix_a/claude" \
  bash "$prefix_a/claude/hooks/mailbox/post.sh" </dev/null >/dev/null 2>&1

a_lines=$(file_line_count "$prefix_a/claude/mailbox/inbox.jsonl")
b_lines=$(file_line_count "$prefix_b/claude/mailbox/inbox.jsonl")
user_after=$(file_line_count "$user_inbox")

if (( a_lines == 1 )); then
  pass "A's inbox has exactly 1 entry after A's hook"
else
  fail "A's inbox should be 1 line, got $a_lines"
  exit 1
fi
if (( b_lines == 0 )); then
  pass "B's inbox is empty"
else
  fail "B's inbox should be empty, got $b_lines"
  exit 1
fi
if (( user_after == user_before )); then
  pass "user's primary inbox unchanged"
else
  fail "user's primary inbox grew from $user_before to $user_after"
  exit 1
fi

# Now reverse: fire B's hook with B's env.
CLAUDE_CONFIG_DIR="$prefix_b/claude" \
  bash "$prefix_b/claude/hooks/mailbox/post.sh" </dev/null >/dev/null 2>&1

a_lines2=$(file_line_count "$prefix_a/claude/mailbox/inbox.jsonl")
b_lines2=$(file_line_count "$prefix_b/claude/mailbox/inbox.jsonl")
user_after2=$(file_line_count "$user_inbox")

if (( a_lines2 == a_lines )); then
  pass "A's inbox unchanged by B's hook"
else
  fail "A's inbox changed from $a_lines to $a_lines2 after B's hook"
  exit 1
fi
if (( b_lines2 == 1 )); then
  pass "B's inbox has exactly 1 entry after B's hook"
else
  fail "B's inbox should be 1 line, got $b_lines2"
  exit 1
fi
if (( user_after2 == user_before )); then
  pass "user's primary inbox still unchanged"
else
  fail "user's primary inbox grew from $user_before to $user_after2"
  exit 1
fi

exit 0
