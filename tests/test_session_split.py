#!/usr/bin/env python3.14
"""Session-record split — persistence round-trip + type mechanics (dev-only gate).

No pytest in this repo, so this is directly runnable:  python3.14 tests/test_session_split.py
Exits non-zero on the first failure; prints "OK — N checks passed".

Covers the split's structural contracts:
  1. from_dict is a role-dispatching factory (llm -> LlmSession, else OtherSession);
  2. the attribute renames keep their legacy JSON keys ("cmd"/"env");
  3. the v4 shape — no "kind"; OtherSession emits no engine/chats/last_activity; LlmSession adds
     turn_started_at (read via .get(), so a record written before the first turn loads None);
  4. save -> load round-trips identically for both subtypes through SessionStore;
  5. the property/field mechanics that must hold for the dataclasses to construct at all:
     LlmSession.role is a read-only property, OtherSession.chats is a read-only [] property,
     activity_at falls back correctly, and needs_attention/read_only stay role-gated on the base;
  6. the v4 version gate refuses a v3 record loudly (UnsupportedRecordError).

Hermetic: a temp $TX_IDE_HOME (records only). No live server, no network.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["TX_IDE_HOME"] = tempfile.mkdtemp()  # before importing tx (storage reads this)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from tx.session import (  # noqa: E402
    SCHEMA_VERSION,
    ChatRef,
    Engine,
    LlmSession,
    Origin,
    OtherSession,
    Role,
    Session,
    State,
    UnsupportedRecordError,
)
from tx.storage import ensure_home  # noqa: E402
from tx.store import SessionStore  # noqa: E402

ensure_home()
PASSED = 0


def check(label, condition):
    global PASSED
    if condition:
        PASSED += 1
        return
    print(f"FAIL: {label}")
    sys.exit(1)


def _chat(cid="c1"):
    return ChatRef(
        id=cid, role="original", cwd="/x", transcript_path="/t",
        origin=Origin(how="spawn", session_id="tx-1", chat_id=None),
        started_at=1.0, engine=Engine.CLAUDE,
    )


now = time.time()
llm = LlmSession(
    id="id-llm", name="auth", state=State.WAITING, cwd="/x", initial_cmd="claude foo",
    engine=Engine.CLAUDE, tags=["auth"], spawn_env={"K": "V"}, chats=[_chat()],
    last_activity=100.0, created_at=50.0,
)
other = OtherSession(
    id="id-sh", name="build", state=State.ALIVE, cwd="/y", initial_cmd="zsh",
    role=Role.SHELL, tags=["build"], spawn_env={"P": "Q"}, created_at=70.0,
)

# ----- 1/2/3. serialization shape + renames + v4 keys -------------------------------------------
ld = llm.to_dict()
check("SCHEMA_VERSION is 4", SCHEMA_VERSION == 4 and ld["schema_version"] == 4)
check("llm to_dict: role from the property", ld["role"] == "llm")
check("rename: initial_cmd serializes to JSON key 'cmd'", ld["cmd"] == "claude foo")
check("rename: spawn_env serializes to JSON key 'env'", ld["env"] == {"K": "V"})
check("v4: no 'kind' key on an llm record", "kind" not in ld)
check("llm keeps engine/chats/last_activity", ld["engine"] == "claude" and ld["chats"] and ld["last_activity"] == 100.0)
check("llm adds turn_started_at (None when unarmed)", ld["turn_started_at"] is None)

od = other.to_dict()
check("other to_dict: role field serialized", od["role"] == "shell")
check("v4: no 'kind' key on a non-llm record", "kind" not in od)
check("non-llm drops engine/chats/last_activity/turn_started_at",
      not any(k in od for k in ("engine", "chats", "last_activity", "turn_started_at")))
check("rename holds for OtherSession too", od["cmd"] == "zsh" and od["env"] == {"P": "Q"})

# ----- 1/4. factory dispatch + round-trip through SessionStore ----------------------------------
back_llm = Session.from_dict(ld)
check("factory: role llm -> LlmSession", isinstance(back_llm, LlmSession))
check("round-trip: initial_cmd/spawn_env from cmd/env", back_llm.initial_cmd == "claude foo" and back_llm.spawn_env == {"K": "V"})
check("round-trip: llm serializes stably", back_llm.to_dict() == ld)

back_other = Session.from_dict(od)
check("factory: role shell -> OtherSession", isinstance(back_other, OtherSession))
check("factory: OtherSession keeps its role", back_other.role == Role.SHELL)
check("round-trip: other serializes stably", back_other.to_dict() == od)

store = SessionStore()
store.save(llm)
store.save(other)
reloaded_llm = store.load("id-llm")
reloaded_other = store.load("id-sh")
check("store round-trip: llm subtype", isinstance(reloaded_llm, LlmSession) and reloaded_llm.to_dict() == ld)
check("store round-trip: other subtype", isinstance(reloaded_other, OtherSession) and reloaded_other.to_dict() == od)
check("store.all() dispatches both subtypes", {type(s) for s in store.all()} == {LlmSession, OtherSession})

# turn_started_at read via .get(): a v4 llm record written before its first turn lacks the key.
no_turn = dict(ld)
del no_turn["turn_started_at"]
check("turn_started_at defaults None when the key is absent", Session.from_dict(no_turn).turn_started_at is None)
armed = dict(ld)
armed["turn_started_at"] = 999.0
check("turn_started_at round-trips when armed", Session.from_dict(armed).turn_started_at == 999.0)

# ----- 5. property/field mechanics --------------------------------------------------------------
check("LlmSession.role is Role.LLM", llm.role is Role.LLM)
try:
    llm.role = Role.SHELL
    check("LlmSession.role is read-only (no setter)", False)
except AttributeError:
    check("LlmSession.role is read-only (no setter)", True)
check("OtherSession.chats is an empty read-only property", other.chats == [])
check("tmux_name is always the id (process-only store)", llm.tmux_name == "id-llm" and other.tmux_name == "id-sh")
check("activity_at: llm prefers last_activity", llm.activity_at == 100.0)
check("activity_at: llm falls back to created_at", LlmSession(id="a", name="a", state=State.IDLE, engine=Engine.CLAUDE, created_at=9.0).activity_at == 9.0)
check("activity_at: other uses created_at", other.activity_at == 70.0)
check("needs_attention: llm + WAITING lights up", llm.needs_attention is True)
check("needs_attention: non-llm never lights up", other.needs_attention is False)
check("read_only reads spawn_env, role-gated",
      LlmSession(id="r", name="r", state=State.IDLE, engine=Engine.CLAUDE, spawn_env={"TX_READ_ONLY": "1"}).read_only is True)
check("matches() no longer indexes kind; still matches role/tags/cwd", llm.matches("llm") and llm.matches("auth") and other.matches("shell"))
check("non-llm has no engine/last_activity attribute at all",
      not hasattr(other, "engine") and not hasattr(other, "last_activity"))

# ----- 6. the v4 version gate refuses a v3 record -----------------------------------------------
legacy_v3 = dict(ld)
legacy_v3["schema_version"] = 3
legacy_v3["kind"] = "process"
try:
    Session.from_dict(legacy_v3)
    check("v4 gate: a v3 record is refused loudly", False)
except UnsupportedRecordError:
    check("v4 gate: a v3 record is refused loudly", True)

print(f"OK — {PASSED} checks passed")
