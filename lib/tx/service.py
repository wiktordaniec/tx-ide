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

import time
import uuid

from . import claude
from .events import EventLog
from .reconcile import Reconciler
from .session import ChatRef, Origin, Session, State
from .spawn import SpawnSpec
from .store import SessionStore
from .tmux import Tmux, format_envelope


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
        """The shared spawn mechanics: create the detached session, set `@tx_id`, persist the
        record, log once. Liveness/EXITED is handled globally (C2 — see below), not per-session."""
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
        # C2 (revised, measured on tmux 3.6a): NO per-session `session-closed` hook is registered
        # here. A session's OWN `session-closed` hook does not fire at its own close on 3.6a —
        # instead a surviving SIBLING's hook fires, carrying the sibling's uuid, so a per-session
        # hook would stamp a still-live session EXITED (permanent: terminal is absorbing + reconcile
        # skips terminal records). Liveness/EXITED is the global id-less `session-closed → reconcile`
        # hook (S2, hooks.py) plus reconcile-on-read (C1/C4): both diff `@tx_id` liveness over one
        # `list-sessions` and stamp only genuinely-vanished records, so neither can cross-fire.

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
        session.attached_to = self.tmux.attached_to(session.name)  # ride-along snapshot (§4)
        self.store.save(session)
        self.log.append("tag", f"{session.name} {','.join(tags)}")
        return session

    def rename(self, name_or_id: str, new_name: str) -> Session:
        """Rename the tmux session and the record together. `@tx_id` rides along (rename-session
        keeps the same session), so the record stays linked and liveness tracking is unbroken —
        liveness is the global id-less `session-closed` hook + reconcile-on-read, not a per-session
        hook that a rename could strand."""
        session = self._require(name_or_id)
        if self.tmux.has_session(new_name):
            raise SessionExists(f"session '{new_name}' already exists")
        previous = session.name
        if self.tmux.has_session(previous):
            self.tmux.rename_session(previous, new_name)
        session.name = new_name
        session.attached_to = self.tmux.attached_to(new_name)  # ride-along snapshot (§4)
        self.store.save(session)
        self.log.append("rename", f"{previous} → {new_name}")
        return session

    # ----- state (hook entry — C3 / C6) ----------------------------------------------------

    def record_state(self, session_id: str, new_state: State) -> bool:
        """Apply a hook-driven state change to ONE record. No-op when the id isn't ours (D4 — a
        hand-started `claude` carries an `@tx_id` we never recorded). Honors C3 (terminal states
        absorbing) + the C4 dirty-check via `Session.transition_to`. Accepts WAITING from either
        Stop or PermissionRequest (C6 — the event→state table is S2; here we just apply a state)."""
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

    def live_sessions(self) -> list[Session]:
        """Reconcile, then return the live records with a FRESH `attached_to` stamped in memory —
        the display source of truth (compute-on-read, attachment-topology §4). ONE `attachment_map`
        sweep feeds every row. This snapshot is NOT persisted on its own: an attachment delta never
        dirties a record (the chattiness the design rejects), it only rides along an actual mutation
        (record_state / tag / rename) or the exit clear. Backs `tx ls` + the picker feed."""
        self.reconcile()
        attachment = self.tmux.attachment_map()
        live = [session for session in self.store.all() if session.is_alive()]
        for session in live:
            session.attached_to = attachment.get(session.name, [])
        return live

    def get(self, name_or_id: str) -> Session | None:
        return self._resolve(name_or_id)

    def focus_envelope(self, pane_id: str) -> str:
        """M-focus: the topology join lives in `Tmux` (one `attachment_map`); here we add the record
        join — the firing session's and the inner session's kind/tags from the store (the old
        `build_envelope`'s `tx-session-state` lookups, now on the v2 record). Empty when no pane."""
        attrs = self.tmux.focus_attrs(pane_id)
        if attrs is None:
            return ""
        self._add_record_attrs(attrs, attrs.get("session-name"), "session-kind", "session-tag")
        if not attrs.get("inner-remote"):
            self._add_record_attrs(
                attrs, attrs.get("inner-session-name"), "inner-session-kind", "inner-session-tag"
            )
        return format_envelope(attrs)

    def _add_record_attrs(
        self, attrs: dict[str, str], name: str | None, kind_key: str, tag_key: str
    ) -> None:
        """Join a session's stored kind/tags into the envelope attrs (no-op when untracked — D4)."""
        if not name:
            return
        record = self._resolve(name)
        if record is None:
            return
        attrs[kind_key] = record.kind.value
        attrs[tag_key] = ",".join(record.tags)

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
