"""HOOK — `tx hook <event>` dispatch (spec section 03, T-HOOK-01 … T-HOOK-21).

Every case drives a crafted record through `tx hook <event>` with `TX_SESSION_ID` in the env and a
JSON payload on stdin, then reads the record file, `log.jsonl`, and `history/` back. The agy leg of
T-HOOK-15 is not written (D3).
"""

from __future__ import annotations

import filecmp
import json
import os
import time
import uuid

from txkit import TxCase

PAYLOAD_C1 = json.dumps({"session_id": "c1", "transcript_path": "/p/c1.jsonl"})
THREE_LINES = '{"type":"user","n":1}\n{"type":"assistant","n":2}\n{"type":"user","n":3}\n'
# A far-past mtime for "no save happened across a >1 s gap" checks (backdated, never slept).
OLD = 1000.0
YIELD_SUBTYPES = ("idle_prompt", "permission_prompt", "elicitation_dialog")


class TestHook(TxCase):
    # ----- helpers ---------------------------------------------------------------------------

    def hook(self, event: str, session_id: str | None = None, stdin: str | None = None, *extra: str):
        env = {"TX_SESSION_ID": session_id} if session_id is not None else None
        return self.tx(["hook", event, *extra], env=env, stdin=stdin)

    def workdir(self) -> str:
        directory = self.root / "w"
        directory.mkdir(exist_ok=True)
        return str(directory)

    def write_transcript(self, cwd: str, chat_id: str, text: str = THREE_LINES):
        path = self.home.claude_transcript_path(cwd, chat_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def captured_record(self, state: str, name: str, chat_id: str = "c1", **fields) -> str:
        """An llm record in `state` whose one chat `chat_id` is captured and has a transcript on disk."""
        cwd = self.workdir()
        session_id = str(uuid.uuid4())
        transcript = self.write_transcript(cwd, chat_id)
        self.records.llm(
            id=session_id,
            name=name,
            state=state,
            cwd=cwd,
            chats=[
                self.records.chat_ref(
                    session_id=session_id, id=chat_id, cwd=cwd, transcript_path=str(transcript)
                )
            ],
            **fields,
        )
        return session_id

    def bundle_transcript(self, session_id: str, chat_id: str = "c1"):
        return self.home.history_dir / session_id / chat_id / "transcript.jsonl"

    def backdate(self, path) -> int:
        os.utime(path, (OLD, OLD))
        return path.stat().st_mtime_ns

    def holds_for(self, predicate, seconds: float) -> bool:
        """Whether `predicate` stays truthy for `seconds` (the bounded negative wait)."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not predicate():
                return False
            time.sleep(0.05)
        return bool(predicate())

    def store_snapshot(self) -> dict[str, bytes]:
        return {path.name: path.read_bytes() for path in self.home.sessions_dir.iterdir()}

    def state_lines(self, name: str) -> list[dict]:
        return [
            line
            for line in self.log_lines()
            if line["type"] == "state" and line["msg"].startswith(f"{name} → ")
        ]

    def assert_ok_silent(self, result) -> None:
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, "")

    # ----- T-HOOK-01 … 04: the no-op paths ---------------------------------------------------

    def test_t_hook_01_exit_0_on_every_path(self):
        session_id = self.records.llm(name="r1", state="idle")
        before = self.store_snapshot()
        results = [
            self.tx(["hook", "prompt-submit"], stdin=PAYLOAD_C1),  # no TX_SESSION_ID
            self.hook("prompt-submit", "no-such-id", PAYLOAD_C1),  # unknown id
            self.tx(["hook"]),  # no event, no id
            self.tx(["hook"], env={"TX_SESSION_ID": session_id}),  # no event, our id
            self.hook("bogus", session_id, PAYLOAD_C1),  # unknown event
            self.hook("bogus", session_id, "garbage"),  # unknown event + garbage stdin
            self.tx(["hook", "prompt-submit"], stdin="garbage"),  # garbage stdin, id-less
            self.hook("stop", "no-such-id", "garbage"),  # garbage stdin, unknown id
        ]
        for result in results:
            self.assert_ok_silent(result)
            self.assertEqual(result.err, "")
        self.assertEqual(self.store_snapshot(), before)
        self.assertFalse(self.home.log_path.exists())

    def test_t_hook_02_no_tx_session_id_is_a_no_op(self):
        session_id = self.records.llm(name="r2", state="idle")
        before = self.records.path(session_id).read_bytes()
        result = self.tx(
            ["hook", "prompt-submit"],
            stdin=json.dumps({"session_id": "c1", "transcript_path": "/t"}),
        )
        self.assert_ok_silent(result)
        record = self.records.load(session_id)
        self.assertEqual(record["state"], "idle")
        self.assertIsNone(record["chats"][0]["id"])
        self.assertEqual(self.records.path(session_id).read_bytes(), before)
        self.assertFalse(self.home.log_path.exists())

    def test_t_hook_03_non_llm_record_is_a_no_op(self):
        session_id = self.records.other(name="sh", role="shell", state="alive")
        before = self.records.path(session_id).read_bytes()
        result = self.hook("prompt-submit", session_id, PAYLOAD_C1)
        self.assert_ok_silent(result)
        self.assertEqual(self.records.load(session_id)["state"], "alive")
        self.assertEqual(self.records.path(session_id).read_bytes(), before)
        self.assertFalse(self.home.log_path.exists())

    def test_t_hook_04_terminal_record_absorbs(self):
        for state in ("exited", "archived"):
            with self.subTest(state=state):
                session_id = self.captured_record(state, name=f"t-{state}", ended_at=OLD + 5)
                path = self.records.path(session_id)
                before_bytes = path.read_bytes()
                before_mtime = path.stat().st_mtime_ns
                self.assert_ok_silent(self.hook("prompt-submit", session_id, "{}"))
                self.assert_ok_silent(self.hook("stop", session_id))
                self.assertEqual(path.read_bytes(), before_bytes)
                self.assertEqual(path.stat().st_mtime_ns, before_mtime)
                record = self.records.load(session_id)
                self.assertEqual(record["state"], state)
                self.assertEqual(record["ended_at"], OLD + 5)
                self.assertEqual(self.state_lines(f"t-{state}"), [])
                history = self.home.history_dir / session_id
                self.assertTrue(self.holds_for(lambda: not history.exists(), 2.0))

    # ----- T-HOOK-05 … 09: the state arms --------------------------------------------------

    def test_t_hook_05_prompt_submit_arms_turn_clock(self):
        session_id = self.records.llm(
            name="r5", state="idle", created_at=OLD, last_activity=OLD, turn_started_at=OLD
        )
        t0 = time.time()
        result = self.hook("prompt-submit", session_id, "{}")
        t1 = time.time()
        self.assert_ok_silent(result)
        record = self.records.load(session_id)
        self.assertEqual(record["state"], "working")
        self.assertEqual(record["turn_started_at"], record["last_activity"])
        self.assertTrue(t0 <= record["turn_started_at"] <= t1)
        self.assertEqual(record["attached_to"], [])
        tail = self.log_tail()[0]
        self.assertEqual(tail["type"], "state")
        self.assertEqual(tail["msg"], "r5 → working")
        self.assertEqual(tail["actor"], session_id)
        history = self.home.history_dir / session_id
        self.assertTrue(self.holds_for(lambda: not history.exists(), 2.0))

    def test_t_hook_06_working_family(self):
        session_id = self.records.llm(name="r6", state="idle")
        self.assert_ok_silent(self.hook("working", session_id))
        self.assertEqual(self.records.load(session_id)["state"], "working")
        self.assertEqual(self.log_tail()[0]["msg"], "r6 → working")
        # Edge: already WORKING → no save (mtime unchanged over a >1 s gap), no new log line.
        path = self.records.path(session_id)
        mtime = self.backdate(path)
        line_count = len(self.log_lines())
        self.assert_ok_silent(self.hook("working", session_id))
        self.assertEqual(path.stat().st_mtime_ns, mtime)
        self.assertEqual(len(self.log_lines()), line_count)
        self.assertEqual(self.records.load(session_id)["state"], "working")

    def test_t_hook_07_stop_waiting_and_detached_ingest(self):
        session_id = self.captured_record("working", name="r7")
        source = self.home.claude_transcript_path(self.workdir(), "c1")
        result = self.hook("stop", session_id)
        self.assert_ok_silent(result)
        self.assertEqual(self.records.load(session_id)["state"], "waiting")
        tail = self.log_tail()[0]
        self.assertEqual((tail["type"], tail["msg"]), ("state", "r7 → waiting"))
        bundle = self.bundle_transcript(session_id)
        self.wait_until(lambda: bundle.exists() and bundle.read_bytes() == source.read_bytes(), timeout=5)
        self.wait_until(
            lambda: self.records.load(session_id)["chats"][0]["bundle_path"] == str(bundle.parent),
            timeout=5,
        )

    def test_t_hook_07_stop_while_waiting_no_ingest_while_idle_real(self):
        # Edge: `stop` while already WAITING → no `state` line, no ingest.
        waiting = self.captured_record("waiting", name="r7w")
        self.assert_ok_silent(self.hook("stop", waiting))
        self.assertEqual(self.state_lines("r7w"), [])
        self.assertEqual(self.records.load(waiting)["state"], "waiting")
        history = self.home.history_dir / waiting
        self.assertTrue(self.holds_for(lambda: not history.exists(), 2.0))
        # Edge: `stop` while IDLE → WAITING + bundle appears (a real transition).
        idle = self.captured_record("idle", name="r7i", chat_id="c2")
        self.assert_ok_silent(self.hook("stop", idle))
        self.assertEqual(self.records.load(idle)["state"], "waiting")
        self.assertEqual(len(self.state_lines("r7i")), 1)
        bundle = self.bundle_transcript(idle, "c2")
        self.wait_until(bundle.exists, timeout=5)
        self.assertEqual(bundle.read_text(), THREE_LINES)

    def test_t_hook_07_hook_returns_before_large_copy(self):
        # Edge: hook latency — a 50 MB transcript, `tx hook stop` returns in < 1 s, the copy lands later.
        session_id = self.captured_record("working", name="r7big")
        source = self.home.claude_transcript_path(self.workdir(), "c1")
        line = json.dumps({"type": "user", "text": "x" * 1000}) + "\n"
        repeat = 50 * 1024 * 1024 // len(line) + 1
        with source.open("w") as handle:
            handle.write(line * repeat)
        started = time.monotonic()
        result = self.hook("stop", session_id)
        elapsed = time.monotonic() - started
        self.assert_ok_silent(result)
        self.assertLess(elapsed, 1.0)
        bundle = self.bundle_transcript(session_id)
        self.wait_until(
            lambda: bundle.exists() and bundle.stat().st_size == source.stat().st_size, timeout=30
        )
        self.assertTrue(filecmp.cmp(source, bundle, shallow=False))

    def test_t_hook_08_session_end_idle_and_ingest(self):
        session_id = self.captured_record("waiting", name="r8")
        self.assertFalse((self.home.history_dir / session_id).exists())
        self.assert_ok_silent(self.hook("session-end", session_id))
        record = self.records.load(session_id)
        self.assertEqual(record["state"], "idle")
        self.assertIsNone(record["ended_at"])
        tail = self.log_tail()[0]
        self.assertEqual((tail["type"], tail["msg"]), ("state", "r8 → idle"))
        bundle = self.bundle_transcript(session_id)
        self.wait_until(bundle.exists, timeout=5)
        self.assertEqual(bundle.read_text(), THREE_LINES)
        # Edge: already IDLE → no `state` line, record mtime unchanged, no ingest.
        idle = self.captured_record("idle", name="r8i", chat_id="c2")
        path = self.records.path(idle)
        mtime = self.backdate(path)
        self.assert_ok_silent(self.hook("session-end", idle))
        self.assertEqual(self.state_lines("r8i"), [])
        self.assertEqual(path.stat().st_mtime_ns, mtime)
        history = self.home.history_dir / idle
        self.assertTrue(self.holds_for(lambda: not history.exists(), 2.0))

    def test_t_hook_09_notification_subtype_filter(self):
        for subtype in YIELD_SUBTYPES:
            with self.subTest(subtype=subtype):
                name = f"n-{subtype}"
                session_id = self.captured_record("working", name=name, chat_id=f"c-{subtype}")
                result = self.hook(
                    "notification", session_id, json.dumps({"notification_type": subtype})
                )
                self.assert_ok_silent(result)
                self.assertEqual(self.records.load(session_id)["state"], "waiting")
                self.assertEqual(len(self.state_lines(name)), 1)
                bundle = self.bundle_transcript(session_id, f"c-{subtype}")
                self.wait_until(bundle.exists, timeout=5)

    def test_t_hook_09_non_yield_and_malformed_payloads_ignored(self):
        payloads = {
            "auth_success": json.dumps({"notification_type": "auth_success"}),
            "elicitation_complete": json.dumps({"notification_type": "elicitation_complete"}),
            "missing-key": "{}",
            "empty": "",
            "not-json": "not json",
        }
        for label, payload in payloads.items():
            with self.subTest(payload=label):
                name = f"x-{label}"
                session_id = self.captured_record("working", name=name, chat_id=f"c-{label}")
                path = self.records.path(session_id)
                mtime = self.backdate(path)
                self.assert_ok_silent(self.hook("notification", session_id, payload))
                self.assertEqual(self.records.load(session_id)["state"], "working")
                self.assertEqual(self.state_lines(name), [])
                self.assertEqual(path.stat().st_mtime_ns, mtime)

    def test_t_hook_09_refired_idle_prompt_no_second_ingest(self):
        session_id = self.captured_record("working", name="refire")
        payload = json.dumps({"notification_type": "idle_prompt"})
        self.assert_ok_silent(self.hook("notification", session_id, payload))
        bundle = self.bundle_transcript(session_id)
        self.wait_until(bundle.exists, timeout=5)
        self.wait_until(
            lambda: self.records.load(session_id)["chats"][0]["bundle_path"] is not None, timeout=5
        )
        bundle_mtime = bundle.stat().st_mtime_ns
        self.assert_ok_silent(self.hook("notification", session_id, payload))
        self.assertEqual(len(self.state_lines("refire")), 1)
        self.assertEqual(self.records.load(session_id)["state"], "waiting")
        self.assertTrue(self.holds_for(lambda: bundle.stat().st_mtime_ns == bundle_mtime, 2.0))

    # ----- T-HOOK-10 … 12: the id-less arms + argv shape ------------------------------------

    def test_t_hook_10_session_closed_reconciles(self):
        vanished = self.records.llm(name="gone", state="idle")
        survivor = self.records.llm(name="stay", state="idle")
        self.tmux.new_session(vanished, "sleep 1000", tx_id=vanished, client_env=self.tx_env())
        self.tmux.new_session(survivor, "sleep 1000", tx_id=survivor, client_env=self.tx_env())
        self.tmux.kill_session(vanished)
        t0 = time.time()
        result = self.tx(["hook", "session-closed"])
        self.assert_ok_silent(result)
        gone = self.records.load(vanished)
        self.assertEqual(gone["state"], "exited")
        self.assertGreaterEqual(gone["ended_at"], t0)
        tail = self.log_tail()[0]
        self.assertEqual((tail["type"], tail["msg"]), ("reconcile", "gone → exited (vanished)"))
        # Edge: live sessions untouched.
        stay = self.records.load(survivor)
        self.assertEqual(stay["state"], "idle")
        self.assertIsNone(stay["ended_at"])
        self.assertIn(survivor, self.tmux.sessions())

    def test_t_hook_11_ingest_pseudo_event(self):
        session_id = self.captured_record("idle", name="r11")
        result = self.tx(["hook", "ingest", session_id])
        self.assert_ok_silent(result)
        bundle = self.bundle_transcript(session_id)
        self.assertEqual(bundle.read_text(), THREE_LINES)
        self.assertEqual(
            self.records.load(session_id)["chats"][0]["bundle_path"], str(bundle.parent)
        )
        # Edge: no arg + TX_SESSION_ID=<id> → same.
        by_env = self.captured_record("idle", name="r11b", chat_id="c2")
        self.assert_ok_silent(self.hook("ingest", by_env))
        self.assertEqual(self.bundle_transcript(by_env, "c2").read_text(), THREE_LINES)
        self.assertEqual(
            self.records.load(by_env)["chats"][0]["bundle_path"],
            str(self.home.history_dir / by_env / "c2"),
        )
        # Edge: no arg and no env → no-op.
        untouched = self.captured_record("idle", name="r11c", chat_id="c3")
        before = self.records.path(untouched).read_bytes()
        self.assert_ok_silent(self.tx(["hook", "ingest"]))
        self.assertFalse((self.home.history_dir / untouched).exists())
        self.assertEqual(self.records.path(untouched).read_bytes(), before)
        # Edge: unknown id → no-op, exit 0.
        self.assert_ok_silent(self.tx(["hook", "ingest", "nope"]))
        self.assertFalse((self.home.history_dir / "nope").exists())
        self.assertFalse(self.home.log_path.exists())

    def test_t_hook_12_extra_argv_ignored(self):
        plain = self.records.llm(name="plain", state="idle")
        shim = self.records.llm(name="shim", state="idle")
        payload_plain = json.dumps({"session_id": "c1", "transcript_path": "/p/c1.jsonl"})
        payload_shim = json.dumps({"session_id": "c2", "transcript_path": "/p/c2.jsonl"})
        for session_id, payload, extra in (
            (plain, payload_plain, ()),
            (shim, payload_shim, ("--engine", "codex")),
        ):
            result = self.hook("prompt-submit", session_id, payload, *extra)
            self.assert_ok_silent(result)
            self.assertEqual(result.err, "")
        for session_id, chat_id in ((plain, "c1"), (shim, "c2")):
            record = self.records.load(session_id)
            self.assertEqual(record["state"], "working")
            self.assertEqual(record["chats"][0]["id"], chat_id)
            self.assertEqual(record["chats"][0]["transcript_path"], f"/p/{chat_id}.jsonl")
        self.assertEqual(
            [line["msg"] for line in self.log_lines()], ["plain → working", "shim → working"]
        )

    # ----- T-HOOK-13 … 21: chat-id capture --------------------------------------------------

    def test_t_hook_13_session_start_captures_only(self):
        session_id = self.records.llm(name="r13", state="idle")
        path = self.records.path(session_id)
        backdated = self.backdate(path)
        result = self.hook("session-start", session_id, PAYLOAD_C1)
        self.assert_ok_silent(result)
        record = self.records.load(session_id)
        self.assertEqual(record["chats"][0]["id"], "c1")
        self.assertEqual(record["chats"][0]["transcript_path"], "/p/c1.jsonl")
        self.assertGreater(path.stat().st_mtime_ns, backdated)
        self.assertEqual(record["state"], "idle")
        self.assertFalse(self.home.log_path.exists())
        listing = self.tx(["chat", "ls", "r13"])
        self.assertEqual(listing.code, 0, listing.err)
        self.assertEqual(listing.lines[0], "r13 — 1 chat(s)")
        self.assertEqual(listing.lines[1].split()[0], "c1")
        self.assertTrue(listing.lines[1].startswith("  c1        original  spawn"))

    def test_t_hook_14_prompt_submit_captures_and_transitions(self):
        session_id = self.records.llm(name="r14", state="idle")
        result = self.hook("prompt-submit", session_id, PAYLOAD_C1)
        self.assert_ok_silent(result)
        record = self.records.load(session_id)
        self.assertEqual(record["chats"][0]["id"], "c1")
        self.assertEqual(record["chats"][0]["transcript_path"], "/p/c1.jsonl")
        self.assertEqual(record["state"], "working")
        tail = self.log_tail()[0]
        self.assertEqual((tail["type"], tail["msg"]), ("state", "r14 → working"))

    def test_t_hook_15_capture_per_engine_payload_shape(self):
        # Codex leg only; the antigravity leg is deferred (D3).
        session_id = self.records.llm(name="cx", state="idle", engine="codex")
        self.assertEqual(self.records.load(session_id)["chats"][0]["engine"], "codex")
        payload = json.dumps({"session_id": "r1", "transcript_path": "/s/rollout-x-r1.jsonl"})
        result = self.hook("session-start", session_id, payload, "--engine", "codex")
        self.assert_ok_silent(result)
        record = self.records.load(session_id)
        self.assertEqual(record["chats"][0]["id"], "r1")
        self.assertEqual(record["chats"][0]["transcript_path"], "/s/rollout-x-r1.jsonl")
        self.assertEqual(record["state"], "idle")

    def test_t_hook_16_capture_idempotent(self):
        session_id = str(uuid.uuid4())
        self.records.llm(
            id=session_id,
            name="r16",
            state="idle",
            chats=[
                self.records.chat_ref(
                    session_id=session_id, id="c1", transcript_path="/orig/c1.jsonl"
                )
            ],
        )
        path = self.records.path(session_id)
        mtime = self.backdate(path)
        result = self.hook(
            "session-start", session_id, json.dumps({"session_id": "c1", "transcript_path": "/x"})
        )
        self.assert_ok_silent(result)
        self.assertEqual(path.stat().st_mtime_ns, mtime)
        self.assertEqual(self.records.load(session_id)["chats"][0]["transcript_path"], "/orig/c1.jsonl")
        # Edge: no pending ref and a new id → no-op, never creates a ref.
        result = self.hook(
            "session-start", session_id, json.dumps({"session_id": "c2", "transcript_path": "/y"})
        )
        self.assert_ok_silent(result)
        self.assertEqual(len(self.records.load(session_id)["chats"]), 1)
        self.assertEqual(path.stat().st_mtime_ns, mtime)
        self.assertFalse(self.home.log_path.exists())

    def test_t_hook_17_latest_pending_wins(self):
        session_id = str(uuid.uuid4())
        self.records.llm(
            id=session_id,
            name="r17",
            state="idle",
            chats=[
                self.records.chat_ref(session_id=session_id, id="c0", started_at=OLD),
                self.records.chat_ref(
                    session_id=session_id,
                    id=None,
                    role="rollover",
                    how="rollover",
                    chat_id="c0",
                    started_at=OLD,
                ),
            ],
        )
        result = self.hook(
            "session-start",
            session_id,
            json.dumps({"session_id": "c9", "transcript_path": "/p/c9.jsonl"}),
        )
        self.assert_ok_silent(result)
        record = self.records.load(session_id)
        self.assertEqual(record["chats"][1]["id"], "c9")
        self.assertEqual(record["chats"][1]["transcript_path"], "/p/c9.jsonl")
        self.assertEqual(record["chats"][0]["id"], "c0")
        listing = self.tx(["chat", "ls", "r17"])
        self.assertEqual(listing.code, 0, listing.err)
        self.assertTrue(listing.lines[2].startswith("  c9        rollover  rollover←c0"), listing.out)

    def test_t_hook_18_lazy_fork_guard(self):
        source_id = str(uuid.uuid4())
        fork_id = str(uuid.uuid4())
        self.records.llm(
            id=source_id,
            name="src",
            state="idle",
            chats=[self.records.chat_ref(session_id=source_id, id="c0")],
        )
        self.records.llm(
            id=fork_id,
            name="fork",
            state="idle",
            chats=[
                self.records.chat_ref(
                    session_id=source_id, id=None, role="fork", how="fork", chat_id="c0"
                )
            ],
        )
        result = self.hook(
            "session-start", fork_id, json.dumps({"session_id": "c0", "transcript_path": "/p/c0.jsonl"})
        )
        self.assert_ok_silent(result)
        self.assertIsNone(self.records.load(fork_id)["chats"][0]["id"])
        self.assertEqual([line for line in self.log_lines() if line["type"] == "capture-skip"], [])
        result = self.hook(
            "prompt-submit", fork_id, json.dumps({"session_id": "c7", "transcript_path": "/p/c7.jsonl"})
        )
        self.assert_ok_silent(result)
        record = self.records.load(fork_id)
        self.assertEqual(record["chats"][0]["id"], "c7")
        self.assertEqual(record["chats"][0]["transcript_path"], "/p/c7.jsonl")
        self.assertEqual(record["state"], "working")
        self.assertEqual(self.records.load(source_id)["chats"][0]["id"], "c0")

    def test_t_hook_18_handover_ref_accepts_its_origin_chat(self):
        # Edge: role `handover` with origin.chat_id c0 and NO other record owning c0 → filled.
        session_id = str(uuid.uuid4())
        self.records.llm(
            id=session_id,
            name="hand",
            state="idle",
            chats=[
                self.records.chat_ref(
                    session_id=str(uuid.uuid4()), id=None, role="handover", how="handover", chat_id="c0"
                )
            ],
        )
        result = self.hook(
            "session-start", session_id, json.dumps({"session_id": "c0", "transcript_path": "/p/c0.jsonl"})
        )
        self.assert_ok_silent(result)
        record = self.records.load(session_id)
        self.assertEqual(record["chats"][0]["id"], "c0")
        self.assertEqual(record["chats"][0]["transcript_path"], "/p/c0.jsonl")
        self.assertFalse(self.home.log_path.exists())

    def test_t_hook_19_ownership_guard_logs_capture_skip(self):
        owner_id = str(uuid.uuid4())
        self.records.llm(
            id=owner_id,
            name="owner",
            state="idle",
            chats=[self.records.chat_ref(session_id=owner_id, id="c1")],
        )
        pending_id = self.records.llm(name="pend", state="idle")
        path = self.records.path(pending_id)
        mtime = self.backdate(path)
        result = self.hook("session-start", pending_id, PAYLOAD_C1)
        self.assert_ok_silent(result)
        self.assertIsNone(self.records.load(pending_id)["chats"][0]["id"])
        self.assertEqual(path.stat().st_mtime_ns, mtime)
        lines = self.log_lines()
        self.assertEqual(len(lines), 1)
        tail = lines[-1]
        self.assertEqual(tail["type"], "capture-skip")
        self.assertEqual(tail["actor"], pending_id)
        self.assertEqual(tail["msg"], "pend: chat c1 owned by another session — refused cross-bind")

    def test_t_hook_20_malformed_payload_on_capture_event_never_fails(self):
        for label, payload in (("foo", '{"foo":1}'), ("empty", ""), ("garbage", "garbage")):
            with self.subTest(payload=label):
                session_id = self.records.llm(name=f"m-{label}", state="idle")
                result = self.hook("prompt-submit", session_id, payload)
                self.assert_ok_silent(result)
                record = self.records.load(session_id)
                self.assertIsNone(record["chats"][0]["id"])
                self.assertEqual(record["chats"][0]["transcript_path"], "")
                self.assertEqual(record["state"], "working")

    def test_t_hook_21_non_capture_events_never_capture(self):
        for event in ("working", "stop", "session-end", "notification"):
            with self.subTest(event=event):
                session_id = self.records.llm(name=f"nc-{event}", state="idle")
                result = self.hook(event, session_id, PAYLOAD_C1)
                self.assert_ok_silent(result)
                record = self.records.load(session_id)
                self.assertIsNone(record["chats"][0]["id"])
                self.assertEqual(record["chats"][0]["transcript_path"], "")
