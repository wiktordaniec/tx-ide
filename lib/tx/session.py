"""Domain entities for tx-ide — the data model the interface-freeze locks (stage S0).

`Session` (with its `LlmSession` / `OtherSession` subtypes), `ChatRef`, `Origin`, `Location`, and
the `Role` / `State` enums are consumed by *every* later stage, so the shapes here are contracts.
References:

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
# record. v4 split the one record shape into role-discriminated types and took views out of the
# store: it drops the dead `kind` key, drops the non-llm `engine`/`chats`/`last_activity` keys, and
# deletes view records. v5 adds a nullable `artifact_id` back-link to `OtherSession` ONLY — an nvim
# view opened on an artifact (Plan 2, sequencing step 3); no other role gains a field. v6 adds the
# nullable `group` override to the shared base — only overrides are stored; the effective group is
# resolved at read time (grouping.py). `tx migrate` chains v3 -> v4 -> v5 -> v6 in one run.
SCHEMA_VERSION = 6

# Access-mode markers live in the already-persisted session environment, so v3 records remain
# readable across this additive behavior change. An absent marker is the writable default.
READ_ONLY_ENV = "TX_READ_ONLY"
REQUIRE_WORKTREE_ENV = "TX_REQUIRE_WORKTREE"


class UnsupportedRecordError(Exception):
    """A persisted record is not a current (v4) tx-ide record (OPEN-0b boundary guard).

    Raised by `Session.from_dict` on a `schema_version` mismatch. `SessionStore.all()` skips such
    records (one stale older-version file must not crash `ls`); `SessionStore.load()` lets it
    propagate (the caller asked for that specific record).
    """


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
    """A tx-managed tmux session — the shared **base** of the role-discriminated hierarchy (§1).
    The record file is `<id>.json`. Only the work fields every consumer reads uniformly live here;
    the llm-only axis (`engine` / `chats` / `last_activity` / `turn_started_at`) lives on
    `LlmSession`, and `role` is supplied by each subtype (a read-only `Role.LLM` property on
    `LlmSession`, a plain field on `OtherSession`) so a uniform reader can duck-type `session.role`.

    The base is abstract by convention — `from_dict` and `_spawn` only ever build a concrete
    subtype. Reading `self.role` (or `self.chats`) on a bare base instance is a loud
    `AttributeError`, which is the point: the split makes the llm axis structurally unreachable off
    a non-llm session instead of a silent `None`.

    `pid` is spawn provenance only: liveness is tmux `has-session` (C1), never a pid check. `state`
    is role-dependent (D3); `attached_to` is the frozen `Location` list (S6 fills it, S0 freezes
    it). Every record is a process — views are no longer persisted (they are live `@tx_view` tmux
    objects), so `tmux_name` is always the id.
    """

    id: str  # uuid, primary key (the record file name)
    name: str  # human display name; tmux always names a session by `id` (see tmux_name), D7
    state: State
    cwd: str = ""
    initial_cmd: str = ""  # the resolved engine/launch command (JSON key stays "cmd")
    tags: list[str] = field(default_factory=list)  # free-form scope chips
    # v6: explicit effort-group override; None = derived at read time (grouping.py).
    group: str | None = None
    spawn_env: dict[str, str] = field(
        default_factory=dict
    )  # spawn-time environment (JSON key stays "env")
    # Work ancestor: chat-ops record the SOURCE session id; a plain spawn its managed executor.
    parent: str | None = None
    pid: int | None = None  # provenance only (C1)
    attached_to: list[Location] = field(default_factory=list)
    created_at: float | None = None
    ended_at: float | None = None
    schema_version: int = SCHEMA_VERSION

    # ----- behavior -------------------------------------------------------------------------

    @property
    def tmux_name(self) -> str:
        """Every record is a process, and a process is tmux-named by its id (D7)."""
        return self.id

    @property
    def activity_at(self) -> float:
        """Uniform recency key for every sort/tiebreak across both subtypes. The base has no
        activity signal, so it falls back to spawn time; `LlmSession` overrides this to prefer its
        real `last_activity`. Always a float (never `None`) so callers can sort without guarding."""
        return self.created_at or 0.0

    def is_alive(self) -> bool:
        """Whether the *record* is in a non-terminal state. NOTE: this reflects recorded state,
        not ground truth — true liveness is tmux `has-session` (C1), which the Reconciler diffs
        against to drive a stale record to EXITED."""
        return not self.state.is_terminal

    @property
    def needs_attention(self) -> bool:
        """The picker's "needs you" signal (§2): a WAITING llm session. Role-gated (via the
        subtype's `role`) so a non-llm session never lights up. Stays on the base because
        `sessions-graph`'s `session_payload` reads it on every session before the llm filter."""
        return self.role == Role.LLM and self.state == State.WAITING

    @property
    def read_only(self) -> bool:
        """Whether this agent was launched in the explicit repository read-only mode."""
        return self.role == Role.LLM and self.spawn_env.get(READ_ONLY_ENV) == "1"

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
        """Case-insensitive substring match over the fields a user filters on (name, role, state,
        tags, cwd). Backs `SessionStore` text search / the picker's `-f` prefill."""
        if not query:
            return True
        needle = query.lower()
        haystack = " ".join(
            [
                self.name,
                self.role.value,
                self.state.value,
                "read-only" if self.read_only else "writable",
                self.cwd,
                *self.tags,
            ]
        ).lower()
        return needle in haystack

    # ----- persistence ----------------------------------------------------------------------

    def to_dict(self) -> dict:
        """Serialize the shared work fields to the v4 on-disk shape. `role` comes from the subtype;
        the attribute renames keep their legacy JSON keys (`initial_cmd` → `"cmd"`, `spawn_env` →
        `"env"`). Subtypes extend this: `LlmSession` adds the llm axis, `OtherSession` adds
        nothing (no `kind`/`engine`/`chats`/`last_activity` keys)."""
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "name": self.name,
            "role": self.role.value,
            "state": self.state.value,
            "cwd": self.cwd,
            "cmd": self.initial_cmd,
            "tags": list(self.tags),
            "group": self.group,
            "env": dict(self.spawn_env),
            "parent": self.parent,
            "pid": self.pid,
            "attached_to": [location.to_dict() for location in self.attached_to],
            "created_at": self.created_at,
            "ended_at": self.ended_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Session:
        """Deserialize a persisted record — the role-dispatching **factory** for the hierarchy.
        This is a system boundary (persisted data), so it validates the schema version here
        (OPEN-0b): a non-current record raises a clear `UnsupportedRecordError`, never a raw
        `TypeError`/`KeyError`. `role == "llm"` builds an `LlmSession` (`engine`/`chats`/
        `last_activity` are guaranteed present on such records; `turn_started_at` is read via
        `.get()` because a v4 record written before the session's first post-split turn lacks it);
        every other role builds an `OtherSession` (`artifact_id` is read via `.get()` for the same
        reason — it is the v5 addition, absent on a v3/v4 record the migrator is upgrading in one
        pass). Within a current record the shape is ours, so fields are read directly (no defensive
        defaults — DEVELOPER standard)."""
        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            raise UnsupportedRecordError(
                f"record schema_version={version!r} is unsupported (expected {SCHEMA_VERSION}); "
                "tx-ide does not back-migrate older records on load (§9) — run `tx migrate` to "
                "upgrade older records"
            )
        common = dict(
            id=data["id"],
            name=data["name"],
            state=State(data["state"]),
            cwd=data["cwd"],
            initial_cmd=data["cmd"],
            tags=list(data["tags"]),
            # `.get()`: the v6 addition, absent mid-migration (same rationale as artifact_id).
            group=data.get("group"),
            spawn_env=dict(data["env"]),
            parent=data["parent"],
            pid=data["pid"],
            attached_to=[
                Location.from_dict(location) for location in data["attached_to"]
            ],
            created_at=data["created_at"],
            ended_at=data["ended_at"],
            schema_version=version,
        )
        if data["role"] == Role.LLM.value:
            return LlmSession(
                **common,
                engine=Engine(data["engine"]),
                chats=[ChatRef.from_dict(chat) for chat in data["chats"]],
                last_activity=data["last_activity"],
                turn_started_at=data.get("turn_started_at"),
            )
        return OtherSession(
            **common, role=Role(data["role"]), artifact_id=data.get("artifact_id")
        )


@dataclass
class LlmSession(Session):
    """A session driven by a coding-agent CLI (`role == llm`). Carries the llm-only axis the
    engine / chat / hook machinery reads unconditionally, so the split makes those reads
    structurally safe: `engine` is the agent CLI (non-optional — every llm record has one), `chats`
    is the conversation-provenance list, `last_activity` is the last recorded turn time, and
    `turn_started_at` is the C5 stuck-`WORKING` clock (armed when a turn starts, read by the
    Reconciler)."""

    engine: Engine = Engine.CLAUDE  # non-optional; a real engine is always passed at spawn/load
    chats: list[ChatRef] = field(default_factory=list)
    last_activity: float | None = None
    turn_started_at: float | None = None  # C5 clock; None on a record written before its first turn

    @property
    def role(self) -> Role:
        """Fixed by type — an `LlmSession` is always `llm`. A read-only property (not a field) so
        the generated `__init__` never accepts or overwrites it: a same-named dataclass field would
        collide with a data descriptor and break construction."""
        return Role.LLM

    @property
    def activity_at(self) -> float:
        """Prefer the real last-turn time; fall back to spawn time when no turn has run yet."""
        return self.last_activity or self.created_at or 0.0

    def to_dict(self) -> dict:
        data = super().to_dict()
        data["engine"] = self.engine.value
        data["last_activity"] = self.last_activity
        data["chats"] = [chat.to_dict() for chat in self.chats]
        data["turn_started_at"] = self.turn_started_at
        return data


@dataclass
class OtherSession(Session):
    """A non-llm work session (nvim / shell / other). It uses none of the llm axis: `role` is a
    plain field set from disk (immutable by convention — nothing mutates it, so there is no setter
    to remove), and `chats` is a read-only empty property so the uniform picker/ls loops that read
    `len(session.chats)` stay branch-free.

    `artifact_id` (v5) is the one mutable extension: an nvim view opened on an artifact via
    `tx artifact open` records which artifact it renders (Plan 2 step 3) — nullable, and set only on
    that flow; every other `OtherSession` leaves it `None`. It lives here rather than on a new
    subtype (the settled lean — no `ArtifactViewSession`)."""

    role: Role = Role.OTHER  # nvim / shell / other, set from disk at load
    artifact_id: str | None = None  # v5: the artifact an nvim view renders (Plan 2 step 3); else None

    @property
    def chats(self) -> list[ChatRef]:
        """Non-llm sessions host no chats; a read-only `[]` keeps uniform readers branch-free."""
        return []

    def to_dict(self) -> dict:
        """Extend the shared shape with the v5 `artifact_id` back-link (null for every non-view
        session). `LlmSession` never carries this key — the field is `OtherSession`-only."""
        data = super().to_dict()
        data["artifact_id"] = self.artifact_id
        return data
