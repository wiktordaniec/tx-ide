#!/usr/bin/env bash
# Start the mx-speaker daemon if not already running. Idempotent: a PID file
# under $CLAUDE_CONFIG_DIR/mailbox gates repeated launches. Output goes to
# the sibling .log. Run by install.sh; safe to re-run by hand.
set -u

script_dir="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
speaker_script="$script_dir/mx_speaker.py"

claude_dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
pid_file="$claude_dir/mailbox/mx-speaker.pid"
log_file="$claude_dir/mailbox/mx-speaker.log"

mkdir -p "$(dirname "$pid_file")"

if [[ -f "$pid_file" ]] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
  printf 'mx-speaker already running (pid %s)\n' "$(cat "$pid_file")"
  exit 0
fi

CLAUDE_CONFIG_DIR="$claude_dir" nohup python3 "$speaker_script" >>"$log_file" 2>&1 &
echo $! >"$pid_file"
disown
printf 'mx-speaker started (pid %s)\n' "$(cat "$pid_file")"
