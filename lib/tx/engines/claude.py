"""`ClaudeEngine` — the Claude Code adapter (design §5), and the Claude specifics behind it.

This is the extraction of the former top-level `lib/tx/claude.py`: the one place Claude specifics
live — the transcript-path rule, the launch-flag builders (fresh / resume / fork / seed / distiller),
and the history-bundle layout — now expressed as an `EngineAdapter` (design §2) the engine-blind core
calls through the registry. The module registers itself at import: `register(Engine.CLAUDE, …)`.

Identity is **capture-after-launch** (design §2/§7, task T4): the session id is read off the hook
payload (`capture_session_id`), never pre-minted. The transcript/path internals + history-bundle
helpers stay as module-level names here (the same symbols the former module exported) — the core
imports them directly (`from .engines import claude`) where the transcript-path rules are reused;
T4 routed transcript *resolution* through `resolve_transcript`, and the pre-mint helpers
(`inject_session_id` / `command_declares_chat`) are gone.

Ground truth measured on claude v2.0.76 (chat-ops.md §1):
  - Transcript:  <claude-home>/projects/<munge(cwd)>/<chat-uuid>.jsonl  + a sibling <chat-uuid>/ dir
                 (`subagents/`, `tool-results/`).
  - Munge rule:  every `/` and every `.` in the absolute cwd becomes `-`
                 (verified on disk: a `…/tx-ide/.claude/worktrees/x` cwd → `…-tx-ide--claude-worktrees-x`).
  - Every hook payload carries `session_id` + `transcript_path`, so capture works from the first
    event (`SessionStart`), before the transcript file exists (`--fork-session` writes it lazily, #8).
"""

from __future__ import annotations

import json
import os
import shlex
from collections.abc import Iterator, Mapping
from pathlib import Path

from ..session import Engine, State
from ..storage import history_dir
from .protocol import EngineAdapter, StateSource
from .registry import register

# Claude's own home (where it writes transcripts). Honors $CLAUDE_CONFIG_DIR like the CLI does;
# defaults to ~/.claude. This is Claude's home, NOT $TX_IDE_HOME.
CLAUDE_HOME_ENV = "CLAUDE_CONFIG_DIR"
DEFAULT_CLAUDE_HOME = "~/.claude"

CLAUDE_BIN = "claude"
TRANSCRIPT_SUFFIX = ".jsonl"
BUNDLE_TRANSCRIPT_NAME = "transcript.jsonl"


# ----- transcript / path internals (transitional — importable from here until T4) -----------

def claude_home() -> Path:
    return Path(os.environ.get(CLAUDE_HOME_ENV, DEFAULT_CLAUDE_HOME)).expanduser()


def projects_root() -> Path:
    """`<claude-home>/projects` — the per-cwd transcript tree."""
    return claude_home() / "projects"


def munge(cwd: str) -> str:
    """Munge an absolute path into Claude's project-dir name: every `/` and `.` becomes `-`
    (verified on disk). A pure string transform (no filesystem I/O) — the caller passes the
    absolute path. claude derives the dir from the cwd's *realpath* (symlinks resolved), so the
    symlink resolution lives in `project_dir`, not here; this stays a plain string op."""
    return cwd.replace("/", "-").replace(".", "-")


def project_dir(cwd: str) -> Path:
    """The transcript directory for a given cwd: `<claude-home>/projects/<munge(realpath(cwd))>`.

    claude computes this dir from the *realpath* of its cwd, so we resolve symlinks here before
    munging — otherwise a symlinked cwd (e.g. macOS `/tmp` → `/private/tmp`) munges to a dir claude
    never wrote, breaking the deterministic transcript path and fork-id capture. A no-op for
    non-symlinked paths. This is the single chokepoint: `transcript_path`, `sidecar_dir`,
    `find_transcript`, and chat.py's snapshot helpers all route through it."""
    return projects_root() / munge(os.path.realpath(cwd))


def transcript_path(chat_id: str, cwd: str) -> Path:
    """The deterministic transcript path `<project_dir>/<chat-uuid>.jsonl`. May not exist yet
    (a fork writes it ~2 s after launch); existence is the caller's check."""
    return project_dir(cwd) / f"{chat_id}{TRANSCRIPT_SUFFIX}"


def sidecar_dir(chat_id: str, cwd: str) -> Path:
    """The sibling `<chat-uuid>/` dir holding `subagents/` + `tool-results/` — the externalized
    parts a bare `.jsonl` omits (chat-ops §1 #3). Copied wholesale into the bundle (S3)."""
    return project_dir(cwd) / chat_id


def find_transcript(chat_id: str, cwd: str) -> Path | None:
    """Resolve a chat's transcript by the deterministic rule, returning it only if present.

    This is the cwd-known fast path. The cross-project glob resolver for when the cwd has moved
    (a deleted worktree) is `history.py`'s job (chat-ops §3.4, S3) — not duplicated here.
    """
    path = transcript_path(chat_id, cwd)
    return path if path.exists() else None


# ----- history bundle layout (§11 / chat-ops §3.1) — the copy itself is S3 -----------------

def bundle_dir(tx_id: str, chat_id: str) -> Path:
    """Where a chat's ingested bundle lives: `$TX_IDE_HOME/history/<tx-id>/<chat-uuid>/`."""
    return history_dir() / tx_id / chat_id


def bundle_transcript_path(tx_id: str, chat_id: str) -> Path:
    """The transcript copy inside a bundle: `<bundle_dir>/transcript.jsonl`."""
    return bundle_dir(tx_id, chat_id) / BUNDLE_TRANSCRIPT_NAME


# ----- chat-op command derivation (Claude's persona parse — moved here from chat.py, T8b) ----
# The Claude flag grammar for reconstructing a fork/handover/rollover launch command from a source
# session's `cmd`. It lives HERE (it is Claude's grammar, not the engine-blind orchestration) and
# backs `ClaudeEngine.fork_command` / `seed_command`. The chat-op orchestration in `chat.py` is
# engine-blind and dispatches on `record.engine` (T8b); this is the Claude rendering of that surface.
# Carried verbatim from the pre-T8b `chat.py` so the derived command is byte-identical (the V-T8b
# differential gate), preserving the `eae09f3` / #50 unknown-value-flag survival.

# Identity flags stripped when reconstructing a launch command: the new op re-supplies its own
# (`--resume … --fork-session` for fork, none for a fresh handover/rollover successor). Everything
# else (model / effort / --append-system-prompt / --settings / skip-permissions) is inherited.
_IDENTITY_VALUE_FLAGS = frozenset({"--session-id", "--resume"})
_IDENTITY_BARE_FLAGS = frozenset({"--fork-session", "--continue", "-c"})

# Bare claude flags — the ones that do NOT consume a following token. They are what lets the baked
# initial-prompt positional be told apart when reconstructing a launch command: a positional that
# follows a bare flag (or stands alone) is the prompt and is dropped (the op seeds its own). EVERY
# OTHER `--flag` is assumed to take a value, so an unrecognised value-flag keeps its value instead of
# having it mistaken for the prompt and dropped — which would corrupt the command by leaving a
# dangling flag to swallow the appended seed. This deny-list (vs. an allow-list of value-flags) is
# deliberate: `claude --help` documents some value-flags only in prose (e.g. `--append-system-prompt
# [-file]`), so an allow-list silently missed them; a missed BARE flag here is benign (the worst case
# is the predecessor prompt carried forward, never a corrupt command). Identity flags are handled above.
_BARE_FLAGS = frozenset({
    "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions", "--verbose",
    "--print", "-p", "--ide", "--tmux", "--strict-mcp-config", "--no-session-persistence",
    "--exclude-dynamic-system-prompt-sections", "--replay-user-messages",
    "--include-partial-messages", "--include-hook-events", "--disable-slash-commands",
    "--chrome", "--no-chrome",
})

# Shell-control tokens. Once shlex surfaces one of these, the rest of a compound source `cmd` is
# shell wrapping (separator / logical / pipe / background / subshell / brace-group), NOT claude
# argv. A fork/handover/rollover is a FRESH claude invocation, not the source's shell pipeline, so
# everything from the first such token on is dropped (bug #2b).
_SHELL_CONTROL_TOKENS = frozenset({";", "&", "&&", "||", "|", "|&", "&>", "&>>", "(", ")", "{", "}"})


def _is_shell_control(token: str) -> bool:
    """Whether a shlex token is a shell operator rather than a claude flag/value: an exact control
    token, or a redirection (any token starting with `<`/`>`). Enough to find the shell boundary
    without re-implementing a full shell parser (bug #2b)."""
    return token in _SHELL_CONTROL_TOKENS or token[:1] in ("<", ">")


def _strip_identity(source_cmd: str) -> tuple[str, list[str]]:
    """Split a source `cmd` into (binary, inherited-flags) with the session-identity flags AND any
    positional initial-prompt removed. Inherits the source's persona (model / effort / system-prompt /
    settings / --dangerously-skip-permissions / …) so a forked or handed-over session keeps it, and
    re-supplies its own identity flags + seed.

    Each non-identity `--flag` is inherited WITH its following value UNLESS it is a known bare flag
    (`_BARE_FLAGS`). Assuming an unknown flag takes a value is the safe default: it keeps an
    unrecognised value-flag's value (e.g. `--append-system-prompt-file <path>`, or any future flag)
    instead of dropping it — dropping it would leave a dangling flag that swallows the appended seed
    and corrupts the command. A positional that follows a bare flag (or stands alone) is the baked
    prompt and is dropped — the op appends its own seed, so carrying the predecessor's forward would
    make the fresh session re-run it. Stops at the first shell-control token (a compound `--cmd`'s
    shell wrapping is not claude argv)."""
    tokens = shlex.split(source_cmd)
    binary = tokens[0] if tokens else CLAUDE_BIN
    inherited: list[str] = []
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if _is_shell_control(token):
            break  # shell wrapping begins here — drop it and everything after (bug #2b)
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
            # A value-flag — a known persona flag or an unknown one. Inherit it WITH its value when a
            # value follows; never drop the value (that is the corruption the bare/value split guards).
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
    """A forked/seeded session must not stop on a permission prompt (its initial prompt would never
    run). Guarantee --dangerously-skip-permissions is present (inherited or added)."""
    if "--dangerously-skip-permissions" not in command:
        command.append("--dangerously-skip-permissions")
    return command


# ----- the adapter --------------------------------------------------------------------------
# Claude renders every hook event onto the three live states (§2): the whole "working" family
# reaffirms WORKING (a missed UserPromptSubmit self-heals on the first tool call), Stop / its
# failure / a permission request yield WAITING, and SessionEnd is IDLE. This mirrors hooks.py's
# active `STATE_FOR_EVENT` table (keyed there by the tx-verb the shim passes); here it is keyed by
# Claude's native hook event names — the engine's own vocabulary the core consumes from T4 on.
# `Notification` is omitted: it is payload-dependent (only idle/permission/elicitation subtypes
# yield WAITING — hooks.py resolves that from the payload), not a static name→state edge.
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
    """The Claude Code adapter — the protocol surface (design §2/§5) over the Claude specifics above.

    Stateless: a single instance is registered for `Engine.CLAUDE`. The launch/ops builders return
    argv lists (the binary first) with Claude's yolo flag (`--dangerously-skip-permissions`) baked
    in, so callers stay engine-blind (design §4.3). `capture_session_id` is the active id path
    (T4, §2/§7) — the core captures the id from the hook payload, never pre-minting.
    """

    # ----- identity ------------------------------------------------------------------------

    @property
    def binary(self) -> str:
        return CLAUDE_BIN

    def matches_binary(self, command: str) -> bool:
        """Whether `command` invokes claude — the basename of its first token is the claude binary.
        Mirrors `spawn.infer_role`'s test (a full path like `/usr/local/bin/claude …` still matches)."""
        tokens = command.split()
        return bool(tokens) and os.path.basename(tokens[0]) == CLAUDE_BIN

    def capture_session_id(self, hook_payload: dict) -> tuple[str, str]:
        """Read `(session_id, transcript_path)` off Claude's hook payload (both keys are present on
        every Claude hook event). The **active** id path (T4, design §2/§7): `hooks.py` calls this on
        the first capture event (`SessionStart` / `UserPromptSubmit`) to fill the pending `ChatRef`."""
        return hook_payload["session_id"], hook_payload["transcript_path"]

    # ----- launch / ops --------------------------------------------------------------------

    def build_launch_command(
        self,
        *,
        model: str | None = None,
        effort: str | None = None,
        initial_prompt: str | None = None,
    ) -> list[str]:
        """Assemble the argv for a fresh session: `claude [--model M] [--effort E]
        --dangerously-skip-permissions [PROMPT]`. Claude renders effort as the `--effort` flag and
        bakes in its own yolo flag; a positional prompt auto-submits in interactive mode (measured)."""
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
        """`claude --resume <chat_id> --dangerously-skip-permissions` — resume a chat in place."""
        return [CLAUDE_BIN, "--resume", chat_id, "--dangerously-skip-permissions"]

    def fork_command(self, source_cmd: str, chat_id: str) -> list[str]:
        """`claude --resume <chat_id> --fork-session …` with the SOURCE's persona flags inherited
        (model / effort / system-prompt / --settings / any unknown value-flag — #50) and the identity
        flags swapped for this fork's own (design §5). Branches a chat into a new session opening on
        its full history; the fork mints its own id at startup (#7/#8)."""
        binary, inherited = _strip_identity(source_cmd)
        return _ensure_skip_permissions(
            [binary, "--resume", chat_id, "--fork-session", *inherited]
        )

    def seed_command(self, source_cmd: str, seed: str) -> list[str]:
        """A fresh `claude …` carrying the SOURCE's persona flags (model / effort / system-prompt /
        any unknown value-flag — #50) with NO identity flag, plus `seed` as the initial-prompt
        positional (handover worker / rollover successor — design §5). claude mints its own chat id,
        captured from the first hook payload (T4); the positional prompt auto-submits, so no
        send-keys."""
        binary, inherited = _strip_identity(source_cmd)
        command = _ensure_skip_permissions([binary, *inherited])
        command.append(seed)
        return command

    def distiller_command(self, seed: str) -> list[str]:
        """The throwaway distiller that summarizes a chat into a handover/rollover brief: opus at
        medium effort (the distillation is the quality hinge — worth opus's judgement), carrying
        `seed` as its initial-prompt positional. A FIXED per-engine command — it carries NO source
        persona (design §5: claude→opus); the op dispatches on `record.engine`. The seed is appended
        unconditionally (not via `build_launch_command`'s truthy filter) so the joined command is
        byte-identical to the pre-T8b distiller derivation for every seed (the V-T8b differential)."""
        command = self.build_launch_command(model="opus", effort="medium")
        command.append(seed)
        return command

    # ----- transcript ----------------------------------------------------------------------

    def resolve_transcript(self, chat_id: str, cwd: str) -> Path:
        """Locate a chat's transcript by Claude's deterministic formula (`<project_dir>/<id>.jsonl`).
        The path may not exist yet; existence is the caller's check (the cross-project glob fallback
        for a moved cwd is history.py's resolver)."""
        return transcript_path(chat_id, cwd)

    def iter_messages(self, transcript: Path) -> Iterator[dict]:
        """Yield each turn of a Claude transcript as a dict. Claude's on-disk schema (Anthropic JSONL,
        one object per line) IS the engine-neutral form the shared history/messages layer models, so
        this is a line-by-line JSON decode with no translation (Codex's adapter maps Responses items)."""
        with transcript.open() as handle:
            for line in handle:
                line = line.strip()
                if line:
                    yield json.loads(line)

    def bundle_sidecars(self, src_transcript: Path, chat_id: str) -> list[Path]:
        """Claude's bundle LAYOUT: the sibling `<chat-id>/` sidecar dir (`subagents/` + `tool-results/`
        — the externalized parts a bare `.jsonl` omits, chat-ops §1 #3), taken relative to the
        **resolved** transcript so a moved cwd still finds its colocated sidecar. **Pure** — it only
        names the dir; `history.py` runs the shared copy-if-absent (and skips it when the dir is
        absent). The transcript itself is mirrored by `history.py`, not listed here."""
        return [src_transcript.parent / chat_id]

    # ----- hooks / state -------------------------------------------------------------------

    @property
    def event_to_state(self) -> Mapping[str, State]:
        return _EVENT_TO_STATE

    @property
    def state_source(self) -> StateSource:
        """Claude emits a `Stop` hook at turn end, so its turn-done state arrives as a hook event."""
        return StateSource.HOOK_EVENTS


register(Engine.CLAUDE, ClaudeEngine())
