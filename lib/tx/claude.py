"""Claude integration — used **directly**, no provider abstraction (§15).

tx uses Claude directly because it is the only agent today and abstracting now would be guesswork.
This module is the one place Claude specifics live: the transcript-path rule, the launch-flag
builder (resume / fork / pre-mint), and the history-bundle layout. It is called directly by
chat-ops (S4) and history (S3); the only agent-specific *install* seam is `setup/agents/claude.sh`
(§10), not this module.

Ground truth measured on claude v2.0.76 (chat-ops.md §1):
  - Transcript:  ~/.claude/projects/<munge(cwd)>/<chat-uuid>.jsonl  + a sibling <chat-uuid>/ dir
                 (`subagents/`, `tool-results/`).
  - Munge rule:  every `/` and every `.` in the absolute cwd becomes `-`
                 (verified on disk: `…/tx-ide/.claude/worktrees/x` → `…-tx-ide--claude-worktrees-x`).
  - `--session-id` is REJECTED together with `--resume` (#5); a fresh `--session-id` alone is
    accepted (#6); `--fork-session` mints its own id (#7) at startup (#8).
"""

from __future__ import annotations

import os
from pathlib import Path

from .storage import history_dir

# Claude's own home (where it writes transcripts). Honors $CLAUDE_CONFIG_DIR like the CLI does;
# defaults to ~/.claude. This is Claude's home, NOT $TX_IDE_HOME.
CLAUDE_HOME_ENV = "CLAUDE_CONFIG_DIR"
DEFAULT_CLAUDE_HOME = "~/.claude"

CLAUDE_BIN = "claude"
TRANSCRIPT_SUFFIX = ".jsonl"
BUNDLE_TRANSCRIPT_NAME = "transcript.jsonl"


def claude_home() -> Path:
    return Path(os.environ.get(CLAUDE_HOME_ENV, DEFAULT_CLAUDE_HOME)).expanduser()


def projects_root() -> Path:
    """`~/.claude/projects` — the per-cwd transcript tree."""
    return claude_home() / "projects"


def munge(cwd: str) -> str:
    """Munge an absolute cwd into Claude's project-dir name: every `/` and `.` becomes `-`
    (verified on disk). Pass an absolute path — the caller owns producing it."""
    return cwd.replace("/", "-").replace(".", "-")


def project_dir(cwd: str) -> Path:
    """The transcript directory for a given cwd: `~/.claude/projects/<munge(cwd)>`."""
    return projects_root() / munge(cwd)


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


# ----- launch-flag builder (§15, chat-ops §10) ---------------------------------------------

def build_launch_command(
    *,
    model: str | None = None,
    effort: str | None = None,
    session_id: str | None = None,
    resume: str | None = None,
    fork_session: bool = False,
    append_system_prompt: str | None = None,
    dangerously_skip_permissions: bool = True,
    initial_prompt: str | None = None,
) -> list[str]:
    """Assemble the `claude` argv from launch options, returning a list (callers `shlex.join` it
    for tmux if they need a string).

    Validates the measured CLI constraints at this boundary (chat-ops §1): a fresh `--session-id`
    cannot combine with `--resume` (#5 — fork/resume mint their own id, captured by snapshot-diff),
    and `--fork-session` needs a `--resume` source. These are external-CLI contracts, so an invalid
    combination is an error, not silent.
    """
    if session_id and resume:
        raise ValueError(
            "claude rejects --session-id together with --resume (chat-ops #5); a resumed/forked "
            "chat mints its own id — capture it by snapshot-diff or the origin-aware hook"
        )
    if fork_session and not resume:
        raise ValueError("--fork-session requires a --resume <source-chat>")

    command = [CLAUDE_BIN]
    if model:
        command += ["--model", model]
    if effort:
        command += ["--effort", effort]
    if resume:
        command += ["--resume", resume]
    if fork_session:
        command.append("--fork-session")
    if session_id:
        command += ["--session-id", session_id]
    if dangerously_skip_permissions:
        command.append("--dangerously-skip-permissions")
    if append_system_prompt:
        command += ["--append-system-prompt", append_system_prompt]
    if initial_prompt:
        command.append(initial_prompt)
    return command
