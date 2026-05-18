#!/usr/bin/env bash
# Scaffold a leader/orchestration directory from leader-template/.
# Never overwrites existing files — reports them and skips.
#
# Usage: ./init-leader.sh <target-path>
set -u

REPO="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE="$REPO/leader-template"

if [[ $# -ne 1 ]]; then
  printf 'usage: %s <target-path>\n' "$0" >&2
  exit 2
fi

target="$1"
target="${target/#\~/$HOME}"

if [[ ! -d "$TEMPLATE" ]]; then
  printf 'error: leader-template not found at %s\n' "$TEMPLATE" >&2
  exit 1
fi

# Don't scaffold into the tx-ide repo itself (would create root-level copies
# of CLAUDE.md, agents/, start-leader.sh next to leader-template/).
target_abs=$(cd "$target" 2>/dev/null && pwd || printf '%s' "$target")
if [[ "$target_abs" == "$REPO" ]] || [[ -d "$target_abs/leader-template" ]]; then
  printf 'error: %s looks like the tx-ide repo itself — refusing to scaffold into it.\n' "$target" >&2
  printf '       pick a different path (e.g. ~/Code/my-orchestrator).\n' >&2
  exit 1
fi

mkdir -p "$target"

G=$'\e[32m'; Y=$'\e[33m'; D=$'\e[2m'; X=$'\e[0m'
copied=0
skipped=0

# Walk every file in the template, mirror into target.
while IFS= read -r -d '' file; do
  rel="${file#$TEMPLATE/}"
  dst="$target/$rel"
  if [[ -e "$dst" ]]; then
    printf '  %s→%s %-44s %s%s%s\n' "$Y" "$X" "$rel" "$Y" "already exists, skipped" "$X"
    skipped=$((skipped + 1))
  else
    mkdir -p "$(dirname "$dst")"
    cp "$file" "$dst"
    # preserve exec bit
    [[ -x "$file" ]] && chmod +x "$dst"
    printf '  %s→%s %-44s %s%s%s\n' "$G" "$X" "$rel" "$G" "copied" "$X"
    copied=$((copied + 1))
  fi
done < <(find "$TEMPLATE" -type f -print0)

printf '\n%scopied: %d, skipped: %d%s\n' "$D" "$copied" "$skipped" "$X"
printf '\nNext:\n'
printf '  cd %s\n' "$target"
printf '  ./start-leader.sh\n'
