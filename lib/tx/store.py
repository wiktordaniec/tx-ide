"""`SessionStore` — the repository over `$TX_IDE_HOME/sessions/<uuid>.json` (stage S0).

The only object that touches session records, and it is **filesystem-direct** (OPEN-0a): atomic
writes via temp file + `os.replace`, reads via directory `glob`. It is NOT routed through the
`Storage` KV abstraction — a `get/put` API cannot preserve `os.replace` atomicity or `glob`, and
that atomicity is what lets uuid-sharded files work with no global lock (§4). `Storage` is the S7
sync boundary only. This module borrows just the `sessions_dir()` path helper from `storage.py`.

Frozen API (consumed by S3/S4/S5/S6 + the CLI): `load`, `save`, `all`, `find_by_name`, `query`
(+ `delete` for `tx rm`).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from .session import Session, UnsupportedRecordError
from .storage import sessions_dir


class SessionStore:
    def __init__(self, directory: Path | None = None):
        self.directory = directory if directory is not None else sessions_dir()

    def _path(self, session_id: str) -> Path:
        return self.directory / f"{session_id}.json"

    def save(self, session: Session) -> None:
        """Persist a record atomically: write a temp file in the same directory, then
        `os.replace` it over the target. Rename is atomic within one filesystem, so a concurrent
        reader sees either the old or the new record, never a half-written one. uuid-sharded
        filenames mean two sessions never contend (§4)."""
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(session.to_dict(), indent=2)
        descriptor, temp_path = tempfile.mkstemp(
            dir=self.directory, prefix=f".{session.id}.", suffix=".tmp"
        )
        try:
            with os.fdopen(descriptor, "w") as handle:
                handle.write(payload)
            os.replace(temp_path, self._path(session.id))
        except BaseException:
            # The replace never happened — drop the orphan temp file before re-raising.
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise

    def load(self, session_id: str) -> Session | None:
        """Load one record by id, or None if there is no such file. Propagates
        `UnsupportedRecordError` (OPEN-0b): the caller named this record, so a non-current-version
        file is a real error, not something to swallow."""
        path = self._path(session_id)
        if not path.exists():
            return None
        return self._read(path)

    def _read(self, path: Path) -> Session:
        with open(path) as handle:
            return Session.from_dict(json.load(handle))

    def all(self) -> list[Session]:
        """Every record in the store, id-sorted. Tolerates unreadable files at this persistence
        boundary (OPEN-0b): a stale v1 record, malformed JSON, or a bad enum value is skipped with
        a one-line stderr warning rather than crashing `ls`/reconcile. (The dev home starts empty,
        so this is belt-and-suspenders during the build; it matters at Flip against the real home.)
        """
        sessions: list[Session] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                sessions.append(self._read(path))
            except (UnsupportedRecordError, json.JSONDecodeError, KeyError, ValueError) as error:
                print(f"tx: skipping unreadable record {path.name}: {error}", file=sys.stderr)
        return sessions

    def find_by_name(self, name: str) -> Session | None:
        """Record with this name, preferring a live one. Names are reusable across non-concurrent
        sessions (D7), so a long-lived name (e.g. tx-assistant) accrues terminal "corpse" records
        alongside the running session. Returning the first id-sorted match lets a corpse shadow the
        live session and breaks name-targeting — `tx send-message <name>` and `_tmux-name` (and so
        the prefix+/ binding) resolve to a dead tmux target. Prefer the first non-terminal record;
        fall back to the first match overall when none are live (pure-history lookups)."""
        first: Session | None = None
        for session in self.all():
            if session.name != name:
                continue
            if session.is_alive():
                return session
            if first is None:
                first = session
        return first

    def query(self, predicate: Callable[[Session], bool]) -> list[Session]:
        """Records matching an arbitrary predicate, e.g.
        `store.query(lambda s: s.state in (State.EXITED, State.ARCHIVED))` for history. A
        predicate keeps the frozen API flexible and stable so consumers don't churn it."""
        return [session for session in self.all() if predicate(session)]

    def delete(self, session_id: str) -> bool:
        """Remove a record file (backs `tx rm`). Returns whether a file was actually removed."""
        path = self._path(session_id)
        if path.exists():
            path.unlink()
            return True
        return False
