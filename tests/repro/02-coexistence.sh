#!/usr/bin/env bash
# 02-coexistence — two instances live side-by-side. Spawning a session inside
# instance A's env produces it on A's tmux server only; same for B.
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

prefix_a=$(mktemp_prefix tx-repro-coex-a); rm -rf "$prefix_a"
prefix_b=$(mktemp_prefix tx-repro-coex-b); rm -rf "$prefix_b"

info "installing A to $prefix_a"
instance_install "$prefix_a" || { fail "install A failed"; exit 1; }
info "installing B to $prefix_b"
instance_install "$prefix_b" || { fail "install B failed"; exit 1; }

with_instance "$prefix_a" tx spawn worker-a --tag llm,a --cmd 'sleep 120' >/dev/null 2>&1 \
  || { fail "spawn worker-a on instance A failed"; exit 1; }
with_instance "$prefix_b" tx spawn worker-b --tag llm,b --cmd 'sleep 120' >/dev/null 2>&1 \
  || { fail "spawn worker-b on instance B failed"; exit 1; }

# tx ls in A must list worker-a, not worker-b.
list_a=$(with_instance "$prefix_a" tx ls 2>/dev/null || true)
if grep -q worker-a <<<"$list_a"; then
  pass "instance A sees worker-a"
else
  fail "instance A missing worker-a"
  printf '%s\n' "$list_a" >&2
  exit 1
fi
if grep -q worker-b <<<"$list_a"; then
  fail "instance A leaked worker-b"
  exit 1
else
  pass "instance A does not see worker-b"
fi

list_b=$(with_instance "$prefix_b" tx ls 2>/dev/null || true)
if grep -q worker-b <<<"$list_b"; then
  pass "instance B sees worker-b"
else
  fail "instance B missing worker-b"
  printf '%s\n' "$list_b" >&2
  exit 1
fi
if grep -q worker-a <<<"$list_b"; then
  fail "instance B leaked worker-a"
  exit 1
else
  pass "instance B does not see worker-a"
fi

exit 0
