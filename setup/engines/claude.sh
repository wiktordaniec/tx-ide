#!/usr/bin/env bash
# setup/engines/claude.sh — the per-engine (Claude) integration for tx-ide (stage S2).
#
#   claude.sh {install|uninstall|status} [--dry-run] [--settings PATH]
#
# The Claude integration is decoupled from the tx core (the future-agent seam, §10/§15): this
# script owns the settings.json hook registration behind the `_tx_ide_managed` marker, so a second
# engine can ship its own setup/engines/<name>.sh without touching the core. install-flip.md §3/§4
# is canonical for the mechanism.
#
# The marker lives in a SIDECAR file, $TX_IDE_HOME/claude-managed.json — NOT inside settings.json.
# Claude Code ≥2.1.257 rejects a settings.json that carries hook-event-shaped keys anywhere outside
# the "hooks" block (the marker's hook_commands is exactly that), and a rejected file is skipped
# ENTIRELY, silently dropping model/permissions/context-profile keys. Legacy installs still have the
# marker embedded as settings.json's `_tx_ide_managed`; every read falls back to that shape, install
# migrates it out, and uninstall's verbatim `previous` restore keeps working for both.
#
#   install   — generate 6 C9-baked hook shims under $TX_IDE_HOME/hooks/claude/{start,pre,work,post,notify,end}.sh
#               and surgically REPOINT settings.json's tx hook events at them (match-by-marker, so
#               interleaved peon-ping / require-worktree / discord entries are preserved); install
#               the ONE global tmux session-closed → reconcile hook (C2); stop mx-speaker (the
#               mailbox goes dark); update the marker (mode=coexist, home, previous).
#   uninstall — reverse all of it via the marker: restore each event's prior command + the prior
#               marker EXACTLY, remove the generated shims + the global tmux hook. Leaves the tx
#               core (records / history / log / symlinks) untouched.
#   status    — report drift (what the marker claims vs what settings.json holds).
#
# COEXISTENCE (S2, install-flip §3): run with TX_IDE_HOME=~/.tx-ide-next so the dev home is baked
# into the shims (C9) and a `txn`-spawned session drives state, while the live ~/.tx-ide, mailbox,
# and statusLine stay untouched. Discrimination is internal (the hook no-ops on an id the dev home
# never recorded, D4) — there is no dispatch key. The statusLine move (C10) is Flip-only.
#
# SAFETY — verifiable without touching the live environment:
#   * --dry-run prints every action and changes nothing.
#   * --settings PATH operates on that file (a COPY) instead of the live settings.json AND treats
#     the run as a sandbox: the two LIVE-GLOBAL steps (the tmux global hook + the mx-speaker stop)
#     are printed, never executed, because they act on live globals a copy can't stand in for.
#   So `install --settings <copy>` fully exercises the shim generation + settings surgery on a
#   copy, and `install --dry-run` previews the live-global steps — neither mutates the daily
#   driver. The real coexistence activation (no --settings, not --dry-run) is a supervised step.
#
# settings.json is a symlink into the claude-server git repo; every edit targets its REALPATH and
# is atomic (temp + os.replace), so the dotfiles repo sees one clean, auditable diff.
set -euo pipefail

SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -P "$SCRIPT_DIR/../.." && pwd)"
LIB_DIR="$REPO_ROOT/lib"
PY="${TX_PYTHON:-python3.14}"

# C9: the home baked into the shims + recorded in the marker. Default ~/.tx-ide; the S2 dev home is
# ~/.tx-ide-next (passed via TX_IDE_HOME). Expand a leading ~ (env vars are not tilde-expanded).
TX_HOME="${TX_IDE_HOME:-$HOME/.tx-ide}"
TX_HOME="${TX_HOME/#\~/$HOME}"

CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
DEFAULT_SETTINGS="$CLAUDE_DIR/settings.json"
STAMP="$(date +%Y%m%d%H%M%S)"

B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; RED=$'\e[31m'; D=$'\e[2m'; X=$'\e[0m'
ok()     { printf '  %s→%s %-46s %s%s%s\n' "$G" "$X" "$1" "$G" "${2:-ok}" "$X"; }
warn()   { printf '  %s→%s %-46s %s%s%s\n' "$Y" "$X" "$1" "$Y" "${2:-}" "$X"; }
info()   { printf '  %s%s%s\n' "$D" "$*" "$X"; }
header() { printf '\n%s%s%s\n' "$B" "$*" "$X"; }

usage() {
  cat >&2 <<EOF
usage: claude.sh {install|uninstall|status} [--dry-run] [--settings PATH] [--no-context-profile]

  install     register tx-ide's Claude hooks (coexistence: TX_IDE_HOME=~/.tx-ide-next)
  uninstall   reverse the registration exactly via the _tx_ide_managed marker
  status      report drift between the marker and settings.json

  --dry-run              print every action, change nothing
  --settings PATH        operate on PATH (a copy) — sandbox: skip the live tmux hook + mx-speaker stop
  --no-context-profile   register hooks only; leave the context profile (below) untouched
EOF
}

# ----- argument parsing --------------------------------------------------------------------

OP=""; DRY_RUN=0; SETTINGS=""; APPLY_PROFILE=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    install|uninstall|status) OP="$1"; shift ;;
    --dry-run)        DRY_RUN=1; shift ;;
    --settings)       SETTINGS="${2:?--settings needs a PATH}"; shift 2 ;;
    --settings=*)     SETTINGS="${1#*=}"; shift ;;
    --no-context-profile) APPLY_PROFILE=0; shift ;;
    -h|--help)        usage; exit 0 ;;
    *) printf 'claude.sh: unknown argument: %s\n' "$1" >&2; usage; exit 2 ;;
  esac
done
[[ -n "$OP" ]] || { usage; exit 2; }

# --settings ⇒ sandbox: don't touch live globals (the tmux hook + mx-speaker stop).
SANDBOX=0
if [[ -n "$SETTINGS" ]]; then SANDBOX=1; else SETTINGS="$DEFAULT_SETTINGS"; fi

# The shims and the events they own. The state hooks span the full Claude set so a missed edge
# self-heals: a missed UserPromptSubmit recovers on the first tool call (work.sh), a missed Stop on
# the idle_prompt Notification (notify.sh). C6: Stop / StopFailure / PermissionRequest → post.sh.
#
# T4 (capture-after-launch): the session id is CAPTURED from the hook payload, never pre-minted, so
# the two shims that establish a chat — SessionStart (at startup) and UserPromptSubmit (first turn) —
# KEEP stdin (`keep-stdin`) so `tx hook` can read `session_id` + `transcript_path` off the payload.
# SessionStart closes the id-unknown window before the first turn. The rest still drain stdin (their
# state maps from the event name alone); notify keeps it to read notification_type.
START_SHIM="$TX_HOME/hooks/claude/start.sh"    # SessionStart                                      → session-start → chat capture
PRE_SHIM="$TX_HOME/hooks/claude/pre.sh"        # UserPromptSubmit                                  → prompt-submit → WORKING + capture
WORK_SHIM="$TX_HOME/hooks/claude/work.sh"      # PreToolUse/PostToolUse/…/SubagentStart/PreCompact → working      → WORKING
POST_SHIM="$TX_HOME/hooks/claude/post.sh"      # Stop / StopFailure / PermissionRequest            → stop         → WAITING (C6)
NOTIFY_SHIM="$TX_HOME/hooks/claude/notify.sh"  # Notification (reads notification_type)            → notification → WAITING if yield
END_SHIM="$TX_HOME/hooks/claude/end.sh"        # SessionEnd                                        → session-end  → IDLE (+ingest)

# The global tmux session-closed hook value (C2): baked like service.py's per-session hook —
# $TX_IDE_HOME + the package lib resolved literally, since tmux runs hooks with a minimal env.
TMUX_SESSION_CLOSED="run-shell -b \"env TX_IDE_HOME=$TX_HOME PYTHONPATH=$LIB_DIR $PY -m tx hook session-closed\""

# ----- shim generation (C9 bake) -----------------------------------------------------------

write_shim() {  # <path> <event> [keep-stdin]
  local path="$1" event="$2" keep_stdin="${3:-}"
  if [[ $DRY_RUN -eq 1 ]]; then
    info "would generate shim $path  ($event, home baked = $TX_HOME)"
    return 0
  fi
  mkdir -p "$(dirname "$path")"
  # Most shims drain the payload (state maps from the event name alone); the notify shim leaves it
  # on stdin so `tx hook notification` can read notification_type.
  local drain="cat >/dev/null"
  [[ -n "$keep_stdin" ]] && drain="# stdin left connected — tx hook reads the JSON payload"
  cat >"$path" <<EOF
#!/bin/bash
# tx-ide hook shim — GENERATED by setup/engines/claude.sh (stage S2). Do NOT edit; re-run the
# installer to regenerate. C9: \$TX_IDE_HOME and the package lib are baked in as literals because
# Claude runs hooks with a minimal env (no shell rc). Drain (or pass) the payload, then drive state.
$drain
exec env TX_IDE_HOME="$TX_HOME" PYTHONPATH="$LIB_DIR" "$PY" -m tx hook $event
EOF
  chmod +x "$path"
  ok "shim $path" "$event"
}

remove_shim() {  # <path>
  local path="$1"
  if [[ $DRY_RUN -eq 1 ]]; then info "would remove shim $path"; return 0; fi
  if [[ -f "$path" ]]; then rm -f "$path"; ok "removed $path"; else info "$path (already absent)"; fi
}

# ----- the context profile (settings keys tx owns alongside the hooks) ----------------------
# Session-start context that a tx worker never uses, measured on claude 2.1.232 by disabling one
# key at a time and reading the first turn's cache_creation+cache_read from the transcript
# (headless baseline 22,340 tok → 11,893):
#
#   disableWorkflows        6,068   Workflow: 0 invocations across 341 transcripts
#   disableArtifact        ~2,400   COMMON.md routes deliverables through `tx artifact`
#   skillOverrides          2,300   "user-invocable-only" keeps /name typable, hides the description
#   includeGitInstructions  1,891   the built-in commit-workflow block; DEVELOPER.md carries ours
#   permissions.deny        1,472   ReportFindings (0 uses) + ListAgents (tx uses `tx send-message`)
#
# Keys are dotted paths. Everything here is reversible: install records each key's prior value in
# the marker under `context_profile_previous`, and uninstall restores it verbatim. `--no-context-
# profile` skips the whole block, for a machine that wants tx's hooks but not tx's token opinions.
#
# Re-enable per session without touching this file — CLI --settings outranks the user layer:
#   claude --settings '{"disableWorkflows": false}'
# (`enableWorkflows: true` does NOT override, and neither does CLAUDE_CODE_DISABLE_WORKFLOWS=1 in
# the environment — the disable has to live in a settings layer.)
read -r -d '' CONTEXT_PROFILE <<'JSON' || true
{
  "disableWorkflows": true,
  "disableArtifact": true,
  "includeGitInstructions": false,
  "skillOverrides": {
    "artifact-design": "off",
    "artifact-diagramming": "off",
    "artifact-capabilities": "off",
    "claude-in-chrome": "off",
    "init": "off",
    "fewer-permission-prompts": "off",
    "code-review": "user-invocable-only",
    "security-review": "user-invocable-only",
    "simplify": "user-invocable-only",
    "dataviz": "user-invocable-only",
    "claude-api": "user-invocable-only",
    "update-config": "user-invocable-only",
    "keybindings-help": "user-invocable-only",
    "schedule": "user-invocable-only",
    "loop": "user-invocable-only",
    "run": "user-invocable-only"
  },
  "permissions.deny": ["ReportFindings", "ListAgents"]
}
JSON

# ----- settings.json surgery (match-by-marker, atomic, reversible) -------------------------

run_settings_py() {  # <install|uninstall|status>
  TX_OP="$1" TX_SETTINGS="$SETTINGS" TX_DRYRUN="$DRY_RUN" TX_SANDBOX="$SANDBOX" TX_HOME="$TX_HOME" \
  TX_START="$START_SHIM" TX_PRE="$PRE_SHIM" TX_WORK="$WORK_SHIM" TX_POST="$POST_SHIM" \
  TX_NOTIFY="$NOTIFY_SHIM" TX_END="$END_SHIM" \
  TX_TMUX_SESSION_CLOSED="$TMUX_SESSION_CLOSED" TX_STAMP="$STAMP" \
  TX_PROFILE="$CONTEXT_PROFILE" TX_APPLY_PROFILE="$APPLY_PROFILE" \
  "$PY" - <<'PY'
import json, os, sys, tempfile

op       = os.environ["TX_OP"]
path     = os.environ["TX_SETTINGS"]
dry_run  = os.environ["TX_DRYRUN"] == "1"
home     = os.environ["TX_HOME"]
stamp    = os.environ["TX_STAMP"]
profile  = json.loads(os.environ["TX_PROFILE"])
apply_profile = os.environ["TX_APPLY_PROFILE"] == "1"
G, Y, D, X = "\033[32m", "\033[33m", "\033[2m", "\033[0m"

# settings.json is a symlink into the claude-server git repo — read + write the REALPATH so the
# edit lands (atomically) in the dotfiles repo and is auditable via `git diff`.
real = os.path.realpath(path)

# The marker sidecar (see the header): read sidecar-first with a legacy in-settings fallback, write
# only the sidecar. A sandbox run (--settings COPY) keeps its sidecar beside the copy so exercising
# install/uninstall against a copy never touches the live marker.
if os.environ["TX_SANDBOX"] == "1":
    marker_path = f"{path}.tx-managed.json"
else:
    marker_path = os.path.join(home, "claude-managed.json")

def load_marker(data):
    if os.path.exists(marker_path):
        with open(marker_path) as fh:
            return json.load(fh)
    return data.get("_tx_ide_managed")

def write_marker(marker):
    directory = os.path.dirname(marker_path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tx-managed.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(marker, indent=2) + "\n")
        os.replace(tmp, marker_path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise

# Event → the shim command tx owns for it. The state hooks cover the full Claude set (collapsed to
# WORKING / WAITING / IDLE in hooks.py); Stop / StopFailure / PermissionRequest share post.sh (C6).
# SessionStart drives no state — it captures the chat id/path from the payload at startup (T4).
EVENTS = {
    "SessionStart":       os.environ["TX_START"],
    "UserPromptSubmit":   os.environ["TX_PRE"],
    "PreToolUse":         os.environ["TX_WORK"],
    "PostToolUse":        os.environ["TX_WORK"],
    "PostToolUseFailure": os.environ["TX_WORK"],
    "SubagentStart":      os.environ["TX_WORK"],
    "PreCompact":         os.environ["TX_WORK"],
    "Stop":               os.environ["TX_POST"],
    "StopFailure":        os.environ["TX_POST"],
    "PermissionRequest":  os.environ["TX_POST"],
    "Notification":       os.environ["TX_NOTIFY"],
    "SessionEnd":         os.environ["TX_END"],
}

def load():
    if not os.path.exists(real):
        return {}
    with open(real) as fh:
        return json.load(fh)

def write(data):
    """Atomic write: temp file in the same dir + os.replace, with a .bak.<stamp> alongside."""
    if os.path.exists(real):
        with open(f"{real}.bak.{stamp}", "w") as fh:
            json.dump(data_before, fh, indent=2); fh.write("\n")
    payload = json.dumps(data, indent=2) + "\n"
    directory = os.path.dirname(real) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tx-settings.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(payload)
        os.replace(tmp, real)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise

def entries_for(data, event, command):
    """Every hook entry under `event` whose command == `command` — the match-by-marker join. We
    only ever touch these exact entries, so interleaved peon-ping / require-worktree are preserved."""
    return [entry
            for block in data.get("hooks", {}).get(event, [])
            for entry in block.get("hooks", [])
            if entry.get("command") == command]

def strip(data, event, command):
    """Remove the matching tx entry (used only when install ADDED it — no prior command to restore),
    dropping a block / event that becomes empty so the structure matches the pre-install shape."""
    hooks = data.get("hooks", {})
    kept_blocks = []
    for block in hooks.get(event, []):
        kept = [e for e in block.get("hooks", []) if e.get("command") != command]
        if kept:
            block["hooks"] = kept
            kept_blocks.append(block)
    if kept_blocks:
        hooks[event] = kept_blocks
    else:
        hooks.pop(event, None)
    if not hooks:
        data.pop("hooks", None)

# ----- the context profile (dotted-path keys, recorded for verbatim restore) -----------------

ABSENT = object()

def path_get(data, key):
    node = data
    for part in key.split("."):
        if part not in node:
            return ABSENT
        node = node[part]
    return node

def path_set(data, key, value):
    parts = key.split(".")
    node = data
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value

def path_del(data, key):
    parts = key.split(".")
    node = data
    for part in parts[:-1]:
        node = node[part]
    node.pop(parts[-1], None)

def install_profile(data, existing):
    """Apply each profile key, recording its prior value the FIRST time tx touches that key. A
    re-install keeps the existing record — re-recording would capture tx's own values and turn
    uninstall into a no-op. A key added to the profile later is recorded on the run that introduces it."""
    previous = dict((existing or {}).get("context_profile_previous") or {})
    plan = []
    for key, wanted in profile.items():
        current = path_get(data, key)
        if key not in previous:
            previous[key] = {"absent": True} if current is ABSENT else {"value": current}
        if current == wanted:
            plan.append((key, "already current"))
            continue
        path_set(data, key, wanted)
        plan.append((key, "set" if current is not ABSENT else "add"))
    return plan, previous

def uninstall_profile(data, marker):
    plan = []
    for key, record in (marker.get("context_profile_previous") or {}).items():
        if record.get("absent"):
            path_del(data, key)
            plan.append((key, "strip    (was absent before tx)"))
        else:
            path_set(data, key, record["value"])
            plan.append((key, f"restore  → {json.dumps(record['value'])[:40]}"))
    return plan

def do_install(data):
    existing = load_marker(data)
    prior_commands = (existing or {}).get("hook_commands", {})
    plan = []
    for event, new_command in EVENTS.items():
        target = prior_commands.get(event)         # the command tx currently owns for this event
        if target == new_command:
            plan.append((event, "already current")); continue
        matched = entries_for(data, event, target) if target is not None else []
        if matched:
            for entry in matched:                  # REPOINT only the recorded tx command
                entry["command"] = new_command
            plan.append((event, f"repoint  {target}  →  {new_command}"))
        elif entries_for(data, event, new_command):
            plan.append((event, "already current"))
        else:                                      # no prior tx entry → ADD one beside the rest
            block = {"matcher": "", "hooks": [
                {"type": "command", "command": new_command, "timeout": 10, "async": True}]}
            data.setdefault("hooks", {}).setdefault(event, []).append(block)
            plan.append((event, f"add      {new_command}"))

    # Preserve the ORIGINAL pre-tx marker across re-installs (never nest a shim marker inside
    # `previous`, or uninstall would restore to a shim instead of the mailbox). "Ours" = a marker
    # whose recorded commands point at this home's hooks/ shims; testing the path (not an exact
    # event-set match) means broadening the event set on re-install still keeps the original previous.
    existing_is_ours = bool(existing) and any(
        str(command).startswith(home + "/hooks/")
        for command in (existing.get("hook_commands") or {}).values()
    )
    previous = existing.get("previous") if existing_is_ours else existing

    if apply_profile:
        profile_plan, profile_previous = install_profile(data, existing)
        plan.extend(profile_plan)
    else:
        # Carry an existing record untouched, so a --no-context-profile re-install never strands
        # keys tx already owns: uninstall can still restore them.
        profile_previous = (existing or {}).get("context_profile_previous")
        plan.append(("context profile", "skipped (--no-context-profile)"))

    if "_tx_ide_managed" in data:   # legacy in-settings marker → absorbed above, migrate it out
        del data["_tx_ide_managed"]
        plan.append(("marker", "migrated out of settings.json → sidecar"))

    marker = {
        "version": 3,
        "mode": "coexist",
        "home": home,
        "hook_commands": dict(EVENTS),
        "context_profile": dict(profile) if apply_profile else (existing or {}).get("context_profile"),
        "context_profile_previous": profile_previous,  # per-key prior value — uninstall restores verbatim
        "statusLine_command": (existing or {}).get("statusLine_command"),  # carried; untouched (C10=Flip)
        "tmux_session_closed": os.environ["TX_TMUX_SESSION_CLOSED"],
        "previous": previous,   # the exact prior marker (or null) — uninstall restores it verbatim
    }
    return plan, marker

def do_uninstall(data):
    marker = load_marker(data)
    if not marker:
        return None
    current = marker.get("hook_commands", {})
    previous = marker.get("previous")
    prior_commands = (previous or {}).get("hook_commands", {})
    plan = []
    for event, command in current.items():
        matched = entries_for(data, event, command)
        if not matched:
            plan.append((event, "missing (drift) — skipped")); continue
        restore_to = prior_commands.get(event)
        if restore_to is not None:
            for entry in matched:
                entry["command"] = restore_to     # exact reversal of the repoint
            plan.append((event, f"restore  {command}  →  {restore_to}"))
        else:
            strip(data, event, command)           # install had ADDED it → remove it
            plan.append((event, f"strip    {command}"))
    plan.extend(uninstall_profile(data, marker))
    # The pre-tx `previous` marker lived in settings.json (the sidecar postdates it) — restore it
    # there verbatim; otherwise just clear any legacy in-settings marker. The sidecar itself is
    # removed after the settings write (see the main flow).
    if previous is not None:
        data["_tx_ide_managed"] = previous
    else:
        data.pop("_tx_ide_managed", None)
    return plan

def do_status(data):
    marker = load_marker(data)
    if not marker:
        print(f"  {Y}!{X} no _tx_ide_managed marker — Claude integration not installed")
        return
    print(f"  marker: version={marker.get('version')}  mode={marker.get('mode')}  home={marker.get('home')}")
    if os.path.exists(marker_path):
        print(f"    {'location':<18} {D}{marker_path}{X}")
    else:
        print(f"    {'location':<18} {Y}LEGACY — embedded in settings.json (Claude ≥2.1.257 rejects "
              f"the whole file; re-run install to migrate){X}")
    for event, command in marker.get("hook_commands", {}).items():
        flag = f"{G}in sync{X}" if entries_for(data, event, command) else f"{Y}DRIFT — not in settings{X}"
        print(f"    {event:<18} {flag}")
        print(f"    {'':<18} {D}{command}{X}")
    sc = marker.get("tmux_session_closed")
    if sc:
        print(f"    {'session-closed':<18} {D}(tmux global) {sc}{X}")
    owned = marker.get("context_profile")
    if not owned:
        print(f"    {'context profile':<18} {D}not managed{X}")
        return
    for key, wanted in owned.items():
        flag = f"{G}in sync{X}" if path_get(data, key) == wanted else f"{Y}DRIFT — settings differ{X}"
        print(f"    {key:<18} {flag}")

data = load()
data_before = json.loads(json.dumps(data))   # deep copy for the .bak (pre-edit snapshot)

if op == "status":
    do_status(data)
    raise SystemExit(0)

if op == "install":
    plan, new_marker = do_install(data)
else:
    plan = do_uninstall(data)
if plan is None:
    print(f"  {Y}!{X} no _tx_ide_managed marker — settings.json left alone")
    raise SystemExit(0)

for event, action in plan:
    print(f"  {G}→{X} {event:<18} {action}")
if dry_run:
    print(f"  {D}(dry-run — {os.path.basename(real)} and the marker sidecar unchanged){X}")
else:
    write(data)
    print(f"  {G}✓{X} {os.path.basename(real)} written  {D}({real}){X}")
    if os.path.exists(f"{real}.bak.{stamp}"):
        print(f"  {D}backup: {real}.bak.{stamp}{X}")
    if op == "install":
        write_marker(new_marker)
        print(f"  {G}✓{X} marker sidecar written  {D}({marker_path}){X}")
    elif os.path.exists(marker_path):
        os.remove(marker_path)
        print(f"  {G}✓{X} marker sidecar removed  {D}({marker_path}){X}")
PY
}

# ----- live-global steps (skipped under --dry-run / sandbox) --------------------------------

# Where a stashed FOREIGN global session-closed hook is parked so uninstall can restore it.
PREV_HOOK_FILE="$TX_HOME/hooks/.session-closed.prev"
# All live-tmux calls go through this so a test can aim them at an isolated socket (TX_TMUX_SOCKET).
tmux_cmd() { tmux ${TX_TMUX_SOCKET:+-L "$TX_TMUX_SOCKET"} "$@"; }

apply_tmux_hook() {  # set | unset
  local action="$1" shown prior
  if [[ "$action" == set ]]; then
    shown="tmux set-hook -g session-closed '$TMUX_SESSION_CLOSED'"
  else
    shown="tmux set-hook -gu session-closed"
  fi
  # Skip the LIVE socket in sandbox/dry-run, but honor an explicit isolated socket (TX_TMUX_SOCKET)
  # so the capture/restore path is testable without touching the live server.
  if [[ $DRY_RUN -eq 1 || ( $SANDBOX -eq 1 && -z "${TX_TMUX_SOCKET:-}" ) ]]; then
    info "live-only (skipped here): $shown"
    return 0
  fi
  if ! command -v tmux >/dev/null 2>&1 || ! tmux_cmd info >/dev/null 2>&1; then
    warn "tmux global session-closed" "no tmux server — will apply on next start"
    return 0
  fi
  if [[ "$action" == set ]]; then
    # Non-destructive: stash a FOREIGN existing hook (one that isn't ours) so uninstall restores it
    # rather than clobbering a user's hook. The live server IS a boundary — observed populated here.
    prior="$(tmux_cmd show-hooks -g session-closed 2>/dev/null | sed -E 's/^session-closed(\[[0-9]+\])? *//')"
    if [[ -n "$prior" && "$prior" != *"tx hook session-closed"* ]]; then
      mkdir -p "$(dirname "$PREV_HOOK_FILE")"
      printf '%s' "$prior" >"$PREV_HOOK_FILE"
      warn "tmux global session-closed" "stashed an existing hook (restored on uninstall)"
    fi
    tmux_cmd set-hook -g session-closed "$TMUX_SESSION_CLOSED" && ok "tmux global session-closed" "set (→ reconcile)"
  elif [[ -f "$PREV_HOOK_FILE" ]]; then
    tmux_cmd set-hook -g session-closed "$(cat "$PREV_HOOK_FILE")" && ok "tmux global session-closed" "restored prior hook"
    rm -f "$PREV_HOOK_FILE"
  else
    tmux_cmd set-hook -gu session-closed 2>/dev/null && ok "tmux global session-closed" "unset" || info "no global session-closed hook"
  fi
}

stop_speaker() {
  local pid_file="$CLAUDE_DIR/mailbox/mx-speaker.pid"
  if [[ $DRY_RUN -eq 1 || $SANDBOX -eq 1 ]]; then
    info "live-only (skipped here): stop mx-speaker via $pid_file (mailbox left on disk, dark)"
    return 0
  fi
  if [[ -f "$pid_file" ]]; then
    local pid; pid="$(cat "$pid_file" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null && ok "mx-speaker" "stopped (pid $pid)" || warn "mx-speaker" "could not stop pid $pid"
    else
      info "mx-speaker not running (stale pid file left in place)"
    fi
  else
    info "no mx-speaker pid file (already dark)"
  fi
  # L5: leave the mailbox files + pid on disk (dark, not deleted) — Flip removes them.
}

# ----- subcommands -------------------------------------------------------------------------

cmd_install() {
  printf '%s== claude.sh install ==%s  %s\n' "$B" "$X" "$( ((DRY_RUN)) && echo '(dry-run)'; ((SANDBOX)) && echo "(sandbox: $SETTINGS)")"
  header "Hook shims under $TX_HOME/hooks (C9-baked)"
  write_shim "$START_SHIM"  session-start keep-stdin
  write_shim "$PRE_SHIM"    prompt-submit keep-stdin
  write_shim "$WORK_SHIM"   working
  write_shim "$POST_SHIM"   stop
  write_shim "$NOTIFY_SHIM" notification keep-stdin
  write_shim "$END_SHIM"    session-end

  header "settings.json — repoint the tx hook events (match-by-marker)"
  run_settings_py install

  header "Global tmux session-closed → reconcile (C2)"
  apply_tmux_hook set

  header "mx-speaker (mailbox goes dark — files left on disk)"
  stop_speaker
}

cmd_uninstall() {
  printf '%s== claude.sh uninstall ==%s  %s\n' "$B" "$X" "$( ((DRY_RUN)) && echo '(dry-run)'; ((SANDBOX)) && echo "(sandbox: $SETTINGS)")"
  header "settings.json — restore via the marker"
  run_settings_py uninstall

  header "Remove generated hook shims"
  remove_shim "$START_SHIM"
  remove_shim "$PRE_SHIM"
  remove_shim "$WORK_SHIM"
  remove_shim "$POST_SHIM"
  remove_shim "$NOTIFY_SHIM"
  remove_shim "$END_SHIM"

  header "Remove global tmux session-closed hook"
  apply_tmux_hook unset
  info "mx-speaker is NOT restarted (manual rollback step — install-flip §7)"
}

cmd_status() {
  printf '%s== claude.sh status ==%s\n' "$B" "$X"
  header "settings.json marker vs reality  ($SETTINGS)"
  run_settings_py status
  header "Generated shims ($TX_HOME/hooks)"
  for shim in "$START_SHIM" "$PRE_SHIM" "$WORK_SHIM" "$POST_SHIM" "$NOTIFY_SHIM" "$END_SHIM"; do
    if [[ -f "$shim" ]]; then ok "$shim" "present"; else warn "$shim" "missing"; fi
  done
}

case "$OP" in
  install)   cmd_install ;;
  uninstall) cmd_uninstall ;;
  status)    cmd_status ;;
esac
