"""ART — artifact records, content layout, `tx artifact` verbs (spec §04 ART, T-ART-01..28).

Every case drives `tx artifact …` and asserts on stdout/stderr/exit, the files under
`$TX_IDE_HOME/artifacts/`, `log.jsonl`, and (for `open`) the private tmux server + the fake `nvim`.
T-ART-14 is DROPPED (no verb reaches `content()`).
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from txkit import TxCase, expected_failure_on_python

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"

ORPHAN = "orphan rev file {rev} not in history (crash debris — ignored on read; blocks that slot until `doctor --repair`)"
DIRTY = "working copy differs from last rev {rev} (dirty — un-snapshotted edits; close with `tx artifact modify`)"
NO_LOG_LINE = "rev {rev} touch has no matching EventLog mutation line (suspected out-of-band write — a truncated log is also possible)"
STRAY = "content directory under artifacts/ has no record (out-of-band creation — the record is authoritative)"
CONFLICT = (
    "tx artifact: artifact {id}: rev {rev} is already claimed — the artifact moved on (a concurrent "
    "write), or a crashed write left an orphan (`tx artifact doctor --repair` clears an orphan). "
    "Re-read and retry.\n"
)
V1_SKIP = (
    "tx: skipping unreadable artifact {name}: record artifact_schema_version={version} is "
    "unsupported (expected 2); tx-ide does not back-migrate older artifact records"
)
HELP_ROWS = (
    ("create <file> [--title T] [--group G]", "register a new artifact from a file"),
    ("modify <id> [<file>] [--changes ...]", "snapshot a new revision (no file = the working copy)"),
    ("group <id> [<group> | --clear]", "read or set the effort-group override"),
    ("ls [--session S]", "list artifacts (--session: what a session touched)"),
    ("show <id>", "metadata + the full touch/version log"),
    ("diff <id> [<revA> <revB>]", "difflib diff between two revisions (default: last two)"),
    ("open <id> [--tag T] [--cwd D]", "open the working copy in an nvim view bound to the artifact"),
    ("doctor [--repair]", "check store invariants; --repair removes orphan rev files"),
)
HELP = "usage: tx artifact <subcommand> [args]\n\nsubcommands:\n" + "".join(
    f"  {usage:<40} {summary}\n" for usage, summary in HELP_ROWS
)

# The T-ART-01 hand-written record: compact JSON, keys deliberately shuffled.
HAND_WRITTEN_A = (
    '{"history":[{"rev":0,"changes":null,"at":1.5,"session_id":"s1"},'
    '{"changes":"tweak","session_id":"user","rev":1,"at":2.0}],"group":null,"created_at":1.5,'
    '"filename":"plan.md","title":null,"id":"a","artifact_schema_version":2}'
)
CANONICAL_KEYS = ["artifact_schema_version", "id", "title", "filename", "created_at", "group", "history"]
TOUCH_KEYS = ["session_id", "at", "rev", "changes"]


def snapshot(directory: Path) -> dict[str, bytes]:
    """Every file under `directory` → its bytes (a byte-identical before/after probe)."""
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


class TestArt(TxCase):
    # ----- helpers -----------------------------------------------------------------------------

    def file(self, name: str, content: str | bytes) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)
        return path

    def create(self, name: str, content: str | bytes, *extra: str, env: dict | None = None) -> str:
        result = self.tx(["artifact", "create", str(self.file(name, content)), *extra], env=env)
        self.assertEqual(result.code, 0, result.err)
        match = re.fullmatch(rf"Created artifact ({UUID}) \({re.escape(Path(name).name)}\)\n", result.out)
        self.assertIsNotNone(match, result.out)
        return match.group(1)

    def modify(self, artifact_id: str, name: str, content: str | bytes, *extra: str, env: dict | None = None):
        result = self.tx(["artifact", "modify", artifact_id, str(self.file(name, content)), *extra], env=env)
        self.assertEqual(result.code, 0, result.err)
        return result

    def record(self, artifact_id: str) -> dict:
        return json.loads(self.records.artifact_record_path(artifact_id).read_text())

    def record_bytes(self, artifact_id: str) -> bytes:
        return self.records.artifact_record_path(artifact_id).read_bytes()

    def revs(self, artifact_id: str) -> Path:
        return self.records.artifact_dir(artifact_id) / "revs"

    def current(self, artifact_id: str, extension: str = ".md") -> Path:
        return self.records.artifact_dir(artifact_id) / f"current{extension}"

    def write_hand_written_a(self) -> None:
        self.records.artifact(id="a", revs={0: "v0\n", 1: "v1\n"})
        self.records.artifact_record_path("a").write_text(HAND_WRITTEN_A)

    def tmp_siblings(self, directory: Path) -> list[str]:
        return [path.name for path in directory.iterdir() if path.name.endswith(".tmp")]

    def broken_store(self) -> dict[str, str]:
        """The T-ART-17 store: A..E built through the CLI (so their touches are logged), then
        broken by hand, plus a stray content dir."""
        ids = {}
        ids["A"] = self.create("a.md", "a")
        (self.revs(ids["A"]) / "5.md").write_text("debris")
        ids["B"] = self.create("b.md", "b")
        self.modify(ids["B"], "b1.md", "b1")
        (self.revs(ids["B"]) / "1.md").unlink()
        ids["C"] = self.create("c.md", "c")
        self.current(ids["C"]).write_text("c-edited")
        ids["D"] = self.create("d.md", "d")
        self.current(ids["D"]).unlink()
        ids["E"] = self.create("e.md", "e")
        (self.revs(ids["E"]) / "1.md").write_text("e1")
        self.current(ids["E"]).write_text("e1")
        record = self.record(ids["E"])
        record["history"].append({"session_id": "user", "at": time.time(), "rev": 1, "changes": None})
        self.records.artifact_record_path(ids["E"]).write_text(json.dumps(record, indent=2))
        (self.home.artifacts_dir / "zzz-stray").mkdir()
        return ids

    def broken_store_lines(self, ids: dict[str, str]) -> list[str]:
        per_artifact = {
            ids["A"]: [ORPHAN.format(rev=5)],
            ids["B"]: ["history rev 1 has no file on disk"],
            ids["C"]: [DIRTY.format(rev=0)],
            ids["D"]: ["working copy current.md is missing"],
            ids["E"]: [NO_LOG_LINE.format(rev=1)],
        }
        lines = [
            f"  {artifact_id}: {line}"
            for artifact_id in sorted(per_artifact)
            for line in per_artifact[artifact_id]
        ]
        return [*lines, f"  zzz-stray: {STRAY}"]

    # ----- T-ART-01 / 05: record round-trip + atomic save ----------------------------------

    def test_t_art_01_record_round_trip(self):
        self.write_hand_written_a()
        result = self.tx(["artifact", "group", "a", "g"])
        self.assertEqual((result.code, result.out, result.err), (0, "Grouped artifact a (group=g)\n", ""))
        with_group = self.record_bytes("a")
        result = self.tx(["artifact", "group", "a", "--clear"])
        self.assertEqual(
            (result.code, result.out, result.err),
            (0, "Cleared group override on artifact a (back to derived)\n", ""),
        )
        cleared = self.record_bytes("a")
        self.assertEqual(with_group, cleared.replace(b'"group": null', b'"group": "g"'))
        self.assertFalse(cleared.endswith(b"\n"))
        record = json.loads(cleared)
        self.assertEqual(list(record), CANONICAL_KEYS)
        self.assertEqual([list(touch) for touch in record["history"]], [TOUCH_KEYS, TOUCH_KEYS])
        self.assertNotIn("updated_at", record)
        self.assertEqual((record["created_at"], record["history"][1]["at"]), (1.5, 2.0))
        self.assertEqual(json.loads(cleared), json.loads(HAND_WRITTEN_A))

        listing = self.tx(["artifact", "ls"])
        self.assertEqual(listing.code, 0, listing.err)
        self.assertRegex(listing.lines[1], r"^  a           2r  \s*\d+d  plan\.md")
        days = int(re.search(r"(\d+)d", listing.lines[1]).group(1))
        self.assertEqual(days, int(time.time() - 2.0) // 86400)

        modified = self.tx(["artifact", "modify", "a", str(self.file("v2.md", "v2\n"))])
        self.assertEqual((modified.code, modified.out), (0, "Modified artifact a → rev 2\n"))

        plan_id = self.create("plan.md", "p")
        self.assertTrue((self.revs(plan_id) / "0.md").is_file())
        self.assertTrue(self.current(plan_id).is_file())
        bare_id = self.create("artifact", "b")
        self.assertTrue((self.revs(bare_id) / "0").is_file())
        self.assertTrue(self.current(bare_id, "").is_file())

        self.assert_golden("art/01", cleared.decode())

    def test_t_art_05_record_save_atomic_and_load(self):
        self.write_hand_written_a()
        result = self.tx(["artifact", "group", "a", "g"])
        self.assertEqual(result.code, 0, result.err)
        saved = self.record_bytes("a")
        self.assertFalse(saved.endswith(b"\n"))
        self.assertEqual(json.loads(saved)["group"], "g")
        self.assertEqual(list(json.loads(saved)), CANONICAL_KEYS)
        self.assertEqual(self.tmp_siblings(self.home.artifacts_dir), [])

        shown = self.tx(["artifact", "show", "a"])
        self.assertEqual(shown.code, 0, shown.err)
        missing = self.tx(["artifact", "show", "missing"])
        self.assertEqual((missing.code, missing.out, missing.err), (1, "", "tx artifact: artifact 'missing' not found\n"))

        (self.home.artifacts_dir / "v1.json").write_text('{"artifact_schema_version": 1, "id": "v1"}')
        listing = self.tx(["artifact", "ls"])
        self.assertEqual(listing.code, 0)
        self.assertEqual(listing.err, V1_SKIP.format(name="v1.json", version=1) + "\n")

        created = self.create("plan.md", "hello")
        self.assertFalse(self.record_bytes(created).endswith(b"\n"))
        self.assertEqual(self.tmp_siblings(self.home.artifacts_dir), [])

        self.assert_golden("art/05", saved.decode())

    # ----- T-ART-02..04, 06: the tolerant store scan ------------------------------------------

    def test_t_art_02_record_version_guard(self):
        (self.home.artifacts_dir / "old.json").write_text('{"artifact_schema_version": 1, "id": "old", "foo": 1}')
        nover = {
            "id": "nover", "title": None, "filename": "plan.md", "created_at": 1.0, "group": None,
            "history": [{"session_id": "s1", "at": 1.0, "rev": 0, "changes": None}],
        }
        (self.home.artifacts_dir / "nover.json").write_text(json.dumps(nover))
        result = self.tx(["artifact", "ls"])
        self.assertEqual((result.code, result.out), (0, "ARTIFACTS\n  (none)\n"))
        self.assertEqual(
            result.err.splitlines(),
            [V1_SKIP.format(name="nover.json", version="None"), V1_SKIP.format(name="old.json", version=1)],
        )

    @expected_failure_on_python
    def test_t_art_02_fixed_show_named_v1_record(self):
        (self.home.artifacts_dir / "old.json").write_text('{"artifact_schema_version": 1, "id": "old", "foo": 1}')
        result = self.tx(["artifact", "show", "old"])
        self.assertEqual((result.code, result.out), (1, ""))
        self.assertIn("artifact_schema_version=1 is unsupported", result.err)
        self.assertNotIn("Traceback", result.err)

    def test_t_art_03_exact_key_set_enforcement(self):
        keys = {
            "artifact_schema_version": 2, "id": "keys", "title": None, "filename": "plan.md",
            "created_at": 1.0, "foo": 1,
            "history": [{"session_id": "s1", "at": 1.0, "rev": 0, "changes": None}],
        }
        touchkeys = {
            "artifact_schema_version": 2, "id": "touchkeys", "title": None, "filename": "plan.md",
            "created_at": 1.0, "group": None,
            "history": [{"session_id": "s1", "at": 1.0, "rev": 0, "changes": None, "how": "create"}],
        }
        (self.home.artifacts_dir / "keys.json").write_text(json.dumps(keys))
        (self.home.artifacts_dir / "touchkeys.json").write_text(json.dumps(touchkeys))
        result = self.tx(["artifact", "ls"])
        self.assertEqual((result.code, result.out), (0, "ARTIFACTS\n  (none)\n"))
        self.assertEqual(
            result.err.splitlines(),
            [
                "tx: skipping unreadable artifact keys.json: artifact 'keys' has an invalid key set: "
                "unexpected ['foo']; missing ['group']",
                "tx: skipping unreadable artifact touchkeys.json: a history entry has an invalid key set: "
                "unexpected ['how']",
            ],
        )

    def test_t_art_04_history_invariants(self):
        def write(stem: str, revs: list[int]) -> None:
            record = {
                "artifact_schema_version": 2, "id": stem, "title": None, "filename": "plan.md",
                "created_at": 1.0, "group": None,
                "history": [{"session_id": "s1", "at": 1.0, "rev": rev, "changes": None} for rev in revs],
            }
            (self.home.artifacts_dir / f"{stem}.json").write_text(json.dumps(record))

        write("empty", [])
        write("gap02", [0, 2])
        write("gap12", [1, 2])
        result = self.tx(["artifact", "ls"])
        self.assertEqual((result.code, result.out), (0, "ARTIFACTS\n  (none)\n"))
        self.assertEqual(
            result.err.splitlines(),
            [
                "tx: skipping unreadable artifact empty.json: artifact 'empty' has an empty history "
                "(entry 0 must be the create)",
                "tx: skipping unreadable artifact gap02.json: artifact 'gap02' has non-contiguous rev "
                "numbers [0, 2] (expected 0..1, one per touch)",
                "tx: skipping unreadable artifact gap12.json: artifact 'gap12' has non-contiguous rev "
                "numbers [1, 2] (expected 0..1, one per touch)",
            ],
        )

    def test_t_art_06_store_scan_tolerant(self):
        self.records.artifact(id="good")
        # `good` was crafted, so give doctor the create line a real `tx artifact create` would leave.
        self.home.log_path.write_text(
            json.dumps({"ts": 1.0, "actor": "s1", "type": "artifact-create", "msg": "good plan.md"}) + "\n"
        )
        (self.home.artifacts_dir / "bad.json").write_text("{")
        (self.home.artifacts_dir / "old.json").write_text('{"artifact_schema_version": 1, "id": "old"}')
        (self.home.artifacts_dir / "x").mkdir()
        (self.home.artifacts_dir / ".good.abc.tmp").write_text("{}")
        result = self.tx(["artifact", "ls"])
        self.assertEqual(result.code, 0)
        self.assertEqual(len(result.lines), 2)
        self.assertEqual(result.lines[0], "ARTIFACTS")
        self.assertTrue(result.lines[1].startswith("  good        1r  "), result.lines[1])
        errors = result.err.splitlines()
        self.assertEqual(len(errors), 2, result.err)
        self.assertTrue(errors[0].startswith("tx: skipping unreadable artifact bad.json: "), errors[0])
        self.assertEqual(errors[1], V1_SKIP.format(name="old.json", version=1))

        doctor = self.tx(["artifact", "doctor"])
        self.assertEqual((doctor.code, doctor.out), (1, f"  x: {STRAY}\nartifacts: 1 problem(s)\n"))
        self.assertEqual(len(doctor.err.splitlines()), 2)

    # ----- T-ART-07..09: content layout, rev claim, doctor detection ---------------------------

    def test_t_art_07_content_path_layout(self):
        artifact_id = self.create("plan.md", "p0")
        for rev in (1, 2, 3):
            self.modify(artifact_id, f"p{rev}.md", f"p{rev}")
        directory = self.home.artifacts_dir / artifact_id
        self.assertEqual(
            sorted(path.name for path in (directory / "revs").iterdir()), ["0.md", "1.md", "2.md", "3.md"]
        )
        self.assertEqual((directory / "current.md").read_text(), "p3")

        opened = self.tx(["artifact", "open", artifact_id])
        self.assertEqual(opened.code, 0, opened.err)
        self.assertTrue(opened.out.endswith(f"({directory}/current.md)\n"), opened.out)
        view = json.loads(self.tx(["show", f"art-{artifact_id[:8]}"]).out)
        self.assertEqual((view["role"], view["cwd"]), ("nvim", str(directory)))

        bare_id = self.create("artifact", "b")
        bare = self.home.artifacts_dir / bare_id
        self.assertEqual(sorted(path.name for path in bare.iterdir()), ["current", "revs"])
        self.assertEqual([path.name for path in (bare / "revs").iterdir()], ["0"])

    def test_t_art_08_rev_claim_exclusive(self):
        artifact_id = self.create("v0.md", "v0")
        (self.revs(artifact_id) / "1.md").write_text("other")
        result = self.tx(["artifact", "modify", artifact_id, str(self.file("new.md", "new"))])
        self.assertEqual((result.code, result.out, result.err), (1, "", CONFLICT.format(id=artifact_id, rev=1)))
        self.assertEqual((self.revs(artifact_id) / "0.md").read_text(), "v0")
        self.assertEqual((self.revs(artifact_id) / "1.md").read_text(), "other")
        self.assertEqual(self.current(artifact_id).read_text(), "v0")
        self.assertEqual(self.tmp_siblings(self.revs(artifact_id)), [])
        self.assertEqual(len(self.record(artifact_id)["history"]), 1)

    def test_t_art_09_orphan_missing_dirty_detection(self):
        artifact_id = self.create("p.md", "p0")
        self.modify(artifact_id, "p1.md", "p1")
        revs = self.revs(artifact_id)
        (revs / ".1.xyz.tmp").write_text("stage")
        (revs / "7.md").write_text("orphan")
        (revs / "notes.txt").write_text("foreign")
        doctor = self.tx(["artifact", "doctor"])
        self.assertEqual(
            (doctor.code, doctor.out),
            (1, f"  {artifact_id}: {ORPHAN.format(rev=7)}\nartifacts: 1 problem(s)\n"),
        )
        shown = self.tx(["artifact", "show", artifact_id])
        self.assertIn("\n  working:    clean\n", shown.out)

        self.current(artifact_id).write_text("c")
        doctor = self.tx(["artifact", "doctor"])
        self.assertEqual(
            (doctor.code, doctor.out),
            (
                1,
                f"  {artifact_id}: {ORPHAN.format(rev=7)}\n"
                f"  {artifact_id}: {DIRTY.format(rev=1)}\n"
                "artifacts: 2 problem(s)\n",
            ),
        )
        shown = self.tx(["artifact", "show", artifact_id])
        self.assertIn("\n  working:    dirty — un-snapshotted edits (close with `tx artifact modify`)\n", shown.out)

        (revs / "1.md").unlink()
        doctor = self.tx(["artifact", "doctor"])
        self.assertEqual(
            (doctor.code, doctor.out),
            (
                1,
                f"  {artifact_id}: {ORPHAN.format(rev=7)}\n"
                f"  {artifact_id}: history rev 1 has no file on disk\n"
                "artifacts: 2 problem(s)\n",
            ),
        )

    # ----- T-ART-10..13: create / modify / snapshot / conflict ---------------------------------

    def test_t_art_10_create_record_files_log(self):
        artifact_id = self.create("plan.md", "hello", "--title", "T", env={"TX_SESSION_ID": "s1"})
        record = self.record(artifact_id)
        self.assertEqual((record["title"], record["filename"], record["group"]), ("T", "plan.md", None))
        self.assertEqual(len(record["history"]), 1)
        touch = record["history"][0]
        self.assertEqual((touch["session_id"], touch["rev"], touch["changes"]), ("s1", 0, None))
        self.assertEqual(record["created_at"], touch["at"])
        self.assertAlmostEqual(touch["at"], time.time(), delta=30)
        self.assertEqual((self.revs(artifact_id) / "0.md").read_text(), "hello")
        self.assertEqual(self.current(artifact_id).read_text(), "hello")
        lines = self.log_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(
            (lines[0]["actor"], lines[0]["type"], lines[0]["msg"]), ("s1", "artifact-create", f"{artifact_id} plan.md")
        )
        self.assertEqual(sorted(lines[0]), ["actor", "msg", "ts", "type"])

        bare_id = self.create("artifact", "b", env={"TX_SESSION_ID": "s1"})
        self.assertEqual(self.record(bare_id)["filename"], "artifact")
        self.assertTrue((self.revs(bare_id) / "0").is_file())
        self.assertTrue(self.current(bare_id, "").is_file())

    def test_t_art_11_modify_with_a_file(self):
        artifact_id = self.create("plan.md", "hello", env={"TX_SESSION_ID": "s1"})
        result = self.modify(artifact_id, "v2.md", "hello2", "--changes", "more", env={"TX_SESSION_ID": "s2"})
        self.assertEqual(result.out, f"Modified artifact {artifact_id} → rev 1\n")
        self.assertEqual((self.revs(artifact_id) / "1.md").read_text(), "hello2")
        self.assertEqual(self.current(artifact_id).read_text(), "hello2")
        touch = self.record(artifact_id)["history"][1]
        self.assertEqual((touch["session_id"], touch["rev"], touch["changes"]), ("s2", 1, "more"))
        self.assertAlmostEqual(touch["at"], time.time(), delta=30)
        lines = self.log_lines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(
            (lines[1]["actor"], lines[1]["type"], lines[1]["msg"]), ("s2", "artifact-modify", f"{artifact_id} rev1")
        )

        record_before, log_before = self.record_bytes(artifact_id), self.home.log_path.read_bytes()
        again = self.modify(artifact_id, "v2.md", "hello2", env={"TX_SESSION_ID": "s2"})
        self.assertEqual(again.out, "No change — the supplied file is identical to rev 1; nothing snapshotted.\n")
        self.assertFalse((self.revs(artifact_id) / "2.md").exists())
        self.assertEqual(self.record_bytes(artifact_id), record_before)
        self.assertEqual(self.home.log_path.read_bytes(), log_before)

    def test_t_art_12_modify_without_a_file_snapshots_current(self):
        artifact_id = self.create("plan.md", "hello", env={"TX_SESSION_ID": "s1"})
        self.modify(artifact_id, "v2.md", "hello2", env={"TX_SESSION_ID": "s2"})
        self.current(artifact_id).write_text("edited")
        before = len(self.log_lines())
        result = self.tx(["artifact", "modify", artifact_id])
        self.assertEqual((result.code, result.out), (0, f"Modified artifact {artifact_id} → rev 2\n"))
        self.assertEqual((self.revs(artifact_id) / "2.md").read_text(), "edited")
        touch = self.record(artifact_id)["history"][2]
        self.assertEqual((touch["session_id"], touch["rev"], touch["changes"]), ("user", 2, None))
        lines = self.log_lines()
        self.assertEqual(len(lines), before + 1)
        self.assertEqual(
            (lines[-1]["actor"], lines[-1]["type"], lines[-1]["msg"]), ("user", "artifact-modify", f"{artifact_id} rev2")
        )
        self.assertNotIn("artifact-read", [line["type"] for line in lines])

        record_before, log_before = self.record_bytes(artifact_id), self.home.log_path.read_bytes()
        again = self.tx(["artifact", "modify", artifact_id])
        self.assertEqual((again.code, again.out), (0, "No change — the working copy is identical to rev 2; nothing to snapshot.\n"))
        self.assertFalse((self.revs(artifact_id) / "3.md").exists())
        self.assertEqual(self.record_bytes(artifact_id), record_before)
        self.assertEqual(self.home.log_path.read_bytes(), log_before)

    def test_t_art_13_lost_modify_race_conflict(self):
        artifact_id = self.create("p.md", "p0")
        self.modify(artifact_id, "p1.md", "p1")
        (self.revs(artifact_id) / "2.md").write_text("x")
        result = self.tx(["artifact", "modify", artifact_id, str(self.file("y.md", "y"))])
        self.assertEqual((result.code, result.out, result.err), (1, "", CONFLICT.format(id=artifact_id, rev=2)))
        self.assertEqual((self.revs(artifact_id) / "2.md").read_text(), "x")
        self.assertEqual(self.current(artifact_id).read_text(), "p1")
        self.assertEqual(len(self.record(artifact_id)["history"]), 2)
        self.assertNotIn(f"{artifact_id} rev2", [line["msg"] for line in self.log_lines()])

        doctor = self.tx(["artifact", "doctor"])
        self.assertIn(f"  {artifact_id}: {ORPHAN.format(rev=2)}\n", doctor.out)
        repair = self.tx(["artifact", "doctor", "--repair"])
        self.assertEqual(repair.code, 0, repair.out)
        retry = self.tx(["artifact", "modify", artifact_id, str(self.root / "y.md")])
        self.assertEqual((retry.code, retry.out), (0, f"Modified artifact {artifact_id} → rev 2\n"))

    # ----- T-ART-15 / 26: diff ---------------------------------------------------------------

    def diff_artifact(self) -> str:
        artifact_id = self.create("r0.md", "a\nb\n")
        self.modify(artifact_id, "r1.md", "a\nc\n")
        self.modify(artifact_id, "r2.md", "a\nc\nd\n")
        return artifact_id

    def test_t_art_15_diff_default_and_explicit(self):
        artifact_id = self.diff_artifact()
        before = len(self.log_lines())
        default = self.tx(["artifact", "diff", artifact_id])
        self.assertEqual((default.code, default.err), (0, ""))
        self.assertEqual(default.out, "--- rev1\n+++ rev2\n@@ -1,2 +1,3 @@\n a\n c\n+d\n")
        explicit = self.tx(["artifact", "diff", artifact_id, "0", "2"])
        self.assertEqual((explicit.code, explicit.out), (0, "--- rev0\n+++ rev2\n@@ -1,2 +1,3 @@\n a\n-b\n+c\n+d\n"))
        lines = self.log_lines()[before:]
        self.assertEqual(
            [(line["actor"], line["type"], line["msg"]) for line in lines],
            [("", "artifact-diff", f"{artifact_id} rev1..rev2"), ("", "artifact-diff", f"{artifact_id} rev0..rev2")],
        )

        single = self.create("one.md", "one")
        result = self.tx(["artifact", "diff", single])
        self.assertEqual((result.code, result.err), (1, f"tx artifact: artifact {single} has only one revision — nothing to diff\n"))
        result = self.tx(["artifact", "diff", artifact_id, "0", "9"])
        self.assertEqual((result.code, result.err), (1, f"tx artifact: artifact {artifact_id} has no revision 9\n"))
        self.modify(artifact_id, "bin.md", b"\xff")
        result = self.tx(["artifact", "diff", artifact_id, "2", "3"])
        self.assertEqual((result.code, result.err), (1, f"tx artifact: artifact {artifact_id} revision 3 is not utf-8 text — cannot diff\n"))
        same = self.tx(["artifact", "diff", artifact_id, "2", "2"])
        self.assertEqual((same.code, same.out), (0, "(no differences)\n"))

        self.assert_golden("art/15", default.out)

    def test_t_art_26_diff_cli(self):
        artifact_id = self.diff_artifact()
        result = self.tx(["artifact", "diff", artifact_id])
        self.assertEqual((result.code, result.out), (0, "--- rev1\n+++ rev2\n@@ -1,2 +1,3 @@\n a\n c\n+d\n"))
        result = self.tx(["artifact", "diff", artifact_id, "0", "0"])
        self.assertEqual((result.code, result.out), (0, "(no differences)\n"))
        result = self.tx(["artifact", "diff", artifact_id, "1"])
        self.assertEqual((result.code, result.out), (2, ""))
        self.assertTrue(result.err.startswith("usage: tx artifact diff"), result.err)
        self.assertTrue(
            result.err.endswith("tx artifact diff: error: give both revs or neither (default: the last two)\n"), result.err
        )
        result = self.tx(["artifact", "diff", artifact_id, "x", "y"])
        self.assertEqual(result.code, 2)
        self.assertTrue(result.err.endswith("tx artifact diff: error: argument rev_a: invalid int value: 'x'\n"), result.err)

    # ----- T-ART-16: ls --session -------------------------------------------------------------

    def test_t_art_16_ls_session(self):
        at = time.time() - 120
        touches = lambda *authors: [
            {"session_id": author, "at": at, "rev": rev, "changes": None} for rev, author in enumerate(authors)
        ]
        self.records.artifact(id="art1", history=touches("s1"))
        self.records.artifact(id="art2", history=touches("s2", "s1"), revs={0: "a\n", 1: "b\n"})
        self.records.artifact(id="art3", history=touches("user"))
        self.records.llm(id="s1", name="alice")

        def ids(result) -> list[str]:
            self.assertEqual(result.code, 0, result.err)
            self.assertEqual(result.lines[0], "ARTIFACTS")
            return [line[2:10].strip() for line in result.lines[1:]]

        self.assertEqual(ids(self.tx(["artifact", "ls", "--session", "s1"])), ["art1", "art2"])
        self.assertEqual(ids(self.tx(["artifact", "ls", "--session", "user"])), ["art3"])
        none = self.tx(["artifact", "ls", "--session", "nope"])
        self.assertEqual((none.code, none.out), (0, "ARTIFACTS\n  (none)\n"))
        by_name = self.tx(["artifact", "ls", "--session", "alice"])
        self.assertEqual((by_name.code, by_name.out), (0, "ARTIFACTS\n  (none)\n"))
        self.assertFalse(self.home.log_path.exists())

    # ----- T-ART-17 / 18 / 28: doctor -----------------------------------------------------------

    def test_t_art_17_doctor_problem_lines(self):
        ids = self.broken_store()
        expected = self.broken_store_lines(ids)
        before = snapshot(self.home.artifacts_dir), self.home.log_path.read_bytes()
        doctor = self.tx(["artifact", "doctor"])
        self.assertEqual((doctor.code, doctor.err), (1, ""))
        self.assertEqual(doctor.lines, [*expected, "artifacts: 6 problem(s)"])
        self.assertEqual((snapshot(self.home.artifacts_dir), self.home.log_path.read_bytes()), before)

        with self.home.log_path.open("a") as handle:
            handle.write("not json\n" + json.dumps({"type": "artifact-modify", "msg": "x"}) + "\n")
        doctor = self.tx(["artifact", "doctor"])
        self.assertEqual(doctor.lines, [*expected, "artifacts: 6 problem(s)"])

        self.home.log_path.unlink()
        doctor = self.tx(["artifact", "doctor"])
        self.assertEqual(doctor.code, 1)
        touch_lines = [line for line in doctor.lines if "touch has no matching EventLog mutation line" in line]
        self.assertEqual(len(touch_lines), 7)
        self.assertEqual(doctor.lines[-1], "artifacts: 12 problem(s)")

    def test_t_art_18_doctor_repair(self):
        artifact_id = self.create("a.md", "a")
        (self.revs(artifact_id) / "5.md").write_text("debris")
        (self.revs(artifact_id) / "1.md").write_text("debris")
        blocked = self.tx(["artifact", "modify", artifact_id, str(self.file("f.md", "f"))])
        self.assertEqual((blocked.code, blocked.err), (1, CONFLICT.format(id=artifact_id, rev=1)))
        repair = self.tx(["artifact", "doctor", "--repair"])
        self.assertEqual(
            (repair.code, repair.out),
            (
                0,
                f"  {artifact_id}: removed orphan rev file 1\n"
                f"  {artifact_id}: removed orphan rev file 5\n"
                "artifacts: clean\n",
            ),
        )
        self.assertEqual(sorted(path.name for path in self.revs(artifact_id).iterdir()), ["0.md"])
        self.assertEqual((self.revs(artifact_id) / "0.md").read_text(), "a")
        after = self.tx(["artifact", "modify", artifact_id, str(self.root / "f.md")])
        self.assertEqual((after.code, after.out), (0, f"Modified artifact {artifact_id} → rev 1\n"))

    def test_t_art_28_doctor_cli(self):
        clean = self.tx(["artifact", "doctor"])
        self.assertEqual((clean.code, clean.out, clean.err), (0, "artifacts: clean\n", ""))
        ids = self.broken_store()
        expected = self.broken_store_lines(ids)
        doctor = self.tx(["artifact", "doctor"])
        self.assertEqual((doctor.code, doctor.lines), (1, [*expected, "artifacts: 6 problem(s)"]))
        repair = self.tx(["artifact", "doctor", "--repair"])
        self.assertEqual(repair.code, 1)
        self.assertEqual(repair.lines[0], f"  {ids['A']}: removed orphan rev file 5")
        remaining = [line for line in expected if not line.startswith(f"  {ids['A']}: ")]
        self.assertEqual(repair.lines[1:], [*remaining, "artifacts: 5 problem(s)"])

    # ----- T-ART-19..21: help, create, actor ---------------------------------------------------

    def test_t_art_19_help_and_unknown_sub(self):
        for argv in (["artifact"], ["artifact", "-h"], ["artifact", "help"]):
            result = self.tx(argv)
            self.assertEqual((result.code, result.out, result.err), (0, HELP, ""), argv)
        bogus = self.tx(["artifact", "bogus"])
        self.assertEqual((bogus.code, bogus.out, bogus.err), (2, HELP, "tx artifact: unknown subcommand 'bogus'\n"))

    def test_t_art_20_create(self):
        artifact_id = self.create(
            "x/plan.md", "hello", "--title", "Plan", "--group", "g1", env={"TX_SESSION_ID": "s1"}
        )
        record = self.record(artifact_id)
        self.assertEqual((record["filename"], record["title"], record["group"]), ("plan.md", "Plan", "g1"))
        self.assertEqual(record["history"][0]["session_id"], "s1")

        missing = self.tx(["artifact", "create", "/nope"])
        self.assertEqual((missing.code, missing.out), (2, ""))
        self.assertTrue(missing.err.startswith("usage: tx artifact create"), missing.err)
        self.assertTrue(missing.err.endswith("tx artifact create: error: no such file: /nope\n"), missing.err)
        empty = self.tx(["artifact", "create", str(self.root / "x" / "plan.md"), "--group", ""])
        self.assertEqual(empty.code, 2)
        self.assertTrue(
            empty.err.endswith("tx artifact create: error: argument --group: a group cannot be empty\n"), empty.err
        )

    def test_t_art_21_actor_resolution(self):
        self.tmux.new_session("s9", "sleep 1000", tx_id="s9")
        self.tmux.new_session("plain", "sleep 1000")
        source = str(self.file("plan.md", "p"))

        def creator(result) -> str:
            self.assertEqual(result.code, 0, result.err)
            artifact_id = re.fullmatch(rf"Created artifact ({UUID}) \(plan\.md\)\n", result.out).group(1)
            return self.record(artifact_id)["history"][0]["session_id"]

        self.assertEqual(creator(self.tx_inside("s9", ["artifact", "create", source], env={"TX_SESSION_ID": "s1"})), "s1")
        self.assertEqual(creator(self.tx_inside("s9", ["artifact", "create", source])), "s9")
        self.assertEqual(creator(self.tx_inside("plain", ["artifact", "create", source])), "user")
        self.assertEqual(creator(self.tx(["artifact", "create", source])), "user")

    # ----- T-ART-22 / 23: id resolution, modify outputs --------------------------------------

    def resolution_store(self) -> tuple[str, str, str]:
        ids = (
            "1234abcd-0000-4000-8000-000000000001",
            "1234ffff-0000-4000-8000-000000000002",
            "9999-0000-4000-8000-000000000003",
        )
        for artifact_id in ids:
            self.records.artifact(id=artifact_id)
        return ids

    def test_t_art_22_id_resolution(self):
        _, _, full = self.resolution_store()
        shown = self.tx(["artifact", "show", full])
        self.assertEqual(shown.code, 0, shown.err)
        self.assertEqual(shown.lines[0], f"Plan  ({full})")
        prefix = self.tx(["artifact", "show", "99"])
        self.assertEqual((prefix.code, prefix.out), (0, shown.out))
        ambiguous = self.tx(["artifact", "show", "1234"])
        self.assertEqual(
            (ambiguous.code, ambiguous.out, ambiguous.err),
            (1, "", "tx artifact: artifact id prefix '1234' is ambiguous (2 matches) — use more characters\n"),
        )
        unknown = self.tx(["artifact", "show", "zz"])
        self.assertEqual((unknown.code, unknown.out, unknown.err), (1, "", "tx artifact: artifact 'zz' not found\n"))

        modified = self.tx(["artifact", "modify", "99", str(self.file("v1.md", "v1\n"))])
        self.assertEqual((modified.code, modified.out), (0, f"Modified artifact {full} → rev 1\n"))
        group = self.tx(["artifact", "group", "99", "g"])
        self.assertEqual((group.code, group.out), (0, f"Grouped artifact {full} (group=g)\n"))
        diff = self.tx(["artifact", "diff", "99"])
        self.assertEqual((diff.code, diff.out), (0, "--- rev0\n+++ rev1\n@@ -1 +1 @@\n-body\n+v1\n"))
        opened = self.tx(["artifact", "open", "99"])
        self.assertEqual((opened.code, opened.out), (0, f"Opened artifact {full} in nvim view 'art-9999-000' ({self.records.artifact_dir(full)}/current.md)\n"))

    @expected_failure_on_python
    def test_t_art_22_fixed_empty_token_never_resolves(self):
        self.resolution_store()
        result = self.tx(["artifact", "show", ""])
        self.assertEqual((result.code, result.out), (1, ""))
        self.assertTrue(result.err.startswith("tx artifact: "), result.err)
        for artifact_id in ("1234abcd-0000-4000-8000-000000000001", "1234ffff-0000-4000-8000-000000000002"):
            self.records.artifact_record_path(artifact_id).unlink()
        single = self.tx(["artifact", "show", ""])
        self.assertEqual((single.code, single.out), (1, ""))
        self.assertTrue(single.err.startswith("tx artifact: "), single.err)
        self.assertNotIn("Traceback", single.err)

    def test_t_art_23_modify_outputs(self):
        artifact_id = self.create("plan.md", "hello")
        source = str(self.file("x/v2.md", "hello2"))
        prefix = artifact_id[:8]
        result = self.tx(["artifact", "modify", prefix, source, "--changes", "more"])
        self.assertEqual((result.code, result.out), (0, f"Modified artifact {artifact_id} → rev 1\n"))
        result = self.tx(["artifact", "modify", prefix, source])
        self.assertEqual((result.code, result.out), (0, "No change — the supplied file is identical to rev 1; nothing snapshotted.\n"))
        self.current(artifact_id).write_text("z")
        result = self.tx(["artifact", "modify", prefix, source])
        self.assertEqual(
            (result.code, result.out),
            (
                0,
                "No change — the supplied file is identical to rev 1; nothing snapshotted. NOTE: the working "
                f"copy still has unsnapshotted edits — run `tx artifact modify {artifact_id}` (no file) to "
                "snapshot them.\n",
            ),
        )
        result = self.tx(["artifact", "modify", artifact_id])
        self.assertEqual((result.code, result.out), (0, f"Modified artifact {artifact_id} → rev 2\n"))
        result = self.tx(["artifact", "modify", artifact_id])
        self.assertEqual((result.code, result.out), (0, "No change — the working copy is identical to rev 2; nothing to snapshot.\n"))
        missing = self.tx(["artifact", "modify", artifact_id, "/nope"])
        self.assertEqual((missing.code, missing.out), (2, ""))
        self.assertTrue(missing.err.endswith("tx artifact modify: error: no such file: /nope\n"), missing.err)

    # ----- T-ART-24 / 25: ls + show shapes (golden) --------------------------------------------

    P_ID = "aaaaaaaa-0000-4000-8000-000000000001"
    Q_ID = "bbbbbbbb-0000-4000-8000-000000000002"
    GONE = "gone-id-0000-4000-8000-000000000000"

    def write_p(self, now: float, *, group: str | None = None, dirty: bool = True) -> None:
        self.records.artifact(
            id=self.P_ID,
            title="Plan",
            created_at=now - 300,
            group=group,
            history=[
                {"session_id": "s1", "at": now - 300, "rev": 0, "changes": None},
                {"session_id": "user", "at": now - 90, "rev": 1, "changes": "tweak"},
            ],
            revs={0: "v0\n", 1: "v1\n"},
            current="edited\n" if dirty else "v1\n",
        )

    def test_t_art_24_ls_shape(self):
        now = time.time()
        self.write_p(now)
        self.records.artifact(
            id=self.Q_ID,
            title=None,
            filename="notes.md",
            created_at=now - 7200,
            history=[{"session_id": self.GONE, "at": now - 7200, "rev": 0, "changes": None}],
        )
        self.records.llm(id="s1", name="alice")
        result = self.tx(["artifact", "ls"])
        self.assertEqual((result.code, result.err), (0, ""))
        self.assertEqual(
            result.out,
            "ARTIFACTS\n"
            "  aaaaaaaa    2r     1m  Plan                         [alice] [user]\n"
            "  bbbbbbbb    1r     2h  notes.md                     [gone-id-]\n",
        )
        by_session = self.tx(["artifact", "ls", "--session", "s1"])
        self.assertEqual(by_session.out, "ARTIFACTS\n  aaaaaaaa    2r     1m  Plan                         [alice] [user]\n")
        by_name = self.tx(["artifact", "ls", "--session", "alice"])
        self.assertEqual(by_name.out, "ARTIFACTS\n  (none)\n")
        self.assert_golden("art/24", result.out)

    def test_t_art_24_ls_edges(self):
        empty = self.tx(["artifact", "ls"])
        self.assertEqual((empty.code, empty.out), (0, "ARTIFACTS\n  (none)\n"))
        title = "T" * 40
        self.records.artifact(id="cccccccc-0000-4000-8000-000000000003", title=title, created_at=time.time() - 30)
        result = self.tx(["artifact", "ls"])
        self.assertEqual(result.lines[1], f"  cccccccc    1r    30s  {'T' * 27}… [s1]")

    def test_t_art_25_show_shape(self):
        now = time.time()
        self.write_p(now)
        self.records.llm(id="s1", name="alice", tags=("feat-x",))
        result = self.tx(["artifact", "show", "aaaaaaaa"])
        self.assertEqual((result.code, result.err), (0, ""))
        expected = (
            "Plan  (aaaaaaaa-0000-4000-8000-000000000001)\n"
            "  filename:   plan.md\n"
            "  created:    5m ago\n"
            "  updated:    1m ago\n"
            "  group:      derived: feat-x\n"
            "  revisions:  2\n"
            "  working:    dirty — un-snapshotted edits (close with `tx artifact modify`)\n"
            "  history:\n"
            "    rev 0      5m ago  alice               \n"
            "    rev 1      1m ago  user                  tweak\n"
        )
        self.assertEqual(result.out, expected)

        self.write_p(now, group="g", dirty=False)
        result = self.tx(["artifact", "show", "aaaaaaaa"])
        self.assertIn("\n  group:      g\n", result.out)
        self.assertIn("\n  working:    clean\n", result.out)

        self.write_p(now)
        self.records.path("s1").unlink()
        result = self.tx(["artifact", "show", "aaaaaaaa"])
        self.assertIn("\n  group:      derived: ungrouped\n", result.out)
        self.assertIn("\n    rev 0      5m ago  s1                  \n", result.out)

        self.assert_golden("art/25", expected)

    @expected_failure_on_python
    def test_t_art_25_fixed_missing_current(self):
        self.write_p(time.time())
        self.current(self.P_ID).unlink()
        result = self.tx(["artifact", "show", "aaaaaaaa"])
        self.assertEqual((result.code, result.out), (1, ""))
        self.assertEqual(len(result.err.splitlines()), 1, result.err)
        self.assertTrue(result.err.startswith("tx artifact: "), result.err)
        self.assertIn("current.md", result.err)
        self.assertNotIn("Traceback", result.err)

    # ----- T-ART-27: open ---------------------------------------------------------------------

    def open_fixture(self, artifact_id: str) -> None:
        self.records.artifact(id=artifact_id)

    def test_t_art_27_open(self):
        artifact_id = "abcdef12-0000-4000-8000-000000000001"
        self.open_fixture(artifact_id)
        self.records.llm(id="s1", name="invoker", tags=("feat-x", "b"))
        self.tmux.new_session("s1", "sleep 1000", tx_id="s1")
        content_dir = self.records.artifact_dir(artifact_id)
        result = self.tx_inside("s1", ["artifact", "open", "abcdef12"], env={"TX_SESSION_ID": "s1"})
        self.assertEqual((result.code, result.err), (0, ""))
        self.assertEqual(
            result.out, f"Opened artifact {artifact_id} in nvim view 'art-abcdef12' ({content_dir}/current.md)\n"
        )
        view = json.loads(self.tx(["show", "art-abcdef12"]).out)
        self.assertEqual(
            (view["role"], view["name"], view["tags"], view["cwd"], view["artifact_id"]),
            ("nvim", "art-abcdef12", ["feat-x", "b"], str(content_dir), artifact_id),
        )
        self.assertNotIn("engine", view)
        self.assertIn("nvim", view["cmd"])
        self.assertIn(f"{content_dir}/current.md", view["cmd"])
        self.assertIn(view["id"], self.tmux.sessions())
        self.assertEqual(self.tmux.option(view["id"], "@tx_id"), view["id"])
        dump = self.fakes.wait_dump("nvim", view["id"])
        self.assertEqual(dump["argv"][-1], f"{content_dir}/current.md")
        lines = self.log_lines()
        self.assertEqual(
            [(line["type"], line["msg"]) for line in lines],
            [
                ("spawn", f"art-abcdef12 [nvim] {content_dir}"),
                ("bind-artifact", f"art-abcdef12 → {artifact_id}"),
                ("artifact-open", f"{artifact_id} → s1"),
            ],
        )
        self.assertEqual(lines[2]["actor"], "s1")

        clash = self.tx_inside("s1", ["artifact", "open", "abcdef12"], env={"TX_SESSION_ID": "s1"})
        self.assertEqual((clash.code, clash.err), (1, "tx artifact: session 'art-abcdef12' already exists\n"))

    def test_t_art_27_open_edges(self):
        tagged = "bbbbbbbb-0000-4000-8000-000000000002"
        untagged = "cccccccc-0000-4000-8000-000000000003"
        with_cwd = "dddddddd-0000-4000-8000-000000000004"
        for artifact_id in (tagged, untagged, with_cwd):
            self.open_fixture(artifact_id)
        self.records.llm(id="s1", name="invoker", tags=("feat-x", "b"))
        self.tmux.new_session("s1", "sleep 1000", tx_id="s1")

        result = self.tx_inside("s1", ["artifact", "open", "bbbbbbbb", "--tag", "t1,t2"], env={"TX_SESSION_ID": "s1"})
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(json.loads(self.tx(["show", "art-bbbbbbbb"]).out)["tags"], ["t1", "t2"])

        result = self.tx(["artifact", "open", "cccccccc"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(json.loads(self.tx(["show", "art-cccccccc"]).out)["tags"], ["artifact"])

        empty = self.tx(["artifact", "open", "dddddddd", "--tag", ""])
        self.assertEqual((empty.code, empty.out), (2, ""))
        self.assertTrue(empty.err.endswith("tx artifact open: error: --tag requires at least one value\n"), empty.err)

        cwd = self.root / "x"
        cwd.mkdir()
        result = self.tx(["artifact", "open", "dddddddd", "--cwd", str(cwd)])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(json.loads(self.tx(["show", "art-dddddddd"]).out)["cwd"], str(cwd))
