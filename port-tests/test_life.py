"""LIFE — `lib/tx/service.py` lifecycle: kill / archive / rm / tag / group / bind / rename mutations,
hook-driven `record_state`, id/name resolution, and the rev-4 `tx revive` verb (spec section 02,
T-LIFE-01..12).

Every mutation is asserted through the record file, `log.jsonl` and the private tmux server; errors
through `tx <cmd>: <msg>` + exit 1.
"""

from __future__ import annotations

import json
import re
import time

from txkit import TxCase, expected_failure_on_python

NOT_FOUND = "not found (no live @tx_id, no store record)"
# A stale Location seeded on every crafted live record: the builders default `attached_to` to `[]`,
# so "cleared / re-snapshotted to []" would otherwise pass without tx writing anything.
STALE_LOCATION = {"host": "stale", "window_index": "9", "window_name": "x", "pane_id": "%99", "pane_index": "9"}


class TestLife(TxCase):
    # ----- fixtures -----------------------------------------------------------------------

    def live_shell(self, session_id: str = "U", name: str = "w", **fields) -> str:
        """A crafted alive shell record whose tmux session (named by its id) is live with `@tx_id`;
        `attached_to` starts stale (see `STALE_LOCATION`) unless the caller passes its own."""
        fields.setdefault("attached_to", (STALE_LOCATION,))
        self.records.other(id=session_id, name=name, state="alive", **fields)
        self.tmux.new_session(session_id, "sleep 300", tx_id=session_id)
        return session_id

    def log_entries(self) -> list[tuple[str, str, str]]:
        return [(line["actor"], line["type"], line["msg"]) for line in self.log_lines()]

    # ----- T-LIFE-01 ----------------------------------------------------------------------

    def test_t_life_01_kill_live_process(self):
        self.live_shell()
        self.home.launch_dir.mkdir(exist_ok=True)
        launch_script = self.home.launch_dir / "U.sh"
        launch_script.write_text("sleep 300\n")
        before = len(self.log_lines())

        result = self.tx(["kill", "w"])

        self.assertEqual((result.code, result.out, result.err), (0, "Killed 'w'\n", ""))
        self.assertFalse(self.tmux.has_session("U"))
        self.assertFalse(launch_script.exists())
        record = self.records.load("U")
        self.assertEqual(record["state"], "exited")
        self.assertAlmostEqual(record["ended_at"], time.time(), delta=5)
        self.assertEqual(record["attached_to"], [])  # the stale Location was cleared
        self.assertEqual(self.log_entries()[before:], [("", "kill", "w")])

    def test_t_life_01_kill_by_id(self):
        self.live_shell()
        self.home.launch_dir.mkdir(exist_ok=True)
        launch_script = self.home.launch_dir / "U.sh"
        launch_script.write_text("sleep 300\n")
        before = len(self.log_lines())

        result = self.tx(["kill", "U"])

        self.assertEqual((result.code, result.out, result.err), (0, "Killed 'w'\n", ""))
        self.assertFalse(self.tmux.has_session("U"))
        self.assertFalse(launch_script.exists())
        record = self.records.load("U")
        self.assertEqual(record["state"], "exited")
        self.assertAlmostEqual(record["ended_at"], time.time(), delta=5)
        self.assertEqual(record["attached_to"], [])
        self.assertEqual(self.log_entries()[before:], [("", "kill", "w")])

    def test_t_life_01_kill_is_idempotent_and_tolerates_missing_launch_script(self):
        self.records.other(id="U", name="w", state="exited", ended_at=1234.5)
        self.assertFalse((self.home.launch_dir / "U.sh").exists())
        before = len(self.log_lines())
        path = self.records.path("U")
        modified_before = path.stat().st_mtime_ns

        result = self.tx(["kill", "w"])

        self.assertEqual((result.code, result.out, result.err), (0, "Killed 'w'\n", ""))
        record = self.records.load("U")
        self.assertEqual((record["state"], record["ended_at"]), ("exited", 1234.5))
        self.assertNotEqual(path.stat().st_mtime_ns, modified_before)  # rewritten
        self.assertEqual(self.log_entries()[before:], [("", "kill", "w")])

    def test_t_life_01_kill_actor_is_the_invoker(self):
        self.live_shell()
        self.tx(["kill", "w"], env={"TX_SESSION_ID": "caller-id"})
        self.assertEqual(self.log_entries()[-1], ("caller-id", "kill", "w"))

    # ----- T-LIFE-02 ----------------------------------------------------------------------

    def test_t_life_02_kill_view_fallback(self):
        self.tmux.new_session("Views", "sleep 300")
        self.tmux.run("set-option", "-t", "Views", "@tx_view", "1", check=True)
        self.tmux.new_session("plain", "sleep 300")
        before = len(self.log_lines())

        result = self.tx(["kill", "Views"])

        self.assertEqual((result.code, result.out, result.err), (0, "Killed 'Views'\n", ""))
        self.assertFalse(self.tmux.has_session("Views"))
        self.assertEqual(self.log_entries()[before:], [("", "kill", "Views")])
        self.assertEqual(list(self.home.sessions_dir.iterdir()), [])

        result = self.tx(["kill", "plain"])
        self.assertEqual((result.code, result.out), (1, ""))
        self.assertEqual(result.err, f"tx kill: session 'plain' {NOT_FOUND}\n")
        self.assertTrue(self.tmux.has_session("plain"))
        self.assertEqual(len(self.log_lines()), before + 1)

    # ----- T-LIFE-03 ----------------------------------------------------------------------

    def test_t_life_03_archive(self):
        self.live_shell()
        before = len(self.log_lines())

        result = self.tx(["archive", "w"])

        self.assertEqual((result.code, result.out, result.err), (0, "Archived 'w'\n", ""))
        record = self.records.load("U")
        self.assertEqual(record["state"], "archived")
        self.assertAlmostEqual(record["ended_at"], time.time(), delta=5)
        self.assertEqual(record["attached_to"], [])
        self.assertEqual(self.log_entries()[before:], [("", "archive", "w")])
        self.assertTrue(self.tmux.has_session("U"))

    def test_t_life_03_archive_exited_record_stays_exited(self):
        self.records.other(id="U", name="w", state="exited", ended_at=1234.5)
        before = len(self.log_lines())
        path = self.records.path("U")
        modified_before = path.stat().st_mtime_ns

        result = self.tx(["archive", "w"])

        self.assertEqual((result.code, result.out, result.err), (0, "Archived 'w'\n", ""))
        record = self.records.load("U")
        self.assertEqual((record["state"], record["ended_at"]), ("exited", 1234.5))
        self.assertNotEqual(path.stat().st_mtime_ns, modified_before)  # rewritten
        self.assertEqual(self.log_entries()[before:], [("", "archive", "w")])

    def test_t_life_03_archive_unknown(self):
        result = self.tx(["archive", "nope"])
        self.assertEqual((result.code, result.out), (1, ""))
        self.assertEqual(result.err, f"tx archive: session 'nope' {NOT_FOUND}\n")

    # ----- T-LIFE-04 ----------------------------------------------------------------------

    def test_t_life_04_remove(self):
        self.live_shell()
        before = len(self.log_lines())

        result = self.tx(["rm", "w"])

        self.assertEqual((result.code, result.out, result.err), (0, "Removed record for 'w'\n", ""))
        self.assertFalse(self.records.path("U").exists())
        self.assertEqual(self.log_entries()[before:], [("", "rm", "w (U)")])
        self.assertTrue(self.tmux.has_session("U"))

        result = self.tx(["rm", "nope"])
        self.assertEqual((result.code, result.out, result.err), (1, "", "tx rm: no record for 'nope'\n"))
        self.assertEqual(len(self.log_lines()), before + 1)

    # ----- T-LIFE-05 ----------------------------------------------------------------------

    def test_t_life_05_tag_with_nested_client(self):
        # The view goes first so a `tx` call (scrubbed env) starts the private server.
        self.spawn_view("Views")
        self.live_shell(tags=("a",))
        self.tmux.run("rename-window", "-t", "Views:0", "w0", check=True)
        nested_pane = self.tmux.split_window("Views:0")
        self.assertEqual(self.tmux.display(nested_pane, "#{pane_index}"), "1")
        self.tmux.nest_attach(nested_pane, "U")
        before = len(self.log_lines())

        result = self.tx(["tag", "w", "x,y"])

        self.assertEqual((result.code, result.out, result.err), (0, "Tagged 'w' (tag=x,y)\n", ""))
        record = self.records.load("U")
        self.assertEqual(record["tags"], ["x", "y"])
        self.assertEqual(
            record["attached_to"],
            [{"host": "Views", "window_index": "0", "window_name": "w0", "pane_id": nested_pane, "pane_index": "1"}],
        )
        self.assertEqual(self.log_entries()[before:], [("", "tag", "w x,y")])

        result = self.tx(["tag", "w", ""])
        self.assertEqual((result.code, result.out), (0, "Tagged 'w' (tag=)\n"))
        self.assertEqual(self.records.load("U")["tags"], [])
        self.assertEqual(self.log_entries()[before + 1:], [("", "tag", "w ")])

    def test_t_life_05_tag_without_nested_client_and_read_mode(self):
        self.live_shell(tags=("a",))
        self.tx(["tag", "w", "x,y"])
        self.assertEqual(self.records.load("U")["attached_to"], [])
        before = len(self.log_lines())
        path = self.records.path("U")
        modified_before = path.stat().st_mtime_ns

        result = self.tx(["tag", "w"])

        self.assertEqual((result.code, result.out, result.err), (0, "x,y\n", ""))
        self.assertEqual(path.stat().st_mtime_ns, modified_before)
        self.assertEqual(len(self.log_lines()), before)

    def test_t_life_05_tag_unknown(self):
        result = self.tx(["tag", "nope", "x"])
        self.assertEqual((result.code, result.out), (1, ""))
        self.assertEqual(result.err, f"tx tag: session 'nope' {NOT_FOUND}\n")
        result = self.tx(["tag", "nope"])
        self.assertEqual((result.code, result.out, result.err), (1, "", "tx tag: session 'nope' not found\n"))

    # ----- T-LIFE-06 ----------------------------------------------------------------------

    def test_t_life_06_set_group(self):
        self.live_shell()
        before = len(self.log_lines())

        result = self.tx(["group", "w", "g1"])

        self.assertEqual((result.code, result.out, result.err), (0, "Grouped 'w' (group=g1)\n", ""))
        record = self.records.load("U")
        self.assertEqual((record["group"], record["attached_to"]), ("g1", []))
        self.assertEqual(self.log_entries()[before:], [("", "group", "w g1")])

        result = self.tx(["group", "w", "--clear"])
        self.assertEqual((result.code, result.out, result.err), (0, "Cleared group override on 'w' (back to derived)\n", ""))
        record = self.records.load("U")
        self.assertEqual((record["group"], record["attached_to"]), (None, []))
        self.assertEqual(self.log_entries()[before + 1:], [("", "group", "w (cleared)")])

    def test_t_life_06_empty_group_rejected(self):
        self.live_shell(group="keep")
        before = len(self.log_lines())
        path = self.records.path("U")
        modified_before = path.stat().st_mtime_ns

        result = self.tx(["group", "w", ""])

        self.assertEqual((result.code, result.out), (2, ""))
        self.assertTrue(result.err.startswith("usage: tx group"), result.err)
        self.assertTrue(
            result.err.endswith("\ntx group: error: a group cannot be empty — use --clear to drop the override\n"),
            result.err,
        )
        self.assertEqual(path.stat().st_mtime_ns, modified_before)
        self.assertEqual(self.records.load("U")["group"], "keep")
        self.assertEqual(len(self.log_lines()), before)

    # ----- T-LIFE-07 ----------------------------------------------------------------------

    def test_t_life_07_bind_artifact(self):
        source = self.root / "plan.md"
        source.write_text("# plan\n")
        created = self.tx(["artifact", "create", str(source), "--title", "T"])
        self.assertEqual(created.code, 0, created.err)
        artifact_id = re.fullmatch(r"Created artifact ([0-9a-f-]{36}) \(plan\.md\)\n", created.out).group(1)
        view_name = f"art-{artifact_id[:8]}"
        current_path = self.home.artifacts_dir / artifact_id / "current.md"
        before = len(self.log_lines())

        result = self.tx(["artifact", "open", artifact_id, "--tag", "a"])

        self.assertEqual((result.code, result.err), (0, ""))
        self.assertEqual(result.out, f"Opened artifact {artifact_id} in nvim view '{view_name}' ({current_path})\n")
        record = json.loads(self.tx(["show", view_name]).out)
        self.assertEqual((record["role"], record["name"], record["artifact_id"], record["tags"]), ("nvim", view_name, artifact_id, ["a"]))
        self.assertIn(record["id"], self.tmux.sessions())
        self.assertEqual(
            self.log_entries()[before:],
            [
                ("", "spawn", f"{view_name} [nvim] {self.home.artifacts_dir / artifact_id}"),
                ("", "bind-artifact", f"{view_name} → {artifact_id}"),
                ("user", "artifact-open", f"{artifact_id} → user"),
            ],
        )

    # ----- T-LIFE-08 ----------------------------------------------------------------------

    def test_t_life_08_rename_is_store_only(self):
        self.live_shell(name="old")
        before = len(self.log_lines())

        result = self.tx(["rename", "old", "new"])

        self.assertEqual((result.code, result.out, result.err), (0, "Renamed to 'new'\n", ""))
        record = self.records.load("U")
        self.assertEqual((record["name"], record["attached_to"]), ("new", []))
        self.assertIn("U", self.tmux.sessions())
        self.assertEqual(self.tmux.option("U", "@tx_id"), "U")
        self.assertEqual(self.log_entries()[before:], [("", "rename", "old → new")])

    def test_t_life_08_rename_to_same_name_is_a_no_op(self):
        self.live_shell(name="old")
        path = self.records.path("U")
        bytes_before = path.read_bytes()
        before = len(self.log_lines())

        result = self.tx(["rename", "old", "old"])

        self.assertEqual((result.code, result.out, result.err), (0, "Renamed to 'old'\n", ""))
        self.assertEqual(path.read_bytes(), bytes_before)
        self.assertEqual(len(self.log_lines()), before)

    def test_t_life_08_rename_collisions(self):
        self.live_shell(name="old")
        self.live_shell(session_id="U2", name="new")
        self.tmux.new_session("viewname", "sleep 300")
        self.tmux.run("set-option", "-t", "viewname", "@tx_view", "1", check=True)
        self.records.other(id="U3", name="gone", state="exited", ended_at=10.0)
        self.tmux.new_session("untracked", "sleep 300")
        before = len(self.log_lines())

        result = self.tx(["rename", "old", "new"])
        self.assertEqual((result.code, result.out, result.err), (1, "", "tx rename: session 'new' already exists\n"))
        result = self.tx(["rename", "old", "viewname"])
        self.assertEqual((result.code, result.out, result.err), (1, "", "tx rename: 'viewname' is a live view session — pick another name\n"))
        self.assertEqual(self.records.load("U")["name"], "old")
        self.assertEqual(len(self.log_lines()), before)

        result = self.tx(["rename", "old", "gone"])
        self.assertEqual((result.code, result.out), (0, "Renamed to 'gone'\n"))
        result = self.tx(["rename", "gone", "untracked"])
        self.assertEqual((result.code, result.out), (0, "Renamed to 'untracked'\n"))
        self.assertEqual(self.records.load("U")["name"], "untracked")
        self.assertEqual(self.log_entries()[before:], [("", "rename", "old → gone"), ("", "rename", "gone → untracked")])

    def test_t_life_08_rename_never_resolves_a_view(self):
        self.tmux.new_session("Views", "sleep 300")
        self.tmux.run("set-option", "-t", "Views", "@tx_view", "1", check=True)
        result = self.tx(["rename", "Views", "x"])
        self.assertEqual((result.code, result.out), (1, ""))
        self.assertEqual(result.err, f"tx rename: session 'Views' {NOT_FOUND}\n")
        self.assertTrue(self.tmux.has_session("Views"))

    # ----- T-LIFE-09 ----------------------------------------------------------------------

    def live_llm(self) -> None:
        self.records.llm(id="U", name="w", state="idle", last_activity=900.0, turn_started_at=None,
                         attached_to=(STALE_LOCATION,))
        self.tmux.new_session("U", "sleep 300", tx_id="U")

    def test_t_life_09_record_state_transitions(self):
        self.live_llm()
        before = len(self.log_lines())

        result = self.tx(["hook", "working"], env={"TX_SESSION_ID": "U"})

        self.assertEqual((result.code, result.out, result.err), (0, "", ""))
        record = self.records.load("U")
        self.assertEqual(record["state"], "working")
        self.assertAlmostEqual(record["turn_started_at"], time.time(), delta=5)
        self.assertEqual(record["last_activity"], record["turn_started_at"])
        self.assertEqual(record["attached_to"], [])
        self.assertEqual(self.log_entries()[before:], [("U", "state", "w → working")])
        turn_started_at = record["turn_started_at"]

        result = self.tx(["hook", "stop"], env={"TX_SESSION_ID": "U"})
        self.assertEqual(result.code, 0, result.err)
        record = self.records.load("U")
        self.assertEqual(record["state"], "waiting")
        self.assertEqual((record["turn_started_at"], record["last_activity"]), (turn_started_at, turn_started_at))
        self.assertEqual(self.log_entries()[before + 1:], [("U", "state", "w → waiting")])

    def test_t_life_09_reaffirmed_working_is_a_no_op(self):
        self.live_llm()
        self.tx(["hook", "working"], env={"TX_SESSION_ID": "U"})
        path = self.records.path("U")
        bytes_before = path.read_bytes()
        before = len(self.log_lines())

        result = self.tx(["hook", "working"], env={"TX_SESSION_ID": "U"})

        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(path.read_bytes(), bytes_before)
        self.assertEqual(len(self.log_lines()), before)

    def test_t_life_09_terminal_is_absorbing(self):
        self.live_llm()
        self.tx(["kill", "w"])
        path = self.records.path("U")
        record = self.records.load("U")
        self.assertEqual((record["state"], record["attached_to"]), ("exited", []))
        self.assertIsNotNone(record["ended_at"])
        bytes_before = path.read_bytes()
        before = len(self.log_lines())

        result = self.tx(["hook", "working"], env={"TX_SESSION_ID": "U"})

        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(path.read_bytes(), bytes_before)
        self.assertEqual(len(self.log_lines()), before)

    def test_t_life_09_unknown_or_unset_session_id_is_ignored(self):
        self.live_llm()
        before = len(self.log_lines())
        entries_before = sorted(path.name for path in self.home.sessions_dir.iterdir())

        result = self.tx(["hook", "working"], env={"TX_SESSION_ID": "nonexistent"})
        self.assertEqual((result.code, result.out, result.err), (0, "", ""))
        result = self.tx(["hook", "working"])
        self.assertEqual((result.code, result.out, result.err), (0, "", ""))

        self.assertEqual(sorted(path.name for path in self.home.sessions_dir.iterdir()), entries_before)
        self.assertEqual(self.records.load("U")["state"], "idle")
        self.assertEqual(len(self.log_lines()), before)

    # ----- T-LIFE-10 ----------------------------------------------------------------------

    def resolution_fixture(self) -> None:
        self.records.other(id="U1", name="n", state="exited", created_at=100.0, ended_at=150.0)
        self.records.other(id="U2", name="n", state="alive", created_at=50.0)
        self.tmux.new_session("U2", "sleep 300", tx_id="U2")
        self.records.other(id="U3", name="n", state="exited", created_at=200.0, ended_at=250.0)
        self.tmux.new_session("alias", "sleep 300", tx_id="U1")

    def shown_id(self, token: str) -> str:
        result = self.tx(["show", token])
        self.assertEqual(result.code, 0, result.err)
        return json.loads(result.out)["id"]

    def test_t_life_10_resolve_order(self):
        self.resolution_fixture()

        self.assertEqual(self.shown_id("U3"), "U3")
        self.assertEqual(self.shown_id("alias"), "U1")
        self.assertEqual(self.shown_id("n"), "U2")
        result = self.tx(["show", "zzz"])
        self.assertEqual((result.code, result.out, result.err), (1, "", "tx show: no record for 'zzz'\n"))
        result = self.tx(["_tmux-name", "n"])
        self.assertEqual((result.code, result.out), (0, "U2\n"))
        result = self.tx(["_tmux-name", "U3"])
        self.assertEqual((result.code, result.out), (0, ""))

    def test_t_life_10_newest_non_live_wins_when_nothing_is_live(self):
        self.resolution_fixture()
        self.tmux.kill_session("U2")
        self.assertEqual(self.shown_id("n"), "U3")

    def test_t_life_10_ghost_tx_id_falls_through_to_name_lookup(self):
        self.resolution_fixture()
        self.tmux.new_session("n", "sleep 300", tx_id="ghost")
        self.assertEqual(self.shown_id("n"), "U2")
        self.tmux.kill_session("U2")
        self.assertEqual(self.shown_id("n"), "U3")

    # ----- T-LIFE-11 ----------------------------------------------------------------------

    def test_t_life_11_cli_error_mapping(self):
        result = self.tx(["tag", "nope", "x"])
        self.assertEqual((result.code, result.out), (1, ""))
        self.assertEqual(result.err, f"tx tag: session 'nope' {NOT_FOUND}\n")

        help_text = self.tx(["help"])
        self.assertEqual(help_text.code, 0)
        self.assertTrue(help_text.out.startswith("tx — tmux + Claude Code session controller\n\nusage: tx <command> [args]\n"))

        result = self.tx(["bogus"])
        self.assertEqual((result.code, result.err), (2, "tx: unknown command: bogus\n\n"))
        self.assertEqual(result.out, help_text.out)

        bare = self.tx([])
        self.assertEqual((bare.code, bare.out, bare.err), (0, help_text.out, ""))

    # ----- T-LIFE-12 ----------------------------------------------------------------------

    @expected_failure_on_python
    def test_t_life_12_revive_exited_record_whose_tx_id_session_is_alive(self):
        # The Q32 shape: `w` was exited while its session lived on (server unreachable / hand edit).
        self.records.llm(id="U", name="w", state="exited", ended_at=1234.5, attached_to=(STALE_LOCATION,))
        self.tmux.new_session("U", "sleep 300", tx_id="U")
        self.records.other(id="U2", name="x", state="exited", ended_at=1234.5)
        self.records.other(id="U3", name="z", state="archived", ended_at=1234.5)
        self.tmux.new_session("U3", "sleep 300", tx_id="U3")
        # Reconcile-on-read never revives (Q32): a plain `tx ls` leaves `w` exited.
        self.assertEqual(self.tx(["ls"]).code, 0)
        self.assertEqual(self.records.load("U")["state"], "exited")
        before = len(self.log_lines())

        result = self.tx(["revive", "w"])

        self.assertEqual((result.code, result.out, result.err), (0, "Revived 'w'\n", ""))
        record = self.records.load("U")
        self.assertEqual((record["state"], record["ended_at"], record["attached_to"]), ("idle", None, []))
        self.assertEqual(self.log_entries()[before:], [("", "revive", "w (U)")])
        revived = self.records.path("U").read_bytes()

        listing = self.tx(["ls"])
        self.assertEqual(listing.code, 0, listing.err)
        self.assertEqual(listing.lines[0], "PROCESSES")
        self.assertTrue(any(re.match(r"\s+w\s+idle\b", line) for line in listing.lines[1:]), listing.out)
        self.assertEqual(self.records.path("U").read_bytes(), revived)  # not re-exited
        self.assertEqual(len(self.log_lines()), before + 1)

        untouched = self.records.path("U2").read_bytes()
        result = self.tx(["revive", "x"])
        self.assertEqual((result.code, result.out), (1, ""))
        self.assertTrue(result.err.startswith("tx revive: ") and "'x'" in result.err, result.err)
        self.assertEqual(self.records.path("U2").read_bytes(), untouched)
        self.assertEqual(len(self.log_lines()), before + 1)

        archived = self.records.path("U3").read_bytes()
        result = self.tx(["revive", "z"])
        self.assertEqual((result.code, result.out), (1, ""))
        self.assertTrue(result.err.startswith("tx revive: ") and "'z'" in result.err, result.err)
        self.assertEqual(self.records.load("U3")["state"], "archived")
        self.assertEqual(self.records.path("U3").read_bytes(), archived)
        self.assertEqual(len(self.log_lines()), before + 1)

        modified_before = self.records.path("U").stat().st_mtime_ns
        result = self.tx(["revive", "w"])
        self.assertEqual((result.code, result.out, result.err), (0, "'w' is already live\n", ""))
        self.assertEqual(self.records.path("U").stat().st_mtime_ns, modified_before)  # no write
        self.assertEqual(self.records.path("U").read_bytes(), revived)
        self.assertEqual(len(self.log_lines()), before + 1)

    @expected_failure_on_python
    def test_t_life_12_revive_non_llm_record_to_alive(self):
        """D14: a non-llm record revives to `alive` (llm → `idle`, the leg above)."""
        self.records.other(id="U4", name="sh", state="exited", ended_at=1234.5, attached_to=(STALE_LOCATION,))
        self.tmux.new_session("U4", "sleep 300", tx_id="U4")
        self.assertEqual(self.tx(["ls"]).code, 0)
        self.assertEqual(self.records.load("U4")["state"], "exited")  # reconcile-on-read never revives
        before = len(self.log_lines())

        result = self.tx(["revive", "sh"])

        self.assertEqual((result.code, result.out, result.err), (0, "Revived 'sh'\n", ""))
        record = self.records.load("U4")
        self.assertEqual((record["state"], record["ended_at"], record["attached_to"]), ("alive", None, []))
        self.assertEqual(self.log_entries()[before:], [("", "revive", "sh (U4)")])
        revived = self.records.path("U4").read_bytes()
        listing = self.tx(["ls"])
        self.assertEqual(listing.code, 0, listing.err)
        self.assertTrue(any(re.match(r"\s+sh\s+alive\b", line) for line in listing.lines[1:]), listing.out)
        self.assertEqual(self.records.path("U4").read_bytes(), revived)  # not re-exited
        self.assertEqual(len(self.log_lines()), before + 1)

    @expected_failure_on_python
    def test_t_life_12_revive_unknown_session(self):
        result = self.tx(["revive", "nope"])
        self.assertEqual((result.code, result.out), (1, ""))
        self.assertEqual(result.err, f"tx revive: session 'nope' {NOT_FOUND}\n")
        self.assertEqual(self.log_lines(), [])
