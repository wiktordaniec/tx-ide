#!/bin/bash
# Serialized macOS `say` invocation. macOS lacks flock(1), so we acquire an
# exclusive advisory lock via python's fcntl.flock to ensure concurrent
# callers queue rather than overlap.

set -u

text="${1:-}"
voice="${2:-Samantha}"
[[ -n "$text" ]] || exit 0
command -v say >/dev/null 2>&1 || exit 0

TEXT="$text" VOICE="$voice" python3 <<'PY'
import fcntl, os, subprocess
with open("/tmp/claude-mx-speak.lock", "w") as lock_handle:
    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
    subprocess.run(["say", "-v", os.environ["VOICE"], os.environ["TEXT"]])
PY
