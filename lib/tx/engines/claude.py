from __future__ import annotations

import json
import os
import shlex
import shutil
from collections.abc import Iterator, Mapping
from pathlib import Path

from ..session import Engine, State
from ..storage import history_dir
from .engine_adapter import DEFAULT_EFFORT, EFFORT_LEVELS, EngineAdapter, StateSource
from .registry import registry

# Claude's own home (where it writes transcripts), honoring $CLAUDE_CONFIG_DIR like the CLI —
# NOT $TX_IDE_HOME.
CLAUDE_HOME_ENV = "CLAUDE_CONFIG_DIR"
DEFAULT_CLAUDE_HOME = "~/.claude"

CLAUDE_BIN = "claude"
TRANSCRIPT_SUFFIX = ".jsonl"
BUNDLE_TRANSCRIPT_NAME = "transcript.jsonl"
SKIP_PERMISSIONS_FLAG = "--dangerously-skip-permissions"
APPEND_SYSTEM_PROMPT_FLAG = "--append-system-prompt"
READ_ONLY_TOOLS = ("Edit", "Write", "NotebookEdit")
READ_ONLY_ALLOWED_TOOLS = ("Bash",)
READ_ONLY_SETTING_SOURCES = "user"


# ----- transcript / path internals ----------------------------------------------------------


def claude_home() -> Path:
    return Path(os.environ.get(CLAUDE_HOME_ENV, DEFAULT_CLAUDE_HOME)).expanduser()


def projects_root() -> Path:
    return claude_home() / "projects"


def munge(cwd: str) -> str:
    """Map an absolute path to Claude's project-dir name: every `/` and `.` becomes `-`."""
    return cwd.replace("/", "-").replace(".", "-")


def project_dir(cwd: str) -> Path:
    # claude derives this dir from the cwd's *realpath*, so resolve symlinks before munging —
    # a symlinked cwd (e.g. macOS /tmp → /private/tmp) would otherwise munge to a dir claude never
    # wrote, breaking the deterministic transcript path and fork-id capture.
    return projects_root() / munge(os.path.realpath(cwd))


def transcript_path(chat_id: str, cwd: str) -> Path:
    return project_dir(cwd) / f"{chat_id}{TRANSCRIPT_SUFFIX}"


def sidecar_dir(chat_id: str, cwd: str) -> Path:
    # The sibling <chat-uuid>/ dir holding subagents/ + tool-results/ — externalized parts a bare
    # .jsonl omits.
    return project_dir(cwd) / chat_id


def find_transcript(chat_id: str, cwd: str) -> Path | None:
    path = transcript_path(chat_id, cwd)
    return path if path.exists() else None


# ----- history bundle layout ----------------------------------------------------------------


def bundle_dir(tx_id: str, chat_id: str) -> Path:
    return history_dir() / tx_id / chat_id


def bundle_transcript_path(tx_id: str, chat_id: str) -> Path:
    return bundle_dir(tx_id, chat_id) / BUNDLE_TRANSCRIPT_NAME


# ----- chat-op command derivation (Claude's flag grammar) -----------------------------------
# Claude's flag grammar for reconstructing a fork/handover/rollover launch command from a source
# session's `cmd`; backs `ClaudeEngine.fork_command` / `seed_command`.

# Identity flags stripped when reconstructing a launch command: the new op re-supplies its own
# (`--resume … --fork-session` for fork, none for a fresh handover/rollover successor). Everything
# else (model / effort / --append-system-prompt / --settings / skip-permissions) is inherited.
# `--session-id` REQUIRES a value; `--resume`/`-r`/`--from-pr` take an OPTIONAL one (commander
# `[value]` — consumed only when the next token is not a flag), so they are stripped with the same
# rule. `--worktree`/`-w` (and `--tmux`, which requires it) place the SOURCE in a fresh worktree —
# workspace placement, not persona: the successor continues in the source's resolved cwd, so
# carrying them would relocate (or fail) the relaunch.
_IDENTITY_VALUE_FLAGS = frozenset({"--session-id"})
_IDENTITY_OPTIONAL_VALUE_FLAGS = frozenset(
    {"--resume", "-r", "--from-pr", "--worktree", "-w"}
)
_IDENTITY_BARE_FLAGS = frozenset({"--fork-session", "--continue", "-c", "--tmux"})

# Bare claude flags — those that do NOT consume a following token, so a positional that follows one
# (or stands alone) is the baked initial prompt and gets dropped. EVERY OTHER `--flag` is assumed to
# take a value, so an unknown value-flag keeps its value instead of being mistaken for the prompt —
# dropping it would leave a dangling flag that swallows the appended seed. A deny-list (not an
# allow-list of value-flags) is deliberate: `claude --help` documents some value-flags only in prose
# (e.g. `--append-system-prompt[-file]`), so an allow-list silently missed them. A MISSED bare flag
# here is NOT benign: it would glue the following token to itself as a bogus value — and in the
# standard worker shape (`claude <flags> "<priming>"`) that token is the baked priming, which then
# survives the strip and rides into the successor's command beside the new seed (AND-171). Kept in
# sync with `claude --help`.
_BARE_FLAGS = frozenset(
    {
        "--dangerously-skip-permissions",
        "--allow-dangerously-skip-permissions",
        "--verbose",
        "--print",
        "-p",
        "--ide",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--exclude-dynamic-system-prompt-sections",
        "--replay-user-messages",
        "--include-partial-messages",
        "--include-hook-events",
        "--disable-slash-commands",
        "--chrome",
        "--no-chrome",
        "--bare",
        "--brief",
        "--safe-mode",
        "--mcp-debug",
        "--help",
        "-h",
        "--version",
        "-v",
    }
)

# Persona flags with an OPTIONAL value (`-d [filter]`, `--prompt-suggestions [value]`,
# `--remote-control [name]`) need no listing: commander consumes the next token exactly when it is
# a non-flag — the same rule the unknown-value-flag default below applies — so they inherit
# correctly as-is.

# Persona flags that are VARIADIC (commander `<values...>`): claude consumes every following token
# up to the next flag as a value, so the strip mirrors that — inheriting them all keeps `--add-dir
# /a /b` intact instead of dropping `/b` as a stray positional.
_VARIADIC_VALUE_FLAGS = frozenset(
    {
        "--add-dir",
        "--allowedTools",
        "--allowed-tools",
        "--disallowedTools",
        "--disallowed-tools",
        "--mcp-config",
        "--betas",
        "--file",
        "--tools",
    }
)

# Shell-control tokens. Once shlex surfaces one of these, the rest of a compound source `cmd` is
# shell wrapping (separator / pipe / redirect / subshell / …), NOT claude argv. A fork/handover/
# rollover is a FRESH claude invocation, not the source's shell pipeline, so everything from the
# first such token on is dropped.
_SHELL_CONTROL_TOKENS = frozenset(
    {";", "&", "&&", "||", "|", "|&", "&>", "&>>", "(", ")", "{", "}"}
)


def _is_shell_control(token: str) -> bool:
    # An exact control token, or a redirection (starts with `<`/`>`) — enough to find the shell
    # boundary without a full shell parser.
    return token in _SHELL_CONTROL_TOKENS or token[:1] in ("<", ">")


def _takes_next_token(tokens: list[str], index: int) -> bool:
    """Whether the token after `tokens[index]` exists and would be consumed as a flag value —
    commander's rule for both optional (`[value]`) and unknown required values: a non-flag,
    non-shell-control token follows."""
    return (
        index + 1 < len(tokens)
        and not tokens[index + 1].startswith("-")
        and not _is_shell_control(tokens[index + 1])
    )


def _strip_identity(source_cmd: str) -> tuple[str, list[str]]:
    """Split a source `cmd` into (binary, inherited-flags), dropping the identity flags and any
    positional initial-prompt so the op can re-supply its own; the persona flags are inherited.
    The flag grammar mirrors claude's own (commander) parse — bare / optional-value / variadic /
    required-value — so every token lands on the same side of the flag/positional line that claude
    itself puts it on."""
    tokens = shlex.split(source_cmd)
    binary = tokens[0] if tokens else CLAUDE_BIN
    inherited: list[str] = []
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if _is_shell_control(token):
            break  # shell wrapping begins here — drop it and everything after
        if token in _IDENTITY_VALUE_FLAGS:
            index += 2  # drop the identity flag and its required value
            continue
        if token in _IDENTITY_OPTIONAL_VALUE_FLAGS:
            index += (
                2 if _takes_next_token(tokens, index) else 1
            )  # drop flag + optional value
            continue
        if token in _IDENTITY_BARE_FLAGS:
            index += 1  # drop — the op re-supplies its own
            continue
        if token in _BARE_FLAGS:
            inherited.append(
                token
            )  # bare flag; any positional that follows it is the prompt (dropped)
            index += 1
            continue
        if token in _VARIADIC_VALUE_FLAGS:
            inherited.append(
                token
            )  # variadic: claude eats every non-flag token that follows
            index += 1
            while (
                index < len(tokens)
                and not tokens[index].startswith("-")
                and not _is_shell_control(tokens[index])
            ):
                inherited.append(tokens[index])
                index += 1
            continue
        if token.startswith("-"):
            # Value-flag (known, unknown, or optional-value): inherit it WITH its value when one
            # follows; never drop the value — a dangling flag would swallow the appended seed.
            if _takes_next_token(tokens, index):
                inherited.extend(tokens[index : index + 2])
                index += 2
            else:
                inherited.append(
                    token
                )  # dangling flag (end of argv / next token is itself a flag)
                index += 1
            continue
        index += 1  # a positional — the source's baked initial prompt; drop it (the op seeds its own)
    return binary, inherited


def _ensure_skip_permissions(command: list[str]) -> list[str]:
    """Guarantee --dangerously-skip-permissions — else a forked/seeded session stalls on a
    permission prompt and its initial prompt never runs."""
    if SKIP_PERMISSIONS_FLAG not in command:
        command.append(SKIP_PERMISSIONS_FLAG)
    return command


def _strip_access_flags(command: list[str]) -> list[str]:
    """Drop tx's writable/read-only controls before applying the destination session's mode."""
    stripped: list[str] = []
    index = 0
    while index < len(command):
        token = command[index]
        if token == SKIP_PERMISSIONS_FLAG:
            index += 1
            continue
        if token == "--permission-mode":
            index += 2
            continue
        if (
            token == "--setting-sources"
            and index + 1 < len(command)
            and command[index + 1] == READ_ONLY_SETTING_SOURCES
        ):
            index += 2
            continue
        if token in (
            "--allowedTools",
            "--allowed-tools",
            "--disallowedTools",
            "--disallowed-tools",
        ):
            index += 1
            while index < len(command) and not command[index].startswith("-"):
                index += 1
            continue
        stripped.append(token)
        index += 1
    return stripped


def _apply_access(command: list[str], read_only: bool) -> list[str]:
    command = _strip_access_flags(command)
    if not read_only:
        return _ensure_skip_permissions(command)
    # Bash remains available for inspection inside tx's whole-process OS sandbox; direct editing
    # tools stay denied. Variadic lists must be followed by another flag, not a positional prompt.
    return [
        *command,
        "--allowedTools",
        *READ_ONLY_ALLOWED_TOOLS,
        "--disallowedTools",
        *READ_ONLY_TOOLS,
        "--permission-mode",
        "dontAsk",
        "--setting-sources",
        READ_ONLY_SETTING_SOURCES,
    ]


# ----- the adapter --------------------------------------------------------------------------
# Claude hook event name → live state. The whole "working" family reaffirms WORKING (a missed
# UserPromptSubmit self-heals on the first tool call). `Notification` is deliberately omitted: it is
# payload-dependent (only idle/permission/elicitation subtypes yield WAITING), not a static
# name→state edge.
_EVENT_TO_STATE: Mapping[str, State] = {
    "UserPromptSubmit": State.WORKING,
    "PreToolUse": State.WORKING,
    "PostToolUse": State.WORKING,
    "PostToolUseFailure": State.WORKING,
    "SubagentStart": State.WORKING,
    "PreCompact": State.WORKING,
    "Stop": State.WAITING,
    "StopFailure": State.WAITING,
    "PermissionRequest": State.WAITING,
    "SessionEnd": State.IDLE,
}


class ClaudeEngine(EngineAdapter):
    # Stateless adapter — a single instance is registered for Engine.CLAUDE. Launch/ops builders
    # return argv lists (binary first) with Claude's yolo flag baked in, so callers stay
    # engine-blind.

    # ----- identity ------------------------------------------------------------------------

    @property
    def binary(self) -> str:
        return CLAUDE_BIN

    def matches_binary(self, command: str) -> bool:
        # Basename match, so a full path (`/usr/local/bin/claude …`) still matches.
        tokens = command.split()
        return bool(tokens) and os.path.basename(tokens[0]) == CLAUDE_BIN

    def capture_session_id(self, hook_payload: dict) -> tuple[str, str]:
        # Both keys are present on every Claude hook event; captured on the first event, never minted.
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
        # A positional prompt auto-submits in interactive mode (measured). `env` is unused: claude
        # consults no engine home while building.
        command = [CLAUDE_BIN]
        if model:
            command += ["--model", model]
        selected_effort = effort if effort is not None else DEFAULT_EFFORT
        command += ["--effort", EFFORT_LEVELS[selected_effort]]
        if role_priming:
            # Additive by contract ("Append a system prompt to the default"); a persona
            # value-flag, so _strip_identity carries it into forks/handovers/rollovers.
            command += [APPEND_SYSTEM_PROMPT_FLAG, role_priming]
        command = _apply_access(command, read_only)
        if initial_prompt:
            command.append(initial_prompt)
        return command

    def resume_command(self, chat_id: str, *, read_only: bool = False) -> list[str]:
        return _apply_access([CLAUDE_BIN, "--resume", chat_id], read_only)

    def fork_command(
        self, source_cmd: str, chat_id: str, *, read_only: bool = False
    ) -> list[str]:
        """Branch a chat onto its full history, inheriting the source persona (incl. unknown
        value-flags) with the identity flags swapped for this fork's own. The fork mints its own id."""
        binary, inherited = _strip_identity(source_cmd)
        return _apply_access(
            [binary, "--resume", chat_id, "--fork-session", *inherited], read_only
        )

    def seed_command(
        self, source_cmd: str, seed: str, *, read_only: bool = False
    ) -> list[str]:
        """A fresh session inheriting the source persona (no identity flag) with `seed` as the
        initial-prompt positional. The positional auto-submits, so no send-keys."""
        binary, inherited = _strip_identity(source_cmd)
        command = _apply_access([binary, *inherited], read_only)
        command.append(seed)
        return command

    def distiller_command(self, seed: str) -> list[str]:
        # Fixed per-engine command (no source persona): claude → opus at medium effort, the quality
        # hinge of the distillation. Seed appended unconditionally (not via build_launch_command's
        # truthy filter).
        command = self.build_launch_command(model="opus", effort=2)
        command.append(seed)
        return command

    # ----- transcript ----------------------------------------------------------------------

    def resolve_transcript(self, chat_id: str, cwd: str) -> Path:
        return transcript_path(chat_id, cwd)

    def prepare_chat_for_cwd(
        self, chat_id: str, source_cwd: str, target_cwd: str
    ) -> None:
        """Copy Claude's cwd-keyed chat files so resume/fork can start in a new worktree."""
        if Path(source_cwd).resolve() == Path(target_cwd).resolve():
            return
        source_transcript = transcript_path(chat_id, source_cwd)
        if not source_transcript.is_file():
            return
        target_transcript = transcript_path(chat_id, target_cwd)
        target_transcript.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_transcript, target_transcript)

        source_sidecars = sidecar_dir(chat_id, source_cwd)
        if source_sidecars.is_dir():
            shutil.copytree(
                source_sidecars,
                sidecar_dir(chat_id, target_cwd),
                dirs_exist_ok=True,
            )

    def is_read_only_command(self, command: str) -> bool:
        tokens = shlex.split(command)
        if SKIP_PERMISSIONS_FLAG in tokens:
            return False
        try:
            permission_mode = tokens[tokens.index("--permission-mode") + 1]
            tools_index = tokens.index("--disallowedTools") + 1
            allowed_index = tokens.index("--allowedTools") + 1
            setting_sources = tokens[tokens.index("--setting-sources") + 1]
        except (ValueError, IndexError):
            return False
        denied: set[str] = set()
        while tools_index < len(tokens) and not tokens[tools_index].startswith("-"):
            denied.update(tokens[tools_index].split(","))
            tools_index += 1
        allowed: set[str] = set()
        while allowed_index < len(tokens) and not tokens[allowed_index].startswith("-"):
            allowed.update(tokens[allowed_index].split(","))
            allowed_index += 1
        return (
            permission_mode == "dontAsk"
            and set(READ_ONLY_TOOLS) <= denied
            and "Bash" not in denied
            and set(READ_ONLY_ALLOWED_TOOLS) <= allowed
            and setting_sources == READ_ONLY_SETTING_SOURCES
        )

    def iter_messages(self, transcript: Path) -> Iterator[dict]:
        # Claude's on-disk schema (Anthropic JSONL, one object per line) IS the engine-neutral form,
        # so this is a plain line-by-line decode with no translation (Codex's adapter maps Responses
        # items).
        with transcript.open() as handle:
            for line in handle:
                line = line.strip()
                if line:
                    yield json.loads(line)

    def bundle_sidecars(self, src_transcript: Path, chat_id: str) -> list[Path]:
        # Pure (no I/O): the sibling <chat-id>/ sidecar dir, taken relative to the *resolved*
        # transcript so a moved cwd still finds its colocated sidecar. history.py runs the copy.
        return [src_transcript.parent / chat_id]

    # ----- hooks / state -------------------------------------------------------------------

    @property
    def event_to_state(self) -> Mapping[str, State]:
        return _EVENT_TO_STATE

    @property
    def state_source(self) -> StateSource:
        """Claude emits a `Stop` hook at turn end, so its turn-done state arrives as a hook event."""
        return StateSource.HOOK_EVENTS


registry.register(Engine.CLAUDE, ClaudeEngine())
