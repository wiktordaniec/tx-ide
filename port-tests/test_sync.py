"""SYNC — `tx sync push|pull|status` over a local `--remote` archive dir (spec §04 SYNC, T-SYNC-01..10).

Every remote lives under the test root; the S3 backend is the deferred stub. Record files here are
raw JSON dicts — sync parses only `ended_at` / `last_activity` / `created_at` (the last-writer-wins
clock), never the v6 shape.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from txkit import TxCase, expected_failure_on_python

S3_DEFERRED = (
    "S3 sync: deferred — the S3 backend is not implemented yet (§14). "
    "Use `--remote PATH` for a local archive, or configure it later."
)
NO_REMOTE_LINE = "Remote: none configured (local-only; pass --remote/--s3 or set config.json)."


def write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content)


def record(**fields) -> str:
    """Compact JSON for a raw record file (`pad` distinguishes local/remote bytes)."""
    return json.dumps(fields)


def files(root: Path) -> dict[str, bytes]:
    """Every regular file under `root` (relative path → bytes), symlinked dirs not followed."""
    found: dict[str, bytes] = {}
    for directory, _dirs, names in os.walk(root):
        for name in names:
            path = Path(directory) / name
            found[str(path.relative_to(root))] = path.read_bytes()
    return found


def snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    """(bytes, mtime_ns) per file — proves a dry run wrote nothing."""
    return {
        relative: (content, (root / relative).stat().st_mtime_ns)
        for relative, content in files(root).items()
    }


class TestSync(TxCase):
    def setUp(self) -> None:
        super().setUp()
        self.arch = self.root / "arch"
        self.arch.mkdir()

    # ----- fixtures ----------------------------------------------------------------------

    def _corpus_01(self) -> None:
        """T-SYNC-01's home: one record, a `.tmp` beside it, one history file, log + config, plus
        non-corpus files under artifacts / user-agents / worktrees / launch."""
        home = self.home.path
        write(home / "sessions" / "a.json", record(created_at=1))
        write(home / "sessions" / ".a.tmp", "tmp")
        write(home / "history" / "t1" / "c1" / "transcript.jsonl", '{"type":"user"}\n')
        write(home / "log.jsonl", '{"ts":1,"actor":"","type":"x","msg":"y"}\n')
        write(home / "config.json", "{}")
        write(home / "artifacts" / "x.json", "{}")
        write(home / "user-agents" / "DEV.md", "# Dev\n")
        write(home / "worktrees" / "w" / "file", "w")
        write(home / "launch" / "x.sh", "#!/bin/sh\n")

    def _state_04(self) -> None:
        """T-SYNC-04's pre-push state: local {r1 (5), r2 (5), h1, log}; remote {r1 same, r2 (7)
        differing, h1 differing, extra r3}."""
        home = self.home.path
        write(home / "sessions" / "r1.json", record(created_at=5, pad="same"))
        write(home / "sessions" / "r2.json", record(created_at=5, pad="local"))
        write(home / "history" / "h1", "local-h1\n")
        write(home / "log.jsonl", '{"ts":1}\n')
        write(self.arch / "sessions" / "r1.json", record(created_at=5, pad="same"))
        write(self.arch / "sessions" / "r2.json", record(created_at=7, pad="remote"))
        write(self.arch / "history" / "h1", "remote-h1\n")
        write(self.arch / "sessions" / "r3.json", record(created_at=1, pad="extra"))

    def _local_block(self, records: int, history: int, log: bool, config: bool) -> str:
        return (
            f"Local corpus ({self.home.path}):\n"
            f"  records:        {records}\n"
            f"  history files:  {history}\n"
            f"  log.jsonl:      {'present' if log else 'missing'}\n"
            f"  config.json:    {'present' if config else 'missing'}\n"
            f"  total keys:     {records + history + int(log) + int(config)}\n"
        )

    # ----- cases -------------------------------------------------------------------------

    def test_t_sync_01_corpus_key_filter(self):
        self._corpus_01()
        status = self.tx(["sync", "status", "--remote", str(self.arch)])
        self.assertEqual(status.code, 0, status.err)
        self.assertEqual(
            status.out,
            self._local_block(1, 1, True, True)
            + f"Remote ({self.arch}): 5 key(s) to push, 0 key(s) to pull.\n",
        )
        push = self.tx(["sync", "push", "--remote", str(self.arch)])
        self.assertEqual(push.code, 0, push.err)
        self.assertEqual(push.out, f"sync push (local → {self.arch}): 5 added, 0 updated, 0 unchanged\n")
        self.assertEqual(push.err, "")
        self.assertEqual(
            sorted(files(self.arch)),
            [
                "config.json",
                "history/t1/c1/transcript.jsonl",
                "log.jsonl",
                "sessions/.a.tmp",
                "sessions/a.json",
            ],
        )

    def test_t_sync_02_record_clock_precedence(self):
        local = self.home.sessions_dir
        remote = self.arch / "sessions"
        pairs = {
            "k1": (record(ended_at=5, last_activity=9, created_at=1, pad="l"),
                   record(ended_at=None, last_activity=6, created_at=1, pad="r")),
            "k2": (record(ended_at=None, last_activity=9, created_at=1, pad="l"),
                   record(ended_at=None, last_activity=None, created_at=10, pad="r")),
            "k3": (record(ended_at=None, last_activity=None, created_at=1, pad="l"),
                   record(ended_at=None, last_activity=None, created_at=2, pad="r")),
            "k4": (record(pad="l"), record(created_at=1, pad="r")),
            "k5": (record(ended_at=0, last_activity=9, pad="l"),
                   record(ended_at=None, last_activity=8, pad="r")),
        }
        for key, (local_bytes, remote_bytes) in pairs.items():
            write(local / f"{key}.json", local_bytes)
            write(remote / f"{key}.json", remote_bytes)
        result = self.tx(["sync", "push", "--remote", str(self.arch)])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(
            result.out,
            f"sync push (local → {self.arch}): 0 added, 1 updated, 0 unchanged, 4 kept (destination newer)\n",
        )
        for key in ("k1", "k2", "k3", "k4"):
            self.assertEqual((remote / f"{key}.json").read_text(), pairs[key][1], key)
        self.assertEqual((remote / "k5.json").read_text(), pairs["k5"][0])

    def test_t_sync_03_verdict_rules(self):
        local = self.home.path
        write(local / "sessions" / "r-same.json", record(created_at=10, pad="same"))
        write(self.arch / "sessions" / "r-same.json", record(created_at=10, pad="same"))
        write(local / "sessions" / "r-older.json", record(created_at=9, pad="l"))
        write(self.arch / "sessions" / "r-older.json", record(created_at=10, pad="r"))
        write(local / "sessions" / "r-equal.json", record(created_at=10, pad="l"))
        write(self.arch / "sessions" / "r-equal.json", record(created_at=10, pad="r"))
        write(local / "sessions" / "r-newer.json", record(created_at=11, pad="l"))
        write(self.arch / "sessions" / "r-newer.json", record(created_at=10, pad="r"))
        write(local / "history" / "h", "local-h\n")
        write(self.arch / "history" / "h", "remote-h\n")
        write(local / "log.jsonl", '{"ts":1}\n')
        result = self.tx(["sync", "push", "--remote", str(self.arch)])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(
            result.out,
            f"sync push (local → {self.arch}): 1 added, 3 updated, 1 unchanged, 1 kept (destination newer)\n",
        )
        remote = files(self.arch)
        self.assertEqual(remote["sessions/r-older.json"], record(created_at=10, pad="r").encode())
        self.assertEqual(remote["sessions/r-equal.json"], record(created_at=10, pad="l").encode())
        self.assertEqual(remote["sessions/r-newer.json"], record(created_at=11, pad="l").encode())
        self.assertEqual(remote["history/h"], b"local-h\n")
        self.assertEqual(remote["log.jsonl"], b'{"ts":1}\n')
        self.assertEqual(remote["sessions/r-same.json"], record(created_at=10, pad="same").encode())

    def test_t_sync_04_push_pull_copy_and_buckets(self):
        self._state_04()
        push = self.tx(["sync", "push", "--remote", str(self.arch)])
        self.assertEqual(push.code, 0, push.err)
        self.assertEqual(
            push.out,
            f"sync push (local → {self.arch}): 1 added, 1 updated, 1 unchanged, 1 kept (destination newer)\n",
        )
        remote = files(self.arch)
        self.assertEqual(remote["sessions/r2.json"], record(created_at=7, pad="remote").encode())
        self.assertEqual(remote["history/h1"], b"local-h1\n")
        self.assertEqual(remote["log.jsonl"], b'{"ts":1}\n')
        self.assertIn("sessions/r3.json", remote)
        pull = self.tx(["sync", "pull", "--remote", str(self.arch)])
        self.assertEqual(pull.code, 0, pull.err)
        self.assertEqual(pull.out, f"sync pull (local ← {self.arch}): 1 added, 1 updated, 3 unchanged\n")
        local = files(self.home.path)
        self.assertEqual(local["sessions/r3.json"], record(created_at=1, pad="extra").encode())
        self.assertEqual(local["sessions/r2.json"], record(created_at=7, pad="remote").encode())
        for key in ("sessions/r1.json", "sessions/r2.json", "history/h1", "log.jsonl"):
            self.assertIn(key, local)
        for key in ("sessions/r1.json", "sessions/r2.json", "sessions/r3.json", "history/h1", "log.jsonl"):
            self.assertIn(key, files(self.arch))

    def test_t_sync_05_status_dry_count(self):
        self._state_04()
        before = (snapshot(self.home.path), snapshot(self.arch))
        expected = (
            self._local_block(2, 1, True, False)
            + f"Remote ({self.arch}): 2 key(s) to push, 3 key(s) to pull.\n"
        )
        for _ in range(2):
            result = self.tx(["sync", "status", "--remote", str(self.arch)])
            self.assertEqual(result.code, 0, result.err)
            self.assertEqual(result.out, expected)
        self.assertEqual((snapshot(self.home.path), snapshot(self.arch)), before)

    def test_t_sync_06_corpus_status(self):
        self._corpus_01()
        result = self.tx(["sync", "status", "--remote", str(self.arch)])
        self.assertEqual(result.code, 0, result.err)
        lines = result.lines
        self.assertEqual(lines[1], "  records:        1")
        self.assertEqual(lines[2], "  history files:  1")
        self.assertEqual(lines[3], "  log.jsonl:      present")
        self.assertEqual(lines[4], "  config.json:    present")
        self.assertEqual(lines[5], "  total keys:     4")
        self.assertEqual(lines[6], f"Remote ({self.arch}): 5 key(s) to push, 0 key(s) to pull.")

    def test_t_sync_07_remote_from_config_none(self):
        for variant in (None, "{}", '{"sync": null}'):
            if variant is None:
                self.home.config_path.unlink(missing_ok=True)
            else:
                self.home.write_config(variant)
            result = self.tx(["sync", "status"])
            self.assertEqual(result.code, 0, result.err)
            self.assertEqual(result.lines[-1], NO_REMOTE_LINE, variant)

    def test_t_sync_07_remote_from_config_local_and_s3(self):
        arch = self.home.user_home / "arch"
        arch.mkdir()
        self.home.write_config({"sync": {"backend": "local", "path": "~/arch"}})
        result = self.tx(["sync", "status"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.lines[-1], f"Remote ({arch}): 1 key(s) to push, 0 key(s) to pull.")
        self.home.write_config({"sync": {"backend": "s3", "bucket": "b", "prefix": "p/"}})
        result = self.tx(["sync", "status"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.lines[-1], f"Remote (s3://b/p/): {S3_DEFERRED}")
        self.home.write_config({"sync": {"backend": "s3", "bucket": "b"}})
        result = self.tx(["sync", "status"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.lines[-1], f"Remote (s3://b): {S3_DEFERRED}")

    @expected_failure_on_python
    def test_t_sync_07_fixed_unknown_backend(self):
        self.home.write_config({"sync": {"backend": "ftp"}})
        for action in ("status", "push"):
            result = self.tx(["sync", action])
            self.assertEqual(result.code, 1, action)
            self.assertIn("tx sync: unknown sync backend 'ftp' (expected 's3' or 'local')", result.err.splitlines(), action)
            self.assertNotIn("Traceback", result.err)

    def test_t_sync_08_status_output(self):
        home = self.home.path
        write(home / "sessions" / "s1.json", record(created_at=1))
        write(home / "sessions" / "s2.json", record(created_at=2))
        write(home / "history" / "t1" / "c1" / "transcript.jsonl", "a\n")
        write(home / "history" / "t1" / "c1" / "meta.json", "{}")
        write(home / "history" / "t2" / "c2" / "transcript.jsonl", "b\n")
        write(home / "log.jsonl", '{"ts":1}\n')
        write(self.arch / "sessions" / "s9.json", record(created_at=9))
        result = self.tx(["sync", "status", "--remote", str(self.arch)])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(
            result.out,
            self._local_block(2, 3, True, False)
            + f"Remote ({self.arch}): 6 key(s) to push, 1 key(s) to pull.\n",
        )
        normalised = result.out.replace(str(self.arch), "<arch>").replace(str(home), "<home>")
        self.assert_golden("sync/08", normalised)
        without = self.tx(["sync", "status"])
        self.assertEqual(without.code, 0, without.err)
        self.assertEqual(without.lines[-1], NO_REMOTE_LINE)

    def test_t_sync_09_push_pull_output(self):
        self._state_04()
        push = self.tx(["sync", "push", "--remote", str(self.arch)])
        self.assertEqual(push.code, 0, push.err)
        self.assertEqual(
            push.out,
            f"sync push (local → {self.arch}): 1 added, 1 updated, 1 unchanged, 1 kept (destination newer)\n",
        )
        pull = self.tx(["sync", "pull", "--remote", str(self.arch)])
        self.assertEqual(pull.code, 0, pull.err)
        self.assertEqual(pull.out, f"sync pull (local ← {self.arch}): 1 added, 1 updated, 3 unchanged\n")

    def test_t_sync_09_no_remote_bogus_action_precedence(self):
        write(self.home.path / "log.jsonl", '{"ts":1}\n')
        result = self.tx(["sync", "push"])
        self.assertEqual(result.code, 1)
        self.assertEqual(
            result.err,
            "tx sync: no remote configured — pass --remote PATH / --s3 BUCKET, or set the 'sync' "
            "section in config.json\n",
        )
        self.assertEqual(result.out, "")
        bogus = self.tx(["sync", "bogus"])
        self.assertEqual(bogus.code, 2)
        self.assertIn("tx sync: error: argument action: invalid choice: 'bogus'", bogus.err)
        # --s3 beats --remote
        result = self.tx(["sync", "status", "--remote", str(self.arch), "--s3", "b"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.lines[-1], f"Remote (s3://b): {S3_DEFERRED}")
        # --remote beats config
        self.home.write_config({"sync": {"backend": "s3", "bucket": "cfg"}})
        result = self.tx(["sync", "status", "--remote", str(self.arch)])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.lines[-1], f"Remote ({self.arch}): 2 key(s) to push, 0 key(s) to pull.")
        # config alone
        result = self.tx(["sync", "status"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.lines[-1], f"Remote (s3://cfg): {S3_DEFERRED}")

    def test_t_sync_10_s3_deferred_error_text(self):
        write(self.home.path / "sessions" / "s1.json", record(created_at=1))
        push = self.tx(["sync", "push", "--s3", "mybucket/pre"])
        self.assertEqual(push.code, 1)
        self.assertEqual(push.err, f"tx sync push: {S3_DEFERRED}\n")
        self.assertEqual(push.out, "")
        status = self.tx(["sync", "status", "--s3", "mybucket"])
        self.assertEqual(status.code, 0, status.err)
        self.assertEqual(
            status.out,
            self._local_block(1, 0, False, False) + f"Remote (s3://mybucket): {S3_DEFERRED}\n",
        )

    def test_t_sync_10_s3_empty_local_corpus(self):
        status = self.tx(["sync", "status", "--s3", "mybucket"])
        self.assertEqual(status.code, 0, status.err)
        self.assertEqual(
            status.out,
            self._local_block(0, 0, False, False) + f"Remote (s3://mybucket): {S3_DEFERRED}\n",
        )
        # No local key ever reaches the stub, so the push completes vacuously (see NOTES).
        push = self.tx(["sync", "push", "--s3", "mybucket"])
        self.assertEqual(push.code, 0, push.err)
        self.assertEqual(push.out, "sync push (local → s3://mybucket): 0 added, 0 updated, 0 unchanged\n")
