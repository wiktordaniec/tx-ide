"""One green test per kit layer against the binary under test (`TX_BIN`, default `bin/tx`).

Not spec cases — these prove the fixtures work end to end: CLI, home, tmux, fake engine, hook,
golden round-trip, artifact create. Area tests (`test_<area>.py`) are Phase 3.
"""

from __future__ import annotations

import json
import os
import re

import subprocess
import tempfile
from pathlib import Path

from txkit import HOME_DIRS, TMUX_SOCKET_ENV, KitSafetyError, TxCase, resolved_home, run_tx, scrubbed_env


class TestSmokeSafety(TxCase):
    def test_tmux_wrapper_is_passthrough_without_socket_env(self):
        self.tmux.new_session("probe", "sleep 30")
        wrapper = str(self.tmux.bin_dir / "tmux")
        # Drop the test process's own tmux client vars: `$TMUX` would otherwise pick its socket.
        base_env = {
            key: value for key, value in os.environ.items() if key not in (TMUX_SOCKET_ENV, "TMUX", "TMUX_PANE")
        }
        # With the env var: the wrapper reaches the private server.
        with_socket = subprocess.run(
            [wrapper, "has-session", "-t", "=probe"], env={**base_env, TMUX_SOCKET_ENV: self.tmux.socket}
        )
        self.assertEqual(with_socket.returncode, 0)
        # Without it: plain tmux, aimed (via TMUX_TMPDIR) at a place with no server at all.
        with tempfile.TemporaryDirectory() as empty_tmpdir:
            without_socket = subprocess.run(
                [wrapper, "has-session", "-t", "=probe"],
                env={**base_env, "TMUX_TMPDIR": empty_tmpdir},
                capture_output=True,
                text=True,
            )
        self.assertNotEqual(without_socket.returncode, 0)
        # tmux tried the DEFAULT socket under the empty TMUX_TMPDIR — no -L was injected.
        self.assertIn(f"{empty_tmpdir}/tmux-", without_socket.stderr)
        self.assertIn("/default", without_socket.stderr)

    def test_run_tx_refuses_a_home_that_resolves_outside_the_kit_root(self):
        real_home = str(Path.home() / ".tx-ide")
        with self.assertRaises(KitSafetyError):
            self.tx(["help"], env={"TX_IDE_HOME": real_home})
        # The `$HOME/.tx-ide` default is refused whenever HOME is not the temp user home …
        with self.assertRaises(KitSafetyError):
            self.tx(["help"], env={"TX_IDE_HOME": None, "HOME": str(Path.home())})
        with self.assertRaises(KitSafetyError):
            self.tx(["help"], env={"TX_IDE_HOME": None, "HOME": None})
        # … and `~` expands against the env's HOME, not the process owner's.
        with self.assertRaises(KitSafetyError):
            self.tx(["help"], env={"TX_IDE_HOME": "~/x", "HOME": str(Path.home())})
        with self.assertRaises(KitSafetyError):
            self.tx(["help"], env={"TX_IDE_HOME": f"~{os.environ.get('USER', 'root')}/x"})
        self.assertFalse(self.home.log_path.exists())
        # Allowed: the default and a `~/` path while HOME is the temp user home (T-HOME-01).
        self.assertEqual(resolved_home(self.env({"TX_IDE_HOME": None})), self.home.user_home / ".tx-ide")
        self.assertEqual(resolved_home(self.env({"TX_IDE_HOME": "~/foo"})), self.home.user_home / "foo")

    def test_no_server_fixture_means_a_dead_private_socket(self):
        env = scrubbed_env(self.home, fakes=self.fakes)
        socket = env[TMUX_SOCKET_ENV]
        self.assertTrue(socket.startswith("txkit-dead-"))
        self.assertNotEqual(socket, self.tmux.socket)
        self.assertTrue(env["PATH"].startswith(str(self.home.root / "tmux-bin") + os.pathsep))
        # `tmux` resolved through that PATH meets an empty server, never the operator's.
        probe = subprocess.run(["tmux", "list-sessions"], env=env, capture_output=True, text=True)
        self.assertNotEqual(probe.returncode, 0)
        self.assertEqual(probe.stdout, "")
        self.assertIn(f"/{socket}", probe.stderr)  # "error connecting to …/<socket>" / "no server running on …"
        result = run_tx(["help"], home=self.home, tmux=None, fakes=self.fakes)
        self.assertEqual(result.code, 0, result.err)


class TestSmokeCli(TxCase):
    def test_help(self):
        result = self.tx(["help"])
        self.assertEqual(result.code, 0, result.err)
        self.assertTrue(result.out.startswith("tx — tmux + Claude Code session controller\n\nusage: tx <command> [args]\n"))
        self.assertIn("\n  spawn  ", result.out)
        self.assertEqual(result.err, "")

    def test_golden_round_trip(self):
        result = self.tx(["help"])
        self.assert_golden("smoke/01", result.out)


class TestSmokeHome(TxCase):
    home_options = {"skeleton": False, "link_agents": False, "hooks": ()}

    def test_init_home(self):
        self.assertFalse(self.home.path.exists())
        result = self.tx(["_init-home"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, f"initialized $TX_IDE_HOME skeleton at {self.home.path}\n")
        self.assertEqual(self.home.entries(), sorted(HOME_DIRS))


class TestSmokeTmux(TxCase):
    def test_spawn_shell_session(self):
        workdir = self.root / "w"
        workdir.mkdir()
        result = self.tx(["spawn", "sh1", "--tag", "t", "--cwd", str(workdir), "--cmd", "sleep 30"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, f"Spawned 'sh1' (cwd={workdir}, tag=t)\n")
        record = json.loads(self.tx(["show", "sh1"]).out)
        self.assertEqual(record["role"], "other")
        self.assertIn(record["id"], self.tmux.sessions())
        self.assertEqual(self.tmux.option(record["id"], "@tx_id"), record["id"])
        self.assertEqual(self.tmux.environment(record["id"])["TX_SESSION_ID"], record["id"])
        self.assertIn("sleep 30", self.tmux.display(record["id"], "#{pane_start_command}"))
        self.assertEqual(self.log_tail()[0]["type"], "spawn")


class TestSmokeFakeEngine(TxCase):
    def test_worker_runs_fake_claude(self):
        result = self.tx(["spawn", "w1", "--tag", "t", "--cwd", str(self.git.path), "--engine", "claude"])
        self.assertEqual(result.code, 0, result.err)
        record = json.loads(self.tx(["show", "w1"]).out)
        dump = self.fakes.wait_dump("claude", record["id"])
        self.assertEqual(os.path.basename(dump["argv"][0]), "claude")
        self.assertEqual(dump["argv"][dump["argv"].index("--effort") + 1], "high")
        self.assertEqual(dump["env"]["TX_SESSION_ID"], record["id"])
        self.assertEqual(dump["env"]["TX_REQUIRE_WORKTREE"], "1")
        self.assertEqual(os.path.realpath(dump["cwd"]), os.path.realpath(record["cwd"]))
        self.assertTrue(record["cwd"].startswith(str(self.home.worktrees_dir)))
        self.assertIn(os.path.realpath(record["cwd"]), [os.path.realpath(path) for path in self.git.worktrees()])
        self.assertIn(record["id"], self.tmux.sessions())


class TestSmokeHook(TxCase):
    def test_prompt_submit_flips_crafted_record_to_working(self):
        session_id = self.records.llm(name="r1", state="idle")
        payload = json.dumps({"session_id": "c1", "transcript_path": "/t/c1.jsonl"})
        result = self.tx(["hook", "prompt-submit"], env={"TX_SESSION_ID": session_id}, stdin=payload)
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, "")
        record = self.records.load(session_id)
        self.assertEqual(record["state"], "working")
        self.assertEqual(record["chats"][0]["id"], "c1")
        self.assertEqual(record["chats"][0]["transcript_path"], "/t/c1.jsonl")
        self.assertIsNotNone(record["turn_started_at"])
        tail = self.log_tail()[0]
        self.assertEqual((tail["actor"], tail["type"], tail["msg"]), (session_id, "state", "r1 → working"))


class TestSmokeArtifact(TxCase):
    def test_create(self):
        source = self.root / "plan.md"
        source.write_text("# plan\n")
        result = self.tx(["artifact", "create", str(source), "--title", "Plan"])
        self.assertEqual(result.code, 0, result.err)
        match = re.fullmatch(r"Created artifact ([0-9a-f-]{36}) \(plan\.md\)\n", result.out)
        self.assertIsNotNone(match, result.out)
        artifact_id = match.group(1)
        record = json.loads(self.records.artifact_record_path(artifact_id).read_text())
        self.assertEqual(record["artifact_schema_version"], 2)
        self.assertEqual((record["title"], record["filename"], record["group"]), ("Plan", "plan.md", None))
        self.assertEqual([touch["rev"] for touch in record["history"]], [0])
        directory = self.records.artifact_dir(artifact_id)
        self.assertEqual((directory / "revs" / "0.md").read_text(), "# plan\n")
        self.assertEqual((directory / "current.md").read_text(), "# plan\n")
        tail = self.log_tail()[0]
        self.assertEqual((tail["actor"], tail["type"]), ("user", "artifact-create"))
