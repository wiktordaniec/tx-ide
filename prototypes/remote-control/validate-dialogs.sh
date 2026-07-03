#!/usr/bin/env bash
# Validate remote answering of blocking dialogs — the feature that lets a session stuck on a
# terminal permission prompt be handled from the phone.
#
#   validate-dialogs.sh manual [PORT] [HOST]   stage one puppet per dialog kind, start the server,
#                                              hand you a URL to tap through (default: 8796 loopback)
#   validate-dialogs.sh auto  [PORT]           headless: approve a real Bash prompt via /api/answer
#                                              and assert the command ran (exit 0 = pass)
#
# HOST wider than 127.0.0.1 (a Tailscale/LAN IP) lets a real phone reach it — the server TYPES INTO
# tmux, so set TX_REMOTE_TOKEN in that case (this script passes it through).
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(git -C "$HERE" rev-parse --show-toplevel)"
SERVER="$REPO/prototypes/remote-control/server.py"
STAGE="${TMPDIR:-/tmp}/tx-remote-validate.$$"
mkdir -p "$STAGE"

MODE="${1:-manual}"
PORT="${2:-$([ "$MODE" = auto ] && echo 8796 || echo 8796)}"
HOST="${3:-127.0.0.1}"

resolve_id() {  # newest live session record by name
  grep -l "\"name\": \"$1\"" ~/.tx-ide/sessions/*.json 2>/dev/null \
    | xargs -r ls -t | head -1 | xargs -r basename | sed 's/\.json//'
}
api() { curl -s "http://127.0.0.1:$PORT$1" "${@:2}"; }

# Accept the one-time workspace-trust prompt for any puppet showing it (each runs in a fresh cwd),
# so puppets proceed to their INTENDED dialog. Runs for `$1` seconds in the foreground-ish loop.
accept_trust_for() {  # names...
  for name in "$@"; do
    local id; id=$(resolve_id "$name")
    [ -z "$id" ] && continue
    tmux has-session -t "$id" 2>/dev/null || continue
    if tmux capture-pane -p -t "$id" 2>/dev/null | grep -q "trust this folder"; then
      tmux send-keys -t "$id" -l "1"; sleep 0.3; tmux send-keys -t "$id" Enter
    fi
  done
}

spawn_puppet() {  # name  extra-args  prompt
  local cwd="$STAGE/$1"; mkdir -p "$cwd"
  printf 'greeting = hi\n' > "$cwd/config.py"   # for the edit puppet
  tx spawn "$1" --tag remote-control --cwd "$cwd" --cmd "claude $2 \"$3\"" >/dev/null 2>&1
}

start_server() {
  echo "→ starting the WORKTREE server:  $SERVER  $PORT  $HOST"
  TX_REMOTE_TOKEN="${TX_REMOTE_TOKEN:-}" python3.14 "$SERVER" "$PORT" "$HOST" >"$STAGE/server.log" 2>&1 &
  SERVER_PID=$!
  sleep 1.5
  api /api/inbox -o /dev/null -w "  server up (HTTP %{http_code})\n"
}

# ------------------------------------------------------------------------------------------------
if [ "$MODE" = auto ]; then
  PUPPETS=(vp-bash)
  cleanup() { tx kill vp-bash >/dev/null 2>&1; [ -n "${SERVER_PID:-}" ] && kill "$SERVER_PID" 2>/dev/null; rm -rf "$STAGE"; }
  trap cleanup EXIT
  start_server
  spawn_puppet vp-bash "" "Use the Bash tool to run exactly: touch marker-file.txt — run it immediately, no questions."
  MARKER="$STAGE/vp-bash/marker-file.txt"
  echo "→ driving puppet to the Bash permission prompt…"
  ID=""; BLOCKED=0
  for _ in $(seq 1 60); do
    ID=$(resolve_id vp-bash); [ -z "$ID" ] && { sleep 1; continue; }
    tmux has-session -t "$ID" 2>/dev/null || { sleep 1; continue; }
    accept_trust_for vp-bash
    tmux capture-pane -p -t "$ID" 2>/dev/null | grep -q "Do you want to proceed?" && { BLOCKED=1; break; }
    sleep 1.5
  done
  [ "$BLOCKED" = 1 ] || { echo "FAIL: puppet never reached the Bash prompt"; exit 1; }
  [ -f "$MARKER" ] && { echo "FAIL: command ran before it was answered"; exit 1; }

  # The pane shows the prompt a beat before tx stamps the session WAITING (build_inbox_feed only
  # reports a block for a WAITING session) — poll the feed until it reports the answerable block.
  TUID=""; OPT=""
  for _ in $(seq 1 15); do
    api /api/inbox > "$STAGE/feed.json"
    read -r TUID OPT <<<"$(python3.14 - "$ID" "$STAGE/feed.json" <<'PY'
import json, sys
pid, path = sys.argv[1], sys.argv[2]
item = next((i for i in json.load(open(path))["items"] if i["id"] == pid), None)
b = item and item["blocked"]
if item and item["reason"] == "blocked" and b and b.get("answerable") and b["tool"] == "Bash":
    labels = [o["label"] for o in b["questions"][0]["options"]]
    yes = next(i for i, l in enumerate(labels, 1) if l.strip() == "Yes")
    print(b["tool_use_id"], yes)
PY
)"
    [ -n "${TUID:-}" ] && break
    sleep 1
  done
  [ -n "${TUID:-}" ] || { echo "FAIL: feed did not report an answerable Bash block within 15s"; exit 1; }
  echo "→ answering option $OPT (Yes) via /api/answer …"
  RESP=$(api /api/answer -X POST -H 'Content-Type: application/json' \
    -d "{\"id\":\"$ID\",\"tool_use_id\":\"$TUID\",\"option\":$OPT}")
  echo "  $RESP"
  echo "$RESP" | grep -q '"ok": true' || { echo "FAIL: /api/answer not ok"; exit 1; }
  for _ in $(seq 1 20); do [ -f "$MARKER" ] && break; sleep 1; done
  [ -f "$MARKER" ] && echo "PASS — dialog answered end-to-end, command ran" || { echo "FAIL: dialog did not resolve"; exit 1; }
  exit 0
fi

# ------------------------------------------------------------------------------------------------
# manual mode: stage one puppet per dialog kind, then hand the operator a URL.
PUPPETS=(vp-bash vp-plan vp-ask vp-multi)
cleanup() {
  echo; echo "→ tearing down puppets + server…"
  for p in "${PUPPETS[@]}"; do tx kill "$p" >/dev/null 2>&1; done
  [ -n "${SERVER_PID:-}" ] && kill "$SERVER_PID" 2>/dev/null
  rm -rf "$STAGE"
}
trap cleanup EXIT INT TERM

start_server
echo "→ staging one puppet per dialog kind…"
spawn_puppet vp-bash  ""                       "Use the Bash tool to run exactly: touch marker-file.txt — run it immediately, no questions."
spawn_puppet vp-plan  "--permission-mode plan" "Plan adding a hello() to app.py in two sentences, then call ExitPlanMode immediately to request approval."
spawn_puppet vp-ask   ""                       "Use the AskUserQuestion tool to ask me ONE single-select question: deploy now or wait. Exactly two options."
spawn_puppet vp-multi ""                       "Use the AskUserQuestion tool with multiSelect true to ask which of alpha, beta, gamma to include. Three options."

echo "→ accepting trust prompts + waiting for dialogs to appear (up to ~90s)…"
for _ in $(seq 1 45); do
  accept_trust_for "${PUPPETS[@]}"
  up=0
  for p in "${PUPPETS[@]}"; do
    id=$(resolve_id "$p"); [ -z "$id" ] && continue
    tmux capture-pane -p -t "$id" 2>/dev/null | grep -qE '❯ +[0-9]+\.' && up=$((up+1))
  done
  [ "$up" -ge 4 ] && break
  sleep 2
done

URL="http://$HOST:$PORT/"
echo
echo "════════════════════════════════════════════════════════════════════════════"
echo "  READY — open the remote-control UI and validate:"
echo "    desktop browser:  ${URL}desktop"
echo "    phone face:       ${URL}mobile"
[ "$HOST" = 127.0.0.1 ] && echo "    (loopback only — for a real phone, re-run: $0 manual $PORT <tailscale-ip>  with TX_REMOTE_TOKEN set)"
echo
echo "  Checklist — open each conversation and confirm:"
echo "    • vp-bash   → amber 'approve Bash touch marker-file.txt' banner + tappable Yes / No."
echo "                  Tap Yes → resolves (marker-file.txt created); or tap No → session unlocks."
echo "    • vp-plan   → the plan question with tappable approve options."
echo "    • vp-ask    → 'deploy now or wait' rendered as two tappable options."
echo "    • vp-multi  → NON-answerable: shows the question but says answer it in the terminal"
echo "                  (multi-select can't be driven by a single tap)."
echo
echo "  Ctrl-C here when done — puppets and the server are torn down automatically."
echo "════════════════════════════════════════════════════════════════════════════"
echo
# Idle until the operator interrupts; the trap cleans everything up.
while true; do sleep 3; accept_trust_for "${PUPPETS[@]}"; done
