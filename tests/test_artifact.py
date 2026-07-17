#!/usr/bin/env python3.14
"""Artifact subsystem core — entity, store/content, service (Plan 2, sequencing step 1).

No pytest in this repo, so this is directly runnable:  python3.14 tests/test_artifact.py
Exits non-zero on the first failure; prints "OK — N checks passed" (the house gate convention).

Covers the plan's testing strategy, items 1–6:
  1. `create` writes `revs/0.<ext>` + `current.<ext>` + a record with one `Touch(rev=0)`; `content`
     matches; the extension is preserved from the source filename.
  2. `modify` writes `revs/1.<ext>`, refreshes `current`, grows `history` to two entries, advances
     the DERIVED `updated_at`, and `content` returns v1; identical content is a no-op (no rev/touch).
  3. `diff(0, 1)` is a sane `difflib` unified diff; `content(rev=n)` returns each version; `diff`
     refuses cleanly on non-utf-8 content (which is accepted, per the plan-body 'any bytes' override).
  4. `history` reflects the touching sessions incl. a `"user"`-sentinel touch; `artifacts_for_session`
     reverse-resolves by query.
  5. `from_dict(to_dict())` round-trips; the strict boundary rejects a bad version, empty history,
     and non-contiguous revs; `updated_at` is derived, never stored.
  6. Concurrency + crash: two racing modifys -> exactly one wins, the loser gets the conflict error;
     a hand-planted orphan `revs/<n>` is ignored on read, flagged by `doctor`, overwritten by the
     next `modify`. Plus: a dirty working copy is a flagged-but-not-error state; `all()` tolerates an
     unreadable record; every mutation and content read leaves an EventLog line.

Hermetic: a temp `$TX_IDE_HOME` (records + content + log). No live server, no network.
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

from tx.artifact import (  # noqa: E402
    ARTIFACT_SCHEMA_VERSION,
    USER_ACTOR,
    Artifact,
    Touch,
    UnsupportedArtifactError,
)
from tx.artifact_service import (  # noqa: E402
    ArtifactConflict,
    ArtifactError,
    ArtifactNotFound,
    ArtifactService,
)
from tx.artifact_store import ArtifactContent, ArtifactStore  # noqa: E402
from tx.events import EventLog  # noqa: E402
from tx.storage import artifacts_dir, ensure_home  # noqa: E402

ensure_home()
PASSED = 0


def check(label, condition):
    global PASSED
    if condition:
        PASSED += 1
        return
    print(f"FAIL: {label}")
    sys.exit(1)


def log_lines():
    path = Path(os.environ["TX_IDE_HOME"]) / "log.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def content_dir(artifact_id):
    return artifacts_dir() / artifact_id


service = ArtifactService()

# ----- 0. layout + constants -------------------------------------------------------------------
check("ensure_home() created artifacts/", artifacts_dir().is_dir())
check("ARTIFACT_SCHEMA_VERSION is 1", ARTIFACT_SCHEMA_VERSION == 1)

# ----- 1. create ------------------------------------------------------------------------------
plan = service.create("sess-A", b"# the plan\nalpha\n", title="Auth plan", filename="plan.md")
check("create returns an Artifact with a fresh id", isinstance(plan, Artifact) and len(plan.id) == 36)
check("create wrote revs/0.md", (content_dir(plan.id) / "revs" / "0.md").is_file())
check("create wrote current.md", (content_dir(plan.id) / "current.md").is_file())
check("record persisted", service.store.load(plan.id) is not None)
check("history has exactly the create touch", len(plan.history) == 1 and plan.history[0].rev == 0)
check("create touch carries the creator session", plan.history[0].session_id == "sess-A")
check("create touch has no changes note", plan.history[0].changes is None)
check("content() matches the created bytes", service.content(plan.id) == b"# the plan\nalpha\n")
check("extension preserved from the source filename", plan.extension == ".md")
check("title stored", service.store.load(plan.id).title == "Auth plan")

# ----- 2. modify + no-op ----------------------------------------------------------------------
created_at = service.store.load(plan.id).updated_at
plan = service.modify(plan.id, "sess-B", b"# the plan\nalpha\nbeta\n", changes="add beta")
check("modify wrote revs/1.md", (content_dir(plan.id) / "revs" / "1.md").is_file())
check("modify refreshed current to v1", service.content(plan.id) == b"# the plan\nalpha\nbeta\n")
check("history grew to two entries", len(plan.history) == 2 and plan.history[1].rev == 1)
check("second touch carries its session + note", plan.history[1].session_id == "sess-B" and plan.history[1].changes == "add beta")
check("derived updated_at advanced", plan.updated_at >= created_at and plan.updated_at == plan.history[-1].at)
check("content(rev=0) still returns v0", service.content(plan.id, rev=0) == b"# the plan\nalpha\n")
check("content(rev=1) returns v1", service.content(plan.id, rev=1) == b"# the plan\nalpha\nbeta\n")

before = len(service.store.load(plan.id).history)
noop = service.modify(plan.id, "sess-B", b"# the plan\nalpha\nbeta\n")
check("no-op modify adds no touch", len(noop.history) == before)
check("no-op modify wrote no revs/2.md", not (content_dir(plan.id) / "revs" / "2.md").exists())

# ----- 3. diff --------------------------------------------------------------------------------
diff_default = service.diff(plan.id)  # last two = rev0..rev1
diff_explicit = service.diff(plan.id, 0, 1)
check("diff default is the last two revs", diff_default == diff_explicit)
check("diff is a unified difflib diff", diff_explicit.startswith("--- rev0") and "+++ rev1" in diff_explicit)
check("diff shows the added line", "+beta\n" in diff_explicit)
single = service.create("sess-A", b"only\n", filename="one.txt")
try:
    service.diff(single.id)
    check("diff of a single-rev artifact refuses", False)
except ArtifactError:
    check("diff of a single-rev artifact refuses", True)

# ----- 4. actors + reverse lookup -------------------------------------------------------------
plan = service.modify(plan.id, USER_ACTOR, b"# the plan\nalpha\nbeta\ngamma\n", changes="user answers")
actors = [touch.session_id for touch in service.store.load(plan.id).history]
check("history records the user sentinel touch", USER_ACTOR in actors and "sess-A" in actors and "sess-B" in actors)
by_a = service.artifacts_for_session("sess-A")
by_user = service.artifacts_for_session(USER_ACTOR)
check("artifacts_for_session(sess-A) finds the two it created", {a.id for a in by_a} == {plan.id, single.id})
check("artifacts_for_session(user) finds the plan it touched", [a.id for a in by_user] == [plan.id])
check("artifacts_for_session is empty for a stranger", service.artifacts_for_session("nobody") == [])

# ----- 5. round-trip + strict boundary --------------------------------------------------------
record = service.store.load(plan.id)
check("from_dict(to_dict()) round-trips", Artifact.from_dict(record.to_dict()).to_dict() == record.to_dict())
check("updated_at is derived, never stored", "updated_at" not in record.to_dict())
check("to_dict carries its own version line", record.to_dict()["artifact_schema_version"] == 1)

good = record.to_dict()
for mutate, label in (
    (lambda d: {**d, "artifact_schema_version": 2}, "bad version rejected"),
    (lambda d: {**d, "history": []}, "empty history rejected"),
    (lambda d: {**d, "history": [d["history"][0], {**d["history"][1], "rev": 7}]}, "non-contiguous revs rejected"),
    (lambda d: {**d, "unexpected": 1}, "an unexpected top-level key is rejected (strict boundary)"),
    (lambda d: {k: v for k, v in d.items() if k != "title"}, "a missing top-level key is rejected"),
    (lambda d: {**d, "history": [{**d["history"][0], "junk": 9}, *d["history"][1:]]}, "an unexpected key inside a history entry is rejected"),
):
    try:
        Artifact.from_dict(mutate(good))
        check(label, False)
    except UnsupportedArtifactError:
        check(label, True)

try:
    service.content("no-such-id")
    check("a missing artifact raises ArtifactNotFound", False)
except ArtifactNotFound:
    check("a missing artifact raises ArtifactNotFound", True)

# ----- 6a. binary content (accepted; diff refuses) --------------------------------------------
blob = service.create("sess-A", b"\xff\xfe\x00\x01raw", filename="blob.bin")
service.modify(blob.id, "sess-A", b"\xff\xfe\x00\x02raw")
check("binary content is accepted and round-trips", service.content(blob.id, rev=0) == b"\xff\xfe\x00\x01raw")
check("extensionless-vs-binary: extension is preserved (.bin)", blob.extension == ".bin")
try:
    service.diff(blob.id, 0, 1)
    check("diff refuses cleanly on non-utf-8", False)
except ArtifactError:
    check("diff refuses cleanly on non-utf-8", True)

noext = service.create("sess-A", b"Makefile body\n", filename="Makefile")
check("an extensionless source names revs bare (revs/0)", (content_dir(noext.id) / "revs" / "0").is_file())
check("extensionless artifact reports empty extension", noext.extension == "")

# ----- 6b. concurrency: two racing modifys — the loser ALWAYS conflicts ------------------------
race = service.create("s", b"v0\n", filename="race.txt")
copy_one = service.store.load(race.id)
copy_two = service.store.load(race.id)
service._apply_modify(copy_one, "s1", b"v1-winner\n", None)  # commits rev1
try:
    service._apply_modify(copy_two, "s2", b"v1-loser\n", None)  # stale -> collides on rev1
    check("the losing racer gets a conflict (record advanced)", False)
except ArtifactConflict:
    check("the losing racer gets a conflict (record advanced)", True)
check("the winner's content stands", service.content(race.id, rev=1) == b"v1-winner\n")
check("exactly one touch committed for the race", len(service.store.load(race.id).history) == 2)

# The adversarial window (QA P0): a rev linked but its record NOT yet saved must ALSO conflict — a
# live in-flight claimer is indistinguishable from crash debris, so `modify` NEVER overwrites it.
windowed = service.create("s", b"w0\n", filename="window.txt")
(content_dir(windowed.id) / "revs" / "1.txt").write_bytes(b"in-flight\n")  # a claimer's link, unrecorded
try:
    service.modify(windowed.id, "s2", b"w1-loser\n")  # record still 1 entry -> must conflict
    check("a collision on an unrecorded rev conflicts, never overwrites", False)
except ArtifactConflict:
    check("a collision on an unrecorded rev conflicts, never overwrites", True)
check("the in-flight rev is left untouched", (content_dir(windowed.id) / "revs" / "1.txt").read_bytes() == b"in-flight\n")

# ----- 6c. crash orphan: ignored on read, flagged by doctor, reclaimed by repair (B2, amended) -
orphaned = service.create("s", b"o0\n", filename="orphan.txt")
(content_dir(orphaned.id) / "revs" / "1.txt").write_bytes(b"orphan-bytes\n")  # a crash left this
check("orphan ignored on read (content is still v0)", service.content(orphaned.id) == b"o0\n")
check("doctor flags the orphan rev", any("orphan rev file 1" in p and orphaned.id in p for p in service.doctor()))
try:
    service.modify(orphaned.id, "s", b"o1\n")  # slot 1 occupied -> conflict, NOT overwrite
    check("modify conflicts on an orphan-occupied slot (never overwrites)", False)
except ArtifactConflict:
    check("modify conflicts on an orphan-occupied slot (never overwrites)", True)
removed = service.repair_orphans()
check("repair removes the orphan", any("removed orphan rev file 1" in r and orphaned.id in r for r in removed))
check("doctor is clean about that artifact after repair", not any("orphan" in p and orphaned.id in p for p in service.doctor()))
reclaimed = service.modify(orphaned.id, "s", b"o1\n")  # slot now free -> succeeds
check("modify succeeds once the orphan is repaired", reclaimed.latest_rev == 1 and service.content(orphaned.id, rev=1) == b"o1\n")

# ----- 6d. dirty working copy is a visible state, not an error --------------------------------
files = ArtifactContent()
dirty = service.create("s", b"clean\n", filename="dirty.txt")
files.write_current(dirty, b"edited-in-nvim\n")  # simulate an un-snapshotted view edit
check("content() returns the dirty working copy", service.content(dirty.id) == b"edited-in-nvim\n")
check("content(rev=0) still returns the snapshot", service.content(dirty.id, rev=0) == b"clean\n")
check("doctor flags the dirty current", any("dirty" in p and dirty.id in p for p in service.doctor()))
service.modify(dirty.id, "s", b"edited-in-nvim\n")  # the no-file close snapshots it
check("modify closes the dirty state", not any("dirty" in p and dirty.id in p for p in service.doctor()))

# ----- 6e. store tolerates an unreadable record -----------------------------------------------
(artifacts_dir() / "garbage.json").write_text("{not json")
stderr = io.StringIO()
with contextlib.redirect_stderr(stderr):
    survivors = ArtifactStore().all()
check("all() skips an unreadable record with a warning", "garbage" in stderr.getvalue())
check("all() still returns the good records", all(isinstance(a, Artifact) for a in survivors) and len(survivors) >= 5)
(artifacts_dir() / "garbage.json").unlink()

# ----- 6f. EventLog: mutations + content reads leave a line -----------------------------------
types = [entry["type"] for entry in log_lines()]
check("create logs artifact-create", "artifact-create" in types)
check("modify logs artifact-modify", "artifact-modify" in types)
check("content read logs artifact-read", "artifact-read" in types)
check("diff logs artifact-diff", "artifact-diff" in types)
create_line = next(e for e in log_lines() if e["type"] == "artifact-create")
check("create log line carries the actor", create_line["actor"] == "sess-A")

# ----- 6g. doctor detection contract: recordless dir + mutation<->log (isolated store) --------
iso_home = Path(tempfile.mkdtemp())
iso = ArtifactService(
    store=ArtifactStore(iso_home),
    content=ArtifactContent(iso_home),
    log=EventLog(iso_home / "log.jsonl"),
)
iso_artifact = iso.create("s", b"body\n", filename="iso.md")
check("a healthy isolated store is doctor-clean", iso.doctor() == [])
# a content directory under artifacts/ with no record = out-of-band creation (report-only)
(iso_home / "recordless" / "revs").mkdir(parents=True)
(iso_home / "recordless" / "revs" / "0.md").write_bytes(b"planted out of band\n")
check("doctor flags a recordless content dir", any("recordless" in p and "no record" in p for p in iso.doctor()))
# a history touch with no matching EventLog line = suspected bypass (a truncated log is legitimate)
(iso_home / "log.jsonl").write_text("")
suspicions = iso.doctor()
check(
    "doctor flags a touch with no EventLog line as suspicion",
    any(iso_artifact.id in p and "no matching EventLog" in p and "suspected" in p for p in suspicions),
)

print(f"OK — {PASSED} checks passed")
