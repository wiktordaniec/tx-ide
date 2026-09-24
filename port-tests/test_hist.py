"""HIST — history ingest + resolver (spec section 03, T-HIST-01 … T-HIST-10).

Every case drives `tx hook ingest <id>` / `tx archive <name>` / `tx chat ls <name>` over crafted
schema-6 records and transcripts under the engine homes, and asserts on `$TX_IDE_HOME/history/`,
the record file, stdout/stderr and `log.jsonl`. No clock injection (D8): "after a >1 s gap"
checks backdate the file under test with `os.utime` and assert its mtime did not move.
"""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from pathlib import Path

from txkit import TxCase, expected_failure_on_python

TRANSCRIPT = b'{"type":"user","text":"one"}\n{"type":"assistant","text":"two"}\n{"type":"user","text":"three"}\n'
ROLLOUT = b'{"type":"session_meta","payload":{"id":"r1","cwd":"/w"}}\n{"type":"response_item"}\n'
KIB_200 = bytes(range(256)) * 800  # 204800 bytes — wider than the 65536-byte trailing prefix window
PREFIX_CHECK_BYTES = 65536
CHAT_ID = "c1c1c1c1-0000-4000-8000-000000000001"
FORK_SOURCE_CHAT_ID = "d2d2d2d2-0000-4000-8000-000000000002"
# A crafted `attached_to` entry (the frozen `Location` shape) — seeded where the Then says `[]`.
LOCATION = {"host": "Views", "window_index": "1", "window_name": "work", "pane_id": "%99", "pane_index": "0"}


def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def flipped_at(data: bytes, index: int) -> bytes:
    mutable = bytearray(data)
    mutable[index] ^= 0xFF
    return bytes(mutable)


def differing_offsets(left: bytes, right: bytes) -> list[int]:
    return [index for index, (a, b) in enumerate(zip(left, right)) if a != b]


class TestHist(TxCase):
    # ----- fixtures ------------------------------------------------------------------------

    def workdir(self, name: str = "w") -> str:
        directory = self.root / name
        directory.mkdir(exist_ok=True)
        return str(directory)

    def claude_record(
        self,
        *,
        name: str = "w1",
        chat_id: str = "c1",
        cwd: str | None = None,
        state: str = "idle",
        ended_at: float | None = None,
        bundle_path: str | None = None,
        transcript: bytes | None = TRANSCRIPT,
    ) -> str:
        """A claude llm record with ONE captured chat; the transcript (when given) is written at
        the fast-path location `$CLAUDE_CONFIG_DIR/projects/<munge(realpath cwd)>/<chat>.jsonl`."""
        cwd = self.workdir() if cwd is None else cwd
        session_id = str(uuid.uuid4())
        self.records.llm(
            id=session_id,
            name=name,
            state=state,
            ended_at=ended_at,
            cwd=cwd,
            chats=[
                self.records.chat_ref(
                    session_id=session_id, id=chat_id, cwd=cwd, bundle_path=bundle_path
                )
            ],
        )
        if transcript is not None:
            write_bytes(self.home.claude_transcript_path(cwd, chat_id), transcript)
        return session_id

    def codex_record(
        self, *, name: str = "x1", chat_id: str = "r1", cwd: str = "/nope/gone", rollout: bytes | None = ROLLOUT
    ) -> str:
        """A codex llm record with ONE captured chat; the rollout (when given) lands under
        `$CODEX_HOME/sessions/2026/09/23/rollout-x-<chat>.jsonl`."""
        session_id = str(uuid.uuid4())
        self.records.llm(
            id=session_id,
            name=name,
            engine="codex",
            chats=[
                self.records.chat_ref(
                    session_id=session_id, id=chat_id, cwd=cwd, engine="codex"
                )
            ],
        )
        if rollout is not None:
            write_bytes(self.rollout_path(chat_id), rollout)
        return session_id

    def rollout_path(self, chat_id: str, date: str = "2026/09/23") -> Path:
        return self.home.codex_home / "sessions" / date / f"rollout-x-{chat_id}.jsonl"

    def projects_transcript(self, munged_dir: str, chat_id: str, content: bytes) -> Path:
        path = self.home.claude_config_dir / "projects" / munged_dir / f"{chat_id}.jsonl"
        write_bytes(path, content)
        return path

    def bundle_dir(self, session_id: str, chat_id: str) -> Path:
        return self.home.history_dir / session_id / chat_id

    def bundle_transcript(self, session_id: str, chat_id: str) -> Path:
        return self.bundle_dir(session_id, chat_id) / "transcript.jsonl"

    def bundle_entries(self, session_id: str, chat_id: str) -> list[str]:
        return sorted(entry.name for entry in self.bundle_dir(session_id, chat_id).iterdir())

    def chat(self, session_id: str, index: int = 0) -> dict:
        return self.records.load(session_id)["chats"][index]

    def ingest(self, session_id: str) -> None:
        result = self.tx(["hook", "ingest", session_id])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, "")

    def hold_lock(self, lock_path: Path, seconds: float) -> float:
        """Hold `flock -x <lock>` in a background process for `seconds`; returns the monotonic
        time the holder was launched (it acquires no earlier than that, so it releases no
        earlier than `launched + seconds`). Returns once the lock is observably held."""
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        launched = time.monotonic()
        holder = subprocess.Popen(["flock", "-x", str(lock_path), "sleep", str(seconds)])
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        self.wait_until(
            lambda: subprocess.run(["flock", "-n", str(lock_path), "true"]).returncode != 0
        )
        return launched

    def hold_lock_until_released(self, lock_path: Path) -> subprocess.Popen:
        """Hold `flock -x <lock>` until `release_lock` — no wall clock in the assertion: "returned
        while the lock was held" is `holder.poll() is None`. The holder runs in its own process
        group so the `sleep` that inherits the lock dies with it."""
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        holder = subprocess.Popen(["flock", "-x", str(lock_path), "sleep", "600"], start_new_session=True)
        self.addCleanup(self.release_lock, holder)
        self.wait_until(
            lambda: subprocess.run(["flock", "-n", str(lock_path), "true"]).returncode != 0
        )
        return holder

    @staticmethod
    def release_lock(holder: subprocess.Popen) -> None:
        if holder.poll() is None:
            os.killpg(holder.pid, 9)
        holder.wait()

    # ----- T-HIST-01 transcript resolution fast path ---------------------------------------

    def test_t_hist_01_fast_path_resolves_claude_transcript(self):
        cwd = self.workdir()
        session_id = self.claude_record(cwd=cwd)
        self.ingest(session_id)
        self.assertEqual(self.bundle_transcript(session_id, "c1").read_bytes(), TRANSCRIPT)

    def test_t_hist_01_codex_rollout_resolved_regardless_of_cwd(self):
        session_id = self.codex_record(cwd="/nope/gone")
        self.ingest(session_id)
        self.assertEqual(self.bundle_transcript(session_id, "r1").read_bytes(), ROLLOUT)

    # ----- T-HIST-02 glob fallback prefers the cwd munge ----------------------------------

    def fallback_record(self, chat_cwd: str) -> str:
        session_id = str(uuid.uuid4())
        self.records.llm(
            id=session_id,
            name=f"w-{session_id[:4]}",
            chats=[self.records.chat_ref(session_id=session_id, id="c1", cwd=chat_cwd)],
        )
        return session_id

    def test_t_hist_02_parity_two_matches_cwd_munge_then_sorted_first(self):
        self.projects_transcript("-a", "c1", b"A")
        self.projects_transcript("-b", "c1", b"B")
        by_cwd = self.fallback_record("/b")
        by_missing_cwd = self.fallback_record("/zzz")
        by_empty_cwd = self.fallback_record("")
        for session_id in (by_cwd, by_missing_cwd, by_empty_cwd):
            self.ingest(session_id)
        self.assertEqual(self.bundle_transcript(by_cwd, "c1").read_bytes(), b"B")
        self.assertEqual(self.bundle_transcript(by_missing_cwd, "c1").read_bytes(), b"A")
        self.assertEqual(self.bundle_transcript(by_empty_cwd, "c1").read_bytes(), b"A")

    def test_t_hist_02_parity_single_match_anywhere_wins(self):
        self.projects_transcript("-elsewhere", "c1", b"A")
        session_id = self.fallback_record("/zzz")
        self.ingest(session_id)
        self.assertEqual(self.bundle_transcript(session_id, "c1").read_bytes(), b"A")

    def test_t_hist_02_parity_no_match_leaves_no_bundle(self):
        session_id = self.fallback_record("/zzz")
        self.ingest(session_id)
        self.assertFalse((self.home.history_dir / session_id).exists())
        self.assertIsNone(self.chat(session_id)["bundle_path"])

    @expected_failure_on_python
    def test_t_hist_02_fixed_codex_fallback_is_per_engine(self):
        # Q7 FIX: a codex chat with no rollout is NOT resolved through the Claude projects root.
        session_id = self.codex_record(cwd="/w", rollout=None)
        self.projects_transcript("-w", "r1", b"stray")
        self.ingest(session_id)
        self.assertFalse(self.bundle_dir(session_id, "r1").exists())
        self.assertIsNone(self.chat(session_id)["bundle_path"])

    # ----- T-HIST-03 ingest basics ---------------------------------------------------------

    def test_t_hist_03_ingest_basics(self):
        cwd = self.workdir()
        session_id = str(uuid.uuid4())
        pending = self.records.chat_ref(session_id=session_id, cwd=cwd)
        captured = self.records.chat_ref(session_id=session_id, id="c1", cwd=cwd)
        self.records.llm(id=session_id, name="w1", cwd=cwd, chats=[pending, captured])
        write_bytes(self.home.claude_transcript_path(cwd, "c1"), TRANSCRIPT)

        result = self.tx(["hook", "ingest", session_id])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, "")
        bundle = self.bundle_dir(session_id, "c1")
        self.assertEqual((bundle / "transcript.jsonl").read_bytes(), TRANSCRIPT)
        self.assertTrue((bundle / ".ingest.lock").exists())
        chats = self.records.load(session_id)["chats"]
        self.assertEqual(chats[1]["bundle_path"], str(bundle))
        self.assertIsNone(chats[0]["id"])
        self.assertIsNone(chats[0]["bundle_path"])
        listing = self.tx(["chat", "ls", "w1"])
        self.assertEqual(listing.code, 0, listing.err)
        self.assertTrue(listing.lines[2].endswith(f" ago   {bundle}"), listing.out)

    def test_t_hist_03_unknown_session_id_is_a_no_op(self):
        result = self.tx(["hook", "ingest", str(uuid.uuid4())])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, "")
        self.assertEqual(list(self.home.history_dir.iterdir()), [])

    def test_t_hist_03_chat_without_transcript_is_skipped(self):
        session_id = self.claude_record(transcript=None)
        self.ingest(session_id)
        self.assertFalse((self.home.history_dir / session_id).exists())
        self.assertIsNone(self.chat(session_id)["bundle_path"])

    # ----- T-HIST-04 append-by-offset ------------------------------------------------------

    def test_t_hist_04_append_by_offset(self):
        cwd = self.workdir()
        session_id = self.claude_record(cwd=cwd, transcript=KIB_200)
        source = self.home.claude_transcript_path(cwd, "c1")
        bundle = self.bundle_transcript(session_id, "c1")
        self.ingest(session_id)
        self.assertEqual(bundle.read_bytes(), KIB_200)
        inode = bundle.stat().st_ino

        flipped = flipped_at(KIB_200, 10)  # outside the trailing 64 KiB check window
        bundle.write_bytes(flipped)
        tail = b"T" * 50
        with source.open("ab") as handle:
            handle.write(tail)
        self.ingest(session_id)

        self.assertEqual(bundle.stat().st_ino, inode)
        self.assertEqual(bundle.read_bytes(), flipped + tail)
        self.assertEqual(differing_offsets(bundle.read_bytes(), source.read_bytes()), [10])

    def test_t_hist_04_current_copy_is_not_rewritten(self):
        session_id = self.claude_record(transcript=KIB_200)
        bundle = self.bundle_transcript(session_id, "c1")
        self.ingest(session_id)
        past = time.time() - 100
        os.utime(bundle, (past, past))
        self.ingest(session_id)
        self.assertEqual(bundle.stat().st_mtime, past)
        self.assertEqual(bundle.read_bytes(), KIB_200)

    # ----- T-HIST-05 shrink / prefix mismatch → full recopy -------------------------------

    def ingested_200k(self) -> tuple[str, Path, Path]:
        cwd = self.workdir()
        session_id = self.claude_record(cwd=cwd, transcript=KIB_200)
        self.ingest(session_id)
        return (
            session_id,
            self.home.claude_transcript_path(cwd, "c1"),
            self.bundle_transcript(session_id, "c1"),
        )

    def test_t_hist_05_shrunk_source_is_recopied(self):
        session_id, source, bundle = self.ingested_200k()
        source.write_bytes(b"x" * 80)
        self.ingest(session_id)
        self.assertEqual(bundle.read_bytes(), b"x" * 80)

    def test_t_hist_05_mismatch_inside_trailing_window_is_recopied(self):
        session_id, source, bundle = self.ingested_200k()
        modified = flipped_at(KIB_200, len(KIB_200) - 100)
        source.write_bytes(modified)
        self.ingest(session_id)
        self.assertEqual(bundle.read_bytes(), modified)

    def test_t_hist_05_parity_divergence_outside_window_is_spliced(self):
        # Q8 PARITY: only the trailing 65536 bytes are verified, so an early divergence in a
        # longer source is appended onto, not detected.
        session_id, source, bundle = self.ingested_200k()
        modified = flipped_at(KIB_200, 10) + b"ABCD"
        source.write_bytes(modified)
        self.ingest(session_id)
        self.assertEqual(bundle.stat().st_size, 204804)
        self.assertEqual(bundle.read_bytes(), KIB_200 + b"ABCD")
        self.assertEqual(differing_offsets(bundle.read_bytes(), modified), [10])

    def test_t_hist_05_empty_bundle_is_a_prefix_and_gets_the_full_copy(self):
        session_id, source, bundle = self.ingested_200k()
        bundle.write_bytes(b"")
        self.ingest(session_id)
        self.assertEqual(bundle.read_bytes(), KIB_200)

    # ----- T-HIST-06 sidecars copy-if-absent (claude only) --------------------------------

    def test_t_hist_06_sidecars_copied_if_absent(self):
        cwd = self.workdir()
        session_id = self.claude_record(cwd=cwd)
        sidecar = self.home.claude_transcript_path(cwd, "c1").parent / "c1"
        write_bytes(sidecar / "tool-results" / "a.txt", b"OLD")
        write_bytes(sidecar / "subagents" / "s.jsonl", b'{"s":1}\n')
        bundle = self.bundle_dir(session_id, "c1")

        self.ingest(session_id)
        self.assertEqual((bundle / "tool-results" / "a.txt").read_bytes(), b"OLD")
        self.assertEqual((bundle / "subagents" / "s.jsonl").read_bytes(), b'{"s":1}\n')

        (sidecar / "tool-results" / "a.txt").write_bytes(b"NEW")
        write_bytes(sidecar / "tool-results" / "b.txt", b"B")
        self.ingest(session_id)
        self.assertEqual((bundle / "tool-results" / "a.txt").read_bytes(), b"OLD")
        self.assertEqual((bundle / "tool-results" / "b.txt").read_bytes(), b"B")

    def test_t_hist_06_no_sidecar_dir_means_transcript_and_lock_only(self):
        session_id = self.claude_record()
        self.ingest(session_id)
        self.assertEqual(self.bundle_entries(session_id, "c1"), [".ingest.lock", "transcript.jsonl"])

    def test_t_hist_06_codex_bundle_has_no_sidecars(self):
        session_id = self.codex_record()
        write_bytes(self.rollout_path("r1").parent / "r1" / "tool-results" / "x.txt", b"x")
        self.ingest(session_id)
        self.assertEqual(self.bundle_entries(session_id, "r1"), [".ingest.lock", "transcript.jsonl"])

    def test_t_hist_06_sidecar_taken_relative_to_the_resolved_transcript(self):
        session_id = self.fallback_record("/nope")
        transcript = self.projects_transcript("-elsewhere", "c1", TRANSCRIPT)
        write_bytes(transcript.parent / "c1" / "tool-results" / "a.txt", b"A")
        self.ingest(session_id)
        bundle = self.bundle_dir(session_id, "c1")
        self.assertEqual((bundle / "transcript.jsonl").read_bytes(), TRANSCRIPT)
        self.assertEqual((bundle / "tool-results" / "a.txt").read_bytes(), b"A")

    # ----- T-HIST-07 flock coalescing vs wait ----------------------------------------------

    def test_t_hist_07_hook_ingest_coalesces_while_archive_waits(self):
        cwd = self.workdir()
        session_id = self.claude_record(name="w1", cwd=cwd)
        source = self.home.claude_transcript_path(cwd, "c1")
        bundle_dir = self.bundle_dir(session_id, "c1")
        bundle = bundle_dir / "transcript.jsonl"
        self.ingest(session_id)
        # Clear the stamp so the coalescing path's "dir returned → stamped" is observable.
        record = self.records.load(session_id)
        record["chats"][0]["bundle_path"] = None
        self.records.write(record)

        holder = self.hold_lock_until_released(bundle_dir / ".ingest.lock")
        grown = TRANSCRIPT + b'{"type":"assistant","text":"four"}\n'
        source.write_bytes(grown)

        self.ingest(session_id)
        self.assertIsNone(holder.poll())  # returned while the lock was still held (coalesced)
        self.assertEqual(bundle.read_bytes(), TRANSCRIPT)  # NOT updated (≠ source)
        self.assertEqual(self.chat(session_id)["bundle_path"], str(bundle_dir))

        archive = self.tx_popen(["archive", "w1"])
        with self.assertRaises(subprocess.TimeoutExpired):
            archive.wait(timeout=1.0)  # still blocked on the held lock
        self.assertEqual(bundle.read_bytes(), TRANSCRIPT)
        self.release_lock(holder)
        out, err = archive.communicate(timeout=60)
        self.assertEqual(archive.returncode, 0, err)
        self.assertEqual(bundle.read_bytes(), grown)
        self.assertEqual(out, "Archived 'w1' (ingested 1 chat bundle(s))\n")

    # ----- T-HIST-08 stamp only on change, on a fresh reload ------------------------------

    def test_t_hist_08_no_save_when_bundle_path_is_unchanged(self):
        session_id = self.claude_record()
        self.ingest(session_id)
        record_path = self.records.path(session_id)
        past = time.time() - 100
        os.utime(record_path, (past, past))
        self.ingest(session_id)
        self.assertEqual(record_path.stat().st_mtime, past)

    def test_t_hist_08_fresh_reload_before_stamp_preserves_a_rename(self):
        cwd = self.workdir()
        session_id = self.claude_record(name="w1", cwd=cwd)
        bundle_dir = self.bundle_dir(session_id, "c1")
        holder = self.hold_lock_until_released(bundle_dir / ".ingest.lock")

        archive = self.tx_popen(["archive", "w1"])
        # archive() saves ARCHIVED first, then the forced ingest blocks on the held lock.
        self.wait_until(lambda: self.records.load(session_id)["state"] == "archived")
        record = self.records.load(session_id)
        self.assertIsNone(record["chats"][0]["bundle_path"])
        record["name"] = "renamed"
        self.records.write(record)
        self.assertIsNone(holder.poll())  # rewritten while the lock was still held
        self.assertIsNone(archive.poll())  # … and archive was still blocked on it
        self.release_lock(holder)

        out, err = archive.communicate(timeout=60)
        self.assertEqual(archive.returncode, 0, err)
        self.assertEqual(out, "Archived 'w1' (ingested 1 chat bundle(s))\n")
        final = self.records.load(session_id)
        self.assertEqual(final["name"], "renamed")
        self.assertEqual(final["chats"][0]["bundle_path"], str(bundle_dir))
        self.assertEqual((bundle_dir / "transcript.jsonl").read_bytes(), TRANSCRIPT)

    # ----- T-HIST-09 tx archive forces full ingest + prints count -------------------------

    def test_t_hist_09_archive_forces_full_ingest_and_prints_count(self):
        cwd_a, cwd_b = self.workdir("a"), self.workdir("b")
        session_id = str(uuid.uuid4())
        self.records.llm(
            id=session_id,
            name="w1",
            cwd=cwd_a,
            attached_to=(LOCATION,),
            chats=[
                self.records.chat_ref(session_id=session_id, id="c1", cwd=cwd_a),
                self.records.chat_ref(session_id=session_id, id="c2", cwd=cwd_b, role="rollover", how="rollover", chat_id="c1"),
            ],
        )
        write_bytes(self.home.claude_transcript_path(cwd_a, "c1"), TRANSCRIPT)
        write_bytes(self.home.claude_transcript_path(cwd_b, "c2"), ROLLOUT)
        self.tmux.new_session(session_id, "sleep 1000", tx_id=session_id)
        # An ingest already in flight for c1: the forced mirror must wait for it, not skip.
        self.hold_lock(self.bundle_dir(session_id, "c1") / ".ingest.lock", 1.0)

        result = self.tx(["archive", "w1"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, "Archived 'w1' (ingested 2 chat bundle(s))\n")
        record = self.records.load(session_id)
        self.assertEqual(record["state"], "archived")
        self.assertIsNotNone(record["ended_at"])
        self.assertEqual(record["attached_to"], [])
        self.assertEqual(self.bundle_transcript(session_id, "c1").read_bytes(), TRANSCRIPT)
        self.assertEqual(self.bundle_transcript(session_id, "c2").read_bytes(), ROLLOUT)
        self.assertEqual(record["chats"][0]["bundle_path"], str(self.bundle_dir(session_id, "c1")))
        self.assertEqual(record["chats"][1]["bundle_path"], str(self.bundle_dir(session_id, "c2")))
        tail = self.log_tail()[0]
        self.assertEqual((tail["type"], tail["msg"]), ("archive", "w1"))

    def test_t_hist_09_no_ingestable_chats(self):
        session_id = self.records.llm(name="w2")  # one pending original ref
        result = self.tx(["archive", "w2"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, "Archived 'w2'\n")
        self.assertEqual(self.records.load(session_id)["state"], "archived")
        self.assertEqual(list(self.home.history_dir.iterdir()), [])

    def test_t_hist_09_unknown_session(self):
        result = self.tx(["archive", "nope"])
        self.assertEqual(result.code, 1)
        self.assertEqual(result.out, "")
        self.assertTrue(result.err.startswith("tx archive: "), result.err)
        self.assertIn("'nope'", result.err)
        self.assertEqual(self.log_lines(), [])

    def test_t_hist_09_archiving_an_archived_record_re_ingests(self):
        cwd = self.workdir()
        ended_at = time.time() - 50
        session_id = self.claude_record(name="w3", cwd=cwd, state="archived", ended_at=ended_at)
        self.ingest(session_id)
        source = self.home.claude_transcript_path(cwd, "c1")
        grown = TRANSCRIPT + b'{"type":"assistant","text":"late"}\n'
        source.write_bytes(grown)

        result = self.tx(["archive", "w3"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, "Archived 'w3' (ingested 1 chat bundle(s))\n")
        self.assertEqual(self.bundle_transcript(session_id, "c1").read_bytes(), grown)
        record = self.records.load(session_id)
        self.assertEqual(record["state"], "archived")
        self.assertEqual(record["ended_at"], ended_at)  # terminal is absorbing — not re-stamped
        self.assertEqual(self.log_tail()[0]["type"], "archive")

    # ----- T-HIST-10 tx chat ls rendering (golden) ----------------------------------------

    def test_t_hist_10_chat_ls_rendering(self):
        now = time.time()
        cwd = self.workdir()
        session_id = str(uuid.uuid4())
        other_session_id = str(uuid.uuid4())
        self.records.llm(
            id=session_id,
            name="w1",
            cwd=cwd,
            chats=[
                self.records.chat_ref(
                    session_id=session_id,
                    id=CHAT_ID,
                    cwd=cwd,
                    started_at=now - 7200,
                    bundle_path="/h/x",
                ),
                self.records.chat_ref(
                    session_id=other_session_id,
                    id=None,
                    role="fork",
                    how="fork",
                    chat_id=FORK_SOURCE_CHAT_ID,
                    cwd=cwd,
                    started_at=now - 259200,
                    bundle_path=None,
                ),
            ],
        )
        result = self.tx(["chat", "ls", "w1"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.err, "")
        expected = (
            "w1 — 2 chat(s)\n"
            "  c1c1c1c1  original  spawn                2h ago   /h/x\n"
            "  pending   fork      fork←d2d2d2d2        3d ago   —\n"
        )
        self.assertEqual(result.out, expected)
        self.assert_golden("hist/10", result.out)

    def test_t_hist_10_no_chats(self):
        self.records.llm(name="w1", chats=[])
        result = self.tx(["chat", "ls", "w1"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, "w1 — 0 chat(s)\n  (none)\n")

    def test_t_hist_10_unknown_session(self):
        result = self.tx(["chat", "ls", "x"])
        self.assertEqual(result.code, 1)
        self.assertEqual(result.out, "")
        self.assertEqual(result.err, "tx chat ls: session 'x' not found\n")
