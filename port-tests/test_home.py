"""HOME — `lib/tx/storage.py` (+ the config readers in reconcile.py / sync.py) through the CLI:
`$TX_IDE_HOME` resolution and skeleton, the hook-shim gate, `config.json`, and the `LocalStorage` /
`S3Storage` sync boundary via `tx sync`.

Spec cases T-HOME-01..10 (T-HOME-10 dropped).
"""

from __future__ import annotations

import json
import time

from txkit import HOME_DIRS, TxCase, expected_failure_on_python

S3_DEFERRED = (
    "S3 sync: deferred — the S3 backend is not implemented yet (§14). "
    "Use `--remote PATH` for a local archive, or configure it later."
)
NO_REMOTE = "Remote: none configured (local-only; pass --remote/--s3 or set config.json)."


class TestHome(TxCase):
    # ----- T-HOME-01 ----------------------------------------------------------------------

    def test_t_home_01_tx_ide_home_resolution(self):
        explicit = self.root / "x"
        result = self.tx(["_init-home"], env={"TX_IDE_HOME": str(explicit)})
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, f"initialized $TX_IDE_HOME skeleton at {explicit}\n")
        self.assertTrue((explicit / "sessions").is_dir())

        result = self.tx(["_init-home"], env={"TX_IDE_HOME": None})
        self.assertEqual(result.code, 0, result.err)
        default = self.home.user_home / ".tx-ide"
        self.assertEqual(result.out, f"initialized $TX_IDE_HOME skeleton at {default}\n")
        self.assertTrue((default / "sessions").is_dir())

        result = self.tx(["_init-home"], env={"TX_IDE_HOME": "~/foo"})
        self.assertEqual(result.code, 0, result.err)
        tilde = self.home.user_home / "foo"
        self.assertEqual(result.out, f"initialized $TX_IDE_HOME skeleton at {tilde}\n")
        self.assertTrue((tilde / "sessions").is_dir())

        cwd = self.root / "cwd"
        cwd.mkdir()
        result = self.tx(["_init-home"], env={"TX_IDE_HOME": ""}, cwd=cwd)
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, "initialized $TX_IDE_HOME skeleton at .\n")
        self.assertEqual(sorted(entry.name for entry in cwd.iterdir()), sorted(HOME_DIRS))

    # ----- T-HOME-02 ----------------------------------------------------------------------

    def test_t_home_02_subpath_layout(self):
        home = self.root / "h"
        env = {"TX_IDE_HOME": str(home)}
        init = self.tx(["_init-home"], env=env)
        self.assertEqual(init.code, 0, init.err)
        for name in ("sessions", "history", "artifacts", "worktrees", "user-agents", "launch"):
            self.assertTrue((home / name).is_dir(), name)

        spawned = self.tx(["spawn", "s", "--tag", "t", "--cwd", str(self.root), "--cmd", "bash"], env=env)
        self.assertEqual(spawned.code, 0, spawned.err)
        records = list((home / "sessions").glob("*.json"))
        self.assertEqual(len(records), 1)
        self.assertEqual(json.loads(records[0].read_text())["id"], records[0].stem)
        self.assertTrue((home / "log.jsonl").is_file())

        status = self.tx(["sync", "status"], env=env)
        self.assertEqual(status.code, 0, status.err)
        self.assertIn("  config.json:    missing\n", status.out)
        (home / "config.json").write_text("{}")
        status = self.tx(["sync", "status"], env=env)
        self.assertIn("  config.json:    present\n", status.out)

        codex = self.tx(["spawn", "w", "--tag", "t", "--cwd", str(self.git.path), "--cmd", "codex"], env=env)
        self.assertEqual(codex.code, 1)
        self.assertEqual(
            codex.err,
            f"tx spawn: codex hooks are not installed in {home} — the worker's chat id would never "
            "be captured and the session could never be resumed. Install them: "
            "setup/engines/install.sh install --engine codex\n",
        )

        role = self.tx(
            ["spawn", "w", "--tag", "t", "--cwd", str(self.git.path), "--engine", "claude", "--role", "NAME"],
            env=env,
        )
        self.assertEqual(role.code, 2)
        self.assertTrue(
            role.err.endswith(
                f"tx spawn: error: unknown role 'COMMON' (no .md file under {home}/user-agents "
                f"or {home}/agents)\n"
            ),
            role.err,
        )

    # ----- T-HOME-05 ----------------------------------------------------------------------

    def _working_records(self) -> float:
        """Three WORKING llm records (turn ages 1 s / 500 s / 700 s), each live on a `sleep` pane;
        `last_activity` mirrors the turn start so `tx ls` lists them w1, w2, w3."""
        now = time.time()
        for name, age in (("w1", 1), ("w2", 500), ("w3", 700)):
            self.records.llm(
                id=name, name=name, state="working", created_at=now - 2000,
                last_activity=now - age, turn_started_at=now - age,
            )
            self.tmux.new_session(name, "sleep 1000", tx_id=name)
        return now

    def _states(self, out: str) -> list[str]:
        rows = out.splitlines()
        self.assertEqual(rows[0], "PROCESSES")
        return [row.split()[1] for row in rows[1:]]

    def _assert_threshold(self, expected: list[str]) -> None:
        result = self.tx(["ls"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self._states(result.out), expected)
        demoted = [name for name, state in zip(("w1", "w2", "w3"), expected) if state == "idle"]
        self.assertEqual(
            [(line["type"], line["msg"]) for line in self.log_lines()],
            [("reconcile", f"{name} working → idle (stuck)") for name in demoted],
        )
        for name, state in zip(("w1", "w2", "w3"), expected):
            self.assertEqual(self.records.load(name)["state"], state)

    def test_t_home_05_stuck_threshold_default(self):
        self._working_records()
        self._assert_threshold(["working", "working", "idle"])

    def test_t_home_05_stuck_threshold_configured(self):
        self.home.write_config({"stuck_working_threshold_seconds": 5})
        self._working_records()
        self._assert_threshold(["working", "idle", "idle"])

    def test_t_home_05_stuck_threshold_empty_config(self):
        self.home.write_config({})
        self._working_records()
        self._assert_threshold(["working", "working", "idle"])

    def test_t_home_05_stuck_threshold_zero(self):
        self.home.write_config({"stuck_working_threshold_seconds": 0})
        self._working_records()
        self._assert_threshold(["idle", "idle", "idle"])

    def test_t_home_05_parity_malformed_config(self):
        self.home.write_config("{oops")
        self._working_records()
        before = {name: self.records.path(name).read_bytes() for name in ("w1", "w2", "w3")}
        result = self.tx(["ls"])
        self.assertEqual(result.code, 1)
        self.assertEqual(result.out, "")
        self.assertEqual({name: self.records.path(name).read_bytes() for name in before}, before)
        self.assertFalse(self.home.log_path.exists())

    @expected_failure_on_python
    def test_t_home_05_fixed_malformed_config_is_a_message(self):
        self.home.write_config("{oops")
        self._working_records()
        result = self.tx(["ls"])
        self.assertEqual(result.code, 1)
        self.assertEqual(result.out, "")
        self.assertIn("config.json", result.err)
        self.assertNotIn("Traceback", result.err)

    # ----- T-HOME-06 ----------------------------------------------------------------------

    def _sync_status_last_line(self, config: dict | str | None) -> tuple[int, str, str]:
        if config is None:
            self.home.config_path.unlink(missing_ok=True)
        else:
            self.home.write_config(config)
        result = self.tx(["sync", "status"])
        lines = result.out.splitlines()
        return result.code, lines[-1] if lines else "", result.err

    def test_t_home_06_parity_config_sync_backend(self):
        for config in (None, {}, {"sync": None}):
            code, last, _ = self._sync_status_last_line(config)
            self.assertEqual(code, 0)
            self.assertEqual(last, NO_REMOTE)

        code, last, _ = self._sync_status_last_line({"sync": {"backend": "s3", "bucket": "b", "prefix": "tx/"}})
        self.assertEqual(code, 0)
        self.assertEqual(last, f"Remote (s3://b/tx/): {S3_DEFERRED}")

        code, last, _ = self._sync_status_last_line({"sync": {"backend": "local", "path": "~/arch"}})
        self.assertEqual(code, 0)
        self.assertEqual(last, f"Remote ({self.home.user_home / 'arch'}): 1 key(s) to push, 0 key(s) to pull.")

        code, last, _ = self._sync_status_last_line({"sync": {"backend": "s3", "bucket": "b"}})
        self.assertEqual(code, 0)
        self.assertEqual(last, f"Remote (s3://b): {S3_DEFERRED}")

        code, last, err = self._sync_status_last_line({"sync": {"backend": "gcs"}})
        self.assertEqual(code, 1)
        self.assertEqual(last, "")
        self.assertIn("unknown sync backend 'gcs' (expected 's3' or 'local')", err)

        code, last, err = self._sync_status_last_line({"sync": {"backend": "local"}})
        self.assertEqual(code, 1)
        self.assertEqual(last, "")
        self.assertIn("path", err)

    @expected_failure_on_python
    def test_t_home_06_fixed_bad_backend_is_a_message(self):
        code, last, err = self._sync_status_last_line({"sync": {"backend": "gcs"}})
        self.assertEqual(code, 1)
        self.assertEqual(last, "")
        self.assertIn("unknown sync backend 'gcs' (expected 's3' or 'local')", err)
        self.assertNotIn("Traceback", err)

        code, last, err = self._sync_status_last_line({"sync": {"backend": "local"}})
        self.assertEqual(code, 1)
        self.assertEqual(last, "")
        self.assertIn("path", err)
        self.assertNotIn("Traceback", err)

    # ----- T-HOME-07 ----------------------------------------------------------------------

    def _corpus(self) -> dict[str, bytes]:
        self.records.llm(id="a", name="a", state="exited", ended_at=1000.0)
        (self.home.history_dir / "x").mkdir()
        (self.home.history_dir / "x" / "y.md").write_text("bundle\n")
        self.home.log_path.write_text('{"ts":1.0,"actor":"","type":"pre","msg":"existing"}\n')
        self.home.write_config({})
        return {
            key: (self.home.path / key).read_bytes()
            for key in ("sessions/a.json", "history/x/y.md", "log.jsonl", "config.json")
        }

    def test_t_home_07_local_storage_put_get_exists(self):
        corpus = self._corpus()
        archive = self.root / "arch"
        archive.mkdir()
        remote = ["--remote", str(archive)]

        push = self.tx(["sync", "push", *remote])
        self.assertEqual(push.code, 0, push.err)
        self.assertEqual(push.out, f"sync push (local → {archive}): 4 added, 0 updated, 0 unchanged\n")
        for key, data in corpus.items():
            self.assertEqual((archive / key).read_bytes(), data, key)

        status = self.tx(["sync", "status", *remote])
        self.assertEqual(status.code, 0, status.err)
        self.assertEqual(status.out.splitlines()[0], f"Local corpus ({self.home.path}):")
        self.assertEqual(status.out.splitlines()[-1], f"Remote ({archive}): 0 key(s) to push, 0 key(s) to pull.")

        again = self.tx(["sync", "push", *remote])
        self.assertEqual(again.out, f"sync push (local → {archive}): 0 added, 0 updated, 4 unchanged\n")

        with open(archive / "log.jsonl", "a") as handle:
            handle.write('{"ts":2.0,"actor":"","type":"remote","msg":"appended"}\n')
        pull = self.tx(["sync", "pull", *remote])
        self.assertEqual(pull.code, 0, pull.err)
        self.assertEqual(pull.out, f"sync pull (local ← {archive}): 0 added, 1 updated, 3 unchanged\n")
        self.assertEqual(self.home.log_path.read_bytes(), (archive / "log.jsonl").read_bytes())

    # ----- T-HOME-08 ----------------------------------------------------------------------

    def test_t_home_08_local_storage_list(self):
        self.records.llm(id="b", name="b", state="exited", ended_at=1000.0)
        self.records.llm(id="a", name="a", state="exited", ended_at=1000.0)
        self.home.log_path.write_text('{"ts":1.0,"actor":"","type":"pre","msg":"existing"}\n')
        (self.home.history_dir / "x").mkdir()
        (self.home.history_dir / "x" / "y.md").write_text("bundle\n")
        self.home.write_config({})
        (self.home.user_agents_dir / "r.md").write_text("# role\n")
        self.assertEqual(list(self.home.worktrees_dir.iterdir()), [])

        status = self.tx(["sync", "status"])
        self.assertEqual(status.code, 0, status.err)
        for line in (
            "  records:        2",
            "  history files:  1",
            "  log.jsonl:      present",
            "  config.json:    present",
            "  total keys:     5",
        ):
            self.assertIn(line, status.out.splitlines())

        archive = self.root / "arch"
        push = self.tx(["sync", "push", "--remote", str(archive)])
        self.assertEqual(push.code, 0, push.err)
        copied = sorted(str(path.relative_to(archive)) for path in archive.rglob("*") if path.is_file())
        self.assertEqual(
            copied, ["config.json", "history/x/y.md", "log.jsonl", "sessions/a.json", "sessions/b.json"]
        )

    # ----- T-HOME-09 ----------------------------------------------------------------------

    def test_t_home_09_s3_storage_deferred(self):
        empty = self.tx(["sync", "push", "--s3", "bucket/p/"])
        self.assertEqual(empty.code, 0, empty.err)
        self.assertEqual(empty.out, "sync push (local → s3://bucket/p/): 0 added, 0 updated, 0 unchanged\n")

        self.records.llm(id="a", name="a", state="exited", ended_at=1000.0)
        push = self.tx(["sync", "push", "--s3", "bucket/p/"])
        self.assertEqual(push.code, 1)
        self.assertEqual(push.out, "")
        self.assertEqual(push.err, f"tx sync push: {S3_DEFERRED}\n")
        self.assert_golden("home/09", push.err)

        pull = self.tx(["sync", "pull", "--s3", "bucket/p/"])
        self.assertEqual(pull.code, 1)
        self.assertEqual(pull.out, "")
        self.assertEqual(pull.err, f"tx sync pull: {S3_DEFERRED}\n")

        status = self.tx(["sync", "status", "--s3", "bucket/p/"])
        self.assertEqual(status.code, 0, status.err)
        self.assertEqual(status.out.splitlines()[-1], f"Remote (s3://bucket/p/): {S3_DEFERRED}")

        bare = self.tx(["sync", "status", "--s3", "bucket"])
        self.assertEqual(bare.code, 0, bare.err)
        self.assertEqual(bare.out.splitlines()[-1], f"Remote (s3://bucket): {S3_DEFERRED}")


class TestHomeNoHooks(TxCase):
    """T-HOME-03: a home with no `hooks/` at all (the shim gate)."""

    home_options = {"hooks": ()}

    def test_t_home_03_engine_hook_shim_gate(self):
        self.assertFalse(self.home.hooks_dir.exists())
        for engine in ("claude", "codex"):
            result = self.tx(["spawn", "w", "--tag", "t", "--cwd", str(self.git.path), "--cmd", engine])
            self.assertEqual(result.code, 1, result.out)
            self.assertEqual(
                result.err,
                f"tx spawn: {engine} hooks are not installed in {self.home.path} — the worker's chat "
                "id would never be captured and the session could never be resumed. Install them: "
                f"setup/engines/install.sh install --engine {engine}\n",
            )
            self.assertEqual(list(self.home.sessions_dir.glob("*.json")), [])

        shim = self.home.hooks_dir / "codex" / "start.sh"
        shim.parent.mkdir(parents=True)
        shim.write_text("")
        result = self.tx(["spawn", "w", "--tag", "t", "--cwd", str(self.git.path), "--cmd", "codex"])
        self.assertEqual(result.code, 0, result.err)
        record = json.loads(self.tx(["show", "w"]).out)
        self.assertEqual(record["role"], "llm")
        self.assertEqual(record["engine"], "codex")
        self.assertIn(record["id"], self.tmux.sessions())


class TestHomeEmpty(TxCase):
    """T-HOME-04: the skeleton against an absent home."""

    home_options = {"skeleton": False, "link_agents": False, "hooks": ()}

    def test_t_home_04_ensure_home_creates_exactly_these_dirs(self):
        self.assertFalse(self.home.path.exists())
        result = self.tx(["_init-home"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, f"initialized $TX_IDE_HOME skeleton at {self.home.path}\n")
        self.assertEqual(
            self.home.entries(), ["artifacts", "history", "launch", "sessions", "user-agents", "worktrees"]
        )

        marker = self.home.sessions_dir / "keep.json"
        marker.write_text("{}")
        again = self.tx(["_init-home"])
        self.assertEqual(again.code, 0, again.err)
        self.assertEqual(again.out, result.out)
        self.assertEqual(self.home.entries(), ["artifacts", "history", "launch", "sessions", "user-agents", "worktrees"])
        self.assertEqual(marker.read_text(), "{}")

        other = self.root / "h2"
        helped = self.tx(["help"], env={"TX_IDE_HOME": str(other)})
        self.assertEqual(helped.code, 0, helped.err)
        self.assertEqual(
            sorted(entry.name for entry in other.iterdir()),
            ["artifacts", "history", "launch", "sessions", "user-agents", "worktrees"],
        )
