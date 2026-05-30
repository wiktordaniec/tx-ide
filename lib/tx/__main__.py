"""Package entry — `python3.14 -m tx` (what `bin/tx` execs).

S0 ships a **minimal foundation entry**, not the real CLI: it exists so the deliverable's verify
step (`TX_IDE_HOME=~/.tx-ide-next python3.14 -m tx` can create / load / list a record) is runnable,
and so the installer has an `_init-home` seam. **S1a replaces this with the command registry
(cli.py)** and its non-interactive verbs; keep the surface here tiny.

Subcommands:
  (none) / help   print foundation status (home, record count)
  selfcheck       round-trip a record through SessionStore + exercise C3 + append a log line
  _init-home      create the $TX_IDE_HOME skeleton (idempotent) — the installer's seam (C9)
"""

from __future__ import annotations

import sys
import time
import uuid

from . import claude
from .events import EventLog
from .session import SCHEMA_VERSION, ChatRef, Kind, Origin, Role, Session, State
from .storage import ensure_home, sessions_dir, tx_ide_home
from .store import SessionStore


def _status() -> int:
    store = SessionStore()
    print(f"tx-ide foundation (S0) — record schema v{SCHEMA_VERSION}, version {_version()}")
    print(f"  TX_IDE_HOME : {tx_ide_home()}")
    print(f"  sessions    : {sessions_dir()}")
    print(f"  records     : {len(store.all())}")
    print("  (the full CLI lands in S1a — this is the foundation entry)")
    return 0


def _version() -> str:
    from . import __version__

    return __version__


def _init_home() -> int:
    home = ensure_home()
    print(f"initialized $TX_IDE_HOME skeleton at {home}")
    return 0


def _selfcheck() -> int:
    """Exercise the frozen foundation against the live $TX_IDE_HOME: create → save → load
    round-trip, the C3 terminal-state guard, find_by_name, delete, and an EventLog append. Prints
    PASS/FAIL and returns a nonzero exit on failure. Cleans up its own demo record."""
    ensure_home()
    store = SessionStore()
    log = EventLog()
    failures: list[str] = []

    def check(condition: bool, label: str) -> None:
        if not condition:
            failures.append(label)

    session_id = str(uuid.uuid4())
    now = time.time()
    cwd = str(tx_ide_home())
    demo = Session(
        id=session_id,
        name="s0-selfcheck",
        kind=Kind.PROCESS,
        role=Role.LLM,
        state=State.initial_for(Role.LLM),
        cwd=cwd,
        cmd="claude --dangerously-skip-permissions",
        tags=["s0", "selfcheck"],
        created_at=now,
        last_activity=now,
        chats=[
            ChatRef(
                id=None,
                role="original",
                cwd=cwd,
                transcript_path=str(claude.transcript_path("00000000-pending", cwd)),
                origin=Origin(how="spawn", session_id=session_id, chat_id=None),
                started_at=now,
            )
        ],
    )

    check(demo.state == State.IDLE, "llm spawn state is IDLE (initial_for)")

    store.save(demo)
    log.append("selfcheck", f"created {demo.name} ({session_id})")

    loaded = store.load(session_id)
    check(loaded is not None, "load returns the saved record")
    check(loaded == demo, "save → load round-trips identically")

    # C3: terminal states are absorbing.
    check(loaded.transition_to(State.WORKING) is True, "IDLE → WORKING applies")
    check(loaded.transition_to(State.EXITED) is True, "WORKING → EXITED applies")
    check(loaded.transition_to(State.WORKING) is False, "C3: EXITED is absorbing (refused)")
    check(loaded.transition_to(State.EXITED) is False, "no-op transition reports no change")

    found = store.find_by_name("s0-selfcheck")
    check(found is not None and found.id == session_id, "find_by_name locates the record")
    listed_ids = {session.id for session in store.all()}
    check(session_id in listed_ids, "all() lists the record")

    check(store.delete(session_id) is True, "delete removes the record")
    check(store.load(session_id) is None, "record is gone after delete")

    if failures:
        print("S0 self-check FAILED ✗")
        for label in failures:
            print(f"  - {label}")
        return 1
    print("S0 self-check PASSED ✓")
    print("  create → save → load round-trip · C3 terminal guard · find_by_name · delete · log append")
    print(f"  home={tx_ide_home()}  records now={len(store.all())}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    command = argv[0] if argv else ""
    if command in ("", "help", "-h", "--help", "status"):
        return _status()
    if command == "selfcheck":
        return _selfcheck()
    if command == "_init-home":
        return _init_home()
    print(f"tx: unknown command {command!r} (S0 foundation entry; full CLI is S1a)", file=sys.stderr)
    print("    try: (no args) | selfcheck | _init-home", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
