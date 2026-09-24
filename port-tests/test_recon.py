"""RECON — the reconcile-on-read sweep (spec section 01, T-RECON-01..10; 11 DROPPED).

Every reconcile here is `tx ls`. A crafted record is "live" when the private server has a session
carrying `@tx_id=<id>`; ages are explicit past timestamps (D8), chosen mid-bucket.
"""

from __future__ import annotations

import json
import os
import re
import time

from txkit import TxCase, expected_failure_on_python

SKIP_V3 = (
    "tx: skipping unreadable record bad.json: record schema_version=3 is unsupported (expected 6); "
    "tx-ide does not back-migrate older records on load (§9) — run `tx migrate` to upgrade older records"
)


class TestRecon(TxCase):
    def snapshot(self, *ids: str) -> dict[str, tuple[bytes, int]]:
        return {
            session_id: (self.records.path(session_id).read_bytes(), self.records.path(session_id).stat().st_mtime_ns)
            for session_id in ids
        }

    def assert_unchanged(self, snapshot: dict[str, tuple[bytes, int]]) -> None:
        self.assertEqual(self.snapshot(*snapshot), snapshot)

    def state_of(self, session_id: str) -> str:
        return self.records.load(session_id)["state"]

    def rows(self, out: str) -> list[str]:
        lines = out.splitlines()
        self.assertEqual(lines[0], "PROCESSES")
        return lines[1:]

    def wait_pane_commands(self, expected: dict[str, str]) -> None:
        self.wait_until(
            lambda: all(self.tmux.pane_commands().get(key) == value for key, value in expected.items())
        )

    # ----- T-RECON-01 --------------------------------------------------------------------------

    def test_t_recon_01_match_key_is_tx_id(self):
        self.records.llm(id="abc", name="abc", state="idle")
        self.tmux.new_session("abc", "sleep 1000")  # foreign: literally named abc, no @tx_id
        self.tmux.new_session("zz9", "sleep 1000", tx_id="abc")
        result = self.tx(["ls"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self.state_of("abc"), "idle")
        self.assertEqual(self.rows(result.out), ["  abc                      idle     —                         -      [t]"])
        self.assertFalse(self.home.log_path.exists())
        self.tmux.kill_session("zz9")
        result = self.tx(["ls"])
        self.assertEqual(result.code, 0, result.err)
        self.assertTrue(self.tmux.has_session("abc"))  # the foreign session is still live
        self.assertEqual(self.state_of("abc"), "exited")
        self.assertEqual(result.out, "PROCESSES\n")  # a foreign row never appears
        lines = self.log_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["type"], "reconcile")
        self.assertEqual(lines[0]["msg"], "abc → exited (vanished)")

    def test_t_recon_01_no_server_drives_every_non_terminal_record_to_exited(self):
        ids = [self.records.llm(id=f"r-{state}", state=state) for state in ("alive", "working", "waiting", "idle")]
        self.tmux.run("kill-server")
        result = self.tx(["ls"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, "PROCESSES\n")
        for session_id in ids:
            self.assertEqual(self.state_of(session_id), "exited")
        self.assertEqual([line["type"] for line in self.log_lines()], ["reconcile"] * 4)

    # ----- T-RECON-02 --------------------------------------------------------------------------

    def test_t_recon_02_vanished_to_exited(self):
        location = {"host": "Views", "window_index": "1", "window_name": "work", "pane_id": "%41", "pane_index": "1"}
        self.records.llm(id="abc", name="worker", state="idle", attached_to=(location,), ended_at=None)
        t0 = time.time()
        result = self.tx(["ls"])
        t1 = time.time()
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, "PROCESSES\n")
        record = self.records.load("abc")
        self.assertEqual(record["state"], "exited")
        self.assertTrue(t0 <= record["ended_at"] <= t1, record["ended_at"])
        self.assertEqual(record["attached_to"], [])
        last = self.log_tail()[0]
        self.assertEqual(list(last), ["ts", "actor", "type", "msg"])
        self.assertEqual(last["type"], "reconcile")
        self.assertEqual(last["msg"], "worker → exited (vanished)")
        raw = self.home.log_path.read_bytes().splitlines()[-1]
        self.assertIn(b'"msg":"worker \\u2192 exited (vanished)"', raw)

    def test_t_recon_02_every_non_terminal_state_and_role(self):
        ids = [self.records.llm(id=f"llm-{state}", name=f"llm-{state}", state=state) for state in ("alive", "working", "waiting", "idle")]
        ids += [self.records.other(id=f"o-{role}", name=f"o-{role}", role=role, state="alive") for role in ("nvim", "shell", "other")]
        result = self.tx(["ls"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, "PROCESSES\n")
        for session_id in ids:
            record = self.records.load(session_id)
            self.assertEqual(record["state"], "exited", session_id)
            self.assertIsNotNone(record["ended_at"])
            self.assertEqual(record["attached_to"], [])
        self.assertEqual(
            sorted(line["msg"] for line in self.log_lines()),
            sorted(f"{session_id} → exited (vanished)" for session_id in ids),
        )

    # ----- T-RECON-03 --------------------------------------------------------------------------

    def test_t_recon_03_terminal_records_skipped(self):
        self.records.llm(id="e", name="e", state="exited", ended_at=1.0)
        self.records.llm(id="a", name="a", state="archived", ended_at=1.0)
        self.records.llm(id="i", name="i", state="idle")
        self.live("i", "bash")
        before = self.snapshot("e", "a", "i")
        self.assertFalse(self.home.log_path.exists())
        for _ in range(2):
            result = self.tx(["ls"])
            self.assertEqual(result.code, 0, result.err)
            rows = self.rows(result.out)
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0].startswith("  i                        idle "), rows[0])
        self.assert_unchanged(before)
        self.assertFalse(self.home.log_path.exists())

    # ----- T-RECON-04 --------------------------------------------------------------------------

    def fixture_lines(self, session_id: str, keys: tuple[str, ...]) -> list[str]:
        text = self.records.path(session_id).read_text()
        return [line for line in text.splitlines() if any(line.startswith(f'  "{key}":') for key in keys)]

    def test_t_recon_04_stuck_working_demotion(self):
        now = time.time()
        self.records.llm(id="w", name="stuck", state="working", turn_started_at=now - 1200, ended_at=None)
        self.live("w", "bash")
        self.wait_pane_commands({"w": "bash"})
        fixture = self.fixture_lines("w", ("attached_to", "turn_started_at"))
        result = self.tx(["ls"])
        self.assertEqual(result.code, 0, result.err)
        rows = self.rows(result.out)
        self.assertEqual(len(rows), 1)
        self.assertRegex(rows[0], r"^  stuck\s+idle ")
        record = self.records.load("w")
        self.assertEqual(record["state"], "idle")
        self.assertIsNone(record["ended_at"])
        self.assertEqual(self.fixture_lines("w", ("attached_to", "turn_started_at")), fixture)
        last = self.log_tail()[0]
        self.assertEqual(last["type"], "reconcile")
        self.assertEqual(last["msg"], "stuck working → idle (stuck)")

    def test_t_recon_04_young_turn_is_not_demoted(self):
        now = time.time()
        self.records.llm(id="w", name="stuck", state="working", turn_started_at=now - 60, ended_at=None)
        self.live("w", "bash")
        self.wait_pane_commands({"w": "bash"})
        before = self.snapshot("w")
        result = self.tx(["ls"])
        self.assertEqual(result.code, 0, result.err)
        self.assertRegex(self.rows(result.out)[0], r"^  stuck\s+working ")
        self.assert_unchanged(before)
        self.assertFalse(self.home.log_path.exists())

    # ----- T-RECON-05 --------------------------------------------------------------------------

    AGENT_COMMANDS = (
        "claude", "/usr/local/bin/claude", "codex", "agy", "bwrap", "sandbox-exec",
        "2.1.138", "1.2", "10.0.0-beta", "1.2abc",
    )
    NON_AGENT_COMMANDS = ("bash", "zsh", "node", "python3.14", "v2.1", "2", "claude-code", ".5.1", "gemini")

    def stuck_probes(self, names: tuple[str, ...]) -> dict[str, str]:
        """One stuck WORKING llm record + a live pane per probe name; returns id → expected
        `pane_current_command` (tmux reports the basename of argv[0])."""
        now = time.time()
        expected: dict[str, str] = {}
        for index, name in enumerate(names):
            session_id = f"probe-{index}"
            self.records.llm(id=session_id, name=session_id, state="working", turn_started_at=now - 1200)
            self.tmux.new_session(session_id, f"bash -c 'exec -a {name} sleep 600'", tx_id=session_id)
            expected[session_id] = os.path.basename(name)
        self.wait_pane_commands(expected)
        return expected

    def test_t_recon_05_agent_command_keeps_working(self):
        probes = self.stuck_probes(self.AGENT_COMMANDS)
        result = self.tx(["ls"])
        self.assertEqual(result.code, 0, result.err)
        for session_id in probes:
            self.assertEqual(self.state_of(session_id), "working", session_id)
        self.assertFalse(self.home.log_path.exists())
        self.assertEqual(len(self.rows(result.out)), len(probes))

    def test_t_recon_05_non_agent_command_is_demoted(self):
        probes = self.stuck_probes(self.NON_AGENT_COMMANDS)
        result = self.tx(["ls"])
        self.assertEqual(result.code, 0, result.err)
        for session_id in probes:
            self.assertEqual(self.state_of(session_id), "idle", session_id)
        self.assertEqual(
            sorted(line["msg"] for line in self.log_lines()),
            sorted(f"{session_id} working → idle (stuck)" for session_id in probes),
        )
        self.assertTrue(all(line["type"] == "reconcile" for line in self.log_lines()))

    # ----- T-RECON-06 --------------------------------------------------------------------------

    def test_t_recon_06_stuck_guards(self):
        now = time.time()
        self.records.llm(id="a", name="a", state="working", turn_started_at=None)
        self.records.llm(id="b", name="b", state="waiting", turn_started_at=now - 9999)
        self.records.other(id="c", name="c", role="shell", state="working")
        for session_id in ("a", "b", "c"):
            self.live(session_id, "bash")
        self.wait_pane_commands({"a": "bash", "b": "bash", "c": "bash"})
        before = self.snapshot("a", "b", "c")
        result = self.tx(["ls"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.err, "")
        states = {row.split()[0]: row.split()[1] for row in self.rows(result.out)}
        self.assertEqual(states, {"a": "working", "b": "waiting", "c": "working"})
        self.assert_unchanged(before)
        self.assertFalse(self.home.log_path.exists())

    # ----- T-RECON-07 --------------------------------------------------------------------------

    def threshold_case(self, config: dict | None, age: float, session_id: str = "c", name: str = "cfg") -> None:
        if config is not None:
            self.home.write_config(config)
        now = time.time()
        self.records.llm(id=session_id, name=name, state="working", turn_started_at=now - age)
        self.live(session_id, "bash")
        self.wait_pane_commands({session_id: "bash"})

    def test_t_recon_07_threshold_from_config(self):
        self.threshold_case({"stuck_working_threshold_seconds": 60}, 300)
        result = self.tx(["ls"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self.state_of("c"), "idle")
        self.assertEqual(self.log_tail()[0]["type"], "reconcile")
        self.assertEqual(self.log_tail()[0]["msg"], "cfg working → idle (stuck)")

    def test_t_recon_07_large_threshold_keeps_working(self):
        self.threshold_case({"stuck_working_threshold_seconds": 3600}, 1200)
        before = self.snapshot("c")
        result = self.tx(["ls"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self.state_of("c"), "working")
        self.assert_unchanged(before)
        self.assertFalse(self.home.log_path.exists())

    def test_t_recon_07_config_without_key_uses_default(self):
        self.threshold_case({"other": 1}, 1200)
        result = self.tx(["ls"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self.state_of("c"), "idle")
        self.assertEqual(self.log_tail()[0]["msg"], "cfg working → idle (stuck)")

    # ----- T-RECON-08 --------------------------------------------------------------------------

    def test_t_recon_08_launch_script_sweep(self):
        now = time.time()
        launch = self.home.launch_dir
        for name, age in (("dead.sh", 120), ("young.sh", 5), ("live.sh", 3600), ("dead.txt", 3600)):
            path = launch / name
            path.write_text("#!/bin/sh\n")
            os.utime(path, (now - age, now - age))
        self.tmux.new_session("live", "sleep 1000", tx_id="live")
        result = self.tx(["ls"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, "PROCESSES\n")
        self.assertEqual(sorted(entry.name for entry in launch.iterdir()), ["dead.txt", "live.sh", "young.sh"])

    def test_t_recon_08_missing_launch_dir(self):
        # `main()` runs the home skeleton before every verb, so the dir is recreated (see NOTES).
        self.home.launch_dir.rmdir()
        result = self.tx(["ls"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, "PROCESSES\n")
        self.assertTrue(self.home.launch_dir.is_dir())
        self.assertEqual(list(self.home.launch_dir.iterdir()), [])

    # ----- T-RECON-09 --------------------------------------------------------------------------

    def test_t_recon_09_dirty_set_in_store_order(self):
        now = time.time()
        self.records.llm(id="o", name="o", state="idle")
        self.records.llm(id="s", name="s", state="working", turn_started_at=now - 1200)
        self.records.llm(id="v", name="v", state="idle")
        self.records.llm(id="x", name="x", state="exited", ended_at=1.0)
        self.live("o", "bash")
        self.live("s", "bash")
        self.wait_pane_commands({"o": "bash", "s": "bash"})
        before = self.snapshot("o", "x")
        self.assertFalse(self.home.log_path.exists())
        result = self.tx(["ls"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(
            [(line["type"], line["msg"]) for line in self.log_lines()],
            [("reconcile", "s working → idle (stuck)"), ("reconcile", "v → exited (vanished)")],
        )
        self.assert_unchanged(before)
        self.assertEqual(self.state_of("v"), "exited")
        self.assertEqual(self.state_of("s"), "idle")
        self.assertEqual(sorted(row.split()[0] for row in self.rows(result.out)), ["o", "s"])

    # ----- T-RECON-10 --------------------------------------------------------------------------

    def unreadable_fixture(self) -> bytes:
        bad = self.home.sessions_dir / "bad.json"
        bad.write_text('{"schema_version": 3, "id": "bad"}')
        self.records.llm(id="abc", name="abc", state="idle")
        return bad.read_bytes()

    def test_t_recon_10_parity_unreadable_record_does_not_abort(self):
        bad = self.unreadable_fixture()
        result = self.tx(["ls"])
        self.assertEqual(result.code, 0)
        self.assertGreaterEqual(result.err.count(SKIP_V3), 1, result.err)
        self.assertNotIn("Traceback", result.err)
        self.assertEqual(self.state_of("abc"), "exited")
        self.assertEqual((self.home.sessions_dir / "bad.json").read_bytes(), bad)

    @expected_failure_on_python
    def test_t_recon_10_fixed_skip_line_printed_once(self):
        self.unreadable_fixture()
        result = self.tx(["ls"])
        self.assertEqual(result.code, 0)
        self.assertEqual(result.err.count(SKIP_V3), 1, result.err)
        self.assertEqual(result.err, SKIP_V3 + "\n")
