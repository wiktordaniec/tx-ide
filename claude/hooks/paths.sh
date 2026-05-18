#!/bin/bash
# Shared mailbox paths. Sourced by post.sh, pre.sh, end.sh:
#   source "$(dirname "$0")/paths.sh"

mailbox_directory="$HOME/.claude/mailbox"
inbox_file="$mailbox_directory/inbox.jsonl"
running_directory="$mailbox_directory/running.d"
