#!/bin/bash
# Shared mailbox paths. Sourced by post.sh, pre.sh, end.sh:
#   source "$(dirname "$0")/paths.sh"

mailbox_directory="$HOME/.claude/mailbox"
inbox_file="$mailbox_directory/inbox.jsonl"
running_directory="$mailbox_directory/running.d"

# pwd -P resolves the ~/.claude/hooks/mailbox install symlink to the real repo.
hooks_directory="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
tx_session_state="$hooks_directory/../../lib/tx-session-state"
tx_python=$(command -v python3.14)
