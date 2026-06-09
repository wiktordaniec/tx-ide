#!/bin/bash
input=$(cat)
cwd=$(echo "$input" | jq -r '.cwd // empty')
# Real context usage = current turn's input + cache creation + cache reads.
# .total_input_tokens only counts fresh non-cached input, which understates usage heavily.
tokens=$(echo "$input" | jq -r '
  (.context_window.current_usage.input_tokens // 0) +
  (.context_window.current_usage.cache_creation_input_tokens // 0) +
  (.context_window.current_usage.cache_read_input_tokens // 0)
')
rl_5h=$(echo "$input" | jq -r '.rate_limits.five_hour.used_percentage // empty')
rl_7d=$(echo "$input" | jq -r '.rate_limits.seven_day.used_percentage // empty')
rl_7d_resets_at=$(echo "$input" | jq -r '.rate_limits.seven_day.resets_at // empty')
rl_5h_resets_at=$(echo "$input" | jq -r '.rate_limits.five_hour.resets_at // empty')
model_full=$(echo "$input" | jq -r '.model.display_name // empty')
# Shorten "Claude 3.5 Sonnet" → "3.5 Sonnet", "Claude Sonnet 4.6" → "Sonnet 4.6", etc.
# Also strip any trailing parenthetical like " (1M context)".
model=$(echo "$model_full" | sed 's/^Claude //; s/ ([^)]*)//')

# Format tokens as "12.3K" or "1.2M"; returns empty for 0 or missing.
format_tokens() {
  local n="$1"
  if [ -z "$n" ] || [ "$n" = "null" ] || [ "$n" -eq 0 ] 2>/dev/null; then return; fi
  if [ "$n" -ge 1000000 ] 2>/dev/null; then
    awk -v n="$n" 'BEGIN { printf "%.1fM", n/1000000 }'
  elif [ "$n" -ge 1000 ] 2>/dev/null; then
    awk -v n="$n" 'BEGIN { printf "%.1fK", n/1000 }'
  else
    echo "$n"
  fi
}

branch=$(git -C "$cwd" symbolic-ref --short HEAD 2>/dev/null)

# Returns colored "label:N%" string; empty if pct is missing.
format_rate_limit() {
  local label="$1" pct="$2" resets_at="$3"
  if [ -z "$pct" ] || [ "$pct" = "null" ]; then return; fi
  pct=$(printf '%.0f' "$pct" 2>/dev/null)
  local suffix=""
  if [ -n "$resets_at" ] && [ "$resets_at" != "null" ]; then
    local now secs_left days_left hours_left
    now=$(date +%s)
    secs_left=$(( resets_at - now ))
    [ "$secs_left" -lt 0 ] && secs_left=0
    days_left=$(( secs_left / 86400 ))
    hours_left=$(( (secs_left % 86400) / 3600 ))
    suffix=" ${DIM}${days_left}d${hours_left}h↺${RESET}"
  fi
  printf "%b" "${DIM}${label}:\033[1m${pct}%${RESET}${suffix}"
}

# Push the Anthropic rate-limit snapshot to a running sessions-graph viewer (fire-and-forget). These
# percentages live ONLY here, on Claude Code's ephemeral statusline stdin — never written to disk —
# so the provider-agnostic viewer cannot read them itself; each render pokes it with the snapshot,
# exactly as bin/tx-graph-focus-poke pokes /api/focus-changed. The dashboard advertises its port in
# $TX_IDE_HOME/sessions-graph.port only while running, so an absent file means "nothing to poke" and
# we no-op without touching the network. Empty (no rate_limits this render) ⇒ nothing to send. The
# caller backgrounds this with its stdout closed, so it never delays the prompt or holds the
# statusline's output pipe open past the tight curl timeout.
push_anthropic_usage() {
  local port_file="${TX_IDE_HOME:-$HOME/.tx-ide}/sessions-graph.port"
  [ -r "$port_file" ] || return 0
  local port; port=$(<"$port_file")
  [ -n "$port" ] || return 0
  [ -n "$rl_5h$rl_7d" ] || return 0
  local body
  body=$(jq -nc \
    --argjson p5 "${rl_5h:-null}" --argjson r5 "${rl_5h_resets_at:-null}" \
    --argjson p7 "${rl_7d:-null}" --argjson r7 "${rl_7d_resets_at:-null}" \
    '{five_hour: {used_percentage: $p5, resets_at: $r5},
      seven_day: {used_percentage: $p7, resets_at: $r7}}')
  curl -s -m 0.3 -X POST "http://127.0.0.1:${port}/api/anthropic-usage" \
    -H 'content-type: application/json' -d "$body" >/dev/null 2>&1 || true
}

# Colors matching p10k theme. Model + tokens + rate limits use DIM so line 2 stays
# muted under the colorful line 1.
DIR_COLOR="\033[38;5;31m"
BRANCH_COLOR="\033[38;5;76m"
WORKTREE_COLOR="\033[38;5;173m"
DIM="\033[38;2;169;177;214m"            # dim_fg #a9b1d6 — keep in sync with DIM_FG_HEX in shared/palette.sh
RESET="\033[0m"

# --- Line 1: repo [worktree] branch ---
line1=""

if [ -n "$cwd" ]; then
  git_root=$(git -C "$cwd" rev-parse --show-toplevel 2>/dev/null)
  if [ -n "$git_root" ]; then
    common_dir=$(git -C "$cwd" rev-parse --git-common-dir 2>/dev/null)
    git_dir=$(git -C "$cwd" rev-parse --git-dir 2>/dev/null)
    if [ "$git_dir" != "$common_dir" ] 2>/dev/null; then
      # In a worktree: show main repo name + worktree name.
      # git worktree list first line is always the main repo, with absolute path.
      main_root=$(git -C "$cwd" worktree list 2>/dev/null | awk 'NR==1{print $1}')
      repo_name=$(basename "$main_root")
      worktree_name=$(basename "$git_root")
      line1="\033[1m${DIR_COLOR}${repo_name}${RESET} \033[1m${WORKTREE_COLOR}${worktree_name}${RESET}"
    else
      repo_name=$(basename "$git_root")
      line1="\033[1m${DIR_COLOR}${repo_name}${RESET}"
    fi
  else
    line1="\033[1m${DIR_COLOR}$(basename "$cwd")${RESET}"
  fi
fi

if [ -n "$branch" ]; then
  if [ -n "$line1" ]; then
    line1="${line1} ${BRANCH_COLOR}${branch}${RESET}"
  else
    line1="${BRANCH_COLOR}${branch}${RESET}"
  fi
fi

# --- Line 2: model tokens ---
line2=""

if [ -n "$model" ]; then
  line2="${DIM}${model}${RESET}"
fi

tok_fmt=$(format_tokens "$tokens")
if [ -n "$tok_fmt" ]; then
  if [ -n "$line2" ]; then
    line2="${line2} \033[1m${DIM}${tok_fmt}${RESET}"
  else
    line2="\033[1m${DIM}${tok_fmt}${RESET}"
  fi
fi

rl_5h_fmt=$(format_rate_limit "5h" "$rl_5h")
rl_7d_fmt=$(format_rate_limit "7d" "$rl_7d" "$rl_7d_resets_at")
for rl_fmt in "$rl_5h_fmt" "$rl_7d_fmt"; do
  if [ -n "$rl_fmt" ]; then
    if [ -n "$line2" ]; then
      line2="${line2} ${rl_fmt}"
    else
      line2="$rl_fmt"
    fi
  fi
done

# Always two lines when both are present.
if [ -n "$line1" ] && [ -n "$line2" ]; then
  printf "%b\n%b" "$line1" "$line2"
elif [ -n "$line1" ]; then
  printf "%b" "$line1"
elif [ -n "$line2" ]; then
  printf "%b" "$line2"
fi

# Poke the viewer after the line is printed (backgrounded, stdout closed) so the snapshot reaches the
# sessions-graph usage readout without ever delaying the prompt.
push_anthropic_usage >/dev/null 2>&1 &
