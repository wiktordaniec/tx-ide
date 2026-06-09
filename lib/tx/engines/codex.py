from __future__ import annotations

import os
import shlex
from collections.abc import Iterator, Mapping
from pathlib import Path

from ..session import Engine, State
from . import codex_rollout
from .engine_adapter import EngineAdapter, StateSource
from .registry import registry

# Codex's own home (where it writes rollouts), honoring $CODEX_HOME like the CLI does. NOT $TX_IDE_HOME.
CODEX_HOME_ENV = "CODEX_HOME"
DEFAULT_CODEX_HOME = "~/.codex"

CODEX_BIN = "codex"

# Codex defaults. Effort is rendered as a `-c` config override, not a flag: `-c model_reasoning_effort=`.
CODEX_MODEL = "gpt-5.5"
CODEX_EFFORT = "high"
REASONING_EFFORT_KEY = "model_reasoning_effort"

# Bypass approvals+sandbox AND hook-trust. The hook-trust bypass is INDEPENDENTLY required: yolo alone
# stops at the trust gate and fires NO hooks; this flag is what runs our (untrusted) hooks headlessly.
# Kept on every op; both `resume` and `fork` accept it.
BYPASS_APPROVALS_FLAG = "--dangerously-bypass-approvals-and-sandbox"
BYPASS_HOOK_TRUST_FLAG = "--dangerously-bypass-hook-trust"
YOLO_FLAGS = [BYPASS_APPROVALS_FLAG, BYPASS_HOOK_TRUST_FLAG]

# A rollout is `sessions/<Y>/<M>/<D>/rollout-<ts>-<id>.jsonl`; the id is unique across the tree, so a
# recursive `**` glob resolves it regardless of date-dir nesting.
SESSIONS_DIR = "sessions"
ROLLOUT_PREFIX = "rollout-"
TRANSCRIPT_SUFFIX = ".jsonl"


# ----- transcript / path internals ----------------------------------------------------------

def codex_home() -> Path:
    return Path(os.environ.get(CODEX_HOME_ENV, DEFAULT_CODEX_HOME)).expanduser()


def sessions_root() -> Path:
    return codex_home() / SESSIONS_DIR


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
# positional are dropped — the op re-supplies its own.
_IDENTITY_SUBCOMMANDS = frozenset({"resume", "fork"})

# BARE flags — those that do NOT consume a following token. The known bare set is the bypass pair;
# every other `-flag` is value-by-default, so an unknown value-flag keeps its value instead of being
# mistaken for the prompt and dropped. Mind the `-c` collision: codex `-c` is a VALUE flag
# (`-c KEY=VALUE`), so it is NOT bare here (it IS for Claude, where `-c` == `--continue`).
_BARE_FLAGS = frozenset(YOLO_FLAGS)

# Once shlex surfaces a shell-control token, the rest of a compound source `cmd` is shell wrapping,
# not codex argv, and is dropped.
_SHELL_CONTROL_TOKENS = frozenset({";", "&", "&&", "||", "|", "|&", "&>", "&>>", "(", ")", "{", "}"})


def _is_shell_control(token: str) -> bool:
    """A shell operator (exact control token) or a redirection (starts with `<`/`>`), not a flag/value."""
    return token in _SHELL_CONTROL_TOKENS or token[:1] in ("<", ">")


def _strip_identity(source_cmd: str) -> tuple[str, list[str]]:
    """Split a source codex `cmd` into (binary, inherited-flags), dropping the identity subcommand + its
    id and the baked positional prompt, keeping the persona (`-m`, `-c KEY=VALUE`, any unknown flag)."""
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
        if token in _BARE_FLAGS:
            inherited.append(token)  # any positional that follows a bare flag is the prompt (dropped)
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
        effort: str | None = None,
        initial_prompt: str | None = None,
    ) -> list[str]:
        """Argv for a fresh session. A positional prompt auto-submits in the interactive TUI, so the
        seed needs no send-keys."""
        command = [
            CODEX_BIN,
            "-m", model or CODEX_MODEL,
            "-c", f"{REASONING_EFFORT_KEY}={effort or CODEX_EFFORT}",
            *YOLO_FLAGS,
        ]
        if initial_prompt:
            command.append(initial_prompt)
        return command

    def resume_command(self, chat_id: str) -> list[str]:
        """Resume a chat in place via Codex's native `resume` subcommand (bypass flags ride along so hooks fire)."""
        return [CODEX_BIN, "resume", chat_id, *YOLO_FLAGS]

    def fork_command(self, source_cmd: str, chat_id: str) -> list[str]:
        """Codex's native `fork` subcommand, carrying the source persona. Mints a NEW id, captured post-hoc."""
        binary, inherited = _strip_identity(source_cmd)
        return _ensure_yolo([binary, "fork", chat_id, *inherited])

    def seed_command(self, source_cmd: str, seed: str) -> list[str]:
        """A fresh `codex` carrying the source persona (no identity subcommand) + `seed` as its positional
        prompt (which auto-submits)."""
        binary, inherited = _strip_identity(source_cmd)
        command = _ensure_yolo([binary, *inherited])
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
