"""T-TMUXCONF-01..18 — `tmux/tx-ide.tmux` bindings/hooks, the `bin/tmux-*` helpers, `bin/tx-assistant`.

Every helper and the fragment run with `env=self.env()` (the scrubbed environment: the private
server's `tmux` wrapper AND the temp `TX_IDE_HOME` together — never one without the other). Extra
panes come from `self.tmux.run` (also scrubbed): on tmux 3.4 a pane inherits the invoking CLIENT's
`PATH`, so a split issued with the operator's environment would give the pane the operator's
`tmux` and its default socket.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import unittest

from txkit import REPO, TX_BIN, TxCase, expected_failure_on_python, platform_only, strip_ansi

FRAGMENT = REPO / "tmux" / "tx-ide.tmux"
# The fragment resolves its helpers `dirname`-relative to its own real path, un-normalised.
HELPER_PREFIX = f"{REPO}/tmux/../bin"
FOCUS_HOOKS = (
    "pane-focus-in",
    "after-select-pane",
    "after-select-window",
    "window-pane-changed",
    "session-window-changed",
    "client-session-changed",
    "pane-exited",
    "client-detached",
)
NAV_KEYS = (("h", "L", "pane_at_left"), ("j", "D", "pane_at_bottom"), ("k", "U", "pane_at_top"), ("l", "R", "pane_at_right"))
AGENT_SCROLL_PREDICATE = (
    "#{||:#{==:#{pane_current_command},claude},#{||:#{==:#{pane_current_command},codex},"
    "#{m:[0-9]*.[0-9]*.[0-9]*,#{pane_current_command}}}}"
)
PRIMING = (
    "Read ~/.tx-ide/agents/COMMON.md and ~/.tx-ide/agents/TX-ASSISTANT.md as your first actions. "
    "Then, if they exist, also read ~/.tx-ide/user-agents/COMMON.md, ~/.tx-ide/user-agents/COMMON.local.md, "
    "~/.tx-ide/user-agents/TX-ASSISTANT.md, and ~/.tx-ide/user-agents/TX-ASSISTANT.local.md (any "
    "user-agents/X.md replaces the shipped one; any user-agents/X.local.md extends it). Follow all of "
    "these for the duration of this session."
)
ASSISTANT_COMMAND = "claude --model opus --effort medium --dangerously-skip-permissions"
POPUP_TOP_BORDER = re.compile(r"╭─ Edit session ─+╮")
RESOURCES_LINE = re.compile(r"^CPU #\[bold\][0-9]+%#\[nobold\]  MEM #\[bold\][0-9]+%#\[nobold\]$")


def collapse(text: str) -> str:
    return " ".join(text.split())


class TmuxconfCase(TxCase):
    """Shared recipes: source the fragment, query keys, split panes and run helpers under the
    scrubbed environment."""

    def source_fragment(self, **options: str) -> subprocess.CompletedProcess:
        """Set `@tx-ide-<name>` options (underscores → dashes), then run the fragment."""
        for name, value in options.items():
            self.tmux.run("set-option", "-g", f"@tx-ide-{name.replace('_', '-')}", value, check=True)
        result = subprocess.run(["bash", str(FRAGMENT)], env=self.env(), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def list_keys(self, table: str, key: str) -> subprocess.CompletedProcess:
        return self.tmux.run("list-keys", "-T", table, key)

    def assert_bound(self, table: str, key: str) -> str:
        result = self.list_keys(table, key)
        self.assertEqual(result.returncode, 0, f"{table} {key}: {result.stderr}")
        self.assertTrue(result.stdout.startswith(f"bind-key -T {table} {key} "), result.stdout)
        return collapse(result.stdout)

    def assert_unknown(self, table: str, key: str) -> None:
        result = self.list_keys(table, key)
        self.assertEqual((result.returncode, result.stderr), (1, f"unknown key: {key}\n"), result.stdout)

    def hook(self, name: str) -> str:
        return self.tmux.run("show-hooks", "-g", name, check=True).stdout.rstrip("\n")

    def window_option(self, name: str) -> str:
        return self.tmux.run("show-options", "-gwv", name, check=True).stdout.rstrip("\n")

    def split_bash(self, session: str, *flags: str) -> str:
        """Split a window of `session` with a plain `/bin/bash` pane; returns the new pane id."""
        before = {pane["pane_id"] for pane in self.tmux.panes(session)}
        self.tmux.run("split-window", *flags, "-t", session, "-c", str(self.root), "/bin/bash", check=True)
        return ({pane["pane_id"] for pane in self.tmux.panes(session)} - before).pop()

    def helper(self, name: str, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([str(REPO / "bin" / name), *args], env=self.env(env), capture_output=True, text=True)

    def nested_worker(self, name: str = "w", tag: str = "a,b") -> tuple[dict, str]:
        """A view `Views` whose first pane nests a spawned process; returns (record, pane id)."""
        self.spawn_view("Views")
        record = self.spawn_process(name, tag=tag)
        pane = self.tmux.pane_id("Views")
        self.tmux.nest_attach(pane, record["id"])
        return record, pane

    def assistant_script(self) -> str:
        """`bin/tx-assistant` spawns with `--cwd <its own checkout>`, so run a COPY inside a temp git
        repo whose `bin/tx` links to TX_BIN — the worktree it creates then belongs to the temp repo."""
        repository = self.git.path
        (repository / "bin").mkdir()
        shutil.copy(REPO / "bin" / "tx-assistant", repository / "bin" / "tx-assistant")
        (repository / "bin" / "tx").symlink_to(TX_BIN)
        return str(repository / "bin" / "tx-assistant")

    def assistant_env(self, pane: str | None = None) -> dict[str, str]:
        env = {"TMUX": f"{self.root}/sock,1,0"}
        if pane is not None:
            env["TMUX_PANE"] = pane
        return env

    def assistant_pane_text(self, session_id: str) -> str:
        return self.tmux.run("capture-pane", "-p", "-J", "-t", session_id, check=True).stdout


class TestTmuxconf(TmuxconfCase):
    # ----- T-TMUXCONF-01 ---------------------------------------------------------------------

    def test_t_tmuxconf_01_defaults_generate_every_feature(self):
        self.spawn_view("Views")
        self.source_fragment()
        for key in ("t", "/", "X", "e", "s", "C-h", "C-j", "C-k", "C-l"):
            self.assert_bound("prefix", key)
        for key in ("C-u", "C-d", "C-h", "C-j", "C-k", "C-l", *(f"M-{n}" for n in range(1, 10)), *(f"User{n}" for n in range(9))):
            self.assert_bound("root", key)
        for n in range(1, 8):
            self.assert_unknown("prefix", f"M-{n}")
        self.assertEqual(self.window_option("pane-border-style"), "fg=#3b4261")
        self.assertEqual(self.window_option("pane-active-border-style"), "fg=#7aa2f7,bold")
        self.assertEqual(self.window_option("pane-border-lines"), "heavy")
        self.assertEqual(self.window_option("pane-border-indicators"), "both")
        self.assertEqual(self.window_option("pane-border-status"), "off")
        self.assertEqual(self.window_option("pane-border-format"), " [#P] #(tmux-pane-session-name #D) ")
        self.assertEqual(self.tmux.run("show-options", "-gv", "focus-events", check=True).stdout, "on\n")
        for index in range(9):
            self.assertEqual(
                self.tmux.run("show-options", "-sv", f"user-keys[{index}]", check=True).stdout,
                f"\\033W{index + 1}\n",
            )
        listing = self.tmux.run("show-hooks", "-g", check=True).stdout + self.tmux.run("show-hooks", "-gw", check=True).stdout
        for name in ("after-new-window", *FOCUS_HOOKS):
            self.assertIn(f"{name}[0] ", listing)
            self.assertTrue(self.hook(name).startswith(f"{name}[0] "), self.hook(name))

    def test_t_tmuxconf_01_resourcing_is_idempotent(self):
        self.spawn_view("Views")
        temp_dir = self.root / "tmp"
        temp_dir.mkdir()
        environment = self.env({"TMPDIR": str(temp_dir)})
        first = subprocess.run(["bash", str(FRAGMENT)], env=environment, capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        keys = self.tmux.run("list-keys", check=True).stdout
        hooks = self.tmux.run("show-hooks", "-g", check=True).stdout + self.tmux.run("show-hooks", "-gw", check=True).stdout
        second = subprocess.run(["bash", str(FRAGMENT)], env=environment, capture_output=True, text=True)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.tmux.run("list-keys", check=True).stdout, keys)
        self.assertEqual(self.tmux.run("show-hooks", "-g", check=True).stdout + self.tmux.run("show-hooks", "-gw", check=True).stdout, hooks)
        self.assertEqual(list(temp_dir.glob("tx-ide-bindings.*")), [])

    # ----- T-TMUXCONF-02 ---------------------------------------------------------------------

    def test_t_tmuxconf_02_popup_and_prompt_bindings(self):
        self.spawn_view("Views")
        self.source_fragment()
        self.assertEqual(
            self.assert_bound("prefix", "t"),
            'bind-key -T prefix t display-popup -E -T " tx " -h 30 -w 118 -x C -y 1 "tx attach"',
        )
        self.assertEqual(
            self.assert_bound("prefix", "/"),
            "bind-key -T prefix / command-prompt -p tx-assistant> "
            '{ set-buffer -b tx-assistant-input "%%" ;; run-shell -b "tx-assistant --from-buffer" }',
        )
        self.assertEqual(
            self.assert_bound("prefix", "X"),
            f"bind-key -T prefix X run-shell \"{HELPER_PREFIX}/tmux-kill-tx-session '#{{pane_id}}' '#{{session_name}}'\"",
        )
        self.assertEqual(
            self.assert_bound("prefix", "e"),
            f"bind-key -T prefix e run-shell -b \"{HELPER_PREFIX}/tmux-edit-tx-session '#{{pane_id}}' '#{{client_name}}'\"",
        )

    def test_t_tmuxconf_02_popups_off_leaves_keys_alone(self):
        self.spawn_view("Views")
        self.tmux.run("bind-key", "X", "kill-pane", check=True)
        self.source_fragment(popups="off")
        self.assertEqual(self.assert_bound("prefix", "t"), "bind-key -T prefix t clock-mode")
        self.assertNotIn("tx-assistant", self.assert_bound("prefix", "/"))
        self.assertEqual(self.assert_bound("prefix", "X"), "bind-key -T prefix X kill-pane")
        self.assert_unknown("prefix", "e")

    # ----- T-TMUXCONF-03 ---------------------------------------------------------------------

    def test_t_tmuxconf_03_pane_border_after_new_window_hook(self):
        self.spawn_view("Views")
        self.tmux.new_session("p", "sleep 300")
        self.source_fragment()
        self.assertEqual(
            self.hook("after-new-window"),
            'after-new-window[0] if-shell -F "#{@tx_view}" "setw pane-border-status top"',
        )
        self.tmux.run("new-window", "-t", "Views", check=True)
        self.tmux.run("new-window", "-t", "p", check=True)
        self.assertEqual(self.tmux.display("Views:1", "#{pane-border-status}"), "top")
        self.assertEqual(self.tmux.display("p:1", "#{pane-border-status}"), "off")

    def test_t_tmuxconf_03_pane_borders_off_sets_nothing(self):
        self.spawn_view("Views")
        self.source_fragment(pane_borders="off")
        self.assertEqual(self.window_option("pane-border-lines"), "single")
        self.assertEqual(self.window_option("pane-border-status"), "off")
        self.assertEqual(self.window_option("pane-border-style"), "default")
        self.assertNotIn("tmux-pane-session-name", self.window_option("pane-border-format"))
        self.assertNotIn("#7aa2f7", self.window_option("pane-active-border-style"))
        self.assertEqual(self.hook("after-new-window"), "after-new-window")

    def test_t_tmuxconf_03_palette_off_keeps_format_drops_colours(self):
        self.spawn_view("Views")
        self.source_fragment(palette="off")
        self.assertEqual(self.window_option("pane-border-format"), " [#P] #(tmux-pane-session-name #D) ")
        self.assertEqual(self.window_option("pane-border-lines"), "heavy")
        self.assertEqual(self.window_option("pane-border-indicators"), "both")
        self.assertEqual(self.window_option("pane-border-style"), "default")
        self.assertNotIn("#7aa2f7", self.window_option("pane-active-border-style"))

    # ----- T-TMUXCONF-04 ---------------------------------------------------------------------

    def test_t_tmuxconf_04_session_labels_bind(self):
        self.spawn_view("Views")
        self.source_fragment()
        self.assertEqual(
            self.assert_bound("prefix", "s"),
            collapse(
                f"bind-key -T prefix s run-shell {HELPER_PREFIX}/tmux-session-relabel \\;\\; choose-tree -Zs -F "
                '"#{?session_format,#{?@tx_name,#{@tx_name}  ,}#{session_windows}w#{?session_attached, (attached),},'
                '#{?window_format,#{window_index}: #{window_name},#{pane_current_command}}}"'
            ),
        )

    def test_t_tmuxconf_04_session_labels_off_keeps_stock_bind(self):
        self.spawn_view("Views")
        self.source_fragment(session_labels="off")
        self.assertEqual(self.assert_bound("prefix", "s"), "bind-key -T prefix s choose-tree -Zs")

    # ----- T-TMUXCONF-05 ---------------------------------------------------------------------

    def test_t_tmuxconf_05_focus_hooks(self):
        self.spawn_view("Views")
        self.source_fragment()
        for name in FOCUS_HOOKS:
            self.assertEqual(self.hook(name), f"{name}[0] run-shell -b {HELPER_PREFIX}/tx-graph-focus-poke")
        self.assertEqual(self.tmux.run("show-options", "-gv", "focus-events", check=True).stdout, "on\n")

    def test_t_tmuxconf_05_graph_focus_off_sets_none(self):
        self.spawn_view("Views")
        self.source_fragment(graph_focus="off")
        for name in FOCUS_HOOKS:
            self.assertEqual(self.hook(name), name)
        self.assertEqual(self.tmux.run("show-options", "-gv", "focus-events", check=True).stdout, "off\n")

    # ----- T-TMUXCONF-06 ---------------------------------------------------------------------

    def test_t_tmuxconf_06_agent_scroll_binds(self):
        self.spawn_view("Views")
        self.source_fragment()
        self.assertEqual(
            self.assert_bound("root", "C-u"),
            f'bind-key -T root C-u if-shell -F "{AGENT_SCROLL_PREDICATE}" "send-keys PageUp" "send-keys C-u"',
        )
        self.assertEqual(
            self.assert_bound("root", "C-d"),
            f'bind-key -T root C-d if-shell -F "{AGENT_SCROLL_PREDICATE}" "send-keys PageDown" "send-keys C-d"',
        )

    def test_t_tmuxconf_06_agent_scroll_off_unbound(self):
        self.spawn_view("Views")
        self.source_fragment(agent_scroll="off")
        self.assert_unknown("root", "C-u")
        self.assert_unknown("root", "C-d")

    # ----- T-TMUXCONF-07 ---------------------------------------------------------------------

    def test_t_tmuxconf_07_nav_keys_generation(self):
        self.spawn_view("Views")
        self.source_fragment()
        for key, direction, edge in NAV_KEYS:
            self.assertEqual(
                self.assert_bound("root", f"C-{key}"),
                f'bind-key -T root C-{key} if-shell -F "#{{||:#{{==:#{{pane_current_command}},nvim}},'
                f'#{{==:#{{pane_current_command}},tmux}}}}" {{ send-keys C-{key} }} '
                f'{{ if-shell -F "#{{{edge}}}" {{ run-shell -b "{HELPER_PREFIX}/tmux-nav {direction} '
                f'#{{pane_id}} #{{client_tty}}" }} {{ select-pane -{direction} }} }}',
            )
            self.assertEqual(
                self.assert_bound("prefix", f"C-{key}"),
                f'bind-key -T prefix C-{key} if-shell -F "#{{==:#{{pane_current_command}},tmux}}" '
                f'"send-keys C-b C-{key}" "send-keys C-{key}"',
            )

    def test_t_tmuxconf_07_nav_keys_off_unbinds_only_ours(self):
        self.spawn_view("Views")
        self.source_fragment()
        self.tmux.run("bind-key", "-n", "C-h", "select-pane", "-L", check=True)
        self.source_fragment(nav_keys="off")
        self.assertEqual(self.assert_bound("root", "C-h"), "bind-key -T root C-h select-pane -L")
        for key in ("C-j", "C-k", "C-l"):
            self.assert_unknown("root", key)
        for key in ("C-h", "C-j", "C-k", "C-l"):
            self.assert_unknown("prefix", key)

    @expected_failure_on_python
    def test_t_tmuxconf_07_fixed_prefix_none_skips_prefix_nav_binds(self):
        # Q24 FIX: tmux reports the prefix as `None`; the compare must be case-insensitive so a
        # prefix-less setup gets the root binds only (the reference binds `send-keys None C-h`).
        self.spawn_view("Views")
        self.tmux.run("set-option", "-g", "prefix", "none", check=True)
        self.source_fragment()
        for key, _direction, _edge in NAV_KEYS:
            self.assert_bound("root", f"C-{key}")
            self.assert_unknown("prefix", f"C-{key}")

    # ----- T-TMUXCONF-08 ---------------------------------------------------------------------

    def test_t_tmuxconf_08_pane_and_window_keys(self):
        self.spawn_view("Views")
        self.source_fragment()
        self.assertEqual(self.assert_bound("root", "M-3"), "bind-key -T root M-3 select-pane -t 3")
        self.assertEqual(self.assert_bound("root", "User2"), "bind-key -T root User2 select-window -t 3")
        self.assertEqual(self.tmux.run("show-options", "-s", "user-keys[2]", check=True).stdout, "user-keys[2] \\033W3\n")

    def test_t_tmuxconf_08_pane_keys_off(self):
        self.spawn_view("Views")
        self.source_fragment(pane_keys="off")
        for n in range(1, 10):
            self.assert_unknown("root", f"M-{n}")
        self.assertEqual(self.assert_bound("prefix", "M-1"), "bind-key -T prefix M-1 select-layout even-horizontal")

    def test_t_tmuxconf_08_window_keys_off(self):
        self.spawn_view("Views")
        self.source_fragment(window_keys="off")
        for n in range(9):
            self.assert_unknown("root", f"User{n}")
        self.assertEqual(self.tmux.run("show-options", "-s", "user-keys", check=True).stdout, "user-keys\n")

    # ----- T-TMUXCONF-09 ---------------------------------------------------------------------

    def test_t_tmuxconf_09_pane_info(self):
        self.records.other(id="U", name="w", tags=("a", "b"))
        self.assertEqual((self.tx(["_pane-info", "U"]).code, self.tx(["_pane-info", "U"]).out), (0, "w\na,b\n"))
        for token in ("nope", ""):
            result = self.tx(["_pane-info", token])
            self.assertEqual((result.code, result.out, result.err), (0, "\n\n", ""))
        self.records.path("U").write_text("{not json")
        result = self.tx(["_pane-info", "U"])
        self.assertEqual((result.code, result.out, result.err), (0, "\n\n", ""))

    # ----- T-TMUXCONF-10 ---------------------------------------------------------------------

    def test_t_tmuxconf_10_tmux_name(self):
        record = self.spawn_process("w")
        self.records.other(name="old", state="exited")
        for token in ("w", record["id"]):
            result = self.tx(["_tmux-name", token])
            self.assertEqual((result.code, result.out, result.err), (0, f"{record['id']}\n", ""))
        for token in ("old", "nope"):
            result = self.tx(["_tmux-name", token])
            self.assertEqual((result.code, result.out, result.err), (0, "", ""))

    # ----- T-TMUXCONF-11 ---------------------------------------------------------------------

    def test_t_tmuxconf_11_pane_session_name(self):
        record, pane = self.nested_worker()
        self.spawn_process("x", tag="t,tx-system")
        result = self.helper("tmux-pane-session-name", pane)
        self.assertEqual(
            (result.returncode, result.stdout),
            (0, "#[bold]w#[default] #[fg=colour167,#{?pane_active,bold,nobold}][a]#[default] "
                "#[fg=colour215,#{?pane_active,bold,nobold}][b]#[default]"),
        )
        chips = dict(re.findall(r"\x1b\[38;5;(\d+)m\[([^\]]+)\]", self.tx(["_list"]).raw_out))
        self.assertEqual({tag: number for number, tag in chips.items()}, {"a": "167", "b": "215", "t": "38", "tx-system": "140"})
        self.tx(["tag", "w", ""])
        self.assertEqual(self.helper("tmux-pane-session-name", pane).stdout, "#[bold]w#[default]")

    def test_t_tmuxconf_11_pane_session_name_fallbacks(self):
        self.spawn_view("Views")
        empty = self.tmux.pane_id("Views")
        self.assertEqual(self.helper("tmux-pane-session-name", empty).stdout, "#[fg=colour240,nobold]—#[default]")
        untracked = self.split_bash("Views")
        self.tmux.new_session("raw", "sleep 300")
        self.tmux.nest_attach(untracked, "raw")
        self.assertEqual(self.helper("tmux-pane-session-name", untracked).stdout, "#[bold]raw#[default]")
        recordless = self.split_bash("Views")
        self.tmux.new_session("ghost", "sleep 300")
        self.tmux.run("set-option", "-t", "ghost", "@tx_id", "no-such-record", check=True)
        self.tmux.nest_attach(recordless, "ghost")
        self.assertEqual(self.helper("tmux-pane-session-name", recordless).stdout, "#[bold]ghost#[default]")
        self.tmux.run("set-option", "-p", "-t", empty, "@remote-session", "box", check=True)
        self.assertEqual(self.helper("tmux-pane-session-name", empty).stdout, "(r) box")
        no_argument = self.helper("tmux-pane-session-name")
        self.assertEqual((no_argument.returncode, no_argument.stdout), (0, ""))
        dead = self.helper("tmux-pane-session-name", "%999")
        self.assertEqual(dead.returncode, 0)
        self.assertIn(dead.stdout, ("", "#[fg=colour240,nobold]—#[default]"))

    # ----- T-TMUXCONF-12 ---------------------------------------------------------------------

    def test_t_tmuxconf_12_nav_edge_walk(self):
        self.spawn_view("Views")
        left = self.tmux.pane_id("Views")
        right = self.split_bash("Views", "-h")
        inner = self.spawn_process("inner", cmd="/bin/bash")
        self.tmux.nest_attach(right, inner["id"])
        inner_pane = self.tmux.panes(inner["id"])[0]["pane_id"]
        right_tty = self.tmux.pane_tty(right)
        self.assertEqual(self.tmux.display(inner_pane, "#{pane_at_left}"), "1")
        self.assertEqual(self.tmux.display(right, "#{pane_at_left}"), "0")
        self.tmux.run("select-pane", "-t", right, check=True)
        result = self.helper("tmux-nav", "L", inner_pane, right_tty)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(self.tmux.display("Views", "#{pane_id}"), left)
        # top-level pane not at the edge → plain select-pane
        self.tmux.run("select-pane", "-t", right, check=True)
        self.assertEqual(self.helper("tmux-nav", "L", right).returncode, 0)
        self.assertEqual(self.tmux.display("Views", "#{pane_id}"), left)
        # top-level pane at the edge with no host → nothing selected
        self.tmux.run("select-pane", "-t", right, check=True)
        self.assertEqual(self.helper("tmux-nav", "L", left).returncode, 0)
        self.assertEqual(self.tmux.display("Views", "#{pane_id}"), right)

    def test_t_tmuxconf_12_nav_argument_and_toggle_guards(self):
        self.spawn_view("Views")
        for arguments in ((), ("Q",)):
            result = self.helper("tmux-nav", *arguments)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (2, "", "usage: tmux-nav {L|D|U|R} [pane_id] [client_tty]\n"))
        empty_pane = self.helper("tmux-nav", "L", "", env={"TMUX_PANE": None})
        self.assertEqual((empty_pane.returncode, empty_pane.stdout, empty_pane.stderr), (0, "", ""))
        left = self.tmux.pane_id("Views")
        right = self.split_bash("Views", "-h")
        self.tmux.run("select-pane", "-t", right, check=True)
        self.tmux.run("set-option", "-g", "@tx-ide-nav-keys", "off", check=True)
        self.assertEqual(self.helper("tmux-nav", "L", right).returncode, 0)
        self.assertEqual(self.tmux.display("Views", "#{pane_id}"), right)
        self.assertNotEqual(right, left)

    def test_t_tmuxconf_12_nav_attach_cycle_stops(self):
        self.spawn_view("Views")
        view_pane = self.tmux.pane_id("Views")
        inner = self.spawn_process("inner", cmd="/bin/bash")
        self.attach_client("Views")
        self.tmux.nest_attach(view_pane, inner["id"])
        inner_pane = self.tmux.panes(inner["id"])[0]["pane_id"]
        self.tmux.nest_attach(inner_pane, "Views")
        started = time.monotonic()
        result = self.helper("tmux-nav", "L", inner_pane)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertLess(time.monotonic() - started, 10)

    # ----- T-TMUXCONF-13 ---------------------------------------------------------------------

    def test_t_tmuxconf_13_kill_tx_session_confirm(self):
        record, pane = self.nested_worker()
        client = self.attach_client("Views")
        helper = subprocess.Popen(
            [str(REPO / "bin" / "tmux-kill-tx-session"), pane, "Views"], env=self.env(),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.addCleanup(helper.kill)
        client.expect('kill tx session "w"? (y/n)')
        client.write("y")
        self.assertEqual(helper.wait(10), 0)
        self.wait_until(lambda: not self.tmux.has_session(record["id"]))
        self.assertEqual(self.records.load(record["id"])["state"], "exited")
        self.assertEqual((self.log_tail()[0]["type"], self.log_tail()[0]["msg"]), ("kill", "w"))

    def test_t_tmuxconf_13_kill_tx_session_declined(self):
        record, pane = self.nested_worker()
        client = self.attach_client("Views")
        lines_before = len(self.log_lines())
        helper = subprocess.Popen(
            [str(REPO / "bin" / "tmux-kill-tx-session"), pane, "Views"], env=self.env(),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.addCleanup(helper.kill)
        client.expect('kill tx session "w"? (y/n)')
        client.write("n")
        self.assertEqual(helper.wait(10), 0)
        self.assertTrue(self.tmux.has_session(record["id"]))
        self.assertEqual(self.records.load(record["id"])["state"], "alive")
        self.assertEqual(len(self.log_lines()), lines_before)

    def test_t_tmuxconf_13_kill_tx_session_view_home_and_plain_session(self):
        self.spawn_view("Views")
        client = self.attach_client("Views")
        result = self.helper("tmux-kill-tx-session", self.tmux.pane_id("Views"), "Views")
        self.assertEqual(result.returncode, 0)
        client.expect("tx: 'Views' is a view home, not a tx session — nothing to kill here")
        self.assertNotIn("kill tx session", strip_ansi(client.output))
        self.tmux.new_session("p", "/bin/bash")
        plain_client = self.attach_client("p")
        lines_before = len(self.log_lines())
        helper = subprocess.Popen(
            [str(REPO / "bin" / "tmux-kill-tx-session"), self.tmux.pane_id("p"), "p"], env=self.env(),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.addCleanup(helper.kill)
        plain_client.expect('kill tx session "p"? (y/n)')
        plain_client.write("y")
        self.assertEqual(helper.wait(10), 0)
        plain_client.expect("returned 1")
        self.assertTrue(self.tmux.has_session("p"))
        self.assertEqual(len(self.log_lines()), lines_before)
        missing = self.helper("tmux-kill-tx-session")
        self.assertEqual((missing.returncode, missing.stdout, missing.stderr), (0, "", ""))

    # ----- T-TMUXCONF-14 ---------------------------------------------------------------------

    def test_t_tmuxconf_14_edit_popup_geometry(self):
        record, pane = self.nested_worker()
        client = self.attach_client("Views", rows=50, cols=200)
        self.split_bash("Views", "-h")
        for pane_width, popup_width in ((100, 80), (30, 44)):
            self.tmux.run("resize-pane", "-t", pane, "-x", str(pane_width), check=True)
            self.assertEqual(self.tmux.display(pane, "#{pane_width}"), str(pane_width))
            self.assert_popup(client, pane, popup_width)
        for other in [row["pane_id"] for row in self.tmux.panes("Views") if row["pane_id"] != pane]:
            self.tmux.run("kill-pane", "-t", other, check=True)
        self.assertEqual(self.tmux.display(pane, "#{pane_width}"), "200")
        self.assert_popup(client, pane, 80)
        self.assertEqual((self.records.load(record["id"])["name"], self.records.load(record["id"])["tags"]), ("w", ["a", "b"]))
        self.assertEqual([line["type"] for line in self.log_lines()], ["spawn-view", "spawn"])

    def assert_popup(self, client, pane: str, popup_width: int) -> None:
        client.read(0.3)
        client.output = ""
        helper = subprocess.Popen(
            [str(REPO / "bin" / "tmux-edit-tx-session"), pane, client.tty], env=self.env(),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.addCleanup(helper.kill)
        text = client.expect("Esc cancel").replace("\x0f", "")
        borders = POPUP_TOP_BORDER.findall(text)
        self.assertTrue(borders, text)
        self.assertEqual({len(border) for border in borders}, {popup_width})
        self.assertIn(" Edit session ", borders[0])
        self.assertRegex(text, r"› Name:\s+w")
        self.assertRegex(text, r"Tags:\s+a,b")
        client.write("\x1b")
        self.assertEqual(helper.wait(10), 0)

    # ----- T-TMUXCONF-15 ---------------------------------------------------------------------

    def test_t_tmuxconf_15_session_relabel(self):
        self.spawn_view("Views")
        tagged = self.spawn_process("w", tag="a,b")
        untagged = self.spawn_process("x")
        self.tx(["tag", "x", ""])
        exited = self.records.other(name="gone", state="exited")
        result = self.helper("tmux-session-relabel")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(self.tmux.option(tagged["id"], "@tx_name"), "w [a,b]")
        self.assertEqual(self.tmux.option(untagged["id"], "@tx_name"), "x")
        self.assertIsNone(self.tmux.option("Views", "@tx_name"))
        self.assertNotIn(exited, self.tmux.sessions())

    # ----- T-TMUXCONF-16 ---------------------------------------------------------------------

    @platform_only("linux")
    def test_t_tmuxconf_16_system_resources(self):
        # The state file follows `$TMUX`'s socket path — pointed under the temp root, never at
        # the operator's `/tmp/tmux-<uid>/default`.
        state = self.root / "fake-sock.system-resources"
        environment = {"TMUX": f"{self.root}/fake-sock,123,0"}
        first = self.helper("tmux-system-resources", env=environment)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertRegex(first.stdout, RESOURCES_LINE)
        self.assertFalse(first.stdout.endswith("\n"))
        total, idle = self.read_state(state)
        self.assertEqual(self.cpu_percent(first.stdout), ((total - idle) * 100 + total // 2) // total)
        subprocess.run(["sh", "-c", "i=0; while [ $i -lt 200000 ]; do i=$((i+1)); done"], check=True)
        second = self.helper("tmux-system-resources", env=environment)
        self.assertRegex(second.stdout, RESOURCES_LINE)
        total_after, idle_after = self.read_state(state)
        elapsed, elapsed_idle = total_after - total, idle_after - idle
        if elapsed > 0 and elapsed_idle >= 0:
            expected = ((elapsed - elapsed_idle) * 100 + elapsed // 2) // elapsed
        else:
            expected = ((total_after - idle_after) * 100 + total_after // 2) // total_after
        self.assertEqual(self.cpu_percent(second.stdout), expected)
        state.write_text(f"{total_after + 10 ** 9} {idle_after}\n")
        stale = self.helper("tmux-system-resources", env=environment)
        total_stale, idle_stale = self.read_state(state)
        self.assertEqual(self.cpu_percent(stale.stdout), ((total_stale - idle_stale) * 100 + total_stale // 2) // total_stale)
        runtime_dir = self.root / "runtime"
        runtime_dir.mkdir()
        without_tmux = self.helper("tmux-system-resources", env={"TMUX": None, "XDG_RUNTIME_DIR": str(runtime_dir)})
        self.assertRegex(without_tmux.stdout, RESOURCES_LINE)
        self.assertEqual([path.name for path in runtime_dir.iterdir()], [f"tx-ide-system-resources-{self.uid()}"])

    @staticmethod
    def read_state(state) -> tuple[int, int]:
        total, idle = state.read_text().split()
        return int(total), int(idle)

    @staticmethod
    def cpu_percent(text: str) -> int:
        return int(re.match(r"CPU #\[bold\](\d+)%", text).group(1))

    @staticmethod
    def uid() -> int:
        import os

        return os.getuid()

    # ----- T-TMUXCONF-17 ---------------------------------------------------------------------

    def test_t_tmuxconf_17_assistant_warm(self):
        self.spawn_view("Views")
        script = self.assistant_script()
        self.fakes.configure("claude", prompt_glyph=True, sleep=300)
        started = time.monotonic()
        result = subprocess.run([script, "--warm"], env=self.env(self.assistant_env()), capture_output=True, text=True)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertLess(time.monotonic() - started, 5)
        session_id = self.tx(["_tmux-name", "tx-assistant"]).out.strip()
        self.assertTrue(session_id)
        record = self.records.load(session_id)
        self.assertEqual((record["name"], record["tags"], record["role"], record["cmd"]), ("tx-assistant", ["tx-system"], "llm", ASSISTANT_COMMAND))
        self.assertTrue(record["cwd"].startswith(str(self.home.worktrees_dir) + "/"), record["cwd"])
        argv = self.fakes.wait_dump("claude", session_id)["argv"]
        self.assertEqual(argv[1:], ["--model", "opus", "--effort", "medium", "--dangerously-skip-permissions"])
        self.wait_until(lambda: PRIMING in self.assistant_pane_text(session_id))
        again = subprocess.run([script, "--warm"], env=self.env(self.assistant_env()), capture_output=True, text=True)
        self.assertEqual((again.returncode, again.stdout, again.stderr), (0, "", ""))
        self.assertEqual([path.name for path in self.home.sessions_dir.iterdir()], [f"{session_id}.json"])
        self.assertEqual(self.assistant_pane_text(session_id).count(PRIMING), 1)

    def test_t_tmuxconf_17_assistant_unknown_invocation(self):
        script = self.assistant_script()
        for arguments in ((), ("--bogus",)):
            result = subprocess.run([script, *arguments], env=self.env(self.assistant_env()), capture_output=True, text=True)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (2, "", "tx-assistant: unknown invocation; pass --warm or --from-buffer\n"))

    # ----- T-TMUXCONF-18 ---------------------------------------------------------------------

    def test_t_tmuxconf_18_assistant_from_buffer(self):
        self.spawn_view("Views")
        pane = self.tmux.pane_id("Views")
        script = self.assistant_script()
        self.fakes.configure("claude", prompt_glyph=True, sleep=300)
        subprocess.run([script, "--warm"], env=self.env(self.assistant_env()), capture_output=True, text=True, check=True)
        session_id = self.tx(["_tmux-name", "tx-assistant"]).out.strip()
        envelope = self.tx(["focus-envelope", pane]).out
        self.assertTrue(envelope.startswith("<tx-command-prompt session-name='Views' "), envelope)
        self.tmux.run("set-buffer", "-b", "tx-assistant-input", "hello", check=True)
        result = subprocess.run([script, "--from-buffer"], env=self.env(self.assistant_env(pane)), capture_output=True, text=True)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        buffer = self.tmux.run("show-buffer", "-b", "tx-assistant-input")
        self.assertEqual((buffer.returncode, buffer.stderr), (1, "no buffer tx-assistant-input\n"))
        self.wait_until(lambda: f"{envelope} hello\n" in self.assistant_pane_text(session_id))
        # whitespace-only / missing buffer → nothing typed
        typed_before = self.assistant_pane_text(session_id)
        self.tmux.run("set-buffer", "-b", "tx-assistant-input", "   ", check=True)
        blank = subprocess.run([script, "--from-buffer"], env=self.env(self.assistant_env(pane)), capture_output=True, text=True)
        missing = subprocess.run([script, "--from-buffer"], env=self.env(self.assistant_env(pane)), capture_output=True, text=True)
        self.assertEqual((blank.returncode, missing.returncode), (0, 0))
        self.assertEqual(self.assistant_pane_text(session_id), typed_before)
        # no $TMUX → no envelope
        self.tmux.run("set-buffer", "-b", "tx-assistant-input", "plain", check=True)
        outside = subprocess.run([script, "--from-buffer"], env=self.env({"TMUX": None, "TMUX_PANE": None}), capture_output=True, text=True)
        self.assertEqual(outside.returncode, 0)
        self.wait_until(lambda: "\nplain\n" in self.assistant_pane_text(session_id))
        # no $TMUX_PANE → the server's current pane
        current_pane = self.tmux.run("display-message", "-p", "#{pane_id}", check=True).stdout.strip()
        self.tmux.run("set-buffer", "-b", "tx-assistant-input", "second", check=True)
        fallback = subprocess.run([script, "--from-buffer"], env=self.env(self.assistant_env()), capture_output=True, text=True)
        self.assertEqual(fallback.returncode, 0)
        self.wait_until(lambda: f"pane-id='{current_pane}'" in self.typed_line(session_id, "second"))

    def typed_line(self, session_id: str, marker: str) -> str:
        return "".join(line for line in self.assistant_pane_text(session_id).splitlines() if line.endswith(f" {marker}"))

    def test_t_tmuxconf_18_assistant_from_buffer_creates_session(self):
        self.spawn_view("Views")
        pane = self.tmux.pane_id("Views")
        script = self.assistant_script()
        self.fakes.configure("claude", prompt_glyph=True, sleep=300)
        self.tmux.run("set-buffer", "-b", "tx-assistant-input", "hello", check=True)
        result = subprocess.run([script, "--from-buffer"], env=self.env(self.assistant_env(pane)), capture_output=True, text=True)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        session_id = self.tx(["_tmux-name", "tx-assistant"]).out.strip()
        self.assertTrue(session_id)
        envelope = self.tx(["focus-envelope", pane]).out
        self.wait_until(lambda: f"{PRIMING} Now: {envelope} hello\n" in self.assistant_pane_text(session_id))
        self.assertEqual(self.tmux.run("show-buffer", "-b", "tx-assistant-input").returncode, 1)


class TestTmuxconfNoHooks(TmuxconfCase):
    """T-TMUXCONF-17 edge: the assistant spawn is refused (no claude hook shims in the home)."""

    home_options = {"hooks": ()}

    def test_t_tmuxconf_17_assistant_warm_refused_spawn(self):
        self.spawn_view("Views")
        script = self.assistant_script()
        started = time.monotonic()
        result = subprocess.run([script, "--warm"], env=self.env(self.assistant_env()), capture_output=True, text=True)
        elapsed = time.monotonic() - started
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertIn(f"tx spawn: claude hooks are not installed in {self.home.path} — ", result.stderr)
        self.assertTrue(result.stderr.endswith("tx-assistant: could not resolve the assistant session\n"), result.stderr)
        self.assertGreaterEqual(elapsed, 9)
        self.assertEqual(list(self.home.sessions_dir.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
