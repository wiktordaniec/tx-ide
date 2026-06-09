#!/usr/bin/env bash
# dogfood-setup.sh — stand up the NEW (Python) tx-ide on a machine that still runs the OLD
# (bash + mailbox) tx-ide, so you can start dogfooding the redesign there.
#
#   ./dogfood-setup.sh [--dry-run] [--keep-records] [--branch NAME]
#
# WHY NOT `./flip`?  `flip` is THIS dev machine's coexistence cutover — its pre-flight asserts the
# settings.json marker is mode=coexist (the ~/.tx-ide-next dev-home state). The other machine never
# went through coexistence; its marker is the OLD v1 install. So `flip` would abort. This script is
# the right tool: a clean SUPERSEDE — pull the redesign, clear the old v1 leftovers, run the
# retargeted ./install (which match-by-marker-repoints the hooks, preserving peon-ping), verify.
#
# WHAT IT DOES (idempotent, safe to re-run; settings.json is backed up by ./install):
#   1. Pre-flight: confirm this is a tx-ide checkout + on the dogfood branch + clean-ish.
#   2. git pull the redesign branch (default feat/dogfood).
#   3. Stop a running OLD mx-speaker (the new system has no speaker) — best-effort.
#   4. Clear OLD state that the new code can't read:
#        - v1 session records in $TX_IDE_HOME/sessions/*.json (new schema is v2; no migration —
#          the new tx SKIPS v1 files but warns on every `ls`). Backed up to a tarball first.
#          (Skip this with --keep-records if you'd rather just live with the warnings.)
#        - the dark mailbox dir ~/.claude/mailbox (old inbox/speaker; expendable).
#   5. ./install  — the retargeted installer: symlinks tx onto PATH, builds $TX_IDE_HOME, registers
#      the new hooks via setup/engines/install.sh (match-by-marker → peon-ping/require-worktree kept),
#      moves the statusline (C10), marker mode=installed. install backs up settings.json itself.
#   6. Verify: tx resolves to the python shim, marker mode=installed, hooks under $TX_IDE_HOME,
#      peon-ping still present, `tx ls` runs clean (no v1 warnings).
#
# It does NOT touch ~/.claude/CLAUDE.md, your tmux.conf (install asks before replacing), your
# permissions/theme/plugins, or any settings.json entry the tx marker didn't add.
#
#   --dry-run       print every action, change nothing.
#   --keep-records  leave the old v1 session records in place (you'll see skip-warnings on `tx ls`).
#   --branch NAME   pull a branch other than feat/dogfood.
#
# ROLLBACK (if you want the old system back): this only ADDS the new code + repoints hooks via the
# marker, so `./uninstall` reverses the hook/statusline registration; restore the old records from
# the printed tarball; re-run the OLD checkout's installer to bring the mailbox back.
set -euo pipefail

# ── locate the repo from THIS script (so it works regardless of cwd) ──────────────────────────
REPO="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOME_DIR="${HOME%/}"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME_DIR/.claude}"
TX_HOME="${TX_IDE_HOME:-$HOME_DIR/.tx-ide}"
TX_HOME="${TX_HOME/#\~/$HOME_DIR}"
STAMP="$(date +%Y%m%d%H%M%S)"

DRY_RUN=0
KEEP_RECORDS=0
BRANCH="feat/dogfood"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)      DRY_RUN=1; shift ;;
    --keep-records) KEEP_RECORDS=1; shift ;;
    --branch)       BRANCH="${2:?--branch needs a NAME}"; shift 2 ;;
    --branch=*)     BRANCH="${1#*=}"; shift ;;
    -h|--help)      sed -n '2,40p' "$0"; exit 0 ;;
    *) printf 'dogfood-setup: unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done

B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; RED=$'\e[31m'; D=$'\e[2m'; X=$'\e[0m'
ok()    { printf '  %s✓%s %s\n' "$G" "$X" "$*"; }
warn()  { printf '  %s!%s %s\n' "$Y" "$X" "$*"; }
info()  { printf '  %s%s%s\n' "$D" "$*" "$X"; }
plan()  { printf '  %s· %s%s\n' "$D" "$*" "$X"; }
step()  { printf '\n%s%s%s\n' "$B" "$*" "$X"; }
die()   { printf '\n%sdogfood-setup: %s%s\n' "$RED" "$*" "$X" >&2; exit 1; }

run() {  # run a mutating command, or just print it under --dry-run
  if [[ $DRY_RUN -eq 1 ]]; then plan "would: $*"; else "$@"; fi
}

printf '%s== tx-ide dogfood setup ==%s  %s\n' "$B" "$X" "$( ((DRY_RUN)) && echo '(dry-run — nothing will change)' )"
info "repo:   $REPO"
info "home:   $TX_HOME"
info "branch: $BRANCH"

# ── STEP 1 — pre-flight ───────────────────────────────────────────────────────────────────────
step "1) Pre-flight"
[[ -f "$REPO/install" && -d "$REPO/lib/tx" ]] || die "this doesn't look like a tx-ide checkout ($REPO) — cd into the clone and re-run"
command -v git >/dev/null 2>&1 || die "git not found on PATH"
command -v python3.14 >/dev/null 2>&1 || warn "python3.14 not found yet — ./install will brew-install it"
git -C "$REPO" rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "$REPO is not a git work tree"
current_branch="$(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
ok "tx-ide checkout detected (on '$current_branch')"
if ! git -C "$REPO" diff --quiet 2>/dev/null || ! git -C "$REPO" diff --cached --quiet 2>/dev/null; then
  warn "working tree has uncommitted changes — the pull may complain; commit/stash if so"
fi

# ── STEP 2 — pull the redesign branch ─────────────────────────────────────────────────────────
step "2) Pull the redesign ($BRANCH)"
run git -C "$REPO" fetch origin "$BRANCH"
if [[ "$current_branch" != "$BRANCH" ]]; then
  plan "checkout $BRANCH"
  run git -C "$REPO" checkout "$BRANCH"
fi
run git -C "$REPO" pull --ff-only origin "$BRANCH"
if [[ $DRY_RUN -eq 0 ]]; then
  ok "on $(git -C "$REPO" rev-parse --abbrev-ref HEAD) @ $(git -C "$REPO" rev-parse --short HEAD)"
fi

# ── STEP 3 — stop the OLD mx-speaker (the new system has none) ─────────────────────────────────
step "3) Stop the old mailbox speaker (best-effort)"
pid_file="$CLAUDE_DIR/mailbox/mx-speaker.pid"
if [[ -f "$pid_file" ]]; then
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
    run kill "$pid"
    ok "mx-speaker stopped (pid $pid)"
  else
    info "mx-speaker not running (stale pid file)"
  fi
else
  info "no mx-speaker pid file (nothing to stop)"
fi

# ── STEP 4 — clear OLD v1 state the new code can't read ───────────────────────────────────────
step "4) Clear old v1 state"
records=()
if [[ -d "$TX_HOME/sessions" ]]; then
  shopt -s nullglob
  records=("$TX_HOME/sessions"/*.json)
  shopt -u nullglob
fi
if [[ $KEEP_RECORDS -eq 1 ]]; then
  info "--keep-records: leaving ${#records[@]} old record(s) (expect skip-warnings on \`tx ls\`)"
elif [[ ${#records[@]} -gt 0 ]]; then
  tarball="$HOME_DIR/.tx-ide.dogfoodbak.$STAMP.tgz"
  plan "back up $TX_HOME → $tarball, then rm ${#records[@]} v1 record(s) in $TX_HOME/sessions"
  if [[ $DRY_RUN -eq 0 ]]; then
    tar czf "$tarball" -C "$HOME_DIR" "$(basename "$TX_HOME")" 2>/dev/null && ok "backup: $tarball" || warn "backup tar reported an error (continuing)"
    rm -f "$TX_HOME/sessions"/*.json
    ok "cleared ${#records[@]} v1 record(s)"
  fi
else
  ok "no v1 records to clear"
fi
# the dark mailbox dir (old inbox/speaker; not used by the new system)
if [[ -e "$CLAUDE_DIR/mailbox" ]]; then
  run rm -rf "${CLAUDE_DIR:?}/mailbox"
  ok "removed old $CLAUDE_DIR/mailbox"
else
  info "no $CLAUDE_DIR/mailbox (already gone)"
fi
# old install symlinks the new installer doesn't manage (harmless if left, tidy to drop)
for squatter in "$CLAUDE_DIR/hooks/mailbox" "$CLAUDE_DIR/statusline.sh"; do
  if [[ -L "$squatter" ]]; then
    run rm -f "$squatter"
    ok "removed old symlink $squatter"
  fi
done

# ── STEP 5 — run the retargeted installer ─────────────────────────────────────────────────────
step "5) ./install (registers the new system)"
if [[ $DRY_RUN -eq 1 ]]; then
  plan "would run: $REPO/install"
  info "(install is interactive: it confirms before deps/tmux.conf changes and backs up settings.json)"
  info "(it match-by-marker-repoints the 4 hook events → preserves peon-ping / require-worktree)"
else
  info "running the installer — answer its prompts (it backs up everything it edits)…"
  "$REPO/install"
fi

# ── STEP 6 — verify ───────────────────────────────────────────────────────────────────────────
step "6) Verify"
if [[ $DRY_RUN -eq 1 ]]; then
  plan "after a real run, this checks: tx → python shim; marker mode=installed; hooks under $TX_HOME;"
  plan "peon-ping still present; \`tx ls\` runs without v1 skip-warnings"
  printf '\n%s%s%s\n' "$B" "=== dogfood-setup --dry-run complete (nothing changed) ===" "$X"
  exit 0
fi
tx_path="$(command -v tx 2>/dev/null || true)"
if [[ -n "$tx_path" ]] && grep -q -- "-m tx" "$(readlink -f "$tx_path" 2>/dev/null || echo "$tx_path")" 2>/dev/null; then
  ok "tx → new python shim ($tx_path)"
else
  warn "tx ($tx_path) is not the new python shim on PATH yet — open a new shell (the installer added ~/.local/bin to PATH)"
fi
SETTINGS_MAIN="$CLAUDE_DIR/settings.json" TX_HOME="$TX_HOME" python3.14 - <<'PY' || warn "settings.json verify reported an issue (see above)"
import json, os
real = os.path.realpath(os.environ["SETTINGS_MAIN"])
home = os.environ["TX_HOME"]
G, Y, X = "\033[32m", "\033[33m", "\033[0m"
def line(good, msg): print(f"  {(G+chr(10003)) if good else (Y+chr(33))}{X} {msg}")
with open(real) as fh:
    data = json.load(fh)
marker = data.get("_tx_ide_managed") or {}
line(marker.get("mode") == "installed", f"marker mode = {marker.get('mode')!r} (expect installed)")
hc = marker.get("hook_commands") or {}
line(all(home in v for v in hc.values()) and hc, f"hooks under {home}/hooks")
line("peon-ping" in json.dumps(data), "peon-ping still present in settings.json")
PY
# tx ls should be clean (no "skipping unreadable record" v1 warnings)
if command -v tx >/dev/null 2>&1; then
  warnings="$(tx ls 2>&1 1>/dev/null | grep -c "skipping unreadable" || true)"
  if [[ "${warnings:-0}" -gt 0 ]]; then
    warn "tx ls still prints $warnings v1 skip-warning(s) — re-run WITHOUT --keep-records to clear them"
  else
    ok "tx ls runs clean (no v1 record warnings)"
  fi
fi

cat <<EOF

${B}=== Done — the new tx-ide is live for dogfooding ===${X}

  ${D}open a new shell${X} (so ~/.local/bin is on PATH), then:
  ${D}tx start${X}        create the Views home-base + warm the tx-assistant
  ${D}tx${X}              pick a session (prefix+t inside tmux)
  ${D}tx spawn …${X}      spawn a worker and watch its state in \`tx ls\`

This checkout is on ${B}$BRANCH${X} — pull it again to get fixes while you dogfood.
EOF
