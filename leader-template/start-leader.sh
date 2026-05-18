#!/usr/bin/env bash
# Start the leader Claude Code session in a tmux session.
# If already running, prints the attach command.
set -u

SESSION_NAME="${LEADER_SESSION_NAME:-leader}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if tmux has-session -t "=$SESSION_NAME" 2>/dev/null; then
  echo "Leader session already running."
  echo "Attach with: tmux attach -t $SESSION_NAME"
  echo "Or: tx (filter by 'leader')"
  exit 0
fi

# Continue prior conversation if available; fall back to fresh session.
tmux new-session -d -s "$SESSION_NAME" -c "$SCRIPT_DIR" \
  -e COLORTERM=truecolor -e TERM=xterm-256color \
  "claude --continue || claude"

# Tag the session so tx surfaces it.
tmux set -t "$SESSION_NAME" @tag "leader,llm"

echo "Leader started in tmux session '$SESSION_NAME'."
echo "Attach with: tmux attach -t $SESSION_NAME"
echo "Or run 'tx' and pick it."
echo ""
echo "On first attach, the session needs to be primed:"
echo "  Type:  Read agents/COMMON.md and agents/LEADER.md as your first actions and follow them for the duration of this session."
