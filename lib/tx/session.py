"""Domain entities for tx-ide — the data model the interface-freeze locks (stage S0).

`Session`, `ChatRef`, `Origin`, `Location`, and the `Kind` / `Role` / `State` enums are
consumed by *every* later stage, so the shapes here are contracts. References:

  - tx-service-redesign.md §1 (object model) + §2 (per-role state model, D3/F2)
  - attachment-topology.md §2 (`Location` / `attached_to` — FROZEN, no `remote` field)
  - chat-ops.md §2 (`ChatRef` + `origin` provenance DAG; F7 `bundle_path` + `ended_at`)

Contracts honored here: D3 (per-role state incl. the `ALIVE` non-llm row, F2), D9-revised (the
explicit `engine` field supersedes the once-rejected `agent` field — schema v3, design §0/§1),
C1 (liveness is tmux `has-session` — `pid` is provenance only, never a liveness check),
C3 (`transition_to` refuses to leave a terminal state), OPEN-0b (`from_dict`
validates the schema version at the persistence boundary instead of raising a raw `TypeError`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# Bumped only when the on-disk record shape changes. There is NO back-migration (§9): the loader
# refuses any other version at the boundary (OPEN-0b) rather than silently mis-reading an older
# record. v3 added the explicit `engine` field (design §1); `tx migrate` upgrades v2 records in place.
SCHEMA_VERSION = 3

# Access-mode markers live in the already-persisted session environment, so v3 records remain
# readable across this additive behavior change. An absent marker is the writable default.
READ_ONLY_ENV = "TX_READ_ONLY"
REQUIRE_WORKTREE_ENV = "TX_REQUIRE_WORKTREE"


class UnsupportedRecordError(Exception):
    """A persisted record is not a current (v3) tx-ide record (OPEN-0b boundary guard).

    Raised by `Session.from_dict` on a `schema_version` mismatch. `SessionStore.all()` skips such
    records (one stale older-version file must not crash `ls`); `SessionStore.load()` lets it
    propagate (the caller asked for that specific record).
    """


class Kind(str, Enum):
    """Structural classification of a tmux session (§1)."""

    VIEW = "view"  # an outer "Views" home the user lives in; hosts nested sessions
    PROCESS = "process"  # a normal worker/agent/shell session


class Role(str, Enum):
    """What runs in the session (§1). Free-form scope lives in `tags`, not here (no first-tag
    convention anymore — the old records encoded role as the leading tag; v2 separates them)."""

    LLM = "llm"
    NVIM = "nvim"
    SHELL = "shell"
    OTHER = "other"


class State(str, Enum):
    """Combined liveness+activity state — **role-dependent** (D3).

    Non-llm sessions only ever use ALIVE / EXITED. Llm sessions add the activity axis
    WORKING / WAITING / IDLE plus ARCHIVED. WORKING / WAITING are llm-only — the picker's
    "needs you" coloring is gated on `role == llm` (§2). See `valid_for` / `initial_for`.
    """

    ALIVE = "alive"  # non-llm live session (F2 — the row D3 adds to today's §2 table)
    WORKING = (
        "working"  # llm: agent actively running a turn          (UserPromptSubmit hook)
    )
    WAITING = (
        "waiting"  # llm: turn finished, awaiting input — needs attention   (Stop hook)
    )
    IDLE = (
        "idle"  # llm: alive, no active turn (between chats / shell)  (SessionEnd/spawn)
    )
    EXITED = (
        "exited"  # the tmux session is gone     (Reconciler + tmux session-closed hook)
    )
    ARCHIVED = "archived"  # intentionally retired, record + history kept            (tx archive)

    @property
    def is_terminal(self) -> bool:
        """EXITED / ARCHIVED are absorbing — nothing transitions out of them (C3)."""
        return self in (State.EXITED, State.ARCHIVED)

    @classmethod
    def initial_for(cls, role: Role) -> State:
        """The state a freshly-spawned session starts in (§2 diagram): IDLE for an llm (it has no
        active turn yet), ALIVE for everything else."""
        return cls.IDLE if role == Role.LLM else cls.ALIVE

    @classmethod
    def valid_for(cls, role: Role) -> frozenset[State]:
        """The states a session of this role may legally hold (D3). Used by callers/tests to
        assert the per-role model; `transition_to` does not enforce it (it guards only C3)."""
        if role == Role.LLM:
            return frozenset(
                {cls.WORKING, cls.WAITING, cls.IDLE, cls.EXITED, cls.ARCHIVED}
            )
        return frozenset({cls.ALIVE, cls.EXITED})


class Engine(str, Enum):
    """Which coding-agent CLI drives an llm session — a separate axis from the `model` it runs
    (`opus` is a model; `claude` is the engine). `None` means no engine (a non-llm session). Lives
    here, not in `engines/` (which imports this), to avoid a circular import."""

    CLAUDE = "claude"
    CODEX = "codex"
    GEMINI = "gemini"  # reserved — not implemented yet; proves the seam is N-way (design §1, §9)


@dataclass(frozen=True)
class Location:
    """One pane currently surfacing a session — attachment-topology.md §2 (FROZEN).

    Self-contained: carries everything rendering and `jump` need so no per-render tmux lookup is
    required. `pane_id` ("%41") is the stable jump key (survives window renumbering); the indices
    and names are for display. **No `remote` field** (D-remote — ssh attaches are handled outside
    `attached_to`). On `Session`, `attached_to == []` means attached nowhere.

    S0 freezes this shape only; `Tmux.attached_to()` is a placeholder returning `[]` until S6
    builds the real TTY-join (attachment-topology §9), so no later stage churns when it lands.
    """

    host: str  # tmux session that OWNS the pane (the view/outer session, e.g. "Views")
    window_index: str  # display + secondary jump target
    window_name: str  # display (e.g. "work")
    pane_id: str  # "%41" — globally-unique, stable; the jump key
    pane_index: str  # display (e.g. "1")

    def to_dict(self) -> dict:
        return {
            "host": self.host,
            "window_index": self.window_index,
            "window_name": self.window_name,
            "pane_id": self.pane_id,
            "pane_index": self.pane_index,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Location:
        return cls(
            host=data["host"],
            window_index=data["window_index"],
            window_name=data["window_name"],
            pane_id=data["pane_id"],
            pane_index=data["pane_index"],
        )


@dataclass
class Origin:
    """Provenance edge for a `ChatRef` (chat-ops.md §2) — "which tx session created this chat,
    how, and from which parent chat." Walking `chat_id` backwards reconstructs a chat's lineage;
    walking `session_id` reconstructs which tx sessions touched it."""

    how: str  # spawn | fork | rollover | handover | resume
    session_id: str  # the tx session id that performed the op (a node in the DAG)
    chat_id: str | None = (
        None  # the source chat this derived from (None for spawn/original)
    )

    def to_dict(self) -> dict:
        return {"how": self.how, "session_id": self.session_id, "chat_id": self.chat_id}

    @classmethod
    def from_dict(cls, data: dict) -> Origin:
        return cls(
            how=data["how"], session_id=data["session_id"], chat_id=data["chat_id"]
        )


@dataclass
class ChatRef:
    """One conversation a tx session has hosted (chat-ops.md §2). Pointer-only, always maintained:
    the original chat plus every fork / rollover / handover lands one. F7: includes `bundle_path`
    (our ingested copy) and `ended_at`; `origin` includes `chat_id`."""

    id: str | None  # claude chat uuid; None while a fork capture is still pending
    role: str  # original | fork | rollover | handover
    cwd: str  # cwd the chat launched in (→ the munged transcript dir)
    transcript_path: str  # source path under the engine's transcript dir — may go stale
    origin: Origin
    bundle_path: str | None = (
        None  # our durable ingested copy: $TX_IDE_HOME/history/<tx>/<chat>/
    )
    started_at: float | None = None
    ended_at: float | None = None
    summary: str = ""  # cheap best-effort title (from sessions-index); optional
    engine: Engine | None = (
        None  # the engine that produced this chat (v3); migrator stamps it, spawn wires it in T1
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "role": self.role,
            "cwd": self.cwd,
            "transcript_path": self.transcript_path,
            "origin": self.origin.to_dict(),
            "bundle_path": self.bundle_path,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "summary": self.summary,
            "engine": self.engine.value if self.engine is not None else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ChatRef:
        return cls(
            id=data["id"],
            role=data["role"],
            cwd=data["cwd"],
            transcript_path=data["transcript_path"],
            origin=Origin.from_dict(data["origin"]),
            bundle_path=data["bundle_path"],
            started_at=data["started_at"],
            ended_at=data["ended_at"],
            summary=data["summary"],
            engine=Engine(data["engine"]) if data["engine"] is not None else None,
        )


@dataclass
class Session:
    """A tx-managed tmux session — the central entity (§1). The record file is `<id>.json`.

    `pid` is spawn provenance only: liveness is tmux `has-session` (C1), never a pid check. The
    `engine` field names the agent CLI of an llm session (schema v3, revisiting D9 — `None` for a
    non-llm session, and `None` on an llm record until T1 wires spawn to populate it). `state` is
    role-dependent (D3); `attached_to` is the frozen `Location` list (S6 fills it, S0 freezes it).
    """

    id: str  # uuid, primary key (the record file name)
    name: str  # human display name; tmux names a PROCESS by `id` (see tmux_name), D7
    kind: Kind
    role: Role
    state: State
    cwd: str = ""
    cmd: str = ""
    engine: Engine | None = (
        None  # the session's agent engine, if it has one (v3); None for non-llm
    )
    tags: list[str] = field(default_factory=list)  # free-form scope chips
    env: dict[str, str] = field(default_factory=dict)
    parent: str | None = None
    pid: int | None = None  # provenance only (C1)
    attached_to: list[Location] = field(default_factory=list)
    created_at: float | None = None
    ended_at: float | None = None
    last_activity: float | None = None
    chats: list[ChatRef] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    # ----- behavior -------------------------------------------------------------------------

    @property
    def tmux_name(self) -> str:
        return self.id if self.kind == Kind.PROCESS else self.name

    def is_alive(self) -> bool:
        """Whether the *record* is in a non-terminal state. NOTE: this reflects recorded state,
        not ground truth — true liveness is tmux `has-session` (C1), which the Reconciler diffs
        against to drive a stale record to EXITED."""
        return not self.state.is_terminal

    @property
    def needs_attention(self) -> bool:
        """The picker's "needs you" signal (§2): a WAITING llm session. Gated on role so a
        non-llm session never lights up."""
        return self.role == Role.LLM and self.state == State.WAITING

    @property
    def read_only(self) -> bool:
        """Whether this agent was launched in the explicit repository read-only mode."""
        return self.role == Role.LLM and self.env.get(READ_ONLY_ENV) == "1"

    def transition_to(self, new_state: State) -> bool:
        """Apply a state transition, honoring C3: terminal states (EXITED / ARCHIVED) are
        absorbing — once terminal, refuse to move anywhere else. This guards the late-async-hook
        race where a Stop hook fires after `session-closed` already marked the record dead.

        Returns True iff the state actually changed — S1a's `SessionService.record_state` uses
        this as the dirty-check (C4): only save + log on a real transition.
        """
        if new_state == self.state:
            return False
        if self.state.is_terminal:
            return False
        self.state = new_state
        return True

    def matches(self, query: str) -> bool:
        """Case-insensitive substring match over the fields a user filters on (name, role, kind,
        state, tags, cwd). Backs `SessionStore` text search / the picker's `-f` prefill."""
        if not query:
            return True
        needle = query.lower()
        haystack = " ".join(
            [
                self.name,
                self.role.value,
                self.kind.value,
                self.state.value,
                "read-only" if self.read_only else "writable",
                self.cwd,
                *self.tags,
            ]
        ).lower()
        return needle in haystack

    # ----- persistence ----------------------------------------------------------------------

    def to_dict(self) -> dict:
        """Serialize to the v3 on-disk shape. Enums → their string values (an unset `engine` → the
        JSON `null`, not a string); nested dataclasses → dicts. Carries the `engine` field
        (D9-revised)."""
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "name": self.name,
            "kind": self.kind.value,
            "role": self.role.value,
            "state": self.state.value,
            "cwd": self.cwd,
            "cmd": self.cmd,
            "engine": self.engine.value if self.engine is not None else None,
            "tags": list(self.tags),
            "env": dict(self.env),
            "parent": self.parent,
            "pid": self.pid,
            "attached_to": [location.to_dict() for location in self.attached_to],
            "created_at": self.created_at,
            "ended_at": self.ended_at,
            "last_activity": self.last_activity,
            "chats": [chat.to_dict() for chat in self.chats],
        }

    @classmethod
    def from_dict(cls, data: dict) -> Session:
        """Deserialize a persisted record. This is a system boundary (persisted data), so it
        validates the schema version here (OPEN-0b) — a non-v3 record raises a clear
        `UnsupportedRecordError`, never a raw `TypeError`/`KeyError`. Within a v3 record the shape
        is ours, so fields are read directly (no defensive defaults — DEVELOPER standard)."""
        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            raise UnsupportedRecordError(
                f"record schema_version={version!r} is unsupported (expected {SCHEMA_VERSION}); "
                "tx-ide does not back-migrate older records on load (§9) — run `tx migrate` to "
                "upgrade v2 records to v3"
            )
        return cls(
            id=data["id"],
            name=data["name"],
            kind=Kind(data["kind"]),
            role=Role(data["role"]),
            state=State(data["state"]),
            cwd=data["cwd"],
            cmd=data["cmd"],
            engine=Engine(data["engine"]) if data["engine"] is not None else None,
            tags=list(data["tags"]),
            env=dict(data["env"]),
            parent=data["parent"],
            pid=data["pid"],
            attached_to=[
                Location.from_dict(location) for location in data["attached_to"]
            ],
            created_at=data["created_at"],
            ended_at=data["ended_at"],
            last_activity=data["last_activity"],
            chats=[ChatRef.from_dict(chat) for chat in data["chats"]],
            schema_version=version,
        )
