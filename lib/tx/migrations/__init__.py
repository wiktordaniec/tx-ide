from __future__ import annotations

import json
from pathlib import Path

from ..session import SCHEMA_VERSION, Session, UnsupportedRecordError
from ..store import SessionStore
from ..tmux import Tmux

# `tx migrate` — the explicit, idempotent v3 -> v4 migrator (replacing the spent v2 -> v3 body). v4
# split the one record shape into role-discriminated types and took views out of the store:
#   - a PROCESS record (v3 `kind` != "view") is re-saved through the v4 factory + serializer. That
#     canonicalizes it: `Session.from_dict` dispatches on `role` and each subtype's `to_dict` emits
#     only its own keys, so the dead "kind" key is dropped, a non-llm record loses the now-absent
#     `engine`/`chats`/`last_activity` keys (they are simply not part of OtherSession), an llm record
#     gains `turn_started_at`, and `schema_version` becomes 4 — no hand-editing of the dict needed.
#   - a VIEW record (v3 `kind` == "view") leaves the store entirely: its durable identity becomes the
#     live `@tx_view` tmux marker, so migration stamps that marker on the matching live tmux session
#     (record name <-> tmux name — views are human-named) and DELETES the record file. Deletion is
#     mandatory and ordering-sensitive: left behind, the role-dispatching factory would load a view
#     as a terminal OtherSession and pollute `tx history` with phantoms.
# The loader refuses any non-current schema (no auto-upgrade-on-load), so pre-v3 records must be
# upgraded out-of-band first; every live record is v3, so no chain is needed.
# SAFETY: run ONCE at deploy against the real $TX_IDE_HOME; a v4 checkout must never migrate a live
# v3 home (it would brick the running v3 crew). Sandbox run: `TX_IDE_HOME=$(mktemp -d) tx migrate`.

_MIGRATE_FROM_VERSION = 3
_VIEW_KIND = "view"


def _skip_reason(raw: dict) -> str:
    version = raw.get("schema_version")
    if version == SCHEMA_VERSION:
        return f"already v{SCHEMA_VERSION}"
    return f"not a v{_MIGRATE_FROM_VERSION} record (schema_version={version!r})"


def migrate_sessions(
    directory: Path, tmux: Tmux | None = None
) -> tuple[list[str], list[str], list[tuple[str, str]]]:
    """Migrate every v3 record under `directory` to v4 in place; return (migrated, views_removed,
    skipped). The target is explicit (not `sessions_dir()`) and `tmux` is injectable so a test can
    point at a temp fixture + a fake tmux. Idempotent: a v4 record is skipped, and a view whose
    record was already deleted is simply absent on a re-run. A single unreadable/malformed file is
    skipped with its error rather than aborting the run."""
    tmux = tmux if tmux is not None else Tmux()
    store = SessionStore(directory=directory)
    migrated: list[str] = []
    views_removed: list[str] = []
    skipped: list[tuple[str, str]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            with open(path) as handle:
                raw = json.load(handle)
            if raw.get("schema_version") != _MIGRATE_FROM_VERSION:
                skipped.append((path.name, _skip_reason(raw)))
                continue
            if raw.get("kind") == _VIEW_KIND:
                # A view leaves the store: stamp its live tmux session with @tx_view (name-matched,
                # since views are human-named) so nest-attach + border chrome keep working through
                # the cutover, then delete the record. A dead view (no live session) is just deleted.
                if tmux.has_session(raw["name"]):
                    tmux.set_tx_view(raw["name"])
                path.unlink()
                views_removed.append(raw["name"])
                continue
            # A process record: re-save through the v4 factory + serializer, which drops the dead
            # keys and stamps schema_version 4 (see the module note).
            store.save(Session.from_dict({**raw, "schema_version": SCHEMA_VERSION}))
        except (OSError, json.JSONDecodeError, KeyError, ValueError, UnsupportedRecordError) as error:
            skipped.append((path.name, f"{type(error).__name__}: {error}"))
            continue
        migrated.append(path.name)
    return migrated, views_removed, skipped
