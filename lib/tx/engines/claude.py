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
from collections.abc import Iterator, Mapping
from pathlib import Path

from ..session import ChatRef, Engine, State
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

    def fork_command(self, chat_id: str) -> list[str]:
        """`claude --resume <chat_id> --fork-session …` — branch a chat into a new session that opens
        on its full history (the fork mints its own id at startup, #7/#8)."""
        return [CLAUDE_BIN, "--resume", chat_id, "--fork-session", "--dangerously-skip-permissions"]

    def seed_command(self, prompt: str) -> list[str]:
        """A fresh session carrying a seed prompt as its initial-prompt positional (handover/rollover's
        new worker) — the prompt auto-submits, so no send-keys."""
        return self.build_launch_command(initial_prompt=prompt)

    def distiller_command(self) -> list[str]:
        """The throwaway distiller that summarizes a chat into a handover/rollover brief: opus at
        medium effort (the distillation is the quality hinge — worth opus's judgement)."""
        return self.build_launch_command(model="opus", effort="medium")

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

    def bundle(self, chat: ChatRef) -> Path:
        """Mirror a chat into its durable history bundle (transcript + the sibling sidecar dir) and
        return the bundle dir. Delegates to history.py's tested incremental copy. The core drives
        ingest through `history` directly (the engine-neutral copy); the per-engine bundle LAYOUT
        (Codex's sidecar-free rollout) lands with `CodexEngine` (T6). Imported lazily because
        `history` imports this module."""
        from .. import history

        tx_id = Path(chat.bundle_path).parent.name
        history.ingest_chat(tx_id, chat.id, chat.cwd, Engine.CLAUDE, wait=True)
        return bundle_dir(tx_id, chat.id)

    # ----- hooks / state -------------------------------------------------------------------

    @property
    def event_to_state(self) -> Mapping[str, State]:
        return _EVENT_TO_STATE

    @property
    def state_source(self) -> StateSource:
        """Claude emits a `Stop` hook at turn end, so its turn-done state arrives as a hook event."""
        return StateSource.HOOK_EVENTS


register(Engine.CLAUDE, ClaudeEngine())
