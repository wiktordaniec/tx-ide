#!/usr/bin/env bash
# setup/engines/install.sh — the unified engine-hooks installer (engine-abstraction design §4.6).
# One orchestrator that drives the per-engine integration scripts setup/engines/<engine>.sh, so the
# tx core never special-cases an agent: a new engine ships its own setup/engines/<name>.sh and is
# driven here, nothing else changes.
#
#   install.sh {install|uninstall|status} [--dry-run] [--engine NAME]... \
#              [--claude-settings PATH] [--codex-settings PATH]
#
# With no --engine, drives every engine tx can spawn whose CLI is ON THIS MACHINE (see the default
# engine set below). An engine tx spawns but never wires is worse than one it does not support at
# all: with no hooks nothing captures the chat id, so the session's conversation can never be
# resumed — the hole Codex sat in while it was opt-in. An absent CLI is still left alone, so a
# machine without codex never grows a ~/.codex. Repeat --engine to force a specific set:
#   install.sh install --engine claude --engine codex
# --claude-settings / --codex-settings forward to that engine's --settings (sandbox testing against a
# settings.json / config.toml COPY); --dry-run forwards to every engine.
set -euo pipefail

SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -P "$SCRIPT_DIR/../.." && pwd)"
LIB_DIR="$REPO_ROOT/lib"
PY="${TX_PYTHON:-python3.14}"

B=$'\e[1m'; D=$'\e[2m'; X=$'\e[0m'
header() { printf '\n%s%s%s\n' "$B" "$*" "$X"; }

usage() {
  cat >&2 <<EOF
usage: install.sh {install|uninstall|status} [--dry-run] [--engine NAME]...
                  [--claude-settings PATH] [--codex-settings PATH]

  drives setup/engines/<engine>.sh for each selected engine.

  --engine NAME         add an engine (claude, codex, antigravity); repeatable
                        (default: every engine whose CLI is on this machine)
  --dry-run             forward --dry-run to every engine
  --claude-settings P   forward to claude.sh --settings P (sandbox)
  --codex-settings P    forward to codex.sh  --settings P (sandbox)
EOF
}

OP=""; DRY_RUN=0; ENGINES=(); CLAUDE_SETTINGS=""; CODEX_SETTINGS=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    install|uninstall|status) OP="$1"; shift ;;
    --dry-run)           DRY_RUN=1; shift ;;
    --engine)            ENGINES+=("${2:?--engine needs a NAME}"); shift 2 ;;
    --engine=*)          ENGINES+=("${1#*=}"); shift ;;
    --claude-settings)   CLAUDE_SETTINGS="${2:?--claude-settings needs a PATH}"; shift 2 ;;
    --claude-settings=*) CLAUDE_SETTINGS="${1#*=}"; shift ;;
    --codex-settings)    CODEX_SETTINGS="${2:?--codex-settings needs a PATH}"; shift 2 ;;
    --codex-settings=*)  CODEX_SETTINGS="${1#*=}"; shift ;;
    -h|--help)           usage; exit 0 ;;
    *) printf 'install.sh: unknown argument: %s\n' "$1" >&2; usage; exit 2 ;;
  esac
done
[[ -n "$OP" ]] || { usage; exit 2; }

# The default engine set, when no --engine was passed. Both the engine list and each engine's binary
# come from the adapter registry (`registry.get(engine).binary`) — the same source of truth
# `tx spawn` resolves an engine through, so an adapter plus its setup/engines/<name>.sh remains all a
# new engine needs. Read at top level (not in a subshell) so an unreadable registry is fatal rather
# than a silent empty set; an engine with no setup script is skipped, not fatal.
if [[ ${#ENGINES[@]} -eq 0 ]]; then
  REGISTRY="$(PYTHONPATH="$LIB_DIR" "$PY" -c '
from tx import spawn  # noqa: F401 - side-effect import: every adapter self-registers
from tx.engines import registry

for engine in sorted(registry.registered(), key=lambda engine: engine.value):
    print(engine.value, registry.get(engine).binary)
')" || {
    printf 'install.sh: could not read the engine registry with %s — pass --engine NAME\n' "$PY" >&2
    exit 2
  }
  while read -r engine binary; do
    [[ -x "$SCRIPT_DIR/$engine.sh" ]] || continue
    # install wires only the engines whose CLI is actually here (an absent one is left alone, so a
    # codex-less machine grows no ~/.codex). uninstall / status cover every engine, so hooks written
    # while an engine WAS installed are still reported and still reversed after the CLI is gone.
    if [[ "$OP" == "install" ]] && ! command -v "$binary" >/dev/null 2>&1; then
      continue
    fi
    ENGINES+=("$engine")
  done <<< "$REGISTRY"
  [[ ${#ENGINES[@]} -gt 0 ]] \
    || { printf 'install.sh: no engine CLI found on PATH — nothing to %s\n' "$OP" >&2; exit 0; }
fi

run_engine() {  # <engine>
  local engine="$1" script="$SCRIPT_DIR/$engine.sh"
  [[ -x "$script" ]] || { printf 'install.sh: no engine script at %s\n' "$script" >&2; exit 2; }
  local -a args=("$OP")
  [[ $DRY_RUN -eq 1 ]] && args+=(--dry-run)
  case "$engine" in
    claude) [[ -n "$CLAUDE_SETTINGS" ]] && args+=(--settings "$CLAUDE_SETTINGS") ;;
    codex)  [[ -n "$CODEX_SETTINGS"  ]] && args+=(--settings "$CODEX_SETTINGS")  ;;
  esac
  header "engine: $engine  ($engine.sh $OP)"
  "$script" "${args[@]}"
}

printf '%s== engines %s ==%s  %sdriving: %s%s\n' "$B" "$OP" "$X" "$D" "${ENGINES[*]}" "$X"
for engine in "${ENGINES[@]}"; do
  run_engine "$engine"
done
