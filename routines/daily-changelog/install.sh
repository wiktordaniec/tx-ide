#!/usr/bin/env bash
#
# Install the daily-changelog routine on this machine:
#   - copy the routine into a stable dir (independent of the repo checkout/branch)
#   - render the launchd LaunchAgent plist with absolute paths
#   - optionally load the schedule (--load)
#
# Re-running is idempotent: it refreshes the installed files and plist.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TX_IDE_HOME="${TX_IDE_HOME:-$HOME/.tx-ide}"
INSTALL_DIR="${TX_IDE_HOME}/routines/daily-changelog"
LOG_DIR="${INSTALL_DIR}/logs"
PLIST_LABEL="com.wiktor.daily-changelog"
PLIST_DEST="${HOME}/Library/LaunchAgents/${PLIST_LABEL}.plist"

LOAD=0
[ "${1:-}" = "--load" ] && LOAD=1

mkdir -p "$INSTALL_DIR" "$LOG_DIR" "${INSTALL_DIR}/runs"

for file in collect.py run.sh AGENT.md config.json README.md; do
  cp "${SRC_DIR}/${file}" "${INSTALL_DIR}/${file}"
done
chmod +x "${INSTALL_DIR}/collect.py" "${INSTALL_DIR}/run.sh"

PATH_VALUE="${HOME}/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
mkdir -p "${HOME}/Library/LaunchAgents"
sed -e "s|__RUN_SH__|${INSTALL_DIR}/run.sh|g" \
    -e "s|__LOG__|${LOG_DIR}/launchd.log|g" \
    -e "s|__PATH__|${PATH_VALUE}|g" \
    -e "s|__HOME__|${HOME}|g" \
    "${SRC_DIR}/${PLIST_LABEL}.plist.template" > "$PLIST_DEST"

echo "Installed routine : ${INSTALL_DIR}"
echo "Rendered plist    : ${PLIST_DEST}"
echo "launchd log       : ${LOG_DIR}/launchd.log"

if [ "$LOAD" -eq 1 ]; then
  launchctl unload "$PLIST_DEST" 2>/dev/null || true
  launchctl load "$PLIST_DEST"
  echo "Schedule LOADED — runs daily at 06:00 local time."
  launchctl list | grep "$PLIST_LABEL" || true
else
  echo
  echo "Schedule NOT loaded. Validate first with a manual run:"
  echo "  ${INSTALL_DIR}/run.sh --date <YYYY-MM-DD>"
  echo "Then enable the daily schedule with:"
  echo "  launchctl load ${PLIST_DEST}        (or: $0 --load)"
fi
