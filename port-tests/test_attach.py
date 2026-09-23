"""ATTACH — `tx attach` (the fzf picker), `tx _list`, `tx _edit-tag` (spec section 02, T-ATTACH-01..12).

Every picker case runs against the kit's fake `fzf` (argv + env + stdin dump; `sequence` to accept a
row once). Inside-tmux legs open the picker in a `display-popup` on a real outer pty client; the
outside-tmux legs run `tx attach` on its own pty.

Hermeticity guard: the private server's GLOBAL environment is whatever process started it. A popup
or an in-pane command inherits that environment, so if a raw `tmux new-session` from the test
process were the first server call, the popup's `tx` would run against the operator's real home.
Every test here starts the server through `self.tx` and `_popup` asserts the server env is the
temp home before launching anything in it.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time

from txkit import REAL_TMUX, TX_BIN, PtyProcess, TxCase

# lib/tx/palette.py literals (shared/palette.sh) as they appear in the fzf binds.
BOLD = "\x1b[1m"
RESET = "\x1b[0m"
ACCENT_ANSI = "\x1b[38;2;122;162;247m"
FG_ANSI = "\x1b[38;2;192;202;245m"
WARN_ANSI = "\x1b[38;2;224;175;104m"
RESET_FG = "\x1b[39m"
LOCATION_W = 25
ROLE_W = 5
FZF_COLOR = (
    "fg:#a9b1d6,pointer:#7aa2f7,fg+:#a9b1d6:regular,bg+:236:regular,hl:#7aa2f7,hl+:#7aa2f7,"
    "header:#a9b1d6,footer:#a9b1d6,prompt:#a9b1d6,query:#c0caf5"
)
EIGHTEEN = "eighteen-char-name"  # len 18 — the T-ATTACH-01 longest live name
LONG_NAME = "a-very-long-session-name-xyz"  # len 28 — T-ATTACH-04


def header_columns(namew: int) -> str:
    return f"{'NAME':<{namew}}   {'LOCATION':<{LOCATION_W}} {'STARTED':<7} {'IDLE':<6} {'ROLE':<{ROLE_W}} TAGS"


def picker_row(name: str, target: str, chips: str = " [t]") -> str:
    """A minimal five-field feed row the fake fzf hands back as the selection."""
    return f"{name}\t{chips}\tL\t{target}\t{name} visual\n"


def mask_ages(row: str, namew: int) -> str:
    """Blank the STARTED (7) and IDLE (6) cells of a feed row's visual field — the only cells that
    differ between two renders of the same sessions."""
    fields = row.split("\t")
    visual = fields[4]
    started_at = namew + 3 + LOCATION_W + 1
    idle_at = started_at + 7 + 1 + len(WARN_ANSI)
    visual = visual[:started_at] + "#" * 7 + visual[started_at + 7 : idle_at] + "#" * 6 + visual[idle_at + 6 :]
    return "\t".join([*fields[:4], visual])


def unquote_start_command(value: str) -> str:
    """tmux renders a `respawn-pane` command back through its own quoting (3.4: the whole command in
    double quotes, `$` and `"` backslash-escaped); undo that so the raw command compares exactly."""
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    return re.sub(r'\\+([$"])', r'\1', value)


class TestAttach(TxCase):
    # ----- helpers -------------------------------------------------------------------------

    def attach_detached(self, argv: list[str], extra: dict[str, str | None] | None = None) -> subprocess.CompletedProcess:
        """`tx attach …` with no controlling tty (`setsid -w`), so the picker sizes from `$COLUMNS`."""
        return subprocess.run(
            ["setsid", "-w", TX_BIN, "attach", *argv],
            env=self.env(extra),
            capture_output=True,
            text=True,
            input="",
            cwd=self.root,
            timeout=60,
        )

    def assert_hermetic_server(self) -> None:
        """The server's global env must be the temp home's before anything runs inside it."""
        environment = self.tmux.run("show-environment", "-g", check=True).stdout
        self.assertIn(f"TX_IDE_HOME={self.home.path}\n", environment)
        self.assertIn(f"FAKE_OUT={self.fakes.out_dir}\n", environment)
        path_line = next(line for line in environment.splitlines() if line.startswith("PATH="))
        self.assertTrue(path_line.startswith(f"PATH={self.tmux.bin_dir}{os.pathsep}"), path_line)

    def popup(self, client: PtyProcess | str, command: str) -> subprocess.Popen:
        """Open `command` in a `display-popup -E` on the outer client (the prefix+t path). The tmux
        call blocks until the popup closes, so it runs detached; a case that keeps the popup open
        (a foreground attach inside it) closes it itself and waits on the returned process."""
        self.assert_hermetic_server()
        tty = client if isinstance(client, str) else client.tty
        process = subprocess.Popen(
            [REAL_TMUX, "-L", self.tmux.socket, "-f", "/dev/null", "display-popup", "-E", "-c", tty, command],
            env=self.env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(process.kill)
        return process

    def client_on(self, tty: str) -> dict[str, str] | None:
        return next((row for row in self.tmux.clients() if row["client_tty"] == tty), None)

    def fzf_argv(self, index: int = -1) -> list[str]:
        return self.fakes.dumps("fzf")[index]["argv"][1:]

    def bind(self, argv: list[str], key: str) -> str:
        prefix = f"--bind={key}:"
        return next(argument for argument in argv if argument.startswith(prefix))[len(prefix):]

    def nest(self, pane: str, session_id: str) -> None:
        self.assert_hermetic_server()
        self.tmux.nest_attach(pane, session_id)

    # ----- T-ATTACH-01 fzf argv ------------------------------------------------------------

    def test_t_attach_01_fzf_argv(self):
        self.spawn_process("short")
        self.spawn_process(EIGHTEEN)
        self.fakes.configure("fzf", exit_code=130)
        result = self.attach_detached(["-f", "q"], {"COLUMNS": "118"})
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        dump = self.fakes.dumps("fzf")[0]
        argv = dump["argv"][1:]
        namew = 18
        header = header_columns(namew)
        self.assertEqual(header, "NAME                 LOCATION                  STARTED IDLE   ROLE  TAGS")
        self.assertEqual(
            argv[:13],
            [
                "--exact", "--ansi", "--prompt=  ❯ ", "--height=100%", "--reverse", "--delimiter=\t",
                "--with-nth=5..", "--listen", "--track", f"--color={FZF_COLOR}", f"--header={header}",
                "--query=q",
                # start bind
                "--bind=start:execute-silent(( while sleep 1; do "
                f'curl -fsS -XPOST "localhost:$FZF_PORT" -d "reload-sync({TX_BIN} _list)" >/dev/null 2>&1 || exit 0; '
                "done ) &)+unbind(y,n)",
            ],
        )
        self.assertEqual(argv[13], f"--bind=ctrl-r:reload-sync({TX_BIN} _list)")
        keys = [argument.split(":", 1)[0].removeprefix("--bind=") for argument in argv if argument.startswith("--bind=")]
        self.assertEqual(keys, ["start", "ctrl-r", "focus", "ctrl-t", "ctrl-d", "y", "n"])
        self.assertEqual(len(argv), 19)
        # stdin == `NAMEW=18 tx _list` (STARTED/IDLE excepted)
        self.assertEqual(dump["env"]["NAMEW"], "18")
        feed = self.tx(["_list"], env={"NAMEW": "18"}).raw_out
        self.assertEqual(
            [mask_ages(row, namew) for row in dump["stdin"].splitlines()],
            [mask_ages(row, namew) for row in feed.splitlines()],
        )
        # header geometry with a narrow terminal: NAME column 12 wide
        self.fakes.configure("fzf", exit_code=130)
        self.attach_detached([], {"COLUMNS": "60"})
        narrow = self.fzf_argv()
        self.assertIn(f"--header={header_columns(12)}", narrow)
        self.assertEqual(header_columns(12)[:15], "NAME           ")

    # ----- T-ATTACH-02 ctrl-d / y / n arm flow ----------------------------------------------

    def test_t_attach_02_arm_flow_binds(self):
        self.spawn_process("w")
        self.fakes.configure("fzf", exit_code=130)
        result = self.attach_detached([], {"COLUMNS": "118"})
        self.assertEqual(result.returncode, 0, result.stderr)
        dump = self.fakes.dumps("fzf")[0]
        argv = dump["argv"][1:]
        header = header_columns(12)
        focus_cmd = f"printf '{ACCENT_ANSI}{BOLD}%s{RESET} {FG_ANSI}{BOLD}%s{RESET}\\n%s' {{1}} {{2}} '{header}'"
        self.assertEqual(
            self.bind(argv, "ctrl-d"),
            "execute-silent(printf '%s' {1} >\"$TX_ARM_FILE\")"
            f"+transform-header(printf '{WARN_ANSI}{BOLD} ⚠  Kill \"%s\"? [y/N]{RESET}\\n%s' {{1}} '{header}')"
            "+rebind(y,n)",
        )
        self.assertEqual(
            self.bind(argv, "y"),
            f'execute-silent({TX_BIN} kill {{1}} >/dev/null 2>&1; : >"$TX_ARM_FILE")'
            f"+reload-sync({TX_BIN} _list)+unbind(y,n)",
        )
        self.assertEqual(
            self.bind(argv, "n"),
            f'transform-header({focus_cmd})+execute-silent(: >"$TX_ARM_FILE")+unbind(y,n)',
        )
        self.assertEqual(
            self.bind(argv, "focus"),
            f'transform-header({focus_cmd})+execute-silent(: >"$TX_ARM_FILE")+unbind(y,n)',
        )
        # literal placeholders survive; `\n` is two characters; square brackets around y/N
        for text in ("$TX_ARM_FILE", "$FZF_PORT", "{1}", "{2}", "\\n", "[y/N]"):
            self.assertTrue(any(text in argument for argument in argv), text)
        self.assertFalse(any("(y/N)" in argument for argument in argv))
        # the arm file is set and exists (empty) during the run
        arm_file = dump["env"]["TX_ARM_FILE"]
        self.assertTrue(arm_file.startswith("/"))
        self.assertFalse(os.path.exists(arm_file))  # removed on exit (existence during the run: T-ATTACH-05)

    # ----- T-ATTACH-03 ctrl-t popup + tx _edit-tag ------------------------------------------

    def test_t_attach_03_ctrl_t_bind_and_edit_tag(self):
        record = self.spawn_process("w", tag="a,b")
        self.fakes.configure("fzf", exit_code=130)
        self.attach_detached([], {"COLUMNS": "118"})
        bind = self.bind(self.fzf_argv(), "ctrl-t")
        prefix = f'execute(tmux display-popup -E -h 5 -w 60% "env TX_IDE_HOME={self.home.path} '
        suffix = f' _edit-tag {{1}}")+reload-sync({TX_BIN} _list)'
        self.assertTrue(bind.startswith(prefix), bind)
        self.assertTrue(bind.endswith(suffix), bind)

        # accepted `c,d`
        self.fakes.configure("fzf", stdout="c,d\n", exit_code=0)
        before = len(self.log_lines())
        result = self.tx(["_edit-tag", "w"])
        self.assertEqual((result.code, result.out, result.err), (0, "", ""))
        self.assertEqual(
            self.fzf_argv(),
            ["--disabled", "--query", "a,b", "--prompt", "Tags for w: ",
             "--header", "Enter: accept field. Esc / Ctrl-C: cancel.",
             "--reverse", "--no-info", "--no-separator", "--bind", "enter:accept-or-print-query"],
        )
        self.assertEqual(self.records.load(record["id"])["tags"], ["c", "d"])
        tail = self.log_tail()[0]
        self.assertEqual((tail["type"], tail["msg"]), ("tag", "w c,d"))
        self.assertEqual(len(self.log_lines()), before + 1)

        # fzf cancelled (exit 1): tags untouched, no log line
        self.fakes.configure("fzf", exit_code=1)
        before = len(self.log_lines())
        result = self.tx(["_edit-tag", "w"])
        self.assertEqual(result.code, 1)
        self.assertEqual(self.records.load(record["id"])["tags"], ["c", "d"])
        self.assertEqual(len(self.log_lines()), before)
        self.assertEqual(self.fzf_argv()[2], "c,d")

        # an accepted empty line clears the tags
        self.fakes.configure("fzf", stdout="\n", exit_code=0)
        result = self.tx(["_edit-tag", "w"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self.records.load(record["id"])["tags"], [])
        tail = self.log_tail()[0]
        self.assertEqual((tail["type"], tail["msg"]), ("tag", "w "))

        # unknown session
        dumps_before = len(self.fakes.dumps("fzf"))
        result = self.tx(["_edit-tag", "nope"])
        self.assertEqual((result.code, result.out, result.err), (1, "", "tx: session 'nope' not found (not tx-managed)\n"))
        self.assertEqual(len(self.fakes.dumps("fzf")), dumps_before)

    # ----- T-ATTACH-04 NAMEW export + _list ------------------------------------------------

    def test_t_attach_04_namew_and_list_feed(self):
        self.spawn_process("abc")
        long_record = self.spawn_process(LONG_NAME)
        self.fakes.configure("fzf", exit_code=130)
        result = self.attach_detached([], {"COLUMNS": "118"})
        self.assertEqual(result.returncode, 0, result.stderr)
        dump = self.fakes.dumps("fzf")[0]
        self.assertEqual(dump["env"]["NAMEW"], "28")
        feed = self.tx(["_list"], env={"NAMEW": "28"}).raw_out
        self.assertEqual(
            [mask_ages(row, 28) for row in dump["stdin"].splitlines()],
            [mask_ages(row, 28) for row in feed.splitlines()],
        )
        # floor 12 at COLUMNS=60; no COLUMNS under setsid → width 80 → ceiling 15
        self.attach_detached([], {"COLUMNS": "60"})
        self.assertEqual(self.fakes.dumps("fzf")[-1]["env"]["NAMEW"], "12")
        self.attach_detached([], {"COLUMNS": None})
        self.assertEqual(self.fakes.dumps("fzf")[-1]["env"]["NAMEW"], "15")
        # `tx _list` standalone: NAMEW unset → 18, NAMEW=abc → 18
        for extra in ({}, {"NAMEW": "abc"}):
            rows = self.tx(["_list"], env=extra).raw_out.splitlines()
            top = rows[0].split("\t")
            self.assertEqual(len(top), 5)
            self.assertEqual(top[4][:18 + 3], f"{LONG_NAME[:17]}…" + "   ")

    def test_t_attach_04_list_row_shape(self):
        abc = self.spawn_process("abc", tag="t")
        time.sleep(0)  # ordering is by created_at; the second spawn is newer
        long_record = self.spawn_process(LONG_NAME, tag="a,b")
        exited = self.spawn_process("gone")
        self.assertEqual(self.tx(["kill", "gone"]).code, 0)
        self.spawn_view("Views")
        rows = self.tx(["_list"], env={"NAMEW": "28"}).raw_out.splitlines()
        self.assertEqual([row.split("\t")[0] for row in rows], [LONG_NAME, "abc"])  # newest first; exited/view absent
        fields = rows[0].split("\t")
        self.assertEqual(fields[:4], [LONG_NAME, " [a] [b]", "L", long_record["id"]])
        visual = fields[4]
        self.assertTrue(visual.startswith(f"{LONG_NAME:<28}   {'—':<{LOCATION_W}} "), visual)
        started = visual[28 + 3 + LOCATION_W + 1 :]
        self.assertRegex(started[:7], r"^\d+s    $|^\d+s     $|^\d+[smhd]\s+$")
        after_started = started[8:]
        self.assertTrue(after_started.startswith(f"{WARN_ANSI}{'—':<6}{RESET_FG} "), repr(after_started))
        role_and_tags = after_started[len(WARN_ANSI) + 6 + len(RESET_FG) + 1 :]
        self.assertRegex(role_and_tags, r"^\x1b\[38;5;\d+mother\x1b\[39m \x1b\[38;5;\d+m\[a\]\x1b\[39m \x1b\[38;5;\d+m\[b\]\x1b\[39m$")
        self.assertEqual(rows[1].split("\t")[3], abc["id"])
        self.assertEqual(exited["id"] in rows[1], False)

    # ----- T-ATTACH-05 arm file lifecycle --------------------------------------------------

    def test_t_attach_05_arm_file_lifecycle(self):
        scratch = self.root / "scratch-tmp"
        scratch.mkdir()
        record = self.spawn_process("n")
        report = self.root / "arm-report.txt"
        counter = self.root / "fzf-runs"
        # A hand-written fzf: record $TX_ARM_FILE + whether it exists (and its size); first run prints a
        # row for a dead target, every later run exits 130.
        self.fakes.write_script(
            "fzf",
            "#!/bin/sh\n"
            f'runs=$(cat {counter} 2>/dev/null || echo 0); runs=$((runs + 1)); echo $runs > {counter}\n'
            f'if [ -e "$TX_ARM_FILE" ]; then state="exists:$(stat -c %s "$TX_ARM_FILE")"; else state=missing; fi\n'
            f'printf "%s %s\\n" "$TX_ARM_FILE" "$state" >> {report}\n'
            'if [ "$runs" = 1 ]; then printf "n\\t [t]\\tL\\tdead-id\\tvisual\\n"; exit 0; fi\n'
            "exit 130\n",
        )
        result = self.tx(["attach"], env={"TMPDIR": str(scratch)})
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.err, "tx: session 'n' does not exist\n")
        lines = report.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        first_path, first_state = lines[0].split(" ")
        self.assertRegex(first_path, rf"^{scratch}/tx-kill-arm\.[A-Za-z0-9_]+$")
        self.assertEqual(first_state, "exists:0")
        self.assertEqual(lines[1].split(" ")[0], first_path)
        self.assertFalse(os.path.exists(first_path))
        self.assertEqual(sorted(scratch.iterdir()), [])
        # a second run gets a fresh name
        counter.unlink()
        result = self.tx(["attach"], env={"TMPDIR": str(scratch)})
        self.assertEqual(result.code, 0, result.err)
        second_path = report.read_text().splitlines()[2].split(" ")[0]
        self.assertNotEqual(second_path, first_path)
        self.assertFalse(os.path.exists(second_path))

    # ----- T-ATTACH-06 selection uses row field 4 -------------------------------------------

    def test_t_attach_06_selection_uses_row_target(self):
        live = self.spawn_process("n")
        husk = self.records.llm(name="n", state="exited", created_at=time.time() + 5, ended_at=time.time() + 6)
        self.fakes.configure("fzf", sequence=[{"stdout": picker_row("n", live["id"]), "exit_code": 0}, {"exit_code": 130}])
        process = self.tx_pty(["attach"])
        client = self.wait_until(lambda: self.client_on(process.tty))
        self.assertEqual(client["client_session"], live["id"])
        self.assertNotIn(husk, self.tmux.sessions())
        self.tmux.run("detach-client", "-t", process.tty, check=True)
        self.assertEqual(process.wait(), 0)
        self.assertEqual(len(self.fakes.dumps("fzf")), 1)

    def test_t_attach_06_row_without_target_resolves_live_record(self):
        live = self.spawn_process("n")
        self.records.llm(name="n", state="exited", created_at=time.time() + 5, ended_at=time.time() + 6)
        self.fakes.configure("fzf", sequence=[{"stdout": "n\t\tL\t\tvisual\n", "exit_code": 0}, {"exit_code": 130}])
        process = self.tx_pty(["attach"])
        client = self.wait_until(lambda: self.client_on(process.tty))
        self.assertEqual(client["client_session"], live["id"])
        self.tmux.run("detach-client", "-t", process.tty, check=True)
        self.assertEqual(process.wait(), 0)

    def test_t_attach_06_untracked_name_row(self):
        self.spawn_process("n")  # starts the server hermetically before the raw session
        self.tmux.new_session("plainx", "sleep 300")
        self.fakes.configure("fzf", sequence=[{"stdout": "plainx\t\tL\t\tvisual\n", "exit_code": 0}, {"exit_code": 130}])
        process = self.tx_pty(["attach"])
        client = self.wait_until(lambda: self.client_on(process.tty))
        self.assertEqual(client["client_session"], "plainx")
        self.tmux.run("detach-client", "-t", process.tty, check=True)
        self.assertEqual(process.wait(), 0)

    def test_t_attach_06_abort_and_dead_target(self):
        self.spawn_process("n")
        # fzf exit ≠ 0 → exit 0 after one run
        self.fakes.configure("fzf", exit_code=130)
        result = self.tx(["attach"])
        self.assertEqual((result.code, result.out, result.err), (0, "", ""))
        self.assertEqual(len(self.fakes.dumps("fzf")), 1)
        # empty stdout with exit 0 → exit 0 after one run
        self.fakes.configure("fzf", stdout="\n", exit_code=0)
        result = self.tx(["attach"])
        self.assertEqual((result.code, result.err), (0, ""))
        self.assertEqual(len(self.fakes.dumps("fzf")), 2)
        # a row whose field 4 is a dead id → message, fzf runs again, exit 0 on 130
        self.fakes.configure("fzf", sequence=[{"stdout": picker_row("n", "dead-id"), "exit_code": 0}, {"exit_code": 130}])
        started = time.monotonic()
        result = self.tx(["attach"])
        elapsed = time.monotonic() - started
        self.assertEqual((result.code, result.out, result.err), (0, "", "tx: session 'n' does not exist\n"))
        self.assertEqual(len(self.fakes.dumps("fzf")), 4)
        self.assertGreater(elapsed, 1.0)

    # ----- T-ATTACH-07 nest-attach respawn command -----------------------------------------

    def test_t_attach_07_nest_attach_into_view_pane(self):
        self.spawn_view("Views")
        record = self.spawn_process("U2")
        client = self.attach_client("Views")
        pane = self.tmux.pane_id("Views")
        self.assertEqual(self.tmux.display(pane, "#{pane_current_command}"), "bash")
        old_pid = self.tmux.display(pane, "#{pane_pid}")
        self.fakes.configure("fzf", sequence=[{"stdout": picker_row("U2", record["id"]), "exit_code": 0}, {"exit_code": 130}])
        popup = self.popup(client, "tx attach")
        self.wait_until(lambda: self.tmux.display(pane, "#{pane_current_command}") == "tmux")
        self.assertEqual(popup.wait(timeout=10), 0)
        self.assertEqual(
            unquote_start_command(self.tmux.display(pane, "#{pane_start_command}")),
            f"bash -c 'set -m; TMUX= tmux attach -t {record['id']}; exec ${{SHELL:-zsh}}'",
        )
        self.assertNotEqual(self.tmux.display(pane, "#{pane_pid}"), old_pid)
        pane_tty = self.tmux.pane_tty(pane)
        nested = self.wait_until(lambda: self.client_on(pane_tty))
        self.assertEqual(nested["client_session"], record["id"])
        window_name = self.tmux.display(pane, "#{window_name}")
        pane_index = self.tmux.display(pane, "#{pane_index}")
        listing = self.tx(["ls"]).out
        self.assertIn(f"  U2                       alive    {window_name}[{pane_index}]", listing)
        shown = json.loads(self.tx(["show", "U2"]).out)
        self.assertEqual(shown["attached_to"], [{"host": "Views", "window_index": "0", "window_name": window_name, "pane_id": pane, "pane_index": "0"}])
        # the Views client itself did not switch
        self.assertEqual(self.client_on(client.tty)["client_session"], "Views")
        self.assertEqual(len(self.fakes.dumps("fzf")), 1)

    def test_t_attach_07_untracked_target_is_quoted(self):
        self.spawn_view("Views")
        self.spawn_process("U2")
        self.tmux.new_session("a b", "sleep 300")
        client = self.attach_client("Views")
        pane = self.tmux.pane_id("Views")
        self.fakes.configure("fzf", sequence=[{"stdout": "a b\t\tL\t\tvisual\n", "exit_code": 0}, {"exit_code": 130}])
        popup = self.popup(client, "tx attach")
        self.wait_until(lambda: self.tmux.display(pane, "#{pane_current_command}") == "tmux")
        # the wrapper quotes the target (`tmux attach -t 'a b'`) and is itself shlex-quoted for `bash -c`
        wrapper = "set -m; TMUX= tmux attach -t 'a b'; exec ${SHELL:-zsh}"
        self.assertEqual(
            unquote_start_command(self.tmux.display(pane, "#{pane_start_command}")),
            f"bash -c {shlex.quote(wrapper)}",
        )
        nested = self.wait_until(lambda: self.client_on(self.tmux.pane_tty(pane)))
        self.assertEqual(nested["client_session"], "a b")
        self.assertEqual(popup.wait(timeout=10), 0)

    def test_t_attach_07_non_shell_pane_falls_through_to_switch_client(self):
        # the view pane runs the fake nvim (a non-shell `pane_current_command`)
        self.spawn_view("Views", cmd="nvim")
        record = self.spawn_process("U2")
        client = self.attach_client("Views")
        pane = self.tmux.pane_id("Views")
        self.assertNotIn(self.tmux.display(pane, "#{pane_current_command}"), ("bash", "zsh", "sh", "fish", "dash"))
        start_command = self.tmux.display(pane, "#{pane_start_command}")
        self.fakes.configure("fzf", sequence=[{"stdout": picker_row("U2", record["id"]), "exit_code": 0}, {"exit_code": 130}])
        popup = self.popup(client, "tx attach")
        self.wait_until(lambda: self.client_on(client.tty)["client_session"] == record["id"])
        self.assertEqual(popup.wait(timeout=10), 0)
        self.assertEqual(self.tmux.display(pane, "#{pane_start_command}"), start_command)
        self.assertIsNone(self.client_on(self.tmux.pane_tty(pane)))

    def test_t_attach_07_tmux_unset_falls_through_to_foreground_attach(self):
        self.spawn_view("Views")
        record = self.spawn_process("U2")
        client = self.attach_client("Views")
        pane = self.tmux.pane_id("Views")
        start_command = self.tmux.display(pane, "#{pane_start_command}")
        self.fakes.configure("fzf", sequence=[{"stdout": picker_row("U2", record["id"]), "exit_code": 0}, {"exit_code": 130}])
        popup = self.popup(client, "env -u TMUX tx attach")
        # no nest-attach, no switch: a foreground `tmux attach` on the popup's own pty
        foreground = self.wait_until(
            lambda: next((row for row in self.tmux.clients()
                          if row["client_session"] == record["id"] and row["client_tty"] != client.tty), None)
        )
        self.assertNotEqual(foreground["client_tty"], self.tmux.pane_tty(pane))
        self.assertEqual(self.tmux.display(pane, "#{pane_start_command}"), start_command)
        self.assertEqual(self.client_on(client.tty)["client_session"], "Views")
        # detaching the foreground client ends the picker, which closes the popup
        self.tmux.run("detach-client", "-t", foreground["client_tty"], check=True)
        self.assertEqual(popup.wait(timeout=10), 0)

    # ----- T-ATTACH-08 switch-client vs foreground attach ----------------------------------

    def test_t_attach_08_inside_switches_client(self):
        record = self.spawn_process("U2")
        self.tmux.new_session("p", "sleep 300")
        client = self.attach_client("p")
        self.fakes.configure("fzf", sequence=[{"stdout": picker_row("U2", record["id"]), "exit_code": 0}, {"exit_code": 130}])
        popup = self.popup(client, "tx attach")
        self.wait_until(lambda: self.client_on(client.tty)["client_session"] == record["id"])
        self.assertEqual(popup.wait(timeout=10), 0)
        self.assertEqual(len(self.tmux.clients()), 1)

    def test_t_attach_08_outside_foreground_attach(self):
        record = self.spawn_process("U2")
        self.fakes.configure("fzf", sequence=[{"stdout": picker_row("U2", record["id"]), "exit_code": 0}, {"exit_code": 130}])
        process = self.tx_pty(["attach"])
        client = self.wait_until(lambda: self.client_on(process.tty))
        self.assertEqual(client["client_session"], record["id"])
        self.tmux.run("detach-client", "-t", process.tty, check=True)
        self.assertEqual(process.wait(), 0)

    def test_t_attach_08_tmux_set_without_client_swallows_switch_failure(self):
        record = self.spawn_process("U2")
        self.fakes.configure("fzf", sequence=[{"stdout": picker_row("U2", record["id"]), "exit_code": 0}, {"exit_code": 130}])
        result = self.attach_detached([], {"TMUX": f"/tmp/tmux-{os.getuid()}/{self.tmux.socket},1,0"})
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(len(self.fakes.dumps("fzf")), 1)
        self.assertEqual(self.tmux.clients(), [])

    # ----- T-ATTACH-09 --jump ----------------------------------------------------------------

    def jump_fixture(self) -> tuple[PtyProcess, dict, str]:
        """Views (window 0 bash, window 1 nesting U2, window 2 current), Alt:0.0 nesting U2 too.

        A popup has no `TMUX_PANE`, so the picker's `#S` resolves through tmux's most recently
        active client. The outer client attaches LAST so it is the newest — what the operator's
        prefix+t keypress guarantees in real use."""
        self.spawn_view("Views")
        self.spawn_view("Alt")
        record = self.spawn_process("U2")
        self.tmux.run("new-window", "-t", "Views:1", check=True)
        nested_pane = self.tmux.pane_id("Views:1")
        self.nest(nested_pane, record["id"])
        self.nest(self.tmux.pane_id("Alt:0"), record["id"])
        self.tmux.run("new-window", "-t", "Views:2", check=True)
        self.tmux.run("select-window", "-t", "Views:2", check=True)
        self.assertEqual(self.tmux.display("Views", "#{window_index}"), "2")
        client = self.attach_client("Views")
        self.fakes.configure("fzf", sequence=[{"stdout": picker_row("U2", record["id"]), "exit_code": 0}, {"exit_code": 130}])
        return client, record, nested_pane

    def test_t_attach_09_jump_selects_hosting_pane(self):
        client, record, nested_pane = self.jump_fixture()
        popup = self.popup(client, "tx attach --jump")
        self.wait_until(lambda: self.tmux.display("Views", "#{window_index}.#{pane_index}") == "1.0")
        self.assertEqual(popup.wait(timeout=10), 0)
        self.assertEqual(self.tmux.display("Views", "#{pane_id}"), nested_pane)
        self.assertEqual(self.client_on(client.tty)["client_session"], "Views")
        self.assertEqual(len(self.fakes.dumps("fzf")), 1)

    def test_t_attach_09_jump_from_the_target_itself_moves_nothing(self):
        client, record, nested_pane = self.jump_fixture()
        nested_client = self.client_on(self.tmux.pane_tty(nested_pane))
        # a keystroke inside the nested client (forwarded to U2's pane) makes it the newest client
        self.tmux.send_keys(nested_pane, "Space")
        clients_before = self.tmux.clients()
        popup = self.popup(nested_client["client_tty"], "tx attach --jump")
        self.assertEqual(popup.wait(timeout=10), 0)
        self.assertEqual(len(self.fakes.dumps("fzf")), 1)
        self.assertEqual(self.tmux.display("Views", "#{window_index}"), "2")
        self.assertEqual(self.tmux.clients(), clients_before)

    def test_t_attach_09_jump_falls_back_to_nest_attach_then_switch(self):
        # hosted only in Alt → not current → nest-attach into the popup's shell pane
        self.spawn_view("Views")
        self.spawn_view("Alt")
        record = self.spawn_process("U2")
        self.nest(self.tmux.pane_id("Alt:0"), record["id"])
        client = self.attach_client("Views")  # newest client — see jump_fixture
        pane = self.tmux.pane_id("Views")
        self.fakes.configure("fzf", sequence=[{"stdout": picker_row("U2", record["id"]), "exit_code": 0}, {"exit_code": 130}])
        popup = self.popup(client, "tx attach --jump")
        self.wait_until(lambda: self.tmux.display(pane, "#{pane_current_command}") == "tmux")
        self.assertEqual(popup.wait(timeout=10), 0)
        self.assertEqual(
            unquote_start_command(self.tmux.display(pane, "#{pane_start_command}")),
            f"bash -c 'set -m; TMUX= tmux attach -t {record['id']}; exec ${{SHELL:-zsh}}'",
        )
        self.assertEqual(self.client_on(client.tty)["client_session"], "Views")
        # a non-shell popup pane → switch-client instead
        self.spawn_view("Nv", cmd="nvim")
        nv_client = self.attach_client("Nv")
        self.fakes.configure("fzf", sequence=[{"stdout": picker_row("U2", record["id"]), "exit_code": 0}, {"exit_code": 130}])
        popup = self.popup(nv_client, "tx attach --jump")
        self.wait_until(lambda: self.client_on(nv_client.tty)["client_session"] == record["id"])
        self.assertEqual(popup.wait(timeout=10), 0)

    def test_t_attach_09_jump_switch_failure_reruns_fzf(self):
        # U2 nested nowhere; the picker runs in a pane of a DETACHED view: the origin pane's
        # foreground is the picker itself (no nest-attach) and there is no client to switch
        self.spawn_view("Views")
        record = self.spawn_process("U2")
        pane = self.tmux.pane_id("Views")
        self.fakes.configure("fzf", sequence=[{"stdout": picker_row("U2", record["id"]), "exit_code": 0}, {"exit_code": 130}])
        result = self.run_in_pane(pane, "tx attach --jump")
        self.assertEqual((result.code, result.out), (0, ""))
        self.assertEqual(result.err, "tx: could not jump to or switch to session U2\n")
        self.assertEqual(len(self.fakes.dumps("fzf")), 2)
        self.assertEqual(self.tmux.clients(), [])
        self.assertEqual(self.tmux.display(pane, "#{pane_start_command}"), "/bin/bash")

    def test_t_attach_09_jump_dead_target(self):
        self.spawn_process("U2")
        self.fakes.configure("fzf", sequence=[{"stdout": picker_row("U2", "dead-id"), "exit_code": 0}, {"exit_code": 130}])
        result = self.tx(["attach", "--jump"])
        self.assertEqual((result.code, result.out), (0, ""))
        self.assertEqual(result.err, "tx: session 'U2' does not exist\ntx: could not jump to or switch to session U2\n")
        self.assertEqual(len(self.fakes.dumps("fzf")), 2)

    # ----- T-ATTACH-10 --host ssh + @remote-session ----------------------------------------

    def ssh_stub(self, pane: str) -> "os.PathLike[str]":
        report = self.root / "ssh-report.txt"
        self.fakes.write_script(
            "ssh",
            "#!/bin/sh\n"
            f'printf "argc=%s\\n" "$#" > {report}\n'
            f'for argument in "$@"; do printf "%s\\n" "$argument" >> {report}; done\n'
            f'printf "option=%s\\n" "$(tmux show-options -pqv -t {pane} @remote-session 2>/dev/null)" >> {report}\n'
            "exit 7\n",
        )
        return report

    def remote_command_lines(self, host: str) -> list[str]:
        return [
            "argc=3", "-t", host,
            '$SHELL -lc "if command -v tx >/dev/null 2>&1; then tx attach; else tmux attach; fi"',
        ]

    def test_t_attach_10_host_ssh_and_remote_session_stamp(self):
        self.spawn_view("Views")
        pane = self.tmux.pane_id("Views")
        report = self.ssh_stub(pane)
        for argv, host in (([], "personal"), (["box"], "box")):
            result = self.tx(["attach", "--host", *argv], env={"TMUX_PANE": pane})
            self.assertEqual((result.code, result.out, result.err), (7, "", ""))
            self.assertEqual(report.read_text().splitlines(), [*self.remote_command_lines(host), f"option={host}"])
            self.assertEqual(self.tmux.run("show-options", "-pqv", "-t", pane, "@remote-session").stdout, "")
        self.assertEqual(len(self.fakes.dumps("fzf")), 0)

    def test_t_attach_10_host_edges(self):
        self.spawn_view("Views")
        pane = self.tmux.pane_id("Views")
        report = self.ssh_stub(pane)
        # TMUX_PANE unset → no stamp, ssh still runs, exit 7
        result = self.tx(["attach", "--host", "box"])
        self.assertEqual((result.code, result.err), (7, ""))
        self.assertEqual(report.read_text().splitlines(), [*self.remote_command_lines("box"), "option="])
        # a dead pane fails the stamp before ssh
        report.unlink()
        result = self.tx(["attach", "--host", "box"], env={"TMUX_PANE": "%999"})
        self.assertEqual((result.code, result.out), (1, ""))
        self.assertEqual(result.err, "tx attach: tmux set-option -t %999 -p @remote-session box failed: no such pane: %999\n")
        self.assertFalse(report.exists())
        # the unset is tolerant when the pane dies during ssh
        self.fakes.write_script("ssh", f"#!/bin/sh\ntmux kill-pane -t {pane}\nexit 7\n")
        result = self.tx(["attach", "--host", "box"], env={"TMUX_PANE": pane})
        self.assertEqual((result.code, result.out, result.err), (7, "", ""))

    # ----- T-ATTACH-11 --all exit 2 ---------------------------------------------------------

    def test_t_attach_11_all_unsupported(self):
        result = self.tx(["attach", "--all"])
        self.assertEqual((result.code, result.out), (2, ""))
        self.assertEqual(
            result.err,
            "tx attach --all (a merged local+remote picker) is not supported; use "
            "`tx attach --host ALIAS` to attach a remote host's sessions over ssh.\n",
        )
        self.assertEqual(self.fakes.dumps("fzf"), [])
        # --host wins when both are given
        self.fakes.write_script("ssh", "#!/bin/sh\nexit 7\n")
        result = self.tx(["attach", "--all", "--host", "box"])
        self.assertEqual((result.code, result.err), (7, ""))

    # ----- T-ATTACH-12 -f prefill -----------------------------------------------------------

    def test_t_attach_12_filter_prefill(self):
        self.fakes.configure("fzf", exit_code=130)
        for flag in ("-f", "--filter"):
            result = self.tx(["attach", flag, "foo"])
            self.assertEqual((result.code, result.err), (0, ""))
            self.assertIn("--query=foo", self.fzf_argv())
