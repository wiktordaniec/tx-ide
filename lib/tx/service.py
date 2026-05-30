"""`SessionService` — the use-case core (stage S1a).

Every mutation flows through this one object, and **every mutation logs exactly ONE line via
`EventLog`** (D8 — the single chokepoint gives complete provenance). It composes `SessionStore`
(records) + `Tmux` (the live server) + `Reconciler` (liveness). **The method signatures here are
FROZEN** — S3/S4/S6 and the CLI consume them; a change is a coordinated re-sync.

Resolution (`_resolve`) accepts an id OR a tmux name — a live `@tx_id` first, then a store name
lookup — because names are reusable across non-concurrent sessions (D7). Service errors are real
errors (not control flow): the CLI catches them at the boundary and maps to a stderr line + exit 1.

See tx-service-redesign.md §1 (object model) + §4 (no-daemon) + §5 (hooks → record_state).
"""

from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

from . import claude
from .events import EventLog
from .reconcile import Reconciler
from .session import ChatRef, Origin, Session, State
from .spawn import SpawnSpec
from .storage import tx_ide_home
from .store import SessionStore
from .tmux import Tmux


class ServiceError(RuntimeError):
    """A use-case precondition failed — surfaced by the CLI as a message + exit 1."""


class SessionExists(ServiceError):
    pass


class SessionNotFound(ServiceError):
    pass


class NotInsideTmux(ServiceError):
    pass


class SessionService:
    def __init__(
        self,
        store: SessionStore | None = None,
        tmux: Tmux | None = None,
        log: EventLog | None = None,
        reconciler: Reconciler | None = None,
    ):
        self.store = store if store is not None else SessionStore()
        self.tmux = tmux if tmux is not None else Tmux()
        self.log = log if log is not None else EventLog()
        self.reconciler = (
            reconciler if reconciler is not None else Reconciler(self.store, self.tmux, self.log)
        )

    # ----- spawn ---------------------------------------------------------------------------

    def spawn(self, spec: SpawnSpec) -> Session:
        return self._spawn(spec)

    def spawn_nvim(self, spec: SpawnSpec) -> Session:
        return self._spawn(spec)

    def spawn_view(self, spec: SpawnSpec) -> Session:
        session = self._spawn(spec)
        # A view is a home base the user lives in: turn on its status bar and put pane borders at
        # window-top so nested panes get a labelled border (mirrors the old cmd_spawn_view).
        self.tmux.set_option(spec.name, "status", "on")
        self.tmux.set_window_option(spec.name, "pane-border-status", "top")
        return session

    def _spawn(self, spec: SpawnSpec) -> Session:
        """The shared spawn mechanics: create the detached session, set `@tx_id`, register the
        per-session `session-closed` hook carrying the uuid (C2), persist the record, log once."""
        if self.tmux.has_session(spec.name):
            raise SessionExists(f"session '{spec.name}' already exists")

        session_id = str(uuid.uuid4())
        now = time.time()
        launch_env = {"TX_SESSION_ID": session_id, **spec.env}
        chats: list[ChatRef] = []
        if spec.chat:
            chat_id = str(uuid.uuid4())
            launch_env["TX_CHAT_ID"] = chat_id
            chats.append(ChatRef(
                id=chat_id,
                role="original",
                cwd=spec.cwd,
                transcript_path=str(claude.transcript_path(chat_id, spec.cwd)),
                origin=Origin(how="spawn", session_id=session_id, chat_id=None),
                started_at=now,
            ))

        parent = self.tmux.current_session_name()
        pid = self.tmux.new_session(name=spec.name, cwd=spec.cwd, command=spec.cmd, env=launch_env)
        self.tmux.set_tx_id(spec.name, session_id)
        # C2: register the session-closed hook per-session, carrying the uuid (a reused name would
        # mis-stamp). NOTE (measured on tmux 3.6a): a session's OWN session-closed hook does not
        # fire after the session is gone — that hook must be GLOBAL. So the ACTIVE EXITED path is
        # reconcile-on-read (C1/C4), which stamps a vanished record within the picker's ~1 Hz
        # reload. This registration + the `_session-closed <uuid>` verb are the C2 wiring S2 builds
        # the (global) hook install on. Flagged to build-orchestrator. Harmless if it never fires.
        self.tmux.set_hook(spec.name, "session-closed", self._session_closed_command(session_id))

        session = Session(
            id=session_id,
            name=spec.name,
            kind=spec.kind,
            role=spec.role,
            state=State.initial_for(spec.role),
            cwd=spec.cwd,
            cmd=spec.cmd,
            tags=list(spec.tags),
            env=dict(spec.env),
            parent=parent,
            pid=pid,
            created_at=now,
            last_activity=now,
            chats=chats,
        )
        self.store.save(session)
        self.log.append("spawn", f"{spec.name} [{spec.role.value}] {spec.cwd}")
        return session

    def _session_closed_command(self, session_id: str) -> str:
        """The tmux `session-closed` hook value (C2): a baked invocation carrying the uuid (not the
        name — a reused name mis-stamps). Paths are resolved at registration (C9) because hooks run
        with a minimal env — `$TX_IDE_HOME`, the package on `PYTHONPATH`, the running interpreter."""
        invocation = (
            f"env TX_IDE_HOME={tx_ide_home()} PYTHONPATH={_package_lib()} "
            f"{sys.executable} -m tx _session-closed {session_id}"
        )
        return f'run-shell -b "{invocation}"'

    # ----- lifecycle -----------------------------------------------------------------------

    def kill(self, name_or_id: str) -> Session:
        """End the tmux session (if live) and mark the record EXITED. Idempotent with the
        `session-closed` hook the kill triggers — whichever runs second is a no-op transition."""
        session = self._require(name_or_id)
        if self.tmux.has_session(session.name):
            self.tmux.kill_session(session.name)
        if session.transition_to(State.EXITED):
            session.ended_at = time.time()
            session.attached_to = []
        self.store.save(session)
        self.log.append("kill", session.name)
        return session

    def archive(self, name_or_id: str) -> Session:
        """Retire a record to ARCHIVED, keeping it (and its history). History INGEST is S3, so for
        now this only marks the record (the design's `archive` = retire + ingest)."""
        session = self._require(name_or_id)
        if session.transition_to(State.ARCHIVED):
            session.ended_at = time.time()
            session.attached_to = []
        self.store.save(session)
        self.log.append("archive", session.name)
        return session

    def remove(self, name_or_id: str) -> bool:
        """Delete a record outright (`tx rm`). Returns whether a record was removed. Manual GC
        (D10 — no `prune`); leaves any live tmux session running."""
        session = self._resolve(name_or_id)
        if session is None:
            return False
        removed = self.store.delete(session.id)
        if removed:
            self.log.append("rm", f"{session.name} ({session.id})")
        return removed

    def tag(self, name_or_id: str, tags: list[str]) -> Session:
        session = self._require(name_or_id)
        session.tags = list(tags)
        self.store.save(session)
        self.log.append("tag", f"{session.name} {','.join(tags)}")
        return session

    def rename(self, name_or_id: str, new_name: str) -> Session:
        """Rename the tmux session and the record together. `@tx_id` and the `session-closed` hook
        ride along (rename-session keeps the same session), so liveness tracking is unbroken."""
        session = self._require(name_or_id)
        if self.tmux.has_session(new_name):
            raise SessionExists(f"session '{new_name}' already exists")
        previous = session.name
        if self.tmux.has_session(previous):
            self.tmux.rename_session(previous, new_name)
        session.name = new_name
        self.store.save(session)
        self.log.append("rename", f"{previous} → {new_name}")
        return session

    # ----- state (hook entry — C3 / C6) ----------------------------------------------------

    def record_state(self, session_id: str, new_state: State) -> bool:
        """Apply a hook-driven state change to ONE record. No-op when the id isn't ours (D4 — a
        hand-started `claude` carries an `@tx_id` we never recorded). Honors C3 (terminal states
        absorbing) + the C4 dirty-check via `Session.transition_to`. Accepts WAITING from either
        Stop or PermissionRequest (C6 — the event→state table is S2; here we just apply a state).
        Backs the internal `tx _session-closed <uuid>` verb (new_state = EXITED)."""
        session = self.store.load(session_id)
        if session is None:
            return False
        if not session.transition_to(new_state):
            return False
        now = time.time()
        if new_state == State.WORKING:
            session.last_activity = now  # turn start — the C5 stuck-WORKING clock
        if new_state.is_terminal:
            session.ended_at = now
        session.attached_to = [] if new_state.is_terminal else self.tmux.attached_to(session.name)
        self.store.save(session)
        self.log.append("state", f"{session.name} → {new_state.value}")
        return True

    # ----- messaging -----------------------------------------------------------------------

    def send_message(self, target: str, body: str) -> None:
        """Peer-message another session: wrap the body in the `<from-claude session="…">` envelope,
        type it into the target's active pane, pause, then send Enter (Claude Code's input box
        drops an Enter that arrives too fast — COMMON.md)."""
        if not self.tmux.has_session(target):
            raise SessionNotFound(f"target session '{target}' does not exist")
        sender = self.tmux.current_session_name()
        if sender is None:
            raise NotInsideTmux("send-message must run inside tmux (needs the sender session name)")
        self.tmux.send_keys(target, f'<from-claude session="{sender}">{body}</from-claude>')
        time.sleep(0.3)
        self.tmux.send_keys(target, "Enter")
        self.log.append("send-message", f"→ {target}")

    # ----- reads ---------------------------------------------------------------------------

    def reconcile(self) -> list[Session]:
        return self.reconciler.reconcile()

    def get(self, name_or_id: str) -> Session | None:
        return self._resolve(name_or_id)

    def focus_envelope(self, pane_id: str) -> str:
        # M-focus: the topology join lives in the Tmux adapter; S6 enriches with the record join.
        return self.tmux.focus_envelope(pane_id)

    # ----- resolution ----------------------------------------------------------------------

    def _resolve(self, token: str) -> Session | None:
        """Resolve a record by id (file lookup), then a live `@tx_id` for a session named `token`,
        then a store name lookup (D7 — names are reusable)."""
        by_id = self.store.load(token)
        if by_id is not None:
            return by_id
        live_id = self.tmux.get_tx_id(token)
        if live_id is not None:
            by_live = self.store.load(live_id)
            if by_live is not None:
                return by_live
        return self.store.find_by_name(token)

    def _require(self, token: str) -> Session:
        session = self._resolve(token)
        if session is None:
            raise SessionNotFound(f"session '{token}' not found (no live @tx_id, no store record)")
        return session


def _package_lib() -> Path:
    """The `lib/` dir that holds the `tx` package — what `PYTHONPATH` must point at so a baked hook
    can `python3.14 -m tx …`. `service.py` → `lib/tx` → `lib`."""
    return Path(__file__).resolve().parents[1]
