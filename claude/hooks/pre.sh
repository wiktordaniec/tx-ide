#!/bin/bash
# UserPromptSubmit-hook: writes a running.d/<session>.json so the session
# shows up in mx's "active" view, and marks unread inbox entries from the
# current tmux session as read so replies implicitly ack pending notifications.

cat > /dev/null

source "$(dirname "$0")/paths.sh"

[[ -n "$TMUX" ]] || exit 0

tmux_session_name=$(tmux display-message -p '#S')
tx_id=$(tmux show-options -v @tx_id 2>/dev/null)
tmux_tags=""
[[ -n "$tx_id" ]] && tmux_tags=$("$tx_python" "$tx_session_state" get "$tx_id" tags 2>/dev/null)
timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
working_directory="$PWD"

mkdir -p "$running_directory"
running_file="$running_directory/${tmux_session_name}.json"

RUNNING_FILE="$running_file" \
TIMESTAMP="$timestamp" \
TMUX_SESSION="$tmux_session_name" \
TMUX_TAGS="$tmux_tags" \
WORKING_DIRECTORY="$working_directory" \
python3 <<'PY'
import json, os
running_file = os.environ["RUNNING_FILE"]
entry = {
    "timestamp": os.environ["TIMESTAMP"],
    "tmux_session": os.environ["TMUX_SESSION"],
    "tmux_tags": os.environ["TMUX_TAGS"],
    "cwd": os.environ["WORKING_DIRECTORY"],
}
temp_file = running_file + ".tmp"
with open(temp_file, "w") as out_handle:
    json.dump(entry, out_handle)
os.replace(temp_file, running_file)
PY

[[ -f "$inbox_file" ]] || exit 0

INBOX_FILE="$inbox_file" \
TMUX_SESSION="$tmux_session_name" \
python3 <<'PY'
import json, os

inbox_file = os.environ["INBOX_FILE"]
session_name = os.environ["TMUX_SESSION"]

entries = []
changed = False
with open(inbox_file) as inbox_handle:
    for raw_line in inbox_handle:
        stripped = raw_line.strip()
        if not stripped:
            continue
        entry = json.loads(stripped)
        if not entry.get("read") and entry.get("tmux_session") == session_name:
            entry["read"] = True
            changed = True
        entries.append(entry)

if changed:
    temp_file = inbox_file + ".tmp"
    with open(temp_file, "w") as out_handle:
        for entry in entries:
            out_handle.write(json.dumps(entry) + "\n")
    os.replace(temp_file, inbox_file)
PY

exit 0
