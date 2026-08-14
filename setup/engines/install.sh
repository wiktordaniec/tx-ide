#!/usr/bin/env bash
# setup/engines/install.sh — the unified engine-hooks installer (engine-abstraction design §4.6).
# One orchestrator that drives the per-engine integration scripts setup/engines/<engine>.sh, so the
# tx core never special-cases an agent: a new engine ships its own setup/engines/<name>.sh and is
# driven here, nothing else changes.
#
#   install.sh {install|uninstall|status} [--dry-run] [--engine NAME]... \
#              [--claude-settings PATH] [--codex-settings PATH]
#
# With no --engine, drives the DEFAULT engine set (claude only) — Codex is opt-in (design §4.3), so a
# bare tx-ide install never writes the user's ~/.codex. Repeat --engine to add engines:
#   install.sh install --engine claude --engine codex
# --claude-settings / --codex-settings forward to that engine's --settings (sandbox testing against a
# settings.json / config.toml COPY); --dry-run forwards to every engine.
set -euo pipefail

SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

B=$'\e[1m'; D=$'\e[2m'; X=$'\e[0m'
header() { printf '\n%s%s%s\n' "$B" "$*" "$X"; }

usage() {
  cat >&2 <<EOF
usage: install.sh {install|uninstall|status} [--dry-run] [--engine NAME]...
                  [--claude-settings PATH] [--codex-settings PATH]

  drives setup/engines/<engine>.sh for each selected engine (default: claude).

  --engine NAME         add an engine (claude, codex, antigravity); repeatable
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
# Default engine set: claude only (Codex is opt-in — design §4.3).
[[ ${#ENGINES[@]} -gt 0 ]] || ENGINES=(claude)

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
