#!/usr/bin/env python3.14
"""Standalone test for the v2 → v3 schema migrator (`tx.cli.migrate_sessions`) — T0.

No pytest in this repo, so this is directly runnable:  python3.14 tests/test_schema_migration.py
Exits non-zero on the first failure (same convention as tests/test_messages.py).

Proves the three things T0's acceptance pins on the migrator:
  - a v2 record gains the v3 `engine` field — `claude` for an llm record, `None` for a non-llm one —
    on both the record and its `ChatRef`s, and then loads cleanly through the *unchanged* loader;
  - the loader still REFUSES v2 (there is no auto-upgrade-on-load — design §9); migration is the
    only path to v3;
  - the migrator is idempotent — a second run over the v3 result migrates nothing and leaves the
    files byte-identical.

SAFETY (T0 §4): the fixture lives in a throwaway temp dir built here. This test NEVER reads or writes
the live `~/.tx-ide/sessions/` (which is v2 and drives the running crew) — `migrate_sessions` takes
the directory explicitly, so nothing here touches `sessions_dir()` / `$TX_IDE_HOME`.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from tx.cli import migrate_record_v2_to_v3, migrate_sessions  # noqa: E402
from tx.session import Engine, Session, UnsupportedRecordError  # noqa: E402

PASSED = 0


def check(label, condition):
    global PASSED
    if condition:
        PASSED += 1
        return
    print(f"FAIL: {label}")
    sys.exit(1)


def v2_record(session_id, role, *, chats):
    """A complete v2 record dict (every key `Session.to_dict` wrote at v2 — no `engine`)."""
    return {
        "schema_version": 2,
        "id": session_id,
        "name": f"{role}-session",
        "kind": "process",
        "role": role,
        "state": "idle" if role == "llm" else "alive",
        "cwd": "/tmp/work",
        "cmd": "claude --dangerously-skip-permissions" if role == "llm" else "nvim",
        "tags": ["scope"],
        "env": {},
        "parent": None,
        "pid": 4242,
        "attached_to": [],
        "created_at": 1.0,
        "ended_at": None,
        "last_activity": 2.0,
        "chats": chats,
    }


def v2_chat(chat_id, session_id):
    """A complete v2 `ChatRef` dict (no `engine`)."""
    return {
        "id": chat_id,
        "role": "original",
        "cwd": "/tmp/work",
        "transcript_path": "/tmp/t.jsonl",
        "origin": {"how": "spawn", "session_id": session_id, "chat_id": None},
        "bundle_path": None,
        "started_at": 1.0,
        "ended_at": None,
        "summary": "",
    }


with tempfile.TemporaryDirectory() as raw_dir:
    sessions = Path(raw_dir)

    # An llm record (has a chat) + a non-llm record (no chat) — the two engine outcomes.
    llm_v2 = v2_record("llm-1", "llm", chats=[v2_chat("chat-1", "llm-1")])
    nvim_v2 = v2_record("nv-1", "nvim", chats=[])
    (sessions / "llm-1.json").write_text(json.dumps(llm_v2, indent=2))
    (sessions / "nv-1.json").write_text(json.dumps(nvim_v2, indent=2))

    # ----- the loader refuses v2 (no auto-upgrade-on-load — design §9) ----------------------
    try:
        Session.from_dict(llm_v2)
        check("loader refuses v2 (raised UnsupportedRecordError)", False)
    except UnsupportedRecordError:
        check("loader refuses v2 (raised UnsupportedRecordError)", True)

    # ----- run 1: v2 → v3 -------------------------------------------------------------------
    migrated, skipped = migrate_sessions(sessions)
    check("run 1 migrated both records", sorted(migrated) == ["llm-1.json", "nv-1.json"])
    check("run 1 skipped nothing", skipped == [])

    llm_after = json.loads((sessions / "llm-1.json").read_text())
    nvim_after = json.loads((sessions / "nv-1.json").read_text())

    check("llm record bumped to v3", llm_after["schema_version"] == 3)
    check("llm record engine == claude", llm_after["engine"] == "claude")
    check("llm chat engine == claude", llm_after["chats"][0]["engine"] == "claude")
    check("non-llm record bumped to v3", nvim_after["schema_version"] == 3)
    check("non-llm record engine is None", nvim_after["engine"] is None)

    # ----- the migrated records now load cleanly as v3 (round-trip through the loader) -------
    llm_session = Session.from_dict(llm_after)
    nvim_session = Session.from_dict(nvim_after)
    check("migrated llm record loads as v3 with engine=CLAUDE", llm_session.engine == Engine.CLAUDE)
    check("migrated llm chat loads with engine=CLAUDE", llm_session.chats[0].engine == Engine.CLAUDE)
    check("migrated non-llm record loads as v3 with engine=None", nvim_session.engine is None)

    # ----- run 2: idempotent (no-op, files byte-identical) ----------------------------------
    bytes_before = {path.name: path.read_bytes() for path in sessions.glob("*.json")}
    migrated_again, skipped_again = migrate_sessions(sessions)
    check("run 2 migrated nothing", migrated_again == [])
    check("run 2 skipped both as already-v3",
          sorted(skipped_again) == [("llm-1.json", "already v3"), ("nv-1.json", "already v3")])
    bytes_after = {path.name: path.read_bytes() for path in sessions.glob("*.json")}
    check("run 2 left files byte-identical (idempotent)", bytes_before == bytes_after)

    # ----- the pure transform: only v2 is rewritten -----------------------------------------
    check("transform rewrites a v2 llm record → engine=claude",
          migrate_record_v2_to_v3(llm_v2)["engine"] == "claude")
    check("transform rewrites a v2 non-llm record → engine=None",
          migrate_record_v2_to_v3(nvim_v2)["engine"] is None)
    check("transform leaves a v3 record untouched (None)", migrate_record_v2_to_v3(llm_after) is None)
    check("transform leaves a v1 record untouched (None)",
          migrate_record_v2_to_v3({"schema_version": 1, "role": "llm", "chats": []}) is None)

print(f"OK — {PASSED} checks passed")
