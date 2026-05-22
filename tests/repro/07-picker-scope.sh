#!/usr/bin/env bash
# 07-picker-scope — `tx ls` (and by extension the fzf picker) inside instance A
# sees only A's sessions; B sees only B's. Sessions don't bleed across sockets.
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

prefix_a=$(mktemp_prefix tx-repro-pick-a); rm -rf "$prefix_a"
prefix_b=$(mktemp_prefix tx-repro-pick-b); rm -rf "$prefix_b"

instance_install "$prefix_a" || { fail "install A failed"; exit 1; }
instance_install "$prefix_b" || { fail "install B failed"; exit 1; }

with_instance "$prefix_a" tx spawn pickerscope-a --tag llm,a --cmd 'sleep 120' >/dev/null 2>&1 \
  || { fail "spawn pickerscope-a on instance A failed"; exit 1; }
with_instance "$prefix_b" tx spawn pickerscope-b --tag llm,b --cmd 'sleep 120' >/dev/null 2>&1 \
  || { fail "spawn pickerscope-b on instance B failed"; exit 1; }

list_a=$(with_instance "$prefix_a" tx ls 2>/dev/null || true)
list_b=$(with_instance "$prefix_b" tx ls 2>/dev/null || true)

if grep -q pickerscope-a <<<"$list_a"; then
  pass "A's tx ls includes pickerscope-a"
else
  fail "A's tx ls missing pickerscope-a"
  printf '%s\n' "$list_a" >&2
  exit 1
fi
if grep -q pickerscope-b <<<"$list_a"; then
  fail "A's tx ls leaked pickerscope-b"
  exit 1
else
  pass "A's tx ls hides pickerscope-b"
fi
if grep -q pickerscope-b <<<"$list_b"; then
  pass "B's tx ls includes pickerscope-b"
else
  fail "B's tx ls missing pickerscope-b"
  printf '%s\n' "$list_b" >&2
  exit 1
fi
if grep -q pickerscope-a <<<"$list_b"; then
  fail "B's tx ls leaked pickerscope-a"
  exit 1
else
  pass "B's tx ls hides pickerscope-a"
fi

# Cross-check against the user's primary tmux server: it must not have these.
if tmux has-session -t pickerscope-a 2>/dev/null || tmux has-session -t pickerscope-b 2>/dev/null; then
  fail "user's primary tmux server unexpectedly has a pickerscope session"
  exit 1
else
  pass "user's primary tmux server is clean of pickerscope sessions"
fi

exit 0
