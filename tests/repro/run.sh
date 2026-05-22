#!/usr/bin/env bash
# tx-ide reproducibility verify driver. Iterates the numbered check scripts
# in this directory and prints PASS/FAIL per check. Exits 0 iff all PASS.
#
# Usage:
#   tests/repro/run.sh                # run all
#   tests/repro/run.sh 03 05          # run only matching prefixes
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/lib.sh"

shopt -s nullglob
all_checks=("$HERE"/[0-9][0-9]-*.sh)
shopt -u nullglob

selected=()
if (( $# == 0 )); then
  selected=("${all_checks[@]}")
else
  for filter in "$@"; do
    for path in "${all_checks[@]}"; do
      base=$(basename "$path")
      if [[ "$base" == "$filter"* ]]; then
        selected+=("$path")
      fi
    done
  done
fi

if (( ${#selected[@]} == 0 )); then
  printf 'run.sh: no matching checks for: %s\n' "$*" >&2
  exit 2
fi

printf '%s=== tx-ide reproducibility verify ===%s\n\n' $'\e[1m' "$RESET"

failed=()
for path in "${selected[@]}"; do
  base=$(basename "$path" .sh)
  printf '%s%s%s\n' $'\e[1m' "$base" "$RESET"
  if bash "$path"; then
    pass "$base"
  else
    fail "$base"
    failed+=("$base")
  fi
  printf '\n'
done

if (( ${#failed[@]} == 0 )); then
  printf '%sAll checks PASS (%d/%d)%s\n' "$GREEN" "${#selected[@]}" "${#selected[@]}" "$RESET"
  exit 0
fi
printf '%s%d/%d FAILED:%s %s\n' "$RED" "${#failed[@]}" "${#selected[@]}" "$RESET" "${failed[*]}"
exit 1
