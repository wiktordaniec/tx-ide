"""EVENTS — `$TX_IDE_HOME/log.jsonl` (spec section 01, T-EVENTS-01..06).

One compact JSON line per mutation, keys `ts,actor,type,msg`, single `O_APPEND` write capped at
512 bytes. Everything is observed through the verbs that log (`tx rm`, `tx tag`, `tx artifact …`)
and the raw bytes of `log.jsonl`.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import time

import txkit
from txkit import TxCase, expected_failure_on_python

COMPACT = {"separators": (",", ":")}
LINE = re.compile(r'^\{"ts":(?P<ts>-?\d+\.\d+(?:e[-+]?\d+)?),"actor":"(?P<actor>[^"]*)","type":"(?P<type>[^"]*)","msg":"(?P<msg>.*)"\}\n$')

# T-EVENTS-06: every `type` value the CLI/hook surface may append.
CATALOGUE = {
    "spawn", "spawn-view", "state", "tag", "group", "rename", "kill", "rm", "archive", "reconcile",
    "fork", "rollover", "rollover-finish", "handover", "handover-finish", "send-message",
    "send-user-message", "capture-skip", "selfcheck", "artifact-create", "artifact-modify",
    "artifact-group", "artifact-read", "artifact-open", "bind-artifact", "artifact-diff",
}


def current_umask() -> int:
    value = os.umask(0)
    os.umask(value)
    return value


class TestEvents(TxCase):
    def raw_lines(self) -> list[bytes]:
        return self.home.log_path.read_bytes().split(b"\n")[:-1]

    def rm_line(self, record_id: str, env: dict | None = None) -> str:
        """`tx rm <id>` on a fresh log; returns the single appended line (text)."""
        result = self.tx(["rm", record_id], env=env)
        self.assertEqual(result.code, 0, result.err)
        raw = self.home.log_path.read_bytes()
        self.assertEqual(raw.count(b"\n"), 1)
        self.assertTrue(raw.endswith(b"\n"))
        return raw.decode("utf-8")

    # ----- T-EVENTS-01 -------------------------------------------------------------------------

    def test_t_events_01_line_shape_and_key_order(self):
        self.assertFalse(self.home.log_path.exists())
        self.records.llm(id="r1", name="worker-1", state="exited", ended_at=1.0)
        before = time.time()
        line = self.rm_line("r1", env={"TX_SESSION_ID": "sess-1"})
        after = time.time()
        match = LINE.match(line)
        self.assertIsNotNone(match, line)
        self.assertEqual(match.group("actor"), "sess-1")
        self.assertEqual(match.group("type"), "rm")
        self.assertEqual(match.group("msg"), "worker-1 (r1)")
        record = json.loads(line)
        self.assertEqual(list(record), ["ts", "actor", "type", "msg"])
        self.assertIsInstance(record["ts"], float)
        self.assertTrue(before - 5 <= record["ts"] <= after + 5, record["ts"])
        # compact separators, newline-terminated: re-encoding the parsed record reproduces the line
        self.assertEqual(json.dumps(record, **COMPACT) + "\n", line)
        # created by tx itself (log absent before): mode 0o644 & ~umask
        self.assertEqual(
            stat.S_IMODE(self.home.log_path.stat().st_mode), 0o644 & ~current_umask()
        )
        self.assert_golden("events/01", json.dumps({**record, "ts": 0.0}, **COMPACT) + "\n")

    def test_t_events_01_non_ascii_msg_is_escaped(self):
        self.records.llm(id="r2", name="wörker→", state="exited", ended_at=1.0)
        line = self.rm_line("r2")
        self.assertIn('"msg":"w\\u00f6rker\\u2192 (r2)"', line)
        self.assertTrue(line.encode("utf-8").isascii())
        self.assertEqual(json.loads(line)["msg"], "wörker→ (r2)")

    # ----- T-EVENTS-02 -------------------------------------------------------------------------

    def test_t_events_02_actor_resolution(self):
        self.records.llm(id="r1", name="worker-1", state="exited", ended_at=1.0)
        plan = self.root / "plan.md"
        plan.write_text("plan\n")
        # (a) TX_SESSION_ID unset (the kit scrubs it) → ""
        result = self.tx(["rm", "r1"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self.log_tail()[0]["actor"], "")
        self.assertEqual(self.log_tail()[0]["type"], "rm")
        # (b) explicit actor wins over the env-derived empty default
        result = self.tx(["artifact", "create", str(plan), "--title", "P"])
        self.assertEqual(result.code, 0, result.err)
        line = self.log_tail()[0]
        self.assertEqual(line["type"], "artifact-create")
        self.assertEqual(line["actor"], "user")
        # (c) TX_SESSION_ID set
        result = self.tx(
            ["artifact", "create", str(plan), "--title", "P"], env={"TX_SESSION_ID": "sess-1"}
        )
        self.assertEqual(result.code, 0, result.err)
        line = self.log_tail()[0]
        self.assertEqual(line["type"], "artifact-create")
        self.assertEqual(line["actor"], "sess-1")
        self.assertEqual(len(self.log_lines()), 3)

    # ----- T-EVENTS-03 -------------------------------------------------------------------------

    def test_t_events_03_single_o_append_write_and_file_mode(self):
        self.records.llm(id="r0", name="seedling", state="exited", ended_at=1.0)
        self.records.llm(id="r1", name="r1", state="exited", ended_at=1.0)
        # Let tx create log.jsonl (so the mode is tx's), then replace its content with the seed.
        self.assertEqual(self.tx(["rm", "r0"]).code, 0)
        seed = b'{"ts":1.0,"actor":"","type":"pre","msg":"existing"}\n'
        self.home.log_path.write_bytes(seed)
        tag = "T" * 400
        env = txkit.scrubbed_env(self.home, self.tmux, self.fakes)
        script = (
            f'for j in $(seq 1 25); do "{txkit.TX_BIN}" tag r1 "{tag}$1$j" >/dev/null || exit 1; done'
        )
        shells = [
            subprocess.Popen(["bash", "-c", script, "bash", str(index)], env=env, cwd=self.root)
            for index in range(8)
        ]
        for shell in shells:
            self.assertEqual(shell.wait(timeout=600), 0)
        lines = self.raw_lines()
        self.assertEqual(len(lines), 201)
        self.assertEqual(lines[0] + b"\n", seed)
        seen = set()
        for line in lines[1:]:
            self.assertLessEqual(len(line) + 1, 512)
            record = json.loads(line)
            self.assertEqual(record["type"], "tag")
            self.assertTrue(record["msg"].startswith(f"r1 {tag}"), record["msg"])
            seen.add(record["msg"])
        self.assertEqual(len(seen), 200)  # every write landed whole, none interleaved or lost
        self.assertEqual(
            stat.S_IMODE(self.home.log_path.stat().st_mode), 0o644 & ~current_umask()
        )

    # ----- T-EVENTS-04 -------------------------------------------------------------------------

    def test_t_events_04_512_byte_truncation_ascii(self):
        self.records.llm(id="r2", name="y" * 1000, state="exited", ended_at=1.0)
        line = self.rm_line("r2", env={"TX_SESSION_ID": "sess-1"})
        self.assertEqual(len(line.encode("utf-8")), 512)
        match = LINE.match(line)
        self.assertIsNotNone(match, line)
        self.assertEqual(match.group("actor"), "sess-1")
        self.assertEqual(match.group("type"), "rm")
        skeleton = f'{{"ts":{match.group("ts")},"actor":"sess-1","type":"rm","msg":""}}\n'
        budget = 512 - len(skeleton.encode("utf-8"))
        full = "y" * 1000 + " (r2)"
        self.assertEqual(match.group("msg"), full[:budget])
        self.assertEqual(json.loads(line)["msg"], "y" * budget)
        # golden: ts → 0.0, msg re-truncated to that skeleton's budget
        golden_skeleton = json.dumps(
            {"ts": 0.0, "actor": "sess-1", "type": "rm", "msg": ""}, **COMPACT
        ) + "\n"
        golden_budget = 512 - len(golden_skeleton.encode("utf-8"))
        golden = json.dumps(
            {"ts": 0.0, "actor": "sess-1", "type": "rm", "msg": full[:golden_budget]}, **COMPACT
        ) + "\n"
        self.assertEqual(len(golden.encode("utf-8")), 512)
        self.assert_golden("events/04", golden)

    # Bytes of the line around `ts` and `msg`: `{"ts":` … `,"actor":"sess-1","type":"rm","msg":"` … `"}\n`.
    BOUNDARY_OVERHEAD = len(
        json.dumps({"ts": 0, "actor": "sess-1", "type": "rm", "msg": ""}, **COMPACT)
    ) - len("0") + len("\n")

    def _boundary_attempt(self, record_id: str, natural_at_18: int) -> int:
        """One `tx rm` of a record whose line would be `natural_at_18` bytes with an 18-char `ts`.
        `ts` is `repr(time.time())` — 18 chars for about three runs in four, 17 otherwise — so the
        line's natural length is only known after the fact. Asserts the `<=` rule for the natural
        length actually produced and returns it."""
        tail = f" ({record_id})"
        name = "y" * (natural_at_18 - self.BOUNDARY_OVERHEAD - 18 - len(tail))
        self.records.llm(id=record_id, name=name, state="exited", ended_at=1.0)
        self.home.log_path.unlink(missing_ok=True)
        line = self.rm_line(record_id, env={"TX_SESSION_ID": "sess-1"})
        match = LINE.match(line)
        self.assertIsNotNone(match, line)
        natural = self.BOUNDARY_OVERHEAD + len(match.group("ts")) + len(name) + len(tail)
        full = name + tail
        if natural <= 512:
            self.assertEqual(len(line.encode("utf-8")), natural)
            self.assertEqual(match.group("msg"), full)
        else:
            self.assertEqual(len(line.encode("utf-8")), 512)
            self.assertEqual(match.group("msg"), full[: 512 - natural])
        return natural

    def test_t_events_04_exact_512_bytes_is_not_truncated(self):
        # The `<=` boundary: a line landing exactly on 512 bytes is intact; one byte over loses
        # exactly one char of `msg`. Both legs must be OBSERVED, not merely tolerated: records are
        # sized for an 18-char `ts` and re-tried with fresh ids (bounded) until a run has produced
        # a 512-byte line and another a 513-byte one.
        observed: set[int] = set()
        for attempt in range(40):
            observed.add(self._boundary_attempt(f"e{attempt}", 512))
            observed.add(self._boundary_attempt(f"o{attempt}", 513))
            if {512, 513} <= observed:
                break
        self.assertLessEqual({512, 513}, observed, f"never observed both legs: {sorted(observed)}")

    # ----- T-EVENTS-05 -------------------------------------------------------------------------

    @expected_failure_on_python
    def test_t_events_05_truncation_with_non_ascii_msg(self):
        name = "é" * 600
        self.records.llm(id="r3", name=name, state="exited", ended_at=1.0)
        line = self.rm_line("r3", env={"TX_SESSION_ID": "me"})
        self.assertLessEqual(len(line.encode("utf-8")), 512)
        record = json.loads(line)
        self.assertEqual(list(record), ["ts", "actor", "type", "msg"])
        self.assertEqual(record["actor"], "me")
        self.assertEqual(record["type"], "rm")
        full = f"{name} (r3)"
        self.assertTrue(full.startswith(record["msg"]), record["msg"])

    # ----- T-EVENTS-06 -------------------------------------------------------------------------

    def test_t_events_06_event_type_catalogue(self):
        # Verbs the CLI/hook surface reaches from a plain home run here. `fork`, `rollover` /
        # `rollover-finish`, `handover` / `handover-finish`, `send-message` and `capture-skip` are
        # DELEGATED (spec rev 5) to test_chat.py (T-CHAT-02/08/11/12/16), test_msg.py (T-MSG-01/02)
        # and test_hook.py (T-HOOK-19), which pin each type/msg pair where the fixture exists. The
        # "no other type ever appears" clause is the subset check at the end.
        cwd = str(self.root)
        seen_types: set[str] = set()

        def run(argv: list[str], *, env: dict | None = None, stdin: str | None = None, code: int = 0):
            before = len(self.log_lines())
            result = self.tx(argv, env=env, stdin=stdin)
            self.assertEqual(result.code, code, f"{argv}: {result.err}")
            appended = self.log_lines()[before:]
            seen_types.update(line["type"] for line in appended)
            return result, appended

        def one(argv, expected_type, expected_msg=None, **kwargs):
            result, appended = run(argv, **kwargs)
            self.assertEqual(len(appended), 1, f"{argv} appended {appended}")
            self.assertEqual(appended[0]["type"], expected_type)
            if expected_msg is not None:
                self.assertEqual(appended[0]["msg"], expected_msg)
            return result, appended[0]

        # spawn family
        one(["spawn", "sh1", "--tag", "t", "--cwd", cwd, "--cmd", "bash"], "spawn", f"sh1 [shell] {cwd}")
        one(["spawn-nvim", "e1", "--tag", "t", "--cwd", cwd], "spawn", f"e1 [nvim] {cwd}")
        one(["spawn-view", "v1", "--cwd", cwd, "--cmd", "sleep 1000"], "spawn-view", f"v1 {cwd}")
        # hook → state
        llm_id = self.records.llm(name="w1", state="idle")
        self.tmux.new_session(llm_id, "sleep 1000", tx_id=llm_id)  # live, so later reconciles skip it
        one(
            ["hook", "prompt-submit"], "state", "w1 → working",
            env={"TX_SESSION_ID": llm_id},
            stdin=json.dumps({"session_id": "c1", "transcript_path": "/t/c1.jsonl"}),
        )
        # tag / group / rename
        one(["tag", "sh1", "a,b"], "tag", "sh1 a,b")
        one(["group", "sh1", "g1"], "group", "sh1 g1")
        one(["group", "sh1", "--clear"], "group", "sh1 (cleared)")
        one(["rename", "sh1", "sh2"], "rename", "sh1 → sh2")
        _, appended = run(["rename", "sh2", "sh2"])
        self.assertEqual(appended, [])  # same name: early return, nothing logged
        # kill (record) / kill (view)
        one(["kill", "sh2"], "kill", "sh2")
        one(["kill", "v1"], "kill", "v1")
        # rm + unknown edge
        one(["rm", "w1"], "rm", f"w1 ({llm_id})")
        _, appended = run(["rm", "nosuch"], code=1)
        self.assertEqual(appended, [])
        # archive
        one(["archive", "e1"], "archive", "e1")
        # reconcile-on-read: vanished + stuck, one `tx ls`
        now = time.time()
        self.records.llm(id="van", name="van", state="idle")
        stuck = self.records.llm(
            id="stk", name="stk", state="working", turn_started_at=now - 1200, last_activity=now - 1200
        )
        self.tmux.new_session(stuck, "sleep 1000", tx_id=stuck)
        _, appended = run(["ls"])
        self.assertEqual(
            [(line["type"], line["msg"]) for line in appended],
            [("reconcile", "stk working → idle (stuck)"), ("reconcile", "van → exited (vanished)")],
        )
        # selfcheck
        _, line = one(["selfcheck"], "selfcheck")
        self.assertTrue(line["msg"].startswith("created s1a-selfcheck ("), line["msg"])
        # artifacts
        plan = self.root / "plan.md"
        plan.write_text("v0\n")
        result, line = one(["artifact", "create", str(plan), "--title", "P"], "artifact-create")
        artifact_id = re.match(r"Created artifact (\S+) \(plan\.md\)\n$", result.out).group(1)
        self.assertEqual(line["msg"], f"{artifact_id} plan.md")
        plan2 = self.root / "plan2.md"
        plan2.write_text("v1\n")
        one(["artifact", "modify", artifact_id, str(plan2), "--changes", "x"], "artifact-modify", f"{artifact_id} rev1")
        one(["artifact", "group", artifact_id, "g1"], "artifact-group", f"{artifact_id} g1")
        one(["artifact", "diff", artifact_id], "artifact-diff", f"{artifact_id} rev0..rev1")
        _, appended = run(["artifact", "show", artifact_id])
        self.assertEqual(appended, [])  # `show` is a pure render — no artifact-read line (see NOTES)
        result, appended = run(
            ["artifact", "open", artifact_id, "--tag", "t"], env={"TX_SESSION_ID": "sess-x"}
        )
        view = json.loads(self.tx(["show", f"art-{artifact_id[:8]}"]).out)
        self.assertEqual(
            [(line["type"], line["msg"], line["actor"]) for line in appended],
            [
                ("spawn", f"art-{artifact_id[:8]} [nvim] {self.home.artifacts_dir / artifact_id}", "sess-x"),
                ("bind-artifact", f"art-{artifact_id[:8]} → {artifact_id}", "sess-x"),
                ("artifact-open", f"{artifact_id} → sess-x", "sess-x"),
            ],
        )
        self.assertEqual(view["artifact_id"], artifact_id)
        # `open` from a plain terminal (no TX_SESSION_ID, not inside tmux): only `artifact-open`
        # resolves the invoker chain down to `user`; `spawn` and `bind-artifact` take the default
        # actor, `$TX_SESSION_ID` verbatim, i.e. "" (spec rev 5). A second artifact, since the view
        # name `art-<id8>` of the first is now taken.
        plan3 = self.root / "plan3.md"
        plan3.write_text("v0\n")
        result, _ = one(["artifact", "create", str(plan3), "--title", "P3"], "artifact-create")
        second_id = re.match(r"Created artifact (\S+) \(plan3\.md\)\n$", result.out).group(1)
        _, appended = run(["artifact", "open", second_id, "--tag", "t"])
        self.assertEqual(
            [(line["type"], line["msg"], line["actor"]) for line in appended],
            [
                ("spawn", f"art-{second_id[:8]} [nvim] {self.home.artifacts_dir / second_id}", ""),
                ("bind-artifact", f"art-{second_id[:8]} → {second_id}", ""),
                ("artifact-open", f"{second_id} → user", "user"),
            ],
        )
        # send-user-message: sender = $TX_SESSION_ID's record, target must be live
        one(["spawn", "tgt", "--tag", "t", "--cwd", cwd, "--cmd", "bash"], "spawn", f"tgt [shell] {cwd}")
        one(["send-user-message", "tgt", "hello"], "send-user-message", "→ tgt", env={"TX_SESSION_ID": stuck})
        # name clash: refused before the mutation, nothing logged
        _, appended = run(["spawn", "tgt", "--tag", "t", "--cwd", cwd, "--cmd", "bash"], code=1)
        self.assertEqual(appended, [])

        self.assertLessEqual(seen_types, CATALOGUE)
        self.assertLessEqual({line["type"] for line in self.log_lines()}, CATALOGUE)
        self.assertEqual(
            seen_types,
            {
                "spawn", "spawn-view", "state", "tag", "group", "rename", "kill", "rm", "archive",
                "reconcile", "selfcheck", "artifact-create", "artifact-modify", "artifact-group",
                "artifact-diff", "bind-artifact", "artifact-open", "send-user-message",
            },
        )
