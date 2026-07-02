#!/usr/bin/env bash
# Deploy the remote-control app to ~/.tx-ide/remote-control-app and (re)start its LaunchAgent.
#
# Why a copy at all: the LaunchAgent runs the server at login/reboot, and macOS TCC forbids
# launchd background items from reading ~/Desktop (where the repo lives) without Full Disk
# Access. So the app + the lib/tx package it imports are rsynced to a TCC-free home path and
# the agent runs from there. Re-run this script after changing the app.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
dest="$HOME/.tx-ide/remote-control-app"

mkdir -p "$dest"
rsync -a --delete \
  "$repo_root/prototypes/remote-control/server.py" \
  "$repo_root/prototypes/remote-control/mobile.html" \
  "$repo_root/prototypes/remote-control/desktop.html" \
  "$repo_root/prototypes/remote-control/manifest.json" \
  "$repo_root/prototypes/remote-control/icon.png" \
  "$dest/"
rsync -a --delete --exclude '__pycache__' "$repo_root/lib/" "$dest/lib/"
rm -f "$dest/index.html"   # pre-split leftover; the server now serves mobile.html/desktop.html

plist="$HOME/Library/LaunchAgents/com.tx-ide.remote-control.plist"
launchctl unload "$plist" 2>/dev/null || true
launchctl load "$plist"
echo "deployed to $dest and (re)loaded the LaunchAgent"
