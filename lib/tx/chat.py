"""Chat operations — fork / handover / rollover (stage S4).

Three operations on the conversation(s) behind a tx session, all transcription-based and **never
`-p`/headless** (CHD: `-p` bills credits; the user is on a subscription). Context always comes from
the transcript *files*; model turns happen in a temporary, interactive tx session seeded via
`tmux send-keys` (§7); the live source session is never paused. Each op appends a `ChatRef` with the
right `origin` so the §11 history shows the full lineage. Implementation reference: `chat-ops.md`
(§1 measured claude facts, §4 fork, §5 handover, §6 rollover, §7 the minimal seed, CHD1–CHD6).

  - **fork**  (§4) — new tx session, FULL history. `claude --resume <src> --fork-session`, launched
    interactively (fork *cannot* pre-mint its id — `--session-id` is rejected with `--resume`, #5 —
    so the new chat uuid is captured by snapshot-diffing the project dir, the file lands ≈2 s after
    launch, #8). `ChatRef.role=fork`, `origin → source`.
  - **handover** (§5) — new tx session, DISTILLED task. A temporary distiller reads the source
    bundle (source untouched), writes a focused brief into `$TX_IDE_HOME/history/<src>/`, then calls
    `_handover-finish`, which spawns + seeds a *fresh* pre-minted worker minimally. `role=handover`.
    `--self-catch-up` skips the distiller (the worker reads the bundle itself, CHD1).
  - **rollover** (§6) — SAME tx session, fresh chat. A temporary distiller summarizes the current
    chat into a note, then calls `_rollover-finish`, which `respawn-pane -k`s the SAME pane onto a
    fresh pre-minted chat and seeds it minimally. SAME record; appends `ChatRef{role:rollover}`.
    `--self-catch-up` fires the finish directly (detached). The tmux session never dies (IDLE→WORKING,
    no EXITED — §2).

This module owns the inline distill/summarize prompts (CHD4 — no `DISTILLER` role file) and the
temporary-session spawn helper. It orchestrates over the FROZEN `SessionService` / `Tmux` /
`claude` surfaces (it never re-signs them) and writes every `ChatRef` **synchronously** as the
authoritative primary; the origin-aware hook (`hooks.py`, CHD6) is the idempotent backstop that
fills a fork's still-null id on first prompt.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import claude, history
from .service import NotInsideTmux, ServiceError, SessionNotFound, SessionService
from .session import ChatRef, Origin, Session
from .spawn import SpawnSpec
from .storage import history_dir, tx_ide_home

# ----- the chat-ops env contract (CHD6) ----------------------------------------------------
# The pane env carries provenance from the launching op to the firing hook (the shim drains the
# payload, so env is the only channel). `hooks.py` reads these to stamp the right role/origin.
CHAT_ID_ENV = "TX_CHAT_ID"                    # the chat uuid (pre-minted; absent for a fork)
CHAT_ROLE_ENV = "TX_CHAT_ROLE"                # original | fork | rollover | handover
CHAT_ORIGIN_TXID_ENV = "TX_CHAT_ORIGIN_TXID"  # the tx session the chat derived from
CHAT_ORIGIN_CHAT_ENV = "TX_CHAT_ORIGIN_CHAT"  # the source chat uuid it derived from
# The control vars are stripped from an inherited env so a fork-of-a-fork does not carry stale
# provenance forward (each op sets its own).
CHAT_CONTROL_ENV = frozenset(
    {CHAT_ID_ENV, CHAT_ROLE_ENV, CHAT_ORIGIN_TXID_ENV, CHAT_ORIGIN_CHAT_ENV}
)

# Readiness poll for a freshly-launched claude before send-keys (§7). claude's input box draws a
# `❯ ` prompt once it is ready — the same heuristic `bin/tx-assistant` uses (F9: one heuristic).
READINESS_MARKER = "❯ "
READINESS_ATTEMPTS = 40
READINESS_INTERVAL = 0.5

# claude's input box drops an Enter that arrives too fast after the text (COMMON.md / send_message).
SEED_SETTLE_SECONDS = 0.3

# Fork id capture: the forked `.jsonl` lands at startup ≈2 s (#8); poll a little past that.
FORK_POLL_ATTEMPTS = 16
FORK_POLL_INTERVAL = 0.5

# A lightweight throwaway helper that distills/summarizes then calls a finish verb (CHD4 — the
# prompt is seeded inline, there is no role file). Modest model: this is a mechanical read→write.
DISTILLER_COMMAND = "claude --model sonnet --dangerously-skip-permissions"
DISTILLER_TAG = "temporary"

# Identity flags stripped when reconstructing a launch command from a source session's `cmd`: the
# new op re-supplies its own (`--resume … --fork-session` for fork, `--session-id <new>` for a fresh
# chat). Everything else (model / effort / --append-system-prompt / skip-permissions) is inherited.
_IDENTITY_VALUE_FLAGS = frozenset({"--session-id", "--resume"})
_IDENTITY_BARE_FLAGS = frozenset({"--fork-session", "--continue", "-c"})


# ----- transcript snapshotting (fork capture, §4 step 5 + the hook backstop) ----------------

def snapshot_transcripts(cwd: str) -> set[str]:
    """The set of chat uuids whose `.jsonl` currently exists in `cwd`'s project dir. The fork
    capture diffs a before/after snapshot to find the file the fork minted at startup (#8)."""
    directory = claude.project_dir(cwd)
    if not directory.is_dir():
        return set()
    return {path.stem for path in directory.glob(f"*{claude.TRANSCRIPT_SUFFIX}")}


def newest_unclaimed_transcript(cwd: str, exclude: set[str]) -> str | None:
    """The most-recently-written transcript uuid in `cwd`'s project dir that is not in `exclude`,
    or None. The origin-aware hook uses this to complete a fork whose snapshot-diff missed: the
    fork's `.jsonl` is the newest one that is neither the source chat nor an already-recorded id."""
    directory = claude.project_dir(cwd)
    if not directory.is_dir():
        return None
    candidates = [
        (path.stat().st_mtime, path.stem)
        for path in directory.glob(f"*{claude.TRANSCRIPT_SUFFIX}")
        if path.stem not in exclude
    ]
    if not candidates:
        return None
    return max(candidates)[1]


# ----- chat selection -----------------------------------------------------------------------

def active_chat(session: Session) -> ChatRef | None:
    """The chat a fork/handover/rollover targets: the last `ChatRef` carrying a real id, preferring
    one still open (`ended_at is None`). Ops append in order, so the last is the live thread."""
    candidates = [chat for chat in session.chats if chat.id is not None]
    if not candidates:
        return None
    open_chats = [chat for chat in candidates if chat.ended_at is None]
    return (open_chats or candidates)[-1]


# ----- launch-command reconstruction (inherit the source's flags, swap the identity ones) ---

def _strip_identity(source_cmd: str) -> tuple[str, list[str]]:
    """Split a source `cmd` into (binary, inherited-flags) with the session-identity flags removed.
    Inherits model / effort / --append-system-prompt / --dangerously-skip-permissions so a forked or
    handed-over session keeps the source's persona, and re-supplies its own identity flags."""
    tokens = shlex.split(source_cmd)
    binary = tokens[0] if tokens else claude.CLAUDE_BIN
    inherited: list[str] = []
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in _IDENTITY_VALUE_FLAGS:
            index += 2  # drop the flag and its value
            continue
        if token in _IDENTITY_BARE_FLAGS:
            index += 1
            continue
        inherited.append(token)
        index += 1
    return binary, inherited


def _ensure_skip_permissions(command: list[str]) -> list[str]:
    """A forked/seeded session must not stop on a permission prompt (it would never get the Enter).
    Guarantee --dangerously-skip-permissions is present (inherited or added)."""
    if "--dangerously-skip-permissions" not in command:
        command.append("--dangerously-skip-permissions")
    return command


def fork_command(source_cmd: str, source_chat: str) -> str:
    """`claude --resume <src> --fork-session …` with the source's persona flags inherited (§4)."""
    binary, inherited = _strip_identity(source_cmd)
    command = _ensure_skip_permissions(
        [binary, "--resume", source_chat, "--fork-session", *inherited]
    )
    return shlex.join(command)


def fresh_command(source_cmd: str, new_chat: str) -> str:
    """`claude --session-id <new> …` for a fresh pre-minted chat (handover worker / rollover
    successor), inheriting the source's persona flags (#6 — a fresh `--session-id` is accepted)."""
    binary, inherited = _strip_identity(source_cmd)
    command = _ensure_skip_permissions([binary, "--session-id", new_chat, *inherited])
    return shlex.join(command)


def _inherited_env(session: Session) -> dict[str, str]:
    """The source session's env minus the chat-ops control vars (a fork/handover must set its own
    provenance, not carry the source's forward)."""
    return {key: value for key, value in session.env.items() if key not in CHAT_CONTROL_ENV}


# ----- helpers shared by the ops -------------------------------------------------------------

def _slug(text: str) -> str:
    """A filesystem-safe slug for a handover task → `handover-<slug>.md` (§5)."""
    cleaned = "".join(character if character.isalnum() else "-" for character in text.lower())
    slug = "-".join(part for part in cleaned.split("-") if part)
    return slug[:48] or "task"


def _tx_program() -> str:
    """The repo's `bin/tx` entrypoint (chat.py → lib/tx → lib → repo). A seeded distiller runs this
    absolute path, not a bare `tx` (during coexistence the bare `tx` on PATH is the OLD bash one)."""
    return str(Path(__file__).resolve().parents[2] / "bin" / "tx")


def _tx_invocation() -> str:
    """The baked `env TX_IDE_HOME=<home> <bin/tx>` a distiller seed runs, so the finish verb resolves
    the SAME home this process resolved (the C9 bake idea — the distiller's `tx` calls must hit the
    dev home during coexistence, the real home after Flip)."""
    return f"env TX_IDE_HOME={shlex.quote(str(tx_ide_home()))} {shlex.quote(_tx_program())}"


class ChatOps:
    """fork / handover / rollover orchestrated over the frozen `SessionService` surface."""

    def __init__(self, service: SessionService):
        self.service = service

    # ----- fork (§4) -----------------------------------------------------------------------

    def fork(self, source: str, new_name: str | None = None) -> Session:
        """Branch the source's active chat into a NEW tx session that starts with the full history,
        then diverges. Native `--fork-session`, interactive, no `-p`. The source `.jsonl` is
        read-only under `--fork-session` (#8) — safe to fork a session you are actively using."""
        source_session = self.service.get(source)
        if source_session is None:
            raise SessionNotFound(f"fork: source session '{source}' not found")
        source_chat = active_chat(source_session)
        if source_chat is None:
            raise ServiceError(f"fork: '{source_session.name}' has no chat to fork")

        cwd = source_chat.cwd
        name = self._unique_name(new_name or f"{source_session.name}-fork")
        before = snapshot_transcripts(cwd)

        env = {
            **_inherited_env(source_session),
            CHAT_ROLE_ENV: "fork",
            CHAT_ORIGIN_TXID_ENV: source_session.id,
            CHAT_ORIGIN_CHAT_ENV: source_chat.id,
        }
        spec = SpawnSpec.for_process(
            name=name, tags=list(source_session.tags), cwd=cwd,
            cmd=fork_command(source_session.cmd, source_chat.id), env=env,
        )
        new_session = self.service.spawn(spec)

        # Tier 1 (synchronous, ~2 s): snapshot-diff the project dir for the id the fork minted (#8).
        fork_chat_id = self._capture_fork_chat(cwd, before)
        self._record_fork_chat(new_session.id, cwd, source_session.id, source_chat.id, fork_chat_id)
        # Tier 2 (CHD6): if the poll missed, the record holds a null-id placeholder the origin-aware
        # hook completes on first prompt — `new_session.env` carries the role/origin it reads.
        self.service.log.append(
            "fork", f"{source_session.name} → {name} (chat {(fork_chat_id or 'pending')[:8]})"
        )
        return self.service.store.load(new_session.id)

    def _capture_fork_chat(self, cwd: str, before: set[str]) -> str | None:
        """Poll the project dir until the fork's new `.jsonl` appears (it lands ≈2 s after launch,
        #8), returning its uuid — or None if it has not appeared within the window (the hook backstop
        then completes it). If several appear, the newest by mtime is the fork."""
        for _ in range(FORK_POLL_ATTEMPTS):
            time.sleep(FORK_POLL_INTERVAL)
            appeared = snapshot_transcripts(cwd) - before
            if appeared:
                return newest_unclaimed_transcript(cwd, before)
        return None

    def _record_fork_chat(
        self, new_txid: str, cwd: str, source_txid: str, source_chat: str, fork_chat_id: str | None
    ) -> None:
        """Append the fork's `ChatRef` to the new record (role=fork, origin→source). Idempotent with
        the hook backstop: if the hook already created/filled it, only fill a still-null id here."""
        session = self.service.store.load(new_txid)
        existing = next(
            (chat for chat in session.chats
             if chat.role == "fork" and chat.origin.chat_id == source_chat),
            None,
        )
        if existing is not None:
            if existing.id is None and fork_chat_id is not None:
                existing.id = fork_chat_id
                existing.transcript_path = str(claude.transcript_path(fork_chat_id, cwd))
                self.service.store.save(session)
            return
        session.chats.append(ChatRef(
            id=fork_chat_id,
            role="fork",
            cwd=cwd,
            transcript_path=str(claude.transcript_path(fork_chat_id, cwd)) if fork_chat_id else "",
            origin=Origin(how="fork", session_id=source_txid, chat_id=source_chat),
            started_at=time.time(),
        ))
        self.service.store.save(session)

    # ----- handover (§5) -------------------------------------------------------------------

    def handover(
        self, source: str, task: str, new_name: str | None = None, self_catch_up: bool = False
    ) -> str:
        """Distill the source chat into a focused brief for a NEW worker (source untouched). The
        default spins a temporary distiller that writes the brief then calls `_handover-finish`;
        `--self-catch-up` skips it and seeds the worker to read the source bundle itself (CHD1).
        Returns the worker's name (the worker is launched by the async tail)."""
        source_session = self.service.get(source)
        if source_session is None:
            raise SessionNotFound(f"handover: source session '{source}' not found")
        source_chat = active_chat(source_session)
        if source_chat is None:
            raise ServiceError(f"handover: '{source_session.name}' has no chat to hand over")

        # Mirror the source bundle so the distiller/worker reads the durable copy, not the live
        # session (source untouched). Blocking (wait=True) so the bundle is complete before we read.
        history.ingest_session(self.service.store, source_session.id, wait=True)

        worker_name = self._unique_name(new_name or f"{source_session.name}-handover")
        worker_chat = str(uuid.uuid4())
        brief_path = history_dir() / source_session.id / f"handover-{_slug(task)}.md"

        if self_catch_up:
            # No distiller: the worker reads the source bundle and writes its own brief (CHD1).
            self.handover_finish(
                source_session.id, source_chat.id, worker_name, worker_chat,
                brief_path=None, self_catch_up=True,
            )
            self.service.log.append("handover", f"{source_session.name} → {worker_name} (self-catch-up)")
            return worker_name

        distiller = self._unique_name(f"{worker_name}-distill")
        finish = (
            f"{_tx_invocation()} _handover-finish {shlex.quote(source_session.id)} "
            f"{shlex.quote(source_chat.id)} {shlex.quote(worker_name)} {shlex.quote(worker_chat)} "
            f"{shlex.quote(str(brief_path))} ; {_tx_invocation()} kill {shlex.quote(distiller)}"
        )
        seed = (
            f"You are a tx-ide handover distiller (a temporary helper). Read the predecessor chat "
            f"bundle at {claude.bundle_transcript_path(source_session.id, source_chat.id)} . "
            f"Distill a focused, self-contained brief for this task and write it to {brief_path} : "
            f"«{task}». Capture only what the new worker needs to start — relevant context, current "
            f"state, constraints, and key file paths — not the whole history. When the brief file is "
            f"saved, run exactly this command and nothing else: {finish}"
        )
        self._spawn_distiller(distiller, source_chat.cwd, seed)
        self.service.log.append("handover", f"{source_session.name} → {worker_name} (distilling)")
        return worker_name

    def handover_finish(
        self, source_txid: str, source_chat: str, worker_name: str, worker_chat: str,
        brief_path: str | None, self_catch_up: bool,
    ) -> Session:
        """Spawn the fresh handover worker (pre-minted `--session-id <worker_chat>`), record its
        `ChatRef{role:handover}`, and seed it minimally (§7). Called by the distiller as its final
        act (or directly for `--self-catch-up`). The worker carries the origin env so the hook
        backstops the same `ChatRef` on first prompt."""
        source = self.service.store.load(source_txid)
        if source is None:
            raise SessionNotFound(f"_handover-finish: source record '{source_txid}' not found")
        cwd = next(
            (chat.cwd for chat in source.chats if chat.id == source_chat), source.cwd
        )
        env = {
            **_inherited_env(source),
            CHAT_ID_ENV: worker_chat,
            CHAT_ROLE_ENV: "handover",
            CHAT_ORIGIN_TXID_ENV: source_txid,
            CHAT_ORIGIN_CHAT_ENV: source_chat,
        }
        spec = SpawnSpec.for_process(
            name=worker_name, tags=list(source.tags), cwd=cwd,
            cmd=fresh_command(source.cmd, worker_chat), env=env,
        )
        worker = self.service.spawn(spec)
        self._record_seeded_chat(worker.id, worker_chat, cwd, "handover", source_txid, source_chat)

        bundle = claude.bundle_dir(source_txid, source_chat)
        if self_catch_up:
            seed = (
                f"You are taking over work via tx handover. There is no pre-written brief — read the "
                f"predecessor bundle at {bundle}/ (transcript.jsonl + subagents/ + tool-results/), "
                f"write yourself a short brief of the task and its state, then begin."
            )
        else:
            seed = (
                f"Your task brief is at {brief_path} — read it and begin. Fuller predecessor history, "
                f"only if the brief is insufficient: {bundle}/ ."
            )
        self._seed(worker.name, seed)
        self.service.log.append("handover-finish", f"{worker_name} (chat {worker_chat[:8]})")
        return worker

    # ----- rollover (§6) -------------------------------------------------------------------

    def rollover(self, session: str | None = None, self_catch_up: bool = False) -> str:
        """Rotate the SAME tx session onto a fresh chat in the SAME pane (context exhausted). The
        default summarizes the current chat into a note via a temporary distiller, which then calls
        `_rollover-finish` to respawn the pane (CHD5 — fires exactly when the note is ready);
        `--self-catch-up` fires the finish directly. Returns the new chat uuid."""
        target = session or self.service.tmux.current_session_name()
        if target is None:
            raise NotInsideTmux("rollover: run inside a tmux session or pass <session>")
        record = self.service.get(target)
        if record is None:
            raise SessionNotFound(f"rollover: session '{target}' not found")
        current_chat = active_chat(record)
        if current_chat is None:
            raise ServiceError(f"rollover: '{record.name}' has no active chat to roll over")

        pane = self._resolve_pane(record)
        new_chat = str(uuid.uuid4())

        if self_catch_up:
            # No distiller, and the caller may BE the pane being respawned — fire the finish detached
            # (start_new_session) so it survives `respawn-pane -k` killing this process's pane.
            self._detach_finish([
                "_rollover-finish", record.id, current_chat.id, new_chat, pane, "",
            ])
            self.service.log.append("rollover", f"{record.name} (self-catch-up, chat {new_chat[:8]})")
            return new_chat

        note_path = self._next_rollover_note(record.id)
        # Read the freshest bundle (blocking) so the summary reflects the latest turn (source = the
        # live session, but we read its mirrored transcript, never the live session itself).
        history.ingest_session(self.service.store, record.id, wait=True)
        distiller = self._unique_name(f"{record.name}-rollover-distill")
        finish = (
            f"{_tx_invocation()} _rollover-finish {shlex.quote(record.id)} "
            f"{shlex.quote(current_chat.id)} {shlex.quote(new_chat)} {shlex.quote(pane)} "
            f"{shlex.quote(str(note_path))} ; {_tx_invocation()} kill {shlex.quote(distiller)}"
        )
        seed = (
            f"You are a tx-ide rollover summariser (a temporary helper). Read the predecessor chat "
            f"bundle at {claude.bundle_transcript_path(record.id, current_chat.id)} . Write a concise "
            f"hand-off note — the current task, what is done, the immediate next steps, and key files "
            f"and decisions — to {note_path} . When the note file is saved, run exactly this command "
            f"and nothing else: {finish}"
        )
        self._spawn_distiller(distiller, current_chat.cwd, seed)
        self.service.log.append("rollover", f"{record.name} (distilling, chat {new_chat[:8]})")
        return new_chat

    def rollover_finish(
        self, txid: str, source_chat: str, new_chat: str, pane: str, note_path: str
    ) -> Session:
        """Rotate `pane` onto `new_chat` in place: `respawn-pane -k` relaunches claude with a fresh
        pre-minted `--session-id`, the env baked into the command (the frozen `respawn_pane` takes no
        `-e`). Appends `ChatRef{role:rollover}` to the SAME record and seeds the successor minimally.
        The tmux session never dies → state goes IDLE→WORKING, never EXITED (§2)."""
        record = self.service.store.load(txid)
        if record is None:
            raise SessionNotFound(f"_rollover-finish: record '{txid}' not found")
        cwd = next((chat.cwd for chat in record.chats if chat.id == source_chat), record.cwd)

        # Same record → same TX_SESSION_ID; pre-mint TX_CHAT_ID; carry the rollover provenance for
        # the hook backstop. Baked via `env …` into the respawn command (no `-e` on respawn_pane).
        env = {
            "TX_SESSION_ID": txid,
            CHAT_ID_ENV: new_chat,
            CHAT_ROLE_ENV: "rollover",
            CHAT_ORIGIN_TXID_ENV: txid,
            CHAT_ORIGIN_CHAT_ENV: source_chat,
        }
        command = _env_prefix(env) + fresh_command(record.cmd, new_chat)
        self.service.tmux.respawn_pane(pane, command)
        # SAME record gains the rollover chat; the rotated-out chat is closed (a hard `respawn-pane -k`
        # kills claude without a clean SessionEnd, so stamp ended_at here rather than rely on the hook).
        self._record_seeded_chat(
            txid, new_chat, cwd, "rollover", txid, source_chat, close_chat=source_chat
        )

        bundle = claude.bundle_dir(txid, source_chat)
        if note_path:
            seed = (
                f"Continuing prior work in a fresh chat (rollover). Hand-off note: {note_path} — read "
                f"it and continue. Fuller history, only if needed: {bundle}/ ."
            )
        else:
            seed = (
                f"Continuing prior work in a fresh chat (rollover). The predecessor bundle is at "
                f"{bundle}/ (transcript.jsonl + subagents/ + tool-results/) — read what you need to "
                f"resume, then continue."
            )
        self._seed(pane, seed)
        self.service.log.append("rollover-finish", f"{record.name} → chat {new_chat[:8]}")
        return self.service.store.load(txid)

    def _resolve_pane(self, record: Session) -> str:
        """The pane to rotate. `$TMUX_PANE` when rollover runs from INSIDE the target (the common
        case); otherwise the target session's active pane (single-pane assumption, §9). Trust
        `$TMUX_PANE` only when it is the target's own session — `tx rollover <other>` runs in the
        caller's pane, not the target's."""
        pane_env = os.environ.get("TMUX_PANE")
        if pane_env and self.service.tmux.current_session_name() == record.name:
            return pane_env
        pane = self.service.tmux.display_message("#{pane_id}", target=record.name)
        if pane is None:
            raise ServiceError(f"rollover: could not resolve a pane for '{record.name}'")
        return pane

    def _next_rollover_note(self, txid: str) -> Path:
        """`$TX_IDE_HOME/history/<txid>/rollover-<n>.md` — n past the existing rollover notes (CHD3,
        notes live in history, not the repo)."""
        directory = history_dir() / txid
        existing = list(directory.glob("rollover-*.md")) if directory.is_dir() else []
        return directory / f"rollover-{len(existing) + 1}.md"

    # ----- ChatRef + seeding shared mechanics ----------------------------------------------

    def _record_seeded_chat(
        self, txid: str, chat_id: str, cwd: str, role: str, origin_txid: str, origin_chat: str,
        close_chat: str | None = None,
    ) -> None:
        """Append a seeded op's `ChatRef` (handover worker / rollover successor) synchronously, so
        `tx chat ls` shows it immediately and ingest finds it. Idempotent with the hook backstop:
        skip the append if a `ChatRef` for this chat id already exists. `close_chat` stamps
        `ended_at` on the rotated-out chat (rollover) in the same save."""
        now = time.time()
        session = self.service.store.load(txid)
        if close_chat is not None:
            for reference in session.chats:
                if reference.id == close_chat and reference.ended_at is None:
                    reference.ended_at = now
        if not any(reference.id == chat_id for reference in session.chats):
            session.chats.append(ChatRef(
                id=chat_id,
                role=role,
                cwd=cwd,
                transcript_path=str(claude.transcript_path(chat_id, cwd)),
                origin=Origin(how=role, session_id=origin_txid, chat_id=origin_chat),
                started_at=now,
            ))
        self.service.store.save(session)

    def _spawn_distiller(self, name: str, cwd: str, seed: str) -> Session:
        """Spawn a temporary interactive claude (tagged `temporary`, no `--chat` — we do not ingest
        the throwaway distiller), then seed it with the inline distill/summarize prompt (CHD4)."""
        spec = SpawnSpec.for_process(name=name, tags=[DISTILLER_TAG], cwd=cwd, cmd=DISTILLER_COMMAND)
        distiller = self.service.spawn(spec)
        self._seed(distiller.name, seed)
        return distiller

    def _seed(self, target: str, prompt: str) -> None:
        """The §7 minimal-seed delivery: wait for claude's input box, type the prompt literally,
        settle, then send Enter (an Enter that arrives too fast is dropped — COMMON.md). Never `-p`.
        Best-effort on readiness — if the marker never shows we still send (a slow first paint should
        not silently drop the seed)."""
        self._await_ready(target)
        self.service.tmux.send_keys(target, prompt, literal=True)
        time.sleep(SEED_SETTLE_SECONDS)
        self.service.tmux.send_keys(target, "Enter")

    def _await_ready(self, target: str) -> bool:
        """Poll `capture-pane` until claude's `❯ ` input box is drawn (§7). capture-pane has no
        Tmux-adapter method, so reuse the adapter's subprocess plumbing for this read rather than
        re-deriving the binary. Returns whether the marker appeared within the window."""
        for _ in range(READINESS_ATTEMPTS):
            code, out = self.service.tmux._run_quiet(["capture-pane", "-p", "-t", target])
            if code == 0 and READINESS_MARKER in out:
                return True
            time.sleep(READINESS_INTERVAL)
        return False

    def _detach_finish(self, finish_argv: list[str]) -> None:
        """Fire `tx <finish-verb> …` DETACHED (`start_new_session`, stdio → /dev/null), so it
        survives `respawn-pane -k` killing the caller's own pane (the rollover self-catch-up case,
        run from inside the very pane being rotated). Mirrors hooks.py's detached ingest: the child
        inherits this process's home + PYTHONPATH, and `sys.executable` is the same python3.14."""
        subprocess.Popen(
            [sys.executable, "-m", "tx", *finish_argv],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def _unique_name(self, base: str) -> str:
        """A tmux-unique session name: `base`, else `base-2`, `-3`, … (tmux names must be unique
        among live sessions, §9). An explicit name that clashes still gets suffixed."""
        if not self.service.tmux.has_session(base):
            return base
        for suffix in range(2, 100):
            candidate = f"{base}-{suffix}"
            if not self.service.tmux.has_session(candidate):
                return candidate
        raise ServiceError(f"could not find a free session name based on '{base}'")


def _env_prefix(env: dict[str, str]) -> str:
    """An `env K=V … ` prefix for a respawn command string. `respawn_pane` (frozen) takes no `-e`,
    so a rotated pane gets its fresh chat env baked into the command itself."""
    if not env:
        return ""
    pairs = " ".join(f"{shlex.quote(key)}={shlex.quote(value)}" for key, value in env.items())
    return f"env {pairs} "
