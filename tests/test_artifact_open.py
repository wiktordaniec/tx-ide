#!/usr/bin/env python3.14
"""Artifact session binding — `tx artifact open`, the v5 back-link, migration (Plan 2, step 3).

Directly runnable (no pytest):  python3.14 tests/test_artifact_open.py
Exits non-zero on the first failure; prints "OK — N checks passed".

Covers:
  - Session schema v5: `OtherSession` gains a nullable `artifact_id` (that role ONLY — an llm record
    never carries the key); it round-trips through `to_dict`/`from_dict`, is read via `.get()` so a
    key-less record loads `None`, and the strict boundary still refuses a non-current record.
  - `tx migrate` chains v3 -> v4 -> v5 in ONE run: a v3 process record canonicalizes (drops `kind`,
    gains a null `artifact_id`), a v3 llm record gains `turn_started_at`, a v3 view record is retired
    (`@tx_view` stamped, record deleted), and a v4 record simply gains `artifact_id`. Idempotent.
  - `SessionService.bind_artifact` records the back-link (asserting the OtherSession narrowing).
  - `tx artifact open <id>` spawns an nvim companion on `current.<ext>` (never a frozen rev), binds
    it to the artifact, roots it in the artifact's dir, logs an `artifact-open` line, and resolves
    the view's tags: bare `artifact` outside tx, inherited from the invoker, or a `--tag` override.

Hermetic: a temp `$TX_IDE_HOME` + an in-memory fake Tmux (no live server).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ["TX_IDE_HOME"] = tempfile.mkdtemp()  # before importing tx (storage reads this)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from tx.artifact_service import ArtifactNotFound, ArtifactService  # noqa: E402
from tx.cli import ArtifactCommand  # noqa: E402
from tx.migrations import migrate_sessions  # noqa: E402
from tx.service import SessionService  # noqa: E402
from tx.session import (  # noqa: E402
    SCHEMA_VERSION,
    Engine,
    LlmSession,
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


def log_types():
    path = Path(os.environ["TX_IDE_HOME"]) / "log.jsonl"
    return [json.loads(line)["type"] for line in path.read_text().splitlines()] if path.exists() else []


class FakeTmux:
    """In-memory Tmux for the spawn path (`_spawn` needs has_session/current_session_name/
    new_session/set_tx_id) and invoker resolution (current_session_name/get_tx_id). Records the
    commands `new_session` was asked to run so the test can assert what nvim was launched on."""

    def __init__(self, current=None):
        self.live: set[str] = set()
        self.tx_ids: dict[str, str] = {}
        self.created: list[tuple[str, str, str]] = []  # (name, cwd, command)
        self._current = current

    def has_session(self, name):
        return name in self.live

    def new_session(self, *, name, cwd, command, env):
        self.live.add(name)
        self.created.append((name, cwd, command))
        return 4242

    def set_tx_id(self, name, session_id):
        self.tx_ids[name] = session_id

    def get_tx_id(self, name):
        return self.tx_ids.get(name)

    def current_session_name(self):
        return self._current

    def current_pane_path(self):
        return "/tmp"

    def attached_to(self, name):
        return []

    def attachment_map(self):
        return {}

    def is_view(self, name):
        return False


class NoReconcile:
    """`_spawn`'s `_require_name_free` calls `reconcile()`; a no-op keeps the spawn hermetic."""

    def reconcile(self):
        return []


def make_service(current=None):
    return SessionService(store=SessionStore(), tmux=FakeTmux(current), reconciler=NoReconcile())


def open_quietly(cmd, argv):
    """Run `tx artifact open` swallowing its stdout, so only the gate's OK line reaches the terminal."""
    with contextlib.redirect_stdout(io.StringIO()):
        return cmd._open(argv)


# ----- 1. schema v5: OtherSession.artifact_id ------------------------------------------------
check("SCHEMA_VERSION is 5", SCHEMA_VERSION == 5)
view = OtherSession(id="v", name="art-view", state=State.ALIVE, cwd="/x", initial_cmd="nvim",
                    role=Role.NVIM, tags=["scope"], created_at=1.0, artifact_id="art-123")
vd = view.to_dict()
check("OtherSession.to_dict emits artifact_id", vd["artifact_id"] == "art-123")
back = Session.from_dict(vd)
check("from_dict restores an OtherSession with its artifact_id", isinstance(back, OtherSession) and back.artifact_id == "art-123")
check("OtherSession round-trips stably", back.to_dict() == vd)
plain = OtherSession(id="s", name="sh", state=State.ALIVE, cwd="/x", initial_cmd="zsh", role=Role.SHELL, created_at=1.0)
check("a non-view OtherSession has a null artifact_id", plain.to_dict()["artifact_id"] is None)
llm = LlmSession(id="l", name="a", state=State.IDLE, cwd="/x", initial_cmd="claude", engine=Engine.CLAUDE, created_at=1.0)
check("an llm record never carries artifact_id", "artifact_id" not in llm.to_dict())
keyless = {k: v for k, v in vd.items() if k != "artifact_id"}
check("from_dict reads artifact_id via .get() (key-less -> None)", Session.from_dict(keyless).artifact_id is None)
stale = {**vd, "schema_version": 4}
try:
    Session.from_dict(stale)
    check("the strict boundary still refuses a non-current record", False)
except UnsupportedRecordError:
    check("the strict boundary still refuses a non-current record", True)

# ----- 2. migration chains v3 -> v4 -> v5 ----------------------------------------------------
mdir = Path(tempfile.mkdtemp())
common3 = dict(schema_version=3, kind="process", state="alive", cwd="/x", cmd="zsh", tags=["t"],
               env={}, parent=None, pid=1, attached_to=[], created_at=1.0, ended_at=None,
               engine="claude", chats=[], last_activity=2.0)


def write_record(directory, data):
    (directory / f"{data['id']}.json").write_text(json.dumps(data))


write_record(mdir, {**common3, "id": "p3", "name": "proc3", "role": "shell"})
write_record(mdir, {**common3, "id": "l3", "name": "llm3", "role": "llm", "state": "idle"})
write_record(mdir, {**common3, "id": "vv", "name": "myview", "role": "shell", "kind": "view"})
write_record(mdir, dict(schema_version=4, id="o4", name="other4", role="nvim", state="alive", cwd="/x",
                        cmd="nvim", tags=["t"], env={}, parent=None, pid=1, attached_to=[],
                        created_at=1.0, ended_at=None))
(mdir / "junk.json").write_text("{ malformed")


class MigrateTmux:
    def __init__(self):
        self.live = {"myview"}
        self.views: set[str] = set()

    def has_session(self, name):
        return name in self.live

    def set_tx_view(self, name):
        self.views.add(name)


migrate_tmux = MigrateTmux()
migrated, views_removed, skipped = migrate_sessions(mdir, migrate_tmux)
store = SessionStore(directory=mdir)
p3, l3, o4 = store.load("p3"), store.load("l3"), store.load("o4")
check("v3 process -> v5 OtherSession with a null artifact_id", isinstance(p3, OtherSession) and p3.schema_version == 5 and p3.artifact_id is None)
check("v3 process dropped the dead kind key", "kind" not in p3.to_dict())
check("v3 llm -> v5 LlmSession that gained turn_started_at", isinstance(l3, LlmSession) and l3.schema_version == 5 and "turn_started_at" in l3.to_dict())
check("v4 -> v5 OtherSession gains artifact_id", isinstance(o4, OtherSession) and o4.schema_version == 5 and "artifact_id" in o4.to_dict())
check("v3 view retired: record deleted", not (mdir / "vv.json").exists())
check("v3 view retired: @tx_view stamped on the live session", "myview" in migrate_tmux.views and "myview" in views_removed)
check("migrated the three upgradable process records", set(migrated) == {"p3.json", "l3.json", "o4.json"})
check("the malformed file is skipped, not fatal", any(name == "junk.json" for name, _ in skipped))
again_migrated, _, again_skipped = migrate_sessions(mdir, migrate_tmux)
check("re-run is idempotent (nothing re-migrated)", again_migrated == [])
check("re-run skips current records as already-current", sum(1 for _, reason in again_skipped if "already v5" in reason) == 3)

# ----- 3. bind_artifact ----------------------------------------------------------------------
svc = make_service()
nvim = OtherSession(id="nv", name="art-view", state=State.ALIVE, cwd="/x", initial_cmd="nvim", role=Role.NVIM, tags=["s"], created_at=1.0)
svc.store.save(nvim)
bound = svc.bind_artifact("nv", "artifact-xyz")
check("bind_artifact sets the back-link", bound.artifact_id == "artifact-xyz" and svc.store.load("nv").artifact_id == "artifact-xyz")
check("bind_artifact logs a line", "bind-artifact" in log_types())

# ----- 4. tx artifact open -------------------------------------------------------------------
# 4a. outside tx: bare `artifact` tag; opens current.<ext>; bound; rooted in the artifact dir.
svc = make_service(current=None)
artifact = ArtifactService().create("sess-A", b"# plan\nalpha\n", title="Plan", filename="plan.md")
code = open_quietly(ArtifactCommand(svc), [artifact.id])
check("open exits 0", code == 0)
name, cwd, command = svc.tmux.created[-1]
check("open launches nvim on current.<ext> (never a rev)", "nvim" in command and "current.md" in command and "/revs/" not in command)
opened_session = next(s for s in svc.store.all() if isinstance(s, OtherSession) and s.artifact_id == artifact.id)
check("open binds the view to the artifact", opened_session.role is Role.NVIM)
check("open roots the view in the artifact's dir", cwd.endswith(artifact.id))
check("open outside tx tags the view bare `artifact`", opened_session.tags == ["artifact"])
check("open logs an artifact-open line", "artifact-open" in log_types())

# 4b. inside tx: inherit the invoking session's tags.
svc = make_service(current="inv")
invoker = OtherSession(id="inv", name="inv", state=State.ALIVE, cwd="/x", initial_cmd="zsh", role=Role.SHELL, tags=["auth", "p1"], created_at=1.0)
svc.store.save(invoker)
svc.tmux.tx_ids["inv"] = "inv"
inherited = ArtifactService().create("sess-A", b"x\n", filename="q.md")
open_quietly(ArtifactCommand(svc), [inherited.id])
inherited_view = next(s for s in svc.store.all() if getattr(s, "artifact_id", None) == inherited.id)
check("open inside tx inherits the invoker's tags", inherited_view.tags == ["auth", "p1"])

# 4c. --tag overrides inheritance.
overridden = ArtifactService().create("sess-A", b"y\n", filename="r.md")
open_quietly(ArtifactCommand(svc), [overridden.id, "--tag", "over1,over2"])
overridden_view = next(s for s in svc.store.all() if getattr(s, "artifact_id", None) == overridden.id)
check("--tag overrides inherited tags", overridden_view.tags == ["over1", "over2"])

# 4d. a bad id resolves to a clean error, not a spawn.
try:
    open_quietly(ArtifactCommand(make_service()), ["no-such-artifact"])
    check("open of a missing artifact raises", False)
except ArtifactNotFound:
    check("open of a missing artifact raises", True)

print(f"OK — {PASSED} checks passed")
