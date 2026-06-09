from __future__ import annotations

import json
import os
import shlex
from collections.abc import Iterator, Mapping
from pathlib import Path

from ..session import Engine, State
from ..storage import history_dir
from .engine_adapter import EngineAdapter, StateSource
from .registry import registry

# Claude's own home (where it writes transcripts), honoring $CLAUDE_CONFIG_DIR like the CLI —
# NOT $TX_IDE_HOME.
CLAUDE_HOME_ENV = "CLAUDE_CONFIG_DIR"
DEFAULT_CLAUDE_HOME = "~/.claude"

CLAUDE_BIN = "claude"
TRANSCRIPT_SUFFIX = ".jsonl"
BUNDLE_TRANSCRIPT_NAME = "transcript.jsonl"


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
_IDENTITY_VALUE_FLAGS = frozenset({"--session-id", "--resume"})
_IDENTITY_BARE_FLAGS = frozenset({"--fork-session", "--continue", "-c"})

# Bare claude flags — those that do NOT consume a following token, so a positional that follows one
# (or stands alone) is the baked initial prompt and gets dropped. EVERY OTHER `--flag` is assumed to
# take a value, so an unknown value-flag keeps its value instead of being mistaken for the prompt —
# dropping it would leave a dangling flag that swallows the appended seed. A deny-list (not an
# allow-list of value-flags) is deliberate: `claude --help` documents some value-flags only in prose
# (e.g. `--append-system-prompt[-file]`), so an allow-list silently missed them; a missed BARE flag
# here is benign (worst case: the predecessor prompt carried forward, never a corrupt command).
_BARE_FLAGS = frozenset({
    "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions", "--verbose",
    "--print", "-p", "--ide", "--tmux", "--strict-mcp-config", "--no-session-persistence",
    "--exclude-dynamic-system-prompt-sections", "--replay-user-messages",
    "--include-partial-messages", "--include-hook-events", "--disable-slash-commands",
    "--chrome", "--no-chrome",
})

# Shell-control tokens. Once shlex surfaces one of these, the rest of a compound source `cmd` is
# shell wrapping (separator / pipe / redirect / subshell / …), NOT claude argv. A fork/handover/
# rollover is a FRESH claude invocation, not the source's shell pipeline, so everything from the
# first such token on is dropped.
_SHELL_CONTROL_TOKENS = frozenset({";", "&", "&&", "||", "|", "|&", "&>", "&>>", "(", ")", "{", "}"})


def _is_shell_control(token: str) -> bool:
    # An exact control token, or a redirection (starts with `<`/`>`) — enough to find the shell
    # boundary without a full shell parser.
    return token in _SHELL_CONTROL_TOKENS or token[:1] in ("<", ">")


def _strip_identity(source_cmd: str) -> tuple[str, list[str]]:
    """Split a source `cmd` into (binary, inherited-flags), dropping the identity flags and any
    positional initial-prompt so the op can re-supply its own; the persona flags are inherited."""
    tokens = shlex.split(source_cmd)
    binary = tokens[0] if tokens else CLAUDE_BIN
    inherited: list[str] = []
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if _is_shell_control(token):
            break  # shell wrapping begins here — drop it and everything after
        if token in _IDENTITY_VALUE_FLAGS:
            index += 2  # drop the identity flag and its value
            continue
        if token in _IDENTITY_BARE_FLAGS:
            index += 1  # drop — the op re-supplies its own
            continue
        if token in _BARE_FLAGS:
            inherited.append(token)  # bare flag; any positional that follows it is the prompt (dropped)
            index += 1
            continue
        if token.startswith("-"):
            # Value-flag (known or unknown): inherit it WITH its value when one follows; never drop
            # the value — a dangling flag would swallow the appended seed.
            if index + 1 < len(tokens) and not tokens[index + 1].startswith("-") \
                    and not _is_shell_control(tokens[index + 1]):
                inherited.extend(tokens[index:index + 2])
                index += 2
            else:
                inherited.append(token)  # dangling flag (end of argv / next token is itself a flag)
                index += 1
            continue
        index += 1  # a positional — the source's baked initial prompt; drop it (the op seeds its own)
    return binary, inherited


def _ensure_skip_permissions(command: list[str]) -> list[str]:
    """Guarantee --dangerously-skip-permissions — else a forked/seeded session stalls on a
    permission prompt and its initial prompt never runs."""
    if "--dangerously-skip-permissions" not in command:
        command.append("--dangerously-skip-permissions")
    return command


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
        effort: str | None = None,
        initial_prompt: str | None = None,
    ) -> list[str]:
        # A positional prompt auto-submits in interactive mode (measured).
        command = [CLAUDE_BIN]
        if model:
            command += ["--model", model]
        if effort:
            command += ["--effort", effort]
        command.append("--dangerously-skip-permissions")
        if initial_prompt:
            command.append(initial_prompt)
        return command

    def resume_command(self, chat_id: str) -> list[str]:
        return [CLAUDE_BIN, "--resume", chat_id, "--dangerously-skip-permissions"]

    def fork_command(self, source_cmd: str, chat_id: str) -> list[str]:
        """Branch a chat onto its full history, inheriting the source persona (incl. unknown
        value-flags) with the identity flags swapped for this fork's own. The fork mints its own id."""
        binary, inherited = _strip_identity(source_cmd)
        return _ensure_skip_permissions(
            [binary, "--resume", chat_id, "--fork-session", *inherited]
        )

    def seed_command(self, source_cmd: str, seed: str) -> list[str]:
        """A fresh session inheriting the source persona (no identity flag) with `seed` as the
        initial-prompt positional. The positional auto-submits, so no send-keys."""
        binary, inherited = _strip_identity(source_cmd)
        command = _ensure_skip_permissions([binary, *inherited])
        command.append(seed)
        return command

    def distiller_command(self, seed: str) -> list[str]:
        # Fixed per-engine command (no source persona): claude → opus at medium effort, the quality
        # hinge of the distillation. Seed appended unconditionally (not via build_launch_command's
        # truthy filter).
        command = self.build_launch_command(model="opus", effort="medium")
        command.append(seed)
        return command

    # ----- transcript ----------------------------------------------------------------------

    def resolve_transcript(self, chat_id: str, cwd: str) -> Path:
        return transcript_path(chat_id, cwd)

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
