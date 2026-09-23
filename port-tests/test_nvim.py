"""NVIM — the tx ↔ nvim contract (spec §NVIM, T-NVIM-01..20).

`tx spawn-nvim` / `tx artifact open` launch nvim companions as `OtherSession` records; nvim's Lua
reaches back through `tx whoami`, `tx send-user-message`, `tx tag`, `tmux list-sessions -F
'#{@tx_id}'` + `sessions/<id>.json`, `bin/tmux-nav`, and `nvim --remote-expr` tours.

"Inside session W" is expressed on the `tx` subprocess as `TMUX=<socket_path>,<pid>,0` +
`TMUX_PANE=<W's pane id>` (tmux resolves `#S` through `TMUX_PANE` for an unattached command
client) — see `inside()`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from txkit import (
    REPO,
    TxCase,
    expected_failure_on_python,
    requires_bin,
    scrubbed_env,
)

NVIM_BASE_COMMAND = "nvim +'set background=dark | colorscheme tokyonight-moon'"
TMUX_NAV = REPO / "bin" / "tmux-nav"
APPLY_TOUR = REPO / "agents" / "skills" / "tx-code-tours" / "scripts" / "apply_tour.py"
TOUR_MAPPINGS = ("]n", "[n", "<leader>cn", "<leader>cN", "<leader>cl")
DIAGNOSTIC_COUNT = 'luaeval("#vim.diagnostic.get(nil,{namespace=vim.api.nvim_create_namespace(\'tx_code_tour\')})")'
DIAGNOSTIC_ROWS = (
    'json_encode(map(luaeval("vim.diagnostic.get(nil,{namespace=vim.api.nvim_create_namespace(\'tx_code_tour\')})"), '
    "{_, v -> [v.lnum, v.col, v.severity, v.source, v.user_data.tour_label]}))"
)
QUICKFIX_ROWS = "json_encode(map(getqflist(), {_, v -> [bufname(v.bufnr), v.lnum, v.col, v.text]}))"


class TestNvim(TxCase):
    # ----- helpers -------------------------------------------------------------------------

    def show(self, name: str) -> dict:
        return json.loads(self.tx(["show", name]).out)

    def inside(self, session_id: str, *, tx_session_id: bool = True) -> dict[str, str]:
        """Env that puts a `tx` subprocess inside tx session `session_id`'s pane."""
        env = {
            "TMUX": f"{self.tmux.socket_path},{os.getpid()},0",
            "TMUX_PANE": self.tmux.display(session_id, "#{pane_id}"),
        }
        if tx_session_id:
            env["TX_SESSION_ID"] = session_id
        return env

    def spawn_worker(self, name: str = "worker-1", tags: str = "scope,p1") -> dict:
        """A live llm worker (fake claude) in a linked worktree of the git fixture."""
        result = self.tx(["spawn", name, "--tag", tags, "--cwd", str(self.git.path), "--engine", "claude"])
        self.assertEqual(result.code, 0, result.err)
        record = self.show(name)
        self.fakes.wait_dump("claude", record["id"])
        return record

    def spawn_nvim(self, *argv: str, env: dict[str, str] | None = None):
        result = self.tx(["spawn-nvim", *argv], env=env)
        self.assertEqual(result.code, 0, result.err)
        return result

    def sessions_files(self) -> set[str]:
        return {path.name for path in self.home.sessions_dir.iterdir()}

    # ----- T-NVIM-01 -----------------------------------------------------------------------

    def test_t_nvim_01_help_flag_set(self):
        result = self.tx(["spawn-nvim", "--help"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.err, "")
        self.assertTrue(result.out.startswith("usage: tx spawn-nvim "), result.out)
        usage = result.out.split("\n\n")[0]
        for flag in ("--tag TAG", "[--group GROUP]", "[--cwd CWD]", "[--diff [DIFF]]", "[--open OPEN]", "[--env ENV]"):
            self.assertIn(flag, usage)
        self.assertRegex(usage, r"\bname$")
        self.assertIn("positional arguments:\n  name\n", result.out)

    def test_t_nvim_01_argument_errors(self):
        for argv, message in (
            (["spawn-nvim", "a"], "the following arguments are required: --tag"),
            (["spawn-nvim", "a", "--tag", ""], "--tag requires at least one value"),
            (["spawn-nvim", "a", "--tag", ","], "--tag requires at least one value"),
            (["spawn-nvim", "a", "--tag", "s", "--group", ""], "argument --group: a group cannot be empty"),
            (["spawn-nvim", "a", "--tag", "s", "--env", "NOEQ"], "argument --env: --env expects KEY=VALUE, got 'NOEQ'"),
        ):
            with self.subTest(argv=argv):
                result = self.tx(argv)
                self.assertEqual(result.code, 2)
                self.assertEqual(result.out, "")
                self.assertTrue(result.err.startswith("usage: tx spawn-nvim "), result.err)
                self.assertTrue(result.err.endswith(f"tx spawn-nvim: error: {message}\n"), result.err)
        self.assertEqual(self.sessions_files(), set())

    # ----- T-NVIM-02 -----------------------------------------------------------------------

    def test_t_nvim_02_launch_tmux_session_env_and_fake_argv(self):
        directory = self.root / "w"
        directory.mkdir()
        result = self.spawn_nvim("ed", "--tag", "scope", "--cwd", str(directory))
        self.assertEqual(result.out, f"Spawned nvim 'ed' (cwd={directory}, tag=scope)\n")
        record = self.show("ed")
        session_id = record["id"]
        self.assertTrue(self.records.path(session_id).exists())
        self.assertEqual(record["cmd"], NVIM_BASE_COMMAND)
        self.assertIn(session_id, self.tmux.sessions())
        self.assertEqual(self.tmux.option(session_id, "@tx_id"), session_id)
        self.assertEqual(self.tmux.display(session_id, "#{pane_current_path}"), str(directory))
        environment = self.tmux.environment(session_id)
        self.assertEqual(environment["COLORTERM"], "truecolor")
        self.assertEqual(environment["TERM"], "xterm-256color")
        self.assertEqual(environment["TX_SESSION_ID"], session_id)
        dump = self.fakes.wait_dump("nvim", session_id)
        self.assertEqual(dump["argv"][1:], ["+set background=dark | colorscheme tokyonight-moon"])
        self.assertEqual(dump["env"]["TX_SESSION_ID"], session_id)
        self.assertEqual(dump["env"]["COLORTERM"], "truecolor")
        self.assertEqual(record["pid"], int(self.tmux.display(session_id, "#{pane_pid}")))

    # ----- T-NVIM-03 -----------------------------------------------------------------------

    def test_t_nvim_03_diff_default_main_and_explicit_base(self):
        result = self.spawn_nvim("d", "--tag", "s", "--diff")
        record = self.show("d")
        self.assertEqual(record["cmd"], NVIM_BASE_COMMAND + " +'DiffviewOpen main'")
        self.assertEqual(self.fakes.wait_dump("nvim", record["id"])["argv"][1:], ["+set background=dark | colorscheme tokyonight-moon", "+DiffviewOpen main"])
        self.assertEqual(result.out, f"Spawned nvim 'd' (cwd={record['cwd']}, tag=s, diff=main)\n")

        result = self.spawn_nvim("d2", "--tag", "s", "--diff", "abc123")
        record = self.show("d2")
        self.assertEqual(record["cmd"], NVIM_BASE_COMMAND + " +'DiffviewOpen abc123'")
        self.assertEqual(self.fakes.wait_dump("nvim", record["id"])["argv"][-1], "+DiffviewOpen abc123")
        self.assertEqual(result.out, f"Spawned nvim 'd2' (cwd={record['cwd']}, tag=s, diff=abc123)\n")

        result = self.spawn_nvim("d3", "--tag", "s")
        record = self.show("d3")
        self.assertEqual(record["cmd"], NVIM_BASE_COMMAND)
        self.assertNotIn("DiffviewOpen", " ".join(self.fakes.wait_dump("nvim", record["id"])["argv"]))
        self.assertEqual(result.out, f"Spawned nvim 'd3' (cwd={record['cwd']}, tag=s)\n")

    # ----- T-NVIM-04 -----------------------------------------------------------------------

    def test_t_nvim_04_open_file_quoting(self):
        plan = self.root / "p" / "my plan (v2).md"
        plan.parent.mkdir()
        plan.write_text("# plan\n")
        result = self.spawn_nvim("v", "--tag", "s", "--open", str(plan), "--diff")
        record = self.show("v")
        self.assertEqual(record["cmd"], f"{NVIM_BASE_COMMAND} +'DiffviewOpen main' '{plan}'")
        self.assertEqual(self.fakes.wait_dump("nvim", record["id"])["argv"][-1], str(plan))
        self.assertEqual(result.out, f"Spawned nvim 'v' (cwd={record['cwd']}, tag=s, diff=main, open={plan})\n")

    # ----- T-NVIM-05 -----------------------------------------------------------------------

    def test_t_nvim_05_record_shape_inside_a_session(self):
        worker = self.spawn_worker()
        directory = self.root / "d"
        directory.mkdir()
        before = self.sessions_files()
        self.spawn_nvim("ed", "--tag", "a,b", "--group", "g1", "--env", "A=1", "--cwd", str(directory), env=self.inside(worker["id"]))
        (new_file,) = self.sessions_files() - before
        record = json.loads((self.home.sessions_dir / new_file).read_text())
        self.assertEqual(new_file, f"{record['id']}.json")
        self.assertIsInstance(record["pid"], int)
        self.assertIsInstance(record["created_at"], float)
        self.assertEqual(
            record,
            {
                "schema_version": 6,
                "id": record["id"],
                "name": "ed",
                "role": "nvim",
                "state": "alive",
                "cwd": str(directory),
                "cmd": NVIM_BASE_COMMAND,
                "tags": ["a", "b"],
                "group": "g1",
                "env": {"A": "1"},
                "parent": worker["id"],
                "pid": record["pid"],
                "attached_to": [],
                "created_at": record["created_at"],
                "ended_at": None,
                "artifact_id": None,
            },
        )
        tail = self.log_tail()[0]
        self.assertEqual((tail["type"], tail["msg"]), ("spawn", f"ed [nvim] {directory}"))

    def test_t_nvim_05_parent_null_without_tmux(self):
        self.spawn_worker()
        self.spawn_nvim("ed", "--tag", "a", "--cwd", str(self.root))
        self.assertIsNone(self.show("ed")["parent"])

    def test_t_nvim_05_default_cwd_is_calling_pane_path(self):
        worker = self.spawn_worker()
        self.spawn_nvim("ed", "--tag", "a", env=self.inside(worker["id"]))
        self.assertEqual(self.show("ed")["cwd"], self.tmux.display(worker["id"], "#{pane_current_path}"))
        self.assertEqual(self.show("ed")["cwd"], worker["cwd"])

    def test_t_nvim_05_default_cwd_is_own_cwd_in_plain_terminal(self):
        # No live sessions at all, so there is no pane path to take: the process cwd wins.
        directory = self.root / "here"
        directory.mkdir()
        result = self.tx(["spawn-nvim", "ed", "--tag", "a"], cwd=directory)
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self.show("ed")["cwd"], str(directory))
        self.assertIsNone(self.show("ed")["parent"])

    # ----- T-NVIM-06 -----------------------------------------------------------------------

    def test_t_nvim_06_role_inferred_nvim_from_bare_spawn(self):
        for name, command in (("e", "nvim README.md"), ("e2", str(self.fakes.bin_dir / "nvim"))):
            with self.subTest(command=command):
                result = self.tx(["spawn", name, "--tag", "s", "--cmd", command])
                self.assertEqual(result.code, 0, result.err)
                shown = self.tx(["show", name])
                record = json.loads(shown.out)
                self.assertEqual(record, self.records.load(record["id"]))
                self.assertEqual(record["role"], "nvim")
                self.assertEqual(record["state"], "alive")
                self.assertNotIn("engine", record)
                self.assertIsNone(record["artifact_id"])
                self.assertIn('"role": "nvim"', shown.out)
                tail = self.log_tail()[0]
                self.assertEqual((tail["type"], tail["msg"]), ("spawn", f"{name} [nvim] {self.root}"))
        listing = self.tx(["ls"]).out
        self.assertTrue(listing.startswith("PROCESSES\n"))
        self.assertRegex(listing, r"\n  e +alive ")

    def test_t_nvim_06_role_other_and_shell_from_basename(self):
        # The command must outlive the spawn: a vanishing pane takes the tmux session (and the
        # `set-option @tx_id` that precedes the record write) with it. `nvim-qt` is a stand-in
        # sleeper on PATH; `zsh` is the kit's long-running fake.
        shutil.copy(self.fakes.bin_dir / "zsh", self.fakes.bin_dir / "nvim-qt")
        result = self.tx(["spawn", "q", "--tag", "s", "--cmd", "nvim-qt"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self.show("q")["role"], "other")
        self.assertEqual(self.log_tail()[0]["msg"], f"q [other] {self.root}")
        result = self.tx(["spawn", "z", "--tag", "s", "--cmd", "zsh"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self.show("z")["role"], "shell")
        self.assertEqual(self.log_tail()[0]["msg"], f"z [shell] {self.root}")

    # ----- T-NVIM-07 -----------------------------------------------------------------------

    def test_t_nvim_07_name_collision(self):
        self.spawn_nvim("ed", "--tag", "s")
        sessions = self.tmux.sessions()
        files = self.sessions_files()
        log_length = len(self.log_lines())
        result = self.tx(["spawn-nvim", "ed", "--tag", "s"])
        self.assertEqual(result.code, 1)
        self.assertEqual(result.out, "")
        self.assertEqual(result.err, "tx spawn-nvim: session 'ed' already exists\n")
        self.assertEqual(self.tmux.sessions(), sessions)
        self.assertEqual(self.sessions_files(), files)
        self.assertEqual(len(self.log_lines()), log_length)

    def test_t_nvim_07_bad_cwd_is_tolerated_by_tmux(self):
        result = self.tx(["spawn-nvim", "bad", "--tag", "s", "--cwd", "/nonexistent"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, "Spawned nvim 'bad' (cwd=/nonexistent, tag=s)\n")
        record = self.show("bad")
        self.assertEqual(record["cwd"], "/nonexistent")
        server_home = self.tmux.run("show-environment", "-g", "HOME", check=True).stdout.strip().partition("=")[2]
        self.assertEqual(self.tmux.display(record["id"], "#{pane_current_path}"), server_home)

    # ----- T-NVIM-08 -----------------------------------------------------------------------

    ARTIFACT_ID = "0123abcd-0000-4000-8000-000000000001"

    def test_t_nvim_08_artifact_open_binds_artifact_id(self):
        worker = self.spawn_worker(tags="scope")
        artifact_id = self.records.artifact(id=self.ARTIFACT_ID)
        history_before = json.loads(self.records.artifact_record_path(artifact_id).read_text())["history"]
        content_dir = self.records.artifact_dir(artifact_id)
        result = self.tx(["artifact", "open", "0123abcd"], env=self.inside(worker["id"]))
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, f"Opened artifact {artifact_id} in nvim view 'art-0123abcd' ({content_dir}/current.md)\n")
        record = self.show("art-0123abcd")
        self.assertEqual(record["name"], "art-0123abcd")
        self.assertEqual(record["tags"], ["scope"])
        self.assertEqual(record["cwd"], str(content_dir))
        self.assertEqual(record["cmd"], f"{NVIM_BASE_COMMAND} {content_dir}/current.md")
        self.assertEqual(record["artifact_id"], artifact_id)
        self.assertEqual(record["role"], "nvim")
        self.assertEqual(self.fakes.wait_dump("nvim", record["id"])["argv"][-1], f"{content_dir}/current.md")
        tail = self.log_tail(3)
        self.assertEqual(
            [(line["type"], line["msg"], line["actor"]) for line in tail],
            [
                ("spawn", f"art-0123abcd [nvim] {content_dir}", worker["id"]),
                ("bind-artifact", f"art-0123abcd → {artifact_id}", worker["id"]),
                ("artifact-open", f"{artifact_id} → {worker['id']}", worker["id"]),
            ],
        )
        self.assertEqual(json.loads(self.records.artifact_record_path(artifact_id).read_text())["history"], history_before)

    def test_t_nvim_08_tag_override_and_empty_tag(self):
        worker = self.spawn_worker(tags="scope")
        self.records.artifact(id=self.ARTIFACT_ID)
        result = self.tx(["artifact", "open", "0123abcd", "--tag", "x,y"], env=self.inside(worker["id"]))
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self.show("art-0123abcd")["tags"], ["x", "y"])
        result = self.tx(["artifact", "open", "0123abcd", "--tag", ""], env=self.inside(worker["id"]))
        self.assertEqual(result.code, 2)
        self.assertTrue(result.err.endswith("tx artifact open: error: --tag requires at least one value\n"), result.err)

    def test_t_nvim_08_untagged_invoker_or_no_tmux_gets_artifact_tag(self):
        self.records.artifact(id=self.ARTIFACT_ID)
        untagged = self.records.llm(name="bare", tags=())
        self.tmux.new_session(untagged, "sleep 300", tx_id=untagged)
        result = self.tx(["artifact", "open", "0123abcd"], env=self.inside(untagged))
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self.show("art-0123abcd")["tags"], ["artifact"])
        self.tx(["kill", "art-0123abcd"])
        worker = self.spawn_worker(tags="scope")
        result = self.tx(["artifact", "open", "0123abcd"], env={"TX_SESSION_ID": worker["id"]})
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self.show("art-0123abcd")["tags"], ["artifact"])

    def test_t_nvim_08_cwd_override(self):
        worker = self.spawn_worker(tags="scope")
        artifact_id = self.records.artifact(id=self.ARTIFACT_ID)
        directory = self.root / "elsewhere"
        directory.mkdir()
        result = self.tx(["artifact", "open", "0123abcd", "--cwd", str(directory)], env=self.inside(worker["id"]))
        self.assertEqual(result.code, 0, result.err)
        record = self.show("art-0123abcd")
        self.assertEqual(record["cwd"], str(directory))
        self.assertEqual(record["cmd"], f"{NVIM_BASE_COMMAND} {self.records.artifact_dir(artifact_id)}/current.md")

    def test_t_nvim_08_ambiguous_and_unknown_prefix(self):
        worker = self.spawn_worker(tags="scope")
        self.records.artifact(id=self.ARTIFACT_ID)
        self.records.artifact(id="0123abce-0000-4000-8000-000000000002")
        files = self.sessions_files()
        result = self.tx(["artifact", "open", "0123abc"], env=self.inside(worker["id"]))
        self.assertEqual(result.code, 1)
        self.assertEqual(result.err, "tx artifact: artifact id prefix '0123abc' is ambiguous (2 matches) — use more characters\n")
        result = self.tx(["artifact", "open", "zzz"], env=self.inside(worker["id"]))
        self.assertEqual(result.code, 1)
        self.assertEqual(result.err, "tx artifact: artifact 'zzz' not found\n")
        self.assertEqual(self.sessions_files(), files)

    # ----- T-NVIM-09 -----------------------------------------------------------------------

    def discovery_fixture(self) -> tuple[dict, dict]:
        worker = self.spawn_worker("L1", tags="scope")
        self.spawn_nvim("N1", "--tag", "scope")
        nvim = self.show("N1")
        result = self.tx(["spawn-view", "view1", "--cmd", "sleep 300"])
        self.assertEqual(result.code, 0, result.err)
        self.tmux.new_session("handmade", "sleep 300")
        return worker, nvim

    def test_t_nvim_09_parity_tx_id_lines(self):
        worker, nvim = self.discovery_fixture()
        output = self.tmux.run("list-sessions", "-F", "#{@tx_id}", check=True).stdout
        lines = output.split("\n")[:-1]
        self.assertEqual(len(lines), len(self.tmux.sessions()))
        self.assertEqual(sorted(lines), sorted([worker["id"], nvim["id"], "", ""]))
        # tmux lists sessions by name: the two tx uuids first, then `handmade` and `view1`.
        self.assertEqual(lines[-2:], ["", ""])

    @expected_failure_on_python
    def test_t_nvim_09_fixed_ls_json(self):
        worker, nvim = self.discovery_fixture()
        result = self.tx(["ls", "--json"])
        self.assertEqual(result.code, 0, result.err)
        rows = json.loads(result.out)
        self.assertEqual(sorted(row["id"] for row in rows), sorted([worker["id"], nvim["id"]]))
        for row in rows:
            self.assertEqual(row, self.records.load(row["id"]))
            self.assertEqual(row["schema_version"], 6)
            self.assertIsInstance(row["name"], str)
            self.assertIsInstance(row["tags"], list)
        self.assertEqual({row["id"]: row["role"] for row in rows}, {worker["id"]: "llm", nvim["id"]: "nvim"})
        listed = self.tx(["ls"]).out
        for row in rows:
            self.assertIn(f"\n  {row['name']} ", listed)
        # Read from $TX_IDE_HOME only: a record planted under $HOME/.tx-ide is not consulted.
        foreign = self.home.user_home / ".tx-ide" / "sessions"
        foreign.mkdir(parents=True)
        (foreign / "ffffffff-0000-4000-8000-000000000000.json").write_text(json.dumps({**self.records.load(worker["id"]), "id": "ffffffff-0000-4000-8000-000000000000", "name": "ghost"}))
        self.assertEqual({row["name"] for row in json.loads(self.tx(["ls", "--json"]).out)}, {"L1", "N1"})

    @expected_failure_on_python
    def test_t_nvim_09_fixed_ls_json_empty(self):
        result = self.tx(["ls", "--json"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(json.loads(result.out), [])

    # ----- T-NVIM-10 -----------------------------------------------------------------------

    BODY = 'why $x != "y"?'

    def message_target(self, name: str = "L1") -> dict:
        """A live llm target whose fake claude logs every raw tty chunk with a timestamp."""
        self.fakes.configure("claude", stdin_log=True)
        result = self.tx(["spawn", name, "--tag", "s", "--cmd", str(self.fakes.bin_dir / "claude"), "--cwd", str(self.git.path)])
        self.assertEqual(result.code, 0, result.err)
        record = self.show(name)
        self.fakes.wait_dump("claude", record["id"])
        return record

    def delivered(self, target_id: str, count: int = 2) -> list[dict]:
        return self.wait_until(lambda: (lambda chunks: chunks if len(chunks) >= count else None)(self.fakes.stdin_log("claude", target_id)))

    def assert_envelope_delivered(self, target: dict, sender: str) -> None:
        envelope = f'<from-user session="{sender}">{self.BODY}</from-user>'
        self.assertIn(envelope, self.tmux.capture(target["id"]))
        chunks = self.delivered(target["id"])
        text = "".join(chunk["data"] for chunk in chunks)
        self.assertEqual(text, envelope + "\r")
        enter = chunks[-1]
        self.assertEqual(enter["data"], "\r")
        last_envelope_chunk = chunks[-2]
        self.assertGreaterEqual(enter["at"] - last_envelope_chunk["at"], 0.3)

    def test_t_nvim_10_argv_form_and_envelope(self):
        target = self.message_target()
        origin = self.records.other(name="ed-diff", role="nvim")
        result = self.tx(["send-user-message", "L1", self.BODY], env={"TX_SESSION_ID": origin})
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, "")
        self.assert_envelope_delivered(target, "ed-diff")
        tail = self.log_tail()[0]
        self.assertEqual((tail["type"], tail["msg"], tail["actor"]), ("send-user-message", "→ L1", origin))

    def test_t_nvim_10_requires_tx_session_id(self):
        target = self.message_target()
        log_length = len(self.log_lines())
        result = self.tx(["send-user-message", "L1", self.BODY], env={"TX_SESSION_ID": None})
        self.assertEqual(result.code, 1)
        self.assertEqual(result.out, "")
        self.assertEqual(result.err, "tx send-user-message: send-user-message must run inside a tx session ($TX_SESSION_ID is unset)\n")
        time.sleep(0.5)
        self.assertEqual(self.fakes.stdin_log("claude", target["id"]), [])
        self.assertNotIn("from-user", self.tmux.capture(target["id"]))
        self.assertEqual(len(self.log_lines()), log_length)

    def test_t_nvim_10_raw_id_without_record_is_the_sender(self):
        target = self.message_target()
        raw = "no-such-record-1234"
        result = self.tx(["send-user-message", "L1", self.BODY], env={"TX_SESSION_ID": raw})
        self.assertEqual(result.code, 0, result.err)
        self.assert_envelope_delivered(target, raw)
        self.assertEqual(self.log_tail()[0]["actor"], raw)

    def test_t_nvim_10_unknown_or_dead_target(self):
        origin = self.records.other(name="ed-diff", role="nvim")
        self.records.other(name="gone", role="shell", state="alive")
        for target in ("L1", "gone"):
            with self.subTest(target=target):
                result = self.tx(["send-user-message", target, self.BODY], env={"TX_SESSION_ID": origin})
                self.assertEqual(result.code, 1)
                self.assertEqual(result.out, "")
                self.assertEqual(result.err, f"tx send-user-message: target session '{target}' does not exist\n")

    def test_t_nvim_10_target_by_id_and_by_tx_id_name(self):
        target = self.message_target()
        origin = self.records.other(name="ed-diff", role="nvim")
        for token in (target["id"], "L1"):
            with self.subTest(token=token):
                result = self.tx(["send-user-message", token, self.BODY], env={"TX_SESSION_ID": origin})
                self.assertEqual(result.code, 0, result.err)
        chunks = self.delivered(target["id"], count=4)
        text = "".join(chunk["data"] for chunk in chunks)
        envelope = f'<from-user session="ed-diff">{self.BODY}</from-user>\r'
        self.assertEqual(text, envelope * 2)

    def test_t_nvim_10_live_record_wins_over_exited_namesake(self):
        target = self.message_target()
        self.records.llm(name="L1", state="exited", created_at=time.time() + 10)
        origin = self.records.other(name="ed-diff", role="nvim")
        result = self.tx(["send-user-message", "L1", self.BODY], env={"TX_SESSION_ID": origin})
        self.assertEqual(result.code, 0, result.err)
        self.assert_envelope_delivered(target, "ed-diff")

    # ----- T-NVIM-11 -----------------------------------------------------------------------

    def test_t_nvim_11_whoami(self):
        worker = self.spawn_worker()
        result = self.tx(["whoami"], env=self.inside(worker["id"]))
        self.assertEqual((result.code, result.out, result.err), (0, "worker-1\n", ""))

    def test_t_nvim_11_whoami_outside_tmux(self):
        self.spawn_worker()
        result = self.tx(["whoami"])
        self.assertEqual((result.code, result.out, result.err), (1, "", "tx whoami: not inside a tmux session\n"))

    def test_t_nvim_11_whoami_untracked_session_prints_raw_name(self):
        self.tmux.new_session("rawsess", "sleep 300")
        result = self.tx(["whoami"], env=self.inside("rawsess", tx_session_id=False))
        self.assertEqual((result.code, result.out), (0, "rawsess\n"))
        result = self.tx(["spawn-view", "view1", "--cmd", "sleep 300"])
        self.assertEqual(result.code, 0, result.err)
        result = self.tx(["whoami"], env=self.inside("view1", tx_session_id=False))
        self.assertEqual((result.code, result.out), (0, "view1\n"))

    # ----- T-NVIM-12 -----------------------------------------------------------------------

    def test_t_nvim_12_tag_read_form(self):
        self.spawn_worker()
        result = self.tx(["tag", "worker-1"])
        self.assertEqual((result.code, result.out, result.err), (0, "scope,p1\n", ""))

    def test_t_nvim_12_tag_unknown_and_set(self):
        self.spawn_worker()
        result = self.tx(["tag", "x"])
        self.assertEqual((result.code, result.out, result.err), (1, "", "tx tag: session 'x' not found\n"))
        result = self.tx(["tag", "worker-1", "a,b"])
        self.assertEqual((result.code, result.out), (0, "Tagged 'worker-1' (tag=a,b)\n"))
        self.assertEqual(self.show("worker-1")["tags"], ["a", "b"])
        tail = self.log_tail()[0]
        self.assertEqual((tail["type"], tail["msg"]), ("tag", "worker-1 a,b"))

    # ----- T-NVIM-13 -----------------------------------------------------------------------

    def test_t_nvim_13_diff_review_recipe(self):
        self.git.with_origin_main()
        worker = self.spawn_worker()
        worktree = worker["cwd"]
        env = self.inside(worker["id"])
        self_name = self.tx(["whoami"], env=env).out.strip()
        tags = self.tx(["tag", self_name], env=env).out.strip()
        merge_base = self.git.git("merge-base", "origin/main", "HEAD", cwd=Path(worktree)).strip()
        result = self.tx(["spawn-nvim", f"{self_name}-diff", "--tag", tags, "--cwd", worktree, "--diff", merge_base], env=env)
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, f"Spawned nvim 'worker-1-diff' (cwd={worktree}, tag=scope,p1, diff={merge_base})\n")
        record = self.show("worker-1-diff")
        self.assertEqual(record["name"], "worker-1-diff")
        self.assertEqual(record["tags"], ["scope", "p1"])
        self.assertEqual(record["role"], "nvim")
        self.assertEqual(record["parent"], worker["id"])
        self.assertEqual(record["cwd"], worktree)
        self.assertTrue(record["cmd"].endswith(f" +'DiffviewOpen {merge_base}'"), record["cmd"])
        self.assertEqual(self.fakes.wait_dump("nvim", record["id"])["argv"][-1], f"+DiffviewOpen {merge_base}")
        listing = self.tx(["ls"]).out
        self.assertRegex(listing, r"\n  worker-1 +\S+ .*\[scope\] \[p1\]")
        self.assertRegex(listing, r"\n  worker-1-diff +\S+ .*\[scope\] \[p1\]")

    # ----- T-NVIM-14 -----------------------------------------------------------------------

    def tmux_nav(self, *argv: str, env: dict[str, str | None] | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(TMUX_NAV), *argv],
            capture_output=True,
            text=True,
            env=scrubbed_env(self.home, self.tmux, self.fakes, env),
        )

    def side_by_side(self, name: str = "S") -> tuple[str, str]:
        """A session with panes L (left) and R (right, active); returns their ids."""
        self.tmux.new_session(name, "sleep 300")
        left = self.tmux.display(name, "#{pane_id}")
        self.tmux.run("split-window", "-h", "-t", name, "sleep 300", check=True)
        right = self.tmux.display(name, "#{pane_id}")
        self.assertNotEqual(left, right)
        return left, right

    def active(self, pane: str) -> str:
        return self.tmux.display(pane, "#{pane_active}")

    def test_t_nvim_14_nav_from_nvim_without_client_tty(self):
        left, right = self.side_by_side()
        self.assertEqual((self.active(left), self.active(right)), ("0", "1"))
        completed = self.tmux_nav("L", right)
        self.assertEqual((completed.returncode, completed.stdout, completed.stderr), (0, "", ""))
        self.assertEqual((self.active(left), self.active(right)), ("1", "0"))

    def test_t_nvim_14_usage_error(self):
        left, right = self.side_by_side()
        completed = self.tmux_nav("X", right)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stderr, "usage: tmux-nav {L|D|U|R} [pane_id] [client_tty]\n")
        self.assertEqual((self.active(left), self.active(right)), ("0", "1"))

    def test_t_nvim_14_no_pane_and_no_tmux_pane(self):
        left, right = self.side_by_side()
        completed = self.tmux_nav("L", env={"TMUX_PANE": None})
        self.assertEqual(completed.returncode, 0)
        self.assertEqual((self.active(left), self.active(right)), ("0", "1"))

    def test_t_nvim_14_nav_keys_off(self):
        left, right = self.side_by_side()
        self.tmux.run("set", "-g", "@tx-ide-nav-keys", "off", check=True)
        completed = self.tmux_nav("L", right)
        self.assertEqual(completed.returncode, 0)
        self.assertEqual((self.active(left), self.active(right)), ("0", "1"))

    def test_t_nvim_14_top_level_edge(self):
        left, right = self.side_by_side()
        completed = self.tmux_nav("L", left)
        self.assertEqual(completed.returncode, 0)
        self.assertEqual((self.active(left), self.active(right)), ("0", "1"))

    # ----- T-NVIM-15 -----------------------------------------------------------------------

    def nested_fixture(self) -> tuple[str, str, str]:
        """Inner `I` (pane P3) hosted by outer `V`'s right pane P2 (`TMUX= tmux attach -t I`)."""
        socket = self.tmux.socket
        self.tmux.new_session("I", "sleep 300")
        inner_pane = self.tmux.display("I", "#{pane_id}")
        self.tmux.new_session("V", "sleep 300")
        left = self.tmux.display("V", "#{pane_id}")
        self.tmux.run("split-window", "-h", "-t", "V", f"TMUX= {shutil.which('tmux')} -L {socket} attach -t I", check=True)
        host = self.tmux.display("V", "#{pane_id}")
        self.wait_until(lambda: self.tmux.run("list-clients", "-t", "I", "-F", "#{client_tty}").stdout.strip())
        return left, host, inner_pane

    def v_panes(self) -> str:
        return self.tmux.run("list-panes", "-t", "V", "-F", "#{pane_id} #{pane_active}", check=True).stdout

    def test_t_nvim_15_bubbles_out_of_nested_session(self):
        left, host, inner_pane = self.nested_fixture()
        self.attach_client("V")
        self.assertEqual(self.v_panes(), f"{left} 0\n{host} 1\n")
        completed = self.tmux_nav("L", inner_pane)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.v_panes(), f"{left} 1\n{host} 0\n")
        self.assertEqual(self.tmux.run("list-panes", "-t", "I", "-F", "#{pane_id} #{pane_active}", check=True).stdout, f"{inner_pane} 1\n")

    def test_t_nvim_15_unattached_outer_session_is_not_a_host(self):
        left, host, inner_pane = self.nested_fixture()
        self.assertEqual(self.tmux.display("V", "#{session_attached}"), "0")
        completed = self.tmux_nav("L", inner_pane)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.v_panes(), f"{left} 0\n{host} 1\n")

    # ----- T-NVIM-16 -----------------------------------------------------------------------

    def real_nvim_env(self) -> dict[str, str]:
        """The scrubbed env after the caller took the fake `nvim` off PATH (`self.fakes.remove`)."""
        return scrubbed_env(self.home, self.tmux, self.fakes)

    def remote_expr(self, socket: str, expression: str) -> subprocess.CompletedProcess:
        return subprocess.run(["nvim", "--server", socket, "--remote-expr", expression], capture_output=True, text=True, env=self.real_nvim_env(), timeout=30)

    def stub_colorscheme(self) -> None:
        """A `tokyonight-moon` colorscheme under the temp HOME, so the real nvim's startup command
        does not stall on a hit-enter prompt (E185) that would block its RPC server."""
        colors = self.home.user_home / ".config" / "nvim" / "colors"
        colors.mkdir(parents=True)
        (colors / "tokyonight-moon.vim").write_text('let g:colors_name = "tokyonight-moon"\n')

    def background(self, argv: list[str], env: dict[str, str]) -> subprocess.Popen:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)

        def stop() -> None:
            process.kill()
            process.wait()

        self.addCleanup(stop)
        return process

    @requires_bin("nvim")
    @requires_bin("lsof")
    def test_t_nvim_16_socket_discovery(self):
        self.fakes.remove("nvim")
        self.stub_colorscheme()
        self.spawn_nvim("N1", "--tag", "s")
        record = self.show("N1")
        pane_pid = self.tmux.run("list-panes", "-st", record["id"], "-F", "#{pane_pid}", check=True).stdout.split()[0]

        def child() -> str | None:
            children = subprocess.run(["pgrep", "-P", pane_pid], capture_output=True, text=True).stdout.split()
            return children[0] if children else None

        nvim_pid = self.wait_until(child)
        self.assertEqual(Path(f"/proc/{nvim_pid}/comm").read_text().strip(), "nvim")

        def socket() -> str | None:
            # The skill's `awk '{print $NF}' | grep nvim` — widened to any field, since newer lsof
            # appends `type=STREAM` after the socket path.
            output = subprocess.run(["lsof", "-U", "-a", "-p", nvim_pid], capture_output=True, text=True).stdout
            candidates = [field for line in output.splitlines() for field in line.split() if "nvim" in field and field.startswith("/")]
            return candidates[0] if candidates else None

        socket_path = self.wait_until(socket)
        self.assertIn("nvim", socket_path)
        completed = self.remote_expr(socket_path, "1+1")
        self.assertEqual((completed.returncode, completed.stdout), (0, "2"), completed.stderr)

    # ----- T-NVIM-17..19 --------------------------------------------------------------------

    def headless_nvim(self) -> str:
        """A `nvim --headless --clean --listen <sock>` in the background; returns the socket path."""
        socket_path = str(self.root / "nvim.sock")
        self.fakes.remove("nvim")
        self.background(["nvim", "--headless", "--clean", "--listen", socket_path], self.real_nvim_env())
        self.wait_until(lambda: os.path.exists(socket_path))
        return socket_path

    def tour_fixture(self) -> tuple[Path, Path]:
        directory = self.root / "ct"
        directory.mkdir()
        text_file = directory / "f.txt"
        text_file.write_text("".join(f"line {number}\n" for number in range(1, 10)))
        stops = directory / "stops.lua"
        stops.write_text(
            "return {\n"
            f'  {{file="{text_file}", line=3, label="1/2 entry", message="first\\nmore", severity="INFO"}},\n'
            f'  {{file="{text_file}", line=7, label="2/2 exit", message="second", severity="WARN"}},\n'
            "}\n"
        )
        return text_file, stops

    def apply_tour(self, socket_path: str, stops: Path) -> subprocess.CompletedProcess:
        return subprocess.run(["python3", str(APPLY_TOUR), socket_path, str(stops)], capture_output=True, text=True, env=self.real_nvim_env(), timeout=60)

    def expr(self, socket_path: str, expression: str) -> str:
        completed = self.remote_expr(socket_path, expression)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout

    @requires_bin("nvim")
    def test_t_nvim_17_apply_tour_argv_and_result(self):
        socket_path = self.headless_nvim()
        _text_file, stops = self.tour_fixture()
        completed = self.apply_tour(socket_path, stops)
        self.assertEqual((completed.returncode, completed.stdout, completed.stderr), (0, "tour applied\n", ""))
        self.assertEqual(self.expr(socket_path, 'luaeval("_G.__tx_code_tour ~= nil")'), "true")

    @requires_bin("nvim")
    def test_t_nvim_17_socket_not_listening(self):
        self.fakes.remove("nvim")
        _text_file, stops = self.tour_fixture()
        completed = self.apply_tour(str(self.root / "nope.sock"), stops)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stdout, "")
        self.assertIn("Traceback", completed.stderr)
        self.assertIn("subprocess.CalledProcessError", completed.stderr)

    @requires_bin("nvim")
    def test_t_nvim_18_nvim_side_state_after_apply(self):
        socket_path = self.headless_nvim()
        text_file, stops = self.tour_fixture()
        self.assertEqual(self.apply_tour(socket_path, stops).stdout, "tour applied\n")
        self.assertEqual(self.expr(socket_path, DIAGNOSTIC_COUNT), "2")
        self.assertEqual(json.loads(self.expr(socket_path, DIAGNOSTIC_ROWS)), [[2, 0, 3, "tour", "1/2 entry"], [6, 0, 2, "tour", "2/2 exit"]])
        self.assertEqual(self.expr(socket_path, "getqflist({'title': 1}).title"), "Code tour")
        self.assertEqual(json.loads(self.expr(socket_path, QUICKFIX_ROWS)), [[str(text_file), 3, 1, "1/2 entry — first"], [str(text_file), 7, 1, "2/2 exit — second"]])
        self.assertEqual(self.expr(socket_path, "expand('%:p')"), str(text_file))
        self.assertEqual(self.expr(socket_path, "line('.')"), "3")
        self.assertEqual(self.expr(socket_path, 'luaeval("_G.__tx_code_tour.index")'), "1")
        for mapping in TOUR_MAPPINGS:
            self.assertNotEqual(self.expr(socket_path, f"maparg('{mapping}', 'n')"), "", mapping)

    @requires_bin("nvim")
    def test_t_nvim_18_severity_names(self):
        socket_path = self.headless_nvim()
        text_file, _stops = self.tour_fixture()
        for severity, expected in (("WARN", 2), ("INFO", 3), ("HINT", 4), ("BOGUS", 1)):
            with self.subTest(severity=severity):
                stops = self.root / "ct" / f"{severity}.lua"
                stops.write_text(f'return {{ {{file="{text_file}", line=2, label="x", message="m", severity="{severity}"}} }}\n')
                completed = self.apply_tour(socket_path, stops)
                self.assertEqual((completed.returncode, completed.stdout), (0, "tour applied\n"), completed.stderr)
                self.assertEqual(json.loads(self.expr(socket_path, DIAGNOSTIC_ROWS)), [[1, 0, expected, "tour", "x"]])

    @requires_bin("nvim")
    def test_t_nvim_19_tour_clear(self):
        socket_path = self.headless_nvim()
        text_file, stops = self.tour_fixture()
        digest = text_file.read_bytes()
        self.assertEqual(self.apply_tour(socket_path, stops).stdout, "tour applied\n")
        completed = self.remote_expr(socket_path, 'execute("TourClear")')
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.expr(socket_path, DIAGNOSTIC_COUNT), "0")
        self.assertEqual(self.expr(socket_path, "len(getqflist())"), "0")
        for mapping in TOUR_MAPPINGS:
            self.assertEqual(self.expr(socket_path, f"maparg('{mapping}', 'n')"), "", mapping)
        self.assertEqual(self.expr(socket_path, 'luaeval("_G.__tx_code_tour == nil")'), "true")
        self.assertEqual(text_file.read_bytes(), digest)

    @requires_bin("nvim")
    def test_t_nvim_19_reapply_resets_namespace(self):
        socket_path = self.headless_nvim()
        text_file, stops = self.tour_fixture()
        self.assertEqual(self.apply_tour(socket_path, stops).stdout, "tour applied\n")
        one_stop = self.root / "ct" / "one.lua"
        one_stop.write_text(f'return {{ {{file="{text_file}", line=5, label="1/1", message="only", severity="INFO"}} }}\n')
        self.assertEqual(self.apply_tour(socket_path, one_stop).stdout, "tour applied\n")
        self.assertEqual(self.expr(socket_path, DIAGNOSTIC_COUNT), "1")
        self.assertEqual(text_file.read_bytes(), "".join(f"line {number}\n" for number in range(1, 10)).encode())

    # ----- T-NVIM-20 (new behaviour, D11) ---------------------------------------------------

    @expected_failure_on_python
    def test_t_nvim_20_fixed_socket_recorded_with_fake_nvim(self):
        self.spawn_nvim("ed", "--tag", "s")
        record = self.show("ed")
        socket_path = f"{self.home.path}/nvim/{record['id']}.sock"
        self.assertEqual(record["nvim_socket"], socket_path)
        self.assertEqual(record["cmd"], NVIM_BASE_COMMAND)
        argv = self.fakes.wait_dump("nvim", record["id"])["argv"]
        self.assertIn("--listen", argv)
        self.assertEqual(argv[argv.index("--listen") + 1], socket_path)
        self.assertLess(argv.index("--listen"), argv.index("+set background=dark | colorscheme tokyonight-moon"))

        self.spawn_nvim("d", "--tag", "s", "--diff", "--open", str(self.root / "x.md"))
        argv = self.fakes.wait_dump("nvim", self.show("d")["id"])["argv"]
        self.assertLess(argv.index("--listen"), argv.index("+DiffviewOpen main"))
        self.assertEqual(argv[-1], str(self.root / "x.md"))
        self.assertEqual(self.show("d")["cmd"], f"{NVIM_BASE_COMMAND} +'DiffviewOpen main' {self.root / 'x.md'}")

        result = self.tx(["spawn", "sh1", "--tag", "s", "--cmd", "sh"])
        self.assertEqual(result.code, 0, result.err)
        self.assertIsNone(self.show("sh1")["nvim_socket"])
        worker = self.spawn_worker()
        self.assertNotIn("nvim_socket", worker)

        artifact_id = self.records.artifact(id=self.ARTIFACT_ID)
        result = self.tx(["artifact", "open", artifact_id], env={"TX_SESSION_ID": worker["id"]})
        self.assertEqual(result.code, 0, result.err)
        art = self.show("art-0123abcd")
        self.assertEqual(art["nvim_socket"], f"{self.home.path}/nvim/{art['id']}.sock")

        result = self.tx(["kill", "ed"])
        self.assertEqual(result.code, 0, result.err)
        self.assertFalse(os.path.exists(socket_path))
        self.assertEqual(self.records.load(record["id"])["nvim_socket"], socket_path)

    @expected_failure_on_python
    @requires_bin("nvim")
    def test_t_nvim_20_fixed_socket_listens_with_real_nvim(self):
        self.fakes.remove("nvim")
        self.stub_colorscheme()
        self.spawn_nvim("ed", "--tag", "s")
        record = self.show("ed")
        socket_path = record["nvim_socket"]
        self.assertEqual(socket_path, f"{self.home.path}/nvim/{record['id']}.sock")
        self.wait_until(lambda: os.path.exists(socket_path))
        self.assertEqual(self.expr(socket_path, "1+1"), "2")
        self.assertEqual(self.expr(socket_path, "v:servername"), socket_path)
