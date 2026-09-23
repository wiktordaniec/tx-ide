"""EDITOR — `lib/tx/session_editor.py` + `tx _edit-session` (spec section 02).

The two-field curses form is driven inside a detached session `Ed` on the private server
(`tx _edit-session <pane>` followed by an `rc=<n>` marker file), keys go in through `send-keys`, the
rendered rows come back through `capture-pane`, and the writes are asserted on the record and
`log.jsonl`. Every form targets pane `%P` of view `Views` that nests process `U` (record `w`).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from txkit import TxCase, wait_until

HINT_ROW = "  ↑↓ switch   Enter save   Esc cancel"
TAGS_HELP_ROW = "            Comma-separated · empty clears tags"
EMPTY_NAME_ROW = "  Name cannot be empty."
FORM_VISIBLE = "› Name:"


class TestEditor(TxCase):
    # ----- fixture helpers -----------------------------------------------------------------

    def nested_pane(self, name: str = "w", tag: str = "a,b") -> tuple[str, dict]:
        """View `Views` whose pane nests process `U` (record `name`, tags from `tag`)."""
        self.spawn_view("Views")
        record = self.spawn_process(name, tag=tag)
        pane = self.tmux.pane_id("Views")
        self.tmux.nest_attach(pane, record["id"])
        return pane, record

    def open_form(self, pane: str, session: str = "Ed", size: tuple[str, str] = ("80", "24"),
                  gate: Path | None = None) -> Path:
        """Run `tx _edit-session pane` in a detached `session`; returns the rc marker path. With
        `gate`, the form waits for that file to exist before starting (so the session can be sized
        before a nested client exists — `window-size latest` sizes a new detached session from ANY
        attached client, and tmux 3.4 aborts on a manual-size 5-row session)."""
        marker = self.root / f"rc-{session}"
        prelude = f"until [ -e {gate} ]; do sleep 0.05; done; " if gate is not None else ""
        self.tmux.run(
            "new-session", "-d", "-s", session, "-x", size[0], "-y", size[1],
            f"{prelude}tx _edit-session {pane}; echo rc=$? > {marker}; sleep 30",
            check=True,
        )
        if gate is None:
            self.wait_form(session)
        return marker

    def wait_form(self, session: str = "Ed") -> None:
        self.wait_until(lambda: FORM_VISIBLE in self.tmux.capture(session))

    def rows(self, session: str = "Ed") -> list[str]:
        return self.tmux.capture(session).split("\n")

    def wait_row(self, session: str, index: int, expected: str) -> None:
        self.wait_until(lambda: self.rows(session)[index] == expected)
        self.assertEqual(self.rows(session)[index], expected)

    def keys(self, session: str, *keys: str) -> None:
        """Send tmux key names; a `("lit", text)`-style literal is given as `"=text"`."""
        for key in keys:
            if key.startswith("="):
                self.tmux.send_keys(session, key[1:], literal=True)
            else:
                self.tmux.send_keys(session, key)

    def wait_rc(self, marker: Path) -> int:
        self.wait_until(lambda: marker.exists() and marker.read_text().strip() != "")
        return int(marker.read_text().strip().removeprefix("rc="))

    def assert_form_stays_open(self, marker: Path, seconds: float = 1.0) -> None:
        """A negative wait: the rc marker must not appear (the form did not return)."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.assertFalse(marker.exists(), "the form returned")
            time.sleep(0.05)

    def snapshot(self, record: dict) -> tuple[bytes, int]:
        return self.records.path(record["id"]).read_bytes(), len(self.log_lines())

    def assert_unchanged(self, record: dict, snapshot: tuple[bytes, int]) -> None:
        self.assertEqual(self.records.path(record["id"]).read_bytes(), snapshot[0])
        self.assertEqual(len(self.log_lines()), snapshot[1])

    # ----- T-EDITOR-01 form accept / cancel ------------------------------------------------

    def test_t_editor_01_enter_escape_ctrl_c_leave_record_untouched(self):
        pane, record = self.nested_pane()
        snapshot = self.snapshot(record)
        for session, key in (("Ed", "Enter"), ("Ed2", "Escape"), ("Ed3", "C-c")):
            marker = self.open_form(pane, session)
            self.keys(session, key)
            self.assertEqual(self.wait_rc(marker), 0, key)
            self.assert_unchanged(record, snapshot)
        shown = json.loads(self.tx(["show", record["id"]]).out)
        self.assertEqual((shown["name"], shown["tags"]), ("w", ["a", "b"]))

    def test_t_editor_01_typed_then_escape_writes_nothing(self):
        pane, record = self.nested_pane()
        snapshot = self.snapshot(record)
        marker = self.open_form(pane)
        self.keys("Ed", "=zzz")
        self.wait_row("Ed", 1, "  › Name:   wzzz")
        self.keys("Ed", "Escape")
        self.assertEqual(self.wait_rc(marker), 0)
        self.assert_unchanged(record, snapshot)
        self.assertEqual(json.loads(self.tx(["show", record["id"]]).out)["name"], "w")

    # ----- T-EDITOR-02 empty name refused ---------------------------------------------------

    def test_t_editor_02_empty_name_refused_then_new_name_accepted(self):
        pane, record = self.nested_pane()
        marker = self.open_form(pane)
        self.keys("Ed", "C-u", "Enter")
        self.wait_row("Ed", 6, EMPTY_NAME_ROW)
        self.assertEqual(self.rows("Ed")[1], "  › Name:")
        self.assert_form_stays_open(marker)
        self.keys("Ed", "=w2", "Enter")
        self.assertEqual(self.wait_rc(marker), 0)
        self.assertEqual(json.loads(self.tx(["show", record["id"]]).out)["name"], "w2")
        tail = self.log_tail()[0]
        self.assertEqual((tail["type"], tail["msg"]), ("rename", "w → w2"))

    def test_t_editor_02_whitespace_only_name_refused(self):
        pane, record = self.nested_pane()
        snapshot = self.snapshot(record)
        marker = self.open_form(pane)
        self.keys("Ed", "C-u", "=   ", "Enter")
        self.wait_row("Ed", 6, EMPTY_NAME_ROW)
        self.assert_form_stays_open(marker)
        self.keys("Ed", "Escape")
        self.assertEqual(self.wait_rc(marker), 0)
        self.assert_unchanged(record, snapshot)

    def test_t_editor_02_returned_name_is_not_stripped(self):
        pane, record = self.nested_pane()
        marker = self.open_form(pane)
        self.keys("Ed", "C-u", "=  x ", "Enter")
        self.assertEqual(self.wait_rc(marker), 0)
        self.assertEqual(json.loads(self.tx(["show", record["id"]]).out)["name"], "  x ")
        tail = self.log_tail()[0]
        self.assertEqual((tail["type"], tail["msg"]), ("rename", "w →   x "))

    # ----- T-EDITOR-03 field navigation and editing keys -----------------------------------

    def test_t_editor_03_rendered_rows(self):
        pane, _ = self.nested_pane("abc", "t")
        self.open_form(pane)
        rows = self.rows("Ed")
        self.assertEqual(rows[1], "  › Name:   abc")
        self.assertEqual(rows[3], "    Tags:   t")
        self.assertEqual(rows[4], TAGS_HELP_ROW)
        self.assertEqual(rows[6], HINT_ROW)

    def test_t_editor_03_navigation_keys_toggle_active_field(self):
        pane, _ = self.nested_pane("abc", "t")
        self.open_form(pane)
        for key, active in (("Down", "Tags"), ("Up", "Name"), ("Tab", "Tags"), ("BTab", "Name")):
            self.keys("Ed", key)
            if active == "Tags":
                self.wait_row("Ed", 3, "  › Tags:   t")
                self.assertEqual(self.rows("Ed")[1], "    Name:   abc")
            else:
                self.wait_row("Ed", 1, "  › Name:   abc")
                self.assertEqual(self.rows("Ed")[3], "    Tags:   t")

    def test_t_editor_03_left_left_ctrl_k_kills_to_end(self):
        pane, record = self.nested_pane("abc", "t")
        marker = self.open_form(pane)
        self.keys("Ed", "Left", "Left", "C-k")
        self.wait_row("Ed", 1, "  › Name:   a")
        self.keys("Ed", "Enter")
        self.assertEqual(self.wait_rc(marker), 0)
        self.assertEqual(json.loads(self.tx(["show", record["id"]]).out)["name"], "a")
        tail = self.log_tail()[0]
        self.assertEqual((tail["type"], tail["msg"]), ("rename", "abc → a"))

    def test_t_editor_03_home_delete_char(self):
        pane, record = self.nested_pane("abc", "t")
        marker = self.open_form(pane)
        self.keys("Ed", "Home", "DC")
        self.wait_row("Ed", 1, "  › Name:   bc")
        self.keys("Ed", "Enter")
        self.assertEqual(self.wait_rc(marker), 0)
        self.assertEqual(json.loads(self.tx(["show", record["id"]]).out)["name"], "bc")

    def test_t_editor_03_control_key_aliases_edit_the_field(self):
        pane, record = self.nested_pane("abc", "t")
        marker = self.open_form(pane)
        # C-a (home), C-f (right), BSpace → drops the `a`
        self.keys("Ed", "C-a", "C-f", "BSpace")
        self.wait_row("Ed", 1, "  › Name:   bc")
        # C-e (end), C-b (left), printable insert → `bXc`
        self.keys("Ed", "C-e", "C-b", "=X")
        self.wait_row("Ed", 1, "  › Name:   bXc")
        # Right (to the end), C-u kills to start
        self.keys("Ed", "Right", "C-u")
        self.wait_row("Ed", 1, "  › Name:")
        self.keys("Ed", "=q", "End", "Enter")
        self.assertEqual(self.wait_rc(marker), 0)
        self.assertEqual(json.loads(self.tx(["show", record["id"]]).out)["name"], "q")

    def test_t_editor_03_tab_edits_tags_only(self):
        pane, record = self.nested_pane("abc", "t")
        before = len(self.log_lines())
        marker = self.open_form(pane)
        self.keys("Ed", "Tab", "=x")
        self.wait_row("Ed", 3, "  › Tags:   tx")
        self.keys("Ed", "Enter")
        self.assertEqual(self.wait_rc(marker), 0)
        shown = json.loads(self.tx(["show", record["id"]]).out)
        self.assertEqual((shown["name"], shown["tags"]), ("abc", ["tx"]))
        new_lines = self.log_lines()[before:]
        self.assertEqual([(line["type"], line["msg"]) for line in new_lines], [("tag", "abc tx")])

    def test_t_editor_03_ctrl_u_on_name_then_enter_refused(self):
        pane, record = self.nested_pane("abc", "t")
        snapshot = self.snapshot(record)
        marker = self.open_form(pane)
        self.keys("Ed", "C-u", "Enter")
        self.wait_row("Ed", 6, EMPTY_NAME_ROW)
        self.assert_form_stays_open(marker)
        self.keys("Ed", "Escape")
        self.assertEqual(self.wait_rc(marker), 0)
        self.assert_unchanged(record, snapshot)

    def test_t_editor_03_any_key_clears_the_error_row(self):
        pane, _ = self.nested_pane("abc", "t")
        self.open_form(pane)
        self.keys("Ed", "C-u", "Enter")
        self.wait_row("Ed", 6, EMPTY_NAME_ROW)
        self.keys("Ed", "Right")
        self.wait_row("Ed", 6, HINT_ROW)

    def test_t_editor_03_too_small_window_only_cancels(self):
        self.spawn_view("Views")
        record = self.spawn_process("abc", tag="t")
        pane = self.tmux.pane_id("Views")
        gate = self.root / "go"
        marker = self.open_form(pane, size=("20", "5"), gate=gate)
        self.tmux.nest_attach(pane, record["id"])
        snapshot = self.snapshot(record)
        gate.write_text("")
        self.wait_until(lambda: "Resize to edit" in self.tmux.capture("Ed"))
        self.assertEqual(self.tmux.display("Ed", "#{pane_width}x#{pane_height}"), "20x5")
        self.assertEqual(self.rows("Ed")[0], "Resize to edit")
        self.keys("Ed", "Enter")
        self.assert_form_stays_open(marker)
        self.keys("Ed", "Escape")
        self.assertEqual(self.wait_rc(marker), 0)
        self.assert_unchanged(record, snapshot)

    # ----- T-EDITOR-04 tx _edit-session resolution + writes ---------------------------------

    def test_t_editor_04_rename_then_tag_by_id(self):
        pane, record = self.nested_pane("w", "a")
        before = len(self.log_lines())
        marker = self.open_form(pane)
        self.keys("Ed", "C-u", "=w2", "Tab", "End", "=,b")
        self.wait_row("Ed", 3, "  › Tags:   a,b")
        self.assertEqual(self.rows("Ed")[1], "    Name:   w2")
        self.keys("Ed", "Enter")
        self.assertEqual(self.wait_rc(marker), 0)
        shown = json.loads(self.tx(["show", record["id"]]).out)
        self.assertEqual((shown["name"], shown["tags"]), ("w2", ["a", "b"]))
        new_lines = self.log_lines()[before:]
        self.assertEqual(
            [(line["type"], line["msg"]) for line in new_lines],
            [("rename", "w → w2"), ("tag", "w2 a,b")],
        )

    def test_t_editor_04_rename_only_when_tags_unchanged(self):
        pane, record = self.nested_pane("w", "a")
        before = len(self.log_lines())
        marker = self.open_form(pane)
        self.keys("Ed", "C-u", "=w2")
        self.wait_row("Ed", 1, "  › Name:   w2")
        self.keys("Ed", "Enter")
        self.assertEqual(self.wait_rc(marker), 0)
        shown = json.loads(self.tx(["show", record["id"]]).out)
        self.assertEqual((shown["name"], shown["tags"]), ("w2", ["a"]))
        new_lines = self.log_lines()[before:]
        self.assertEqual([(line["type"], line["msg"]) for line in new_lines], [("rename", "w → w2")])

    def test_t_editor_04_plain_enter_and_escape_write_nothing(self):
        pane, record = self.nested_pane("w", "a")
        snapshot = self.snapshot(record)
        marker = self.open_form(pane)
        self.keys("Ed", "Enter")
        self.assertEqual(self.wait_rc(marker), 0)
        self.assert_unchanged(record, snapshot)
        marker = self.open_form(pane, "Ed2")
        self.keys("Ed2", "Escape")
        self.assertEqual(self.wait_rc(marker), 0)
        self.assert_unchanged(record, snapshot)

    def test_t_editor_04_unresolvable_panes_exit_1_without_a_form(self):
        self.spawn_view("Views")
        view_pane = self.tmux.pane_id("Views")
        self.tmux.new_session("p", "bash")
        untracked_pane = self.tmux.pane_id("p")
        before = len(self.log_lines())

        result = self.tx(["_edit-session", view_pane])
        self.assertEqual(result.code, 1)
        self.assertEqual(result.err, "tx _edit-session: no tx session in this pane (view homes cannot be edited)\n")
        self.assertEqual(result.out, "")

        result = self.tx(["_edit-session", untracked_pane])
        self.assertEqual(result.code, 1)
        self.assertEqual(result.err, "tx _edit-session: the session in this pane is not tx-managed\n")
        self.assertEqual(result.out, "")

        result = self.tx(["_edit-session", "%999"])
        self.assertEqual(result.code, 1)
        self.assertEqual(result.err, "tx _edit-session: no tx session in this pane (view homes cannot be edited)\n")
        self.assertEqual(result.out, "")
        self.assertEqual(len(self.log_lines()), before)

    def test_t_editor_04_name_collision_leaves_name_and_tags_untouched(self):
        pane, record = self.nested_pane("w", "a")
        self.spawn_process("taken")
        snapshot = self.snapshot(record)
        stderr = self.root / "stderr"
        marker = self.root / "rc-Ed"
        self.tmux.run(
            "new-session", "-d", "-s", "Ed", "-x", "80", "-y", "24",
            f"tx _edit-session {pane} 2>{stderr}; echo rc=$? > {marker}; sleep 30",
            check=True,
        )
        self.wait_form()
        self.keys("Ed", "C-u", "=taken", "Tab", "End", "=,b")
        self.wait_row("Ed", 3, "  › Tags:   a,b")
        self.keys("Ed", "Enter")
        self.assertEqual(self.wait_rc(marker), 1)
        self.assertEqual(stderr.read_text(), "tx _edit-session: session 'taken' already exists\n")
        shown = json.loads(self.tx(["show", record["id"]]).out)
        self.assertEqual((shown["name"], shown["tags"]), ("w", ["a"]))
        self.assert_unchanged(record, snapshot)
