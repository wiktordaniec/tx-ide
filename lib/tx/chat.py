"""Chat operations — fork / handover / rollover (stage S4).

Three operations on the conversation(s) behind a tx session, all transcription-based and **never
`-p`/headless** (CHD: `-p` bills credits; the user is on a subscription). The continuation always
starts from a DISTILLED brief/note — never the origin's full history — so it carries no baggage.
Each op appends a `ChatRef` with the right `origin` so the §11 history shows the full lineage.

  - **fork** (§4) — new tx session, FULL history. `claude --resume <src> --fork-session`, launched
    interactively. The fork mints its own new chat id at startup; tx records a PENDING `ChatRef`
    (`id=None`) and the capture hook fills it from the first payload (T4, hooks.py). `role=fork`,
    `origin→src`. Fork is the one op that deliberately keeps the full history — handover/rollover
    distill it away.
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
FROZEN `SessionService` / `Tmux` / `claude` surfaces and writes every op's PENDING `ChatRef`
synchronously (`id=None`); the capture hook (`hooks.py`, T4) fills its id + transcript_path from the
first payload.
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
from .engines import claude, registry
from .service import NotInsideTmux, ServiceError, SessionNotFound, SessionService
from .session import ChatRef, Engine, Origin, Session
from .spawn import SpawnSpec
from .storage import chat_ops_dir, history_dir, tx_ide_home

# The throwaway distiller's command is a FIXED per-engine command from the source's engine adapter
# (`registry.get(record.engine).distiller_command(seed)` — claude→opus/medium, codex→gpt-5.6-sol/high),
# not a claude-hardcoded constant. The distillation is the quality hinge of a handover/rollover, so
# each engine picks a model worth its judgement even though the mechanics are a read→write (T8b).
DISTILLER_TAG = "temporary"  # plus the op kind (handover|rollover) so the in-flight helper is visible

# Watchdog cadence. A detached `_chat-op-watch` polls for the distiller's artifact (brief/note); once
# it lands it gives the distiller a grace window to run the finish itself (the user's "distiller spawns
# the new session" happy path), then runs the idempotent finish deterministically and tears the
# distiller down. The artifact-driven wait means a slow opus distiller is never cut off early.
WATCH_POLL_INTERVAL = 2.0
WATCH_GRACE_SECONDS = 20.0
WATCH_TIMEOUT_SECONDS = 600.0


# ----- chat selection -----------------------------------------------------------------------

def active_chat(session: Session) -> ChatRef | None:
    """The chat a fork/handover/rollover targets: the last `ChatRef` carrying a real id, preferring
    one still open (`ended_at is None`). Ops append in order, so the last is the live thread."""
    candidates = [chat for chat in session.chats if chat.id is not None]
    if not candidates:
        return None
    open_chats = [chat for chat in candidates if chat.ended_at is None]
    return (open_chats or candidates)[-1]


def _inherited_env(session: Session) -> dict[str, str]:
    """The source session's env, inherited by a derived op's new session (fork / handover). Provenance
    no longer rides the env — each op records its own pending `ChatRef` and the hook captures the id
    from the payload (T4) — so this is a plain copy; `_spawn` overlays a fresh `TX_SESSION_ID`."""
    return dict(session.env)


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
    reads the same spec. Fields not used by a kind stay empty (handover ignores `pane`; rollover
    ignores `worker_name`). No chat id is carried — the successor's id is captured from its first hook
    payload (T4), so the op records a pending `ChatRef` the hook fills."""

    op_id: str
    kind: str  # "handover" | "rollover"
    source_txid: str
    source_chat: str
    cwd: str
    artifact_path: str  # the brief (handover) / note (rollover) the distiller writes; "" = self-catch-up
    self_catch_up: bool = False
    worker_name: str = ""   # handover
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
            "pane": self.pane, "distiller_name": self.distiller_name,
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

        spec = SpawnSpec.for_process(
            name=name, tags=list(source_session.tags), cwd=cwd,
            cmd=shlex.join(registry.get(source_session.engine).fork_command(source_session.cmd, source_chat.id)),
            env=_inherited_env(source_session), records_own_chat=True,
            engine=source_session.engine,
        )
        new_session = self.service.spawn(spec)

        self._record_fork_chat(new_session.id, cwd, source_session.id, source_chat.id)
        self.service.log.append("fork", f"{source_session.name} → {name} (chat pending)")
        return self.service.store.load(new_session.id)

    def _record_fork_chat(
        self, new_txid: str, cwd: str, source_txid: str, source_chat: str
    ) -> None:
        """Append the fork's PENDING `ChatRef` (id=None, role=fork, origin→source) to the new record.
        The fork mints its own id at startup; the capture hook fills it from the first payload (T4).
        Idempotent: skip if a fork ref for this source already exists (the hook never creates one —
        it only fills a pending ref — so this synchronous write is the sole creator)."""
        session = self.service.store.load(new_txid)
        if any(chat.role == "fork" and chat.origin.chat_id == source_chat for chat in session.chats):
            return
        session.chats.append(ChatRef(
            id=None,
            role="fork",
            cwd=cwd,
            transcript_path="",
            origin=Origin(how="fork", session_id=source_txid, chat_id=source_chat),
            started_at=time.time(),
            engine=session.engine,
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
        brief_path = history_dir() / source_session.id / f"handover-{_slug(task)}.md"

        spec = ChatOpSpec(
            op_id=str(uuid.uuid4()), kind="handover", source_txid=source_session.id,
            source_chat=source_chat.id, cwd=source_chat.cwd,
            artifact_path="" if self_catch_up else str(brief_path),
            self_catch_up=self_catch_up, worker_name=worker_name,
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
        self._spawn_distiller(distiller, "handover", source_chat.cwd, seed, source_session.engine)
        self._detach(["_chat-op-watch", spec.op_id])
        self.service.log.append("handover", f"{source_session.name} → {worker_name} (distilling)")
        return worker_name

    # ----- rollover (§6) -------------------------------------------------------------------

    def rollover(self, session: str | None = None, self_catch_up: bool = False) -> None:
        """Rotate the SAME tx session onto a fresh chat in the SAME pane (context exhausted). The
        default summarizes the current chat into a note via a temporary opus distiller, which then
        triggers `_chat-op-finish` (a detached watchdog backstops it); `--self-catch-up` fires the
        finish directly (detached). The successor mints its own chat id, captured from its first hook
        payload (T4), so there is no id to return."""
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
        note_path = self._next_rollover_note(record.id)

        spec = ChatOpSpec(
            op_id=str(uuid.uuid4()), kind="rollover", source_txid=record.id,
            source_chat=current_chat.id, cwd=current_chat.cwd,
            artifact_path="" if self_catch_up else str(note_path),
            self_catch_up=self_catch_up, pane=pane,
        )

        if self_catch_up:
            spec.save()
            # The caller may BE the pane being respawned — fire the finish detached so it survives
            # `respawn-pane -k` killing this process's pane.
            self._detach(["_chat-op-finish", spec.op_id])
            self.service.log.append("rollover", f"{record.name} (self-catch-up)")
            return

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
        self._spawn_distiller(distiller, "rollover", current_chat.cwd, seed, record.engine)
        self._detach(["_chat-op-watch", spec.op_id])
        self.service.log.append("rollover", f"{record.name} (distilling)")

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
        """Spawn the FRESH handover worker, seeded via its initial prompt to read only the brief (no
        origin baggage). Records a PENDING `ChatRef{role:handover}`; the worker's first hook captures
        its chat id from the payload (T4). `records_own_chat` so `_spawn` does not also add an
        `original` ref."""
        source = self.service.store.load(spec.source_txid)
        if source is None:
            raise SessionNotFound(f"_chat-op-finish: source record '{spec.source_txid}' not found")
        bundle = claude.bundle_dir(spec.source_txid, spec.source_chat)
        if self._artifact_missing(spec):
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
        launch = shlex.join(registry.get(source.engine).seed_command(source.cmd, seed))
        worker = self.service.spawn(SpawnSpec.for_process(
            name=spec.worker_name, tags=list(source.tags), cwd=spec.cwd, cmd=launch,
            env=_inherited_env(source), records_own_chat=True,
            engine=source.engine,
        ))
        self._record_seeded_chat(
            worker.id, spec.cwd, "handover", spec.source_txid, spec.source_chat
        )
        self.service.log.append("handover-finish", f"{spec.worker_name} (chat pending)")

    def _finish_rollover(self, spec: ChatOpSpec) -> None:
        """Rotate `pane` onto a fresh chat in place. Re-ingests the source's FINAL transcript first
        (so the predecessor bundle the successor catches up from includes anything that happened
        during the rollover window), then `respawn-pane -k` relaunches a fresh claude (no identity
        flag) with the note seeded as the initial prompt — or the bundle pointer when the note was
        never written (`_artifact_missing`). Appends a PENDING `ChatRef{role:rollover}` to the SAME
        record (the successor's first hook captures its id, T4) and closes the rotated-out chat."""
        record = self.service.store.load(spec.source_txid)
        if record is None:
            raise SessionNotFound(f"_chat-op-finish: record '{spec.source_txid}' not found")
        # Capture the predecessor's final state right before the hard kill (closes the snapshot→
        # respawn gap, so the successor can catch up on anything the note missed).
        history.ingest_session(self.service.store, spec.source_txid, wait=True)
        catch_up = claude.bundle_dir(spec.source_txid, spec.source_chat)

        if self._artifact_missing(spec):
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
        # The rotated pane keeps the SAME tx session, so its hook (TX_SESSION_ID) fills the pending
        # rollover ref. No chat-control env — provenance is on the ref, the id is captured (T4).
        env = {"TX_SESSION_ID": spec.source_txid}
        command = _env_prefix(env) + shlex.join(registry.get(record.engine).seed_command(record.cmd, seed))
        self.service.tmux.respawn_pane(spec.pane, command)
        self._record_seeded_chat(
            spec.source_txid, spec.cwd, "rollover", spec.source_txid,
            spec.source_chat, close_chat=spec.source_chat,
        )
        self.service.log.append("rollover-finish", f"{record.name} (chat pending)")

    def _artifact_missing(self, spec: ChatOpSpec) -> bool:
        """Whether the finish must fall back to the self-catch-up seed: the op was self-catch-up
        (no artifact by design), or the distiller never wrote its brief/note (it flaked and the
        watchdog force-finished, or it ran the finish before saving). A successor primed to read a
        nonexistent file starts from nothing — the bundle pointer is the working fallback (AND-171)."""
        return spec.self_catch_up or not Path(spec.artifact_path).exists()

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
        self, txid: str, cwd: str, role: str, origin_txid: str, origin_chat: str,
        close_chat: str | None = None,
    ) -> None:
        """Append a seeded op's PENDING `ChatRef` (handover worker / rollover successor) synchronously,
        so `tx chat ls` shows it immediately. Its id + transcript_path are captured from the
        successor's first hook payload (T4). `close_chat` stamps `ended_at` on the rotated-out chat
        (rollover) in the same save — the durable predecessor link the successor's `origin.chat_id`
        points back to for catch-up. Idempotent: skip if a pending ref for this role+origin exists."""
        now = time.time()
        session = self.service.store.load(txid)
        if close_chat is not None:
            for reference in session.chats:
                if reference.id == close_chat and reference.ended_at is None:
                    reference.ended_at = now
        already = any(
            reference.id is None and reference.role == role and reference.origin.chat_id == origin_chat
            for reference in session.chats
        )
        if not already:
            session.chats.append(ChatRef(
                id=None,
                role=role,
                cwd=cwd,
                transcript_path="",
                origin=Origin(how=role, session_id=origin_txid, chat_id=origin_chat),
                started_at=now,
                engine=session.engine,
            ))
        self.service.store.save(session)

    def _spawn_distiller(self, name: str, kind: str, cwd: str, seed: str, engine: Engine) -> Session:
        """Spawn the temporary distiller with its instructions baked in as the initial prompt (no
        send-keys). The command is the SOURCE engine's fixed distiller (`distiller_command(seed)` —
        claude→opus/medium, codex→gpt-5.6-sol/high; design §5), dispatched on the source's `engine`.
        Tagged `temporary` + the op kind so the in-flight helper is visible in `tx ls`. A plain llm
        spawn, so `_spawn` gives it a pending `original` ChatRef captured from its first hook (T4);
        the throwaway bundle is harmless."""
        spec = SpawnSpec.for_process(
            name=name, tags=[DISTILLER_TAG, kind], cwd=cwd,
            cmd=shlex.join(registry.get(engine).distiller_command(seed)),
            engine=engine,
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
