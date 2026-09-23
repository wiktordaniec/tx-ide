"""STORE — `lib/tx/store.py` through the CLI: atomic save, tolerant scan, delete, default dir.

Spec cases T-STORE-01..10 (T-STORE-06 dropped). Everything is observed through `tx spawn` /
`tx show` / `tx rename` / `tx rm` / `tx ls` / `tx history` / `tx artifact` and the files under
`$TX_IDE_HOME/sessions/`.
"""

from __future__ import annotations

import json
import time

from txkit import TxCase, expected_failure_on_python

SKIP_PREFIX = "tx: skipping unreadable record "
UNSUPPORTED = (
    "record schema_version={version} is unsupported (expected 6); tx-ide does not back-migrate "
    "older records on load (§9) — run `tx migrate` to upgrade older records"
)


class TestStore(TxCase):
    # ----- T-STORE-01 ---------------------------------------------------------------------

    def test_t_store_01_save_writes_record_atomically(self):
        self.assertEqual(sorted(self.home.sessions_dir.iterdir()), [])
        result = self.tx(["spawn", "abc", "--tag", "t", "--cwd", str(self.root), "--cmd", "bash"])
        self.assertEqual(result.code, 0, result.err)
        entries = sorted(path.name for path in self.home.sessions_dir.iterdir())
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0].endswith(".json"))
        self.assertFalse(entries[0].startswith("."))
        record_id = entries[0][: -len(".json")]
        content = self.records.path(record_id).read_text()
        self.assertTrue(content.startswith('{\n  "schema_version": 6,'))
        self.assertEqual(content[-1], "}")
        self.assertEqual(json.loads(content)["id"], record_id)
        self.assertEqual(content, json.dumps(json.loads(content), indent=2))
        shown = self.tx(["show", record_id])
        self.assertEqual(shown.code, 0, shown.err)
        self.assertEqual(shown.out, content + "\n")

    # ----- T-STORE-02 ---------------------------------------------------------------------

    def test_t_store_02_save_overwrites_in_place(self):
        self.records.llm(id="abc", name="a", state="exited", ended_at=1000.0)
        result = self.tx(["rename", "abc", "b"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, "Renamed to 'b'\n")
        self.assertEqual(self.records.load("abc")["name"], "b")
        self.assertEqual([path.name for path in self.home.sessions_dir.glob("*.json")], ["abc.json"])

    # ----- T-STORE-03 ---------------------------------------------------------------------

    def _store_03_fixture(self) -> None:
        self.records.llm(id="abc", name="abc", state="exited", ended_at=1000.0)
        self.records.write({"schema_version": 4, "id": "v4"})
        (self.home.sessions_dir / "bad.json").write_text("{not json")

    def test_t_store_03_parity_load(self):
        self._store_03_fixture()
        shown = self.tx(["show", "abc"])
        self.assertEqual(shown.code, 0, shown.err)
        self.assertEqual(json.loads(shown.out)["id"], "abc")
        self.assertEqual(shown.out, self.records.path("abc").read_text() + "\n")

        missing = self.tx(["show", "zzz"])
        self.assertEqual(missing.code, 1)
        self.assertEqual(missing.out, "")
        # The name fallback scans the store, so the unreadable-file warnings precede the verdict.
        self.assertEqual(missing.err.splitlines()[-1], "tx show: no record for 'zzz'")

        old = self.tx(["show", "v4"])
        self.assertEqual(old.code, 1)
        self.assertEqual(old.out, "")
        self.assertIn(UNSUPPORTED.format(version=4), old.err)

        bad = self.tx(["show", "bad"])
        self.assertEqual(bad.code, 1)
        self.assertEqual(bad.out, "")

    @expected_failure_on_python
    def test_t_store_03_fixed_load_errors_are_messages(self):
        self._store_03_fixture()
        old = self.tx(["show", "v4"])
        self.assertEqual(old.code, 1)
        self.assertEqual(old.out, "")
        self.assertIn(UNSUPPORTED.format(version=4), old.err)
        self.assertNotIn("Traceback", old.err)

        bad = self.tx(["show", "bad"])
        self.assertEqual(bad.code, 1)
        self.assertEqual(bad.out, "")
        self.assertIn("bad.json", bad.err)
        self.assertNotIn("Traceback", bad.err)

    # ----- T-STORE-04 ---------------------------------------------------------------------

    def _store_04_fixture(self) -> list[str]:
        self.records.llm(id="b", name="b", state="exited", ended_at=1000.0)
        self.records.llm(id="a", name="a", state="exited", ended_at=1000.0)
        self.records.write({"schema_version": 3, "id": "c", "name": "c"})
        (self.home.sessions_dir / "d.json").write_text("{not json")
        missing_cmd = json.loads(self.records.path("a").read_text())
        missing_cmd["id"] = "e"
        del missing_cmd["cmd"]
        self.records.write(missing_cmd)
        bogus_state = json.loads(self.records.path("a").read_text())
        bogus_state["id"] = "f"
        bogus_state["state"] = "bogus"
        self.records.write(bogus_state)
        (self.home.sessions_dir / ".x.tmp").write_text("garbage")
        (self.home.sessions_dir / "notes.txt").write_text("not a record")
        return [
            f"{SKIP_PREFIX}c.json: {UNSUPPORTED.format(version=3)}",
            f"{SKIP_PREFIX}d.json: ",
            f"{SKIP_PREFIX}e.json: 'cmd'",
            f"{SKIP_PREFIX}f.json: 'bogus' is not a valid State",
        ]

    def _assert_skip_lines(self, lines: list[str], expected: list[str]) -> None:
        self.assertEqual(len(lines), len(expected), lines)
        for line, want in zip(lines, expected):
            if want.endswith(": "):
                self.assertTrue(line.startswith(want), line)
            else:
                self.assertEqual(line, want)

    def test_t_store_04_parity_scan_sorted_and_tolerant(self):
        expected = self._store_04_fixture()
        result = self.tx(["show", "nosuch"])
        self.assertEqual(result.code, 1)
        self.assertEqual(result.out, "")
        lines = result.err.splitlines()
        self.assertEqual(lines[-1], "tx show: no record for 'nosuch'")
        self._assert_skip_lines(lines[:-1], expected)
        self.assertNotIn(".x.tmp", result.err)
        self.assertNotIn("notes.txt", result.err)

        history = self.tx(["history"])
        self.assertEqual(history.code, 0)
        rows = history.out.splitlines()
        self.assertEqual(rows[0], "HISTORY")
        self.assertEqual(sorted(row.split()[0] for row in rows[1:]), ["a", "b"])

        listing = self.tx(["ls"])
        self.assertEqual(listing.code, 0)
        self.assertEqual(listing.out, "PROCESSES\n")

    @expected_failure_on_python
    def test_t_store_04_fixed_ls_history_warn_once(self):
        expected = self._store_04_fixture()
        for verb in (["ls"], ["history"]):
            result = self.tx(verb)
            self.assertEqual(result.code, 0)
            self._assert_skip_lines(result.err.splitlines(), expected)

    # ----- T-STORE-05 ---------------------------------------------------------------------

    def test_t_store_05_empty_store(self):
        listing = self.tx(["ls"])
        self.assertEqual(listing.code, 0)
        self.assertEqual(listing.out, "PROCESSES\n")
        self.assertEqual(listing.err, "")
        history = self.tx(["history"])
        self.assertEqual(history.code, 0)
        self.assertEqual(history.out, "HISTORY\n  (no exited or archived sessions)\n")

    # ----- T-STORE-07 ---------------------------------------------------------------------

    def test_t_store_07_names_for_author_chips(self):
        alpha = "aaaaaaaa-0000-4000-8000-000000000001"
        stale = "cccccccc-0000-4000-8000-000000000003"
        absent = "dddddddd-0000-4000-8000-000000000004"
        self.records.llm(id=alpha, name="alpha", state="exited", ended_at=1000.0)
        self.records.write({"schema_version": 3, "id": stale, "name": "gamma"})
        plan = self.root / "plan.md"
        plan.write_text("# plan\n")
        for session_id, title in ((alpha, "P2"), (stale, "P3"), (absent, "P4")):
            created = self.tx(
                ["artifact", "create", str(plan), "--title", title],
                env={"TX_SESSION_ID": session_id},
            )
            self.assertEqual(created.code, 0, created.err)
        listing = self.tx(["artifact", "ls"])
        self.assertEqual(listing.code, 0)
        self.assertEqual(listing.err, "")
        rows = {row.split()[3]: row for row in listing.out.splitlines()[1:]}
        self.assertEqual(sorted(rows), ["P2", "P3", "P4"])
        self.assertTrue(rows["P2"].endswith(" [alpha]"), rows["P2"])
        self.assertTrue(rows["P3"].endswith(" [cccccccc]"), rows["P3"])
        self.assertTrue(rows["P4"].endswith(" [dddddddd]"), rows["P4"])

    # ----- T-STORE-08 ---------------------------------------------------------------------

    def test_t_store_08_query_predicate_history(self):
        ended = time.time() - 100
        live = self.records.llm(id="i", name="i", state="idle")
        self.tmux.new_session(live, "sleep 1000", tx_id=live)
        self.records.llm(id="e", name="e", state="exited", ended_at=ended)
        self.records.llm(id="a", name="a", state="archived", ended_at=ended)
        history = self.tx(["history"])
        self.assertEqual(history.code, 0, history.err)
        rows = history.out.splitlines()
        self.assertEqual(rows[0], "HISTORY")
        self.assertEqual(len(rows), 3)
        self.assertEqual(sorted(row.split()[0] for row in rows[1:]), ["a", "e"])
        self.assertEqual(sorted(row.split()[1] for row in rows[1:]), ["archived", "exited"])
        self.assertNotIn("i", [row.split()[0] for row in rows[1:]])
        self.assertEqual(self.records.load("i")["state"], "idle")

    # ----- T-STORE-09 ---------------------------------------------------------------------

    def test_t_store_09_delete(self):
        self.records.llm(id="abc", name="abc", state="exited", ended_at=1000.0)
        first = self.tx(["rm", "abc"])
        self.assertEqual(first.code, 0, first.err)
        self.assertEqual(first.out, "Removed record for 'abc'\n")
        self.assertFalse(self.records.path("abc").exists())
        log = self.log_lines()
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["type"], "rm")
        second = self.tx(["rm", "abc"])
        self.assertEqual(second.code, 1)
        self.assertEqual(second.out, "")
        self.assertEqual(second.err, "tx rm: no record for 'abc'\n")
        self.assertEqual(self.log_lines(), log)

    # ----- T-STORE-10 ---------------------------------------------------------------------

    def test_t_store_10_default_directory(self):
        other_home = self.root / "h"
        env = {"TX_IDE_HOME": str(other_home)}
        init = self.tx(["_init-home"], env=env)
        self.assertEqual(init.code, 0, init.err)
        self.assertTrue((other_home / "sessions").is_dir())
        spawned = self.tx(["spawn", "s", "--tag", "t", "--cwd", str(self.root), "--cmd", "bash"], env=env)
        self.assertEqual(spawned.code, 0, spawned.err)
        record = json.loads(self.tx(["show", "s"], env=env).out)
        self.assertTrue((other_home / "sessions" / f"{record['id']}.json").is_file())
        self.assertEqual(list(self.home.sessions_dir.glob("*.json")), [])
