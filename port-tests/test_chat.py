"""Section 03 — CHAT: fork / handover / rollover / resume (spec T-CHAT-01 … T-CHAT-21).

Fixture "source S" (`_source`) is a crafted schema-6 claude record whose cwd is a linked worktree
of the git fixture and whose chat `c1` transcript sits at Claude's fast path. A LIVE S is a raw
tmux session named by its id, created with the CLIENT environment of `self.tx` so the pane finds
the fake engine (`_go_live`). Every distiller path leaves a detached `_chat-op-watch`, killed at
cleanup (`_watch_op`). Rollover finishes are made observable by holding the bundle's ingest lock
(`_hold_ingest_lock`) — the finish blocks on it until the test releases it.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from txkit import TX_BIN, TxCase, expected_failure_on_python, python_reference_only

SKIP = "--dangerously-skip-permissions"
PERSONA = ["--model", "opus", "--effort", "high", "--no-chrome", "--append-system-prompt", "P"]
RO_BLOCK = [
    "--allowedTools", "Bash",
    "--disallowedTools", "Edit", "Write", "NotebookEdit",
    "--permission-mode", "dontAsk",
    "--setting-sources", "user",
]
YOLO = ["--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust"]
CODEX_DEFAULTS = ["-m", "gpt-5.6-sol", "-c", "model_reasoning_effort=high"]
DISTILLER_PREFIX = ["--model", "opus", "--effort", "medium", "--no-chrome", SKIP]

SOURCE_CMD = shlex.join(["claude", *PERSONA, SKIP, "prime"])
RO_SOURCE_CMD = shlex.join(["claude", *PERSONA, *RO_BLOCK, "prime"])
CODEX_SOURCE_CMD = shlex.join(["codex", *CODEX_DEFAULTS, *YOLO, "prime"])
SOURCE_ENV = {"TX_SKILLS": "", "K": "V"}
LIVE_CMD = "claude --model opus prime"
TRANSCRIPT = (
    '{"type":"user","sessionId":"c1","text":"one"}\n'
    '{"type":"assistant","text":"two"}\n'
    '{"type":"user","text":"three"}\n'
)
ROLLOUT = '{"type":"session_meta","payload":{"id":"r1"}}\n{"type":"response_item"}\n'
INSIDE_TMUX = "/tmp/txkit-nonexistent-socket,1,0"

# T-CHAT-15 timings (Q12 FIX, D11): the watcher's poll / grace / timeout via env. TIMEOUT is wide
# enough that "≈GRACE" (GRACE + a poll + the finish itself, WATCH_SLACK) never touches it; leg (b)
# uses the longer grace so the test's own finish provably lands inside the window.
WATCH_POLL = 0.05
WATCH_GRACE = 0.2
WATCH_LONG_GRACE = 1.0
WATCH_TIMEOUT = 4.0
WATCH_SLACK = 2.5
WATCH_ENV = {"TX_CHAT_OP_POLL_S": str(WATCH_POLL), "TX_CHAT_OP_GRACE_S": str(WATCH_GRACE), "TX_CHAT_OP_TIMEOUT_S": str(WATCH_TIMEOUT)}
WATCH_ENV_LONG_GRACE = {**WATCH_ENV, "TX_CHAT_OP_GRACE_S": str(WATCH_LONG_GRACE)}


@dataclass
class Source:
    id: str
    name: str
    cwd: str
    transcript: Path
    env: dict[str, str]


class TestChat(TxCase):
    maxDiff = None

    # ----- fixtures ------------------------------------------------------------------------

    def _chat(
        self,
        session_id: str,
        chat_id: str | None,
        cwd: str,
        transcript_path: str = "",
        *,
        engine: str = "claude",
        role: str = "original",
        how: str = "spawn",
        origin_chat: str | None = None,
        ended_at: float | None = None,
        bundle_path: str | None = None,
    ) -> dict:
        return self.records.chat_ref(
            session_id=session_id,
            id=chat_id,
            role=role,
            cwd=cwd,
            transcript_path=transcript_path,
            how=how,
            chat_id=origin_chat,
            bundle_path=bundle_path,
            started_at=time.time() - 100,
            ended_at=ended_at,
            engine=engine,
        )

    def _source(
        self,
        *,
        name: str = "w1",
        state: str = "exited",
        cmd: str = SOURCE_CMD,
        env: dict[str, str] | None = None,
        chats: list[dict] | None = None,
        transcript: bool = True,
        worktree: str | None = None,
        chat_id: str = "c1",
    ) -> Source:
        cwd = str(self.git.as_linked_worktree(worktree or name))
        source_id = str(uuid.uuid4())
        environment = dict(SOURCE_ENV if env is None else env)
        transcript_path = self.home.claude_transcript_path(cwd, chat_id)
        if transcript:
            transcript_path.parent.mkdir(parents=True, exist_ok=True)
            transcript_path.write_text(TRANSCRIPT)
        if chats is None:
            chats = [self._chat(source_id, chat_id, cwd, str(transcript_path))]
        self.records.llm(
            id=source_id, name=name, state=state, cwd=cwd, cmd=cmd, tags=("a",),
            env=environment, chats=chats, created_at=time.time() - 200,
        )
        return Source(source_id, name, cwd, transcript_path, environment)

    def _codex_source(self, name: str = "cx") -> Source:
        cwd = str(self.git.as_linked_worktree(name))
        source_id = str(uuid.uuid4())
        rollout = self.home.codex_home / "sessions" / "2026" / "09" / "23" / "rollout-2026-09-23T10-00-00-r1.jsonl"
        rollout.parent.mkdir(parents=True, exist_ok=True)
        rollout.write_text(ROLLOUT)
        environment = {"TX_SKILLS": ""}
        self.records.llm(
            id=source_id, name=name, state="exited", cwd=cwd, cmd=CODEX_SOURCE_CMD, tags=("a",),
            env=environment, engine="codex", created_at=time.time() - 200,
            chats=[self._chat(source_id, "r1", cwd, str(rollout), engine="codex")],
        )
        return Source(source_id, name, cwd, rollout, environment)

    def _go_live(self, source: Source, cmd: str = LIVE_CMD) -> str:
        """S's tmux session running the fake engine; returns its pane id."""
        self.tmux.new_session(
            source.id, cmd, tx_id=source.id, cwd=source.cwd,
            env={"TX_SESSION_ID": source.id, **source.env}, client_env=self.tx_env(),
        )
        self.fakes.wait_dump("claude", source.id)
        return self.tmux.display(source.id, "#{pane_id}")

    def _shell_pane(self, source: Source, extra_env: dict[str, str] | None = None) -> str:
        """S's tmux session running a plain `sh` that sees the fakes and tx (T-CHAT-16)."""
        self.tmux.new_session(
            source.id, "sh", tx_id=source.id, cwd=source.cwd,
            env={"TX_SESSION_ID": source.id, **source.env, **(extra_env or {})},
            client_env=self.tx_env(),
        )
        return self.tmux.display(source.id, "#{pane_id}")

    def _craft_spec(
        self,
        source: Source,
        *,
        kind: str = "handover",
        artifact_path: str | None = None,
        self_catch_up: bool = False,
        read_only: bool | None = False,
        worker_name: str = "w1-handover",
        pane: str = "",
        distiller_name: str = "w1-handover-distill",
        omit: tuple[str, ...] = (),
    ) -> str:
        op_id = str(uuid.uuid4())
        if artifact_path is None:
            artifact_path = str(self.home.history_dir / source.id / "handover-t.md")
        spec = {
            "op_id": op_id, "kind": kind, "source_txid": source.id, "source_chat": "c1",
            "cwd": source.cwd, "artifact_path": artifact_path, "self_catch_up": self_catch_up,
            "read_only": read_only, "worker_name": worker_name, "pane": pane,
            "distiller_name": distiller_name,
        }
        if read_only is None:
            del spec["read_only"]
        for key in omit:
            del spec[key]
        directory = self.home.chat_ops_dir / op_id
        directory.mkdir(parents=True)
        (directory / "spec.json").write_text(json.dumps(spec))
        return op_id

    def _hold_ingest_lock(self, source: Source) -> subprocess.Popen:
        lock = self.home.history_dir / source.id / "c1" / ".ingest.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        holder = subprocess.Popen(["flock", "-x", str(lock), "sleep", "120"], start_new_session=True)
        self.addCleanup(self._release, holder)
        self.wait_until(lambda: subprocess.run(["flock", "-n", str(lock), "true"]).returncode != 0)
        return holder

    @staticmethod
    def _release(holder: subprocess.Popen) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(holder.pid, signal.SIGKILL)
        holder.wait()

    # ----- observation ---------------------------------------------------------------------

    def _show(self, target: str) -> dict:
        result = self.tx(["show", target])
        self.assertEqual(result.code, 0, result.err)
        return json.loads(result.out)

    def _records(self) -> list[dict]:
        return [json.loads(path.read_text()) for path in self.home.sessions_dir.glob("*.json")]

    def _record_ids(self) -> list[str]:
        return sorted(path.stem for path in self.home.sessions_dir.glob("*.json"))

    def _new_record(self, before: list[str]) -> dict:
        added = sorted(set(self._record_ids()) - set(before))
        self.assertEqual(len(added), 1, added)
        return self._show(added[0])

    def _named(self, name: str) -> list[dict]:
        return [record for record in self._records() if record["name"] == name]

    def _worktrees(self) -> list[str]:
        return sorted(os.path.realpath(path) for path in self.git.worktrees())

    def _log(self, count: int) -> list[tuple[str, str]]:
        return [(line["type"], line["msg"]) for line in self.log_tail(count)]

    def _op_ids(self) -> list[str]:
        if not self.home.chat_ops_dir.exists():
            return []
        return sorted(path.name for path in self.home.chat_ops_dir.iterdir())

    def _only_op(self) -> str:
        ops = self._op_ids()
        self.assertEqual(len(ops), 1, ops)
        return ops[0]

    def _spec(self, op_id: str) -> dict:
        return json.loads((self.home.chat_ops_dir / op_id / "spec.json").read_text())

    def _watch_op(self, op_id: str) -> None:
        self.addCleanup(subprocess.run, ["pkill", "-f", f"_chat-op-watch {op_id}"])

    def _watcher_running(self, op_id: str) -> bool:
        return subprocess.run(["pgrep", "-f", f"_chat-op-watch {op_id}"], capture_output=True).returncode == 0

    def _start_command(self, pane: str) -> str:
        """The single command argument a respawn passed to tmux. tmux 3.4 renders it in
        `#{pane_start_command}` double-quoted (args_escape) — split it back to the argument."""
        arguments = shlex.split(self.tmux.display(pane, "#{pane_start_command}"))
        self.assertEqual(len(arguments), 1, arguments)
        return arguments[0]

    def _screen(self, pane: str, *, scrollback: bool = False) -> str:
        """The pane text with wrapped lines joined (`-J`); `scrollback` includes the history."""
        history = ["-S", "-"] if scrollback else []
        return self.tmux.run("capture-pane", "-p", "-J", *history, "-t", pane, check=True).stdout

    def _wait_respawn(self, source: Source, seed: str) -> dict:
        """The fake dump for S's pane once a NEW fake claude carrying `seed` runs in it."""
        path = self.fakes.dump_path("claude", source.id)
        self.wait_until(lambda: path.exists() and json.loads(path.read_text())["argv"][-1] == seed, timeout=15)
        return json.loads(path.read_text())

    def _assert_worker_cwd(self, cwd: str, basename: str) -> None:
        self.assertTrue(cwd.startswith(str(self.home.worktrees_dir)), cwd)
        self.assertEqual(os.path.basename(cwd), basename)
        self.assertTrue(Path(cwd).parent.name.startswith("repo-"), cwd)
        self.assertIn(os.path.realpath(cwd), self._worktrees())

    def _pending(self, role: str, cwd: str, session_id: str, chat_id: str, engine: str = "claude") -> dict:
        return {
            "id": None, "role": role, "cwd": cwd, "transcript_path": "",
            "origin": {"how": role, "session_id": session_id, "chat_id": chat_id},
            "bundle_path": None, "ended_at": None, "summary": "", "engine": engine,
        }

    # ----- seeds (exactly as lib/tx/chat.py builds them) -----------------------------------

    def _bundle(self, source: Source, chat_id: str = "c1") -> Path:
        return self.home.history_dir / source.id / chat_id

    def _invocation(self, op_id: str) -> str:
        return (
            f"env TX_IDE_HOME={shlex.quote(str(self.home.path))} {shlex.quote(TX_BIN)} "
            f"_chat-op-finish {shlex.quote(op_id)}"
        )

    def _distiller_seed(self, source: Source, brief: str, task: str, op_id: str, chat_id: str = "c1") -> str:
        return (
            f"You are a tx-ide handover distiller (a temporary helper). Read the predecessor chat "
            f"bundle at {self._bundle(source, chat_id)}/transcript.jsonl . "
            f"Distill a focused, self-contained brief for this task and write it to {brief} : "
            f"«{task}». Capture only what the new worker needs to start — relevant context, current "
            f"state, constraints, and key file paths — not the whole history. When the brief file is "
            f"saved, run exactly this command and nothing else: {self._invocation(op_id)}"
        )

    def _catch_up_seed(self, source: Source) -> str:
        return (
            f"You are taking over work via tx handover. There is no pre-written brief — read the "
            f"predecessor bundle at {self._bundle(source)}/ (transcript.jsonl + subagents/ + tool-results/), "
            f"write yourself a short brief of the task and its state, then begin."
        )

    def _brief_seed(self, source: Source, brief: str) -> str:
        return (
            f"Your task brief is at {brief} — read it and begin. Fuller predecessor "
            f"history, only if the brief is insufficient: {self._bundle(source)}/ ."
        )

    def _summariser_seed(self, source: Source, note: str, op_id: str) -> str:
        return (
            f"You are a tx-ide rollover summariser (a temporary helper). Read the predecessor chat "
            f"bundle at {self._bundle(source)}/transcript.jsonl . Write a concise "
            f"hand-off note — the current task, what is done, the immediate next steps, and key files "
            f"and decisions — to {note} . When the note file is saved, run exactly this command "
            f"and nothing else: {self._invocation(op_id)}"
        )

    def _note_seed(self, source: Source, note: str) -> str:
        return (
            f"Continuing prior work in a fresh chat (rollover). Hand-off note: {note} "
            f"— read it and continue. If it looks truncated or you are missing the most recent "
            f"context, catch up from the predecessor transcript at {self._bundle(source)}/transcript.jsonl ."
        )

    def _rollover_bundle_seed(self, source: Source) -> str:
        return (
            f"Continuing prior work in a fresh chat (rollover). The predecessor bundle is at "
            f"{self._bundle(source)}/ (transcript.jsonl + subagents/ + tool-results/) — read what you need to "
            f"resume, then continue."
        )

    # ----- T-CHAT-01 active_chat selection ----------------------------------------------------

    def _variant(self, name: str, shape: list[tuple[str | None, float | None]]) -> Source:
        source_id = str(uuid.uuid4())
        cwd = str(self.git.as_linked_worktree(name))
        chats = [self._chat(source_id, chat_id, cwd, ended_at=ended_at) for chat_id, ended_at in shape]
        self.records.llm(id=source_id, name=name, state="exited", cwd=cwd, cmd=SOURCE_CMD, tags=("a",), env=SOURCE_ENV, chats=chats)
        return Source(source_id, name, cwd, self.home.claude_transcript_path(cwd, "c1"), dict(SOURCE_ENV))

    def test_t_chat_01_active_chat_selection(self):
        ended = time.time() - 50
        selecting = {
            "v3": ([("a", None), ("b", None)], "b"),
            "v4": ([("a", None), ("b", ended)], "a"),
            "v5": ([("a", ended), ("b", ended)], "b"),
            "v6": ([("a", None), (None, None)], "a"),
        }
        for name, (shape, expected) in selecting.items():
            self._variant(name, shape)
            result = self.tx(["fork", name])
            self.assertEqual(result.code, 0, result.err)
            new = self._show(f"{name}-fork")
            dump = self.fakes.wait_dump("claude", new["id"])
            self.assertEqual(dump["argv"][1:3], ["--resume", expected])
            self.assertEqual(new["chats"][0]["origin"]["chat_id"], expected)
        for name, shape in {"v1": [], "v2": [(None, None)]}.items():
            self._variant(name, shape)
            before = (self._record_ids(), len(self.fakes.dumps("claude")))
            result = self.tx(["fork", name])
            self.assertEqual(result.code, 1)
            self.assertEqual(result.err, f"tx fork: fork: '{name}' has no chat to fork\n")
            self.assertEqual(result.out, "")
            self.assertEqual((self._record_ids(), len(self.fakes.dumps("claude"))), before)

    def test_t_chat_01_resume_applies_the_same_rule(self):
        ended = time.time() - 50
        self._variant("r1", [])
        self._variant("r2", [(None, None)])
        for name in ("r1", "r2"):
            before = self._record_ids()
            result = self.tx(["resume", name])
            self.assertEqual(result.code, 1)
            self.assertEqual(result.err, f"tx resume: '{name}' has no chat to resume — use `tx spawn` for a fresh session\n")
            self.assertEqual(self._record_ids(), before)
        selecting = {
            "r3": ([("a", None), ("b", None)], "b"),
            "r4": ([("a", None), ("b", ended)], "a"),
            "r5": ([("a", ended), ("b", ended)], "b"),
            "r6": ([("a", None), (None, None)], "a"),
        }
        for name, (shape, expected) in selecting.items():
            self._variant(name, shape)
            before = self._record_ids()
            result = self.tx(["resume", name])
            self.assertEqual(result.code, 0, result.err)
            new = self._new_record(before)
            dump = self.fakes.wait_dump("claude", new["id"])
            self.assertEqual(dump["argv"][1:3], ["--resume", expected], name)
            self.assertEqual(new["chats"][0]["id"], expected, name)
            self.assertEqual(new["chats"][0]["origin"]["chat_id"], expected, name)

    # ----- T-CHAT-02 fork happy path --------------------------------------------------------

    def test_t_chat_02_fork_happy_path(self):
        source = self._source()
        before = time.time()
        result = self.tx(["fork", "w1"])
        self.assertEqual(result.code, 0, result.err)
        new = self._show("w1-fork")
        self.assertEqual(result.out, f"Forked 'w1' → 'w1-fork' (chat pending, cwd={new['cwd']})\n")
        self.assertEqual(new["name"], "w1-fork")
        self.assertEqual(new["role"], "llm")
        self.assertEqual(new["engine"], "claude")
        self.assertEqual(new["tags"], ["a"])
        self.assertEqual(new["parent"], source.id)
        self.assertIsNone(new["group"])
        self.assertEqual(new["env"], {"TX_SKILLS": "", "K": "V", "TX_REQUIRE_WORKTREE": "1"})
        self._assert_worker_cwd(new["cwd"], "repo--w1-fork")
        expected_argv = ["--resume", "c1", "--fork-session", *PERSONA, SKIP]
        self.assertEqual(new["cmd"], shlex.join(["claude", *expected_argv]))
        self.assertEqual(len(new["chats"]), 1)
        started_at = new["chats"][0].pop("started_at")
        self.assertTrue(before <= started_at <= time.time())
        self.assertEqual(new["chats"][0], self._pending("fork", new["cwd"], source.id, "c1"))
        chat_ls = self.tx(["chat", "ls", "w1-fork"])
        self.assertEqual(chat_ls.code, 0, chat_ls.err)
        self.assertEqual(chat_ls.lines[0], "w1-fork — 1 chat(s)")
        self.assertTrue(chat_ls.lines[1].startswith("  pending   fork      fork←c1"), chat_ls.out)
        dump = self.fakes.wait_dump("claude", new["id"])
        self.assertEqual(dump["argv"][1:], expected_argv)
        self.assertEqual(dump["env"]["TX_SESSION_ID"], new["id"])
        self.assertEqual(dump["env"]["TX_REQUIRE_WORKTREE"], "1")
        self.assertEqual(dump["env"]["TX_SKILLS"], "")
        self.assertNotIn("TX_READ_ONLY", dump["env"])
        self.assertTrue(self.home.claude_transcript_path(new["cwd"], "c1").exists())
        self.assertIn(new["id"], self.tmux.sessions())
        self.assertEqual(self.tmux.option(new["id"], "@tx_id"), new["id"])
        self.assertEqual(self._log(2), [("spawn", f"w1-fork [llm] {new['cwd']}"), ("fork", "w1 → w1-fork (chat pending)")])

    def test_t_chat_02_fork_name_group_and_read_only_edges(self):
        source = self._source()
        result = self.tx(["fork", "w1", "mine"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self._show("mine")["name"], "mine")
        result = self.tx(["fork", "w1"])
        self.assertEqual(result.code, 0, result.err)
        first = self._show("w1-fork")
        result = self.tx(["fork", "w1"])
        self.assertEqual(result.code, 0, result.err)
        self.assertIn("→ 'w1-fork-2' ", result.out)
        second = self._show("w1-fork-2")
        self._assert_worker_cwd(second["cwd"], "repo--w1-fork-2")
        self.assertNotEqual(first["id"], second["id"])
        result = self.tx(["fork", "w1", "grouped", "--group", "g"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self._show("grouped")["group"], "g")
        result = self.tx(["fork", "w1", "ro", "--read-only"])
        self.assertEqual(result.code, 0, result.err)
        read_only = self._show("ro")
        self.assertEqual(read_only["env"], {"TX_SKILLS": "", "K": "V", "TX_READ_ONLY": "1"})
        dump = self.fakes.wait_dump("claude", read_only["id"])
        self.assertEqual(dump["argv"][1:], ["--resume", "c1", "--fork-session", *PERSONA, *RO_BLOCK])
        self.assertEqual(dump["env"]["TX_READ_ONLY"], "1")
        self.assertNotIn("TX_REQUIRE_WORKTREE", dump["env"])
        writable_dump = self.fakes.wait_dump("claude", first["id"])
        self.assertEqual(writable_dump["env"]["TX_REQUIRE_WORKTREE"], "1")
        self.assertNotIn("TX_READ_ONLY", writable_dump["env"])
        self.assertEqual(first["env"], {"TX_SKILLS": "", "K": "V", "TX_REQUIRE_WORKTREE": "1"})

    def test_t_chat_02_fork_refusals(self):
        self._source(name="nochat", chats=[])
        for argv, message in (
            (["fork", "nope"], "tx fork: fork: source session 'nope' not found\n"),
            (["fork", "nochat"], "tx fork: fork: 'nochat' has no chat to fork\n"),
        ):
            before = (self._record_ids(), self._worktrees())
            result = self.tx(argv)
            self.assertEqual(result.code, 1)
            self.assertEqual(result.err, message)
            self.assertEqual(result.out, "")
            self.assertEqual((self._record_ids(), self._worktrees()), before)

    # ----- T-CHAT-03 fork per engine --------------------------------------------------------

    def test_t_chat_03_fork_per_engine_codex(self):
        source = self._codex_source()
        result = self.tx(["fork", "cx"])
        self.assertEqual(result.code, 0, result.err)
        new = self._show("cx-fork")
        self.assertEqual(result.out, f"Forked 'cx' → 'cx-fork' (chat pending, cwd={new['cwd']})\n")
        expected_argv = ["fork", "r1", *CODEX_DEFAULTS, *YOLO]
        dump = self.fakes.wait_dump("codex", new["id"])
        self.assertEqual(dump["argv"][1:], expected_argv)
        self.assertEqual(dump["env"]["TX_SESSION_ID"], new["id"])
        self.assertEqual(new["engine"], "codex")
        self.assertEqual(new["cmd"], shlex.join(["codex", *expected_argv]))
        self.assertEqual(len(new["chats"]), 1)
        new["chats"][0].pop("started_at")
        self.assertEqual(new["chats"][0], self._pending("fork", new["cwd"], source.id, "r1", engine="codex"))
        self.assertFalse((self.home.claude_config_dir / "projects").exists())

    # ----- T-CHAT-05 handover with distiller ------------------------------------------------

    def _handover_spec(self, source: Source, op_id: str, brief: str, worker: str, distiller: str) -> dict:
        return {
            "op_id": op_id, "kind": "handover", "source_txid": source.id, "source_chat": "c1",
            "cwd": source.cwd, "artifact_path": brief, "self_catch_up": False, "read_only": False,
            "worker_name": worker, "pane": "", "distiller_name": distiller,
        }

    def test_t_chat_05_handover_with_distiller(self):
        source = self._source()
        result = self.tx(["handover", "w1", "Fix the parser bug!"])
        op_id = self._only_op()
        self._watch_op(op_id)
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, "Handover 'w1' → worker 'w1-handover' (distilling brief; launches when ready)\n")
        bundle = self._bundle(source) / "transcript.jsonl"
        self.assertEqual(bundle.read_bytes(), TRANSCRIPT.encode())
        self.assertEqual(uuid.UUID(op_id).version, 4)
        self.assertEqual(sorted(path.name for path in (self.home.chat_ops_dir / op_id).iterdir()), ["spec.json"])
        brief = str(self.home.history_dir / source.id / "handover-fix-the-parser-bug.md")
        self.assertEqual(self._spec(op_id), self._handover_spec(source, op_id, brief, "w1-handover", "w1-handover-distill"))
        distiller = self._show("w1-handover-distill")
        self.assertEqual(distiller["tags"], ["temporary", "handover"])
        self.assertEqual(distiller["engine"], "claude")
        self._assert_worker_cwd(distiller["cwd"], "repo--w1-handover-distill")
        self.assertEqual(distiller["env"], {"TX_REQUIRE_WORKTREE": "1"})
        self.assertIsNone(distiller["parent"])
        self.assertEqual(len(distiller["chats"]), 1)
        distiller["chats"][0].pop("started_at")
        self.assertEqual(distiller["chats"][0], {**self._pending("original", distiller["cwd"], distiller["id"], None), "origin": {"how": "spawn", "session_id": distiller["id"], "chat_id": None}})
        seed = self._distiller_seed(source, brief, "Fix the parser bug!", op_id)
        self.assertEqual(distiller["cmd"], shlex.join(["claude", *DISTILLER_PREFIX, seed]))
        dump = self.fakes.wait_dump("claude", distiller["id"])
        self.assertEqual(dump["argv"][1:], [*DISTILLER_PREFIX, seed])
        self.assertTrue(self._watcher_running(op_id))
        self.assertEqual(self._log(2), [("spawn", f"w1-handover-distill [llm] {distiller['cwd']}"), ("handover", "w1 → w1-handover (distilling)")])
        show = self.tx(["show", "w1-handover"])
        self.assertEqual(show.code, 1)
        self.assertEqual(show.err, "tx show: no record for 'w1-handover'\n")

    def test_t_chat_05_slug_and_name_edges(self):
        source = self._source()
        sixty = "a" * 60
        for task, basename in (("!!!", "handover-task.md"), (sixty, f"handover-{'a' * 48}.md"), ("A  b", "handover-a-b.md")):
            before = set(self._op_ids())
            result = self.tx(["handover", "w1", task])
            self.assertEqual(result.code, 0, result.err)
            op_id = (set(self._op_ids()) - before).pop()
            self._watch_op(op_id)
            self.assertEqual(os.path.basename(self._spec(op_id)["artifact_path"]), basename)
        before = set(self._op_ids())
        result = self.tx(["handover", "w1", "task", "myname"])
        self.assertEqual(result.code, 0, result.err)
        op_id = (set(self._op_ids()) - before).pop()
        self._watch_op(op_id)
        spec = self._spec(op_id)
        self.assertEqual((spec["worker_name"], spec["distiller_name"]), ("myname", "myname-distill"))
        self.assertEqual(self._show("myname-distill")["tags"], ["temporary", "handover"])

    def test_t_chat_05_codex_source(self):
        source = self._codex_source()
        result = self.tx(["handover", "cx", "task"])
        self.assertEqual(result.code, 0, result.err)
        op_id = self._only_op()
        self._watch_op(op_id)
        brief = str(self.home.history_dir / source.id / "handover-task.md")
        seed = self._distiller_seed(source, brief, "task", op_id, chat_id="r1")
        distiller = self._show("cx-handover-distill")
        dump = self.fakes.wait_dump("codex", distiller["id"])
        self.assertEqual(dump["argv"][1:], [*CODEX_DEFAULTS, *YOLO, seed])
        self.assertEqual(distiller["cmd"], shlex.join(["codex", *CODEX_DEFAULTS, *YOLO, seed]))
        self.assertEqual((self._bundle(source, "r1") / "transcript.jsonl").read_bytes(), ROLLOUT.encode())

    # ----- T-CHAT-06 handover --self-catch-up -----------------------------------------------

    def test_t_chat_06_handover_self_catch_up_finishes_synchronously(self):
        source = self._source()
        result = self.tx(["handover", "w1", "t", "--self-catch-up"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, "Handover 'w1' → worker 'w1-handover' (self-catch-up; launches when ready)\n")
        worker = self._show("w1-handover")
        self.assertEqual(worker["tags"], ["a"])
        self.assertEqual(worker["parent"], source.id)
        self.assertEqual(worker["engine"], "claude")
        self.assertEqual(worker["env"], {"TX_SKILLS": "", "K": "V", "TX_REQUIRE_WORKTREE": "1"})
        self._assert_worker_cwd(worker["cwd"], "repo--w1-handover")
        seed = self._catch_up_seed(source)
        self.assertEqual(worker["cmd"], shlex.join(["claude", *PERSONA, SKIP, seed]))
        dump = self.fakes.wait_dump("claude", worker["id"])
        self.assertEqual(dump["argv"][1:], [*PERSONA, SKIP, seed])
        self.assertEqual(dump["env"]["TX_SESSION_ID"], worker["id"])
        self.assertEqual(len(worker["chats"]), 1)
        worker["chats"][0].pop("started_at")
        self.assertEqual(worker["chats"][0], self._pending("handover", worker["cwd"], source.id, "c1"))
        self.assertTrue((self._bundle(source) / "transcript.jsonl").exists())
        self.assertEqual(self._op_ids(), [])
        self.assertEqual([record["name"] for record in self._records() if record["name"].endswith("-distill")], [])
        self.assertEqual(self._log(3), [
            ("spawn", f"w1-handover [llm] {worker['cwd']}"),
            ("handover-finish", "w1-handover (chat pending)"),
            ("handover", "w1 → w1-handover (self-catch-up)"),
        ])

    # ----- T-CHAT-07 _chat-op-finish handover with brief ------------------------------------

    def test_t_chat_07_finish_handover_with_brief_present(self):
        source = self._source()
        brief = self.home.history_dir / source.id / "handover-t.md"
        brief.parent.mkdir(parents=True)
        brief.write_text("# brief\n")
        op_id = self._craft_spec(source, artifact_path=str(brief))
        result = self.tx(["_chat-op-finish", op_id])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, "")
        worker = self._show("w1-handover")
        dump = self.fakes.wait_dump("claude", worker["id"])
        seed = dump["argv"][-1]
        self.assertEqual(seed, self._brief_seed(source, str(brief)))
        self.assertEqual(
            seed,
            f"Your task brief is at {brief} — read it and begin. Fuller predecessor history, only if the brief is insufficient: {self.home.path}/history/{source.id}/c1/ .",
        )
        normalised = seed.replace(str(brief), "<brief>").replace(str(self.home.path), "<home>").replace(source.id, "<S.id>")
        self.assert_golden("chat/07", normalised)
        op_dir = self.home.chat_ops_dir / op_id
        self.assertTrue((op_dir / "claim").is_dir())
        self.assertTrue((op_dir / "done").is_file())
        self.assertTrue((op_dir / "spec.json").is_file())

    def test_t_chat_07_finish_handover_edges(self):
        source = self._source()
        op_id = self._craft_spec(source, worker_name="w1-b")
        result = self.tx(["_chat-op-finish", op_id])
        self.assertEqual(result.code, 0, result.err)
        dump = self.fakes.wait_dump("claude", self._show("w1-b")["id"])
        self.assertEqual(dump["argv"][-1], self._catch_up_seed(source))
        op_id = self._craft_spec(source, worker_name="w1-ro", read_only=True)
        result = self.tx(["_chat-op-finish", op_id])
        self.assertEqual(result.code, 0, result.err)
        worker = self._show("w1-ro")
        dump = self.fakes.wait_dump("claude", worker["id"])
        self.assertEqual(dump["argv"][1:], [*PERSONA, *RO_BLOCK, self._catch_up_seed(source)])
        self.assertEqual(worker["env"]["TX_READ_ONLY"], "1")
        self.assertNotIn("TX_REQUIRE_WORKTREE", worker["env"])
        op_id = self._craft_spec(source, worker_name="w1-gone")
        self.records.path(source.id).unlink()
        before = self._record_ids()
        result = self.tx(["_chat-op-finish", op_id])
        self.assertEqual(result.code, 1)
        self.assertEqual(result.err, f"tx _chat-op-finish: _chat-op-finish: source record '{source.id}' not found\n")
        self.assertEqual(self._record_ids(), before)

    # ----- T-CHAT-08 finish idempotence -----------------------------------------------------

    def test_t_chat_08_finish_idempotent(self):
        source = self._source()
        op_id = self._craft_spec(source)
        racers = [self.tx_popen(["_chat-op-finish", op_id]) for _ in range(2)]
        outputs = [racer.communicate(timeout=60) for racer in racers]
        self.assertEqual([racer.returncode for racer in racers], [0, 0], outputs)
        workers = self._named("w1-handover")
        self.assertEqual(len(workers), 1)
        # The pane's fake claude starts asynchronously after the finisher exits: wait for THAT
        # worker's dump, then count — exactly one fake ran.
        self.fakes.wait_dump("claude", workers[0]["id"])
        self.assertEqual(len(self.fakes.dumps("claude")), 1)
        result = self.tx(["_chat-op-finish", op_id])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(len(self._named("w1-handover")), 1)
        self.assertEqual(len(self.fakes.dumps("claude")), 1)
        done_first = self._craft_spec(source, worker_name="w1-done")
        (self.home.chat_ops_dir / done_first / "done").write_text("")
        result = self.tx(["_chat-op-finish", done_first])
        self.assertEqual(result.code, 0, result.err)
        self.assertFalse((self.home.chat_ops_dir / done_first / "claim").exists())
        self.assertEqual(self._named("w1-done"), [])
        claimed = self._craft_spec(source, worker_name="w1-claimed")
        (self.home.chat_ops_dir / claimed / "claim").mkdir()
        result = self.tx(["_chat-op-finish", claimed])
        self.assertEqual(result.code, 0, result.err)
        self.assertFalse((self.home.chat_ops_dir / claimed / "done").exists())
        self.assertEqual(self._named("w1-claimed"), [])
        self.assertEqual(len(self.fakes.dumps("claude")), 1)

    # ----- T-CHAT-09 ChatOpSpec load defaults -----------------------------------------------

    def test_t_chat_09_spec_without_read_only_key_is_writable(self):
        source = self._source()
        op_id = self._craft_spec(source, read_only=None)
        result = self.tx(["_chat-op-finish", op_id])
        self.assertEqual(result.code, 0, result.err)
        worker = self._show("w1-handover")
        dump = self.fakes.wait_dump("claude", worker["id"])
        self.assertIn(SKIP, dump["argv"])
        self.assertNotIn("--permission-mode", dump["argv"])
        self.assertEqual(worker["env"]["TX_REQUIRE_WORKTREE"], "1")
        self.assertNotIn("TX_READ_ONLY", worker["env"])

    @expected_failure_on_python
    def test_t_chat_09_fixed_missing_key_exit_1_no_traceback(self):
        source = self._source()
        op_id = self._craft_spec(source, omit=("cwd",))
        result = self.tx(["_chat-op-finish", op_id])
        self.assertEqual(result.code, 1)
        self.assertNotIn("Traceback", result.err)
        self.assertTrue(result.err.startswith("tx _chat-op-finish: "), result.err)
        self.assertIn("cwd", result.err)
        self.assertEqual(self._named("w1-handover"), [])
        self.assertEqual(self.fakes.dumps("claude"), [])
        self.assertFalse((self.home.chat_ops_dir / op_id / "done").exists())
        retry = self.tx(["_chat-op-finish", op_id])
        self.assertEqual(retry.code, 1)
        self.assertEqual(self._named("w1-handover"), [])

    @python_reference_only
    def test_t_chat_09_parity_missing_worker_name_spawns_dash_2(self):
        # Q39 (reference): `worker_name` defaults to "" and `next_name("")` yields `-2`.
        source = self._source()
        op_id = self._craft_spec(source, omit=("worker_name",))
        result = self.tx(["_chat-op-finish", op_id])
        self.assertEqual(result.code, 0, result.err)
        workers = self._named("-2")
        self.assertEqual(len(workers), 1)
        self.fakes.wait_dump("claude", workers[0]["id"])
        self.assertTrue((self.home.chat_ops_dir / op_id / "done").is_file())

    @expected_failure_on_python
    def test_t_chat_09_fixed_missing_worker_name_refused(self):
        # Q39 FIX (folds into Q25): an empty worker name is refused, nothing is spawned.
        source = self._source()
        op_id = self._craft_spec(source, omit=("worker_name",))
        before = self._record_ids()
        result = self.tx(["_chat-op-finish", op_id])
        self.assertEqual(result.code, 1)
        self.assertNotIn("Traceback", result.err)
        self.assertTrue(result.err.startswith("tx _chat-op-finish: "), result.err)
        self.assertRegex(result.err, r"worker[ _]name")
        self.assertEqual(self._named("-2"), [])
        self.assertEqual(self._record_ids(), before)
        self.assertEqual(self.fakes.dumps("claude"), [])
        self.assertFalse((self.home.chat_ops_dir / op_id / "done").exists())

    # ----- T-CHAT-10 rollover pane resolution -----------------------------------------------

    def _rollover_op(self, argv: list[str], env: dict[str, str] | None = None) -> str:
        before = set(self._op_ids())
        result = self.tx(argv, env=env)
        self.assertEqual(result.code, 0, result.err)
        op_id = (set(self._op_ids()) - before).pop()
        self._watch_op(op_id)
        return op_id

    def test_t_chat_10_rollover_pane_resolution(self):
        source = self._source(state="waiting")
        pane = self._go_live(source)
        other = self._source(name="other", state="waiting")
        other_pane = self._go_live(other)
        self.assertNotEqual(pane, other_pane)
        inside = self._rollover_op(["rollover"], env={"TMUX": INSIDE_TMUX, "TMUX_PANE": pane})
        self.assertEqual(self._spec(inside)["pane"], pane)
        outside = self._rollover_op(["rollover", "w1"])
        self.assertEqual(self._spec(outside)["pane"], self.tmux.display(source.id, "#{pane_id}"))
        self.assertEqual(self._spec(outside)["pane"], pane)
        elsewhere = self._rollover_op(["rollover", "w1"], env={"TMUX": INSIDE_TMUX, "TMUX_PANE": other_pane})
        self.assertEqual(self._spec(elsewhere)["pane"], pane)

    def test_t_chat_10_rollover_refusals(self):
        self._source()
        self._source(name="nochat", chats=[])
        for argv, message in (
            (["rollover"], "tx rollover: rollover: run inside a tmux session or pass <session>\n"),
            (["rollover", "w1"], "tx rollover: rollover: could not resolve a pane for 'w1'\n"),
            (["rollover", "nochat"], "tx rollover: rollover: 'nochat' has no active chat to roll over\n"),
        ):
            result = self.tx(argv)
            self.assertEqual(result.code, 1, argv)
            self.assertEqual(result.err, message)
            self.assertEqual(result.out, "")
        self.assertEqual(self._op_ids(), [])

    # ----- T-CHAT-11 rollover with distiller ------------------------------------------------

    def test_t_chat_11_rollover_with_distiller(self):
        source = self._source(state="waiting")
        pane = self._go_live(source)
        pane_pid = self.tmux.display(pane, "#{pane_pid}")
        first_dump = self.fakes.wait_dump("claude", source.id)
        (self.home.history_dir / source.id).mkdir(parents=True)
        (self.home.history_dir / source.id / "rollover-1.md").write_text("old note\n")
        result = self.tx(["rollover", "w1"])
        op_id = self._only_op()
        self._watch_op(op_id)
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, "Rollover scheduled (summarizing first); the same session rotates onto a fresh chat when ready\n")
        self.assertEqual((self._bundle(source) / "transcript.jsonl").read_bytes(), TRANSCRIPT.encode())
        note = str(self.home.history_dir / source.id / "rollover-2.md")
        self.assertEqual(self._spec(op_id), {
            "op_id": op_id, "kind": "rollover", "source_txid": source.id, "source_chat": "c1",
            "cwd": source.cwd, "artifact_path": note, "self_catch_up": False, "read_only": False,
            "worker_name": "", "pane": pane, "distiller_name": "w1-rollover-distill",
        })
        self.assertEqual(sorted(path.name for path in (self.home.chat_ops_dir / op_id).iterdir()), ["spec.json"])
        distiller = self._show("w1-rollover-distill")
        self.assertEqual(distiller["tags"], ["temporary", "rollover"])
        self._assert_worker_cwd(distiller["cwd"], "repo--w1-rollover-distill")
        seed = self._summariser_seed(source, note, op_id)
        self.assertEqual(distiller["cmd"], shlex.join(["claude", *DISTILLER_PREFIX, seed]))
        dump = self.fakes.wait_dump("claude", distiller["id"])
        self.assertEqual(dump["argv"][1:], [*DISTILLER_PREFIX, seed])
        self.assertEqual(self.tmux.display(pane, "#{pane_pid}"), pane_pid)
        self.assertEqual(self.fakes.wait_dump("claude", source.id)["pid"], first_dump["pid"])
        record = self._show(source.id)
        self.assertEqual(len(record["chats"]), 1)
        self.assertEqual((record["chats"][0]["id"], record["chats"][0]["ended_at"]), ("c1", None))
        self.assertEqual(record["chats"][0]["bundle_path"], str(self._bundle(source)))
        self.assertTrue(self._watcher_running(op_id))
        self.assertEqual(self._log(2), [("spawn", f"w1-rollover-distill [llm] {distiller['cwd']}"), ("rollover", "w1 (distilling)")])

    def test_t_chat_11_first_note_is_rollover_1(self):
        source = self._source(state="waiting")
        self._go_live(source)
        op_id = self._rollover_op(["rollover", "w1"])
        self.assertEqual(self._spec(op_id)["artifact_path"], str(self.home.history_dir / source.id / "rollover-1.md"))

    # ----- T-CHAT-12 rollover --self-catch-up -----------------------------------------------

    def test_t_chat_12_rollover_self_catch_up_detaches_the_finish(self):
        source = self._source(state="waiting")
        pane = self._go_live(source)
        holder = self._hold_ingest_lock(source)
        before = time.time()
        result = self.tx(["rollover", "w1", "--self-catch-up"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, "Rollover scheduled (self-catch-up); the same session rotates onto a fresh chat when ready\n")
        self.assertEqual(self._log(1), [("rollover", "w1 (self-catch-up)")])
        op_id = self._only_op()
        spec = self._spec(op_id)
        self.assertEqual((spec["kind"], spec["artifact_path"], spec["self_catch_up"], spec["distiller_name"], spec["pane"]), ("rollover", "", True, "", pane))
        released_at = time.monotonic()
        self._release(holder)
        seed = self._rollover_bundle_seed(source)
        dump = self._wait_respawn(source, seed)
        self.assertEqual(dump["env"]["TX_SESSION_ID"], source.id)
        self.assertEqual(self.tmux.display(source.id, "#{pane_id}"), pane)
        self.wait_until(lambda: not (self.home.chat_ops_dir / op_id).exists())
        self.assertLessEqual(time.monotonic() - released_at, 5.0)  # the Then's bounded wait
        record = self._show(source.id)
        self.assertEqual(record["state"], "waiting")
        self.assertEqual(len(record["chats"]), 2)
        self.assertEqual(record["chats"][0]["id"], "c1")
        self.assertTrue(before <= record["chats"][0]["ended_at"] <= time.time())
        record["chats"][1].pop("started_at")
        self.assertEqual(record["chats"][1], self._pending("rollover", source.cwd, source.id, "c1"))
        self.assertTrue((self._bundle(source) / "transcript.jsonl").exists())
        self.assertEqual([record["name"] for record in self._records() if "distill" in record["name"]], [])
        self.assertEqual(self._log(1), [("rollover-finish", "w1 (chat pending)")])

    # ----- T-CHAT-13 _chat-op-finish rollover respawns the pane -----------------------------

    def _rollover_spec(self, source: Source, pane: str, artifact_path: str) -> str:
        return self._craft_spec(
            source, kind="rollover", artifact_path=artifact_path, pane=pane,
            worker_name="", distiller_name="w1-rollover-distill",
        )

    def test_t_chat_13_finish_rollover_respawns_pane_in_place(self):
        source = self._source(state="waiting", env={"K": "V"})
        pane = self._go_live(source)
        ingest = self.tx(["hook", "ingest", source.id])
        self.assertEqual(ingest.code, 0, ingest.err)
        with source.transcript.open("a") as handle:
            handle.write('{"type":"assistant","text":"after the earlier ingest"}\n')
        note = self.home.history_dir / source.id / "rollover-1.md"
        note.write_text("# note\n")
        op_id = self._rollover_spec(source, pane, str(note))
        before = time.time()
        result = self.tx(["_chat-op-finish", op_id])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual((self._bundle(source) / "transcript.jsonl").read_bytes(), source.transcript.read_bytes())
        seed = self._note_seed(source, str(note))
        self.assertEqual(
            seed,
            f"Continuing prior work in a fresh chat (rollover). Hand-off note: {note} — read it and continue. If it looks truncated or you are missing the most recent context, catch up from the predecessor transcript at {self.home.path}/history/{source.id}/c1/transcript.jsonl .",
        )
        engine_command = shlex.join(["claude", *PERSONA, SKIP, seed])
        self.assertEqual(self._start_command(pane), f"env K=V TX_SESSION_ID={source.id} {engine_command}")
        dump = self._wait_respawn(source, seed)
        self.assertEqual(dump["env"]["TX_SESSION_ID"], source.id)
        self.assertEqual(dump["env"]["K"], "V")
        self.assertEqual(os.path.realpath(dump["cwd"]), os.path.realpath(source.cwd))
        self.assertEqual(dump["argv"][1:], [*PERSONA, SKIP, seed])
        self.assertIn(source.id, self.tmux.sessions())
        record = self._show(source.id)
        self.assertEqual(record["state"], "waiting")
        self.assertIsNone(record["ended_at"])
        self.assertEqual(len(record["chats"]), 2)
        self.assertEqual(record["chats"][0]["id"], "c1")
        self.assertTrue(before <= record["chats"][0]["ended_at"] <= time.time())
        record["chats"][1].pop("started_at")
        self.assertEqual(record["chats"][1], self._pending("rollover", source.cwd, source.id, "c1"))
        op_dir = self.home.chat_ops_dir / op_id
        self.assertTrue((op_dir / "done").is_file())
        self.assertTrue((op_dir / "claim").is_dir())
        self.assertTrue((op_dir / "spec.json").is_file())
        self.assertEqual(self._log(1), [("rollover-finish", "w1 (chat pending)")])
        hook = self.tx(
            ["hook", "session-start"], env={"TX_SESSION_ID": source.id},
            stdin=json.dumps({"session_id": "c9", "transcript_path": "/p/c9.jsonl"}),
        )
        self.assertEqual(hook.code, 0, hook.err)
        self.assertEqual(self._show(source.id)["chats"][1]["id"], "c9")

    def test_t_chat_13_note_missing_seeds_the_bundle_pointer(self):
        source = self._source(state="waiting", env={"K": "V"})
        pane = self._go_live(source)
        op_id = self._rollover_spec(source, pane, str(self.home.history_dir / source.id / "rollover-1.md"))
        result = self.tx(["_chat-op-finish", op_id])
        self.assertEqual(result.code, 0, result.err)
        seed = self._rollover_bundle_seed(source)
        self.assertEqual(
            seed,
            f"Continuing prior work in a fresh chat (rollover). The predecessor bundle is at {self.home.path}/history/{source.id}/c1/ (transcript.jsonl + subagents/ + tool-results/) — read what you need to resume, then continue.",
        )
        dump = self._wait_respawn(source, seed)
        self.assertEqual(dump["argv"][1:], [*PERSONA, SKIP, seed])

    def test_t_chat_13_read_only_source_respawns_inside_the_sandbox(self):
        source = self._source(state="waiting", cmd=RO_SOURCE_CMD, env={"TX_READ_ONLY": "1"})
        pane = self._go_live(source)
        op_id = self._rollover_spec(source, pane, str(self.home.history_dir / source.id / "rollover-1.md"))
        result = self.tx(["_chat-op-finish", op_id])
        self.assertEqual(result.code, 0, result.err)
        seed = self._rollover_bundle_seed(source)
        dump = self._wait_respawn(source, seed)
        self.assertEqual(dump["argv"][1:], [*PERSONA, *RO_BLOCK, seed])
        self.assertNotIn(SKIP, dump["argv"])
        start_command = self._start_command(pane)
        self.assertTrue(start_command.startswith(str(self.fakes.bin_dir / "bwrap") + " "), start_command)
        tokens = shlex.split(start_command)
        inner = tokens[tokens.index("--") + 1:]
        self.assertEqual(inner, ["env", "TX_READ_ONLY=1", f"TX_SESSION_ID={source.id}", "claude", *PERSONA, *RO_BLOCK, seed])

    def test_t_chat_13_long_command_launches_through_a_script(self):
        long_value = "x" * 9000
        source = self._source(state="waiting", env={"K": long_value})
        pane = self._go_live(source)
        op_id = self._rollover_spec(source, pane, str(self.home.history_dir / source.id / "rollover-1.md"))
        result = self.tx(["_chat-op-finish", op_id])
        self.assertEqual(result.code, 0, result.err)
        seed = self._rollover_bundle_seed(source)
        script = self.home.launch_dir / f"{source.id}.sh"
        self.assertEqual(self._start_command(pane), f"/bin/sh {shlex.quote(str(script))}")
        self.assertEqual(script.stat().st_mode & 0o777, 0o700)
        command = f"env K={long_value} TX_SESSION_ID={source.id} " + shlex.join(["claude", *PERSONA, SKIP, seed])
        self.assertEqual(script.read_text(), command + "\n")
        dump = self._wait_respawn(source, seed)
        self.assertEqual(dump["env"]["K"], long_value)

    # ----- T-CHAT-14 _record_seeded_chat idempotent + close_chat ----------------------------

    def test_t_chat_14_seeded_chat_idempotent_and_close_chat(self):
        source_id = str(uuid.uuid4())
        cwd = str(self.git.as_linked_worktree("w1"))
        closed_at = time.time() - 3000
        transcript = self.home.claude_transcript_path(cwd, "c1")
        transcript.parent.mkdir(parents=True)
        transcript.write_text(TRANSCRIPT)
        self.records.llm(
            id=source_id, name="w1", state="waiting", cwd=cwd, cmd=SOURCE_CMD, tags=("a",), env=SOURCE_ENV,
            chats=[
                self._chat(source_id, "c1", cwd, str(transcript), ended_at=closed_at),
                self._chat(source_id, None, cwd, role="rollover", how="rollover", origin_chat="c1"),
            ],
        )
        source = Source(source_id, "w1", cwd, transcript, dict(SOURCE_ENV))
        pane = self._go_live(source)
        op_id = self._rollover_spec(source, pane, str(self.home.history_dir / source.id / "rollover-1.md"))
        result = self.tx(["_chat-op-finish", op_id])
        self.assertEqual(result.code, 0, result.err)
        self._wait_respawn(source, self._rollover_bundle_seed(source))
        record = self._show(source.id)
        self.assertEqual(len(record["chats"]), 2)
        self.assertEqual(record["chats"][0]["ended_at"], closed_at)
        self.assertEqual((record["chats"][1]["id"], record["chats"][1]["role"]), (None, "rollover"))
        self.assertTrue((self.home.chat_ops_dir / op_id / "done").is_file())

    # ----- T-CHAT-15 chat_op_watch lifecycle (FIX — timings via env) ------------------------

    def _watched_handover(self, source: Source, *, worker: str = "w1-handover", artifact_path: str | None = None) -> tuple[str, dict, Path]:
        brief = self.home.history_dir / source.id / f"handover-{worker}.md"
        brief.parent.mkdir(parents=True, exist_ok=True)
        op_id = self._craft_spec(
            source, artifact_path=str(brief) if artifact_path is None else artifact_path,
            worker_name=worker, distiller_name=f"{worker}-distill",
        )
        spawn = self.tx(["spawn", f"{worker}-distill", "--tag", "temporary,handover", "--engine", "claude", "--cwd", source.cwd, "--prompt", "distill"])
        self.assertEqual(spawn.code, 0, spawn.err)
        distiller = self._show(f"{worker}-distill")
        self.fakes.wait_dump("claude", distiller["id"])
        return op_id, distiller, brief

    def _watch(self, op_id: str, env: dict[str, str] = WATCH_ENV) -> subprocess.Popen:
        return self.tx_popen(["_chat-op-watch", op_id], env=env)

    def _assert_about_grace(self, elapsed: float, grace: float = WATCH_GRACE) -> None:
        """`≈GRACE`: at least the grace window, and well short of the timeout path."""
        self.assertGreaterEqual(elapsed, grace)
        self.assertLess(elapsed, grace + WATCH_POLL + WATCH_SLACK)
        self.assertLess(grace + WATCH_POLL + WATCH_SLACK, WATCH_TIMEOUT)

    def _assert_torn_down(self, op_id: str, distiller: dict, worker: str) -> None:
        ended = self._show(distiller["id"])
        self.assertEqual(ended["state"], "exited")
        self.assertIsNotNone(ended["ended_at"])
        self.assertNotIn(distiller["id"], self.tmux.sessions())
        self.assertNotIn(os.path.realpath(distiller["cwd"]), self._worktrees())
        self.assertFalse((self.home.chat_ops_dir / op_id).exists())
        entries = [(line["type"], line["msg"]) for line in self.log_lines()]
        finish = entries.index(("handover-finish", f"{worker} (chat pending)"))
        self.assertIn(("kill", distiller["name"]), entries[finish:])

    @expected_failure_on_python
    def test_t_chat_15_fixed_timeout_path(self):
        source = self._source()
        op_id, distiller, _ = self._watched_handover(source)
        start = time.monotonic()
        result = self.tx(["_chat-op-watch", op_id], env=WATCH_ENV)
        elapsed = time.monotonic() - start
        self.assertEqual(result.code, 0, result.err)
        self.assertTrue(WATCH_TIMEOUT <= elapsed < 2 * WATCH_TIMEOUT, elapsed)
        worker = self._show("w1-handover")
        self.assertEqual(self.fakes.wait_dump("claude", worker["id"])["argv"][-1], self._catch_up_seed(source))
        self._assert_torn_down(op_id, distiller, "w1-handover")

    @expected_failure_on_python
    def test_t_chat_15_fixed_distiller_finishes_within_grace(self):
        # (b) the "distiller" (the test) writes the artifact, then runs the finish itself inside the
        # grace window — the long grace makes that ordering certain; `done` right after the test's
        # finish proves it won the claim, so the watcher had nothing left to re-finish.
        source = self._source()
        op_id, distiller, brief = self._watched_handover(source)
        watcher = self._watch(op_id, WATCH_ENV_LONG_GRACE)
        time.sleep(1.0)
        brief.write_text("# brief\n")
        time.sleep(0.1)
        finish = self.tx(["_chat-op-finish", op_id])
        self.assertEqual(finish.code, 0, finish.err)
        finished_at = time.monotonic()
        self.assertTrue((self.home.chat_ops_dir / op_id / "done").is_file())
        _, err = watcher.communicate(timeout=30)
        returned_after = time.monotonic() - finished_at
        self.assertEqual(watcher.returncode, 0, err)
        self.assertEqual(len(self._named("w1-handover")), 1)
        self._assert_torn_down(op_id, distiller, "w1-handover")
        self.assertLess(returned_after, WATCH_LONG_GRACE + WATCH_POLL + WATCH_SLACK)

    @expected_failure_on_python
    def test_t_chat_15_fixed_artifact_without_finish(self):
        # (c) the artifact appears and nobody finishes: the watcher finishes ≈GRACE later.
        source = self._source()
        op_id, distiller, brief = self._watched_handover(source)
        watcher = self._watch(op_id)
        time.sleep(0.5)
        brief.write_text("# brief\n")
        written_at = time.monotonic()
        _, err = watcher.communicate(timeout=30)
        elapsed = time.monotonic() - written_at
        self.assertEqual(watcher.returncode, 0, err)
        workers = self._named("w1-handover")
        self.assertEqual(len(workers), 1)
        self.assertEqual(self.fakes.wait_dump("claude", workers[0]["id"])["argv"][-1], self._brief_seed(source, str(brief)))
        self._assert_torn_down(op_id, distiller, "w1-handover")
        self._assert_about_grace(elapsed)

    @expected_failure_on_python
    def test_t_chat_15_fixed_edges_distiller_gone_and_no_artifact(self):
        source = self._source()
        op_id, distiller, brief = self._watched_handover(source, worker="w1-a")
        kill = self.tx(["kill", distiller["name"]])
        self.assertEqual(kill.code, 0, kill.err)
        brief.write_text("# brief\n")
        result = self.tx(["_chat-op-watch", op_id], env=WATCH_ENV)
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(len(self._named("w1-a")), 1)
        self.assertFalse((self.home.chat_ops_dir / op_id).exists())
        # spec `artifact_path:""` → no artifact wait: finish after GRACE, not TIMEOUT
        op_id, distiller, _ = self._watched_handover(source, worker="w1-b", artifact_path="")
        start = time.monotonic()
        result = self.tx(["_chat-op-watch", op_id], env=WATCH_ENV)
        elapsed = time.monotonic() - start
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(len(self._named("w1-b")), 1)
        self._assert_torn_down(op_id, distiller, "w1-b")
        self._assert_about_grace(elapsed)

    @expected_failure_on_python
    def test_t_chat_15_fixed_non_linked_distiller_cwd_is_left_untouched(self):
        # Teardown removes only a LINKED worktree; a distiller whose cwd is a plain directory keeps
        # it (the record is still killed and the spec dir removed).
        source = self._source()
        plain = self.root / "plain-distill-cwd"
        plain.mkdir()
        (plain / "keep.txt").write_text("keep\n")
        distiller_id = self.records.llm(
            name="w1-plain-distill", state="working", cwd=str(plain), tags=("temporary", "handover"),
        )
        self.live(distiller_id)
        brief = self.home.history_dir / source.id / "handover-w1-plain.md"
        brief.parent.mkdir(parents=True, exist_ok=True)
        op_id = self._craft_spec(
            source, artifact_path=str(brief), worker_name="w1-plain", distiller_name="w1-plain-distill",
        )
        brief.write_text("# brief\n")
        result = self.tx(["_chat-op-watch", op_id], env=WATCH_ENV)
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(len(self._named("w1-plain")), 1)
        ended = self._show(distiller_id)
        self.assertEqual(ended["state"], "exited")
        self.assertIsNotNone(ended["ended_at"])
        self.assertNotIn(distiller_id, self.tmux.sessions())
        self.assertFalse((self.home.chat_ops_dir / op_id).exists())
        self.assertTrue(plain.is_dir())
        self.assertEqual((plain / "keep.txt").read_text(), "keep\n")

    # ----- T-CHAT-16 detached finish survives its caller's pane -----------------------------

    def test_t_chat_16_detached_finish_survives_callers_pane(self):
        source = self._source(state="waiting")
        pane = self._shell_pane(source)
        holder = self._hold_ingest_lock(source)
        self.tmux.run("send-keys", "-t", pane, f"{TX_BIN} rollover --self-catch-up; echo rc=$?", "Enter", check=True)
        self.wait_until(lambda: "rc=0" in self._screen(pane))
        screen = self._screen(pane)
        self.assertIn("Rollover scheduled (self-catch-up); the same session rotates onto a fresh chat when ready", screen)
        op_id = self._only_op()
        self._release(holder)
        seed = self._rollover_bundle_seed(source)
        dump = self._wait_respawn(source, seed)
        self.assertEqual(dump["env"]["TX_SESSION_ID"], source.id)
        self.assertEqual(self.tmux.display(source.id, "#{pane_id}"), pane)
        self.wait_until(lambda: not (self.home.chat_ops_dir / op_id).exists())
        record = self._show(source.id)
        self.assertEqual(len(record["chats"]), 2)
        self.assertIsNotNone(record["chats"][0]["ended_at"])
        record["chats"][1].pop("started_at")
        self.assertEqual(record["chats"][1], self._pending("rollover", source.cwd, source.id, "c1"))
        self.assertEqual(self._log(1), [("rollover-finish", "w1 (chat pending)")])
        scrollback = self._screen(pane, scrollback=True)
        self.assertNotIn("Rollover scheduled", scrollback)
        self.assertNotIn("Traceback", scrollback)
        self.assertEqual([line for line in scrollback.splitlines() if line.startswith("tx ")], [])

    @expected_failure_on_python
    def test_t_chat_16_fixed_handover_watchdog_outlives_killed_pane(self):
        source = self._source(state="waiting")
        pane = self._shell_pane(source, WATCH_ENV)
        self.tmux.run("send-keys", "-t", pane, f"{TX_BIN} handover w1 t; echo rc=$?", "Enter", check=True)
        self.wait_until(lambda: "rc=0" in self._screen(pane))
        op_id = self._only_op()
        self._watch_op(op_id)
        kill = self.tx(["kill", "w1"])
        self.assertEqual(kill.code, 0, kill.err)
        self.wait_until(lambda: not (self.home.chat_ops_dir / op_id).exists(), timeout=WATCH_TIMEOUT + 4 * WATCH_GRACE + 15)
        self.assertEqual(len(self._named("w1-handover")), 1)
        self.assertEqual(self._show("w1-handover-distill")["state"], "exited")

    # ----- T-CHAT-17 resume happy path ------------------------------------------------------

    def test_t_chat_17_resume_happy_path(self):
        source = self._source()
        source_bytes = self.records.path(source.id).read_bytes()
        worktrees_before = self._worktrees()
        before_ids = self._record_ids()
        before = time.time()
        result = self.tx(["resume", "w1"])
        self.assertEqual(result.code, 0, result.err)
        new = self._new_record(before_ids)
        self.assertEqual(result.out, f"Resumed 'w1' as 'w1' (chat c1, cwd={new['cwd']})\n")
        self.assertEqual(result.err, "")
        self.assertEqual(new["name"], "w1")
        self.assertEqual(new["cwd"], source.cwd)
        self.assertEqual(self._worktrees(), worktrees_before)
        self.assertEqual(new["tags"], ["a"])
        self.assertEqual(new["engine"], "claude")
        self.assertEqual(new["parent"], source.id)
        self.assertEqual(new["env"], {"TX_SKILLS": "", "K": "V", "TX_REQUIRE_WORKTREE": "1"})
        expected_argv = ["--resume", "c1", *PERSONA, SKIP]
        self.assertEqual(new["cmd"], shlex.join(["claude", *expected_argv]))
        self.assertEqual(len(new["chats"]), 1)
        started_at = new["chats"][0].pop("started_at")
        self.assertTrue(before <= started_at <= time.time())
        self.assertEqual(new["chats"][0], {
            "id": "c1", "role": "original", "cwd": new["cwd"],
            "transcript_path": str(self.home.claude_transcript_path(new["cwd"], "c1")),
            "origin": {"how": "resume", "session_id": new["id"], "chat_id": "c1"},
            "bundle_path": f"{self.home.path}/history/{new['id']}/c1",
            "ended_at": None, "summary": "", "engine": "claude",
        })
        dump = self.fakes.wait_dump("claude", new["id"])
        self.assertEqual(dump["argv"][1:], expected_argv)
        self.assertEqual(dump["env"]["TX_SESSION_ID"], new["id"])
        self.assertEqual(self._log(1), [("spawn", f"w1 [llm] {new['cwd']}")])
        self.assertEqual(self.records.path(source.id).read_bytes(), source_bytes)

    def test_t_chat_17_resume_as_new_name_copies_the_transcript(self):
        source = self._source()
        before_ids = self._record_ids()
        result = self.tx(["resume", "w1", "--as", "w2"])
        self.assertEqual(result.code, 0, result.err)
        new = self._new_record(before_ids)
        self.assertEqual(new["name"], "w2")
        self._assert_worker_cwd(new["cwd"], "repo--w2")
        self.assertEqual(result.out, f"Resumed 'w1' as 'w2' (chat c1, cwd={new['cwd']})\n")
        self.assertEqual(self.home.claude_transcript_path(new["cwd"], "c1").read_text(), TRANSCRIPT)
        self.fakes.wait_dump("claude", new["id"])

    def test_t_chat_17_resume_codex_source(self):
        source = self._codex_source()
        before_ids = self._record_ids()
        result = self.tx(["resume", "cx"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.err, "")
        new = self._new_record(before_ids)
        self.assertEqual(new["engine"], "codex")
        dump = self.fakes.wait_dump("codex", new["id"])
        self.assertEqual(dump["argv"][1:], ["resume", "r1", *CODEX_DEFAULTS, *YOLO])
        self.assertEqual(new["chats"][0]["transcript_path"], str(source.transcript))

    def test_t_chat_17_resume_prefers_the_last_open_chat(self):
        source_id = str(uuid.uuid4())
        cwd = str(self.git.as_linked_worktree("w1"))
        self.records.llm(
            id=source_id, name="w1", state="exited", cwd=cwd, cmd=SOURCE_CMD, tags=("a",), env=SOURCE_ENV,
            chats=[self._chat(source_id, "a", cwd), self._chat(source_id, "b", cwd, ended_at=time.time() - 10)],
        )
        before_ids = self._record_ids()
        result = self.tx(["resume", "w1"])
        self.assertEqual(result.code, 0, result.err)
        new = self._new_record(before_ids)
        self.assertEqual(self.fakes.wait_dump("claude", new["id"])["argv"][1:3], ["--resume", "a"])

    # ----- T-CHAT-18 resume refusals --------------------------------------------------------

    def _refused(self, argv: list[str], message: str | None) -> str:
        before = (self._record_ids(), self._worktrees(), self.tmux.sessions())
        result = self.tx(argv)
        self.assertEqual(result.code, 1, result.err)
        self.assertEqual(result.out, "")
        if message is not None:
            self.assertEqual(result.err, message)
        self.assertEqual((self._record_ids(), self._worktrees(), self.tmux.sessions()), before)
        return result.err

    def test_t_chat_18_parity_refusals(self):
        source = self._source()
        self._source(name="nochat", chats=[])
        self._refused(["resume", "nope"], "tx resume: no record for 'nope'\n")
        self._refused(["resume", "nochat"], "tx resume: 'nochat' has no chat to resume — use `tx spawn` for a fresh session\n")
        self.tmux.new_session("w1", "sleep 1000", client_env=self.tx_env())
        self._refused(["resume", "w1"], "tx resume: a live session named 'w1' already exists — pass --as <new-name>\n")
        self.tmux.kill_session("w1")
        live = self._source(name="lv", state="waiting")
        self._go_live(live)
        error = self._refused(["resume", "lv"], None)
        self.assertTrue(error.startswith("tx resume: "), error)
        self.assertNotIn("repo--lv-2", "\n".join(self._worktrees()))
        before_ids = self._record_ids()
        result = self.tx(["resume", "lv", "--as", "lv2"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self._new_record(before_ids)["name"], "lv2")
        gone = self._source(name="gone")
        shutil.rmtree(gone.cwd)
        self._refused(["resume", "gone"], f"tx resume: cwd '{gone.cwd}' does not exist — pass --cwd <dir> (C8)\n")
        self._refused(["resume", "gone", "--cwd", "/nope"], "tx resume: cwd '/nope' does not exist — '/nope' is not a directory (C8)\n")
        missing = self._source(name="missing", transcript=False, chat_id="c1c1c1c1-0000-4000-8000-000000000001")
        before_ids = self._record_ids()
        result = self.tx(["resume", "missing"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.err, f"tx resume: warning — transcript for chat c1c1c1c1 not found under {missing.cwd}; claude --resume may start a fresh conversation\n")
        self._new_record(before_ids)

    @expected_failure_on_python
    def test_t_chat_18_fixed_live_record_clash_hint(self):
        source = self._source(state="waiting")
        self._go_live(source)
        self._refused(["resume", "w1"], "tx resume: a live session named 'w1' already exists — pass --as <new-name>\n")
        self.assertNotIn("repo--w1-2", "\n".join(self._worktrees()))
        before_ids = self._record_ids()
        result = self.tx(["resume", "w1", "--as", "w2"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self._new_record(before_ids)["name"], "w2")

    # ----- T-CHAT-19 read-only propagation --------------------------------------------------

    def test_t_chat_19_read_only_propagation_resume_fork_handover(self):
        source = self._source(cmd=RO_SOURCE_CMD, env={"TX_READ_ONLY": "1"})
        before_ids = self._record_ids()
        result = self.tx(["resume", "w1", "--as", "w2"])
        self.assertEqual(result.code, 0, result.err)
        resumed = self._new_record(before_ids)
        dump = self.fakes.wait_dump("claude", resumed["id"])
        self.assertEqual(dump["argv"][1:], ["--resume", "c1", *PERSONA, *RO_BLOCK])
        self.assertEqual(resumed["env"], {"TX_READ_ONLY": "1"})
        self.assertEqual(dump["env"]["TX_READ_ONLY"], "1")
        result = self.tx(["fork", "w1"])
        self.assertEqual(result.code, 0, result.err)
        fork = self._show("w1-fork")
        dump = self.fakes.wait_dump("claude", fork["id"])
        self.assertEqual(dump["argv"][1:], ["--resume", "c1", "--fork-session", *PERSONA, SKIP])
        self.assertEqual(fork["env"], {"TX_REQUIRE_WORKTREE": "1"})
        self.assertNotIn("TX_READ_ONLY", dump["env"])
        result = self.tx(["handover", "w1", "t", "--self-catch-up"])
        self.assertEqual(result.code, 0, result.err)
        worker = self._show("w1-handover")
        dump = self.fakes.wait_dump("claude", worker["id"])
        self.assertEqual(dump["argv"][1:], [*PERSONA, SKIP, self._catch_up_seed(source)])
        self.assertEqual(worker["env"], {"TX_REQUIRE_WORKTREE": "1"})
        result = self.tx(["fork", "w1", "ro-fork", "--read-only"])
        self.assertEqual(result.code, 0, result.err)
        ro_fork = self._show("ro-fork")
        dump = self.fakes.wait_dump("claude", ro_fork["id"])
        self.assertEqual(dump["argv"][1:], ["--resume", "c1", "--fork-session", *PERSONA, *RO_BLOCK])
        self.assertEqual(ro_fork["env"], {"TX_READ_ONLY": "1"})
        result = self.tx(["handover", "w1", "t", "ro-hand", "--self-catch-up", "--read-only"])
        self.assertEqual(result.code, 0, result.err)
        ro_worker = self._show("ro-hand")
        dump = self.fakes.wait_dump("claude", ro_worker["id"])
        self.assertEqual(dump["argv"][1:], [*PERSONA, *RO_BLOCK, self._catch_up_seed(source)])
        self.assertEqual(ro_worker["env"], {"TX_READ_ONLY": "1"})

    def test_t_chat_19_read_only_propagation_rollover(self):
        source = self._source(state="waiting", cmd=RO_SOURCE_CMD, env={"TX_READ_ONLY": "1"})
        pane = self._go_live(source)
        result = self.tx(["rollover", "w1", "--self-catch-up"])
        self.assertEqual(result.code, 0, result.err)
        seed = self._rollover_bundle_seed(source)
        dump = self._wait_respawn(source, seed)
        self.assertEqual(dump["argv"][1:], [*PERSONA, *RO_BLOCK, seed])
        start_command = self._start_command(pane)
        self.assertTrue(start_command.startswith(str(self.fakes.bin_dir / "bwrap") + " "), start_command)
        tokens = shlex.split(start_command)
        self.assertEqual(tokens[tokens.index("--") + 1:], ["env", "TX_READ_ONLY=1", f"TX_SESSION_ID={source.id}", "claude", *PERSONA, *RO_BLOCK, seed])

    # ----- T-CHAT-21 chat-op verbs ----------------------------------------------------------

    def test_t_chat_21_parity_no_arg_exit_2_and_missing_spec_has_no_side_effects(self):
        for verb in ("_chat-op-finish", "_chat-op-watch"):
            result = self.tx([verb])
            self.assertEqual(result.code, 2)
            self.assertTrue(result.err.startswith(f"usage: tx {verb}"), result.err)
            self.assertIn(f"tx {verb}: error: the following arguments are required: op_id", result.err)
        for verb in ("_chat-op-finish", "_chat-op-watch"):
            result = self.tx([verb, "no-such-op"])
            self.assertEqual(result.code, 1)
            self.assertFalse(self.home.chat_ops_dir.exists())
            self.assertEqual(self._record_ids(), [])
            self.assertEqual(self.fakes.dumps("claude"), [])

    @expected_failure_on_python
    def test_t_chat_21_fixed_missing_spec_exit_1_no_traceback(self):
        for verb in ("_chat-op-finish", "_chat-op-watch"):
            result = self.tx([verb, "no-such-op"])
            self.assertEqual(result.code, 1)
            self.assertEqual(len(result.err.splitlines()), 1, result.err)
            self.assertTrue(result.err.startswith(f"tx {verb}: "), result.err)
            self.assertIn("no-such-op", result.err)
            self.assertNotIn("Traceback", result.err)
