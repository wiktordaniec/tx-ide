"""tx-ide — an object-oriented Python core for tmux + Claude Code session management.

Stages **S0 (Foundation)** + **S1a (Service + non-interactive CLI)**: the data model, persistence,
adapters, and the application core that every later stage depends on and that the interface freeze
locks. See `.claude/plans/execution-plan.md` (S0/S1a) and `.claude/plans/tx-service-redesign.md`.

Public API frozen here:
  - S0 data model: `Session` (+ `LlmSession` / `OtherSession`) + `ChatRef` / `Origin` / `Location`
    + `Role` / `State` / `Engine` (the agent-engine axis — the adapter protocol/registry live in
    `tx.engines`)
  - S0 persistence: `SessionStore` (filesystem-direct), `Storage` / `LocalStorage` / `S3Storage`
    + the `$TX_IDE_HOME` layout helpers, `EventLog` (D8 log); the engine adapters live in `tx.engines`
  - S1a core: `SessionService` (the mutation chokepoint), `Tmux` (the tmux adapter, attachment
    reads are S6 placeholders), `Reconciler` (no-daemon liveness), `SpawnSpec` (+ `infer_role`)

Later stages add: history.py (S3), chat.py (S4), hooks.py (S2). cli.py / render.py are the
presentation layer (not part of the frozen import surface).
"""

from .events import EventLog
from .reconcile import Reconciler
from .service import (
    NotInsideTmux,
    ServiceError,
    SessionExists,
    SessionNotFound,
    SessionService,
)
from .session import (
    SCHEMA_VERSION,
    ChatRef,
    Engine,
    LlmSession,
    Location,
    Origin,
    OtherSession,
    Role,
    Session,
    State,
    UnsupportedRecordError,
)
from .spawn import SpawnSpec, infer_role
from .storage import (
    DEFAULT_HOME,
    LocalStorage,
    S3Storage,
    Storage,
    config_path,
    ensure_home,
    history_dir,
    hooks_dir,
    log_path,
    sessions_dir,
    tx_ide_home,
    user_agents_dir,
    worktrees_dir,
)
from .store import SessionStore
from .tmux import Tmux, TmuxError

__version__ = "0.0.0-s1a"

__all__ = [
    "SCHEMA_VERSION",
    "__version__",
    "ChatRef",
    "Engine",
    "EventLog",
    "LlmSession",
    "Location",
    "LocalStorage",
    "NotInsideTmux",
    "Origin",
    "OtherSession",
    "Reconciler",
    "Role",
    "S3Storage",
    "ServiceError",
    "Session",
    "SessionExists",
    "SessionNotFound",
    "SessionService",
    "SessionStore",
    "SpawnSpec",
    "State",
    "Storage",
    "Tmux",
    "TmuxError",
    "UnsupportedRecordError",
    "config_path",
    "DEFAULT_HOME",
    "ensure_home",
    "history_dir",
    "hooks_dir",
    "infer_role",
    "log_path",
    "sessions_dir",
    "tx_ide_home",
    "user_agents_dir",
    "worktrees_dir",
]
