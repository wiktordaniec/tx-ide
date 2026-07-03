#!/usr/bin/env python3.14
"""Artifacts — store, register/snapshot semantics, CLI verbs, picker feed, open resolution.

No pytest in this repo, so this is directly runnable:  python3.14 tests/test_artifacts.py
Exits non-zero on the first failure; prints "OK — N checks passed" (same convention as the other
gate tests).

Covers the artifacts acceptance checklist:
  1. `Artifact` round-trips through `to_dict`/`from_dict`; a wrong schema_version raises;
     `ArtifactStore.all()` skips an unreadable record instead of crashing.
  2. `register` snapshots a file artifact into `<store>/<id>/` and keeps the source path;
     `open_path` prefers the live source and falls back to the snapshot once the source is gone;
     a diff artifact snapshots nothing.
  3. `tx artifact add` fills producer id/name + tags from `$TX_SESSION_ID`; `--tag` overrides;
     a diff add rejects a path, a file add requires one; `rm` removes record + snapshot dir and
     resolves a unique id prefix.
  4. The picker feed (`artifact_picker_rows`) carries `id \t title \t chips \t visual`, newest
     first; `render_artifacts` lists the resolved open target.
  5. `ArtifactsCommand._companion_spec` builds `--open`-shaped nvim specs for file artifacts
     (live path, then snapshot) and `--diff`-shaped specs for diff artifacts, and returns None
     (not a spec) when the deliverable is truly gone.

Hermetic: a temp `$TX_IDE_HOME` (records + log) + a fake Tmux (no live server).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path

# Point the home at a temp dir BEFORE importing tx (storage reads $TX_IDE_HOME).
os.environ["TX_IDE_HOME"] = tempfile.mkdtemp()

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from tx.artifact import (  # noqa: E402
    Artifact,
    ArtifactStore,
    ArtifactType,
    UnsupportedArtifactError,
    open_path,
    register,
)
from tx.cli import ArtifactCommand, ArtifactsCommand  # noqa: E402
from tx.render import artifact_picker_rows, render_artifacts  # noqa: E402
from tx.service import SessionService  # noqa: E402
from tx.session import Kind, Role, Session, State  # noqa: E402
from tx.storage import artifacts_dir, ensure_home  # noqa: E402
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


class FakeTmux:
    """Just enough Tmux for `ArtifactCommand` (producer fallback + default cwd) and
    `ArtifactsCommand._companion_spec` (never reaches tmux)."""

    def has_session(self, name):
        return False

    def current_session_name(self):
        return None

    def current_pane_path(self):
        return "/tmp"


def make_service():
    return SessionService(store=SessionStore(), tmux=FakeTmux())


# ----- 1. record + store ---------------------------------------------------------------------

store = ArtifactStore()
plan_source = Path(tempfile.mkdtemp()) / "plan.md"
plan_source.write_text("# the plan\n")

round_trip = Artifact.from_dict(
    Artifact(
        id="0" * 36, type=ArtifactType.PLAN, title="t", repo="/r", tags=["x"], created_at=1.0
    ).to_dict()
)
check("round-trip keeps type", round_trip.type is ArtifactType.PLAN)
check("round-trip keeps tags", round_trip.tags == ["x"])

try:
    Artifact.from_dict({"schema_version": 99})
    check("wrong schema raises", False)
except UnsupportedArtifactError:
    check("wrong schema raises", True)

(artifacts_dir() / "garbage.json").write_text("{not json")
stderr = io.StringIO()
with contextlib.redirect_stderr(stderr):
    survivors = store.all()
check("all() skips unreadable record", "garbage" in stderr.getvalue())
(artifacts_dir() / "garbage.json").unlink()

# ----- 2. register / snapshot / open_path ----------------------------------------------------

plan = register(
    store,
    type=ArtifactType.PLAN,
    title="auth plan",
    repo="/tmp",
    tags=["auth"],
    source_path=str(plan_source),
)
check("register persists", store.load(plan.id) is not None)
check("snapshot lives under <store>/<id>/", plan.snapshot_path == str(store.snapshot_dir(plan.id) / "plan.md"))
check("snapshot copied", Path(plan.snapshot_path).read_text() == "# the plan\n")
check("source kept", plan.source_path == str(plan_source))
check("open_path prefers live source", open_path(plan) == str(plan_source))
plan_source.unlink()
check("open_path falls back to snapshot", open_path(plan) == plan.snapshot_path)

diff = register(
    store, type=ArtifactType.DIFF, title="PR diff", repo="/tmp", tags=["auth"], diff_base="main"
)
check("diff artifact snapshots nothing", diff.snapshot_path is None and diff.source_path is None)
check("diff artifact keeps base", diff.diff_base == "main")

log_lines = [json.loads(line) for line in (Path(os.environ["TX_IDE_HOME"]) / "log.jsonl").read_text().splitlines()]
check("register logs one artifact line each", [l["type"] for l in log_lines].count("artifact") == 2)

# ----- 3. the `tx artifact` CLI --------------------------------------------------------------

service = make_service()
producer = Session(
    id="11111111-1111-1111-1111-111111111111",
    name="auth-worker",
    kind=Kind.PROCESS,
    role=Role.LLM,
    state=State.WORKING,
    cwd="/tmp",
    cmd="claude",
    tags=["auth", "p1"],
    created_at=time.time(),
)
service.store.save(producer)
os.environ["TX_SESSION_ID"] = producer.id

command = ArtifactCommand(service)
report_source = Path(tempfile.mkdtemp()) / "findings.md"
report_source.write_text("findings\n")
stdout = io.StringIO()
with contextlib.redirect_stdout(stdout):
    code = command.run(["add", "--type", "report", str(report_source)])
check("add exits 0", code == 0)
added = [a for a in store.all() if a.type is ArtifactType.REPORT]
check("add stamps the producer", added[0].session_id == producer.id and added[0].session_name == "auth-worker")
check("add inherits the producer's tags", added[0].tags == ["auth", "p1"])
check("add defaults the title to the file name", added[0].title == "findings.md")

stdout = io.StringIO()
with contextlib.redirect_stdout(stdout):
    code = command.run(["add", "--type", "doc", "--tag", "other", "--title", "notes", str(report_source)])
check("--tag overrides inheritance", [a for a in store.all() if a.title == "notes"][0].tags == ["other"])

for bad_argv, label in (
    (["add", "--type", "diff", str(report_source)], "diff add rejects a path"),
    (["add", "--type", "plan"], "file add requires a path"),
    (["add", "--type", "plan", "--base", "main", str(report_source)], "--base rejected off diff"),
):
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            command.run(bad_argv)
        check(label, False)
    except SystemExit as bail:
        check(label, bail.code == 2)

stdout = io.StringIO()
with contextlib.redirect_stdout(stdout):
    command.run(["ls", "--type", "report"])
listing = stdout.getvalue()
check("ls filters by type", "findings.md" in listing and "auth plan" not in listing)

stdout = io.StringIO()
with contextlib.redirect_stdout(stdout):
    code = command.run(["rm", plan.id[:8]])
check("rm resolves a unique prefix", code == 0 and store.load(plan.id) is None)
check("rm removes the snapshot dir", not store.snapshot_dir(plan.id).exists())

# ----- 4. renders -----------------------------------------------------------------------------

older = register(store, type=ArtifactType.DOC, title="older", repo="/tmp", tags=[])
newer = register(store, type=ArtifactType.DOC, title="newer", repo="/tmp", tags=["z"])
older.created_at, newer.created_at = 100.0, 200.0
store.save(older)
store.save(newer)
feed_rows = [row.split("\t") for row in artifact_picker_rows([older, newer], now=300.0).splitlines()]
check("feed rows are id/title/chips/visual", feed_rows[0][0] == newer.id and feed_rows[0][1] == "newer")
check("feed is newest first", [row[0] for row in feed_rows] == [newer.id, older.id])
check("feed chips are plain in field 3", feed_rows[1][2] == "" and feed_rows[0][2] == " [z]")

plain = render_artifacts([diff], now=300.0)
check("render_artifacts shows the diff target", "diff vs main @ /tmp" in plain)

# ----- 5. the picker's open spec --------------------------------------------------------------

picker = ArtifactsCommand(make_service())

file_spec = picker._companion_spec(added[0], "art-view")
check("file artifact opens its live path", str(report_source) in file_spec.cmd and "DiffviewOpen" not in file_spec.cmd)
check("companion inherits the artifact's tags", file_spec.tags == ["auth", "p1"])
check("companion is an nvim spec", file_spec.role is Role.NVIM)

diff_spec = picker._companion_spec(diff, "art-view")
check("diff artifact opens a diffview vs its base", "DiffviewOpen main" in diff_spec.cmd)
check("diff companion runs in the recorded repo", diff_spec.cwd == "/tmp")

gone = register(store, type=ArtifactType.PLAN, title="gone", repo="/tmp", tags=[], source_path=str(report_source))
Path(gone.snapshot_path).unlink()
report_source.unlink()
with contextlib.redirect_stderr(io.StringIO()):
    check("gone deliverable yields no spec", picker._companion_spec(gone, "art-view") is None)

missing_repo_diff = register(store, type=ArtifactType.DIFF, title="d", repo="/nonexistent-dir", tags=[], diff_base="main")
with contextlib.redirect_stderr(io.StringIO()):
    check("gone repo yields no diff spec", picker._companion_spec(missing_repo_diff, "art-view") is None)

print(f"OK — {PASSED} checks passed")
