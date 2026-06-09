"""`Reconciler` — the no-daemon liveness sweep (stage S1a).

This is what replaces a resident process. Every read (`ls` / `_list` / the picker) runs
`reconcile()`, which drives stored records to ground truth against ONE `tmux list-sessions`:

  - **C1** — that single `list-sessions` is the SOLE liveness signal; `pid` is NEVER checked.
  - **C4** — bounded: one server scan diffed in memory (never `has-session` per record), terminal
    records skipped, and a **dirty-check** (via `Session.transition_to`'s bool) so the ~1 Hz picker
    reload only `save()`s + logs on a real transition.
  - **C5** — a stuck `WORKING` llm (last activity past the threshold AND the pane no longer running
    the agent) is demoted to `IDLE` (agent-crashed-but-pane-alive).
  - **C11** — remote `--host`/`--all` sessions have no local record, so the Reconciler never sees
    them; it only ever touches what tx spawned.

See tx-service-redesign.md §4 (no-daemon) + §2 (state model).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

# Import the bundled adapters for their registry SIDE-EFFECT only (each registers itself at import),
# so the C5 agent-pane check below sees EVERY engine via `registered()`, not just Claude. The module
# names are otherwise unused here — the lookup goes through `get`/`registered` — hence the noqa.
from .engines import claude, codex  # noqa: F401
from .engines import get, registered
from .events import EventLog
from .session import Session, State
from .storage import config_path
from .store import SessionStore
from .tmux import Tmux

# C5 threshold (10 min) — overridable via config.json's `stuck_working_threshold_seconds` (§19).
DEFAULT_STUCK_WORKING_SECONDS = 600
# Claude shows a version string ("2.1.138") in `pane_current_command` while its TUI loads — treat
# that as "agent still up" alongside the bare binary name so C5 doesn't demote a loading agent.
_VERSION_COMMAND = re.compile(r"^\d+\.\d+")

# One list-sessions row. Match key is `@tx_id`, NOT the name (names are reusable, D7 — a reused
# name would make a dead record look alive); `pane_current_command` backs the C5 stuck check.
_LIST_FORMAT = "#{@tx_id}\t#{session_name}\t#{pane_current_command}"


@dataclass
class _Live:
    name: str
    command: str


class Reconciler:
    def __init__(self, store: SessionStore, tmux: Tmux, log: EventLog):
        self.store = store
        self.tmux = tmux
        self.log = log

    def reconcile(self) -> list[Session]:
        """Drive every stored record to ground truth. Returns only the records that actually
        changed (the dirty set), so a caller sees what moved without re-reading the store."""
        live = self._live_by_id()
        threshold = self._stuck_threshold()
        changed: list[Session] = []
        for session in self.store.all():
            if session.state.is_terminal:  # C4: terminal records are settled — skip
                continue
            if session.id not in live:
                if self._mark_exited(session):
                    changed.append(session)
                continue
            if self._demote_if_stuck(session, live[session.id], threshold):
                changed.append(session)
        return changed

    def _live_by_id(self) -> dict[str, _Live]:
        """The single server scan (C1/C4), keyed by `@tx_id`. Sessions without an `@tx_id` (not
        ours / old-tx during coexistence) are ignored — the Reconciler only touches what tx
        spawned (this is also what keeps remote rows out of reach, C11)."""
        live: dict[str, _Live] = {}
        for row in self.tmux.list_sessions(_LIST_FORMAT):
            tx_id, name, command = row.split("\t")
            if tx_id:
                live[tx_id] = _Live(name=name, command=command)
        return live

    def _mark_exited(self, session: Session) -> bool:
        if not session.transition_to(State.EXITED):  # C3 guard + C4 dirty-check
            return False
        session.ended_at = time.time()
        session.attached_to = []  # a dead session surfaces nowhere (attachment-topology §4)
        self.store.save(session)
        self.log.append("reconcile", f"{session.name} → exited (vanished)")
        return True

    def _demote_if_stuck(self, session: Session, live: _Live, threshold: float) -> bool:
        """C5: a `WORKING` llm idle past the threshold whose pane is no longer the agent has
        crashed under a live pane — demote to IDLE so the picker stops showing a phantom turn."""
        if session.state != State.WORKING or session.last_activity is None:
            return False
        if time.time() - session.last_activity < threshold:
            return False
        if self._is_agent_command(live.command):
            return False
        if not session.transition_to(State.IDLE):
            return False
        self.store.save(session)
        self.log.append("reconcile", f"{session.name} working → idle (stuck)")
        return True

    def _is_agent_command(self, command: str) -> bool:
        """Whether the live `pane_current_command` is an agent still up (C5). True for ANY registered
        engine's binary (`claude` / `codex` / …) — the input is the BARE pane command, and
        `matches_binary` basenames the first token, so a bare `claude` / `codex` still matches — OR a
        dotted version string an engine's TUI shows while loading. The `_VERSION_COMMAND` branch is
        PRESERVED (it covers the load window before the binary name settles) and stays engine-neutral."""
        return (
            any(get(engine).matches_binary(command) for engine in registered())
            or bool(_VERSION_COMMAND.match(command))
        )

    def _stuck_threshold(self) -> float:
        """Read the C5 threshold from config.json (a user boundary → tolerate absence + default),
        once per reconcile so the ~1 Hz reload doesn't re-read it per WORKING record."""
        path = config_path()
        if not path.exists():
            return DEFAULT_STUCK_WORKING_SECONDS
        config = json.loads(path.read_text())
        return config.get("stuck_working_threshold_seconds", DEFAULT_STUCK_WORKING_SECONDS)
