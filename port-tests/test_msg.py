"""MSG — messages.py + send paths (spec section 04, T-MSG-01..04).

`tx send-message` / `tx send-user-message` type an envelope into the target's pane. T-MSG-05..14
(the transcript parser) are DROPPED — nothing observable reaches `parse_message`.

Harness: target record `bob` (id `t1`) with tmux session `t1` running `cat`; sender record `alice`
(id `s1`) with tmux session `s1`; `tx_inside("s1", …)` runs tx with `$TMUX` set and `#S` == `s1`.
"""

from __future__ import annotations

from txkit import TxCase, expected_failure_on_python


class TestMsg(TxCase):
    def setUp(self) -> None:
        super().setUp()
        self.records.llm(id="t1", name="bob")
        self.tmux.new_session("t1", "cat", tx_id="t1")
        self.records.other(id="s1", name="alice")
        self.tmux.new_session("s1", "sleep 1000", tx_id="s1")
        self.records.other(id="v1", name="viewer", role="nvim")

    def wait_for_line(self, line: str, count: int = 1) -> str:
        """Block until the target pane shows `line` at least `count` times; returns the capture."""
        def shown():
            capture = self.tmux.capture("t1")
            return capture if capture.count(line) >= count else None

        return self.wait_until(shown)

    def assert_delivered(self, envelope: str) -> None:
        """The envelope is typed on the input line and echoed by `cat` once Enter lands — two
        identical lines in the pane."""
        capture = self.wait_for_line(envelope, 2)
        self.assertEqual([line for line in capture.splitlines() if line.strip()], [envelope, envelope])

    # ----- T-MSG-01 -----------------------------------------------------------------------

    def test_t_msg_01_envelope_exact_strings(self):
        result = self.tx_inside("s1", ["send-message", "bob", 'hi <b> & "q"'])
        self.assertEqual((result.code, result.out, result.err), (0, "", ""))
        self.wait_for_line('<from-agent session="alice">hi <b> & "q"</from-agent>')

        result = self.tx(["send-user-message", "bob", "q?"], env={"TX_SESSION_ID": "v1"})
        self.assertEqual((result.code, result.out, result.err), (0, "", ""))
        capture = self.wait_for_line('<from-user session="viewer">q?</from-user>')
        self.assertIn('<from-agent session="alice">hi <b> & "q"</from-agent>', capture)

    # ----- T-MSG-02 -----------------------------------------------------------------------

    def test_t_msg_02_send_message_delivery(self):
        result = self.tx_inside("s1", ["send-message", "bob", "ping"])
        self.assertEqual((result.code, result.out, result.err), (0, "", ""))
        self.assert_delivered('<from-agent session="alice">ping</from-agent>')
        tail = self.log_tail()[0]
        self.assertEqual((tail["type"], tail["msg"]), ("send-message", "→ bob"))
        self.assertEqual(len(self.log_lines()), 1)

    @expected_failure_on_python
    def test_t_msg_02_fixed_literal_key_name_body(self):
        result = self.tx_inside("s1", ["send-message", "bob", "Enter"])
        self.assertEqual((result.code, result.out, result.err), (0, "", ""))
        self.assert_delivered('<from-agent session="alice">Enter</from-agent>')

    def test_t_msg_02_edge_untracked_sender_uses_raw_session_name(self):
        self.tmux.new_session("Views", "sleep 1000")
        result = self.tx_inside("Views", ["send-message", "bob", "ping"])
        self.assertEqual((result.code, result.out, result.err), (0, "", ""))
        self.assert_delivered('<from-agent session="Views">ping</from-agent>')
        tail = self.log_tail()[0]
        self.assertEqual((tail["type"], tail["msg"]), ("send-message", "→ bob"))

    # ----- T-MSG-03 -----------------------------------------------------------------------

    def assert_nothing_sent(self, before: str) -> None:
        self.assertEqual(self.tmux.capture("t1"), before)
        self.assertEqual(self.log_lines(), [])

    def test_t_msg_03_errors(self):
        before = self.tmux.capture("t1")

        result = self.tx(["send-message", "bob", "ping"])
        self.assertEqual(
            (result.code, result.out, result.err),
            (1, "", "tx send-message: send-message must run inside tmux (needs the sender session name)\n"),
        )
        self.assert_nothing_sent(before)

        result = self.tx_inside("s1", ["send-message", "nobody", "ping"])
        self.assertEqual(
            (result.code, result.out, result.err),
            (1, "", "tx send-message: target session 'nobody' does not exist\n"),
        )
        self.assert_nothing_sent(before)

        self.tmux.kill_session("t1")
        self.assertFalse(self.tmux.has_session("t1"))
        result = self.tx_inside("s1", ["send-message", "bob", "ping"])
        self.assertEqual(
            (result.code, result.out, result.err),
            (1, "", "tx send-message: target session 'bob' does not exist\n"),
        )
        self.assertEqual(self.log_lines(), [])

    def test_t_msg_03_edge_missing_body(self):
        before = self.tmux.capture("t1")
        result = self.tx_inside("s1", ["send-message", "bob"])
        self.assertEqual((result.code, result.out), (2, ""))
        self.assertTrue(result.err.startswith("usage: tx send-message"), result.err)
        self.assertTrue(result.err.endswith("tx send-message: error: the following arguments are required: body\n"), result.err)
        self.assert_nothing_sent(before)

    # ----- T-MSG-04 -----------------------------------------------------------------------

    def test_t_msg_04_send_user_message(self):
        result = self.tx(["send-user-message", "bob", "what is this?"], env={"TX_SESSION_ID": "v1"})
        self.assertEqual((result.code, result.out, result.err), (0, "", ""))
        self.assert_delivered('<from-user session="viewer">what is this?</from-user>')
        tail = self.log_tail()[0]
        self.assertEqual((tail["type"], tail["msg"], tail["actor"]), ("send-user-message", "→ bob", "v1"))
        self.assertEqual(len(self.log_lines()), 1)

    def test_t_msg_04_edge_no_session_id(self):
        before = self.tmux.capture("t1")
        for env in ({}, {"TX_SESSION_ID": ""}):
            result = self.tx(["send-user-message", "bob", "q"], env=env)
            self.assertEqual(
                (result.code, result.out, result.err),
                (1, "", "tx send-user-message: send-user-message must run inside a tx session ($TX_SESSION_ID is unset)\n"),
            )
        self.assertEqual(self.tmux.capture("t1"), before)
        self.assertEqual(self.log_lines(), [])

    def test_t_msg_04_edge_unknown_session_id_is_used_raw(self):
        result = self.tx(["send-user-message", "bob", "q"], env={"TX_SESSION_ID": "deadbeef"})
        self.assertEqual((result.code, result.out, result.err), (0, "", ""))
        self.assert_delivered('<from-user session="deadbeef">q</from-user>')
