"""SPAWN — `lib/tx/spawn.py` + `lib/tx/service.py` spawn paths + `tx spawn*` (spec section 02).

One method per case `T-SPAWN-<nn>` (T-SPAWN-04 is DROPPED). Everything goes through `tx spawn` /
`tx spawn-nvim` / `tx spawn-view` / `tx show` and is asserted on the record, `log.jsonl`, the private
tmux server, the fake-engine dumps and the git fixture (Appendix B3).

Host notes: tmux 3.4 renders `#{pane_start_command}` through `args_escape`, so a command holding a
space comes back wrapped in double quotes (`"sleep 30"`) — `_start_command` unwraps that one layer.
The shipped `agents/COMMON.md` grants `skills: [tx-sessions, tx-artifacts]`, so every engine-built
spawn also carries `TX_SKILLS=tx-sessions,tx-artifacts` on its env (see NOTES-02).
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid

from txkit import FakeBins, TxCase, expected_failure_on_python

WORKER_CMD = "claude --effort high --no-chrome --dangerously-skip-permissions"
READ_ONLY_CMD = (
    "claude --effort high --no-chrome --allowedTools Bash --disallowedTools Edit Write NotebookEdit "
    "--permission-mode dontAsk --setting-sources user"
)
CODEX_CMD = (
    "codex -m gpt-5.6-sol -c model_reasoning_effort=high "
    "--dangerously-bypass-approvals-and-sandbox --dangerously-bypass-hook-trust"
)
NVIM_BASE_CMD = "nvim +'set background=dark | colorscheme tokyonight-moon'"
# The skill grant of the shipped agents/COMMON.md, stamped on every engine-built spawn's env.
COMMON_SKILLS = "tx-sessions,tx-artifacts"
MAX_COMMAND_BYTES = 8192


def _start_command(case: TxCase, target: str) -> str:
    """`#{pane_start_command}` with tmux's own `"…"` rendering of a spaced argument removed."""
    value = case.tmux.display(target, "#{pane_start_command}")
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    return value


def _worktree_pattern(case: TxCase, name: str) -> re.Pattern:
    """`<home>/worktrees/<slug>-<sha8>/<slug>--<name>` for the lazily created `repo` fixture."""
    slug = case.git.path.name
    return re.compile(
        rf"^{re.escape(str(case.home.worktrees_dir))}/{slug}-[0-9a-f]{{8}}/{slug}--{re.escape(name)}$"
    )


def _detached_listing_line(case: TxCase, path: str) -> re.Pattern:
    return re.compile(rf"^{re.escape(path)}\s+{case.git.head()[:7]} \(detached HEAD\)$", re.MULTILINE)


class TestSpawn(TxCase):
    def _show(self, name: str) -> dict:
        result = self.tx(["show", name])
        self.assertEqual(result.code, 0, result.err)
        return json.loads(result.out)

    def _workdir(self, name: str = "w"):
        directory = self.root / name
        directory.mkdir(exist_ok=True)
        return directory

    # ----- T-SPAWN-01 ------------------------------------------------------------------------

    def test_t_spawn_01_role_inferred_from_command(self):
        """The antigravity leg (`agy` → llm, visible only as the hooks-not-installed refusal) is
        skipped per D3."""
        for name in ("claude-foo", "gemini", "fish", "dash", "mytool"):
            self.fakes.add(name)
            self.fakes.configure(name, sleep=300)
        repo = str(self.git.path)
        expectations = [
            ("n1", f"{self.fakes.bin_dir}/claude --model opus", "llm", "claude"),
            ("n2", "codex", "llm", "codex"),
            ("n3", "nvim +x f", "nvim", None),
            ("n4", "/bin/bash", "shell", None),
            ("n5", "bash", "shell", None),
            ("n6", "sh", "shell", None),
            ("n7", "fish", "shell", None),
            ("n8", "dash", "shell", None),
            ("n9", "zsh", "shell", None),
            ("n10", "python3 -c 'import time;time.sleep(300)'", "other", None),
        ]
        for name, command, role, engine in expectations:
            result = self.tx(["spawn", name, "--tag", "t", "--cwd", repo, "--cmd", command])
            self.assertEqual(result.code, 0, f"{command}: {result.err}")
            record = self._show(name)
            self.assertEqual(record["role"], role, command)
            if engine is None:
                self.assertNotIn("engine", record, command)
                self.assertEqual(record["cwd"], repo, command)
            else:
                self.assertEqual(record["engine"], engine, command)
                self.assertRegex(record["cwd"], _worktree_pattern(self, name))
        worktrees_before = self.git.worktrees()
        for name, command in (("e1", "claude-foo"), ("e2", "gemini")):
            result = self.tx(["spawn", name, "--tag", "t", "--cwd", repo, "--cmd", command])
            self.assertEqual(result.code, 0, result.err)
            record = self._show(name)
            self.assertEqual(record["role"], "other", command)
            self.assertNotIn("engine", record)
            self.assertEqual(record["cwd"], repo)
        self.assertEqual(self.git.worktrees(), worktrees_before)

    # ----- T-SPAWN-02 ------------------------------------------------------------------------

    def test_t_spawn_02_process_spawn_spec_as_persisted(self):
        repo = str(self.git.path)
        result = self.tx(["spawn", "n", "--tag", "a,b", "--cwd", repo, "--engine", "claude", "--env", "K=V", "--group", "g"])
        self.assertEqual(result.code, 0, result.err)
        record = self._show("n")
        self.assertEqual(record["role"], "llm")
        self.assertEqual(record["tags"], ["a", "b"])
        self.assertEqual(record["env"], {"K": "V", "TX_SKILLS": COMMON_SKILLS, "TX_REQUIRE_WORKTREE": "1"})
        self.assertEqual(record["group"], "g")
        self.assertEqual(record["engine"], "claude")
        self.assertIsNone(record["parent"])
        self.assertEqual(record["cmd"], WORKER_CMD)
        self.assertEqual(len(record["chats"]), 1)
        self.assertEqual((record["chats"][0]["id"], record["chats"][0]["role"]), (None, "original"))

        result = self.tx(["spawn", "plain", "--tag", "t", "--cwd", repo, "--engine", "claude"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self._show("plain")["env"], {"TX_SKILLS": COMMON_SKILLS, "TX_REQUIRE_WORKTREE": "1"})

        result = self.tx(["spawn", "sh", "--tag", "t", "--cwd", repo, "--cmd", "bash"])
        self.assertEqual(result.code, 0, result.err)
        record = self._show("sh")
        self.assertEqual((record["role"], record["env"], record["cwd"]), ("shell", {}, repo))
        self.assertNotIn("engine", record)

    # ----- T-SPAWN-03 ------------------------------------------------------------------------

    def test_t_spawn_03_nvim_companion_command_line(self):
        workdir = str(self._workdir())
        plan = f"{workdir}/my plan.md"
        expectations = [
            ("n1", [], NVIM_BASE_CMD),
            ("n2", ["--diff"], f"{NVIM_BASE_CMD} +'DiffviewOpen main'"),
            ("n3", ["--diff", "main"], f"{NVIM_BASE_CMD} +'DiffviewOpen main'"),
            ("n4", ["--open", plan], f"{NVIM_BASE_CMD} '{plan}'"),
            ("n5", ["--diff", "main", "--open", plan], f"{NVIM_BASE_CMD} +'DiffviewOpen main' '{plan}'"),
            ("n6", ["--open", "plan.md"], f"{NVIM_BASE_CMD} plan.md"),
        ]
        for name, flags, command in expectations:
            result = self.tx(["spawn-nvim", name, "--tag", "t", "--cwd", workdir, *flags])
            self.assertEqual(result.code, 0, result.err)
            record = self._show(name)
            self.assertEqual(record["cmd"], command, flags)
            self.assertEqual(record["role"], "nvim")
            self.assertNotIn("engine", record)
            self.assertEqual(_start_command(self, record["id"]), command, flags)

    # ----- T-SPAWN-05 ------------------------------------------------------------------------

    def test_t_spawn_05_spawn_creates_uuid_session_with_id_and_option(self):
        workdir = str(self._workdir())
        before = time.time()
        result = self.tx(["spawn", "sh1", "--tag", "t", "--cwd", workdir, "--cmd", "sleep 30"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, f"Spawned 'sh1' (cwd={workdir}, tag=t)\n")
        self.assertEqual(result.err, "")
        files = list(self.home.sessions_dir.glob("*.json"))
        self.assertEqual(len(files), 1)
        record = json.loads(files[0].read_text())
        session_id = record["id"]
        self.assertEqual(uuid.UUID(session_id).version, 4)
        self.assertEqual(files[0].name, f"{session_id}.json")
        pane_pid = int(self.tmux.display(session_id, "#{pane_pid}"))
        self.assertAlmostEqual(record["created_at"], before, delta=5)
        self.assertEqual(
            record,
            {
                "schema_version": 6, "id": session_id, "name": "sh1", "role": "other", "state": "alive",
                "cwd": workdir, "cmd": "sleep 30", "tags": ["t"], "group": None, "env": {}, "parent": None,
                "pid": pane_pid, "attached_to": [], "created_at": record["created_at"], "ended_at": None,
                "artifact_id": None,
            },
        )
        self.assertIn(session_id, self.tmux.sessions())
        environment = self.tmux.run("show-environment", "-t", session_id, "TX_SESSION_ID")
        self.assertEqual(environment.stdout, f"TX_SESSION_ID={session_id}\n")
        self.assertEqual(self.tmux.option(session_id, "@tx_id"), session_id)
        self.assertEqual(_start_command(self, session_id), record["cmd"])
        lines = self.log_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(list(lines[0]), ["ts", "actor", "type", "msg"])
        self.assertAlmostEqual(lines[0]["ts"], before, delta=5)
        self.assertEqual(
            (lines[0]["actor"], lines[0]["type"], lines[0]["msg"]), ("", "spawn", f"sh1 [other] {workdir}")
        )

        result = self.tx(["spawn", "sh2", "--tag", "t", "--cwd", workdir, "--cmd", "sleep 30", "--env", "TX_SESSION_ID=x"])
        self.assertEqual(result.code, 0, result.err)
        override = self._show("sh2")
        self.assertEqual(self.tmux.environment(override["id"])["TX_SESSION_ID"], "x")

        result = self.tx(["spawn", "sh3", "--tag", "t", "--cwd", workdir, "--cmd", "sleep 30"], env={"TX_SESSION_ID": "caller"})
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual((self.log_tail()[0]["actor"], self.log_tail()[0]["msg"]), ("caller", f"sh3 [other] {workdir}"))

    # ----- T-SPAWN-06 ------------------------------------------------------------------------

    def test_t_spawn_06_llm_record_pending_original_chat_ref(self):
        repo = str(self.git.path)
        before = time.time()
        result = self.tx(["spawn", "w", "--tag", "t", "--cwd", repo, "--engine", "claude"])
        self.assertEqual(result.code, 0, result.err)
        record = self._show("w")
        self.assertEqual((record["role"], record["state"], record["engine"]), ("llm", "idle", "claude"))
        self.assertAlmostEqual(record["last_activity"], before, delta=5)
        self.assertIsNone(record["turn_started_at"])
        self.assertRegex(record["cwd"], _worktree_pattern(self, "w"))
        chat = record["chats"][0]
        self.assertAlmostEqual(chat["started_at"], before, delta=5)
        self.assertEqual(
            record["chats"],
            [
                {
                    "id": None, "role": "original", "cwd": record["cwd"], "transcript_path": "",
                    "origin": {"how": "spawn", "session_id": record["id"], "chat_id": None},
                    "bundle_path": None, "started_at": chat["started_at"], "ended_at": None, "summary": "",
                    "engine": "claude",
                }
            ],
        )
        self.assertEqual((self.log_tail()[0]["type"], self.log_tail()[0]["msg"]), ("spawn", f"w [llm] {record['cwd']}"))

        result = self.tx(["spawn", "x", "--tag", "t", "--cwd", repo, "--cmd", "bash", "--engine", "claude"])
        self.assertEqual(result.code, 0, result.err)
        other = self._show("x")
        self.assertNotIn("engine", other)
        self.assertNotIn("chats", other)
        self.assertEqual(other["state"], "alive")

    # ----- T-SPAWN-07 ------------------------------------------------------------------------

    def test_t_spawn_07_parent_is_executor_tx_id(self):
        workdir = str(self._workdir())
        executor = self.spawn_process("X", cmd="bash", cwd=workdir)
        self.spawn_view("V")

        result = self.run_in_pane(executor["id"], "tx spawn kid --tag t --cmd 'sleep 30'")
        self.assertEqual(result.code, 0, result.err)
        kid = self._show("kid")
        self.assertEqual(kid["parent"], executor["id"])
        self.assertEqual(kid["cwd"], workdir)

        result = self.run_in_pane("V", "tx spawn kid2 --tag t --cmd 'sleep 30'")
        self.assertEqual(result.code, 0, result.err)
        self.assertIsNone(self._show("kid2")["parent"])

        result = self.tx(["spawn", "kid3", "--tag", "t", "--cmd", "sleep 30"])
        self.assertEqual(result.code, 0, result.err)
        self.assertIsNone(self._show("kid3")["parent"])

    # ----- T-SPAWN-08 ------------------------------------------------------------------------

    def test_t_spawn_08_live_duplicate_name_refused(self):
        self.spawn_process("dup", cmd="sleep 30")
        sessions_before = self.tmux.sessions()
        files_before = sorted(self.home.sessions_dir.iterdir())
        log_before = self.log_lines()

        result = self.tx(["spawn", "dup", "--tag", "t", "--cmd", "sleep 30"])
        self.assertEqual(result.code, 1)
        self.assertEqual(result.err, "tx spawn: session 'dup' already exists\n")
        self.assertEqual(result.out, "")
        self.assertEqual(self.tmux.sessions(), sessions_before)
        self.assertEqual(sorted(self.home.sessions_dir.iterdir()), files_before)
        self.assertEqual(self.log_lines(), log_before)

        killed = self.tx(["kill", "dup"])
        self.assertEqual(killed.code, 0, killed.err)
        result = self.tx(["spawn", "dup", "--tag", "t", "--cmd", "sleep 30"])
        self.assertEqual(result.code, 0, result.err)

        ghost = self.spawn_process("ghost", cmd="sleep 30")
        self.tmux.kill_session(ghost["id"])
        result = self.tx(["spawn", "ghost", "--tag", "t", "--cmd", "sleep 30"])
        self.assertEqual(result.code, 0, result.err)
        tail = self.log_tail(2)
        self.assertEqual((tail[0]["type"], tail[0]["msg"]), ("reconcile", "ghost → exited (vanished)"))
        self.assertEqual((tail[1]["type"], tail[1]["msg"]), ("spawn", f"ghost [other] {self.root}"))
        self.assertEqual(self.records.load(ghost["id"])["state"], "exited")

    # ----- T-SPAWN-09 ------------------------------------------------------------------------

    def test_t_spawn_09_live_view_name_refused(self):
        self.spawn_view("Views")
        self.spawn_process("x", cmd="sleep 30")
        files_before = sorted(self.home.sessions_dir.iterdir())

        result = self.tx(["spawn", "Views", "--tag", "t", "--cmd", "sleep 30"])
        self.assertEqual(result.code, 1)
        self.assertEqual(result.err, "tx spawn: 'Views' is a live view session — pick another name\n")
        self.assertEqual(sorted(self.home.sessions_dir.iterdir()), files_before)

        result = self.tx(["rename", "x", "Views"])
        self.assertEqual(result.code, 1)
        self.assertEqual(result.err, "tx rename: 'Views' is a live view session — pick another name\n")
        self.assertEqual(self._show("x")["name"], "x")

        self.tmux.new_session("plain", "sleep 300")
        result = self.tx(["spawn", "plain", "--tag", "t", "--cmd", "sleep 30"])
        self.assertEqual(result.code, 0, result.err)

    # ----- T-SPAWN-10 ------------------------------------------------------------------------

    def test_t_spawn_10_launch_script_threshold(self):
        workdir = str(self._workdir())
        small = "sleep 30 #" + "x" * (MAX_COMMAND_BYTES - 10)
        big = "sleep 30 #" + "x" * (MAX_COMMAND_BYTES + 1 - 10)
        self.assertEqual((len(small.encode()), len(big.encode())), (MAX_COMMAND_BYTES, MAX_COMMAND_BYTES + 1))
        self.home.launch_dir.rmdir()

        result = self.tx(["spawn", "small", "--tag", "t", "--cwd", workdir, "--cmd", small])
        self.assertEqual(result.code, 0, result.err)
        small_record = self._show("small")
        self.assertFalse((self.home.launch_dir / f"{small_record['id']}.sh").exists())
        self.assertEqual(_start_command(self, small_record["id"]), small)
        self.assertEqual(small_record["cmd"], small)

        result = self.tx(["spawn", "big", "--tag", "t", "--cwd", workdir, "--cmd", big])
        self.assertEqual(result.code, 0, result.err)
        big_record = self._show("big")
        script = self.home.launch_dir / f"{big_record['id']}.sh"
        self.assertEqual(_start_command(self, big_record["id"]), f"/bin/sh {script}")
        self.assertTrue(script.exists())
        self.assertEqual(script.stat().st_mode & 0o777, 0o700)
        self.assertEqual(script.read_text(), big + "\n")
        self.assertEqual(big_record["cmd"], big)

        # The threshold counts UTF-8 bytes, not characters: 4092 two-byte `é` = 8194 bytes in 4102
        # characters → script; 4091 = exactly 8192 bytes → inline (the spec's "4091 → 8193" is off by one).
        inline = "sleep 30 #" + "é" * 4091
        oversize = "sleep 30 #" + "é" * 4092
        self.assertEqual((len(inline.encode()), len(oversize.encode())), (MAX_COMMAND_BYTES, MAX_COMMAND_BYTES + 2))
        result = self.tx(["spawn", "u1", "--tag", "t", "--cwd", workdir, "--cmd", inline])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(_start_command(self, self._show("u1")["id"]), inline)
        result = self.tx(["spawn", "u2", "--tag", "t", "--cwd", workdir, "--cmd", oversize])
        self.assertEqual(result.code, 0, result.err)
        utf_record = self._show("u2")
        self.assertEqual(_start_command(self, utf_record["id"]), f"/bin/sh {self.home.launch_dir}/{utf_record['id']}.sh")

        killed = self.tx(["kill", "big"])
        self.assertEqual(killed.code, 0, killed.err)
        self.assertFalse(script.exists())

    # ----- T-SPAWN-11 ------------------------------------------------------------------------

    def test_t_spawn_11_dispatch_agent_worker_else_direct(self):
        repo = str(self.git.path)
        result = self.tx(["spawn", "s", "--tag", "t", "--cwd", repo, "--cmd", "bash"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self._show("s")["cwd"], repo)
        self.assertEqual(self.git.worktrees(), [repo])

        result = self.tx(["spawn", "agent", "--tag", "t", "--cwd", repo, "--cmd", "claude"])
        self.assertEqual(result.code, 0, result.err)
        worker = self._show("agent")
        self.assertRegex(worker["cwd"], _worktree_pattern(self, "agent"))
        self.assertEqual(self.git.worktrees(), [repo, worker["cwd"]])

    # ----- T-SPAWN-12 ------------------------------------------------------------------------

    def test_t_spawn_12_worker_env_flags(self):
        """`$FAKE_OUT` reaches the fakes through the private server's global environment (the kit
        starts the server from a `tx` run that carries it), so no `--env FAKE_OUT` is passed."""
        repo = str(self.git.path)
        result = self.tx(["spawn", "w", "--tag", "t", "--cwd", repo, "--engine", "claude"])
        self.assertEqual(result.code, 0, result.err)
        worker = self._show("w")
        self.assertEqual(worker["env"]["TX_REQUIRE_WORKTREE"], "1")
        self.assertNotIn("TX_READ_ONLY", worker["env"])
        environment = self.tmux.environment(worker["id"])
        self.assertEqual(environment["TX_REQUIRE_WORKTREE"], "1")
        self.assertNotIn("TX_READ_ONLY", environment)
        dump = self.fakes.wait_dump("claude", worker["id"])
        self.assertEqual((dump["env"]["TX_REQUIRE_WORKTREE"], dump["env"]["TX_SESSION_ID"]), ("1", worker["id"]))

        result = self.tx(["spawn", "ro", "--tag", "t", "--cwd", repo, "--engine", "claude", "--read-only"])
        self.assertEqual(result.code, 0, result.err)
        read_only = self._show("ro")
        self.assertEqual(read_only["env"]["TX_READ_ONLY"], "1")
        self.assertNotIn("TX_REQUIRE_WORKTREE", read_only["env"])
        environment = self.tmux.environment(read_only["id"])
        self.assertEqual(environment["TX_READ_ONLY"], "1")
        self.assertNotIn("TX_REQUIRE_WORKTREE", environment)
        dump = self.fakes.wait_dump("claude", read_only["id"])
        self.assertEqual(dump["env"]["TX_READ_ONLY"], "1")
        self.assertNotIn("TX_REQUIRE_WORKTREE", dump["env"])

        result = self.tx(["spawn", "p", "--tag", "t", "--cwd", repo, "--engine", "claude", "--env", "TX_READ_ONLY=1", "--env", "K=V"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self._show("p")["env"], {"K": "V", "TX_SKILLS": COMMON_SKILLS, "TX_REQUIRE_WORKTREE": "1"})

    # ----- T-SPAWN-14 ------------------------------------------------------------------------

    def test_t_spawn_14_worktree_placement_and_name_bump(self):
        repo = str(self.git.path)
        first = self.tx(["spawn", "w", "--tag", "t", "--cwd", repo, "--engine", "claude"])
        self.assertEqual(first.code, 0, first.err)
        worker = self._show("w")
        self.assertRegex(worker["cwd"], _worktree_pattern(self, "w"))
        self.assertEqual(first.out, f"Spawned 'w' (cwd={worker['cwd']}, tag=t)\n")
        self.assertEqual(worker["name"], "w")
        self.assertRegex(self.git.git("worktree", "list"), _detached_listing_line(self, worker["cwd"]))
        self.assertEqual(self.tmux.display(worker["id"], "#{pane_current_path}"), worker["cwd"])

        second = self.tx(["spawn", "w", "--tag", "t", "--cwd", repo, "--engine", "claude"])
        self.assertEqual(second.code, 0, second.err)
        bumped = self._show("w-2")
        self.assertRegex(bumped["cwd"], _worktree_pattern(self, "w-2"))
        self.assertEqual(second.out, f"Spawned 'w-2' (cwd={bumped['cwd']}, tag=t)\n")
        self.assertEqual(bumped["name"], "w-2")

        notgit = self.root / "notgit"
        notgit.mkdir()
        files_before = sorted(self.home.sessions_dir.iterdir())
        result = self.tx(["spawn", "q", "--tag", "t", "--cwd", str(notgit), "--engine", "claude"])
        self.assertEqual(result.code, 1)
        self.assertEqual(
            result.err,
            "tx spawn: could not create worktree: fatal: not a git repository (or any of the parent directories): .git\n",
        )
        self.assertEqual(sorted(self.home.sessions_dir.iterdir()), files_before)

    def test_t_spawn_14_exited_worker_worktree_on_disk_bumps(self):
        repo = str(self.git.path)
        result = self.tx(["spawn", "w", "--tag", "t", "--cwd", repo, "--engine", "claude"])
        self.assertEqual(result.code, 0, result.err)
        worktree = self._show("w")["cwd"]
        killed = self.tx(["kill", "w"])
        self.assertEqual(killed.code, 0, killed.err)
        self.assertTrue(os.path.isdir(worktree))
        result = self.tx(["spawn", "w", "--tag", "t", "--cwd", repo, "--engine", "claude"])
        self.assertEqual(result.code, 0, result.err)
        self.assertRegex(result.out, r"^Spawned 'w-2' \(cwd=.*/repo--w-2, tag=t\)\n$")
        self.assertEqual(self._show("w-2")["name"], "w-2")

    # ----- T-SPAWN-16 ------------------------------------------------------------------------

    def test_t_spawn_16_read_only_wrapper(self):
        repo = str(self.git.path)
        result = self.tx(["spawn", "ro", "--tag", "t", "--cwd", repo, "--engine", "claude", "--read-only"])
        self.assertEqual(result.code, 0, result.err)
        record = self._show("ro")
        self.assertEqual(record["cmd"], READ_ONLY_CMD)
        self.assertEqual(record["env"], {"TX_SKILLS": COMMON_SKILLS, "TX_READ_ONLY": "1"})
        worktree = record["cwd"]
        repository_key = os.path.dirname(worktree)
        start_command = _start_command(self, record["id"])
        self.assertTrue(
            start_command.startswith(
                f"{self.fakes.bin_dir}/bwrap --bind / / --ro-bind {repo} {repo} --ro-bind {repository_key} {repository_key}"
            ),
            start_command,
        )
        self.assertIn(f"--chdir {worktree} -- claude ", start_command)
        self.assertTrue(start_command.endswith(f" -- {READ_ONLY_CMD}"), start_command)

        result = self.tx(["spawn", "w", "--tag", "t", "--cwd", repo, "--engine", "claude"])
        self.assertEqual(result.code, 0, result.err)
        writable = self._show("w")
        self.assertEqual(_start_command(self, writable["id"]), writable["cmd"])

    def test_t_spawn_16_bwrap_absent_refused(self):
        repo = str(self.git.path)
        self.fakes.remove("bwrap")
        path = os.pathsep.join([str(self.tmux.bin_dir), str(self.fakes.bin_dir), str(self.fakes.helpers_dir), "/usr/bin", "/bin"])
        result = self.tx(["spawn", "ro", "--tag", "t", "--cwd", repo, "--engine", "claude", "--read-only"], env={"PATH": path})
        self.assertEqual(result.code, 1)
        self.assertEqual(
            result.err,
            "tx spawn: could not enforce read-only process sandbox: Linux read-only sessions require bubblewrap (bwrap)\n",
        )
        self.assertEqual(self.git.worktrees(), [repo])
        self.assertEqual(list(self.home.sessions_dir.iterdir()), [])

    # ----- T-SPAWN-18 ------------------------------------------------------------------------

    def test_t_spawn_18_argv_validation(self):
        expectations = [
            (["spawn", "n"], "the following arguments are required: --tag"),
            (["spawn", "n", "--tag", ""], "--tag requires at least one value"),
            (
                ["spawn", "n", "--tag", "t", "--cmd", "zsh", "--prompt", "p"],
                "--prompt/--model/--effort/--role build a launch command and cannot be combined with --cmd "
                "(the full hand-written command)",
            ),
            (["spawn", "n", "--tag", "t", "--cmd", "zsh", "--read-only"], "--read-only requires an engine-built launch; it cannot enforce --cmd"),
            (["spawn", "n", "--tag", "t", "--cmd", "zsh", "--chrome"], "--chrome requires an engine-built launch; put the engine's own flag in --cmd"),
            (["spawn", "n", "--tag", "t", "--read-only"], "--read-only requires an agent launch"),
            (["spawn", "n", "--tag", "t", "--env", "NOEQ"], "--env expects KEY=VALUE, got 'NOEQ'"),
            (["spawn", "n", "--tag", "t", "--group", ""], "a group cannot be empty"),
            (["spawn", "n", "--tag", "t", "--effort", "6"], "argument --effort: invalid choice"),
        ]
        for argv, message in expectations:
            result = self.tx(argv)
            self.assertEqual(result.code, 2, argv)
            self.assertTrue(result.err.startswith("usage: tx spawn"), result.err)
            self.assertIn(f"tx spawn: error: ", result.err)
            self.assertIn(message, result.err)
            self.assertEqual(result.out, "")
        self.assertEqual(list(self.home.sessions_dir.iterdir()), [])

    # ----- T-SPAWN-19 ------------------------------------------------------------------------

    def test_t_spawn_19_bare_spawn_is_shell_and_cmd_stamps_engine(self):
        workdir = str(self._workdir())
        repo = str(self.git.path)
        self.fakes.add("mytool")
        self.fakes.configure("mytool", sleep=300)
        self.assertEqual(self.tmux.sessions(), [])

        result = self.tx(["spawn", "s", "--tag", "a,b"], cwd=workdir)
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, f"Spawned 's' (cwd={workdir}, tag=a,b)\n")
        shell = self._show("s")
        self.assertEqual((shell["cmd"], shell["role"], shell["cwd"], shell["tags"]), ("/bin/bash", "shell", workdir, ["a", "b"]))

        # Non-hex names throughout: with several uuid-named sessions live, a bare hex display name
        # (`c`, `e1`) can prefix-match ANOTHER session's uuid on the reference (Q27; ~6 % per pair).
        result = self.tx(["spawn", "cw", "--tag", "t", "--cwd", repo, "--cmd", "codex --foo"])
        self.assertEqual(result.code, 0, result.err)
        worker = self._show("cw")
        self.assertEqual((worker["engine"], worker["role"], worker["cmd"]), ("codex", "llm", "codex --foo"))
        self.assertTrue(worker["cwd"].startswith(str(self.home.worktrees_dir)), worker["cwd"])
        dump = self.fakes.wait_dump("codex", worker["id"])
        self.assertEqual(os.path.basename(dump["argv"][0]), "codex")
        self.assertEqual(dump["argv"][1:], ["--foo"])

        result = self.tx(["spawn", "z", "--tag", "t", "--cwd", workdir], env={"SHELL": None})
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual((self._show("z")["cmd"], self._show("z")["role"]), ("zsh", "shell"))

        other = str(self._workdir("other"))
        executor = self.spawn_process("X", cmd="bash", cwd=workdir)
        result = self.run_in_pane(executor["id"], f"tx spawn ex1 --tag t --cwd {other} --cmd 'sleep 30'")
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self._show("ex1")["cwd"], other)
        result = self.run_in_pane(executor["id"], "tx spawn ex2 --tag t --cmd 'sleep 30'")
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self._show("ex2")["cwd"], workdir)

        result = self.tx(["spawn", "m", "--tag", "t", "--cwd", repo, "--cmd", "mytool"])
        self.assertEqual(result.code, 0, result.err)
        tool = self._show("m")
        self.assertNotIn("engine", tool)
        self.assertEqual((tool["role"], tool["cwd"]), ("other", repo))
        self.assertNotIn(tool["cwd"], self.git.worktrees()[1:])

    @expected_failure_on_python
    def test_t_spawn_19_fixed_outside_tmux_with_live_server_uses_caller_cwd(self):
        """Q20 FIX: outside tmux with a live server, an omitted `--cwd` is the caller's cwd — never the
        server's current pane path (the reference takes the pane path)."""
        pane_dir = str(self._workdir("pane"))
        caller_dir = str(self._workdir("caller"))
        self.spawn_process("first", cmd="sleep 30", cwd=pane_dir)
        result = self.tx(["spawn", "n", "--tag", "t", "--cmd", "sleep 30"], cwd=caller_dir)
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self._show("n")["cwd"], caller_dir)
        self.assertEqual(result.out, f"Spawned 'n' (cwd={caller_dir}, tag=t)\n")

    # ----- T-SPAWN-20 ------------------------------------------------------------------------

    def test_t_spawn_20_spawn_nvim_cli(self):
        workdir = str(self._workdir())
        result = self.tx(["spawn-nvim", "rev", "--tag", "t", "--diff"], cwd=workdir)
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, f"Spawned nvim 'rev' (cwd={workdir}, tag=t, diff=main)\n")
        record = self._show("rev")
        self.assertEqual(record["cmd"], f"{NVIM_BASE_CMD} +'DiffviewOpen main'")
        self.assertEqual(uuid.UUID(record["id"]).version, 4)
        self.assertIn(record["id"], self.tmux.sessions())
        self.assertEqual(self.tmux.option(record["id"], "@tx_id"), record["id"])
        self.assertEqual((record["role"], record["state"]), ("nvim", "alive"))
        self.assertEqual((self.log_tail()[0]["type"], self.log_tail()[0]["msg"]), ("spawn", f"rev [nvim] {workdir}"))

        killed = self.tx(["kill", "rev"])
        self.assertEqual(killed.code, 0, killed.err)
        result = self.tx(["spawn-nvim", "rev", "--tag", "t", "--cwd", workdir, "--diff", "abc123", "--open", f"{workdir}/p.md"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, f"Spawned nvim 'rev' (cwd={workdir}, tag=t, diff=abc123, open={workdir}/p.md)\n")
        self.assertEqual(self._show("rev")["cmd"], f"{NVIM_BASE_CMD} +'DiffviewOpen abc123' {workdir}/p.md")

        result = self.tx(["spawn-nvim", "rev3", "--diff"], cwd=workdir)
        self.assertEqual(result.code, 2)
        self.assertIn("the following arguments are required: --tag", result.err)

    # ----- T-SPAWN-21 ------------------------------------------------------------------------

    def test_t_spawn_21_spawn_view(self):
        workdir = str(self._workdir())
        self.assertNotIn("Views", self.tmux.sessions())
        result = self.tx(["spawn-view", "Views", "--cwd", workdir])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, f"Spawned view 'Views' (cwd={workdir})\n")
        self.assertIn("Views", self.tmux.sessions())
        self.assertEqual(_start_command(self, "Views"), "/bin/bash")
        self.assertEqual(self.tmux.display("Views", "#{pane_current_path}"), workdir)
        self.assertEqual(self.tmux.option("Views", "@tx_view"), "1")
        self.assertIsNone(self.tmux.option("Views", "@tx_id"))
        environment = self.tmux.environment("Views")
        self.assertEqual((environment["COLORTERM"], environment["TERM"]), ("truecolor", "xterm-256color"))
        self.assertNotIn("TX_SESSION_ID", environment)
        self.assertEqual(self.tmux.option("Views", "status"), "on")
        self.assertEqual(self.tmux.option("Views", "pane-border-status", "window"), "top")
        self.assertEqual(list(self.home.sessions_dir.iterdir()), [])
        lines = self.log_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual((lines[0]["type"], lines[0]["msg"]), ("spawn-view", f"Views {workdir}"))

        result = self.tx(["spawn-view", "Views", "--cwd", workdir])
        self.assertEqual(result.code, 1)
        self.assertEqual(result.err, "tx spawn-view: session 'Views' already exists\n")

        self.spawn_process("P", cmd="sleep 30")
        result = self.tx(["spawn-view", "P", "--cwd", workdir])
        self.assertEqual(result.code, 1)
        self.assertEqual(result.err, "tx spawn-view: session 'P' already exists\n")

        result = self.tx(["spawn-view", "V2", "--cwd", workdir, "--cmd", "sleep 30"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(_start_command(self, "V2"), "sleep 30")
        listing = self.tx(["ls"])
        self.assertEqual(listing.code, 0, listing.err)
        self.assertNotIn("Views", listing.out)
        self.assertNotIn("V2", listing.out)
        self.assertEqual(listing.out.splitlines()[0], "PROCESSES")
        self.assertEqual(len(listing.out.splitlines()), 2)


class TestSpawnCodexHooksMissing(TxCase):
    """`hooks/codex/start.sh` absent (claude shims only) — the worker-refusal cases."""

    home_options = {"hooks": ("claude",)}

    def _show(self, name: str) -> dict:
        result = self.tx(["show", name])
        self.assertEqual(result.code, 0, result.err)
        return json.loads(result.out)

    # ----- T-SPAWN-13 ------------------------------------------------------------------------

    def test_t_spawn_13_worker_refusals(self):
        repo = str(self.git.path)
        result = self.tx(["spawn", "c", "--tag", "t", "--cwd", repo, "--cmd", "codex --foo"])
        self.assertEqual(result.code, 1)
        self.assertEqual(
            result.err,
            f"tx spawn: codex hooks are not installed in {self.home.path} — the worker's chat id would never be "
            "captured and the session could never be resumed. Install them: setup/engines/install.sh install "
            "--engine codex\n",
        )
        self.assertEqual(result.out, "")
        self.assertEqual(self.git.worktrees(), [repo])
        repository_keys = list(self.home.worktrees_dir.iterdir())
        self.assertEqual(len(repository_keys), 1)
        self.assertRegex(repository_keys[0].name, r"^repo-[0-9a-f]{8}$")
        self.assertEqual(list(repository_keys[0].iterdir()), [])
        self.assertEqual(list(self.home.sessions_dir.iterdir()), [])
        self.assertEqual(self.tmux.sessions(), [])
        self.assertEqual(self.log_lines(), [])

    # ----- T-SPAWN-17 ------------------------------------------------------------------------

    def test_t_spawn_17_worker_name_resolution_and_worktree_removal(self):
        repo = str(self.git.path)
        result = self.tx(["spawn", "c", "--tag", "t", "--cwd", repo, "--cmd", "codex"])
        self.assertEqual(result.code, 1)
        self.assertTrue(result.err.startswith("tx spawn: codex hooks are not installed in "), result.err)
        self.assertEqual(self.git.worktrees(), [repo])

        first = self.tx(["spawn", "w", "--tag", "t", "--cwd", repo, "--engine", "claude"])
        self.assertEqual(first.code, 0, first.err)
        second = self.tx(["spawn", "w", "--tag", "t", "--cwd", repo, "--engine", "claude"])
        self.assertEqual(second.code, 0, second.err)
        bumped = self._show("w-2")
        self.assertRegex(bumped["cwd"], _worktree_pattern(self, "w-2"))
        self.assertEqual(second.out, f"Spawned 'w-2' (cwd={bumped['cwd']}, tag=t)\n")


class TestSpawnCustomAgents(TxCase):
    """A plain `agents/` dir whose `COMMON.md` grants only `tx-sessions` (T-SPAWN-15)."""

    home_options = {"link_agents": False}

    def setUp(self) -> None:
        super().setUp()
        skill = self.home.agents / "skills" / "tx-sessions"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: tx-sessions\ndescription: spawn conventions\n---\n\n# tx-sessions\n")
        (self.home.agents / "COMMON.md").write_text("---\ntx:\n  skills: [tx-sessions]\n---\n\n# COMMON\n")

    def _show(self, name: str) -> dict:
        result = self.tx(["show", name])
        self.assertEqual(result.code, 0, result.err)
        return json.loads(result.out)

    # ----- T-SPAWN-15 ------------------------------------------------------------------------

    def test_t_spawn_15_worker_access_preparation(self):
        repo = str(self.git.path)
        result = self.tx(["spawn", "c", "--tag", "t", "--cwd", repo, "--engine", "codex"])
        self.assertEqual(result.code, 0, result.err)
        record = self._show("c")
        self.assertRegex(record["cwd"], _worktree_pattern(self, "c"))
        self.assertEqual(record["cmd"], CODEX_CMD)
        self.assertEqual(record["env"], {"TX_SKILLS": "tx-sessions", "TX_REQUIRE_WORKTREE": "1"})
        self.assertEqual(_start_command(self, record["id"]), record["cmd"])
        link = self.root / record["cwd"] / ".agents" / "skills" / "tx-sessions"
        self.assertTrue(link.is_symlink(), link)
        self.assertEqual(os.path.realpath(link), os.path.realpath(self.home.agents / "skills" / "tx-sessions"))
        exclude = (self.git.path / ".git" / "info" / "exclude").read_text().splitlines()
        self.assertIn(".agents/skills/", exclude)

        result = self.tx(["spawn", "k", "--tag", "t", "--cwd", repo, "--engine", "claude"])
        self.assertEqual(result.code, 0, result.err)
        claude_record = self._show("k")
        claude_link = self.root / claude_record["cwd"] / ".claude" / "skills" / "tx-sessions"
        self.assertTrue(claude_link.is_symlink(), claude_link)
        self.assertIn(".claude/skills/", (self.git.path / ".git" / "info" / "exclude").read_text().splitlines())
