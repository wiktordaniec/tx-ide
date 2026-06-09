from __future__ import annotations

import json
from pathlib import Path

from ..session import SCHEMA_VERSION, Engine, Role, Session, UnsupportedRecordError
from ..store import SessionStore

# `tx migrate` — the explicit, idempotent v2 → v3 migrator. The loader refuses any non-current
# schema (no auto-upgrade-on-load), so pre-v3 records must be upgraded out-of-band first.
# SAFETY: run ONCE at deploy against the real $TX_IDE_HOME; a v3 checkout must never migrate a live
# v2 home (it would brick the running v2 crew). Sandbox run: `TX_IDE_HOME=$(mktemp -d) tx migrate`.

_MIGRATE_FROM_VERSION = 2
_MIGRATE_TO_VERSION = 3


def migrate_record_v2_to_v3(raw: dict) -> dict | None:
    """One raw v2 record → v3, or None if it isn't v2 (already v3, or v1/unknown — left untouched,
    which is what makes the migration idempotent). v3 is additive: stamp `engine` (claude for an llm
    session, else None) on the record + each ChatRef, and bump schema_version."""
    if raw.get("schema_version") != _MIGRATE_FROM_VERSION:
        return None
    engine = Engine.CLAUDE.value if raw["role"] == Role.LLM.value else None
    migrated = dict(raw)
    migrated["schema_version"] = _MIGRATE_TO_VERSION
    migrated["engine"] = engine
    migrated["chats"] = [{**chat, "engine": engine} for chat in raw["chats"]]
    return migrated


def _skip_reason(raw: dict) -> str:
    version = raw.get("schema_version")
    if version == SCHEMA_VERSION:
        return "already v3"
    return f"not a v2 record (schema_version={version!r})"


def migrate_sessions(directory: Path) -> tuple[list[str], list[tuple[str, str]]]:
    """Rewrite every v2 record under `directory` to v3 in place; return (migrated, skipped). The
    target is explicit (not sessions_dir()) so a test can point at a temp fixture. A single
    unreadable/malformed file is skipped with its error rather than aborting the run."""
    store = SessionStore(directory=directory)
    migrated: list[str] = []
    skipped: list[tuple[str, str]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            with open(path) as handle:
                raw = json.load(handle)
            new_record = migrate_record_v2_to_v3(raw)
            if new_record is None:
                skipped.append((path.name, _skip_reason(raw)))
                continue
            store.save(Session.from_dict(new_record))  # validates v3 + writes the canonical shape
        except (OSError, json.JSONDecodeError, KeyError, ValueError, UnsupportedRecordError) as error:
            skipped.append((path.name, f"{type(error).__name__}: {error}"))
            continue
        migrated.append(path.name)
    return migrated, skipped
