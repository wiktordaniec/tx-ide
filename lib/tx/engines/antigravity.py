from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import sqlite3
import uuid
from collections.abc import Iterator, Mapping
from pathlib import Path

from ..session import Engine, State
from ..storage import hooks_dir, tx_ide_home
from ..worktree import WorktreeError, WorktreeManager
from .engine_adapter import DEFAULT_EFFORT, EngineAdapter, EngineError, StateSource
from .registry import registry

AGY_BIN = "agy"
# The CLI self-updates in place (brew ledger goes stale), so behavior can shift under us — this is
# the version every recipe in this adapter (flag grammar, hooks.json schema, db fork surgery, brain
# transcript layout) was verified against. Re-verify the fork surgery before trusting it on a
# different version, then bump this.
AGY_VERIFIED_VERSION = "1.1.13"

# Antigravity has no separate effort flag for tx to drive: models are slugs with the effort baked in
# (`gemini-3.7-flash-high`), and `--effort` beside a suffixed slug is a hard client-side error — so
# tx renders effort into the slug and NEVER emits `--effort`. The catalog churns with releases
# (3.5→3.6→3.7 observed); update the pinned family on breakage like CODEX_MODEL.
AGY_MODEL_FAMILY = "gemini-3.7-flash"
EFFORT_SUFFIXES = {1: "low", 2: "medium", 3: "high"}
DISTILLER_MODEL = "gemini-3.7-flash-high"

SKIP_PERMISSIONS_FLAG = "--dangerously-skip-permissions"
ADD_DIR_FLAG = "--add-dir"
LOG_FILE_FLAG = "--log-file"
MODE_FLAG = "--mode"
PLAN_MODE = "plan"
# `-i` auto-submits in the TUI and leaves the session interactive; a bare positional prompt is
# SILENTLY DROPPED by agy, so every seed/prompt must ride this flag.
PROMPT_FLAG = "-i"

# Role priming channel: agy has no working additive system-prompt flag (`--agent` resolves before
# workspace-customization discovery and silently falls back), but a rules file in the workspace IS
# injected (`always_on`). The priming text lives in a content-addressed file under
# `$TX_IDE_HOME/priming/`, its path rides the session's launch env under this key — env is copied
# onto every derived chat op (fork / handover / rollover / resume), which is what carries the
# persona across ops — and `prepare_workspace` copies it into each worktree's `.agents/rules/`.
RULES_FILE_ENV = "TX_AGY_RULES_FILE"
RULES_FILE_NAME = "tx-role.md"
HOOKS_FILE_NAME = "hooks.json"
HOOKS_TEMPLATE_NAME = "hooks.json.template"
AGENTS_DIR_NAME = ".agents"

TRANSCRIPT_SUFFIX = ".jsonl"


# ----- agy home / path internals -------------------------------------------------------------


def agy_home() -> Path:
    """Antigravity's own state home. Unlike CODEX_HOME / CLAUDE_CONFIG_DIR there is no documented
    env override for the CLI's app-data dir, so the path is fixed."""
    return Path("~/.gemini/antigravity-cli").expanduser()


def conversation_db(chat_id: str) -> Path:
    return agy_home() / "conversations" / f"{chat_id}.db"


def brain_transcript(chat_id: str) -> Path:
    """The clean per-step JSONL agy writes alongside each conversation — cwd-independent, keyed by
    conversation id only (hook payloads point at the sibling `transcript_full.jsonl`; the clean one
    is the history source)."""
    return (
        agy_home() / "brain" / chat_id / ".system_generated" / "logs" / "transcript.jsonl"
    )


def settings_path() -> Path:
    return agy_home() / "settings.json"


def priming_file(role_priming: str) -> Path:
    """The content-addressed priming file backing `RULES_FILE_ENV` — same roles, same file."""
    digest = hashlib.sha256(role_priming.encode()).hexdigest()[:16]
    return tx_ide_home() / "priming" / f"agy-rules-{digest}.md"


def hooks_template_path() -> Path:
    return hooks_dir() / "antigravity" / HOOKS_TEMPLATE_NAME


def run_log_path(cwd: str) -> Path:
    """The per-worktree `--log-file` target. Outside the repository so a read-only worker's OS
    sandbox (which denies every repo checkout) never blocks agy's own logging."""
    return tx_ide_home() / "agy-logs" / f"{Path(cwd).name}.log"


# ----- chat-op command derivation (Go `flag` grammar) ----------------------------------------
# Go's stdlib flag package: single- and double-dash are interchangeable, `--flag=value` works
# everywhere, and BOOLEAN flags never consume the next token — worse, writing `--bool value`
# actively desyncs agy's parse, so the builders below only ever emit booleans bare.

# Booleans (never consume a following token). Kept in sync with `agy --help`.
_BARE_NAMES = frozenset({
    "dangerously-skip-permissions", "disable-slash-commands", "new-project", "sandbox",
    "continue", "c", "help", "h", "version",
})

# Identity — the op re-supplies its own: `--conversation <id>` selects the chat; `--continue`/`-c`
# is the cwd-scoped last-conversation shortcut (identity by another key).
_IDENTITY_VALUE_NAMES = frozenset({"conversation"})
_IDENTITY_BARE_NAMES = frozenset({"continue", "c"})

# The prompt riders: `-i`/`--prompt-interactive` (interactive) and `-p`/`--print`/`--prompt`
# (headless) carry the baked seed — dropped flag+value, the op re-seeds its own.
_PROMPT_VALUE_NAMES = frozenset({"i", "prompt-interactive", "p", "print", "prompt"})

# Workspace-bound, not persona: re-bound to the destination worktree by `prepare_workspace`, so a
# derived command never carries the SOURCE worktree's binding.
_CWD_BOUND_VALUE_NAMES = frozenset({"add-dir", "log-file"})

# tx's access controls, stripped before applying the destination session's own mode.
_ACCESS_BARE_NAMES = frozenset({"dangerously-skip-permissions", "sandbox"})
_ACCESS_VALUE_NAMES = frozenset({"mode"})

# Once shlex surfaces a shell-control token, the rest of a compound source `cmd` is shell wrapping,
# not agy argv, and is dropped (same truncation as the Claude/Codex strips).
_SHELL_CONTROL_TOKENS = frozenset({";", "&", "&&", "||", "|", "|&", "&>", "&>>", "(", ")", "{", "}"})


def _is_shell_control(token: str) -> bool:
    return token in _SHELL_CONTROL_TOKENS or token[:1] in ("<", ">")


def _flag_parts(token: str) -> tuple[str | None, str | None]:
    """`(name, inline_value)` for a flag token — dashes normalized away, `--flag=value` split —
    or `(None, None)` for a positional."""
    if not token.startswith("-") or token.strip("-") == "":
        return None, None
    body = token.lstrip("-")
    if "=" in body:
        name, _, inline_value = body.partition("=")
        return name, inline_value
    return body, None


def _takes_next_token(tokens: list[str], index: int) -> bool:
    return (
        index + 1 < len(tokens)
        and not tokens[index + 1].startswith("-")
        and not _is_shell_control(tokens[index + 1])
    )


def _strip_identity(source_cmd: str) -> tuple[str, list[str]]:
    """Split a source agy `cmd` into (binary, inherited-flags): drop the identity flags, the baked
    prompt rider, the workspace binding, and any positional (agy silently drops positionals — they
    are never meaningful); keep the persona (`--model`, any unknown flag, which is value-by-default
    so its value is never mistaken for a prompt)."""
    tokens = shlex.split(source_cmd)
    binary = tokens[0] if tokens else AGY_BIN
    inherited: list[str] = []
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if _is_shell_control(token):
            break  # shell wrapping begins here — drop it and everything after
        name, inline_value = _flag_parts(token)
        if name is None:
            index += 1  # positional — agy drops it, so the strip does too
            continue
        if name in _IDENTITY_BARE_NAMES:
            index += 1
            continue
        if name in _IDENTITY_VALUE_NAMES | _PROMPT_VALUE_NAMES | _CWD_BOUND_VALUE_NAMES:
            index += 1 if inline_value is not None else 2
            continue
        if name in _BARE_NAMES:
            inherited.append(token)
            index += 1
            continue
        # Unknown flag: value-by-default (Go flag strings) — inherit it WITH its value.
        if inline_value is not None:
            inherited.append(token)
            index += 1
        elif _takes_next_token(tokens, index):
            inherited.extend(tokens[index : index + 2])
            index += 2
        else:
            inherited.append(token)  # dangling flag (end of argv / next token is a flag)
            index += 1
    return binary, inherited


def _drop_flags(
    tokens: list[str], bare_names: frozenset[str], value_names: frozenset[str]
) -> list[str]:
    """Remove the named flags (with their values) from an argv, keeping everything else in place.
    A prompt flag's value is kept atomically so a seed that textually resembles a flag never gets
    classified as one."""
    kept: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        name, inline_value = _flag_parts(token)
        if name in _PROMPT_VALUE_NAMES and inline_value is None:
            kept.extend(tokens[index : index + 2])
            index += 2
            continue
        if name is not None and name in bare_names:
            index += 1
            continue
        if name is not None and name in value_names:
            index += 1 if inline_value is not None else 2
            continue
        kept.append(token)
        index += 1
    return kept


def _apply_access(command: list[str], read_only: bool) -> list[str]:
    """Strip tx's access controls, then apply the destination mode. Writable = skip-permissions
    (the TUI must never stall a worker on a prompt). Read-only = `--mode plan` (a verified
    independent write-block) with NO skip-permissions and NO `--sandbox` — agy's own Seatbelt
    nested inside tx's outer sandbox-exec breaks every command with exit 71, and tx's fail-closed
    outer sandbox is the real boundary."""
    command = _drop_flags(command, _ACCESS_BARE_NAMES, _ACCESS_VALUE_NAMES)
    if read_only:
        command += [MODE_FLAG, PLAN_MODE]
    else:
        command.append(SKIP_PERMISSIONS_FLAG)
    return command


# ----- fork surgery (version-fragile, no CLI fork verb) --------------------------------------


def _fork_conversation_db(source_id: str) -> str:
    """Copy the source conversation db under a fresh id and rewrite every embedded occurrence of
    the source uuid — agy embeds it as `cascade_id` throughout the step/metadata blobs, and a raw
    copy alone fails with `trajectory not found`. The uuid strings are equal length, so the
    byte-replace never shifts protobuf length prefixes (no varint fixups). Returns the new id.

    This is an UNSUPPORTED on-disk surgery (no CLI fork verb; `/fork` is interactive-only) —
    version-fragile by nature, verified against `AGY_VERIFIED_VERSION`."""
    source_db = conversation_db(source_id)
    if not source_db.is_file():
        raise EngineError(f"antigravity fork: conversation db not found: {source_db}")
    new_id = str(uuid.uuid4())  # agy ids are lowercase uuids; uuid4() already renders lowercase
    new_db = conversation_db(new_id)
    try:
        # Fold the WAL into the main db file so the copy sees every committed page.
        checkpoint = sqlite3.connect(source_db)
        try:
            checkpoint.execute("PRAGMA wal_checkpoint(FULL)")
        finally:
            checkpoint.close()
        shutil.copy2(source_db, new_db)
        _rewrite_embedded_id(new_db, source_id, new_id)
    except sqlite3.Error as error:
        new_db.unlink(missing_ok=True)
        raise EngineError(
            f"antigravity fork surgery failed (version-fragile — verified against agy "
            f"{AGY_VERIFIED_VERSION}): {error}"
        ) from error
    return new_id


def _rewrite_embedded_id(db_path: Path, old_id: str, new_id: str) -> None:
    """Replace every occurrence of `old_id` with `new_id` across all TEXT and BLOB values of every
    table, via row-level UPDATEs (so indexes stay consistent — a raw file-level byte patch would
    desync the b-trees). Same-length replace by construction."""
    old_text, new_text = old_id, new_id
    old_bytes, new_bytes = old_id.encode(), new_id.encode()
    connection = sqlite3.connect(db_path)
    try:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        for table in tables:
            columns = [
                row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')
            ]
            rows = connection.execute(f'SELECT rowid, * FROM "{table}"').fetchall()
            for row in rows:
                rowid, values = row[0], row[1:]
                updates: dict[str, object] = {}
                for column, value in zip(columns, values):
                    if isinstance(value, str) and old_text in value:
                        updates[column] = value.replace(old_text, new_text)
                    elif isinstance(value, bytes) and old_bytes in value:
                        updates[column] = value.replace(old_bytes, new_bytes)
                if updates:
                    set_clause = ", ".join(f'"{column}" = ?' for column in updates)
                    connection.execute(
                        f'UPDATE "{table}" SET {set_clause} WHERE rowid = ?',
                        [*updates.values(), rowid],
                    )
        connection.commit()
    finally:
        connection.close()


# ----- workspace preparation (the cwd-bound half of a launch) --------------------------------


def _write_workspace_customizations(cwd: str, env: Mapping[str, str]) -> None:
    """Write the per-worktree `.agents/` files agy discovers at startup: the tx hooks.json (from
    the installed template — state tracking and chat-id capture are hook-driven, so this file is
    mandatory) and, when the session carries a priming pointer, the role rules file."""
    agents_directory = Path(cwd) / AGENTS_DIR_NAME
    agents_directory.mkdir(exist_ok=True)
    template = hooks_template_path()
    if not template.is_file():
        raise EngineError(
            f"antigravity hooks template missing at {template} — run "
            "`setup/engines/antigravity.sh install` (tx state tracking needs per-worktree hooks)"
        )
    shutil.copy2(template, agents_directory / HOOKS_FILE_NAME)
    rules_source = env.get(RULES_FILE_ENV, "")
    if rules_source:
        if not Path(rules_source).is_file():
            raise EngineError(
                f"antigravity role priming file missing at {rules_source} "
                f"(recorded in ${RULES_FILE_ENV}) — it is content-addressed under "
                "$TX_IDE_HOME/priming/ and should never be removed"
            )
        rules_directory = agents_directory / "rules"
        rules_directory.mkdir(exist_ok=True)
        shutil.copy2(rules_source, rules_directory / RULES_FILE_NAME)


def _exclude_agents_dir(cwd: str) -> None:
    """Hide the generated `.agents/` from git status via the repository's shared info/exclude (a
    linked worktree has no per-worktree exclude — info/ lives in the common git dir). A non-git cwd
    (scratch dir) has nothing to hide."""
    try:
        common_directory = WorktreeManager().git_common_directory(cwd)
    except WorktreeError:
        return
    exclude = common_directory / "info" / "exclude"
    line = f"{AGENTS_DIR_NAME}/"
    existing = exclude.read_text() if exclude.is_file() else ""
    if line in existing.splitlines():
        return
    exclude.parent.mkdir(exist_ok=True)
    separator = "" if existing.endswith("\n") or not existing else "\n"
    exclude.write_text(f"{existing}{separator}{line}\n")


def _seed_workspace_trust(cwd: str) -> None:
    """Pre-trust the worktree in agy's settings.json (`trustedWorkspaces`) so the first TUI launch
    never stalls on the "Do you trust this folder?" prompt. settings.json is agy's own file: an
    unparseable one is left alone (the TUI prompt remains as the fallback), and the write is an
    atomic replace so a concurrent agy rewrite never sees a torn file."""
    settings = settings_path()
    try:
        data = json.loads(settings.read_text()) if settings.is_file() else {}
    except ValueError:
        return
    trusted = data.setdefault("trustedWorkspaces", [])
    # agy stores resolved paths; add the literal spelling too when it differs so either compare hits.
    additions = [
        path for path in {cwd, os.path.realpath(cwd)} if path not in trusted
    ]
    if not additions:
        return
    trusted.extend(sorted(additions))
    settings.parent.mkdir(parents=True, exist_ok=True)
    temporary = settings.parent / f".tx-trust-{os.getpid()}.tmp"
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(temporary, settings)


# ----- the adapter ---------------------------------------------------------------------------

# Hook-event-name → live state. `SessionStart` fires (undocumented but verified) and is wired to
# the capture-only shim, so it is absent here — a bare `--conversation` resume must rest WAITING,
# not wedge WORKING. `PreInvocation` covers the missing UserPromptSubmit (it fires before every
# model call, reaffirming WORKING mid-turn); `Stop` carries the turn end. No permission event and
# no session-end event (EXITED comes from the tmux-close reconcile, the Codex precedent).
_EVENT_TO_STATE: Mapping[str, State] = {
    "PreInvocation": State.WORKING,
    "PreToolUse": State.WORKING,
    "PostToolUse": State.WORKING,
    "PostInvocation": State.WORKING,
    "Stop": State.WAITING,
}


class AntigravityEngine(EngineAdapter):
    """The Google Antigravity CLI (`agy`) adapter — NOT gemini-cli (the reserved `GEMINI` slot).
    Stateless; a single instance is registered for `Engine.ANTIGRAVITY`."""

    # ----- identity ------------------------------------------------------------------------

    @property
    def binary(self) -> str:
        return AGY_BIN

    def matches_binary(self, command: str) -> bool:
        tokens = command.split()
        return bool(tokens) and os.path.basename(tokens[0]) == AGY_BIN

    def capture_session_id(self, hook_payload: dict) -> tuple[str, str]:
        """Every hook payload carries these two, camelCase (protojson) — there is no snake_case
        session_id and no top-level cwd. The captured transcriptPath points at agy's
        `transcript_full.jsonl`; history resolution uses the clean sibling via `brain_transcript`."""
        return hook_payload["conversationId"], hook_payload["transcriptPath"]

    # ----- launch / ops --------------------------------------------------------------------

    def build_launch_command(
        self,
        *,
        model: str | None = None,
        effort: int | None = None,
        initial_prompt: str | None = None,
        read_only: bool = False,
        role_priming: str | None = None,
        env: dict[str, str] | None = None,
    ) -> list[str]:
        """Argv for a fresh session. Effort is rendered into the model slug (1→low, 2→medium,
        3→high on the pinned flash family); an explicit `model` wins verbatim — no suffixing, and
        effort is not applicable to it. The workspace binding (`--add-dir`/`--log-file`) and the
        `.agents/` files are NOT emitted here — the final worktree does not exist yet;
        `prepare_workspace` binds them. `role_priming` is written content-addressed and pointed at
        from `env` (see RULES_FILE_ENV)."""
        if model is None:
            selected_effort = effort if effort is not None else DEFAULT_EFFORT
            if selected_effort not in EFFORT_SUFFIXES:
                raise EngineError(
                    f"antigravity supports efforts 1-3 (rendered into the "
                    f"{AGY_MODEL_FAMILY}-low/medium/high slugs); effort {selected_effort} has no "
                    "slug — pass --model for a pro/opus tier instead"
                )
            model = f"{AGY_MODEL_FAMILY}-{EFFORT_SUFFIXES[selected_effort]}"
        command = [AGY_BIN, "--model", model]
        if role_priming:
            rules_file = priming_file(role_priming)
            if not rules_file.is_file():
                rules_file.parent.mkdir(parents=True, exist_ok=True)
                rules_file.write_text(role_priming)
            env[RULES_FILE_ENV] = str(rules_file)
        command = _apply_access(command, read_only)
        if initial_prompt:
            command += [PROMPT_FLAG, initial_prompt]
        return command

    def resume_command(
        self, chat_id: str, *, read_only: bool = False, source_cmd: str | None = None
    ) -> list[str]:
        """`--conversation <id>` — verified fully cwd-agnostic with complete history. Bare (no
        `-i`): the resumed TUI waits for input on the reattached history."""
        binary, inherited = (
            _strip_identity(source_cmd) if source_cmd is not None else (AGY_BIN, [])
        )
        return _apply_access([binary, "--conversation", chat_id, *inherited], read_only)

    def fork_command(
        self, source_cmd: str, chat_id: str, *, read_only: bool = False
    ) -> list[str]:
        """Fork via db surgery (agy has no CLI fork verb): copy + uuid-rewrite mints the new
        conversation here, synchronously — the I/O in the adapter follows the
        `prepare_chat_for_cwd` precedent — and the returned command resumes it. The capture hook
        confirms the id from the fork's first payload."""
        binary, inherited = _strip_identity(source_cmd)
        new_id = _fork_conversation_db(chat_id)
        return _apply_access([binary, "--conversation", new_id, *inherited], read_only)

    def seed_command(
        self, source_cmd: str, seed: str, *, read_only: bool = False
    ) -> list[str]:
        """A fresh session carrying the source persona, seeded via `-i` (a bare positional is
        silently dropped by agy — never seed positionally)."""
        binary, inherited = _strip_identity(source_cmd)
        command = _apply_access([binary, *inherited], read_only)
        command += [PROMPT_FLAG, seed]
        return command

    def distiller_command(self, seed: str) -> list[str]:
        """The throwaway distiller: a FIXED command (no source persona) on the flash-high slug —
        the quality hinge of the distillation — seeded via `-i`."""
        return [AGY_BIN, "--model", DISTILLER_MODEL, SKIP_PERMISSIONS_FLAG, PROMPT_FLAG, seed]

    def prepare_workspace(
        self, command: str, cwd: str, env: Mapping[str, str]
    ) -> str:
        """Bind a launch to its final worktree: re-point `--add-dir` + `--log-file` (workspace
        resolution cross-talk between concurrent agy processes was observed without an explicit
        binding), write the `.agents/` customizations (tx hooks + role rules), hide them from git,
        and pre-trust the directory. Idempotent — resume re-runs it on a prepared worktree."""
        tokens = shlex.split(command)
        if not tokens:
            return command
        tokens = _drop_flags(tokens, frozenset(), _CWD_BOUND_VALUE_NAMES)
        log_file = run_log_path(cwd)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        tokens[1:1] = [ADD_DIR_FLAG, cwd, LOG_FILE_FLAG, str(log_file)]
        _write_workspace_customizations(cwd, env)
        _exclude_agents_dir(cwd)
        _seed_workspace_trust(cwd)
        return shlex.join(tokens)

    def prepare_chat_for_cwd(
        self, chat_id: str, source_cwd: str, target_cwd: str
    ) -> None:
        """Conversations and brain transcripts are global by id, not cwd-keyed — nothing to move.
        The per-worktree `.agents/` files are rebuilt by `prepare_workspace` on every spawn."""

    def is_read_only_command(self, command: str) -> bool:
        """The read-only shape: no skip-permissions, no `--sandbox` (nested-Seatbelt breakage),
        and `--mode plan` present."""
        tokens = shlex.split(command)
        mode = None
        for index, token in enumerate(tokens):
            name, inline_value = _flag_parts(token)
            if name in _ACCESS_BARE_NAMES:
                return False
            if name == "mode":
                mode = inline_value if inline_value is not None else (
                    tokens[index + 1] if index + 1 < len(tokens) else None
                )
        return mode == PLAN_MODE

    # ----- transcript ----------------------------------------------------------------------

    def resolve_transcript(self, chat_id: str, cwd: str) -> Path:
        """The brain-dir JSONL — cwd-independent (`cwd` unused). May not exist yet; existence is
        the caller's check."""
        return brain_transcript(chat_id)

    def iter_messages(self, transcript: Path) -> Iterator[dict]:
        """Brain-JSONL steps → neutral dicts: USER_EXPLICIT → user, MODEL → assistant (planner
        text and tool steps alike carry plain-text `content`); SYSTEM bookkeeping (checkpoints,
        conversation-history splices) is skipped."""
        roles = {"USER_EXPLICIT": "user", "MODEL": "assistant"}
        with transcript.open() as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                step = json.loads(line)
                role = roles.get(step.get("source", ""))
                if role is None:
                    continue
                yield {"role": role, "text": step.get("content", "")}

    def bundle_sidecars(self, src_transcript: Path, chat_id: str) -> list[Path]:
        """No sidecar: the brain transcript inlines step content; the conversation `.db` is
        internal state, not history."""
        return []

    # ----- hooks / state -------------------------------------------------------------------

    @property
    def event_to_state(self) -> Mapping[str, State]:
        return _EVENT_TO_STATE

    @property
    def state_source(self) -> StateSource:
        """agy fires a `Stop` hook at turn end (print mode included), so turn-done arrives as a
        hook event — the unbuilt STATUS_POLL path stays unbuilt."""
        return StateSource.HOOK_EVENTS


registry.register(Engine.ANTIGRAVITY, AntigravityEngine())
