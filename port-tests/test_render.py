"""RENDER — lib/tx/render.py + lib/tx/palette.py (spec section 01, T-RENDER-01..17).

Pure formatting observed through `tx ls`, `tx _list`, `tx history`, `tx chat ls`, `tx artifact ls|show`
and the fzf argv/env of `tx attach`. Every listed record is backed by a live `@tx_id` session on the
private server (reconcile-on-read); ages come from crafted timestamps (D8).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import unittest
from pathlib import Path

import txkit
from txkit import TxCase

WARN = "\x1b[38;2;224;175;104m"
RESET_FG = "\x1b[39m"


def cube(n: int) -> str:
    return f"\x1b[38;5;{n}m"


def header_cols(namew: int) -> str:
    return f"{'NAME':<{namew}}   {'LOCATION':<25} {'STARTED':<7} {'IDLE':<6} {'ROLE':<5} TAGS"


class TestRender(TxCase):
    def setUp(self) -> None:
        super().setUp()
        self.now = time.time()

    # ----- helpers -------------------------------------------------------------------------

    def list_rows(self, namew: str = "12") -> list[list[str]]:
        result = self.tx(["_list"], env={"NAMEW": namew})
        self.assertEqual(result.code, 0, result.err)
        return [row.split("\t") for row in result.raw_out.rstrip("\n").split("\n")]

    def attach_dump(self, cols: int, run_dir: Path) -> dict:
        """`COLUMNS=<cols> setsid -w tx attach` with no controlling tty; returns the fake fzf dump."""
        run_dir.mkdir()
        result = self.tx_detached(["attach"], env={"COLUMNS": str(cols), "FAKE_OUT": str(run_dir)})
        self.assertEqual(result.code, 0, result.err)
        dumps = list(run_dir.glob("fzf-*.json"))
        self.assertEqual(len(dumps), 1)
        return json.loads(dumps[0].read_text())

    def render10_chats(self, session_id: str) -> list[dict]:
        return [
            self.records.chat_ref(
                session_id="3", id="abcdef0123456789", role="original", cwd="/r",
                transcript_path="/t", how="spawn", chat_id=None, started_at=self.now - 900,
            ),
            self.records.chat_ref(
                session_id="3", id=None, role="fork", cwd="/r", transcript_path="/t",
                how="fork", chat_id="0123456789abcdef", bundle_path="/h/b",
                started_at=self.now - 780,
            ),
        ]

    def history_fixture(self) -> None:
        self.records.llm(
            id="ex", name="x" * 30, state="exited", cwd="/r", tags=("a", "b"),
            created_at=self.now - 2000, ended_at=self.now - 600, last_activity=self.now - 700,
            chats=self.render10_chats("ex"),
        )
        self.records.other(
            id="ar", name="ed", role="nvim", state="archived", cwd="/repo", tags=(),
            created_at=self.now - 90, ended_at=None,
        )

    def artifacts_fixture(self) -> tuple[str, str]:
        a = "abcdef01-2345-4678-8abc-000000000001"
        b = "ffffffff-0000-4000-8000-000000000002"
        self.records.artifact(
            id=a, title="Rust port plan", filename="plan.md", created_at=self.now - 900, group=None,
            history=[
                {"session_id": "sess-aaaaaaaa-1", "at": self.now - 900, "rev": 0, "changes": None},
                {"session_id": "user", "at": self.now - 300, "rev": 1, "changes": "typo fix"},
                {"session_id": "sess-aaaaaaaa-1", "at": self.now - 90, "rev": 2, "changes": None},
            ],
            revs={0: "v0\n", 1: "v1\n", 2: "v2\n"},
            current="v2 edited\n",
        )
        self.records.artifact(
            id=b, title=None, filename="a-very-long-file-name-that-overflows.txt",
            created_at=self.now - 900, group="g1",
            history=[{"session_id": "gone-1234-5678", "at": self.now - 900, "rev": 0, "changes": None}],
            revs={0: "t0\n"},
            current="t0\n",
        )
        self.records.llm(
            id="sess-aaaaaaaa-1", name="worker-1", state="exited", group="derived-g",
            created_at=self.now - 5400, ended_at=self.now - 100, chats=[],
        )
        return a, b

    # ----- T-RENDER-01 ---------------------------------------------------------------------

    def test_t_render_01_reltime(self):
        for record_id, ended in (
            ("f", self.now + 100000), ("m", self.now - 90), ("h", self.now - 5400),
            ("d", self.now - 3 * 86400),
        ):
            self.records.llm(id=record_id, name=record_id, state="exited", cwd="/r", tags=(),
                             chats=[], created_at=self.now - 200000, ended_at=ended)
        self.records.llm(id="n", name="n", state="exited", cwd="/r", tags=(), chats=[])
        self.records.patch("n", ended_at=None, last_activity=None, created_at=None)
        self.records.llm(id="z", name="z", state="exited", cwd="/r", tags=(), chats=[])
        self.records.patch("z", ended_at=0, last_activity=None, created_at=None)

        result = self.tx(["history"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.err, "")
        expected = (
            "HISTORY\n"
            "  f                        exited      0s   0c   /r\n"
            "  m                        exited      1m   0c   /r\n"
            "  h                        exited      1h   0c   /r\n"
            "  d                        exited      3d   0c   /r\n"
            "  n                        exited       -   0c   /r\n"
            "  z                        exited       -   0c   /r\n"
        )
        self.assertEqual(result.out, expected)
        self.assert_golden("render/01", result.out)

    # ----- T-RENDER-02 ---------------------------------------------------------------------

    def test_t_render_02_location_text(self):
        names = ["loc-none", "loc-one", "loc-three", "loc-22", "loc-23", "loc-30"]
        for offset, name in enumerate(names):
            self.records.other(id=name, name=name, role="nvim", state="alive", tags=(),
                               created_at=self.now - 3600 - 60 * offset)
            self.live(name)
        # Global options need a running server (the record sessions above); set before `Views`.
        self.tmux.run("set-option", "-g", "base-index", "1", check=True)
        self.tmux.run("set-option", "-g", "pane-base-index", "1", check=True)

        def attach(name: str) -> str:
            return f"env -u TMUX {txkit.REAL_TMUX} -L {self.tmux.socket} attach -t ={name}"

        self.tmux.run("new-session", "-d", "-s", "Views", "-n", "work", attach("loc-one"), check=True)
        self.tmux.run("split-window", "-t", "Views:work", attach("loc-three"), check=True)
        self.tmux.run("new-window", "-t", "Views", "-n", "x", attach("loc-three"), check=True)
        self.tmux.run("new-window", "-t", "Views", "-n", "y", attach("loc-three"), check=True)
        self.tmux.run("new-window", "-t", "Views", "-n", "a" * 22, attach("loc-22"), check=True)
        self.tmux.run("new-window", "-t", "Views", "-n", "a" * 23, attach("loc-23"), check=True)
        self.tmux.run("new-window", "-t", "Views", "-n", "a" * 30, attach("loc-30"), check=True)
        self.tmux.run("new-window", "-t", "Views", "-n", "b", attach("loc-30"), check=True)
        self.wait_until(
            lambda: len(self.tmux.run("list-clients", "-F", "#{client_session}").stdout.splitlines()) == 8
        )

        result = self.tx(["ls"])
        self.assertEqual(result.code, 0, result.err)
        cells = ["—", "work[1]", "work[2] +2", "a" * 22 + "[1]", "a" * 21 + "…[1]", "a" * 18 + "…[1] +1"]
        for cell in cells[3:]:
            self.assertEqual(len(cell), 25)
        expected = "PROCESSES\n" + "".join(
            f"  {name:<24} {'alive':<8} {cell:<25} {'—':<6}\n" for name, cell in zip(names, cells)
        )
        self.assertEqual(result.out, expected)
        self.assert_golden("render/02", result.out)

        show = json.loads(self.tx(["show", "loc-three"]).out)
        self.assertEqual(len(show["attached_to"]), 3)
        for location in show["attached_to"]:
            self.assertEqual(
                list(location), ["host", "window_index", "window_name", "pane_id", "pane_index"]
            )
            self.assertEqual(location["host"], "Views")
        self.assertEqual(
            [(loc["window_name"], loc["pane_index"]) for loc in show["attached_to"]],
            [("work", "2"), ("x", "1"), ("y", "1")],
        )

    # ----- T-RENDER-03 ---------------------------------------------------------------------

    def render03_fixture(self) -> None:
        self.records.llm(id="w1", name="worker-1", state="waiting", tags=("docs",),
                         created_at=self.now - 5400, last_activity=self.now - 90)
        self.records.other(id="ed", name="ed", role="nvim", state="alive", tags=(),
                           created_at=self.now - 5400)
        self.live("w1")
        self.live("ed")

    def test_t_render_03_render_ls(self):
        self.render03_fixture()
        result = self.tx(["ls"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(
            result.out,
            "PROCESSES\n"
            "  worker-1                 waiting  —                         1m     [docs]\n"
            "  ed                       alive    —                         —     \n",
        )
        self.assert_golden("render/03", result.out)

    def test_t_render_03_long_name_not_truncated_and_empty(self):
        self.assertEqual(self.tx(["ls"]).out, "PROCESSES\n")
        name = "n" * 30
        self.records.other(id="long", name=name, role="shell", state="alive", tags=(),
                           created_at=self.now - 5400)
        self.live("long")
        result = self.tx(["ls"])
        self.assertEqual(result.out, f"PROCESSES\n  {name} alive    —                         —     \n")

    # ----- T-RENDER-04 ---------------------------------------------------------------------

    def test_t_render_04_picker_namew(self):
        self.fakes.configure("fzf", exit_code=130)
        self.records.other(id="nv", name="x", role="nvim", state="alive", tags=(), created_at=self.now - 90)
        self.live("nv")
        pairs = [(118, 10), (118, 53), (118, 60), (118, 80), (200, 80), (200, 59), (60, 5),
                 (77, 20), (76, 20), (125, 60), (126, 61)]
        expected = [12, 53, 53, 53, 60, 59, 12, 12, 12, 60, 60]
        seen = []
        for index, (cols, longest) in enumerate(pairs):
            self.records.patch("nv", name="x" * longest)
            dump = self.attach_dump(cols, self.root / f"fzf-run-{index}")
            seen.append(int(dump["env"]["NAMEW"]))
        self.assertEqual(seen, expected)
        text = "".join(f"NAMEW={value}\n" for value in seen)
        self.assert_golden("render/04", text)

    # ----- T-RENDER-05 ---------------------------------------------------------------------

    def test_t_render_05_trunc(self):
        self.records.other(id="t12", name="abcdefghijkl", role="nvim", state="alive", tags=(),
                           created_at=self.now - 5400)
        self.records.other(id="t13", name="abcdefghijklm", role="nvim", state="alive", tags=(),
                           created_at=self.now - 5460)
        self.live("t12")
        self.live("t13")
        result = self.tx(["_list"], env={"NAMEW": "12"})
        rows = [row.split("\t") for row in result.raw_out.rstrip("\n").split("\n")]
        self.assertEqual([row[0] for row in rows], ["abcdefghijkl", "abcdefghijklm"])
        self.assertTrue(rows[0][4].startswith("abcdefghijkl   —"), rows[0][4])
        self.assertTrue(rows[1][4].startswith("abcdefghijk…   —"), rows[1][4])
        self.assert_golden("render/05", result.raw_out)

    # ----- T-RENDER-06 ---------------------------------------------------------------------

    def test_t_render_06_picker_row_local_llm(self):
        self.records.llm(id="0f3a-id", name="worker-1", state="waiting", tags=("docs",),
                         created_at=self.now - 5400, last_activity=self.now - 90)
        self.live("0f3a-id")
        result = self.tx(["_list"], env={"NAMEW": "12"})
        self.assertEqual(result.code, 0, result.err)
        expected = (
            "worker-1\t [docs]\tL\t0f3a-id\t"
            "worker-1       —                         1h      "
            f"{WARN}1m    {RESET_FG} {cube(167)}llm  {RESET_FG} {cube(140)}[docs]{RESET_FG}\n"
        )
        self.assertEqual(result.raw_out, expected)
        self.assert_golden("render/06", result.raw_out)

    def test_t_render_06_default_namew_18(self):
        self.records.llm(id="0f3a-id", name="worker-1", state="waiting", tags=("docs",),
                         created_at=self.now - 5400, last_activity=self.now - 90)
        self.live("0f3a-id")
        result = self.tx(["_list"])
        visual = result.raw_out.split("\t")[4]
        self.assertTrue(visual.startswith("worker-1" + " " * 10 + "   —"), visual)

    # ----- T-RENDER-07 ---------------------------------------------------------------------

    def test_t_render_07_picker_row_nvim_truncated(self):
        self.records.other(id="a-very-long-id", name="a-very-long-session-name", role="nvim",
                           state="alive", tags=(), created_at=self.now - 90)
        self.live("a-very-long-id")
        result = self.tx(["_list"], env={"NAMEW": "12"})
        expected = (
            "a-very-long-session-name\t\tL\ta-very-long-id\t"
            "a-very-long…   —                         1m      "
            f"{WARN}—     {RESET_FG} {cube(80)}nvim {RESET_FG}\n"
        )
        self.assertEqual(result.raw_out, expected)
        self.assert_golden("render/07", result.raw_out)

    # ----- T-RENDER-08 ---------------------------------------------------------------------

    def test_t_render_08_picker_display_rows(self):
        self.render03_fixture()
        rows = self.list_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual([row[0] for row in rows], ["worker-1", "ed"])
        self.assertEqual([row[3] for row in rows], ["w1", "ed"])
        self.assertEqual([row[2] for row in rows], ["L", "L"])
        self.assertIn(f"{WARN}—     {RESET_FG}", rows[1][4])
        self.assertIn(f"{WARN}1m    {RESET_FG}", rows[0][4])

    def test_t_render_08_no_live_records(self):
        result = self.tx(["_list"], env={"NAMEW": "12"})
        self.assertEqual(result.code, 0)
        self.assertEqual(result.raw_out, "\n")

    # ----- T-RENDER-09 ---------------------------------------------------------------------

    def test_t_render_09_render_history(self):
        self.history_fixture()
        result = self.tx(["history"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(
            result.out,
            "HISTORY\n"
            "  ed                       archived    1m   0c   /repo\n"
            "  xxxxxxxxxxxxxxxxxxxxxxx… exited     10m   2c  [a] [b]  /r\n",
        )
        self.assert_golden("render/09", result.out)

    def test_t_render_09_no_terminal_records(self):
        self.assertEqual(self.tx(["history"]).out, "HISTORY\n  (no exited or archived sessions)\n")

    # ----- T-RENDER-10 ---------------------------------------------------------------------

    def test_t_render_10_render_chats(self):
        self.history_fixture()
        result = self.tx(["chat", "ls", "ex"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(
            result.out,
            "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx — 2 chat(s)\n"
            "  abcdef01  original  spawn               15m ago   —\n"
            "  pending   fork      fork←01234567       13m ago   /h/b\n",
        )
        self.assert_golden("render/10", result.out)

    def test_t_render_10_no_chats_and_unknown(self):
        self.history_fixture()
        result = self.tx(["chat", "ls", "ar"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, "ed — 0 chat(s)\n  (none)\n")
        missing = self.tx(["chat", "ls", "x"])
        self.assertEqual(missing.code, 1)
        self.assertEqual(missing.out, "")
        self.assertEqual(missing.err, "tx chat ls: session 'x' not found\n")

    # ----- T-RENDER-11 ---------------------------------------------------------------------

    def test_t_render_11_actor_label(self):
        self.records.llm(id="sess-aaaaaaaa-1", name="worker-1", state="exited",
                         created_at=self.now - 5000, ended_at=self.now - 100, chats=[])
        self.records.llm(id="x", name="", state="exited",
                         created_at=self.now - 5000, ended_at=self.now - 100, chats=[])
        artifact_id = "11111111-2222-4333-8444-000000000011"
        authors = ["user", "gone-1234-5678", "sess-aaaaaaaa-1", "x"]
        self.records.artifact(
            id=artifact_id, title="T", filename="t.md", created_at=self.now - 900,
            history=[
                {"session_id": author, "at": self.now - 900 + 100 * rev, "rev": rev, "changes": None}
                for rev, author in enumerate(authors)
            ],
            revs={rev: f"r{rev}\n" for rev in range(4)},
        )
        listing = self.tx(["artifact", "ls"])
        self.assertEqual(listing.code, 0, listing.err)
        self.assertEqual(listing.err, "")
        self.assertEqual(
            listing.out,
            "ARTIFACTS\n  11111111    4r    10m  T                            [user] [gone-123] [worker-1] [x]\n",
        )
        show = self.tx(["artifact", "show", artifact_id])
        self.assertEqual(show.code, 0, show.err)
        labels = [line[23:].rstrip() for line in show.lines if line.startswith("    rev ")]
        self.assertEqual(labels, ["user", "gone-123", "worker-1", "x"])

    # ----- T-RENDER-12 ---------------------------------------------------------------------

    def test_t_render_12_render_artifacts(self):
        self.artifacts_fixture()
        result = self.tx(["artifact", "ls"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.err, "")
        self.assertEqual(
            result.out,
            "ARTIFACTS\n"
            "  abcdef01    3r     1m  Rust port plan               [worker-1] [user]\n"
            "  ffffffff    1r    15m  a-very-long-file-name-that-… [gone-123]\n",
        )
        self.assert_golden("render/12", result.out)

    def test_t_render_12_no_artifacts(self):
        self.assertEqual(self.tx(["artifact", "ls"]).out, "ARTIFACTS\n  (none)\n")

    # ----- T-RENDER-13 ---------------------------------------------------------------------

    def test_t_render_13_render_artifact_show(self):
        a, _ = self.artifacts_fixture()
        result = self.tx(["artifact", "show", a])
        self.assertEqual(result.code, 0, result.err)
        expected = (
            "Rust port plan  (abcdef01-2345-4678-8abc-000000000001)\n"
            "  filename:   plan.md\n"
            "  created:    15m ago\n"
            "  updated:    1m ago\n"
            "  group:      derived: derived-g\n"
            "  revisions:  3\n"
            "  working:    dirty — un-snapshotted edits (close with `tx artifact modify`)\n"
            "  history:\n"
            "    rev 0     15m ago  worker-1            \n"
            "    rev 1      5m ago  user                  typo fix\n"
            "    rev 2      1m ago  worker-1            \n"
        )
        self.assertEqual(result.out, expected)
        self.assert_golden("render/13", result.out)

    def test_t_render_13_explicit_group_clean_and_prefix(self):
        a, b = self.artifacts_fixture()
        result = self.tx(["artifact", "show", b])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(
            result.out,
            f"a-very-long-file-name-that-overflows.txt  ({b})\n"
            "  filename:   a-very-long-file-name-that-overflows.txt\n"
            "  created:    15m ago\n"
            "  updated:    15m ago\n"
            "  group:      g1\n"
            "  revisions:  1\n"
            "  working:    clean\n"
            "  history:\n"
            "    rev 0     15m ago  gone-123            \n",
        )
        by_prefix = self.tx(["artifact", "show", "abcdef01"])
        self.assertEqual(by_prefix.code, 0, by_prefix.err)
        self.assertTrue(by_prefix.out.startswith(f"Rust port plan  ({a})\n"))

    # ----- T-RENDER-14 ---------------------------------------------------------------------

    def test_t_render_14_tag_color_index_hash(self):
        tags = ["llm", "nvim", "shell", "other", "worker", "review", "tx", "docs", "rust-port",
                "test", "plan", "ide", "assistant", "a", "", "é"]
        cubes = [167, 80, 38, 210, 140, 141, 38, 140, 37, 198, 167, 37, 210, 167, 210, 73]
        self.records.llm(id="tags", name="tags", state="idle", tags=tuple(tags),
                         created_at=self.now - 5400, last_activity=self.now - 90)
        self.live("tags")
        result = self.tx(["_list"], env={"NAMEW": "12"})
        self.assertEqual(result.code, 0, result.err)
        chips = "".join(f" {cube(n)}[{tag}]{RESET_FG}" for tag, n in zip(tags, cubes))
        self.assertTrue(result.raw_out.endswith(chips + "\n"), result.raw_out)
        self.assertIn(f" {cube(73)}[é]{RESET_FG}", result.raw_out)
        self.assertNotIn(f"{cube(198)}[é]", result.raw_out)
        self.assert_golden("render/14", result.raw_out)

    # ----- T-RENDER-15 / 17 ----------------------------------------------------------------

    def roles_fixture(self, llm_tags: tuple[str, ...]) -> None:
        self.records.llm(id="r-llm", name="r-llm", state="idle", tags=llm_tags,
                         created_at=self.now - 5400, last_activity=self.now - 90)
        for offset, role in enumerate(("nvim", "shell", "other")):
            self.records.other(id=f"r-{role}", name=f"r-{role}", role=role, state="alive", tags=(),
                               created_at=self.now - 5400 - 60 * offset)
        for record_id in ("r-llm", "r-nvim", "r-shell", "r-other"):
            self.live(record_id)

    def test_t_render_15_tag_cube_tag_ansi(self):
        self.roles_fixture(("worker", "docs"))
        result = self.tx(["_list"], env={"NAMEW": "12"})
        self.assertEqual(result.code, 0, result.err)
        rows = [row.split("\t") for row in result.raw_out.rstrip("\n").split("\n")]
        self.assertEqual([row[0] for row in rows], ["r-llm", "r-nvim", "r-shell", "r-other"])
        self.assertTrue(rows[0][4].endswith(
            f" {cube(167)}llm  {RESET_FG} {cube(140)}[worker]{RESET_FG} {cube(140)}[docs]{RESET_FG}"
        ), rows[0][4])
        self.assertTrue(rows[1][4].endswith(f" {cube(80)}nvim {RESET_FG}"), rows[1][4])
        self.assertTrue(rows[2][4].endswith(f" {cube(38)}shell{RESET_FG}"), rows[2][4])
        self.assertTrue(rows[3][4].endswith(f" {cube(210)}other{RESET_FG}"), rows[3][4])
        self.assert_golden("render/15", result.raw_out)

    def test_t_render_17_role_cell_colour(self):
        self.roles_fixture(())
        rows = self.list_rows()
        self.assertEqual([row[0] for row in rows], ["r-llm", "r-nvim", "r-shell", "r-other"])
        for row, (role, n) in zip(rows, (("llm", 167), ("nvim", 80), ("shell", 38), ("other", 210))):
            self.assertTrue(row[4].endswith(f" {cube(n)}{role:<5}{RESET_FG}"), row[4])

    # ----- T-RENDER-16 ---------------------------------------------------------------------

    def test_t_render_16_semantic_palette_constants(self):
        self.fakes.configure("fzf", exit_code=130)
        self.records.other(id="nv", name="x" * 53, role="nvim", state="alive", tags=(), created_at=self.now - 90)
        self.live("nv")
        dump = self.attach_dump(118, self.root / "fzf-run-16")
        argv = dump["argv"]
        header = header_cols(53)
        color = next(arg for arg in argv if arg.startswith("--color="))
        focus = next(arg for arg in argv if arg.startswith("--bind=focus:"))
        ctrl_d = next(arg for arg in argv if arg.startswith("--bind=ctrl-d:"))
        self.assertEqual(
            color,
            "--color=fg:#a9b1d6,pointer:#7aa2f7,fg+:#a9b1d6:regular,bg+:236:regular,hl:#7aa2f7,"
            "hl+:#7aa2f7,header:#a9b1d6,footer:#a9b1d6,prompt:#a9b1d6,query:#c0caf5",
        )
        self.assertEqual(
            focus,
            "--bind=focus:transform-header(printf '\x1b[38;2;122;162;247m\x1b[1m%s\x1b[0m "
            "\x1b[38;2;192;202;245m\x1b[1m%s\x1b[0m\\n%s' {1} {2} '" + header + "')"
            '+execute-silent(: >"$TX_ARM_FILE")+unbind(y,n)',
        )
        self.assertEqual(
            ctrl_d,
            "--bind=ctrl-d:execute-silent(printf '%s' {1} >\"$TX_ARM_FILE\")"
            "+transform-header(printf '\x1b[38;2;224;175;104m\x1b[1m ⚠  Kill \"%s\"? [y/N]\x1b[0m\\n%s' "
            "{1} '" + header + "')+rebind(y,n)",
        )
        self.assertEqual(
            header,
            "NAME" + " " * 49 + "   LOCATION" + " " * 17 + " STARTED IDLE   ROLE  TAGS",
        )
        self.assert_golden("render/16", "\n".join([color, focus, ctrl_d]) + "\n")


if __name__ == "__main__":
    unittest.main()
