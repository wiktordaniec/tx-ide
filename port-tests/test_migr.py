"""MIGR — `tx migrate`: sessions v3/v4/v5 → v6 (view eviction to `@tx_view`), artifacts v1 → v2.

Spec section 01, cases T-MIGR-01..10 (T-MIGR-11 DROPPED). Every fixture is a hand-written older
record under `$TX_IDE_HOME/sessions/` or `artifacts/`; the trigger is always `tx migrate` (no
flags), which reaches the private tmux server through the PATH wrapper to stamp live views.
"""

from __future__ import annotations

import json
from pathlib import Path

from txkit import TxCase

SESSIONS_SUMMARY = "migrated {} record(s) to v6; retired {} view record(s); left {} untouched."
ARTIFACTS_SUMMARY = "migrated {} artifact record(s) to v2; left {} untouched."

# Canonical v6 key order (`Session.to_dict` + the subtype extensions).
LLM_V6_KEYS = (
    "schema_version", "id", "name", "role", "state", "cwd", "cmd", "tags", "group", "env",
    "parent", "pid", "attached_to", "created_at", "ended_at", "engine", "last_activity", "chats",
    "turn_started_at",
)
OTHER_V6_KEYS = (
    "schema_version", "id", "name", "role", "state", "cwd", "cmd", "tags", "group", "env",
    "parent", "pid", "attached_to", "created_at", "ended_at", "artifact_id",
)


def v3_llm(session_id: str, **overrides) -> dict:
    record = {
        "schema_version": 3, "kind": "process", "id": session_id, "name": "w", "role": "llm",
        "state": "idle", "cwd": "/r", "cmd": "claude", "tags": ["t"], "env": {}, "parent": None,
        "pid": 1, "attached_to": [], "created_at": 1.0, "ended_at": None, "engine": "claude",
        "chats": [], "last_activity": 2.0,
    }
    record.update(overrides)
    return record


def v3_nvim(session_id: str, **overrides) -> dict:
    record = {
        "schema_version": 3, "kind": "process", "id": session_id, "name": "ed", "role": "nvim",
        "state": "exited", "cwd": "/repo", "cmd": "nvim", "tags": [], "env": {}, "parent": None,
        "pid": None,
        "attached_to": [
            {"host": "Views", "window_index": "1", "window_name": "work", "pane_id": "%41",
             "pane_index": "1"}
        ],
        "created_at": 950.0, "ended_at": 960.0, "engine": None, "chats": [], "last_activity": None,
    }
    record.update(overrides)
    return record


def v3_view(session_id: str, name: str) -> dict:
    return {
        "schema_version": 3, "kind": "view", "id": session_id, "name": name, "role": "shell",
        "state": "alive", "cwd": "/r", "cmd": "zsh", "tags": [], "env": {}, "parent": None,
        "pid": 7, "attached_to": [], "created_at": 1.0, "ended_at": None,
    }


def v4_nvim(session_id: str) -> dict:
    return {
        "schema_version": 4, "id": session_id, "name": "ed4", "role": "nvim", "state": "exited",
        "cwd": "/repo", "cmd": "nvim", "tags": [], "env": {}, "parent": None, "pid": None,
        "attached_to": [], "created_at": 10.0, "ended_at": 11.0,
    }


def v5_llm(session_id: str) -> dict:
    return {
        "schema_version": 5, "id": session_id, "name": "w5", "role": "llm", "state": "exited",
        "cwd": "/r", "cmd": "claude", "tags": ["t"], "env": {}, "parent": None, "pid": 2,
        "attached_to": [], "created_at": 20.0, "ended_at": 21.0, "engine": "claude",
        "last_activity": 20.5, "chats": [], "turn_started_at": None,
    }


def v5_nvim_with_artifact(session_id: str) -> dict:
    return {
        "schema_version": 5, "id": session_id, "name": "art-view", "role": "nvim",
        "state": "exited", "cwd": "/repo", "cmd": "nvim", "tags": [], "env": {}, "parent": None,
        "pid": None, "attached_to": [], "created_at": 30.0, "ended_at": 31.0,
        "artifact_id": "art-1",
    }


def v1_artifact(artifact_id: str, **overrides) -> dict:
    record = {
        "artifact_schema_version": 1, "id": artifact_id, "title": "T", "filename": "t.md",
        "created_at": 1.0,
        "history": [{"session_id": "s", "at": 1.0, "rev": 0, "changes": None}],
    }
    record.update(overrides)
    return record


def snapshot(directory: Path) -> dict[str, tuple[bytes, int]]:
    return {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(directory.iterdir())
        if path.is_file()
    }


class TestMigr(TxCase):
    def write_session(self, name: str, record, *, raw: str | None = None) -> Path:
        path = self.home.sessions_dir / name
        path.write_text(raw if raw is not None else json.dumps(record))
        return path

    def write_artifact(self, name: str, record, *, raw: str | None = None) -> Path:
        path = self.home.artifacts_dir / name
        path.write_text(raw if raw is not None else json.dumps(record))
        return path

    def migrate(self):
        result = self.tx(["migrate"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.err, "")
        return result

    def new_view_session(self, name: str) -> None:
        self.tmux.run("new-session", "-d", "-s", name, "sleep 1000", check=True)

    # ----- T-MIGR-01 ---------------------------------------------------------------------

    def test_t_migr_01_v3_llm_process_record(self):
        path = self.write_session("s1.json", v3_llm("s1"))
        result = self.migrate()
        self.assertIn("  migrated s1.json → v6\n", result.out)
        self.assertIn(SESSIONS_SUMMARY.format(1, 0, 0) + "\n", result.out)
        expected = {
            "schema_version": 6, "id": "s1", "name": "w", "role": "llm", "state": "idle",
            "cwd": "/r", "cmd": "claude", "tags": ["t"], "group": None, "env": {}, "parent": None,
            "pid": 1, "attached_to": [], "created_at": 1.0, "ended_at": None, "engine": "claude",
            "last_activity": 2.0, "chats": [], "turn_started_at": None,
        }
        self.assertEqual(tuple(expected), LLM_V6_KEYS)
        self.assertEqual(path.read_text(), json.dumps(expected, indent=2))
        saved = json.loads(path.read_text())
        self.assertEqual(tuple(saved), LLM_V6_KEYS)
        self.assertNotIn("kind", saved)
        show = self.tx(["show", "s1"])
        self.assertEqual(show.code, 0, show.err)
        self.assertEqual(json.loads(show.out)["schema_version"], 6)

    def test_t_migr_01_turn_started_at_preserved(self):
        path = self.write_session("s1.json", v3_llm("s1", turn_started_at=7.0))
        result = self.migrate()
        self.assertIn("  migrated s1.json → v6\n", result.out)
        self.assertEqual(json.loads(path.read_text())["turn_started_at"], 7.0)

    # ----- T-MIGR-02 ---------------------------------------------------------------------

    def test_t_migr_02_v3_non_llm_process_record(self):
        path = self.write_session("s2.json", v3_nvim("s2"))
        result = self.migrate()
        self.assertIn("  migrated s2.json → v6\n", result.out)
        expected = {
            "schema_version": 6, "id": "s2", "name": "ed", "role": "nvim", "state": "exited",
            "cwd": "/repo", "cmd": "nvim", "tags": [], "group": None, "env": {}, "parent": None,
            "pid": None,
            "attached_to": [
                {"host": "Views", "window_index": "1", "window_name": "work", "pane_id": "%41",
                 "pane_index": "1"}
            ],
            "created_at": 950.0, "ended_at": 960.0, "artifact_id": None,
        }
        self.assertEqual(tuple(expected), OTHER_V6_KEYS)
        self.assertEqual(path.read_text(), json.dumps(expected, indent=2))
        saved = json.loads(path.read_text())
        for key in ("engine", "chats", "last_activity", "kind", "turn_started_at"):
            self.assertNotIn(key, saved)
        self.assertEqual(list(saved)[-1], "artifact_id")

    # ----- T-MIGR-03 / 04 ----------------------------------------------------------------

    def test_t_migr_03_live_view_stamped_and_deleted(self):
        path = self.write_session("v1.json", v3_view("v1", "Views"))
        self.new_view_session("Views")
        self.assertIsNone(self.tmux.option("Views", "@tx_view"))
        result = self.migrate()
        self.assertIn("  view     Views → stamped @tx_view, record removed\n", result.out)
        self.assertIn(SESSIONS_SUMMARY.format(0, 1, 0) + "\n", result.out)
        self.assertNotIn("migrated v1.json", result.out)
        self.assertEqual(self.tmux.option("Views", "@tx_view"), "1")
        self.assertFalse(path.exists())
        self.assertEqual(sorted(self.home.sessions_dir.iterdir()), [])

    def test_t_migr_03_id_named_session_not_stamped(self):
        path = self.write_session("v1.json", v3_view("v1", "Views"))
        self.new_view_session("v1")
        result = self.migrate()
        self.assertIn("  view     Views → stamped @tx_view, record removed\n", result.out)
        self.assertIsNone(self.tmux.option("v1", "@tx_view"))
        self.assertFalse(path.exists())
        self.assertEqual(self.tmux.sessions(), ["v1"])

    def test_t_migr_04_dead_view_deleted_only(self):
        path = self.write_session("v2.json", v3_view("v2", "Gone"))
        # The bystander's name is prefixed by the dead view's, so a stamp through a bare `-t Gone`
        # (tmux prefix-matches an unanchored target, Q27) would land on it and be caught below.
        self.new_view_session("Gone-2")
        before = self.tmux.sessions()
        result = self.migrate()
        self.assertFalse(path.exists())
        self.assertEqual(self.tmux.sessions(), before)
        self.assertFalse(self.tmux.has_session("Gone"))
        self.assertIsNone(self.tmux.option("Gone-2", "@tx_view"))
        self.assertIn("  view     Gone → stamped @tx_view, record removed\n", result.out)
        self.assertIn(SESSIONS_SUMMARY.format(0, 1, 0) + "\n", result.out)
        self.assertNotIn("  migrated", result.out)

    # ----- T-MIGR-05 ---------------------------------------------------------------------

    def test_t_migr_05_v4_and_v5(self):
        a = self.write_session("a.json", v4_nvim("a"))
        b = self.write_session("b.json", v5_llm("b"))
        c = self.write_session("c.json", v5_nvim_with_artifact("c"))
        result = self.migrate()
        self.assertIn("  migrated a.json → v6\n  migrated b.json → v6\n  migrated c.json → v6\n", result.out)
        saved_a = json.loads(a.read_text())
        self.assertEqual(saved_a["schema_version"], 6)
        self.assertIsNone(saved_a["artifact_id"])
        self.assertIsNone(saved_a["group"])
        self.assertEqual(tuple(saved_a), OTHER_V6_KEYS)
        saved_b = json.loads(b.read_text())
        self.assertEqual(saved_b["schema_version"], 6)
        self.assertIsNone(saved_b["group"])
        self.assertEqual(tuple(saved_b), LLM_V6_KEYS)
        saved_c = json.loads(c.read_text())
        self.assertEqual(saved_c["schema_version"], 6)
        self.assertEqual(saved_c["artifact_id"], "art-1")

    # ----- T-MIGR-06 ---------------------------------------------------------------------

    def test_t_migr_06_skipped_reasons(self):
        cur = {
            "schema_version": 6, "id": "cur", "name": "c", "role": "shell", "state": "exited",
            "cwd": "/r", "cmd": "bash", "tags": [], "group": None, "env": {}, "parent": None,
            "pid": None, "attached_to": [], "created_at": 1.0, "ended_at": 2.0, "artifact_id": None,
        }
        self.write_session("cur.json", cur)
        self.write_session("old.json", {"schema_version": 2})
        self.write_session("nov.json", {"id": "x"})
        self.write_session("bad.json", None, raw="{not json")
        nokey = v3_llm("nokey")
        del nokey["cmd"]
        self.write_session("nokey.json", nokey)
        self.write_session("list.json", [1, 2])
        self.write_session("badstate.json", {**v4_nvim("badstate"), "state": "bogus"})
        before = snapshot(self.home.sessions_dir)
        result = self.migrate()
        expected = (
            "  skipped  bad.json (JSONDecodeError: Expecting property name enclosed in double quotes: line 1 column 2 (char 1))\n"
            "  skipped  badstate.json (ValueError: 'bogus' is not a valid State)\n"
            "  skipped  cur.json (already v6)\n"
            "  skipped  list.json (AttributeError: 'list' object has no attribute 'get')\n"
            "  skipped  nokey.json (KeyError: 'cmd')\n"
            "  skipped  nov.json (not an upgradable record (schema_version=None))\n"
            "  skipped  old.json (not an upgradable record (schema_version=2))\n"
            "migrated 0 record(s) to v6; retired 0 view record(s); left 7 untouched.\n"
            "migrated 0 artifact record(s) to v2; left 0 untouched.\n"
        )
        self.assertEqual(result.raw_out, expected)
        self.assertEqual(snapshot(self.home.sessions_dir), before)
        self.assert_golden_raw("migr/06", result.raw_out)

    # ----- T-MIGR-07 ---------------------------------------------------------------------

    def test_t_migr_07_idempotence(self):
        self.write_session("s1.json", v3_llm("s1"))
        self.write_session("s2.json", v3_nvim("s2"))
        self.write_session("v1.json", v3_view("v1", "Views"))
        self.write_session("v2.json", v3_view("v2", "Gone"))
        self.write_session("a.json", v4_nvim("a"))
        self.write_session("b.json", v5_llm("b"))
        self.new_view_session("Views")
        first = self.migrate()
        self.assertIn(SESSIONS_SUMMARY.format(4, 2, 0) + "\n", first.out)
        self.assertEqual(self.tmux.option("Views", "@tx_view"), "1")
        remaining = snapshot(self.home.sessions_dir)
        self.assertEqual(sorted(remaining), ["a.json", "b.json", "s1.json", "s2.json"])
        sessions_before = self.tmux.sessions()
        second = self.migrate()
        self.assertNotIn("  migrated", second.out)
        self.assertNotIn("  view", second.out)
        expected = "".join(f"  skipped  {name} (already v6)\n" for name in sorted(remaining))
        self.assertEqual(
            second.out,
            expected + SESSIONS_SUMMARY.format(0, 0, 4) + "\n" + ARTIFACTS_SUMMARY.format(0, 0) + "\n",
        )
        self.assertEqual(snapshot(self.home.sessions_dir), remaining)
        self.assertEqual(self.tmux.option("Views", "@tx_view"), "1")
        self.assertEqual(self.tmux.sessions(), sessions_before)

    # ----- T-MIGR-08 ---------------------------------------------------------------------

    def test_t_migr_08_writes_via_store_save(self):
        path = self.write_session("s1.json", v3_llm("s1"))
        self.migrate()
        self.assertEqual([entry.name for entry in self.home.sessions_dir.iterdir()], ["s1.json"])
        text = path.read_text()
        self.assertTrue(text.startswith('{\n  "schema_version": 6,\n'))
        self.assertIn('\n  "tags": [\n    "t"\n  ],\n', text)
        self.assertTrue(text.endswith("}"))
        self.assertEqual(text, json.dumps(json.loads(text), indent=2))

    # ----- T-MIGR-09 ---------------------------------------------------------------------

    def test_t_migr_09_artifacts_v1_to_v2(self):
        path = self.write_artifact("art1.json", v1_artifact("art1"))
        result = self.migrate()
        self.assertIn("  migrated art1.json → artifact v2\n", result.out)
        self.assertIn(ARTIFACTS_SUMMARY.format(1, 0) + "\n", result.out)
        expected = {
            "artifact_schema_version": 2, "id": "art1", "title": "T", "filename": "t.md",
            "created_at": 1.0, "group": None,
            "history": [{"session_id": "s", "at": 1.0, "rev": 0, "changes": None}],
        }
        self.assertEqual(path.read_text(), json.dumps(expected, indent=2))
        self.assertNotIn("updated_at", json.loads(path.read_text()))

    def test_t_migr_09_group_preserved(self):
        path = self.write_artifact("art1.json", v1_artifact("art1", group="g"))
        result = self.migrate()
        self.assertIn("  migrated art1.json → artifact v2\n", result.out)
        self.assertEqual(json.loads(path.read_text())["group"], "g")

    def test_t_migr_09_rev1_single_touch_skipped(self):
        record = v1_artifact("art1", history=[{"session_id": "s", "at": 1.0, "rev": 1, "changes": None}])
        path = self.write_artifact("art1.json", record)
        before = path.read_bytes()
        result = self.migrate()
        self.assertIn(
            "  skipped  art1.json (UnsupportedArtifactError: artifact 'art1' has non-contiguous rev "
            "numbers [1] (expected 0..0, one per touch))\n",
            result.out,
        )
        self.assertIn(ARTIFACTS_SUMMARY.format(0, 1) + "\n", result.out)
        self.assertEqual(path.read_bytes(), before)

    # ----- T-MIGR-10 ---------------------------------------------------------------------

    def test_t_migr_10_artifacts_skipped_and_idempotent(self):
        cur = {
            "artifact_schema_version": 2, "id": "cur", "title": "C", "filename": "c.md",
            "created_at": 1.0, "group": None,
            "history": [{"session_id": "s", "at": 1.0, "rev": 0, "changes": None}],
        }
        self.write_artifact("cur.json", cur)
        self.write_artifact("v0.json", {"artifact_schema_version": 0})
        self.write_artifact("extra.json", v1_artifact("extra", updated_at=5))
        self.write_artifact("nohist.json", v1_artifact("nohist", history=[]))
        self.write_artifact("bad.json", None, raw="{nope")
        before = snapshot(self.home.artifacts_dir)
        first = self.migrate()
        expected = (
            SESSIONS_SUMMARY.format(0, 0, 0) + "\n"
            "  skipped  bad.json (JSONDecodeError: Expecting property name enclosed in double quotes: line 1 column 2 (char 1))\n"
            "  skipped  cur.json (already v2)\n"
            "  skipped  extra.json (UnsupportedArtifactError: artifact 'extra' has an invalid key set: unexpected ['updated_at'])\n"
            "  skipped  nohist.json (UnsupportedArtifactError: artifact 'nohist' has an empty history (entry 0 must be the create))\n"
            "  skipped  v0.json (not an upgradable artifact record (artifact_schema_version=0))\n"
            + ARTIFACTS_SUMMARY.format(0, 5) + "\n"
        )
        self.assertEqual(first.out, expected)
        self.assertNotIn("  migrated", first.out)
        self.assertEqual(snapshot(self.home.artifacts_dir), before)
        second = self.migrate()
        self.assertEqual(second.out, first.out)
        self.assertEqual(snapshot(self.home.artifacts_dir), before)
