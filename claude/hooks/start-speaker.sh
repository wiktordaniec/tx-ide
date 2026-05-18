#!/usr/bin/env bash
# Start the mx-speaker daemon if not already running. Idempotent: a PID file
# at ~/.claude/mailbox/mx-speaker.pid gates repeated launches. Output goes to
# ~/.claude/mailbox/mx-speaker.log. Run by install.sh; safe to re-run by hand.
set -u

script_dir="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
speaker_script="$script_dir/mx_speaker.py"

pid_file="$HOME/.claude/mailbox/mx-speaker.pid"
log_file="$HOME/.claude/mailbox/mx-speaker.log"

mkdir -p "$(dirname "$pid_file")"

if [[ -f "$pid_file" ]] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
  printf 'mx-speaker already running (pid %s)\n' "$(cat "$pid_file")"
  exit 0
fi

nohup python3 "$speaker_script" >>"$log_file" 2>&1 &
echo $! >"$pid_file"
disown
printf 'mx-speaker started (pid %s)\n' "$(cat "$pid_file")"
