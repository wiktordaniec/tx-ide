"""Spec area TMUX — `lib/tx/tmux.py` through the verbs that exercise it (T-TMUX-01..20).

Every case drives the binary under test against the private server and asserts through B3
surfaces: stdout / stderr / exit code, `sessions/*.json`, `log.jsonl`, `tmux list-sessions /
show-options / display-message / capture-pane / list-clients`, and fake-binary dumps.

tmux-version notes (host 3.4, CI floor 3.6):
- `#{pane_start_command}` renders args_escape-quoted on 3.4 (`"sleep 30"`, `\\$` for `$`);
  `_start_command` normalises that before comparing to the command tx handed tmux.
- A nonexistent `-c` cwd is tolerated on 3.4 (the pane starts in the server's `$HOME`, Q21).
- A pane created by a raw client call inherits that CLIENT's environment (PATH, cwd) on 3.4;
  `self.tmux.run` carries the scrubbed env, so a split/new-window pane resolves the wrapper `tmux`.
- `focus_attrs` reads `@remote-session` with a plain `show-options -vqt <pane>` (session scope), so
  the pane-scoped stamp `tx attach --host` writes (`-p`) is invisible to the reference envelope;
  T-TMUX-15's remote edge is a parity leg (session scope) plus a fixed leg (pane scope).
"""

from __future__ import annotations

import json
import os
import re
import subprocess

from txkit import REAL_TMUX, TxCase, expected_failure_on_python, python_reference_only, tmux_version

NOT_FOUND = "tx kill: session '{name}' not found (no live @tx_id, no store record)\n"
SET_OPTION_DEAD = re.compile(
    r"^tx spawn: tmux set-option -t (?P<id>[0-9a-f-]{36}) @tx_id (?P=id) failed: "
    r"no such session: (?P=id)\n$"
)
JUMP_FAILED = "tx: could not jump to or switch to session U\n"


def _start_command(case: TxCase, target: str) -> str:
    """`#{pane_start_command}` with tmux 3.4's args_escape rendering undone (surrounding double
    quotes; `\\$` → `$`, `\\"` → `"`, `\\\\` → `\\`). An unquoted rendering passes through."""
    rendered = case.tmux.display(target, "#{pane_start_command}")
    if len(rendered) >= 2 and rendered[0] == rendered[-1] == '"':
        return rendered[1:-1].replace("\\\\$", "$").replace('\\"', '"').replace("\\\\", "\\")
    return rendered


def _ls_rows(output: str) -> dict[str, tuple[str, str]]:
    """`tx ls` rows → name: (state, LOCATION cell), by the fixed columns of `render_ls`."""
    rows = {}
    for line in output.splitlines()[1:]:
        rows[line[2:26].strip()] = (line[27:35].strip(), line[36:61].strip())
    return rows


def _lines(capture: str) -> list[str]:
    return [line for line in capture.splitlines() if line]


def _picker_row(name: str, record_id: str) -> str:
    return f"{name}\t [t]\tL\t{record_id}\t{name} visual\n"


def _fzf_accepts(case: TxCase, name: str, record_id: str) -> None:
    """The fake fzf prints `name`'s row once (the row's field 4 is the tmux target), then exits 130
    on every later run — so a re-rendered picker ends the loop. Resets the sequence counter so a
    second configure in one test starts over (KIT CANDIDATE: `configure` should reset it)."""
    (case.fakes.knobs_dir / "fzf.json.count").unlink(missing_ok=True)
    case.fakes.configure(
        "fzf",
        sequence=[{"stdout": _picker_row(name, record_id), "exit_code": 0}, {"exit_code": 130}],
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class TestTmux(TxCase):
    def setUp(self) -> None:
        super().setUp()
        self.workdir = self.root / "w"
        self.workdir.mkdir()

    def _view(self, name: str) -> str:
        """A view on `self.workdir`; returns its (only) pane id."""
        self.spawn_view(name, cwd=self.workdir)
        return self.tmux.pane_id(name)

    def _nest(self, pane_id: str, session: str) -> None:
        self.tmux.nest_attach(pane_id, session)
        self.wait_until(lambda: self.tmux.display(pane_id, "#{pane_current_command}") == "tmux")

    # ----- T-TMUX-01 new-session defaults ------------------------------------------------------

    def test_t_tmux_01_new_session_defaults(self):
        result = self.tx(["spawn", "s1", "--tag", "t", "--cwd", str(self.workdir), "--cmd", "sleep 30"])
        self.assertEqual((result.code, result.out, result.err), (0, f"Spawned 's1' (cwd={self.workdir}, tag=t)\n", ""))
        record = json.loads(self.tx(["show", "s1"]).out)
        self.assertIn(record["id"], self.tmux.sessions())
        self.assertEqual(self.tmux.display(record["id"], "#{pane_current_path}"), str(self.workdir))
        self.assertEqual(_start_command(self, record["id"]), "sleep 30")
        environment = self.tmux.environment(record["id"])
        self.assertEqual(environment["COLORTERM"], "truecolor")
        self.assertEqual(environment["TERM"], "xterm-256color")
        self.assertEqual(environment["TX_SESSION_ID"], record["id"])
        self.assertEqual(record["pid"], int(self.tmux.display(record["id"], "#{pane_pid}")))
        tail = self.log_tail()[0]
        self.assertEqual((tail["type"], tail["msg"]), ("spawn", f"s1 [other] {self.workdir}"))

    def test_t_tmux_01_env_overrides_truecolor_default(self):
        record = self.spawn_process("s1", cmd="sleep 30", cwd=self.workdir, extra=("--env", "TERM=screen", "--env", "FOO=bar"))
        environment = self.tmux.environment(record["id"])
        self.assertEqual(environment["TERM"], "screen")
        self.assertEqual(environment["FOO"], "bar")
        self.assertEqual(environment["COLORTERM"], "truecolor")

    def test_t_tmux_01_nonexistent_cwd_falls_back_to_home(self):
        """Q21 PARITY, gated on the tmux version (spec): tmux 3.4 tolerates a bad `-c` (the pane
        starts in the server's `$HOME`), so the argv-level TmuxError never fires — verified on 3.4
        only. On a newer tmux the tolerance is unverified (the changelog is silent), so the leg
        accepts either documented outcome, each asserted in full: the fallback, or the refusal
        surfacing as `tx spawn: tmux new-session … failed: …` with nothing persisted."""
        result = self.tx(["spawn", "s1", "--tag", "t", "--cwd", "/nope", "--cmd", "sleep 30"])
        tolerant = tmux_version() < (3, 5) or result.code == 0
        if tolerant:
            self.assertEqual((result.code, result.out, result.err), (0, "Spawned 's1' (cwd=/nope, tag=t)\n", ""))
            record = json.loads(self.tx(["show", "s1"]).out)
            self.assertEqual(record["cwd"], "/nope")
            self.assertEqual(self.tmux.display(record["id"], "#{pane_current_path}"), str(self.home.user_home))
            self.assertEqual([line["msg"] for line in self.log_lines()], ["s1 [other] /nope"])
            return
        self.assertEqual((result.code, result.out), (1, ""))
        self.assertRegex(result.err, r"^tx spawn: tmux new-session .* failed: .+\n$")
        self.assertEqual(list(self.home.sessions_dir.iterdir()), [])
        self.assertEqual(self.log_lines(), [])
        self.assertEqual(self.tmux.sessions(), [])

    def test_t_tmux_01_command_exits_before_tx_id_stamp(self):
        """With `s1` live the server survives the dead pane and tmux reports `no such session`;
        were it the server's only session, tmux would say `no server running on <socket>`."""
        keeper = self.spawn_process("s1", cmd="sleep 30", cwd=self.workdir)
        result = self.tx(["spawn", "d", "--tag", "t", "--cwd", str(self.workdir), "--cmd", "/bin/true"])
        self.assertEqual((result.code, result.out), (1, ""))
        self.assertRegex(result.err, SET_OPTION_DEAD)
        self.assertEqual([path.name for path in self.home.sessions_dir.iterdir()], [f"{keeper['id']}.json"])
        self.assertEqual([line["msg"] for line in self.log_lines()], [f"s1 [other] {self.workdir}"])
        self.assertEqual(self.tmux.sessions(), [keeper["id"]])

    # ----- T-TMUX-02 has-session exact match ---------------------------------------------------

    def test_t_tmux_02_has_session_exact_match(self):
        self.spawn_view("work2", cwd=self.workdir)
        killed = self.tx(["kill", "work"])
        self.assertEqual((killed.code, killed.out, killed.err), (1, "", NOT_FOUND.format(name="work")))
        self.assertTrue(self.tmux.has_session("work2"))
        view = self.tx(["spawn-view", "work", "--cwd", str(self.workdir)])
        self.assertEqual((view.code, view.out), (0, f"Spawned view 'work' (cwd={self.workdir})\n"))
        self.assertEqual(sorted(self.tmux.sessions()), ["work", "work2"])
        prefix = self.tx(["kill", "wor"])
        self.assertEqual((prefix.code, prefix.err), (1, NOT_FOUND.format(name="wor")))
        self.assertEqual(sorted(self.tmux.sessions()), ["work", "work2"])
        self.assertEqual([line["type"] for line in self.log_lines()], ["spawn-view", "spawn-view"])

    def test_t_tmux_02_no_server(self):
        # The kit's server is always up (`exit-empty off`): take it down so "no server" is real.
        self.tmux.run("kill-server", check=True)
        self.assertNotEqual(self.tmux.run("list-sessions").returncode, 0)
        killed = self.tx(["kill", "work"])
        self.assertEqual((killed.code, killed.out, killed.err), (1, "", NOT_FOUND.format(name="work")))
        self.assertNotIn("Traceback", killed.err)
        self.assertEqual(self.log_lines(), [])
        view = self.tx(["spawn-view", "work", "--cwd", str(self.workdir)])
        self.assertEqual((view.code, view.out, view.err), (0, f"Spawned view 'work' (cwd={self.workdir})\n", ""))
        self.assertEqual(self.tmux.sessions(), ["work"])
        self.assertEqual([line["type"] for line in self.log_lines()], ["spawn-view"])

    # ----- T-TMUX-03 kill-session quiet; no tmux rename ----------------------------------------

    def test_t_tmux_03_kill_quiet_twice(self):
        record = self.spawn_process("a", cmd="sleep 30", cwd=self.workdir)
        self.spawn_view("V", cwd=self.workdir)
        for _ in range(2):
            killed = self.tx(["kill", "a"])
            self.assertEqual((killed.code, killed.out, killed.err), (0, "Killed 'a'\n", ""))
        self.assertNotIn(record["id"], self.tmux.sessions())
        stored = self.records.load(record["id"])
        self.assertEqual(stored["state"], "exited")
        self.assertIsInstance(stored["ended_at"], float)
        kills = [line for line in self.log_lines() if line["type"] == "kill"]
        self.assertEqual([line["msg"] for line in kills], ["a", "a"])

    def test_t_tmux_03_kill_view_then_not_found(self):
        self.spawn_view("V", cwd=self.workdir)
        killed = self.tx(["kill", "V"])
        self.assertEqual((killed.code, killed.out), (0, "Killed 'V'\n"))
        self.assertFalse(self.tmux.has_session("V"))
        tail = self.log_tail()[0]
        self.assertEqual((tail["type"], tail["msg"]), ("kill", "V"))
        again = self.tx(["kill", "V"])
        self.assertEqual((again.code, again.err), (1, NOT_FOUND.format(name="V")))
        self.assertEqual(len(self.log_lines()), 2)

    def test_t_tmux_03_rename_never_touches_tmux(self):
        record = self.spawn_process("a", cmd="sleep 30", cwd=self.workdir)
        renamed = self.tx(["rename", "a", "b"])
        self.assertEqual((renamed.code, renamed.out), (0, "Renamed to 'b'\n"))
        self.assertEqual(self.records.load(record["id"])["name"], "b")
        self.assertIn(record["id"], self.tmux.sessions())
        self.assertNotIn("b", self.tmux.sessions())
        self.assertEqual(self.tmux.option(record["id"], "@tx_id"), record["id"])

    # ----- T-TMUX-04 list-sessions is the liveness scan ----------------------------------------

    def test_t_tmux_04_list_sessions_liveness_scan(self):
        self.spawn_process("alpha", cwd=self.workdir)
        record_b = self.spawn_process("beta", cwd=self.workdir)
        first = self.tx(["ls"])
        self.assertEqual(first.code, 0, first.err)
        self.assertEqual({name: state for name, (state, _) in _ls_rows(first.out).items()}, {"alpha": "alive", "beta": "alive"})
        self.tmux.kill_session(record_b["id"])
        second = self.tx(["ls"])
        self.assertEqual(list(_ls_rows(second.out)), ["alpha"])
        stored = self.records.load(record_b["id"])
        self.assertEqual(stored["state"], "exited")
        self.assertIsInstance(stored["ended_at"], float)
        self.assertEqual(stored["attached_to"], [])
        tail = self.log_tail()[0]
        self.assertEqual((tail["type"], tail["msg"]), ("reconcile", "beta → exited (vanished)"))

    def test_t_tmux_04_no_server_marks_all_exited(self):
        record_a = self.spawn_process("alpha", cwd=self.workdir)
        record_b = self.spawn_process("beta", cwd=self.workdir)
        self.tmux.run("kill-server")
        listing = self.tx(["ls"])
        self.assertEqual((listing.code, listing.out, listing.err), (0, "PROCESSES\n", ""))
        for record in (record_a, record_b):
            self.assertEqual(self.records.load(record["id"])["state"], "exited")
        reconciles = [line["msg"] for line in self.log_lines() if line["type"] == "reconcile"]
        self.assertEqual(sorted(reconciles), ["alpha → exited (vanished)", "beta → exited (vanished)"])

    # ----- T-TMUX-05 send-keys: envelope verbatim, then Enter ----------------------------------

    def test_t_tmux_05_send_keys_envelope_then_enter(self):
        record = self.spawn_process("a", cmd="cat", cwd=self.workdir)
        sender = self.records.llm(name="s")
        envelope = '<from-user session="s">Enter</from-user>'
        result = self.tx(["send-user-message", "a", "Enter"], env={"TX_SESSION_ID": sender})
        self.assertEqual((result.code, result.out, result.err), (0, "", ""))
        self.wait_until(lambda: _lines(self.tmux.capture(record["id"])).count(envelope) == 2)
        self.assertEqual(_lines(self.tmux.capture(record["id"])), [envelope, envelope])
        tail = self.log_tail()[0]
        self.assertEqual((tail["actor"], tail["type"], tail["msg"]), (sender, "send-user-message", "→ a"))

        flag_body = '<from-user session="s">--help</from-user>'
        result = self.tx(["send-user-message", "a", "--", "--help"], env={"TX_SESSION_ID": sender})
        self.assertEqual((result.code, result.out, result.err), (0, "", ""))
        self.wait_until(lambda: _lines(self.tmux.capture(record["id"])).count(flag_body) == 2)
        self.assertEqual(_lines(self.tmux.capture(record["id"])), [envelope, envelope, flag_body, flag_body])

    def test_t_tmux_05_send_message_outside_tmux(self):
        record = self.spawn_process("a", cmd="cat", cwd=self.workdir)
        result = self.tx(["send-message", "a", "hi"])
        self.assertEqual(
            (result.code, result.out, result.err),
            (1, "", "tx send-message: send-message must run inside tmux (needs the sender session name)\n"),
        )
        self.assertEqual(_lines(self.tmux.capture(record["id"])), [])
        self.assertEqual([line["type"] for line in self.log_lines()], ["spawn"])

    # ----- T-TMUX-06 options get/set/unset -----------------------------------------------------

    def test_t_tmux_06_options_through_verbs(self):
        pane = self._view("Views")
        record = self.spawn_process("a", cwd=self.workdir)
        self.assertEqual(self.tmux.option("Views", "status"), "on")
        self.assertEqual(self.tmux.option("Views", "pane-border-status", "window"), "top")
        self.assertEqual(self.tmux.option("Views", "@tx_view"), "1")
        self.assertEqual(self.tmux.option(record["id"], "@tx_id"), record["id"])
        dump = self.root / "ssh-remote"
        self.fakes.write_script(
            "ssh", f'#!/bin/sh\ntmux show-options -pqv -t "$TMUX_PANE" @remote-session >{dump}\nexit 7\n'
        )
        result = self.tx(["attach", "--host", "h1"], env={"TMUX_PANE": pane})
        self.assertEqual(result.code, 7, result.err)
        self.assertEqual(dump.read_text(), "h1\n")
        after = self.tmux.run("show-options", "-pqv", "-t", pane, "@remote-session")
        self.assertEqual((after.returncode, after.stdout), (0, ""))

    def test_t_tmux_06_show_option_missing_target_quiet(self):
        result = self.tx(["kill", "nosuch"])
        self.assertEqual((result.code, result.err), (1, NOT_FOUND.format(name="nosuch")))
        self.assertNotIn("Traceback", result.err)

    def test_t_tmux_06_unset_option_dead_target_never_raises(self):
        pane = self._view("Views")
        self.fakes.write_script("ssh", '#!/bin/sh\ntmux kill-pane -t "$TMUX_PANE"\nexit 7\n')
        result = self.tx(["attach", "--host", "h1"], env={"TMUX_PANE": pane})
        self.assertEqual(result.code, 7, result.err)
        self.assertNotIn("Traceback", result.err)
        self.assertFalse(self.tmux.has_session("Views"))

    def test_t_tmux_06_set_option_dead_target_raises(self):
        self._view("Views")
        result = self.tx(["spawn", "d", "--tag", "t", "--cmd", "/bin/true"])
        self.assertEqual(result.code, 1)
        self.assertRegex(result.err, SET_OPTION_DEAD)

    # ----- T-TMUX-07 @tx_id / @tx_view / is_view -----------------------------------------------

    def test_t_tmux_07_tx_id_and_tx_view_markers(self):
        record = self.spawn_process("a", cmd="sleep 30", cwd=self.workdir)
        self.spawn_view("V", cwd=self.workdir)
        self.assertEqual(self.tmux.option(record["id"], "@tx_id"), record["id"])
        name = self.tx(["_tmux-name", "a"])
        self.assertEqual((name.code, name.out), (0, f"{record['id']}\n"))
        self.assertEqual(self.tmux.option("V", "@tx_view"), "1")
        self.assertIsNone(self.tmux.option("V", "@tx_id"))
        killed = self.tx(["kill", "V"])
        self.assertEqual((killed.code, killed.out), (0, "Killed 'V'\n"))

    def test_t_tmux_07_raw_session_with_tx_id_resolves_record(self):
        record = self.spawn_process("a", cmd="sleep 30", cwd=self.workdir)
        self.tmux.run("new-session", "-d", "-s", "raw", "sleep 300", check=True)
        self.tmux.run("set-option", "-t", "raw", "@tx_id", record["id"], check=True)
        self.tmux.run("new-session", "-d", "-s", "untracked", "sleep 300", check=True)
        killed = self.tx(["kill", "raw"])
        self.assertEqual((killed.code, killed.out), (0, "Killed 'a'\n"))
        self.assertNotIn(record["id"], self.tmux.sessions())
        self.assertTrue(self.tmux.has_session("raw"))
        self.assertEqual(self.records.load(record["id"])["state"], "exited")
        name = self.tx(["_tmux-name", "untracked"])
        self.assertEqual((name.code, name.out, name.err), (0, "", ""))

    def test_t_tmux_07_tx_view_only_literal_one_counts(self):
        self.tmux.run("new-session", "-d", "-s", "p", "sleep 300", check=True)
        for value in ("yes", "0"):
            self.tmux.run("set-option", "-t", "p", "@tx_view", value, check=True)
            killed = self.tx(["kill", "p"])
            self.assertEqual((killed.code, killed.err), (1, NOT_FOUND.format(name="p")), value)
        self.tmux.run("set-option", "-t", "p", "-u", "@tx_view", check=True)
        killed = self.tx(["kill", "p"])
        self.assertEqual((killed.code, killed.err), (1, NOT_FOUND.format(name="p")))
        self.assertTrue(self.tmux.has_session("p"))
        self.assertEqual(self.log_lines(), [])

    # ----- T-TMUX-08 display-message with / without a target -----------------------------------

    @expected_failure_on_python
    def test_t_tmux_07_fixed_short_hex_name_never_prefix_matches(self):
        """FIX (decided 2026-09-23): `show_option` targets `=name`, so a hex display name can no
        longer prefix-match another uuid-named session. Reference: `show-options -vqt b @tx_id`
        prefix-matches the single live session whose uuid starts with `b`, and `tx show b` /
        `_tmux-name b` / `kill b` act on THAT record instead of the record named `b`."""
        stranger = "b0000000-0000-4000-8000-000000000001"
        self.records.other(id=stranger, name="stranger")
        self.tmux.new_session(stranger, "sleep 300", tx_id=stranger)
        wanted = "c0000000-0000-4000-8000-000000000002"
        self.records.other(id=wanted, name="b")
        self.tmux.new_session(wanted, "sleep 300", tx_id=wanted)
        self.assertEqual(json.loads(self.tx(["show", "b"]).out)["id"], wanted)
        self.assertEqual(self.tx(["_tmux-name", "b"]).out, f"{wanted}\n")
        killed = self.tx(["kill", "b"])
        self.assertEqual((killed.code, killed.out), (0, "Killed 'b'\n"), killed.err)
        self.assertFalse(self.tmux.has_session(wanted))
        self.assertTrue(self.tmux.has_session(stranger))
        self.assertEqual(self.records.load(stranger)["state"], "alive")

    def test_t_tmux_08_display_message_targeted_and_untargeted(self):
        pane = self._view("Views")
        self.tmux.run("rename-window", "-t", "Views:0", "win", check=True)
        envelope = self.tx(["focus-envelope", pane])
        self.assertEqual(envelope.code, 0, envelope.err)
        self.assertIn("window-name='win'", envelope.out)
        self.assertIn(f"pane-id='{pane}'", envelope.out)
        inside = self.run_in_pane(pane, "tx whoami")
        self.assertEqual((inside.code, inside.out, inside.err), (0, "Views\n", ""))
        outside = self.tx(["whoami"])
        self.assertEqual((outside.code, outside.out, outside.err), (1, "", "tx whoami: not inside a tmux session\n"))

    def test_t_tmux_08_pane_gone(self):
        """3.4: the reference prints an envelope with every pane attr empty; a port may print ``."""
        self._view("Views")
        gone = self.tx(["focus-envelope", "%999"])
        self.assertEqual((gone.code, gone.err), (0, ""))
        self.assertFalse(gone.raw_out.endswith("\n"))
        self.assertNotIn("inner-session-name", gone.out)
        self.assertNotIn("session-kind", gone.out)
        self.tmux.run("kill-server")
        no_server = self.tx(["focus-envelope", "%0"])
        self.assertEqual((no_server.code, no_server.err), (0, ""))
        self.assertFalse(no_server.raw_out.endswith("\n"))
        self.assertNotIn("inner-session-name", no_server.out)
        self.assertNotIn("session-kind", no_server.out)

    # ----- T-TMUX-09 current session name gated on $TMUX ---------------------------------------

    def test_t_tmux_09_whoami_gated_on_tmux_env(self):
        self.spawn_view("stranger", cwd=self.workdir)
        who = self.tx(["whoami"])
        self.assertEqual((who.code, who.out, who.err), (1, "", "tx whoami: not inside a tmux session\n"))
        kid = self.spawn_process("kid", cmd="sleep 30", cwd=self.workdir)
        self.assertIsNone(kid["parent"])

    def test_t_tmux_09_whoami_inside_view_and_process_panes(self):
        home_pane = self._view("home")
        process = self.spawn_process("x", cmd="bash", cwd=self.workdir)
        in_view = self.run_in_pane(home_pane, "tx whoami")
        self.assertEqual((in_view.code, in_view.out), (0, "home\n"))
        in_process = self.run_in_pane(process["id"], "tx whoami")
        self.assertEqual((in_process.code, in_process.out), (0, "x\n"))

    def test_t_tmux_09_pane_path_not_gated(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        # outside tmux with NO server (the kit's is taken down first): the caller's cwd
        self.tmux.run("kill-server", check=True)
        self.assertNotEqual(self.tmux.run("list-sessions").returncode, 0)
        no_server = self.tx(["spawn", "n0", "--tag", "t", "--cmd", "sleep 30"], cwd=elsewhere)
        self.assertEqual(no_server.code, 0, no_server.err)
        self.assertEqual(json.loads(self.tx(["show", "n0"]).out)["cwd"], str(elsewhere))
        # inside a pane whose path is the view's cwd
        home_pane = self._view("home")
        inside = self.run_in_pane(home_pane, "tx spawn n --tag t --cmd 'sleep 30'")
        self.assertEqual(inside.code, 0, inside.err)
        self.assertEqual(json.loads(self.tx(["show", "n"]).out)["cwd"], str(self.workdir))

    @expected_failure_on_python
    def test_t_tmux_09_fixed_cwd_outside_tmux_with_live_server(self):
        """Q20 FIX: outside tmux, a live server's current pane path never wins over the caller's
        cwd (the reference takes the server pane path)."""
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        self._view("home")
        result = self.tx(["spawn", "n", "--tag", "t", "--cmd", "sleep 30"], cwd=elsewhere)
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(json.loads(self.tx(["show", "n"]).out)["cwd"], str(elsewhere))

    # ----- T-TMUX-10 attachment join -----------------------------------------------------------

    def test_t_tmux_10_attachment_join(self):
        pane = self._view("Views")
        self.tmux.run("rename-window", "-t", "Views:0", "w0", check=True)
        record = self.spawn_process("U", cwd=self.workdir)
        self._nest(pane, record["id"])
        shown = json.loads(self.tx(["show", "U"]).out)
        self.assertEqual(
            shown["attached_to"],
            [{"host": "Views", "window_index": "0", "window_name": "w0", "pane_id": pane, "pane_index": "0"}],
        )
        self.assertEqual(_ls_rows(self.tx(["ls"]).out)["U"], ("alive", "w0[0]"))

    def test_t_tmux_10_self_loop_guard(self):
        pane = self._view("Alt")
        self._nest(pane, "Alt")
        envelope = self.tx(["focus-envelope", pane])
        self.assertEqual(envelope.code, 0, envelope.err)
        self.assertIn("pane-cmd='tmux'", envelope.out)
        self.assertNotIn("inner-session-name", envelope.out)

    def test_t_tmux_10_outer_client_changes_nothing(self):
        pane = self._view("Views")
        self.tmux.run("rename-window", "-t", "Views:0", "w0", check=True)
        record = self.spawn_process("U", cwd=self.workdir)
        self._nest(pane, record["id"])
        before = json.loads(self.tx(["show", "U"]).out)["attached_to"]
        client = self.attach_client("Views")
        self.assertNotIn(client.tty, [row["pane_tty"] for row in self.tmux.panes()])
        self.assertEqual(json.loads(self.tx(["show", "U"]).out)["attached_to"], before)
        self.assertEqual(_ls_rows(self.tx(["ls"]).out)["U"], ("alive", "w0[0]"))

    # ----- T-TMUX-11 attachment order ----------------------------------------------------------

    def test_t_tmux_11_attachment_order(self):
        self._view("Views")
        self._view("Alt")
        record = self.spawn_process("U", cwd=self.workdir)
        # Kit panes (non-login bash): a command-less split's login shell could reorder PATH ahead
        # of the wrapper and aim the nested `TMUX= tmux attach` at the operator's server (D15).
        self.tmux.new_window("Views:10")
        self.tmux.split_window("Views:10")
        self.tmux.split_window("Views:10")
        self.tmux.new_window("Views:2")
        self.tmux.run("rename-window", "-t", "Views:10", "w10", check=True)
        self.tmux.run("rename-window", "-t", "Views:2", "w2", check=True)
        self.tmux.run("rename-window", "-t", "Alt:0", "alt0", check=True)
        for target in ("Views:10.2", "Views:2.0", "Alt:0.0"):
            self._nest(self.tmux.pane_id(target), record["id"])
        attached = json.loads(self.tx(["show", "U"]).out)["attached_to"]
        self.assertEqual(
            [(location["host"], location["window_index"], location["pane_index"]) for location in attached],
            [("Alt", "0", "0"), ("Views", "2", "0"), ("Views", "10", "2")],
        )
        self.assertEqual([location["window_name"] for location in attached], ["alt0", "w2", "w10"])
        self.assertEqual(_ls_rows(self.tx(["ls"]).out)["U"], ("alive", "alt0[0] +2"))

    def test_t_tmux_11_attached_nowhere(self):
        self._view("Views")
        self.spawn_process("W", cwd=self.workdir)
        self.assertEqual(json.loads(self.tx(["show", "W"]).out)["attached_to"], [])
        self.assertEqual(_ls_rows(self.tx(["ls"]).out)["W"], ("alive", "—"))

    # ----- T-TMUX-12 jump target -----------------------------------------------------------------

    def _nested_in_views_and_alt(self) -> dict:
        """`U` nested in `Views:0.1` and `Alt:0.0`; `Views:1` (a bash pane) active."""
        self._view("Views")
        self._view("Alt")
        record = self.spawn_process("U", cwd=self.workdir)
        self.tmux.split_window("Views:0")
        self._nest(self.tmux.pane_id("Views:0.1"), record["id"])
        self._nest(self.tmux.pane_id("Alt:0.0"), record["id"])
        self.tmux.new_window("Views:1")
        return record

    def test_t_tmux_12_jump_prefers_current_view(self):
        record = self._nested_in_views_and_alt()
        picker_pane = self.tmux.pane_id("Views:1")
        self.assertEqual(self.tmux.display("Views", "#{window_index}.#{pane_index}"), "1.0")
        _fzf_accepts(self, "U", record["id"])
        result = self.run_in_pane(picker_pane, "tx attach --jump -f U")
        self.assertEqual((result.code, result.err), (0, ""))
        self.assertEqual(self.tmux.display("Views", "#{window_index}.#{pane_index}"), "0.1")
        self.assertEqual(self.tmux.display("Alt", "#{window_index}.#{pane_index}"), "0.0")

    def test_t_tmux_12_jump_from_other_host_takes_first_sorted(self):
        record = self._nested_in_views_and_alt()
        picker_pane = self.tmux.new_window("Alt:1")
        self.assertEqual(self.tmux.display("Alt", "#{window_index}.#{pane_index}"), "1.0")
        _fzf_accepts(self, "U", record["id"])
        result = self.run_in_pane(picker_pane, "tx attach --jump -f U")
        self.assertEqual((result.code, result.err), (0, ""))
        self.assertEqual(self.tmux.display("Alt", "#{window_index}.#{pane_index}"), "0.0")
        self.assertEqual(self.tmux.display("Views", "#{window_index}.#{pane_index}"), "1.0")

    def test_t_tmux_12_jump_remote_session_pane_fallback(self):
        self._view("Views")
        record = self.spawn_process("U", cwd=self.workdir)
        self.tmux.new_window("Views:1")
        for _ in range(3):
            self.tmux.split_window("Views:1")
        remote_pane = self.tmux.pane_id("Views:1.3")
        self.tmux.run("set-option", "-p", "-t", remote_pane, "@remote-session", record["id"], check=True)
        picker_pane = self.tmux.new_window("Views:2")
        _fzf_accepts(self, "U", record["id"])
        result = self.run_in_pane(picker_pane, "tx attach --jump -f U")
        self.assertEqual((result.code, result.err), (0, ""))
        self.assertEqual(self.tmux.display("Views", "#{window_index}.#{pane_index}"), "1.3")

    def test_t_tmux_12_jump_nowhere_reports_and_rerenders(self):
        picker_pane = self._view("Views")
        record = self.spawn_process("U", cwd=self.workdir)
        _fzf_accepts(self, "U", record["id"])
        result = self.run_in_pane(picker_pane, "tx attach --jump -f U")
        self.assertEqual((result.code, result.err), (0, JUMP_FAILED))
        self.assertEqual(len(self.fakes.dumps("fzf")), 2)
        self.assertEqual(self.tmux.display("Views", "#{window_index}.#{pane_index}"), "0.0")

    # ----- T-TMUX-13 inner session for a pane --------------------------------------------------

    def test_t_tmux_13_inner_session_for_pane(self):
        nested_pane = self._view("Views")
        record = self.spawn_process("U", cwd=self.workdir)
        self._nest(nested_pane, record["id"])
        plain_pane = self.tmux.split_window("Views:0")
        nested = self.tx(["focus-envelope", nested_pane])
        self.assertIn(f"inner-session-name='{record['id']}'", nested.out)
        plain = self.tx(["focus-envelope", plain_pane])
        self.assertEqual(plain.code, 0, plain.err)
        self.assertNotIn("inner-session-name", plain.out)
        edit = self.tx(["_edit-session", plain_pane])
        self.assertEqual(
            (edit.code, edit.out, edit.err),
            (1, "", "tx _edit-session: no tx session in this pane (view homes cannot be edited)\n"),
        )

    def test_t_tmux_13_untracked_session_pane(self):
        self.tmux.run("new-session", "-d", "-s", "plain", "bash", check=True)
        pane = self.tmux.pane_id("plain")
        edit = self.tx(["_edit-session", pane])
        self.assertEqual((edit.code, edit.err), (1, "tx _edit-session: the session in this pane is not tx-managed\n"))

    # ----- T-TMUX-15 focus-envelope attribute order --------------------------------------------

    def _titled_nested_pane(self) -> tuple[str, dict]:
        pane = self._view("Views")
        self.tmux.run("rename-window", "-t", "Views:0", "w0", check=True)
        record = self.spawn_process("U", tag="t1,t2", cwd=self.workdir)
        self._nest(pane, record["id"])
        self.tmux.run("select-pane", "-T", "t", "-t", pane, check=True)
        return pane, record

    def test_t_tmux_15_focus_envelope_attribute_order(self):
        pane, record = self._titled_nested_pane()
        result = self.tx(["focus-envelope", pane])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(
            result.raw_out,
            f"<tx-command-prompt session-name='Views' window-index='0' window-name='w0' pane-id='{pane}' "
            f"pane-index='0' pane-title='t' pane-cmd='tmux' pane-path='{self.workdir}' "
            f"inner-session-name='{record['id']}' session-kind='view' inner-session-kind='process' "
            f"inner-session-tag='t1,t2'/>",
        )

    def _remote_envelope(self, pane: str) -> str:
        return (
            f"<tx-command-prompt session-name='Views' window-index='0' window-name='w0' pane-id='{pane}' "
            f"pane-index='0' pane-title='t' pane-cmd='tmux' pane-path='{self.workdir}' "
            f"inner-remote='1' inner-session-name='host1' session-kind='view'/>"
        )

    @python_reference_only
    def test_t_tmux_15_parity_remote_session_read_at_session_scope(self):
        """Q30 parity twin (D17): the reference reads `@remote-session` without `-p`, so the host
        SESSION's option drives the `inner-remote` shape and the pane-scoped stamp of
        `tx attach --host` is not seen. A Q30-fixed port reads pane scope only (rev 5), so this leg
        and `…_15_fixed_…` are mutually exclusive — reference-only."""
        pane, record = self._titled_nested_pane()
        self.tmux.run("set-option", "-p", "-t", pane, "@remote-session", "host1", check=True)
        pane_scoped = self.tx(["focus-envelope", pane])
        self.assertIn(f"inner-session-name='{record['id']}'", pane_scoped.out)
        self.assertNotIn("inner-remote", pane_scoped.out)
        self.tmux.run("set-option", "-p", "-t", pane, "-u", "@remote-session", check=True)
        self.tmux.run("set-option", "-t", "Views", "@remote-session", "host1", check=True)
        session_scoped = self.tx(["focus-envelope", pane])
        self.assertEqual(session_scoped.raw_out, self._remote_envelope(pane))

    @expected_failure_on_python
    def test_t_tmux_15_fixed_remote_session_pane_option(self):
        """The spec's leg: the PANE option `@remote-session` (what `tx attach --host` stamps with
        `-p`) yields the `inner-remote` shape with no record join."""
        pane, _ = self._titled_nested_pane()
        self.tmux.run("set-option", "-p", "-t", pane, "@remote-session", "host1", check=True)
        result = self.tx(["focus-envelope", pane])
        self.assertEqual(result.raw_out, self._remote_envelope(pane))

    def test_t_tmux_15_process_host_and_no_nesting(self):
        view_pane = self._view("Views")
        plain = self.tx(["focus-envelope", view_pane])
        self.assertTrue(plain.raw_out.endswith("pane-cmd='bash' pane-path='" + str(self.workdir) + "' session-kind='view'/>"), plain.out)
        self.assertNotIn("inner-", plain.out)
        record = self.spawn_process("x", tag="a,b", cmd="bash", cwd=self.workdir)
        process_pane = self.tmux.pane_id(record["id"])
        result = self.tx(["focus-envelope", process_pane])
        self.assertTrue(result.raw_out.startswith(f"<tx-command-prompt session-name='{record['id']}' "), result.out)
        self.assertTrue(result.raw_out.endswith("session-kind='process' session-tag='a,b'/>"), result.out)
        self.assertNotIn("inner-", result.out)

    # ----- T-TMUX-16 envelope XML escaping -----------------------------------------------------

    def test_t_tmux_16_xml_escaping(self):
        first = self._view("Views")
        second = self.tmux.split_window("Views:0")
        self.tmux.run("select-pane", "-t", first, "-T", "it's <b>&x", check=True)
        self.tmux.run("select-pane", "-t", second, "-T", 'a"b', check=True)
        escaped = self.tx(["focus-envelope", first])
        self.assertEqual((escaped.code, escaped.err), (0, ""))
        self.assertIn("pane-title='it&apos;s &lt;b&gt;&amp;x'", escaped.out)
        self.assertTrue(escaped.raw_out.endswith("/>"))
        quoted = self.tx(["focus-envelope", second])
        self.assertEqual((quoted.code, quoted.err), (0, ""))
        self.assertIn("pane-title='a\"b'", quoted.out)
        self.assertTrue(quoted.raw_out.endswith("/>"))
        gone = self.tx(["focus-envelope", "%gone"])
        self.assertEqual(gone.code, 0, gone.err)
        self.assertFalse(gone.raw_out.endswith("\n"))

    # ----- T-TMUX-17 foreground attach from outside tmux ---------------------------------------

    def test_t_tmux_17_foreground_attach_from_outside_tmux(self):
        record = self.spawn_process("U", cwd=self.workdir)
        argv_dump = self.root / "attach-argv"
        tmux_dump = self.root / "attach-tmux"
        # A recording `tmux` AHEAD of the kit wrapper (never overwriting it): `attach` is recorded
        # and refused with 3, anything else is handed to the kit wrapper (still the private socket).
        recorder_dir = self.root / "attach-recorder"
        recorder_dir.mkdir()
        (recorder_dir / "tmux").write_text(
            "#!/bin/sh\n"
            f'if [ "$1" = attach ]; then printf \'%s\\n\' "$@" >{argv_dump}; printf \'%s\' "${{TMUX-<unset>}}" >{tmux_dump}; exit 3; fi\n'
            f'exec "{self.tmux.bin_dir / "tmux"}" "$@"\n'
        )
        (recorder_dir / "tmux").chmod(0o755)
        _fzf_accepts(self, "U", record["id"])
        picker = self.tx_pty(["attach", "-f", "U"], env={"PATH": f"{recorder_dir}{os.pathsep}{self.env()['PATH']}"})
        self.assertEqual(picker.wait(), 0, picker.output)
        self.assertEqual(argv_dump.read_text(), f"attach\n-t\n{record['id']}\n")
        self.assertEqual(tmux_dump.read_text(), "<unset>")
        self.assertEqual(len(self.fakes.dumps("fzf")), 1)

    # ----- T-TMUX-18 respawn-pane -k nest-attach -----------------------------------------------

    def test_t_tmux_18_respawn_pane_nest_attach(self):
        pane = self._view("Views")
        record = self.spawn_process("U", cwd=self.workdir)
        client = self.attach_client("Views")
        old_pid = int(self.tmux.display(pane, "#{pane_pid}"))
        _fzf_accepts(self, "U", record["id"])
        # `display-popup -E` blocks the invoking tmux until the popup closes: run it detached with
        # a bounded wait, so a port that hangs in the picker fails the case instead of the suite.
        popup = subprocess.Popen(
            [REAL_TMUX, "-L", self.tmux.socket, "-f", "/dev/null", "display-popup", "-E", "-c", client.tty,
             "-t", "Views", "tx attach -f U"],
            env=self.env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.addCleanup(popup.kill)
        self.assertEqual(popup.wait(timeout=15), 0)
        self.wait_until(lambda: int(self.tmux.display(pane, "#{pane_pid}")) != old_pid)
        self.assertEqual(
            _start_command(self, pane),
            f"bash -c 'set -m; TMUX= tmux attach -t {record['id']}; exec ${{SHELL:-zsh}}'",
        )
        self.wait_until(lambda: self.tmux.display(pane, "#{pane_current_command}") == "tmux")
        self.wait_until(lambda: not _pid_alive(old_pid))
        pane_tty = self.tmux.pane_tty(pane)
        self.assertIn(
            (pane_tty, record["id"]),
            [(row["client_tty"], row["client_session"]) for row in self.tmux.clients()],
        )
        attached = json.loads(self.tx(["show", "U"]).out)["attached_to"]
        self.assertEqual(
            [(location["host"], location["window_index"], location["pane_index"], location["pane_id"]) for location in attached],
            [("Views", "0", "0", pane)],
        )
        client.read(0.2)

    def test_t_tmux_18_in_pane_without_popup_skips_respawn(self):
        pane = self._view("Views")
        record = self.spawn_process("U", cwd=self.workdir)
        before = self.tmux.display(pane, "#{pane_start_command}")
        old_pid = int(self.tmux.display(pane, "#{pane_pid}"))
        _fzf_accepts(self, "U", record["id"])
        result = self.run_in_pane(pane, "tx attach -f U")
        self.assertEqual((result.code, result.err), (0, ""))
        self.assertEqual(self.tmux.display(pane, "#{pane_start_command}"), before)
        self.assertEqual(int(self.tmux.display(pane, "#{pane_pid}")), old_pid)
        self.assertEqual(self.tmux.clients(), [])
        self.assertEqual(json.loads(self.tx(["show", "U"]).out)["attached_to"], [])

    # ----- T-TMUX-19 select-window / select-pane / switch-client fallthrough --------------------

    def test_t_tmux_19_select_window_and_pane(self):
        pane = self._view("Views")
        record = self.spawn_process("U", cwd=self.workdir)
        self._nest(pane, record["id"])
        picker_pane = self.tmux.new_window("Views:1")
        self.assertEqual(self.tmux.display("Views", "#{window_index}"), "1")
        _fzf_accepts(self, "U", record["id"])
        result = self.run_in_pane(picker_pane, "tx attach --jump -f U")
        self.assertEqual((result.code, result.err), (0, ""))
        self.assertEqual(self.tmux.display("Views", "#{window_index}.#{pane_index}"), "0.0")

    def test_t_tmux_19_switch_client_failure_tolerated(self):
        picker_pane = self._view("Views")
        record = self.spawn_process("U", cwd=self.workdir)
        _fzf_accepts(self, "U", record["id"])
        result = self.run_in_pane(picker_pane, "tx attach --jump -f U")
        self.assertEqual((result.code, result.err), (0, JUMP_FAILED))
        self.assertNotIn("Traceback", result.err)
        self.assertEqual(len(self.fakes.dumps("fzf")), 2)
        self.assertEqual(self.tmux.clients(), [])

    def test_t_tmux_19_start_switch_client_failure_surfaces(self):
        """`tx start` warms the assistant through `bin/tx-assistant --warm`, which would spawn a
        worker with `--cwd <its own checkout>` — a worktree of the REAL repo. Two guards: the
        pre-seeded live `tx-assistant` record (the reference resolves the helper repo-relative and
        skips the warm-up when the record is live), and an inert `tx-assistant` first on PATH for a
        port that resolves the helper through PATH. Both leave the stdout the spec states."""
        process = self.spawn_process("X", cmd="bash", cwd=self.workdir)
        assistant = self.records.llm(name="tx-assistant", tags=("tx-system",))
        self.tmux.new_session(assistant, "sleep 300", tx_id=assistant)
        stub_dir = self.root / "assistant-stub"
        stub_dir.mkdir()
        stub = stub_dir / "tx-assistant"
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
        self.tmux.type_line(process["id"], f"export PATH={stub_dir}{os.pathsep}$PATH")
        result = self.run_in_pane(process["id"], "tx start")
        self.assertEqual(result.code, 1)
        self.assertEqual(result.out, "tx-assistant already running.\nViews session created.\n")
        self.assertRegex(result.err, r"^tx start: tmux switch-client -t Views failed: .+\n$")
        self.assertTrue(self.tmux.has_session("Views"))
        self.assertEqual(self.tmux.option("Views", "@tx_view"), "1")
        self.assertEqual(
            sorted(entry.name for entry in self.home.sessions_dir.iterdir()),
            sorted([f"{process['id']}.json", f"{assistant}.json"]),
        )
        self.assertEqual([line["type"] for line in self.log_lines()], ["spawn", "spawn-view"])

    # ----- T-TMUX-20 command-size threshold ----------------------------------------------------

    def test_t_tmux_20_command_size_threshold(self):
        prefix = "sleep 30 #"
        small = prefix + "x" * (8192 - len(prefix.encode()))
        big = small + "x"
        self.assertEqual((len(small.encode()), len(big.encode())), (8192, 8193))
        record_small = self.spawn_process("small", cmd=small, cwd=self.workdir)
        record_big = self.spawn_process("big", cmd=big, cwd=self.workdir)
        self.assertEqual(_start_command(self, record_small["id"]), small)
        self.assertFalse((self.home.launch_dir / f"{record_small['id']}.sh").exists())
        script = self.home.launch_dir / f"{record_big['id']}.sh"
        self.assertEqual(_start_command(self, record_big["id"]), f"/bin/sh {script}")
        self.assertTrue(script.exists())
        self.assertEqual(script.read_text(), big + "\n")
