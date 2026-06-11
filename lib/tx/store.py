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
        # Latched off the first time a write proves the home unwritable (a sandboxed caller confined
        # away from $TX_IDE_HOME — e.g. codex under a read-only Seatbelt profile). One process owns
        # one SessionStore, so this resets per `tx` invocation; within an invocation it stops a
        # multi-record reconcile sweep from re-attempting (and re-warning) a doomed write per record.
        self._can_persist = True

    def _path(self, session_id: str) -> Path:
        return self.directory / f"{session_id}.json"

    def save(self, session: Session) -> bool:
        """Persist a record atomically: write a temp file in the same directory, then
        `os.replace` it over the target. Rename is atomic within one filesystem, so a concurrent
        reader sees either the old or the new record, never a half-written one. uuid-sharded
        filenames mean two sessions never contend (§4).

        Returns True when the record was persisted, False when the write was SKIPPED because
        `$TX_IDE_HOME` is not writable. A caller confined away from the home by a sandbox — codex
        under a read-only Seatbelt profile running `tx send-message`/`ls`/`whoami` — must degrade,
        not crash: the home lives outside its writable tree, so `mkstemp`/`os.replace` raise EPERM.
        We treat that exactly like the unreadable-record tolerance in `all()` (OPEN-0b) — warn once
        on stderr and let the caller's primary action stand (the message was still delivered; `ls`
        still lists from disk). The first failed write latches `_can_persist` off so a multi-record
        reconcile sweep makes no further write attempt — a read-only command then writes nothing at
        all. The atomic temp+replace itself is unchanged; only its failure is now soft."""
        if not self._can_persist:
            return False
        payload = json.dumps(session.to_dict(), indent=2)
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
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
        except OSError as error:
            self._can_persist = False
            print(
                f"tx: $TX_IDE_HOME not writable ({error}); session state not persisted this run",
                file=sys.stderr,
            )
            return False
        return True

    def load(self, session_id: str) -> Session | None:
        """Load one record by id, or None if there is no such file. Propagates
        `UnsupportedRecordError` (OPEN-0b): the caller named this record, so a non-v2 file is a
        real error, not something to swallow."""
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
        """First record with this tmux name (id-sorted order). Names are reusable across
        non-concurrent sessions (D7), so a caller that needs the *live* one reconciles first and
        filters on liveness — this is the plain lookup."""
        for session in self.all():
            if session.name == name:
                return session
        return None

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
