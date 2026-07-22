from __future__ import annotations

import os
import shlex
import tomllib
from collections.abc import Iterator, Mapping
from pathlib import Path

from ..session import Engine, State
from . import codex_rollout
from .engine_adapter import DEFAULT_EFFORT, EFFORT_LEVELS, EngineAdapter, StateSource
from .registry import registry

# Codex's own home (where it writes rollouts), honoring $CODEX_HOME like the CLI does. NOT $TX_IDE_HOME.
CODEX_HOME_ENV = "CODEX_HOME"
DEFAULT_CODEX_HOME = "~/.codex"

CODEX_BIN = "codex"

# Codex defaults. Effort is rendered as a `-c` config override, not a flag: `-c model_reasoning_effort=`.
CODEX_MODEL = "gpt-5.6-sol"
CODEX_EFFORT = DEFAULT_EFFORT
REASONING_EFFORT_KEY = "model_reasoning_effort"
# Additive instructions channel: appended to codex's base instructions, never replacing them
# (contrast experimental_instructions_file, which replaces). The multi-line contents fail `-c`'s
# TOML parse and fall back to the raw string literal, arriving intact as one argv token. A `-c`
# override DOES replace a `config.toml`-configured value of the same key, so the launch builder
# composes with the configured value instead of clobbering it.
DEVELOPER_INSTRUCTIONS_KEY = "developer_instructions"
CONFIG_FILE_NAME = "config.toml"

# Writable workers bypass approvals+sandbox AND hook trust. Read-only workers deliberately avoid a
# nested native sandbox and run inside tx's outer process sandbox; hook trust remains headless so tx
# hooks still run. Both `resume` and `fork` accept these flags.
BYPASS_APPROVALS_FLAG = "--dangerously-bypass-approvals-and-sandbox"
BYPASS_HOOK_TRUST_FLAG = "--dangerously-bypass-hook-trust"
YOLO_FLAGS = [BYPASS_APPROVALS_FLAG, BYPASS_HOOK_TRUST_FLAG]
READ_ONLY_FLAGS = [
    "--sandbox",
    "danger-full-access",
    "--ask-for-approval",
    "never",
    BYPASS_HOOK_TRUST_FLAG,
]

# A rollout is `sessions/<Y>/<M>/<D>/rollout-<ts>-<id>.jsonl`; the id is unique across the tree, so a
# recursive `**` glob resolves it regardless of date-dir nesting.
SESSIONS_DIR = "sessions"
ROLLOUT_PREFIX = "rollout-"
TRANSCRIPT_SUFFIX = ".jsonl"


# ----- transcript / path internals ----------------------------------------------------------

def codex_home(env: Mapping[str, str] | None = None) -> Path:
    """Codex's home as the SPAWNED process will resolve it: a launch-env override (`--env
    CODEX_HOME=…`) wins over this parent process's environment."""
    overridden = (env or {}).get(CODEX_HOME_ENV) or os.environ.get(CODEX_HOME_ENV)
    return Path(overridden or DEFAULT_CODEX_HOME).expanduser()


def sessions_root() -> Path:
    return codex_home() / SESSIONS_DIR


def configured_developer_instructions(env: Mapping[str, str] | None = None) -> str | None:
    """The user's own top-level `developer_instructions` from `$CODEX_HOME/config.toml`, or None.
    Role priming prepends this so the `-c` override composes with the configured value instead of
    silently replacing it. The file is an external input (system boundary): absent, unreadable, or
    invalid TOML degrades to None. Profile-scoped values are out of scope — codex's own precedence
    already lets a `-c` override win there."""
    try:
        with (codex_home(env) / CONFIG_FILE_NAME).open("rb") as handle:
            value = tomllib.load(handle).get(DEVELOPER_INSTRUCTIONS_KEY)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    return value if isinstance(value, str) and value.strip() else None


def find_rollout(chat_id: str) -> Path | None:
    """Glob the sessions tree for `rollout-*-<id>.jsonl` (most recent if several), or None if absent."""
    # The path is date+timestamp+id, not a munged cwd like Claude, so this glob is cwd-independent —
    # no separate moved-cwd resolver needed.
    pattern = f"**/{ROLLOUT_PREFIX}*-{chat_id}{TRANSCRIPT_SUFFIX}"
    matches = sorted(sessions_root().glob(pattern))
    return matches[-1] if matches else None


# ----- chat-op command derivation --------------------------

# Codex's identity is a positional SUBCOMMAND, not a `--flag` (contrast Claude's `--resume`): a
# forked/resumed source `cmd` begins `codex fork <id> …` / `codex resume <id> …`. The verb + its id
# positional are dropped — the op re-supplies its own. The subcommands' own session-picker flags
# (`resume --last` / `--all` / `--include-non-interactive`) are identity too: they select WHICH
# session to continue, and clap rejects them on a bare `codex`, so carrying one kills the relaunch.
_IDENTITY_SUBCOMMANDS = frozenset({"resume", "fork"})
_IDENTITY_BARE_FLAGS = frozenset({"--last", "--all", "--include-non-interactive"})

# BARE flags — those that do NOT consume a following token (kept in sync with `codex --help`): the
# bypass pair plus today's boolean flags. Every other `-flag` is value-by-default, so an unknown
# value-flag keeps its value instead of being mistaken for the prompt and dropped — a MISSED bare
# flag would glue the following token (the baked priming, in the standard worker shape) to itself
# and carry it into the successor's command (AND-171). Mind the `-c` collision: codex `-c` is a
# VALUE flag (`-c KEY=VALUE`), so it is NOT bare here (it IS for Claude, where `-c` == `--continue`).
_BARE_FLAGS = frozenset({
    *YOLO_FLAGS,
    "--oss", "--search", "--no-alt-screen", "--strict-config",
    "--help", "-h", "--version", "-V",
})

# VARIADIC persona flags (clap `<FILE>...`): codex consumes every following token up to the next
# flag as a value, so the strip mirrors that — `-i a.png b.png` keeps both images.
_VARIADIC_VALUE_FLAGS = frozenset({"-i", "--image"})

# Once shlex surfaces a shell-control token, the rest of a compound source `cmd` is shell wrapping,
# not codex argv, and is dropped.
_SHELL_CONTROL_TOKENS = frozenset({";", "&", "&&", "||", "|", "|&", "&>", "&>>", "(", ")", "{", "}"})


def _is_shell_control(token: str) -> bool:
    """A shell operator (exact control token) or a redirection (starts with `<`/`>`), not a flag/value."""
    return token in _SHELL_CONTROL_TOKENS or token[:1] in ("<", ">")


def _strip_identity(source_cmd: str) -> tuple[str, list[str]]:
    """Split a source codex `cmd` into (binary, inherited-flags), dropping the identity subcommand,
    its id, its session-picker flags (`--last` / `--all` / `--include-non-interactive`), and the baked
    positional prompt, keeping the persona (`-m`, `-c KEY=VALUE`, any unknown flag)."""
    tokens = shlex.split(source_cmd)
    binary = tokens[0] if tokens else CODEX_BIN
    index = 1
    # Drop a leading identity subcommand + its id positional.
    if index < len(tokens) and tokens[index] in _IDENTITY_SUBCOMMANDS:
        index += 1
        if index < len(tokens) and not tokens[index].startswith("-"):
            index += 1
    inherited: list[str] = []
    while index < len(tokens):
        token = tokens[index]
        if _is_shell_control(token):
            break  # shell wrapping begins here — drop it and everything after
        if token in _IDENTITY_BARE_FLAGS:
            index += 1  # a resume/fork session-picker flag — the op re-supplies its own identity
            continue
        if token in _BARE_FLAGS:
            inherited.append(token)  # any positional that follows a bare flag is the prompt (dropped)
            index += 1
            continue
        if token in _VARIADIC_VALUE_FLAGS:
            inherited.append(token)  # variadic: codex eats every non-flag token that follows
            index += 1
            while index < len(tokens) and not tokens[index].startswith("-") \
                    and not _is_shell_control(tokens[index]):
                inherited.append(tokens[index])
                index += 1
            continue
        if token.startswith("-"):
            # Value-flag: inherit it WITH its value when one follows; assuming an unknown flag takes a
            # value keeps an unrecognised `-c KEY=VALUE` rather than dropping its value as a stray prompt.
            if index + 1 < len(tokens) and not tokens[index + 1].startswith("-") \
                    and not _is_shell_control(tokens[index + 1]):
                inherited.extend(tokens[index:index + 2])
                index += 2
            else:
                inherited.append(token)  # dangling flag (end of argv / next token is itself a flag)
                index += 1
            continue
        index += 1  # a positional — the source's baked seed; drop it
    return binary, inherited


def _ensure_yolo(command: list[str]) -> list[str]:
    """Guarantee both bypass flags are present (inherited or appended), so a seed isn't stalled on an
    approval prompt and hooks still fire."""
    for flag in YOLO_FLAGS:
        if flag not in command:
            command.append(flag)
    return command


def _strip_access_flags(command: list[str]) -> list[str]:
    """Drop tx's writable/read-only controls before applying the destination session's mode."""
    stripped: list[str] = []
    index = 0
    value_flags = frozenset({"--sandbox", "-s", "--ask-for-approval", "-a"})
    while index < len(command):
        token = command[index]
        if token in YOLO_FLAGS:
            index += 1
            continue
        if token in value_flags:
            index += 2
            continue
        stripped.append(token)
        index += 1
    return stripped


def _apply_access(command: list[str], read_only: bool) -> list[str]:
    command = _strip_access_flags(command)
    return [*command, *READ_ONLY_FLAGS] if read_only else _ensure_yolo(command)


# ----- the adapter --------------------------

# Hook-event-name → State. Codex has NO session-end event: a finished turn rests in WAITING and EXITED
# comes from tmux-close→reconcile. `SessionStart` is capture-only (id+path, not a state edge), so it is
# absent here. No `*Failure` events.
_EVENT_TO_STATE: Mapping[str, State] = {
    "UserPromptSubmit": State.WORKING,
    "PreToolUse": State.WORKING,
    "PostToolUse": State.WORKING,
    "PreCompact": State.WORKING,
    "PostCompact": State.WORKING,
    "SubagentStart": State.WORKING,
    "Stop": State.WAITING,
    "PermissionRequest": State.WAITING,
}


class CodexEngine(EngineAdapter):
    """The OpenAI Codex adapter. Stateless; a single instance is registered for `Engine.CODEX`."""

    # ----- identity ------------------------------------------------------------------------

    @property
    def binary(self) -> str:
        return CODEX_BIN

    def matches_binary(self, command: str) -> bool:
        """Whether `command` invokes codex (by first-token basename, so a full path still matches)."""
        tokens = command.split()
        return bool(tokens) and os.path.basename(tokens[0]) == CODEX_BIN

    def capture_session_id(self, hook_payload: dict) -> tuple[str, str]:
        """`(session_id, transcript_path)` off a hook payload (both keys on every event) — captured, never minted."""
        return hook_payload["session_id"], hook_payload["transcript_path"]

    # ----- launch / ops --------------------------------------------------------------------

    def build_launch_command(
        self,
        *,
        model: str | None = None,
        effort: int | None = None,
        initial_prompt: str | None = None,
        read_only: bool = False,
        role_priming: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> list[str]:
        """Argv for a fresh session. A positional prompt auto-submits in the interactive TUI, so the
        seed needs no send-keys."""
        selected_effort = effort if effort is not None else CODEX_EFFORT
        command = [
            CODEX_BIN,
            "-m", model or CODEX_MODEL,
            "-c", f"{REASONING_EFFORT_KEY}={EFFORT_LEVELS[selected_effort]}",
        ]
        if role_priming:
            # A `-c KEY=VALUE` persona pair, so _strip_identity inherits it across chat ops. The
            # configured config.toml value rides in front — the override would replace it otherwise.
            configured = configured_developer_instructions(env)
            instructions = (
                f"{configured}\n\n{role_priming}" if configured else role_priming
            )
            command += ["-c", f"{DEVELOPER_INSTRUCTIONS_KEY}={instructions}"]
        command = _apply_access(command, read_only)
        if initial_prompt:
            command.append(initial_prompt)
        return command

    def resume_command(self, chat_id: str, *, read_only: bool = False) -> list[str]:
        """Resume a chat in place via Codex's native `resume` subcommand (bypass flags ride along so hooks fire)."""
        return _apply_access([CODEX_BIN, "resume", chat_id], read_only)

    def fork_command(
        self, source_cmd: str, chat_id: str, *, read_only: bool = False
    ) -> list[str]:
        """Codex's native `fork` subcommand, carrying the source persona. Mints a NEW id, captured post-hoc."""
        binary, inherited = _strip_identity(source_cmd)
        return _apply_access([binary, "fork", chat_id, *inherited], read_only)

    def seed_command(
        self, source_cmd: str, seed: str, *, read_only: bool = False
    ) -> list[str]:
        """A fresh `codex` carrying the source persona (no identity subcommand) + `seed` as its positional
        prompt (which auto-submits)."""
        binary, inherited = _strip_identity(source_cmd)
        command = _apply_access([binary, *inherited], read_only)
        command.append(seed)
        return command

    def distiller_command(self, seed: str) -> list[str]:
        """The throwaway distiller: a fresh codex at default model/effort with `seed` appended. A FIXED
        command — carries NO source persona. Appending directly lets it survive even an empty seed."""
        command = self.build_launch_command()
        command.append(seed)
        return command

    # ----- transcript ----------------------------------------------------------------------

    def resolve_transcript(self, chat_id: str, cwd: str) -> Path:
        """Locate a chat's rollout by id. `cwd` is unused — a rollout's path is date+timestamp+id, not
        cwd-derived. Returns a non-existent sentinel when none is on disk yet, so the caller's existence
        check fails cleanly."""
        found = find_rollout(chat_id)
        if found is not None:
            return found
        return sessions_root() / f"{ROLLOUT_PREFIX}{chat_id}{TRANSCRIPT_SUFFIX}"

    def prepare_chat_for_cwd(
        self, chat_id: str, source_cwd: str, target_cwd: str
    ) -> None:
        """Codex rollouts are global by id rather than keyed to cwd; no relocation is needed."""

    def is_read_only_command(self, command: str) -> bool:
        tokens = shlex.split(command)
        if BYPASS_APPROVALS_FLAG in tokens:
            return False
        try:
            sandbox = tokens[tokens.index("--sandbox") + 1]
            approval = tokens[tokens.index("--ask-for-approval") + 1]
        except (ValueError, IndexError):
            return False
        return (
            sandbox == "danger-full-access"
            and approval == "never"
            and BYPASS_APPROVALS_FLAG not in tokens
        )

    def iter_messages(self, transcript: Path) -> Iterator[dict]:
        """Yield each turn as an engine-neutral message dict, normalizing the OpenAI Responses-item
        schema. Carries `is_environment_context` so the messages layer can drop Codex's injected
        first-user `<environment_context>` turn."""
        for message in codex_rollout.iter_messages(transcript):
            yield {
                "role": message.role,
                "text": message.text,
                "is_environment_context": message.is_environment_context,
            }

    def bundle_sidecars(self, src_transcript: Path, chat_id: str) -> list[Path]:
        """No sidecar (pure, no I/O): the rollout inlines tool calls / reasoning, so there is no sibling
        dir to copy."""
        return []

    # ----- hooks / state -------------------------------------------------------------------

    @property
    def event_to_state(self) -> Mapping[str, State]:
        return _EVENT_TO_STATE

    @property
    def state_source(self) -> StateSource:
        """Codex emits a `Stop` hook at turn end, so turn-done arrives as a hook event (no statusline poll)."""
        return StateSource.HOOK_EVENTS


registry.register(Engine.CODEX, CodexEngine())
