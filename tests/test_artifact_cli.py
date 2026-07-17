#!/usr/bin/env python3.14
"""Artifact CLI — `tx artifact` verbs, actor resolution, renders (Plan 2, sequencing step 2).

Directly runnable (no pytest):  python3.14 tests/test_artifact_cli.py
Exits non-zero on the first failure; prints "OK — N checks passed".

Covers the settled CLI surface:
  - create stamps the actor (resolved $TX_SESSION_ID -> tmux @tx_id -> "user" sentinel, never by
    hand) and defaults the filename to the source basename; --title is carried.
  - modify with a file, and the no-file form that snapshots the working copy; the no-op notice.
  - ls (all + --session reverse lookup); show (metadata + full touch/version log + dirty flag);
    diff (default last two, explicit pair, single-rev refusal, bad rev-count rejected).
  - doctor is clean on a healthy store and exits 1 with a report once an orphan rev is planted.
  - id resolution accepts a unique prefix and rejects an ambiguous one / a miss.
  - dispatch: unknown subcommand and a missing create file exit 2; bare `artifact` prints help.

Hermetic: a temp `$TX_IDE_HOME` + a fake Tmux (no live server).
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path

os.environ["TX_IDE_HOME"] = tempfile.mkdtemp()  # before importing tx (storage reads this)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from tx.artifact import Artifact, Touch  # noqa: E402
from tx.artifact_service import ArtifactError, ArtifactNotFound, ArtifactService  # noqa: E402
from tx.artifact_store import ArtifactStore  # noqa: E402
from tx.cli import ArtifactCommand  # noqa: E402
from tx.service import SessionService  # noqa: E402
from tx.storage import artifacts_dir, ensure_home  # noqa: E402
from tx.store import SessionStore  # noqa: E402

ensure_home()
PASSED = 0
SCRATCH = Path(tempfile.mkdtemp())


def check(label, condition):
    global PASSED
    if condition:
        PASSED += 1
        return
    print(f"FAIL: {label}")
    sys.exit(1)


class FakeTmux:
    """Just enough Tmux for `ArtifactCommand._actor` (current session name + its @tx_id) and the
    default-cwd path. Never reaches a live server."""

    def __init__(self, current=None, tx_ids=None):
        self._current = current
        self._tx_ids = tx_ids or {}

    def current_session_name(self):
        return self._current

    def get_tx_id(self, name):
        return self._tx_ids.get(name)

    def current_pane_path(self):
        return str(SCRATCH)

    def has_session(self, name):
        return False


def command(tmux=None):
    service = SessionService(store=SessionStore(), tmux=tmux or FakeTmux())
    return ArtifactCommand(service)


def run(cmd, argv):
    """Run a subcommand, capturing stdout; returns (exit_code, stdout)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cmd.run(argv)
    return code, out.getvalue()


def write(name, text):
    path = SCRATCH / name
    path.write_text(text)
    return str(path)


inspect = ArtifactStore()  # a fresh view over the same temp home the commands write to


def only(predicate):
    matches = [a for a in inspect.all() if predicate(a)]
    assert len(matches) == 1, f"expected exactly one match, got {len(matches)}"
    return matches[0]


# ----- 1. create ------------------------------------------------------------------------------
os.environ["TX_SESSION_ID"] = "sess-1"
code, out = run(command(), ["create", write("plan.md", "# plan\nalpha\n"), "--title", "Auth plan"])
check("create exits 0", code == 0)
created = only(lambda a: a.title == "Auth plan")
check("create stamps the actor from $TX_SESSION_ID", created.history[0].session_id == "sess-1")
check("create defaults the filename to the source basename", created.filename == "plan.md")
check("create prints the new id", created.id in out)
check("create wrote the content", ArtifactService().content(created.id) == b"# plan\nalpha\n")

# ----- 2. actor resolution: TX_SESSION_ID -> tmux-only-if-inside -> user (QA P1) ---------------
del os.environ["TX_SESSION_ID"]
os.environ.pop("TMUX", None)  # start from a known plain-terminal state
# inside tmux ($TMUX set): resolve the current session's @tx_id.
os.environ["TMUX"] = "/private/tmp/tmux-501/default,1,0"
run(command(FakeTmux(current="Views", tx_ids={"Views": "sess-from-tmux"})), ["create", write("a.md", "a\n"), "--title", "via-tmux"])
check("inside tmux, actor is the current session's @tx_id", only(lambda a: a.title == "via-tmux").history[0].session_id == "sess-from-tmux")
# P1: a plain terminal ($TMUX unset) must NOT consult tmux — even with a live server it stays `user`.
os.environ.pop("TMUX", None)
run(command(FakeTmux(current="Views", tx_ids={"Views": "sess-from-tmux"})), ["create", write("p.md", "p\n"), "--title", "plain-terminal"])
check("outside tmux ($TMUX unset), actor is the user sentinel, never a live server session", only(lambda a: a.title == "plain-terminal").history[0].session_id == "user")
os.environ["TX_SESSION_ID"] = "sess-1"

# ----- 3. modify: file, no-file (working copy), no-op -----------------------------------------
code, out = run(command(), ["modify", created.id, write("plan.md", "# plan\nalpha\nbeta\n"), "--changes", "add beta"])
check("modify with a file exits 0 and reports the new rev", code == 0 and "rev 1" in out)
check("modify appended a touch with its session", inspect.load(created.id).history[1].session_id == "sess-1")
check("modify content is v1", ArtifactService().content(created.id) == b"# plan\nalpha\nbeta\n")

# no-file: snapshot the working copy — simulate an nvim edit landing in current, then close the loop
ArtifactService().files.write_current(inspect.load(created.id), b"# plan\nalpha\nbeta\ngamma\n")
code, out = run(command(), ["modify", created.id, "--changes", "user answers in the view"])
check("no-file modify snapshots the working copy", code == 0 and "rev 2" in out)
check("no-file modify content is the edited working copy", ArtifactService().content(created.id, rev=2) == b"# plan\nalpha\nbeta\ngamma\n")

code, out = run(command(), ["modify", created.id])
check("no-op modify prints the notice and adds no rev", "No change" in out and len(inspect.load(created.id).history) == 3)

# ----- 4. ls + ls --session -------------------------------------------------------------------
code, out = run(command(), ["ls"])
check("ls lists all artifacts newest-first with the header", out.startswith("ARTIFACTS") and created.id[:8] in out)
check("ls shows the revision count", "3r" in out)
code, out = run(command(), ["ls", "--session", "sess-from-tmux"])
check("ls --session is the reverse lookup", "via-tmux" in out and "Auth plan" not in out)
code, out = run(command(), ["ls", "--session", "nobody"])
check("ls --session for a stranger is empty", out.strip() == "ARTIFACTS\n  (none)".strip() or "(none)" in out)

# ----- 5. show --------------------------------------------------------------------------------
code, out = run(command(), ["show", created.id])
check("show prints metadata", code == 0 and "filename:   plan.md" in out and "revisions:  3" in out)
check("show prints the full touch log", out.count("rev ") >= 3 and "add beta" in out and "user answers in the view" in out)
check("show reports a clean working copy", "working:    clean" in out)
# dirty flag: edit current without snapshotting, then show flags it
ArtifactService().files.write_current(inspect.load(created.id), b"dirty edit\n")
code, out = run(command(), ["show", created.id])
check("show flags a dirty working copy", "working:    dirty" in out)
ArtifactService().files.write_current(inspect.load(created.id), ArtifactService().content(created.id, rev=2))  # restore clean

# ----- 6. diff --------------------------------------------------------------------------------
code, out = run(command(), ["diff", created.id, "0", "1"])
check("diff of an explicit pair is a unified diff", code == 0 and out.startswith("--- rev0") and "+beta\n" in out)
code, out_default = run(command(), ["diff", created.id])
check("diff default is the last two revs", "--- rev1" in out_default and "+++ rev2" in out_default)
single = only(lambda a: a.title == "plain-terminal")
try:
    run(command(), ["diff", single.id])
    check("diff of a single-rev artifact refuses", False)
except ArtifactError:
    check("diff of a single-rev artifact refuses", True)
try:
    with contextlib.redirect_stderr(io.StringIO()):
        run(command(), ["diff", created.id, "0"])  # one rev only
    check("diff with one rev is rejected", False)
except SystemExit as bail:
    check("diff with one rev is rejected", bail.code == 2)

# ----- 7. doctor + --repair -------------------------------------------------------------------
code, out = run(command(), ["doctor"])
check("doctor is clean and exits 0", code == 0 and "clean" in out)
(artifacts_dir() / created.id / "revs" / "9.md").write_bytes(b"planted orphan\n")
code, out = run(command(), ["doctor"])
check("doctor reports the orphan and exits 1", code == 1 and "orphan rev file 9" in out)
code, out = run(command(), ["doctor", "--repair"])
check("doctor --repair removes the orphan and exits clean", code == 0 and "removed orphan rev file 9" in out and "clean" in out)
check("the orphan file is gone after --repair", not (artifacts_dir() / created.id / "revs" / "9.md").exists())

# ----- 8. id resolution (isolated store so bare records don't pollute doctor above) ------------
iso = ArtifactService(store=ArtifactStore(Path(tempfile.mkdtemp())))
for suffix in ("1111", "2222"):
    iso.store.save(Artifact(id=f"abcd{suffix}-0000-0000-0000-000000000000", title=None, filename="x.md",
                            created_at=1.0, history=[Touch("s", 1.0, 0, None)]))
iso.store.save(Artifact(id="ffff9999-0000-0000-0000-000000000000", title=None, filename="y.md",
                        created_at=1.0, history=[Touch("s", 1.0, 0, None)]))
check("resolve_id accepts a unique prefix", iso.resolve_id("ffff") == "ffff9999-0000-0000-0000-000000000000")
check("resolve_id accepts a full id", iso.resolve_id("abcd1111-0000-0000-0000-000000000000").startswith("abcd1111"))
try:
    iso.resolve_id("abcd")
    check("resolve_id rejects an ambiguous prefix", False)
except ArtifactError:
    check("resolve_id rejects an ambiguous prefix", True)
try:
    iso.resolve_id("zzzz")
    check("resolve_id raises on a miss", False)
except ArtifactNotFound:
    check("resolve_id raises on a miss", True)

# ----- 9. dispatch edges ----------------------------------------------------------------------
code, _ = run(command(), [])
check("bare `artifact` prints help, exit 0", code == 0)
with contextlib.redirect_stderr(io.StringIO()):
    code, _ = run(command(), ["bogus"])
check("unknown subcommand exits 2", code == 2)
try:
    with contextlib.redirect_stderr(io.StringIO()):
        run(command(), ["create", "/no/such/file/anywhere"])
    check("create of a missing file exits 2", False)
except SystemExit as bail:
    check("create of a missing file exits 2", bail.code == 2)

print(f"OK — {PASSED} checks passed")
