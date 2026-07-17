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
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from .events import EventLog
from .engines import registry
from .messages import build_envelope
from .read_only import ReadOnlySandboxError, wrap_read_only_command
from .reconcile import Reconciler
from .session import (
    READ_ONLY_ENV,
    REQUIRE_WORKTREE_ENV,
    ChatRef,
    Engine,
    LlmSession,
    Origin,
    OtherSession,
    Role,
    Session,
    State,
)
from .spawn import SpawnSpec
from .store import SessionStore
from .tmux import Tmux, format_envelope
from .worktree import WorktreeError, WorktreeManager


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
        worktrees: WorktreeManager | None = None,
    ):
        self.store = store if store is not None else SessionStore()
        self.tmux = tmux if tmux is not None else Tmux()
        self.log = log if log is not None else EventLog()
        self.reconciler = (
            reconciler
            if reconciler is not None
            else Reconciler(self.store, self.tmux, self.log)
        )
        self.worktrees = worktrees if worktrees is not None else WorktreeManager()

    # ----- spawn ---------------------------------------------------------------------------

    def spawn(self, spec: SpawnSpec) -> Session:
        """Public spawn policy: every ordinary agent is a worker; non-agents launch directly."""
        return self.spawn_worker(spec) if spec.role == Role.LLM else self._spawn(spec)

    def spawn_worker(
        self,
        spec: SpawnSpec,
        *,
        reuse_existing_worktree: bool = False,
        before_spawn: Callable[[SpawnSpec], None] | None = None,
    ) -> Session:
        """Apply the agent placement invariant, then launch.

        Every worker gets a detached tx-owned worktree before tmux starts. Read-only workers bind
        tx's whole-process sandbox after the checkout's final path is known. Resume callers may
        explicitly reuse a linked worktree that already belongs to the continuing session.
        """
        if spec.role != Role.LLM:
            raise ServiceError("a worker spawn requires an agent command")

        environment = dict(spec.env)
        if spec.read_only:
            environment[READ_ONLY_ENV] = "1"
            environment.pop(REQUIRE_WORKTREE_ENV, None)
        else:
            environment.pop(READ_ONLY_ENV, None)
            environment[REQUIRE_WORKTREE_ENV] = "1"

        if reuse_existing_worktree and self.worktrees.is_linked(spec.cwd):
            prepared = self._prepare_worker_access(spec, Path(spec.cwd), environment)
            if before_spawn is not None:
                before_spawn(prepared)
            return self._spawn(prepared)

        self.reconcile()
        unavailable_names = {
            session.name for session in self.store.all() if session.is_alive()
        }
        try:
            name, worktree_directory = self.worktrees.create_unique(
                spec.cwd, spec.name, unavailable_names
            )
        except WorktreeError as error:
            raise ServiceError(f"could not create worktree: {error}") from error
        try:
            prepared = self._prepare_worker_access(
                replace(spec, name=name), worktree_directory, environment
            )
            if before_spawn is not None:
                before_spawn(prepared)
            return self._spawn(prepared)
        except Exception:
            try:
                self.worktrees.remove(worktree_directory)
            except WorktreeError:
                pass
            raise

    def _prepare_worker_access(
        self, spec: SpawnSpec, worktree_directory: Path, environment: dict[str, str]
    ) -> SpawnSpec:
        command = spec.cmd
        if spec.read_only:
            engine = spec.engine or Engine.CLAUDE
            adapter = registry.get(engine)
            if not adapter.is_read_only_command(command):
                raise ServiceError(
                    f"{engine.value} command does not enforce the requested read-only mode"
                )
        return replace(
            spec,
            cwd=str(worktree_directory),
            cmd=command,
            env=environment,
            launch_cmd=self.worker_launch_command(
                command, str(worktree_directory), spec.read_only
            ),
        )

    def worker_launch_command(
        self, command: str, worktree_directory: str, read_only: bool
    ) -> str:
        """Execution command for a worker; records keep the unwrapped engine command."""
        if not read_only:
            return command
        try:
            repository_worktrees = self.worktrees.repository_worktrees(worktree_directory)
            git_common_directory = self.worktrees.git_common_directory(worktree_directory)
            return wrap_read_only_command(
                command,
                worktree_directory,
                [str(path) for path in repository_worktrees],
                str(git_common_directory),
            )
        except (WorktreeError, ReadOnlySandboxError) as error:
            raise ServiceError(f"could not enforce read-only process sandbox: {error}") from error

    def next_worker_name(self, starting_directory: str, base_name: str) -> str:
        """Resolve the display/worktree name before an asynchronous handover is scheduled."""
        self.reconcile()
        unavailable_names = {
            session.name for session in self.store.all() if session.is_alive()
        }
        try:
            return self.worktrees.next_name(
                starting_directory, base_name, unavailable_names
            )
        except WorktreeError as error:
            raise ServiceError(f"could not resolve worktree name: {error}") from error

    def remove_worker_worktree(self, directory: str) -> None:
        """Remove a temporary worker's linked worktree after its tx session has ended."""
        if not self.worktrees.is_linked(directory):
            return
        try:
            self.worktrees.remove(Path(directory))
        except WorktreeError as error:
            raise ServiceError(f"could not remove worktree: {error}") from error

    def spawn_nvim(self, spec: SpawnSpec) -> Session:
        return self._spawn(spec)

    def spawn_view(self, spec: SpawnSpec) -> str:
        """Bring up a view — a home base the user lives in and nests other sessions into. Unlike a
        worker/agent/shell, a view is **not a store record**: it is a pure live tmux object marked
        by `@tx_view` (its whole durable identity — checked by the picker's nest-attach, the
        after-new-window border hook, the focus envelope, and `kill`'s view fallback). So this does
        NOT go through `_spawn`: it gets no `TX_SESSION_ID`/`@tx_id` (nothing keys on it — the
        reconciler only ever sees process records) and no tags (Q4). It replicates what `_spawn`
        gave a view — the `has_session` duplicate guard and ONE `EventLog` line (a view spawn must
        not vanish from the event history) — then applies the chrome (status bar + window-top pane
        borders so nested panes get a labelled border, mirroring the old cmd_spawn_view). Views are
        human-named, so the tmux session name is the human name. Returns that name for the CLI to
        print; there is no `Session` object to return."""
        if self.tmux.has_session(spec.name):
            raise SessionExists(f"session '{spec.name}' already exists")
        self.tmux.new_session(
            name=spec.name,
            cwd=spec.cwd,
            command=spec.launch_cmd or spec.cmd,
            env=dict(spec.env),
        )
        self.tmux.set_tx_view(spec.name)
        self.tmux.set_option(spec.name, "status", "on")
        self.tmux.set_window_option(spec.name, "pane-border-status", "top")
        self.log.append("spawn-view", f"{spec.name} {spec.cwd}")
        return spec.name

    def _spawn(self, spec: SpawnSpec) -> Session:
        """The shared spawn mechanics: create the detached session, set `@tx_id`, persist the
        record, log once. Liveness/EXITED is handled globally (C2 — see below), not per-session.

        Every record is a process now (views leave the store), so a session is always created in
        tmux under its `id` (Session.tmux_name) and the human `name` is a free, store-owned display
        label. Live human-name uniqueness — which used to fall out of tmux's own unique-session-name
        rule — is enforced against the store, since tmux only ever sees the collision-free id."""
        session_id = str(uuid.uuid4())
        tmux_name = session_id
        self._require_name_free(spec.name)
        if self.tmux.has_session(tmux_name):
            raise SessionExists(f"session '{tmux_name}' already exists")

        now = time.time()
        # An llm session's engine: set from the spawn spec, read off the record thereafter, never
        # re-derived from `cmd`. Unset ⇒ default Claude; a non-llm session has none.
        engine = (spec.engine or Engine.CLAUDE) if spec.role == Role.LLM else None
        launch_env = {"TX_SESSION_ID": session_id, **spec.env}
        chats: list[ChatRef] = []
        # Capture-after-launch (design §2/§7): every plain llm spawn gets a PENDING `original` ChatRef
        # — id + transcript_path are unknown until the first hook (`SessionStart` / `UserPromptSubmit`)
        # reads them off the payload (hooks.py). No pre-mint, no `--session-id` injection. A chat-op
        # that records its own ref (fork / handover / resume — `records_own_chat`) skips this.
        if spec.role == Role.LLM and not spec.records_own_chat:
            chats.append(
                ChatRef(
                    id=None,
                    role="original",
                    cwd=spec.cwd,
                    transcript_path="",
                    origin=Origin(how="spawn", session_id=session_id, chat_id=None),
                    started_at=now,
                    engine=engine,
                )
            )

        parent = self.tmux.current_session_name()
        pid = self.tmux.new_session(
            name=tmux_name,
            cwd=spec.cwd,
            command=spec.launch_cmd or spec.cmd,
            env=launch_env,
        )
        self.tmux.set_tx_id(tmux_name, session_id)
        # C2 (revised, measured on tmux 3.6a): NO per-session `session-closed` hook is registered
        # here. A session's OWN `session-closed` hook does not fire at its own close on 3.6a —
        # instead a surviving SIBLING's hook fires, carrying the sibling's uuid, so a per-session
        # hook would stamp a still-live session EXITED (permanent: terminal is absorbing + reconcile
        # skips terminal records). Liveness/EXITED is the global id-less `session-closed → reconcile`
        # hook (S2, hooks.py) plus reconcile-on-read (C1/C4): both diff `@tx_id` liveness over one
        # `list-sessions` and stamp only genuinely-vanished records, so neither can cross-fire.

        session: Session
        if spec.role == Role.LLM:
            # engine is non-None here (the llm branch of the line above); an LlmSession also carries
            # the pending `original` chat, a fresh last_activity, and an unarmed C5 turn clock.
            session = LlmSession(
                id=session_id,
                name=spec.name,
                state=State.initial_for(spec.role),
                cwd=spec.cwd,
                initial_cmd=spec.cmd,
                engine=engine or Engine.CLAUDE,
                tags=list(spec.tags),
                spawn_env=dict(spec.env),
                parent=parent,
                pid=pid,
                created_at=now,
                last_activity=now,
                turn_started_at=None,
                chats=chats,
            )
        else:
            # nvim / shell / other: none of the llm axis (no engine/chats/last_activity).
            session = OtherSession(
                id=session_id,
                name=spec.name,
                role=spec.role,
                state=State.initial_for(spec.role),
                cwd=spec.cwd,
                initial_cmd=spec.cmd,
                tags=list(spec.tags),
                spawn_env=dict(spec.env),
                parent=parent,
                pid=pid,
                created_at=now,
            )
        self.store.save(session)
        self.log.append("spawn", f"{spec.name} [{spec.role.value}] {spec.cwd}")
        return session

    def _require_name_free(self, name: str) -> None:
        """Refuse a spawn/rename onto a display name a LIVE record already holds — the human-name
        uniqueness that used to fall out of tmux's unique-session-name rule (now that a process is
        tmux-named by its id, tmux no longer enforces it, so name resolution stays unambiguous).
        Reconcile first so a vanished session's stale record does not block reuse (D7)."""
        self.reconcile()
        if any(s.name == name and s.is_alive() for s in self.store.all()):
            raise SessionExists(f"session '{name}' already exists")

    # ----- lifecycle -----------------------------------------------------------------------

    def kill(self, name_or_id: str) -> Session | None:
        """End the tmux session (if live) and mark the record EXITED. Idempotent with the
        `session-closed` hook the kill triggers — whichever runs second is a no-op transition.

        Q3: `kill` is the one view op tx keeps. A view is not a record, so when nothing resolves,
        fall back to a live `@tx_view` session by that name and end it (one log line, `None`
        returned — there is no record). This keeps `tx kill <view>` working so every kill still goes
        through tx (COMMON.md), without giving views any other tx verb."""
        session = self._resolve(name_or_id)
        if session is None:
            if self.tmux.has_session(name_or_id) and self.tmux.is_view(name_or_id):
                self.tmux.kill_session(name_or_id)
                self.log.append("kill", name_or_id)
                return None
            raise SessionNotFound(
                f"session '{name_or_id}' not found (no live @tx_id, no store record)"
            )
        if self.tmux.has_session(session.tmux_name):
            self.tmux.kill_session(session.tmux_name)
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
        session.attached_to = self.tmux.attached_to(
            session.tmux_name
        )  # ride-along snapshot (§4)
        self.store.save(session)
        self.log.append("tag", f"{session.name} {','.join(tags)}")
        return session

    def bind_artifact(self, session_id: str, artifact_id: str) -> Session:
        """Bind an nvim view session to the artifact it renders — the `OtherSession.artifact_id`
        back-link (v5). Backs `tx artifact open`: after the companion is spawned, this records which
        artifact it surfaces, so the session->artifact direction is first-class on the session record
        too (the artifact->sessions direction is `artifact.history`). Only ever called on the freshly
        spawned nvim `OtherSession`, so the narrowing always holds — asserted loudly (mirroring
        `record_state`'s `LlmSession` narrowing) rather than silently stashing the field on a base."""
        session = self._require(session_id)
        assert isinstance(session, OtherSession)
        session.artifact_id = artifact_id
        self.store.save(session)
        self.log.append("bind-artifact", f"{session.name} → {artifact_id}")
        return session

    def rename(self, name_or_id: str, new_name: str) -> Session:
        """Rename a session's DISPLAY name — a pure store write. tmux names every session by its
        (unchanging) id, so nothing moves in tmux and the pane border reflects the new name on its
        next ≤1s refresh. `@tx_id` is untouched, so the record link + liveness tracking are unbroken
        (liveness is the global id-less `session-closed` hook + reconcile-on-read). Views are not
        records (Q3 — a view has no tags and renaming one is not a tx concern), so a view name never
        resolves here."""
        session = self._require(name_or_id)
        previous = session.name
        if new_name == previous:
            return session
        self._require_name_free(new_name)
        session.name = new_name
        session.attached_to = self.tmux.attached_to(
            session.tmux_name
        )  # ride-along snapshot (§4)
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
            # Turn start: arm the C5 stuck-WORKING clock and bump activity. Both live on
            # LlmSession, and WORKING is an llm-only state reached only after hooks.dispatch has
            # narrowed the session to an LlmSession — so this narrowing always holds (and fails
            # loudly rather than silently stashing the fields on a non-llm record if it ever didn't).
            assert isinstance(session, LlmSession)
            session.turn_started_at = now
            session.last_activity = now
        if new_state.is_terminal:
            session.ended_at = now
        session.attached_to = (
            [] if new_state.is_terminal else self.tmux.attached_to(session.tmux_name)
        )
        self.store.save(session)
        self.log.append("state", f"{session.name} → {new_state.value}")
        return True

    # ----- messaging -----------------------------------------------------------------------

    def send_message(self, target: str, body: str) -> None:
        """Peer-message another session: wrap the body in the neutral `<from-agent session="…">`
        envelope, type it into the target's active pane, pause, then send Enter (the agent's input
        box drops an Enter that arrives too fast — COMMON.md). Both ends resolve through the store: the
        user addresses a PROCESS by its human name but tmux targets it by id, and the envelope must
        carry the sender's human name, not the raw `#S` (which is the sender's id for a worker)."""
        record = self._resolve(target)
        if record is None or not self.tmux.has_session(record.tmux_name):
            raise SessionNotFound(f"target session '{target}' does not exist")
        current = self.tmux.current_session_name()
        if current is None:
            raise NotInsideTmux(
                "send-message must run inside tmux (needs the sender session name)"
            )
        sender = self._resolve(current)
        sender_name = sender.name if sender is not None else current
        envelope = build_envelope(sender_name, body)
        self.tmux.send_keys(record.tmux_name, envelope)
        time.sleep(0.3)
        self.tmux.send_keys(record.tmux_name, "Enter")
        self.log.append("send-message", f"→ {record.name}")

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
            session.attached_to = attachment.get(session.tmux_name, [])
        return live

    def get(self, name_or_id: str) -> Session | None:
        return self._resolve(name_or_id)

    def focus_envelope(self, pane_id: str) -> str:
        """M-focus: the topology join lives in `Tmux` (one `attachment_map`); here we add the record
        join — the firing session's and the inner session's kind/tags for the assistant envelope.
        `session-kind` can no longer come from a record (there is no `kind` field, and the outer
        focus session is almost always the view, which has no record at all): it is derived from a
        live `@tx_view` read — `"view"` when marked, `"process"` otherwise. Empty when no pane."""
        attrs = self.tmux.focus_attrs(pane_id)
        if attrs is None:
            return ""
        self._add_record_attrs(
            attrs, attrs.get("session-name"), "session-kind", "session-tag"
        )
        if not attrs.get("inner-remote"):
            self._add_record_attrs(
                attrs,
                attrs.get("inner-session-name"),
                "inner-session-kind",
                "inner-session-tag",
            )
        return format_envelope(attrs)

    def _add_record_attrs(
        self, attrs: dict[str, str], name: str | None, kind_key: str, tag_key: str
    ) -> None:
        """Join a session's kind (+ tags for a process) into the envelope attrs. A live `@tx_view`
        session is a view — kind `"view"`, and NO tag attr (Q4: views carry no tags). Otherwise
        resolve the store record: kind `"process"` and its tags. No-op when the name is untracked
        (D4 — a hand-started session with no record and no marker)."""
        if not name:
            return
        if self.tmux.has_session(name) and self.tmux.is_view(name):
            attrs[kind_key] = "view"
            return
        record = self._resolve(name)
        if record is None:
            return
        attrs[kind_key] = "process"
        attrs[tag_key] = ",".join(record.tags)

    # ----- resolution ----------------------------------------------------------------------

    def _resolve(self, token: str) -> Session | None:
        """Resolve a record by id (file lookup), then a live `@tx_id` for a session named `token`,
        then a name lookup that prefers the LIVE same-name record (D7 — names are reusable)."""
        by_id = self.store.load(token)
        if by_id is not None:
            return by_id
        live_id = self.tmux.get_tx_id(token)
        if live_id is not None:
            by_live = self.store.load(live_id)
            if by_live is not None:
                return by_live
        return self._resolve_name(token)

    def _resolve_name(self, name: str) -> Session | None:
        """Name fallback: among records sharing `name` (reusable — D7), prefer the LIVE one, then the
        most recent. A PROCESS is tmux-named by its id, so the live-`@tx_id` path above can't match it
        by human name; without this a lingering exited husk (`find_by_name`'s first hit) shadows the
        live session — which is what broke `_tmux-name tx-assistant` and the prefix+/ comms path."""
        matches = [s for s in self.store.all() if s.name == name]
        if not matches:
            return None
        live = [s for s in matches if self.tmux.has_session(s.tmux_name)]
        return max(live or matches, key=lambda s: s.created_at or 0.0)

    def _require(self, token: str) -> Session:
        session = self._resolve(token)
        if session is None:
            raise SessionNotFound(
                f"session '{token}' not found (no live @tx_id, no store record)"
            )
        return session
