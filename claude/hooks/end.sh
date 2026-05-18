#!/bin/bash
# SessionEnd-hook: acks unread inbox entries for this tmux session and
# removes the running.d/<session>.json so the session leaves mx's "active"
# view. Does not write a new inbox entry — session has ended, there is
# nothing left to surface.

cat > /dev/null

source "$(dirname "$0")/paths.sh"

[[ -n "$TMUX" ]] || exit 0

tmux_session_name=$(tmux display-message -p '#S')

rm -f "$running_directory/${tmux_session_name}.json"

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
        try:
            entry = json.loads(stripped)
        except Exception:
            continue
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
