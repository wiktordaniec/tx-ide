"""Chat operations — fork / handover / rollover (stage S4).

Three operations on the conversation(s) behind a tx session, all transcription-based and **never
`-p`/headless** (CHD: `-p` bills credits; the user is on a subscription). The continuation always
starts from a DISTILLED brief/note — never the origin's full history — so it carries no baggage.
Each op appends a `ChatRef` with the right `origin` so the §11 history shows the full lineage.

  - **fork** (§4) — new tx session, FULL history. `claude --resume <src> --fork-session`, launched
    interactively (fork *cannot* pre-mint its id — `--session-id` is rejected with `--resume`, #5).
    The new chat uuid is captured by the origin-aware hook at the first Stop (CHD6); a brief
    snapshot-diff poll is a best-effort fast path. `role=fork`, `origin→src`. Fork is the one op that
    deliberately keeps the full history — handover/rollover distill it away.
  - **handover** (§5) — new tx session, DISTILLED brief. A temporary **opus** distiller reads the
    source bundle (source untouched), writes a focused brief, then triggers `_chat-op-finish`, which
    spawns a FRESH worker seeded to read only that brief. `role=handover`. `--self-catch-up` skips the
    distiller (the worker reads the bundle itself, CHD1).
  - **rollover** (§6) — SAME tx session, fresh chat in the SAME pane (context rotated). A temporary
    **opus** distiller summarizes the current chat into a note, then triggers `_chat-op-finish`, which
    re-ingests the source's FINAL transcript (closing the snapshot→respawn gap) and `respawn-pane -k`s
    the pane onto a FRESH chat, seeded to read the note and to catch up from the predecessor transcript
    if it looks truncated. SAME record; appends `ChatRef{role:rollover}`, closes the rotated-out chat.
    The tmux session never dies (IDLE→WORKING, no EXITED — §2). `--self-catch-up` fires the finish
    directly (detached).

Seeding is the claude **initial-prompt argument** (a positional prompt auto-submits in interactive
mode — measured), NOT tmux send-keys: the prompt is baked into the launch command, so there is no
readiness race and no dropped Enter. The distiller triggers completion with one short verb
`tx _chat-op-finish <op-id>` over a spec written under `$TX_IDE_HOME/chat-ops/`; the finish is
**idempotent** (an atomic `claim/` mkdir), and a detached `_chat-op-watch` backstops it — it runs the
finish if the distiller flaked, then tears the distiller down. This module orchestrates over the
FROZEN `SessionService` / `Tmux` / `claude` surfaces and writes every `ChatRef` synchronously; the
origin-aware hook (`hooks.py`, CHD6) is the idempotent backstop.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import history
from .engines import claude
from .service import NotInsideTmux, ServiceError, SessionNotFound, SessionService
from .session import ChatRef, Origin, Session
from .spawn import SpawnSpec
from .storage import chat_ops_dir, history_dir, tx_ide_home

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

# Fork id capture. chat-ops §1 #8: on claude ≥2.1 `--fork-session` writes the new `.jsonl` LAZILY on
# the first prompt, so a fork that has not been prompted yet has no file to snapshot. This poll is a
# cheap best-effort fast path (it catches the old-claude / already-written cases); the AUTHORITATIVE
# capture is the origin-aware hook completing the null-id placeholder at the first Stop (hooks.py).
FORK_POLL_ATTEMPTS = 6
FORK_POLL_INTERVAL = 0.5

# A lightweight throwaway distiller that reads the source transcript and writes a brief/note, then
# triggers the finish. **opus + medium effort** (was sonnet): the distillation is the quality hinge of
# a handover/rollover, so it is worth opus's judgement even though the mechanics are a read→write.
DISTILLER_COMMAND = "claude --model opus --effort medium --dangerously-skip-permissions"
DISTILLER_TAG = "temporary"  # plus the op kind (handover|rollover) so the in-flight helper is visible

# Watchdog cadence. A detached `_chat-op-watch` polls for the distiller's artifact (brief/note); once
# it lands it gives the distiller a grace window to run the finish itself (the user's "distiller spawns
# the new session" happy path), then runs the idempotent finish deterministically and tears the
# distiller down. The artifact-driven wait means a slow opus distiller is never cut off early.
WATCH_POLL_INTERVAL = 2.0
WATCH_GRACE_SECONDS = 20.0
WATCH_TIMEOUT_SECONDS = 600.0

# Identity flags stripped when reconstructing a launch command from a source session's `cmd`: the
# new op re-supplies its own (`--resume … --fork-session` for fork, `--session-id <new>` for a fresh
# chat). Everything else (model / effort / --append-system-prompt / skip-permissions) is inherited.
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
    binary = tokens[0] if tokens else claude.CLAUDE_BIN
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


def seeded_command(base_command: str, seed: str) -> str:
    """Append `seed` as claude's initial-prompt positional argument. A positional prompt auto-submits
    in interactive mode (measured), so this replaces tmux send-keys seeding entirely: the prompt is
    baked into the launch command — no readiness poll, no dropped Enter, fully deterministic.
    `claude.inject_session_id` keeps this tail verbatim when tx mints a chat id for the distiller."""
    return f"{base_command} {shlex.quote(seed)}"


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
    the SAME home this process resolved (the distiller's `tx` calls must hit the same home)."""
    return f"env TX_IDE_HOME={shlex.quote(str(tx_ide_home()))} {shlex.quote(_tx_program())}"


# ----- the op-spec (a distiller triggers completion by op-id, not a long exact command) ------

@dataclass
class ChatOpSpec:
    """Everything `_chat-op-finish` needs to complete a handover/rollover, written under
    `$TX_IDE_HOME/chat-ops/<op-id>/spec.json`. The distiller triggers the op with one short verb
    (`tx _chat-op-finish <op-id>`) instead of echoing a long exact command, and the detached watchdog
    reads the same spec. Fields not used by a kind stay empty (handover ignores `pane`/`new_chat`;
    rollover ignores `worker_name`/`worker_chat`)."""

    op_id: str
    kind: str  # "handover" | "rollover"
    source_txid: str
    source_chat: str
    cwd: str
    artifact_path: str  # the brief (handover) / note (rollover) the distiller writes; "" = self-catch-up
    self_catch_up: bool = False
    worker_name: str = ""   # handover
    worker_chat: str = ""   # handover
    new_chat: str = ""      # rollover
    pane: str = ""          # rollover
    distiller_name: str = ""

    @property
    def dir(self) -> Path:
        return chat_ops_dir() / self.op_id

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "spec.json").write_text(json.dumps(self.to_dict()))

    @classmethod
    def load(cls, op_id: str) -> ChatOpSpec:
        data = json.loads((chat_ops_dir() / op_id / "spec.json").read_text())
        return cls(**data)

    def to_dict(self) -> dict:
        return {
            "op_id": self.op_id, "kind": self.kind, "source_txid": self.source_txid,
            "source_chat": self.source_chat, "cwd": self.cwd, "artifact_path": self.artifact_path,
            "self_catch_up": self.self_catch_up, "worker_name": self.worker_name,
            "worker_chat": self.worker_chat, "new_chat": self.new_chat, "pane": self.pane,
            "distiller_name": self.distiller_name,
        }


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

        fork_chat_id = self._capture_fork_chat(cwd, before)
        self._record_fork_chat(new_session.id, cwd, source_session.id, source_chat.id, fork_chat_id)
        self.service.log.append(
            "fork", f"{source_session.name} → {name} (chat {(fork_chat_id or 'pending')[:8]})"
        )
        return self.service.store.load(new_session.id)

    def _capture_fork_chat(self, cwd: str, before: set[str]) -> str | None:
        """Poll the project dir briefly for a new `.jsonl` the fork wrote at startup, returning its
        uuid — or None (the common case on claude ≥2.1, where the fork writes only on first prompt;
        the hook then completes the placeholder). If several appear, the newest by mtime is the fork."""
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
        default spins a temporary opus distiller that writes the brief then triggers `_chat-op-finish`
        (a detached watchdog backstops it); `--self-catch-up` skips the distiller and seeds the worker
        to read the source bundle itself (CHD1). Returns the worker's name."""
        source_session = self.service.get(source)
        if source_session is None:
            raise SessionNotFound(f"handover: source session '{source}' not found")
        source_chat = active_chat(source_session)
        if source_chat is None:
            raise ServiceError(f"handover: '{source_session.name}' has no chat to hand over")

        # Mirror the source bundle so the distiller/worker reads the durable copy, not the live
        # session (source untouched). Blocking so the bundle is complete before we read.
        history.ingest_session(self.service.store, source_session.id, wait=True)

        worker_name = self._unique_name(new_name or f"{source_session.name}-handover")
        worker_chat = str(uuid.uuid4())
        brief_path = history_dir() / source_session.id / f"handover-{_slug(task)}.md"

        spec = ChatOpSpec(
            op_id=str(uuid.uuid4()), kind="handover", source_txid=source_session.id,
            source_chat=source_chat.id, cwd=source_chat.cwd,
            artifact_path="" if self_catch_up else str(brief_path),
            self_catch_up=self_catch_up, worker_name=worker_name, worker_chat=worker_chat,
        )

        if self_catch_up:
            spec.save()
            self.chat_op_finish(spec.op_id)
            self.service.log.append("handover", f"{source_session.name} → {worker_name} (self-catch-up)")
            return worker_name

        distiller = self._unique_name(f"{worker_name}-distill")
        spec.distiller_name = distiller
        spec.save()
        seed = (
            f"You are a tx-ide handover distiller (a temporary helper). Read the predecessor chat "
            f"bundle at {claude.bundle_transcript_path(source_session.id, source_chat.id)} . "
            f"Distill a focused, self-contained brief for this task and write it to {brief_path} : "
            f"«{task}». Capture only what the new worker needs to start — relevant context, current "
            f"state, constraints, and key file paths — not the whole history. When the brief file is "
            f"saved, run exactly this command and nothing else: "
            f"{_tx_invocation()} _chat-op-finish {shlex.quote(spec.op_id)}"
        )
        self._spawn_distiller(distiller, "handover", source_chat.cwd, seed)
        self._detach(["_chat-op-watch", spec.op_id])
        self.service.log.append("handover", f"{source_session.name} → {worker_name} (distilling)")
        return worker_name

    # ----- rollover (§6) -------------------------------------------------------------------

    def rollover(self, session: str | None = None, self_catch_up: bool = False) -> str:
        """Rotate the SAME tx session onto a fresh chat in the SAME pane (context exhausted). The
        default summarizes the current chat into a note via a temporary opus distiller, which then
        triggers `_chat-op-finish` (a detached watchdog backstops it); `--self-catch-up` fires the
        finish directly (detached). Returns the new chat uuid."""
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
        note_path = self._next_rollover_note(record.id)

        spec = ChatOpSpec(
            op_id=str(uuid.uuid4()), kind="rollover", source_txid=record.id,
            source_chat=current_chat.id, cwd=current_chat.cwd,
            artifact_path="" if self_catch_up else str(note_path),
            self_catch_up=self_catch_up, new_chat=new_chat, pane=pane,
        )

        if self_catch_up:
            spec.save()
            # The caller may BE the pane being respawned — fire the finish detached so it survives
            # `respawn-pane -k` killing this process's pane.
            self._detach(["_chat-op-finish", spec.op_id])
            self.service.log.append("rollover", f"{record.name} (self-catch-up, chat {new_chat[:8]})")
            return new_chat

        # Read the freshest bundle (blocking) so the summary reflects the latest turn.
        history.ingest_session(self.service.store, record.id, wait=True)
        distiller = self._unique_name(f"{record.name}-rollover-distill")
        spec.distiller_name = distiller
        spec.save()
        seed = (
            f"You are a tx-ide rollover summariser (a temporary helper). Read the predecessor chat "
            f"bundle at {claude.bundle_transcript_path(record.id, current_chat.id)} . Write a concise "
            f"hand-off note — the current task, what is done, the immediate next steps, and key files "
            f"and decisions — to {note_path} . When the note file is saved, run exactly this command "
            f"and nothing else: {_tx_invocation()} _chat-op-finish {shlex.quote(spec.op_id)}"
        )
        self._spawn_distiller(distiller, "rollover", current_chat.cwd, seed)
        self._detach(["_chat-op-watch", spec.op_id])
        self.service.log.append("rollover", f"{record.name} (distilling, chat {new_chat[:8]})")
        return new_chat

    # ----- the idempotent finish + its watchdog --------------------------------------------

    def chat_op_finish(self, op_id: str) -> None:
        """Complete a handover/rollover from its spec — the distiller's last act (one short verb) and
        the watchdog's fallback. **Idempotent**: an atomic `claim/` mkdir gives single execution, so
        a distiller and a watchdog racing both call this safely (the loser no-ops). Writes `done` when
        the continuation is live."""
        spec = ChatOpSpec.load(op_id)
        if (spec.dir / "done").exists():
            return
        try:
            (spec.dir / "claim").mkdir()
        except FileExistsError:
            return  # another finisher holds the claim
        if spec.kind == "handover":
            self._finish_handover(spec)
        else:
            self._finish_rollover(spec)
        (spec.dir / "done").write_text("")
        # Self-catch-up has no distiller and therefore no watchdog to tear the op down — clean up the
        # spec dir here. The distiller path leaves cleanup (and the distiller kill) to `_chat-op-watch`.
        if not spec.distiller_name:
            self._cleanup_op(spec)

    def _finish_handover(self, spec: ChatOpSpec) -> None:
        """Spawn the FRESH handover worker (pre-minted `--session-id`), seeded via its initial prompt
        to read only the brief (no origin baggage). Records `ChatRef{role:handover}`; the worker
        carries the origin env so the hook backstops the same ref on first prompt."""
        source = self.service.store.load(spec.source_txid)
        if source is None:
            raise SessionNotFound(f"_chat-op-finish: source record '{spec.source_txid}' not found")
        bundle = claude.bundle_dir(spec.source_txid, spec.source_chat)
        if spec.self_catch_up:
            seed = (
                f"You are taking over work via tx handover. There is no pre-written brief — read the "
                f"predecessor bundle at {bundle}/ (transcript.jsonl + subagents/ + tool-results/), "
                f"write yourself a short brief of the task and its state, then begin."
            )
        else:
            seed = (
                f"Your task brief is at {spec.artifact_path} — read it and begin. Fuller predecessor "
                f"history, only if the brief is insufficient: {bundle}/ ."
            )
        env = {
            **_inherited_env(source),
            CHAT_ID_ENV: spec.worker_chat,
            CHAT_ROLE_ENV: "handover",
            CHAT_ORIGIN_TXID_ENV: spec.source_txid,
            CHAT_ORIGIN_CHAT_ENV: spec.source_chat,
        }
        launch = seeded_command(fresh_command(source.cmd, spec.worker_chat), seed)
        worker = self.service.spawn(SpawnSpec.for_process(
            name=spec.worker_name, tags=list(source.tags), cwd=spec.cwd, cmd=launch, env=env,
        ))
        self._record_seeded_chat(
            worker.id, spec.worker_chat, spec.cwd, "handover", spec.source_txid, spec.source_chat
        )
        self.service.log.append("handover-finish", f"{spec.worker_name} (chat {spec.worker_chat[:8]})")

    def _finish_rollover(self, spec: ChatOpSpec) -> None:
        """Rotate `pane` onto a fresh chat in place. Re-ingests the source's FINAL transcript first
        (so the predecessor bundle the successor catches up from includes anything that happened
        during the rollover window), then `respawn-pane -k` relaunches claude with the fresh
        `--session-id` + env baked in and the note seeded as the initial prompt. Appends
        `ChatRef{role:rollover}` to the SAME record and closes the rotated-out chat."""
        record = self.service.store.load(spec.source_txid)
        if record is None:
            raise SessionNotFound(f"_chat-op-finish: record '{spec.source_txid}' not found")
        # Capture the predecessor's final state right before the hard kill (closes the snapshot→
        # respawn gap, so the successor can catch up on anything the note missed).
        history.ingest_session(self.service.store, spec.source_txid, wait=True)
        catch_up = claude.bundle_dir(spec.source_txid, spec.source_chat)

        if spec.self_catch_up:
            seed = (
                f"Continuing prior work in a fresh chat (rollover). The predecessor bundle is at "
                f"{catch_up}/ (transcript.jsonl + subagents/ + tool-results/) — read what you need to "
                f"resume, then continue."
            )
        else:
            seed = (
                f"Continuing prior work in a fresh chat (rollover). Hand-off note: {spec.artifact_path} "
                f"— read it and continue. If it looks truncated or you are missing the most recent "
                f"context, catch up from the predecessor transcript at {catch_up}/transcript.jsonl ."
            )
        env = {
            "TX_SESSION_ID": spec.source_txid,
            CHAT_ID_ENV: spec.new_chat,
            CHAT_ROLE_ENV: "rollover",
            CHAT_ORIGIN_TXID_ENV: spec.source_txid,
            CHAT_ORIGIN_CHAT_ENV: spec.source_chat,
        }
        command = _env_prefix(env) + seeded_command(fresh_command(record.cmd, spec.new_chat), seed)
        self.service.tmux.respawn_pane(spec.pane, command)
        self._record_seeded_chat(
            spec.source_txid, spec.new_chat, spec.cwd, "rollover", spec.source_txid,
            spec.source_chat, close_chat=spec.source_chat,
        )
        self.service.log.append("rollover-finish", f"{record.name} → chat {spec.new_chat[:8]}")

    def chat_op_watch(self, op_id: str) -> None:
        """Detached backstop for the distiller (CHD5). Polls for the distiller's artifact (brief/note);
        once it lands, gives the distiller a grace window to run the finish itself (the happy path),
        then runs the idempotent finish deterministically — so a flaked distiller never leaves the op
        half-done — and tears the distiller + spec down."""
        spec = ChatOpSpec.load(op_id)
        artifact = Path(spec.artifact_path) if spec.artifact_path else None
        deadline = time.time() + WATCH_TIMEOUT_SECONDS

        # 1) wait for the distiller to write its artifact (or finish on its own).
        while time.time() < deadline and not self._op_done(spec):
            if artifact is None or artifact.exists():
                break
            time.sleep(WATCH_POLL_INTERVAL)
        # 2) artifact present — let the distiller run the finish itself within the grace window.
        grace_end = time.time() + WATCH_GRACE_SECONDS
        while time.time() < grace_end and not self._op_done(spec):
            time.sleep(WATCH_POLL_INTERVAL)
        # 3) ensure completion: if no one has finished, do it ourselves; if the distiller holds the
        # claim (mid-finish), our `chat_op_finish` no-ops and we wait for its `done` before teardown
        # so we never kill a distiller spawning the worker.
        if not self._op_done(spec):
            self.chat_op_finish(op_id)
        for _ in range(int(WATCH_GRACE_SECONDS / WATCH_POLL_INTERVAL)):
            if self._op_done(spec):
                break
            time.sleep(WATCH_POLL_INTERVAL)
        self._cleanup_op(spec)

    def _op_done(self, spec: ChatOpSpec) -> bool:
        return (spec.dir / "done").exists()

    def _cleanup_op(self, spec: ChatOpSpec) -> None:
        """Kill the throwaway distiller (it has done its job — initial-prompt-seeded, it never needed
        to self-kill) and remove the op-spec dir. Best-effort: a missing session/dir is success."""
        if spec.distiller_name:
            try:
                self.service.kill(spec.distiller_name)
            except ServiceError:
                pass  # already gone
        shutil.rmtree(spec.dir, ignore_errors=True)

    # ----- pane / note resolution ----------------------------------------------------------

    def _resolve_pane(self, record: Session) -> str:
        """The pane to rotate. `$TMUX_PANE` when rollover runs from INSIDE the target (the common
        case); otherwise the target session's active pane (single-pane assumption, §9). Trust
        `$TMUX_PANE` only when it is the target's own session."""
        pane_env = os.environ.get("TMUX_PANE")
        if pane_env and self.service.tmux.current_session_name() == record.tmux_name:
            return pane_env
        pane = self.service.tmux.display_message("#{pane_id}", target=record.tmux_name)
        if pane is None:
            raise ServiceError(f"rollover: could not resolve a pane for '{record.name}'")
        return pane

    def _next_rollover_note(self, txid: str) -> Path:
        """`$TX_IDE_HOME/history/<txid>/rollover-<n>.md` — n past the existing rollover notes (CHD3,
        notes live in history, not the repo)."""
        directory = history_dir() / txid
        existing = list(directory.glob("rollover-*.md")) if directory.is_dir() else []
        return directory / f"rollover-{len(existing) + 1}.md"

    # ----- ChatRef + distiller spawn shared mechanics --------------------------------------

    def _record_seeded_chat(
        self, txid: str, chat_id: str, cwd: str, role: str, origin_txid: str, origin_chat: str,
        close_chat: str | None = None,
    ) -> None:
        """Append a seeded op's `ChatRef` (handover worker / rollover successor) synchronously, so
        `tx chat ls` shows it immediately and ingest finds it. Idempotent with the hook backstop:
        skip the append if a `ChatRef` for this chat id already exists. `close_chat` stamps
        `ended_at` on the rotated-out chat (rollover) in the same save — and is the durable
        predecessor link the successor's `origin.chat_id` points back to for catch-up."""
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

    def _spawn_distiller(self, name: str, kind: str, cwd: str, seed: str) -> Session:
        """Spawn the temporary distiller with its instructions baked in as the initial prompt (no
        send-keys). Tagged `temporary` + the op kind so the in-flight helper is visible in `tx ls`.
        Like every llm session it gets a chat id minted + `--session-id`-injected by `_spawn`
        (`inject_session_id` keeps the prompt tail verbatim); the throwaway bundle is harmless."""
        spec = SpawnSpec.for_process(
            name=name, tags=[DISTILLER_TAG, kind], cwd=cwd,
            cmd=seeded_command(DISTILLER_COMMAND, seed),
        )
        return self.service.spawn(spec)

    def _detach(self, argv: list[str]) -> None:
        """Fire `tx <verb> …` DETACHED (`start_new_session`, stdio → /dev/null), so it survives the
        caller's pane being killed (rollover self-catch-up) and never sits on the caller's latency
        path (the watchdog). The child inherits this process's home + PYTHONPATH; `sys.executable` is
        the same python3.14."""
        subprocess.Popen(
            [sys.executable, "-m", "tx", *argv],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def _unique_name(self, base: str) -> str:
        """A display name no LIVE record holds: `base`, else `base-2`, `-3`, … A worker is tmux-named
        by its id, so tmux no longer enforces name uniqueness (§9) — it is enforced against the store
        instead. An explicit clash still gets suffixed."""
        self.service.reconcile()
        taken = {session.name for session in self.service.store.all() if session.is_alive()}
        if base not in taken:
            return base
        for suffix in range(2, 100):
            candidate = f"{base}-{suffix}"
            if candidate not in taken:
                return candidate
        raise ServiceError(f"could not find a free session name based on '{base}'")


def _env_prefix(env: dict[str, str]) -> str:
    """An `env K=V … ` prefix for a respawn command string. `respawn_pane` (frozen) takes no `-e`,
    so a rotated pane gets its fresh chat env baked into the command itself."""
    if not env:
        return ""
    pairs = " ".join(f"{shlex.quote(key)}={shlex.quote(value)}" for key, value in env.items())
    return f"env {pairs} "
