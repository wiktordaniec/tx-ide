#!/usr/bin/env bash
#
# Daily changelog routine — the wrapper launchd invokes at 06:00 Europe/Warsaw.
#
#   1. fetch each project's origin so origin/<branch> reflects what landed
#   2. collect the previous day's git activity (collect.py -> digest JSON)
#   3. tx spawn a tracked Claude agent (tag: daily-changelog,routine) that
#      summarizes the digest and prepends a dated entry to each project's
#      Linear changelog doc
#   4. wait for the agent's sentinel, then end the session (record kept)
#
# Written for the macOS system bash (3.2) — no mapfile / associative arrays.
#
# Usage:
#   run.sh                       # yesterday (Europe/Warsaw), full run
#   run.sh --date 2026-06-13     # a specific day
#   run.sh --no-spawn            # collector only: print the digest and stop
#   run.sh --no-teardown         # leave the spawned session alive afterwards
#   run.sh --timeout 1800        # seconds to wait for the agent (default 1200)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="${SCRIPT_DIR}/config.json"
COLLECT="${SCRIPT_DIR}/collect.py"
AGENT_MD="${SCRIPT_DIR}/AGENT.md"
RUN_DIR="${SCRIPT_DIR}/runs"
mkdir -p "$RUN_DIR"

# launchd hands us a minimal PATH; make sure tx, claude, git, python3 resolve.
export PATH="${HOME}/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
PYTHON="$(command -v python3)"
TXIDE_REPO="${HOME}/Desktop/Coding/tx-ide"

DATE=""
SPAWN=1
TEARDOWN=1
TIMEOUT_SECONDS=1200

while [ $# -gt 0 ]; do
  case "$1" in
    --date) DATE="$2"; shift 2 ;;
    --no-spawn) SPAWN=0; shift ;;
    --no-teardown) TEARDOWN=0; shift ;;
    --timeout) TIMEOUT_SECONDS="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] $*"; }

if [ -z "$DATE" ]; then
  DATE="$("$PYTHON" "$COLLECT" --config "$CONFIG" --print-date)"
fi
log "daily-changelog run for ${DATE}"

# 1. fetch each repo (best-effort; one bad repo must not abort the others)
while read -r repo; do
  [ -n "$repo" ] || continue
  if git -C "$repo" rev-parse --git-dir >/dev/null 2>&1; then
    log "fetching ${repo}"
    git -C "$repo" fetch --quiet --prune origin 2>/dev/null || log "WARN: fetch failed for ${repo}"
  else
    log "WARN: not a git repo (skipping fetch): ${repo}"
  fi
done < <("$PYTHON" "$COLLECT" --config "$CONFIG" --print-repos)

# 2. collect the digest
DIGEST="${RUN_DIR}/digest-${DATE}.json"
log "collecting git activity -> ${DIGEST}"
"$PYTHON" "$COLLECT" --config "$CONFIG" --date "$DATE" --output "$DIGEST"

if [ "$SPAWN" -eq 0 ]; then
  log "collector-only run (--no-spawn); digest follows:"
  cat "$DIGEST"
  exit 0
fi

# 3. spawn the routine agent through tx (tagged routine, as the user requested)
SENTINEL="${RUN_DIR}/done-${DATE}.json"
rm -f "$SENTINEL"
SESSION="changelog-${DATE}"
PRIMING="Automated daily-changelog routine, unattended. Read ${AGENT_MD} and follow it exactly. Digest JSON: ${DIGEST}. Write your result sentinel to: ${SENTINEL}. Target date: ${DATE}."

# Retire a same-named session from a prior run before re-spawning.
tx kill "$SESSION" >/dev/null 2>&1 || true

log "spawning tx routine session: ${SESSION}"
tx spawn "$SESSION" --tag "daily-changelog,routine" --cwd "$TXIDE_REPO" \
  --engine claude --model "opus[1m]" --effort max --prompt "$PRIMING"

# 4. wait for the agent's sentinel, then tear the session down
log "waiting for sentinel (timeout ${TIMEOUT_SECONDS}s): ${SENTINEL}"
elapsed=0
while [ ! -f "$SENTINEL" ]; do
  if [ "$elapsed" -ge "$TIMEOUT_SECONDS" ]; then
    log "ERROR: timed out waiting for the routine agent; leaving ${SESSION} for inspection"
    exit 1
  fi
  sleep 15
  elapsed=$((elapsed + 15))
done

log "routine finished; sentinel:"
cat "$SENTINEL"
echo

if [ "$TEARDOWN" -eq 1 ]; then
  log "ending session ${SESSION} (record kept; resume with: tx resume ${SESSION})"
  tx kill "$SESSION" >/dev/null 2>&1 || log "WARN: could not end ${SESSION}"
fi
log "done."
