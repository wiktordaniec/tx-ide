"""CLI — cli.py dispatch + verb contracts (spec section 04, T-CLI-01..27).

Every case drives `TX_BIN` and asserts stdout / stderr / exit code, the record files, tmux state on
the private server, fake-binary dumps, and the `log.jsonl` tail. Spec/code disagreements are in
`NOTES-04.md`.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import time
from datetime import datetime
from pathlib import Path

from txkit import (
    HOME_DIRS,
    REAL_TMUX,
    REPO,
    SESSION_RECORD_CMD,
    TxCase,
    expected_failure_on_python,
    platform_only,
    python_reference_only,
    strip_ansi,
    tmux_version,
)

W1_ID = "11111111-1111-4111-8111-111111111111"
SH1_ID = "22222222-2222-4222-8222-222222222222"
E1_ID = "33333333-3333-4333-8333-333333333333"
I0_ID = "00000000-0000-4000-8000-000000000000"

PUBLIC_COMMANDS = [
    ("start", "Ensure the Views home base + tx-assistant exist, then attach Views."),
    ("attach", "Open the interactive session picker (fzf)."),
    ("ls", "List current (live) sessions (a single PROCESSES listing; views live in tmux)."),
    ("spawn", "Spawn a detached tmux session (--tag mandatory)."),
    ("spawn-nvim", "Spawn a detached nvim companion (--diff opens a diffview; --open opens a file)."),
    ("spawn-view", "Spawn a detached view session (a live @tx_view tmux home, not a store record)."),
    ("tag", "Read or set a session's tags (comma-separated)."),
    ("group", "Read or set a session's effort-group override (--clear returns to derived)."),
    ("rename", "Rename a session's display name (record; a process leaves its tmux id untouched)."),
    ("whoami", "Print the current session's display name (resolves #S — the id for a process)."),
    ("send-message", "Peer-message another agent session (delivered in a <from-agent> envelope)."),
    ("send-user-message", "Message another session as the operator (<from-user>, not a peer agent)."),
    ("kill", "End a tmux session and mark its record EXITED."),
    ("archive", "Retire a session (mark ARCHIVED, keep the record) + force a full history ingest."),
    ("rm", "Delete a session record (by id or name)."),
    ("show", "Print a session record as JSON (by id or name)."),
    ("history", "List past (EXITED / ARCHIVED) sessions — filter by --tag / --cwd / --since / --until."),
    ("chat", "Inspect a session's chats: `chat ls <session>` lists its ChatRefs + bundle paths."),
    ("resume", "Re-spawn a past session + reattach its chat (claude --resume); collision-safe (§7)."),
    ("artifact", "Create / modify / inspect durable versioned artifacts (tx artifact <subcommand>)."),
    ("sync", "Manual archive sync of the reproducible corpus (push/pull/status) — never hot-path."),
    ("fork", "Fork a session's chat into a NEW session that starts with the full history (§4)."),
    ("handover", "Distill a session's chat into a focused brief for a NEW worker session (§5)."),
    ("rollover", "Rotate a session onto a fresh chat in the SAME pane (context exhausted) (§6)."),
    ("migrate", "Upgrade $TX_IDE_HOME records to the current schemas (sessions v3→v6, artifacts v1→v2)."),
]
HELP_TEXT = "tx — tmux + Claude Code session controller\n\nusage: tx <command> [args]\n\ncommands:\n" + "".join(
    f"  {name:<18} {summary}\n" for name, summary in PUBLIC_COMMANDS
)

COMMON_MD = "---\ntx:\n  skills: [tx-sessions]\n---\n# Common\nbody\n"
DEV_MD = "---\ntx:\n  skills: [tx-artifacts]\n---\n# Dev\ndev body\n"
SPAWN_COMBINE_ERROR = (
    "--prompt/--model/--effort/--role build a launch command and cannot be combined with --cmd "
    "(the full hand-written command)"
)
LLM_KEYS = [
    "schema_version", "id", "name", "role", "state", "cwd", "cmd", "tags", "group", "env", "parent",
    "pid", "attached_to", "created_at", "ended_at", "engine", "last_activity", "chats", "turn_started_at",
]
OTHER_KEYS = LLM_KEYS[:15] + ["artifact_id"]


def _local_epoch(year: int, month: int, day: int, hour: int, minute: int) -> float:
    return datetime(year, month, day, hour, minute).timestamp()


class TestCli(TxCase):
    home_options = {"link_agents": False}

    def setUp(self) -> None:
        super().setUp()
        self._write_roles()

    def _write_roles(self) -> None:
        """Fixture: `agents/` as a plain dir with COMMON + DEV and the two skills they grant."""
        skills = self.home.agents / "skills"
        for name in ("tx-sessions", "tx-artifacts"):
            (skills / name).mkdir(parents=True, exist_ok=True)
            (skills / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\nbody\n")
        (self.home.agents / "COMMON.md").write_text(COMMON_MD)
        (self.home.agents / "DEV.md").write_text(DEV_MD)

    def _live(self, record_id: str, cwd: str | None = None) -> None:
        self.tmux.new_session(record_id, "sleep 1000", tx_id=record_id, cwd=cwd)

    def _view(self, name: str = "Views", window: str = "main", command: str = "sleep 1000",
              cwd: str | None = None) -> None:
        args = ["new-session", "-d", "-s", name, "-n", window]
        if cwd is not None:
            args += ["-c", cwd]
        self.tmux.run(*args, command, check=True)
        self.tmux.run("set-option", "-t", name, "@tx_view", "1", check=True)

    def _attach_command(self, inner: str) -> str:
        return f"TMUX= {REAL_TMUX} -L {self.tmux.socket} attach -t {inner}"

    def _clients(self) -> list[str]:
        return self.tmux.run("list-clients", "-F", "#{client_session}").stdout.split()

    def _nest_attach(self, inner: str, cwd: str | None = None) -> str:
        """The T-CLI-09 topology: view `Views`, window 0 `main`, pane 1 nest-attached to `inner`.
        Returns the nested pane's id."""
        self._view()
        args = ["split-window", "-t", "Views:main"]
        if cwd is not None:
            args += ["-c", cwd]
        self.tmux.run(*args, self._attach_command(inner), check=True)
        self.wait_until(lambda: inner in self._clients())
        return self.tmux.display("Views:main.1", "#{pane_id}")

    def _show(self, target: str) -> dict:
        result = self.tx(["show", target])
        self.assertEqual(result.code, 0, result.err)
        return json.loads(result.out)

    def _record_files(self) -> list[str]:
        return sorted(path.name for path in self.home.sessions_dir.glob("*.json"))

    def _assert_spawn_refused(self, argv: list[str], message: str, *, code: int = 2) -> None:
        records, log, sessions = self._record_files(), self.log_lines(), self.tmux.sessions()
        result = self.tx(["spawn", *argv])
        self.assertEqual(result.code, code, result.err)
        self.assertEqual(result.out, "")
        self.assertTrue(result.err.endswith(f"tx spawn: error: {message}\n"), result.err)
        self.assertEqual(self._record_files(), records)
        self.assertEqual(self.log_lines(), log)
        self.assertEqual(self.tmux.sessions(), sessions)
        self.assertEqual(self.git.worktrees(), [str(self.git.path)])

    # ----- T-CLI-01 ---------------------------------------------------------------------------

    def test_t_cli_01_help_and_unknown_verb(self):
        for argv in ([], ["help"], ["-h"], ["--help"]):
            result = self.tx(argv)
            self.assertEqual((result.code, result.err), (0, ""), argv)
            self.assertEqual(result.out, HELP_TEXT, argv)
        self.assert_golden("cli/01", self.tx(["help"]).out)
        unknown = self.tx(["frobnicate"])
        self.assertEqual(unknown.code, 2)
        self.assertEqual(unknown.err, "tx: unknown command: frobnicate\n\n")
        self.assertEqual(unknown.out, HELP_TEXT)
        # Hidden verbs dispatch but are not listed.
        self.assertNotIn("_init-home", HELP_TEXT)
        self.assertEqual(self.tx(["_init-home"]).code, 0)

    # ----- T-CLI-02 ---------------------------------------------------------------------------

    def test_t_cli_02_error_boundary(self):
        result = self.tx(["kill", "zzz"])
        self.assertEqual(result.code, 1)
        self.assertEqual(result.err, "tx kill: session 'zzz' not found (no live @tx_id, no store record)\n")
        self.assertEqual(result.out, "")
        # A tmux failure: `new-session -s ""` is refused by tmux itself.
        view = self.tx(["spawn-view", "", "--cwd", str(self.root)])
        self.assertEqual(view.code, 1)
        self.assertEqual(view.out, "")
        self.assertTrue(
            view.err.startswith("tx spawn-view: tmux new-session -d -P -F #{pane_pid} -s  -c "), view.err
        )
        self.assertTrue(view.err.endswith("failed: invalid session:\n"), view.err)
        self.assertEqual(self.tmux.sessions(), [])
        self.assertEqual([line for line in self.log_lines() if line["type"] == "spawn-view"], [])

    def test_t_cli_02_missing_cwd_is_not_a_tmux_error(self):
        # Q21 (PARITY, tmux-version gated): tmux < 3.6 tolerates a nonexistent `-c` dir, so the
        # spawn succeeds and the record keeps the cwd. On the 3.6 floor the tolerance is unverified
        # here: the port must then match the reference — either the same tolerant spawn, or the
        # error-boundary shape `tx spawn: tmux new-session … failed: …` with nothing created.
        result = self.tx(["spawn", "x", "--tag", "t", "--cwd", "/nonexistent"])
        if tmux_version() < (3, 6) or result.code == 0:
            self.assertEqual(result.code, 0, result.err)
            self.assertEqual(result.out, "Spawned 'x' (cwd=/nonexistent, tag=t)\n")
            self.assertEqual(self._show("x")["cwd"], "/nonexistent")
        else:
            self.assertEqual((result.code, result.out), (1, ""))
            self.assertTrue(result.err.startswith("tx spawn: tmux new-session "), result.err)
            self.assertIn(" failed: ", result.err)
            self.assertEqual(self._record_files(), [])
            self.assertEqual(self.tmux.sessions(), [])

    # ----- T-CLI-03 ---------------------------------------------------------------------------

    def test_t_cli_03_parity_required_and_exclusive_flags(self):
        repo = str(self.git.path)
        effort_choices = "(choose from '1', '2', '3', '4', '5')"
        engine_choices = "(choose from 'claude', 'codex', 'antigravity')"
        cases = [
            (["w"], "the following arguments are required: --tag"),
            (["w", "--tag", ""], "--tag requires at least one value"),
            (["w", "--tag", ","], "--tag requires at least one value"),
            (["w", "--tag", "t", "--cmd", "zsh", "--prompt", "p"], SPAWN_COMBINE_ERROR),
            (["w", "--tag", "t", "--cmd", "zsh", "--model", "m"], SPAWN_COMBINE_ERROR),
            (["w", "--tag", "t", "--cmd", "zsh", "--effort", "2"], SPAWN_COMBINE_ERROR),
            (["w", "--tag", "t", "--cmd", "zsh", "--role", "X"], SPAWN_COMBINE_ERROR),
            (["w", "--tag", "t", "--cmd", "zsh", "--read-only"],
             "--read-only requires an engine-built launch; it cannot enforce --cmd"),
            (["w", "--tag", "t", "--cmd", "zsh", "--chrome"],
             "--chrome requires an engine-built launch; put the engine's own flag in --cmd"),
            (["w", "--tag", "t", "--effort", "0"], f"argument --effort: invalid choice: '0' {effort_choices}"),
            (["w", "--tag", "t", "--effort", "6"], f"argument --effort: invalid choice: '6' {effort_choices}"),
            (["w", "--tag", "t", "--engine", "gpt"], f"argument --engine: invalid choice: 'gpt' {engine_choices}"),
            (["w", "--tag", "t", "--env", "FOO"], "argument --env: --env expects KEY=VALUE, got 'FOO'"),
            (["w", "--tag", "t", "--group", ""], "argument --group: a group cannot be empty"),
            (["w", "--tag", "t", "--role", ""], "--role requires at least one role name"),
            (["w", "--tag", "t", "--role", "NOPE"],
             f"unknown role 'NOPE' (no .md file under {self.home.user_agents_dir} or {self.home.agents})"),
            (["w", "--tag", "t", "--read-only"], "--read-only requires an agent launch"),
        ]
        for argv, message in cases:
            with self.subTest(argv=argv):
                self._assert_spawn_refused([*argv, "--cwd", repo], message)

    @expected_failure_on_python
    def test_t_cli_03_fixed_empty_prompt_with_cmd(self):
        # Q16 FIX: an empty --prompt is still a prompt; the reference's falsy check lets it through.
        self._assert_spawn_refused(
            ["w", "--tag", "t", "--cmd", "zsh", "--prompt", "", "--cwd", str(self.git.path)],
            SPAWN_COMBINE_ERROR,
        )

    def test_t_cli_03_env_pair_split_on_first_equals(self):
        scratch = self.root / "scratch"
        scratch.mkdir()
        result = self.tx(["spawn", "e1", "--tag", "t", "--cwd", str(scratch), "--env", "K=", "--env", "J=a=b"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self._show("e1")["env"], {"K": "", "J": "a=b"})

    # ----- T-CLI-04 ---------------------------------------------------------------------------

    def test_t_cli_04_bare_shell(self):
        scratch = self.root / "scratch"
        scratch.mkdir()
        result = self.tx(["spawn", "sh1", "--tag", "t1,t2", "--cwd", str(scratch), "--env", "A=1"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, f"Spawned 'sh1' (cwd={scratch}, tag=t1,t2)\n")
        record = self._show("sh1")
        self.assertEqual(list(record), OTHER_KEYS)
        self.assertEqual(record["role"], "shell")
        self.assertEqual(record["cmd"], "/bin/bash")
        self.assertEqual(record["tags"], ["t1", "t2"])
        self.assertEqual(record["env"], {"A": "1"})
        self.assertNotIn("engine", record)
        self.assertIsNone(record["parent"])
        self.assertIsNone(record["artifact_id"])
        self.assertEqual(record["cwd"], str(scratch))
        session_id = record["id"]
        self.assertIn(session_id, self.tmux.sessions())
        self.assertEqual(self.tmux.option(session_id, "@tx_id"), session_id)
        environment = self.tmux.environment(session_id)
        self.assertEqual(environment["TX_SESSION_ID"], session_id)
        self.assertEqual(environment["A"], "1")
        tail = self.log_tail()[0]
        self.assertEqual((tail["type"], tail["msg"]), ("spawn", f"sh1 [shell] {scratch}"))
        # Edges: a live same-name record, and a live view's name.
        clash = self.tx(["spawn", "sh1", "--tag", "t", "--cwd", str(scratch)])
        self.assertEqual((clash.code, clash.err), (1, "tx spawn: session 'sh1' already exists\n"))
        self._view()
        view_clash = self.tx(["spawn", "Views", "--tag", "t", "--cwd", str(scratch)])
        self.assertEqual(
            (view_clash.code, view_clash.err),
            (1, "tx spawn: 'Views' is a live view session — pick another name\n"),
        )

    def test_t_cli_04_shell_unset_defaults_to_zsh(self):
        scratch = self.root / "scratch"
        scratch.mkdir()
        result = self.tx(["spawn", "z1", "--tag", "t", "--cwd", str(scratch)], env={"SHELL": None})
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self._show("z1")["cmd"], "zsh")

    def test_t_cli_04_cwd_omitted_falls_back_to_the_callers_cwd(self):
        # No live sessions on the private server → `#{pane_current_path}` is empty → os.getcwd().
        scratch = self.root / "scratch"
        scratch.mkdir()
        result = self.tx(["spawn", "c1", "--tag", "t"], cwd=scratch)
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self._show("c1")["cwd"], str(scratch))

    # ----- T-CLI-05 ---------------------------------------------------------------------------

    def test_t_cli_05_engine_built_worker_with_role_and_skills(self):
        repo = str(self.git.path)
        result = self.tx(["spawn", "w1", "--tag", "t", "--role", "DEV", "--effort", "4", "--model", "opus",
                          "--prompt", "go", "--cwd", repo])
        self.assertEqual(result.code, 0, result.err)
        record = self._show("w1")
        worktree = record["cwd"]
        self.assertEqual(result.out, f"Spawned 'w1' (cwd={worktree}, tag=t)\n")
        self.assertEqual((record["role"], record["engine"], record["name"]), ("llm", "claude", "w1"))
        self.assertTrue(worktree.startswith(str(self.home.worktrees_dir)), worktree)
        self.assertIn(os.path.realpath(worktree), [os.path.realpath(path) for path in self.git.worktrees()])
        self.assertEqual(record["env"]["TX_SKILLS"], "tx-sessions,tx-artifacts")
        self.assertEqual(record["env"]["TX_REQUIRE_WORKTREE"], "1")
        dump = self.fakes.wait_dump("claude", record["id"])
        argv = dump["argv"]
        priming = "# Common\nbody\n\n# Dev\ndev body"
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        self.assertEqual(argv[argv.index("--effort") + 1], "xhigh")
        self.assertEqual(argv[argv.index("--append-system-prompt") + 1], priming)
        self.assertIn("--dangerously-skip-permissions", argv)
        self.assertEqual(argv[-1], "go")
        self.assertEqual(shlex.split(record["cmd"])[1:], argv[1:])
        for name in ("tx-sessions", "tx-artifacts"):
            link = Path(worktree) / ".claude" / "skills" / name
            self.assertTrue(link.is_symlink(), link)
            self.assertEqual(os.readlink(link), str(self.home.agents / "skills" / name))
        exclude = (self.git.path / ".git" / "info" / "exclude").read_text()
        self.assertIn(".claude/skills/", exclude.splitlines())

    def test_t_cli_05_codex_engine_links_under_agents_skills(self):
        repo = str(self.git.path)
        result = self.tx(["spawn", "w1", "--tag", "t", "--role", "DEV", "--engine", "codex", "--cwd", repo])
        self.assertEqual(result.code, 0, result.err)
        record = self._show("w1")
        self.assertEqual(record["engine"], "codex")
        for name in ("tx-sessions", "tx-artifacts"):
            link = Path(record["cwd"]) / ".agents" / "skills" / name
            self.assertTrue(link.is_symlink(), link)
        exclude = (self.git.path / ".git" / "info" / "exclude").read_text()
        self.assertIn(".agents/skills/", exclude.splitlines())
        self.assertNotIn(".claude/skills/", exclude.splitlines())

    def test_t_cli_05_role_lists_concatenate_in_order(self):
        for name in ("a", "b", "c"):
            (self.home.agents / f"{name}.md").write_text(f"# {name.upper()}\n")
        result = self.tx(["spawn", "w1", "--tag", "t", "--role", "a,b", "--role", "c", "--cwd", str(self.git.path)])
        self.assertEqual(result.code, 0, result.err)
        record = self._show("w1")
        argv = self.fakes.wait_dump("claude", record["id"])["argv"]
        self.assertEqual(argv[argv.index("--append-system-prompt") + 1], "# Common\nbody\n\n# A\n\n# B\n\n# C")

    def test_t_cli_05_cmd_infers_engine_and_links_the_common_grant(self):
        result = self.tx(["spawn", "w1", "--tag", "t", "--cmd", "codex --foo", "--cwd", str(self.git.path)])
        self.assertEqual(result.code, 0, result.err)
        record = self._show("w1")
        self.assertEqual(record["engine"], "codex")
        self.assertEqual(record["cmd"], "codex --foo")
        self.assertNotIn("TX_SKILLS", record["env"])
        skills = Path(record["cwd"]) / ".agents" / "skills"
        self.assertEqual(sorted(path.name for path in skills.iterdir()), ["tx-sessions"])

    def test_t_cli_05_missing_hook_shim_refuses_the_spawn(self):
        (self.home.hooks_dir / "claude" / "start.sh").unlink()
        records = self._record_files()
        result = self.tx(["spawn", "w1", "--tag", "t", "--engine", "claude", "--cwd", str(self.git.path)])
        self.assertEqual(result.code, 1)
        self.assertEqual(
            result.err,
            f"tx spawn: claude hooks are not installed in {self.home.path} — the worker's chat id would "
            "never be captured and the session could never be resumed. Install them: "
            "setup/engines/install.sh install --engine claude\n",
        )
        self.assertEqual(self._record_files(), records)
        self.assertEqual(self.git.worktrees(), [str(self.git.path)])

    # ----- T-CLI-06 ---------------------------------------------------------------------------

    @platform_only("linux")
    def test_t_cli_06_read_only_spawn(self):
        result = self.tx(["spawn", "ro", "--tag", "t", "--read-only", "--engine", "claude", "--cwd", str(self.git.path)])
        self.assertEqual(result.code, 0, result.err)
        record = self._show("ro")
        self.assertEqual(record["role"], "llm")
        self.assertEqual(record["env"]["TX_READ_ONLY"], "1")
        self.assertNotIn("TX_REQUIRE_WORKTREE", record["env"])
        tokens = shlex.split(record["cmd"])
        self.assertEqual(tokens[0], "claude")
        for flag in ("--allowedTools", "--disallowedTools", "--setting-sources"):
            self.assertIn(flag, tokens)
        self.assertEqual(tokens[tokens.index("--permission-mode") + 1], "dontAsk")
        self.assertNotIn("--dangerously-skip-permissions", tokens)
        launch = self.tmux.display(record["id"], "#{pane_start_command}").strip('"')
        script = self.home.launch_dir / f"{record['id']}.sh"
        if launch == f"/bin/sh {shlex.quote(str(script))}":
            launch = script.read_text().rstrip("\n")
        launch_tokens = shlex.split(launch)
        self.assertEqual(os.path.basename(launch_tokens[0]), "bwrap")
        self.assertEqual(launch_tokens[launch_tokens.index("--") + 1:], tokens)

    @platform_only("linux")
    def test_t_cli_06_bwrap_absent(self):
        self.fakes.remove("bwrap")
        path = os.pathsep.join(
            [str(self.tmux.bin_dir), str(self.fakes.bin_dir)]
            + [entry for entry in os.environ["PATH"].split(os.pathsep) if not (Path(entry) / "bwrap").exists()]
        )
        result = self.tx(
            ["spawn", "ro", "--tag", "t", "--read-only", "--engine", "claude", "--cwd", str(self.git.path)],
            env={"PATH": path},
        )
        self.assertEqual(result.code, 1)
        self.assertEqual(
            result.err,
            "tx spawn: could not enforce read-only process sandbox: Linux read-only sessions require "
            "bubblewrap (bwrap)\n",
        )
        self.assertEqual(self._record_files(), [])
        self.assertEqual(self.git.worktrees(), [str(self.git.path)])

    # ----- T-CLI-07 ---------------------------------------------------------------------------

    def test_t_cli_07_spawn_nvim_flags_and_output(self):
        cwd = str(self.root)
        nvim_prefix = "nvim +'set background=dark | colorscheme tokyonight-moon'"
        result = self.tx(["spawn-nvim", "ed", "--tag", "t", "--diff", "--cwd", cwd])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, f"Spawned nvim 'ed' (cwd={cwd}, tag=t, diff=main)\n")
        record = self._show("ed")
        self.assertEqual(record["role"], "nvim")
        self.assertTrue(record["cmd"].startswith(nvim_prefix), record["cmd"])
        self.assertIn("+'DiffviewOpen main'", record["cmd"])
        result = self.tx(["spawn-nvim", "ed2", "--tag", "t", "--diff", "origin/x", "--open", "f.txt", "--cwd", cwd])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, f"Spawned nvim 'ed2' (cwd={cwd}, tag=t, diff=origin/x, open=f.txt)\n")
        self.assertTrue(self._show("ed2")["cmd"].endswith("+'DiffviewOpen origin/x' f.txt"))
        result = self.tx(["spawn-nvim", "ed3", "--tag", "t", "--cwd", cwd])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, f"Spawned nvim 'ed3' (cwd={cwd}, tag=t)\n")
        self.assertEqual(self._show("ed3")["cmd"], nvim_prefix)
        # Edges.
        missing = self.tx(["spawn-nvim", "ed4", "--cwd", cwd])
        self.assertEqual(missing.code, 2)
        self.assertTrue(missing.err.endswith("tx spawn-nvim: error: the following arguments are required: --tag\n"))
        empty = self.tx(["spawn-nvim", "ed4", "--tag", "", "--cwd", cwd])
        self.assertEqual(empty.code, 2)
        self.assertTrue(empty.err.endswith("tx spawn-nvim: error: --tag requires at least one value\n"))
        group = self.tx(["spawn-nvim", "ed4", "--tag", "t", "--group", "", "--cwd", cwd])
        self.assertEqual(group.code, 2)
        self.assertTrue(group.err.endswith("tx spawn-nvim: error: argument --group: a group cannot be empty\n"))

    # ----- T-CLI-08 ---------------------------------------------------------------------------

    def test_t_cli_08_spawn_view(self):
        cwd = self.root / "v"
        cwd.mkdir()
        result = self.tx(["spawn-view", "Views", "--cwd", str(cwd)], env={"SHELL": "/bin/bash"})
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, f"Spawned view 'Views' (cwd={cwd})\n")
        self.assertEqual(self._record_files(), [])
        self.assertIn("Views", self.tmux.sessions())
        self.assertEqual(self.tmux.option("Views", "@tx_view"), "1")
        self.assertEqual(self.tmux.option("Views", "status"), "on")
        self.assertEqual(self.tmux.option("Views", "pane-border-status", "window"), "top")
        self.assertEqual(self.tmux.display("Views", "#{pane_start_command}"), "/bin/bash")
        tail = self.log_tail()[0]
        self.assertEqual((tail["type"], tail["msg"]), ("spawn-view", f"Views {cwd}"))
        again = self.tx(["spawn-view", "Views", "--cwd", str(cwd)])
        self.assertEqual((again.code, again.err), (1, "tx spawn-view: session 'Views' already exists\n"))
        self.tx(["spawn", "w1", "--tag", "t", "--cwd", str(cwd)])
        clash = self.tx(["spawn-view", "w1", "--cwd", str(cwd)])
        self.assertEqual((clash.code, clash.err), (1, "tx spawn-view: session 'w1' already exists\n"))
        tagged = self.tx(["spawn-view", "V2", "--cwd", str(cwd), "--tag", "t"])
        self.assertEqual(tagged.code, 2)
        self.assertTrue(tagged.err.endswith("tx spawn-view: error: unrecognized arguments: --tag t\n"), tagged.err)

    # ----- T-CLI-09 ---------------------------------------------------------------------------

    def _ls_fixture(self, w1_created: float, sh1_created: float) -> None:
        now = time.time()
        self.records.llm(id=W1_ID, name="w1", state="working", tags=("a", "b"), created_at=w1_created,
                         last_activity=now - 90, turn_started_at=now - 90)
        self.records.other(id=SH1_ID, name="sh1", tags=(), created_at=sh1_created)
        self.records.llm(name="gone", state="exited", ended_at=now - 5)
        self._live(W1_ID)
        self._live(SH1_ID)
        self._nest_attach(W1_ID)

        [w1_record] = self._record_files()
    def test_t_cli_09_ls_output(self):
        now = time.time()
        self._ls_fixture(now - 600, now - 10)
        vanished = self.records.llm(name="vanished", state="idle")  # no tmux session → reconciled EXITED
        result = self.tx(["ls"])
        # Edge: `--cmd` defaults to `$SHELL`, else `zsh` (the fake `zsh` on PATH keeps the pane alive).
        unset = self.tx(["spawn-view", "V3", "--cwd", str(cwd)], env={"SHELL": None})
        self.assertEqual((unset.code, unset.out), (0, f"Spawned view 'V3' (cwd={cwd})\n"), unset.err)
        self.assertEqual(self.tmux.display("V3", "#{pane_start_command}"), "zsh")
        self.assertEqual(self._record_files(), [w1_record])
        self.assertEqual((result.code, result.err), (0, ""))
        self.assertEqual(
            result.out,
            "PROCESSES\n"
            "  sh1                      alive    —                         —     \n"
            "  w1                       working  main[1]                   1m     [a] [b]\n",
        )
        self.assert_golden("cli/09", result.out)
        self.assertEqual(self.records.load(vanished)["state"], "exited")
        self.assertEqual(self._show("vanished")["state"], "exited")

    def test_t_cli_09_empty_and_stray_argv(self):
        self.assertEqual(self.tx(["ls"]).out, "PROCESSES\n")
        stray = self.tx(["ls", "extra"])
        self.assertEqual(stray.code, 2)
        self.assertTrue(stray.err.endswith("tx ls: error: unrecognized arguments: extra\n"), stray.err)

    # ----- T-CLI-10 ---------------------------------------------------------------------------

    def test_t_cli_10_show_json(self):
        self.records.llm(id=W1_ID, name="w1", state="idle", tags=("a", "b"), cwd="/w1", pid=4242,
                         created_at=1700000000.0, last_activity=1700000100.0)
        self.records.other(id=SH1_ID, name="sh1", cwd="/sh1", pid=4343, created_at=1700000200.0)
        self._live(W1_ID)
        pane_id = self._nest_attach(W1_ID)
        stored = self.records.path(W1_ID).read_bytes()
        result = self.tx(["show", "w1"])
        self.assertEqual((result.code, result.err), (0, ""))
        self.assertEqual(result.out, self.tx(["show", W1_ID]).out)
        record = json.loads(result.out)
        self.assertEqual(list(record), LLM_KEYS)
        self.assertEqual(
            record["attached_to"],
            [{"host": "Views", "window_index": "0", "window_name": "main", "pane_id": pane_id, "pane_index": "1"}],
        )
        self.assertEqual(json.loads(stored)["attached_to"], [])
        self.assertEqual(result.out, json.dumps(record, indent=2) + "\n")
        self.assertEqual(self.records.path(W1_ID).read_bytes(), stored)
        self.assert_golden("cli/10", result.out.replace(pane_id, "%PANE"))
        other = self._show("sh1")
        self.assertEqual(list(other), OTHER_KEYS)
        # Edges: an EXITED record prints its stored `[]`; unknown → stderr.
        exited = self.records.llm(name="e1", state="exited", ended_at=1700000300.0)
        self.assertEqual(self._show(exited)["attached_to"], [])
        unknown = self.tx(["show", "zzz"])
        self.assertEqual((unknown.code, unknown.out, unknown.err), (1, "", "tx show: no record for 'zzz'\n"))

    # ----- T-CLI-11 ---------------------------------------------------------------------------

    def _history_names(self, *argv: str) -> list[str]:
        result = self.tx(["history", *argv])
        self.assertEqual((result.code, result.err), (0, ""), argv)
        self.assertEqual(result.lines[0], "HISTORY")
        if result.lines[1:] == ["  (no exited or archived sessions)"]:
            return []
        return [line.split()[0] for line in result.lines[1:]]

    def test_t_cli_11_history_filters_and_rendering(self):
        self.records.llm(name="e1", state="exited", tags=("x",), cwd="/a/proj", ended_at=_local_epoch(2026, 9, 10, 12, 0))
        self.records.llm(name="e2", state="exited", tags=("y",), cwd="/b/other", ended_at=_local_epoch(2026, 9, 15, 23, 30))
        self.records.llm(name="e3", state="archived", tags=(), cwd="/c", ended_at=_local_epoch(2026, 9, 16, 1, 0))
        live = self.records.llm(name="live", state="idle")
        self._live(live)
        result = self.tx(["history"])
        self.assertEqual((result.code, result.err), (0, ""))
        self.assertEqual(self._history_names(), ["e3", "e2", "e1"])
        row = re.compile(r"^  e1                       exited   +\d+d   1c  \[x\]  /a/proj$")
        self.assertRegex(result.lines[3], row)
        self.assertRegex(result.lines[1], r"^  e3                       archived +\d+d   1c   /c$")
        self.assertEqual(self._history_names("--tag", "x"), ["e1"])
        self.assertEqual(self._history_names("--cwd", "proj"), ["e1"])
        self.assertEqual(self._history_names("--since", "2026-09-15"), ["e3", "e2"])
        self.assertEqual(self._history_names("--until", "2026-09-15"), ["e2", "e1"])
        self.assertEqual(self._history_names("--until", "2026-09-14"), ["e1"])
        bad = self.tx(["history", "--since", "15-09-2026"])
        self.assertEqual(bad.code, 2)
        self.assertTrue(bad.err.endswith("tx history: error: --since expects YYYY-MM-DD, got '15-09-2026'\n"), bad.err)
        none = self.tx(["history", "--tag", "nope"])
        self.assertEqual(none.out, "HISTORY\n  (no exited or archived sessions)\n")

    def test_t_cli_11_null_ended_at_filters_on_activity(self):
        self.records.llm(name="e4", state="exited", tags=(), cwd="/c/x", ended_at=None,
                         last_activity=_local_epoch(2026, 9, 12, 10, 0))
        self.assertEqual(self._history_names("--since", "2026-09-12"), ["e4"])
        self.assertEqual(self._history_names("--until", "2026-09-11"), [])

    # ----- T-CLI-12 ---------------------------------------------------------------------------

    def test_t_cli_12_whoami(self):
        self.records.llm(id=W1_ID, name="w1")
        self._live(W1_ID)
        self.tmux.new_session("raw", "sleep 1000")
        inside = self.tx_inside(W1_ID, ["whoami"])
        self.assertEqual((inside.code, inside.out, inside.err), (0, "w1\n", ""))
        raw = self.tx_inside("raw", ["whoami"])
        self.assertEqual((raw.code, raw.out, raw.err), (0, "raw\n", ""))
        outside = self.tx(["whoami"])
        self.assertEqual((outside.code, outside.out, outside.err), (1, "", "tx whoami: not inside a tmux session\n"))

    # ----- T-CLI-13 ---------------------------------------------------------------------------

    def test_t_cli_13_tag_read_and_set(self):
        self.records.llm(id=W1_ID, name="w1", tags=("a", "b"))
        read = self.tx(["tag", "w1"])
        self.assertEqual((read.code, read.out), (0, "a,b\n"))
        result = self.tx(["tag", "w1", "c,,d"])
        self.assertEqual((result.code, result.out), (0, "Tagged 'w1' (tag=c,,d)\n"))
        self.assertEqual(self.records.load(W1_ID)["tags"], ["c", "d"])
        tail = self.log_tail()[0]
        self.assertEqual((tail["type"], tail["msg"]), ("tag", "w1 c,d"))
        cleared = self.tx(["tag", "w1", ""])
        self.assertEqual((cleared.code, cleared.out), (0, "Tagged 'w1' (tag=)\n"))
        self.assertEqual(self.records.load(W1_ID)["tags"], [])
        self.assertEqual(self.tx(["tag", "w1"]).out, "\n")
        unknown_read = self.tx(["tag", "zzz"])
        self.assertEqual((unknown_read.code, unknown_read.err), (1, "tx tag: session 'zzz' not found\n"))
        unknown_set = self.tx(["tag", "zzz", "t"])
        self.assertEqual(
            (unknown_set.code, unknown_set.err),
            (1, "tx tag: session 'zzz' not found (no live @tx_id, no store record)\n"),
        )

    # ----- T-CLI-14 ---------------------------------------------------------------------------

    def test_t_cli_14_rename(self):
        self.records.llm(id=W1_ID, name="w1")
        w2 = self.records.llm(name="w2")
        self._live(W1_ID)
        self._live(w2)
        self._view()
        result = self.tx(["rename", "w1", "w9"])
        self.assertEqual((result.code, result.out, result.err), (0, "Renamed to 'w9'\n", ""))
        record = self.records.load(W1_ID)
        self.assertEqual((record["name"], record["id"]), ("w9", W1_ID))
        self.assertIn(W1_ID, self.tmux.sessions())
        tail = self.log_tail()[0]
        self.assertEqual((tail["type"], tail["msg"]), ("rename", "w1 → w9"))
        log = self.log_lines()
        same = self.tx(["rename", "w9", "w9"])
        self.assertEqual((same.code, same.out), (0, "Renamed to 'w9'\n"))
        self.assertEqual(self.log_lines(), log)
        clash = self.tx(["rename", "w9", "w2"])
        self.assertEqual((clash.code, clash.err), (1, "tx rename: session 'w2' already exists\n"))
        view = self.tx(["rename", "w9", "Views"])
        self.assertEqual((view.code, view.err), (1, "tx rename: 'Views' is a live view session — pick another name\n"))
        self.records.llm(name="old", state="exited", ended_at=time.time())
        onto_exited = self.tx(["rename", "w9", "old"])
        self.assertEqual((onto_exited.code, onto_exited.out), (0, "Renamed to 'old'\n"))

    # ----- T-CLI-15 ---------------------------------------------------------------------------

    def test_t_cli_15_rm(self):
        self.records.llm(id=W1_ID, name="w1")
        self._live(W1_ID)
        result = self.tx(["rm", "w1"])
        self.assertEqual((result.code, result.out, result.err), (0, "Removed record for 'w1'\n", ""))
        self.assertFalse(self.records.path(W1_ID).exists())
        self.assertIn(W1_ID, self.tmux.sessions())
        tail = self.log_tail()[0]
        self.assertEqual((tail["type"], tail["msg"]), ("rm", f"w1 ({W1_ID})"))
        again = self.tx(["rm", "w1"])
        self.assertEqual((again.code, again.out, again.err), (1, "", "tx rm: no record for 'w1'\n"))

    # ----- T-CLI-16 ---------------------------------------------------------------------------

    def test_t_cli_16_kill_record_and_view_fallback(self):
        self.records.llm(id=W1_ID, name="w1")
        self._live(W1_ID)
        self._view()
        script = self.home.launch_dir / f"{W1_ID}.sh"
        script.write_text("claude\n")
        result = self.tx(["kill", "w1"])
        self.assertEqual((result.code, result.out, result.err), (0, "Killed 'w1'\n", ""))
        self.assertNotIn(W1_ID, self.tmux.sessions())
        record = self.records.load(W1_ID)
        self.assertEqual(record["state"], "exited")
        self.assertIsNotNone(record["ended_at"])
        self.assertEqual(record["attached_to"], [])
        self.assertFalse(script.exists())
        tail = self.log_tail()[0]
        self.assertEqual((tail["type"], tail["msg"]), ("kill", "w1"))
        stored = self.records.path(W1_ID).read_bytes()
        view = self.tx(["kill", "Views"])
        self.assertEqual((view.code, view.out), (0, "Killed 'Views'\n"))
        self.assertNotIn("Views", self.tmux.sessions())
        tail = self.log_tail()[0]
        self.assertEqual((tail["type"], tail["msg"]), ("kill", "Views"))
        self.assertEqual(self.records.path(W1_ID).read_bytes(), stored)
        self.assertEqual(self._record_files(), [f"{W1_ID}.json"])
        again = self.tx(["kill", "w1"])
        self.assertEqual((again.code, again.out), (0, "Killed 'w1'\n"))
        self.assertEqual([line["msg"] for line in self.log_lines() if line["type"] == "kill"], ["w1", "Views", "w1"])

    # ----- T-CLI-17 ---------------------------------------------------------------------------

    def test_t_cli_17_pane_info(self):
        self.records.llm(id="i1", name="w1", tags=("a", "b"))
        self.records.write({"schema_version": 3, "id": "old", "name": "old", "kind": "process"})
        self.assertEqual(self.tx(["_pane-info", "i1"]).out, "w1\na,b\n")
        for token in ("nope", "old", ""):
            result = self.tx(["_pane-info", token])
            self.assertEqual((result.code, result.out, result.err), (0, "\n\n", ""), token)

    # ----- T-CLI-18 ---------------------------------------------------------------------------

    def test_t_cli_18_tmux_name(self):
        self.records.llm(id="i1", name="w1")
        self.records.llm(id="i2", name="w2")
        self._live("i1")
        for token, expected in (("w1", "i1\n"), ("i1", "i1\n"), ("w2", ""), ("nope", "")):
            result = self.tx(["_tmux-name", token])
            self.assertEqual((result.code, result.out, result.err), (0, expected, ""), token)

    def test_t_cli_18_live_same_name_record_wins(self):
        self.records.llm(id=I0_ID, name="w1", state="exited", created_at=time.time() - 100, ended_at=time.time())
        self.records.llm(id="i1", name="w1")
        self._live("i1")
        self.assertEqual(self.tx(["_tmux-name", "w1"]).out, "i1\n")

    # ----- T-CLI-19 ---------------------------------------------------------------------------

    def test_t_cli_19_list_picker_feed(self):
        now = time.time()
        self._ls_fixture(now - 2 * 86400, now - 3 * 3600)
        result = self.tx(["_list"], env={"NAMEW": "30"})
        self.assertEqual((result.code, result.err), (0, ""))
        rows = [line.split("\t") for line in result.out.splitlines()]
        self.assertEqual([row[0] for row in rows], ["w1", "sh1"])  # newest activity first (see NOTES)
        self.assertEqual(rows[0][:4], ["w1", " [a] [b]", "L", W1_ID])
        self.assertEqual(rows[1][:4], ["sh1", "", "L", SH1_ID])
        self.assertEqual(
            rows[0][4],
            f"{'w1':<30}   {'main[1]':<25} {'2d':<7} {'1m':<6} {'llm':<5} [a] [b]",
        )
        self.assertEqual(rows[1][4], f"{'sh1':<30}   {'—':<25} {'3h':<7} {'—':<6} {'shell':<5}")
        self.assert_golden("cli/19", result.raw_out)

    def test_t_cli_19_namew_default_truncation_and_stray_argv(self):
        long_name = "a-very-long-session-name-here"
        record_id = self.records.llm(name=long_name)
        self._live(record_id)
        for namew in (None, "abc"):
            result = self.tx(["_list", "stray"], env={"NAMEW": namew})
            self.assertEqual(result.code, 0, result.err)
            visual = result.lines[0].split("\t")[4]
            self.assertTrue(visual.startswith(long_name[:17] + "…   "), visual)

    # ----- T-CLI-20 ---------------------------------------------------------------------------

    def test_t_cli_20_edit_tag(self):
        self.records.llm(id=W1_ID, name="w1", tags=("a",))
        self.fakes.configure("fzf", stdout="a,b\n")
        result = self.tx(["_edit-tag", "w1"])
        self.assertEqual((result.code, result.err), (0, ""))
        self.assertEqual(self.records.load(W1_ID)["tags"], ["a", "b"])
        tail = self.log_tail()[0]
        self.assertEqual((tail["type"], tail["msg"]), ("tag", "w1 a,b"))
        dump = self.fakes.dumps("fzf")[-1]
        self.assertEqual(dump["argv"][dump["argv"].index("--query") + 1], "a")
        self.fakes.configure("fzf", stdout="x\n", exit_code=1)
        log = self.log_lines()
        cancelled = self.tx(["_edit-tag", "w1"])
        self.assertEqual(cancelled.code, 1)
        self.assertEqual(self.records.load(W1_ID)["tags"], ["a", "b"])
        self.assertEqual(self.log_lines(), log)
        unknown = self.tx(["_edit-tag", "zzz"])
        self.assertEqual((unknown.code, unknown.err), (1, "tx: session 'zzz' not found (not tx-managed)\n"))

    # ----- T-CLI-21 ---------------------------------------------------------------------------

    def test_t_cli_21_focus_envelope(self):
        self.records.llm(id="i1", name="w1", tags=("a", "b"))
        inner_dir = self.root / "inner"
        inner_dir.mkdir()
        self._live("i1", cwd=str(inner_dir))
        pane_dir = self.root / "p"
        pane_dir.mkdir()
        self._view(cwd=str(pane_dir), command=self._attach_command("i1"))
        self.wait_until(lambda: "i1" in self._clients())
        pane = self.tmux.display("Views:main.0", "#{pane_id}")
        self.tmux.run("select-pane", "-t", pane, "-T", "T&'<>", check=True)
        path = os.path.realpath(pane_dir)
        result = self.tx(["focus-envelope", pane])
        self.assertEqual((result.code, result.err), (0, ""))
        self.assertEqual(
            result.out,
            f"<tx-command-prompt session-name='Views' window-index='0' window-name='main' pane-id='{pane}' "
            f"pane-index='0' pane-title='T&amp;&apos;&lt;&gt;' pane-cmd='tmux' pane-path='{path}' "
            "inner-session-name='i1' session-kind='view' inner-session-kind='process' inner-session-tag='a,b'/>",
        )
        # Edge: the pane of a plain process session — kind/tags on the outer, no inner attrs.
        inner_pane = self.tmux.display("i1:0.0", "#{pane_id}")
        self.tmux.run("select-pane", "-t", inner_pane, "-T", "I", check=True)
        self.tmux.run("rename-window", "-t", "i1:0", "0", check=True)  # pin (automatic-rename is on)
        process = self.tx(["focus-envelope", inner_pane])
        self.assertEqual(
            process.out,
            f"<tx-command-prompt session-name='i1' window-index='0' window-name='0' pane-id='{inner_pane}' "
            f"pane-index='0' pane-title='I' pane-cmd='sleep' pane-path='{os.path.realpath(inner_dir)}' "
            "session-kind='process' session-tag='a,b'/>",
        )
        # Edge: an untracked outer session carries no session-kind.
        self.tmux.new_session("raw", "sleep 1000", cwd=str(pane_dir))
        raw_pane = self.tmux.display("raw:0.0", "#{pane_id}")
        self.tmux.run("select-pane", "-t", raw_pane, "-T", "R", check=True)
        self.tmux.run("rename-window", "-t", "raw:0", "0", check=True)
        untracked = self.tx(["focus-envelope", raw_pane])
        self.assertEqual(
            untracked.out,
            f"<tx-command-prompt session-name='raw' window-index='0' window-name='0' pane-id='{raw_pane}' "
            f"pane-index='0' pane-title='R' pane-cmd='sleep' pane-path='{path}'/>",
        )

    def _remote_fixture(self) -> tuple[str, str, str]:
        """The T-CLI-21 topology (`Views` pane nest-attached to process `i1`); returns the nested
        pane's id, its realpath and the envelope's pure-tmux prefix up to `pane-path`."""
        self.records.llm(id="i1", name="w1", tags=("a", "b"))
        pane_dir = self.root / "p"
        pane_dir.mkdir()
        self._live("i1", cwd=str(self.root))
        self._view(cwd=str(pane_dir), command=self._attach_command("i1"))
        self.wait_until(lambda: "i1" in self._clients())
        pane = self.tmux.display("Views:main.0", "#{pane_id}")
        self.tmux.run("select-pane", "-t", pane, "-T", "T", check=True)
        path = os.path.realpath(pane_dir)
        prefix = (
            f"<tx-command-prompt session-name='Views' window-index='0' window-name='main' pane-id='{pane}' "
            f"pane-index='0' pane-title='T' pane-cmd='tmux' pane-path='{path}' "
        )
        return pane, path, prefix

    @expected_failure_on_python
    def test_t_cli_21_fixed_remote_pane_scope(self):
        # Q30 FIX: `@remote-session` at PANE scope (`set-option -p`, what `tx attach --host` writes)
        # → the remote marker replaces the nested join (no inner-kind / inner-tag).
        pane, _, prefix = self._remote_fixture()
        self.tmux.run("set-option", "-p", "-t", pane, "@remote-session", "host", check=True)
        remote = self.tx(["focus-envelope", pane])
        self.assertEqual((remote.code, remote.err), (0, ""))
        self.assertEqual(remote.out, prefix + "inner-remote='1' inner-session-name='host' session-kind='view'/>")

    @python_reference_only
    def test_t_cli_21_parity_remote_session_scope(self):
        # Q30 PARITY (reference-only, D17): `focus_attrs` reads `show-options -vqt <pane>` WITHOUT
        # `-p`, so the pane-scoped stamp is invisible (plain nested join) and the same option at
        # SESSION scope on `Views` yields the remote shape. A Q30-fixed port reads pane scope only.
        pane, _, prefix = self._remote_fixture()
        nested = "inner-session-name='i1' session-kind='view' inner-session-kind='process' inner-session-tag='a,b'/>"
        self.tmux.run("set-option", "-p", "-t", pane, "@remote-session", "host", check=True)
        pane_scoped = self.tx(["focus-envelope", pane])
        self.assertEqual((pane_scoped.code, pane_scoped.err), (0, ""))
        self.assertEqual(pane_scoped.out, prefix + nested)
        self.tmux.run("set-option", "-p", "-u", "-t", pane, "@remote-session", check=True)
        self.tmux.run("set-option", "-t", "Views", "@remote-session", "host", check=True)
        session_scoped = self.tx(["focus-envelope", pane])
        self.assertEqual((session_scoped.code, session_scoped.err), (0, ""))
        self.assertEqual(session_scoped.out, prefix + "inner-remote='1' inner-session-name='host' session-kind='view'/>")

    # ----- T-CLI-24 ---------------------------------------------------------------------------

    def _v3_llm(self, name: str) -> dict:
        record = {
            "schema_version": 3, "id": name, "name": name, "role": "llm", "state": "idle", "cwd": "/x",
            "cmd": SESSION_RECORD_CMD, "tags": [], "env": {}, "parent": None, "pid": None, "attached_to": [],
            "created_at": 1.0, "ended_at": None, "engine": "claude", "last_activity": None, "chats": [],
        }
        return record

    def test_t_cli_24_migrate_output(self):
        self.records.write(self._v3_llm("old"))
        cur = self.records.llm(id="cur")
        art = self.home.artifacts_dir / "art.json"
        art.write_text(json.dumps({
            "artifact_schema_version": 1, "id": "art", "title": None, "filename": "a.md", "created_at": 1.0,
            "history": [{"session_id": "s1", "at": 1.0, "rev": 0, "changes": None}],
        }))
        result = self.tx(["migrate"])
        self.assertEqual((result.code, result.err), (0, ""))
        self.assertEqual(
            result.lines,
            [
                "  migrated old.json → v6",
    def test_t_cli_21_pane_gone(self):
        # Q37 PARITY (tmux-version dependent): only exit 0, no trailing newline and the ABSENCE of
        # the record-join attrs are the contract. tmux 3.4 expands `display-message -p -t %999`
        # with empty fields (exit 0) so the reference prints an all-empty envelope; a port may
        # print nothing (see NOTES-04.md).
        self.tmux.new_session("raw", "sleep 1000")
        result = self.tx(["focus-envelope", "%999"])
        self.assertEqual(result.code, 0, result.err)
        self.assertFalse(result.raw_out.endswith("\n"), result.raw_out)
        self.assertNotIn("session-kind", result.out)
        self.assertNotIn("inner-", result.out)

    # ----- T-CLI-23 ---------------------------------------------------------------------------

    def test_t_cli_23_selfcheck(self):
        result = self.tx(["selfcheck"])
        self.assertEqual((result.code, result.err), (0, ""))
        self.assertEqual(result.out, f"S1a self-check PASSED ✓\n  home={self.home.path}  schema v6  records now=0\n")
        self.assertEqual(self._record_files(), [])
        lines = self.log_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["type"], "selfcheck")
        self.assertRegex(lines[0]["msg"], r"^created s1a-selfcheck \([0-9a-f-]{36}\)$")

                "  skipped  cur.json (already v6)",
                "migrated 1 record(s) to v6; retired 0 view record(s); left 1 untouched.",
                "  migrated art.json → artifact v2",
                "migrated 1 artifact record(s) to v2; left 0 untouched.",
            ],
        )
        self.assertEqual(self.records.load("old")["schema_version"], 6)
        self.assertIn('"schema_version": 6', self.records.path("old").read_text())
        migrated_artifact = json.loads(art.read_text())
        self.assertEqual((migrated_artifact["artifact_schema_version"], migrated_artifact["group"]), (2, None))
        self.assertIn('"artifact_schema_version": 2', art.read_text())
        self.assertIn('"group": null', art.read_text())
        second = self.tx(["migrate"])
        self.assertEqual(
            second.lines,
            [
                "  skipped  cur.json (already v6)",
                "  skipped  old.json (already v6)",
                "migrated 0 record(s) to v6; retired 0 view record(s); left 2 untouched.",
                "  skipped  art.json (already v2)",
                "migrated 0 artifact record(s) to v2; left 1 untouched.",
            ],
        )
        stray = self.tx(["migrate", "--x"])
        self.assertEqual(stray.code, 2)
        self.assertTrue(stray.err.endswith("tx migrate: error: unrecognized arguments: --x\n"), stray.err)

    def test_t_cli_24_migrate_retires_a_v3_view_record(self):
        self.records.write({**self._v3_llm("Views"), "kind": "view", "role": "shell"})
        self.tmux.new_session("Views", "sleep 1000")
        result = self.tx(["migrate"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.lines[0], "  view     Views → stamped @tx_view, record removed")
        self.assertEqual(result.lines[1], "migrated 0 record(s) to v6; retired 1 view record(s); left 0 untouched.")
        self.assertFalse(self.records.path("Views").exists())
        self.assertEqual(self.tmux.option("Views", "@tx_view"), "1")

    # ----- T-CLI-25 ---------------------------------------------------------------------------

    def test_t_cli_25_start(self):
        spawned = self.tx(["spawn", "tx-assistant", "--tag", "tx-system", "--cwd", str(self.root)])
        self.assertEqual(spawned.code, 0, spawned.err)
        assistant = self._show("tx-assistant")
        result = self.tx(["start"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, "tx-assistant already running.\nViews session created.\n")
        self.assertIn("open terminal failed", result.err)
        self.assertIn("Views", self.tmux.sessions())
        self.assertEqual(self.tmux.option("Views", "@tx_view"), "1")
        checkout = os.path.realpath(REPO)
        self.assertEqual(os.path.realpath(self.tmux.display("Views", "#{pane_current_path}")), checkout)
        tail = self.log_tail()[0]
        self.assertEqual((tail["type"], tail["msg"]), ("spawn-view", f"Views {checkout}"))
        self.assertNotIn("tx-assistant", self.tmux.sessions())  # liveness target is the record's id
        self.assertIn(assistant["id"], self.tmux.sessions())
        log = self.log_lines()
        again = self.tx(["start"])
        self.assertEqual((again.code, again.out), (0, "tx-assistant already running.\n"))
        self.assertEqual(self.log_lines(), log)
        self.assertEqual(self.tmux.sessions().count("Views"), 1)

    # ----- T-CLI-26 ---------------------------------------------------------------------------

    def _resume_fixture(self) -> str:
        w1 = self.records.llm(name="w1")
        self._live(w1)
        chat = self.records.chat_ref(session_id=E1_ID, id="c1", cwd="/gone", transcript_path="/gone/c1.jsonl")
        self.records.llm(id=E1_ID, name="e1", state="exited", cwd="/gone", chats=[chat], ended_at=time.time())
        live_e1 = self.records.llm(name="e1")
        self._live(live_e1)
        return w1

    def _assert_refused(self, argv: list[str], message: str, *, code: int = 1) -> None:
        records, sessions = self._record_files(), self.tmux.sessions()
        views_before = self.tmux.display("Views", "#{session_id} #{session_created} #{pane_id} #{pane_pid}")
        result = self.tx(argv)
        self.assertEqual((result.code, result.out, result.err), (code, "", message), argv)
        self.assertIn("open terminal failed", again.err)
        self.assertEqual(self._record_files(), records)
        self.assertEqual(self.tmux.sessions(), sessions)
        self.assertEqual(self.tmux.display("Views", "#{session_id} #{session_created} #{pane_id} #{pane_pid}"), views_before)
        # Edge: with `$TMUX` set the trailing step is `switch-client -t Views`, not `attach`: the
        # outer client attached to `raw` is moved onto `Views` and no attach error is printed.
        self.tmux.new_session("raw", "sleep 1000")
        client = self.attach_client("raw")
        self.assertEqual([row["client_session"] for row in self.tmux.clients()], ["raw"])
        inside = self.tx_inside("raw", ["start"])
        self.assertEqual((inside.code, inside.out, inside.err), (0, "tx-assistant already running.\n", ""))
        self.wait_until(lambda: [row["client_session"] for row in self.tmux.clients()] == ["Views"])
        self.assertEqual(self.log_lines(), log)
        self.assertEqual(self.tmux.display("Views", "#{session_id} #{session_created} #{pane_id} #{pane_pid}"), views_before)
        client.close()

    def test_t_cli_26_parity_chat_ls_and_resume_guards(self):
        self._resume_fixture()
        self._assert_refused(["chat", "ls", "zzz"], "tx chat ls: session 'zzz' not found\n")
        self._assert_refused(["resume", "zzz"], "tx resume: no record for 'zzz'\n")
        self._assert_refused(
            ["resume", "w1"], "tx resume: 'w1' has no chat to resume — use `tx spawn` for a fresh session\n"
        )
        self._assert_refused(
            ["resume", E1_ID, "--as", "e2"], "tx resume: cwd '/gone' does not exist — pass --cwd <dir> (C8)\n"
        )
        self._assert_refused(
            ["resume", E1_ID, "--as", "e2", "--cwd", "/also-gone"],
            "tx resume: cwd '/also-gone' does not exist — '/also-gone' is not a directory (C8)\n",
        )

    @expected_failure_on_python
    def test_t_cli_26_fixed_live_name_clash_consults_store(self):
        # Q6 FIX: the clash check consults the store, so a live RECORD named `e1` refuses the resume.
        self._resume_fixture()
        self._assert_refused(
            ["resume", E1_ID], "tx resume: a live session named 'e1' already exists — pass --as <new-name>\n"
        )

    # ----- T-CLI-27 ---------------------------------------------------------------------------

    def test_t_cli_27_attach_all_unsupported(self):
        result = self.tx(["attach", "--all"])
        self.assertEqual((result.code, result.out), (2, ""))
        self.assertEqual(
            result.err,
            "tx attach --all (a merged local+remote picker) is not supported; use `tx attach --host ALIAS` "
            "to attach a remote host's sessions over ssh.\n",
        )

    def test_t_cli_27_attach_host_runs_ssh(self):
        argv_dump = self.root / "ssh-argv"
        remote_dump = self.root / "ssh-remote"
        ssh = self.fakes.bin_dir / "ssh"
        ssh.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$@\" > {shlex.quote(str(argv_dump))}\n"
            f"tmux show-options -p -t \"$TMUX_PANE\" -v @remote-session > {shlex.quote(str(remote_dump))} 2>&1\n"
            "exit 3\n"
        )
        ssh.chmod(0o755)
        self.tmux.new_session("raw", "sleep 1000")
        pane = self.tmux.display("raw", "#{pane_id}")
        result = self.tx_inside("raw", ["attach", "--host"])
        self.assertEqual(result.code, 3, result.err)
        self.assertEqual(
            argv_dump.read_text().splitlines(),
            ["-t", "personal", '$SHELL -lc "if command -v tx >/dev/null 2>&1; then tx attach; else tmux attach; fi"'],
        )
        self.assertEqual(remote_dump.read_text(), "personal\n")
        self.assertIsNone(self.tmux.option(pane, "@remote-session", "pane"))


class TestCliInitHome(TxCase):
    home_options = {"skeleton": False, "link_agents": False, "hooks": ()}

    def test_t_cli_22_init_home(self):
        self.assertFalse(self.home.path.exists())
        result = self.tx(["_init-home"])
        self.assertEqual((result.code, result.err), (0, ""))
        self.assertEqual(result.out, f"initialized $TX_IDE_HOME skeleton at {self.home.path}\n")
        self.assertEqual(self.home.entries(), sorted(HOME_DIRS))
        again = self.tx(["_init-home"])
        self.assertEqual((again.code, again.out), (0, f"initialized $TX_IDE_HOME skeleton at {self.home.path}\n"))
        self.assertEqual(self.home.entries(), sorted(HOME_DIRS))
        self.assertFalse(self.home.agents.exists())
        self.assertFalse(self.home.hooks_dir.exists())
