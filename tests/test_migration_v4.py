#!/usr/bin/env python3.14
"""Session-record split — the v3 -> v4 migration (dev-only gate).

Directly runnable:  python3.14 tests/test_migration_v4.py
Exits non-zero on the first failure; prints "OK — N checks passed".

Covers `migrate_sessions` over a v3 fixture store holding all three v3 shapes:
  1. a v3 llm PROCESS record -> v4 LlmSession: "kind" dropped, engine/chats/last_activity kept,
     turn_started_at added (None), schema_version 4, and it loads back as an LlmSession;
  2. a v3 non-llm PROCESS record -> v4 OtherSession: "kind" AND the dead engine/chats/last_activity
     keys dropped, schema_version 4, loads back as an OtherSession;
  3. a v3 VIEW record: the matching LIVE tmux session is stamped @tx_view and the record file is
     DELETED (so the role-dispatching factory can't later resurrect it as a phantom); a DEAD view
     (no live session) is deleted without a stamp and without crashing;
  4. idempotence: a second pass migrates nothing, removes no view, and skips every v4 record.

Hermetic: an explicit temp fixture directory + a FakeTmux. No live server, no real home touched.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ["TX_IDE_HOME"] = tempfile.mkdtemp()
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))
sys.path.insert(0, str(HERE))

from _tmux_fakes import FakeTmux  # noqa: E402

from tx.migrations import migrate_sessions  # noqa: E402
from tx.session import LlmSession, OtherSession  # noqa: E402
from tx.store import SessionStore  # noqa: E402

PASSED = 0


def check(label, condition):
    global PASSED
    if condition:
        PASSED += 1
        return
    print(f"FAIL: {label}")
    sys.exit(1)


def _v3_chat(cid):
    return {
        "id": cid, "role": "original", "cwd": "/x", "transcript_path": "/t",
        "origin": {"how": "spawn", "session_id": cid, "chat_id": None},
        "bundle_path": None, "started_at": 1.0, "ended_at": None, "summary": "", "engine": "claude",
    }


def _v3_record(**overrides):
    """A canonical v3 on-disk record (schema_version 3, with the dead `kind` key)."""
    base = {
        "schema_version": 3, "id": "x", "name": "x", "kind": "process", "role": "shell",
        "state": "alive", "cwd": "/c", "cmd": "zsh", "engine": None, "tags": [], "env": {},
        "parent": None, "pid": 111, "attached_to": [], "created_at": 10.0, "ended_at": None,
        "last_activity": 10.0, "chats": [],
    }
    base.update(overrides)
    return base


directory = Path(tempfile.mkdtemp())


def write(record):
    (directory / f"{record['id']}.json").write_text(json.dumps(record, indent=2))


llm_v3 = _v3_record(id="id-llm", name="auth", role="llm", state="waiting", cmd="claude foo",
                    engine="claude", tags=["auth"], env={"K": "V"}, last_activity=99.0,
                    chats=[_v3_chat("c-llm")])
shell_v3 = _v3_record(id="id-sh", name="build", role="shell", cmd="zsh", tags=["build"])
view_live_v3 = _v3_record(id="id-view", name="upside-office", kind="view", role="shell", cmd="zsh")
view_dead_v3 = _v3_record(id="id-deadview", name="Views-Left", kind="view", role="other", cmd="zsh")
for record in (llm_v3, shell_v3, view_live_v3, view_dead_v3):
    write(record)

# Only `upside-office` is a live tmux session; `Views-Left` is a dead view (record only).
tmux = FakeTmux(live={"upside-office"})
migrated, views_removed, skipped = migrate_sessions(directory, tmux)

# ----- 1/2. process records upgraded to v4 ------------------------------------------------------
check("migrated exactly the two process records", set(migrated) == {"id-llm.json", "id-sh.json"})
check("nothing skipped on the v3 pass", skipped == [])

llm_raw = json.loads((directory / "id-llm.json").read_text())
check("llm: schema_version bumped to 4", llm_raw["schema_version"] == 4)
check("llm: 'kind' key dropped", "kind" not in llm_raw)
check("llm: engine/chats/last_activity kept", llm_raw["engine"] == "claude" and llm_raw["chats"] and llm_raw["last_activity"] == 99.0)
check("llm: turn_started_at added (None)", llm_raw.get("turn_started_at", "MISSING") is None)
check("llm: renamed keys stay 'cmd'/'env'", llm_raw["cmd"] == "claude foo" and llm_raw["env"] == {"K": "V"})

shell_raw = json.loads((directory / "id-sh.json").read_text())
check("non-llm: schema_version bumped to 4", shell_raw["schema_version"] == 4)
check("non-llm: 'kind' dropped", "kind" not in shell_raw)
check("non-llm: dead engine/chats/last_activity/turn_started_at dropped",
      not any(k in shell_raw for k in ("engine", "chats", "last_activity", "turn_started_at")))

# ----- 3. view records: stamp live + delete record ----------------------------------------------
check("view records removed (both)", set(views_removed) == {"upside-office", "Views-Left"})
check("live view record file deleted", not (directory / "id-view.json").exists())
check("dead view record file deleted", not (directory / "id-deadview.json").exists())
check("live view stamped @tx_view", tmux.is_view("upside-office"))
check("dead view NOT stamped (no live session)", not tmux.is_view("Views-Left"))

# ----- the store now loads only the two process subtypes, no view phantom -----------------------
loaded = {s.id: s for s in SessionStore(directory=directory).all()}
check("store loads exactly the two process records", set(loaded) == {"id-llm", "id-sh"})
check("llm loads as LlmSession", isinstance(loaded["id-llm"], LlmSession))
check("non-llm loads as OtherSession", isinstance(loaded["id-sh"], OtherSession))

# ----- 4. idempotence: a second pass is a no-op -------------------------------------------------
migrated2, views_removed2, skipped2 = migrate_sessions(directory, FakeTmux(live={"upside-office"}))
check("re-run: nothing migrated", migrated2 == [])
check("re-run: no views removed", views_removed2 == [])
check("re-run: every v4 record skipped as already-current",
      {name for name, _ in skipped2} == {"id-llm.json", "id-sh.json"} and all("already v4" in reason for _, reason in skipped2))

print(f"OK — {PASSED} checks passed")
