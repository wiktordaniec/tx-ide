#!/bin/bash
# Stop-hook: appends one JSONL entry to ~/.claude/mailbox/inbox.jsonl so
# the user can see which tmux session needs their attention, and clears
# the session's running.d entry so it leaves mx's "active" view.

cat > /dev/null

source "$(dirname "$0")/paths.sh"
mkdir -p "$mailbox_directory"

timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
entry_id=$(openssl rand -hex 4)
tmux_session_name="no-tmux"
tmux_tags=""
if [[ -n "$TMUX" ]]; then
  tmux_session_name=$(tmux display-message -p '#S')
  tx_id=$(tmux show-options -v @tx_id 2>/dev/null)
  [[ -n "$tx_id" ]] && tmux_tags=$("$tx_session_state" get "$tx_id" tags 2>/dev/null)
fi
working_directory="$PWD"

INBOX_FILE="$inbox_file" \
ENTRY_ID="$entry_id" \
TIMESTAMP="$timestamp" \
TMUX_SESSION="$tmux_session_name" \
TMUX_TAGS="$tmux_tags" \
WORKING_DIRECTORY="$working_directory" \
python3 <<'PY'
import json, os

inbox_file = os.environ["INBOX_FILE"]
session_name = os.environ["TMUX_SESSION"]

new_entry = {
    "id": os.environ["ENTRY_ID"],
    "timestamp": os.environ["TIMESTAMP"],
    "tmux_session": session_name,
    "tmux_tags": os.environ["TMUX_TAGS"],
    "cwd": os.environ["WORKING_DIRECTORY"],
}

entries = []
if os.path.exists(inbox_file):
    with open(inbox_file) as inbox_handle:
        for raw_line in inbox_handle:
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                entry = json.loads(stripped)
            except Exception:
                continue
            if not entry.get("read") and entry.get("tmux_session") == session_name:
                entry["read"] = True
            entries.append(entry)
entries.append(new_entry)

temp_file = inbox_file + ".tmp"
with open(temp_file, "w") as out_handle:
    for entry in entries:
        out_handle.write(json.dumps(entry) + "\n")
os.replace(temp_file, inbox_file)
PY

if [[ "$tmux_session_name" != "no-tmux" ]]; then
  rm -f "$mailbox_directory/running.d/${tmux_session_name}.json"
fi

exit 0
