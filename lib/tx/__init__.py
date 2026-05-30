"""tx-ide — an object-oriented Python core for tmux + Claude Code session management.

Stage **S0 (Foundation)**: the data model, persistence, and adapter skeletons that every later
stage depends on and that the interface freeze locks. See `.claude/plans/execution-plan.md` (S0)
and `.claude/plans/tx-service-redesign.md`.

Public API frozen here:
  - `Session` + `ChatRef` / `Origin` / `Location` + `Kind` / `Role` / `State` (session.py)
  - `SessionStore` — filesystem-direct repository (store.py)
  - `Storage` / `LocalStorage` / `S3Storage` + the `$TX_IDE_HOME` layout helpers (storage.py)
  - `EventLog` — the D8 provenance log (events.py)
  - the `claude` module — Claude paths / launch flags / bundle layout (claude.py)

Later stages add (not in S0): service.py, reconcile.py, tmux.py, spawn.py, cli.py, render.py,
history.py, chat.py, hooks.py.
"""

from . import claude
from .events import EventLog
from .session import (
    SCHEMA_VERSION,
    ChatRef,
    Kind,
    Location,
    Origin,
    Role,
    Session,
    State,
    UnsupportedRecordError,
)
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
)
from .store import SessionStore

__version__ = "0.0.0-s0"

__all__ = [
    "SCHEMA_VERSION",
    "__version__",
    "ChatRef",
    "EventLog",
    "Kind",
    "Location",
    "LocalStorage",
    "Origin",
    "Role",
    "S3Storage",
    "Session",
    "SessionStore",
    "State",
    "Storage",
    "UnsupportedRecordError",
    "claude",
    "config_path",
    "DEFAULT_HOME",
    "ensure_home",
    "history_dir",
    "hooks_dir",
    "log_path",
    "sessions_dir",
    "tx_ide_home",
    "user_agents_dir",
]
