"""T-WT — `lib/tx/worktree.py` through `tx spawn --engine` and `tx _chat-op-watch` (spec 02).

The *worker fixture*: a main checkout, the fake `claude`, the claude hook shims and a plain
`agents/COMMON.md` (no frontmatter skill grant, so a worker's launch env is exactly the flag the
cases list — the shipped COMMON.md would add `TX_SKILLS`). tx-owned worktrees land under
`$TX_IDE_HOME/worktrees/<slug>-<sha8>/<slug>--<name>`; the test recomputes `<sha8>` from the
resolved common git dir (`WorktreeManager._repository_key`).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from pathlib import Path

from txkit import GitFixture, Result, TxCase, expected_failure_on_python

PLAIN_COMMON = "# COMMON\n\nBe concise.\n"
HOOKS_MISSING = (
    "tx spawn: claude hooks are not installed in {home} — the worker's chat id would never be "
    "captured and the session could never be resumed. Install them: "
    "setup/engines/install.sh install --engine claude\n"
)


def repository_slug(checkout: Path) -> str:
    """`WorktreeManager._repository_slug`: `[^A-Za-z0-9._-]+` → `-`, then `.strip("-._")`."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", checkout.name).strip("-._")
    return slug or "repository"


def repository_digest(checkout: Path) -> str:
    """`<D>`: sha256 of the resolved common git dir, first 8 hex."""
    return hashlib.sha256(str((checkout / ".git").resolve()).encode()).hexdigest()[:8]


def repository_key(checkout: Path) -> str:
    return f"{repository_slug(checkout)}-{repository_digest(checkout)}"


def porcelain_entries(checkout: GitFixture) -> dict[str, list[str]]:
    """`git worktree list --porcelain` → {realpath: [attribute lines]} (`HEAD …`, `detached`, …)."""
    entries: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in checkout.git("worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            current = entries.setdefault(os.path.realpath(line.removeprefix("worktree ")), [])
        elif line and current is not None:
            current.append(line)
    return entries


def realpaths(paths: list[str] | list[Path]) -> list[str]:
    return [os.path.realpath(path) for path in paths]


class WorkerFixtureCase(TxCase):
    """The worker fixture (see the module docstring); no test methods of its own."""

    home_options = {"link_agents": False}

    def setUp(self) -> None:
        super().setUp()
        self.home.agents.mkdir()
        (self.home.agents / "COMMON.md").write_text(PLAIN_COMMON)

    def checkout(self, name: str = "r", parent: Path | None = None) -> GitFixture:
        return GitFixture(parent if parent is not None else self.root, name=name)

    def worktree_path(self, checkout: GitFixture, name: str) -> Path:
        return (
            self.home.worktrees_dir
            / repository_key(checkout.path)
            / f"{repository_slug(checkout.path)}--{name}"
        )

    def spawn_worker(
        self, name: str, cwd: Path, *extra: str, env: dict[str, str | None] | None = None
    ) -> Result:
        return self.tx(
            ["spawn", name, "--tag", "t", "--engine", "claude", "--cwd", str(cwd), *extra], env=env
        )

    def record(self, name: str) -> dict:
        return json.loads(self.tx(["show", name]).out)

    def assert_nothing_spawned(self, checkout: GitFixture, main: str) -> None:
        self.assertEqual(realpaths(checkout.worktrees()), [main])
        self.assertEqual(list(self.home.sessions_dir.iterdir()), [])
        self.assertEqual(self.log_lines(), [])
        self.assertEqual(self.tmux.sessions(), [])

    def write_chat_op(self, distiller_name: str) -> str:
        """A crafted handover op-spec whose `done` marker already exists (the watchdog returns at
        once and only tears down)."""
        op_id = uuid.uuid4().hex[:8]
        directory = self.home.chat_ops_dir / op_id
        directory.mkdir(parents=True)
        (directory / "spec.json").write_text(
            json.dumps(
                {
                    "op_id": op_id,
                    "kind": "handover",
                    "source_txid": "source",
                    "source_chat": "chat",
                    "cwd": str(self.root),
                    "artifact_path": "",
                    "self_catch_up": False,
                    "read_only": False,
                    "worker_name": "",
                    "pane": "",
                    "distiller_name": distiller_name,
                }
            )
        )
        (directory / "done").write_text("")
        return op_id


class TestWt(WorkerFixtureCase):
    # ----- T-WT-01 path scheme -------------------------------------------------------------

    def test_t_wt_01_path_scheme(self):
        checkout = self.checkout("My Repo!", parent=self.root / "x")
        digest = repository_digest(checkout.path)
        expected = self.home.worktrees_dir / f"My-Repo-{digest}" / "My-Repo--w"
        result = self.spawn_worker("w", checkout.path)
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, f"Spawned 'w' (cwd={expected}, tag=t)\n")
        record = self.record("w")
        self.assertEqual(record["cwd"], str(expected))
        self.assertEqual(record["env"], {"TX_REQUIRE_WORKTREE": "1"})
        head = checkout.head()
        short = checkout.git("rev-parse", "--short", "HEAD").strip()
        listing = checkout.git("worktree", "list").splitlines()
        self.assertTrue(
            any(re.fullmatch(rf"{re.escape(str(expected))}\s+{short} \(detached HEAD\)", line) for line in listing),
            listing,
        )
        self.assertEqual(checkout.git("rev-parse", "--abbrev-ref", "HEAD", cwd=expected).strip(), "HEAD")
        self.assertEqual(checkout.git("rev-parse", "HEAD", cwd=expected).strip(), head)
        dump = self.fakes.wait_dump("claude", record["id"])
        self.assertEqual(dump["env"]["PWD"], str(expected))
        self.assertEqual(dump["env"]["TX_REQUIRE_WORKTREE"], "1")
        self.assertEqual(dump["env"]["TX_SESSION_ID"], record["id"])

    def test_t_wt_01_cwd_inside_linked_worktree_shares_key(self):
        checkout = self.checkout("My Repo!", parent=self.root / "x")
        self.assertEqual(self.spawn_worker("w", checkout.path).code, 0)
        first = self.worktree_path(checkout, "w")
        result = self.spawn_worker("w2", first)
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, f"Spawned 'w2' (cwd={first.parent / 'My-Repo--w2'}, tag=t)\n")
        self.assertEqual(self.record("w2")["cwd"], str(first.parent / "My-Repo--w2"))
        self.assertIn(os.path.realpath(first.parent / "My-Repo--w2"), realpaths(checkout.worktrees()))

    def test_t_wt_01_dashes_basename_slugs_to_repository(self):
        checkout = self.checkout("---")
        expected = self.home.worktrees_dir / f"repository-{repository_digest(checkout.path)}" / "repository--w"
        result = self.spawn_worker("w", checkout.path)
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, f"Spawned 'w' (cwd={expected}, tag=t)\n")
        self.assertEqual(self.record("w")["cwd"], str(expected))

    # ----- T-WT-02 invalid names -----------------------------------------------------------

    def test_t_wt_02_slash_name_refused(self):
        checkout = self.checkout("My Repo!", parent=self.root / "x")
        main = os.path.realpath(checkout.path)
        result = self.spawn_worker("a/b", checkout.path)
        self.assertEqual(result.code, 1)
        self.assertEqual(
            result.err,
            "tx spawn: could not create worktree: could not find a free worktree name based on 'a/b'\n",
        )
        self.assertEqual(result.out, "")
        self.assertEqual(list(self.home.worktrees_dir.iterdir()), [])
        self.assert_nothing_spawned(checkout, main)

    @expected_failure_on_python
    def test_t_wt_02_fixed_empty_and_dot_names_refused(self):
        """Q25 FIX: `""`, `.`, `..` are refused. Every leg is a WORKER spawn (`--engine claude`,
        spec rev 5): only the worker path reaches `next_name`, where the reference derives `-2`,
        `.-2`, `..-2`. The refusal is a `tx spawn: …` error naming the rejected name (the exact
        wording is the port's); nothing is persisted."""
        checkout = self.checkout("My Repo!", parent=self.root / "x")
        main = os.path.realpath(checkout.path)
        for name in ("", ".", ".."):
            with self.subTest(name=name):
                result = self.spawn_worker(name, checkout.path)
                self.assertEqual(result.code, 1, result.err)
                self.assertEqual(result.out, "")
                self.assertTrue(result.err.startswith("tx spawn: "), result.err)
                self.assertIn(f"'{name}'", result.err)
                self.assertEqual(result.err.count("\n"), 1)
                self.assertEqual(list(self.home.sessions_dir.iterdir()), [])
                self.assertEqual(realpaths(checkout.worktrees()), [main])
                self.assertEqual(list(self.home.worktrees_dir.glob("*/*")), [])
                self.assertEqual(self.tmux.sessions(), [])
                self.assertEqual(self.log_lines(), [])

    # ----- T-WT-03 existing path skipped ---------------------------------------------------

    def test_t_wt_03_existing_path_skipped(self):
        checkout = self.checkout("My Repo!", parent=self.root / "x")
        self.assertEqual(self.spawn_worker("w", checkout.path).code, 0)
        old = self.worktree_path(checkout, "w")
        self.assertEqual(self.tx(["kill", "w"]).code, 0)
        old_entries = sorted(entry.name for entry in old.iterdir())
        old_readme = (old / "README.md").read_text()
        bumped = self.worktree_path(checkout, "w-2")
        result = self.spawn_worker("w", checkout.path)
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, f"Spawned 'w-2' (cwd={bumped}, tag=t)\n")
        self.assertEqual(self.record("w-2")["name"], "w-2")
        listed = realpaths(checkout.worktrees())
        self.assertIn(os.path.realpath(old), listed)
        self.assertIn(os.path.realpath(bumped), listed)
        self.assertEqual(sorted(entry.name for entry in old.iterdir()), old_entries)
        self.assertEqual((old / "README.md").read_text(), old_readme)

    def test_t_wt_03_registered_but_deleted_path_skipped(self):
        checkout = self.checkout("My Repo!", parent=self.root / "x")
        gone = self.worktree_path(checkout, "gone")
        gone.parent.mkdir(parents=True)
        checkout.git("worktree", "add", "--detach", str(gone), "HEAD")
        shutil.rmtree(gone)
        bumped = self.worktree_path(checkout, "gone-2")
        result = self.spawn_worker("gone", checkout.path)
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, f"Spawned 'gone-2' (cwd={bumped}, tag=t)\n")
        listing = checkout.git("worktree", "list").splitlines()
        gone_line = next(line for line in listing if line.startswith(f"{gone} "))
        self.assertIn("prunable", gone_line)
        self.assertTrue(any(line.startswith(f"{bumped} ") for line in listing), listing)
        self.assertIn("detached", porcelain_entries(checkout)[os.path.realpath(bumped)])

    # ----- T-WT-04 next_name ---------------------------------------------------------------

    def test_t_wt_04_next_name_skips_paths_and_live_names(self):
        checkout = self.checkout("My Repo!", parent=self.root / "x")
        for taken in ("w", "w-2"):
            self.worktree_path(checkout, taken).mkdir(parents=True, exist_ok=True)
        self.spawn_process("w-3")  # live names are global across repos
        expected = self.worktree_path(checkout, "w-4")
        result = self.spawn_worker("w", checkout.path)
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, f"Spawned 'w-4' (cwd={expected}, tag=t)\n")
        self.assertEqual(self.record("w-4")["name"], "w-4")
        self.assertIn("detached", porcelain_entries(checkout)[os.path.realpath(expected)])

    def test_t_wt_04_nothing_taken_keeps_base_name(self):
        checkout = self.checkout("My Repo!", parent=self.root / "x")
        result = self.spawn_worker("w", checkout.path)
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, f"Spawned 'w' (cwd={self.worktree_path(checkout, 'w')}, tag=t)\n")

    def test_t_wt_04_ninety_nine_names_taken_refused(self):
        checkout = self.checkout("My Repo!", parent=self.root / "x")
        main = os.path.realpath(checkout.path)
        for candidate in ["w", *(f"w-{suffix}" for suffix in range(2, 100))]:
            self.worktree_path(checkout, candidate).mkdir(parents=True, exist_ok=True)
        before = sorted(entry.name for entry in self.worktree_path(checkout, "w").parent.iterdir())
        result = self.spawn_worker("w", checkout.path)
        self.assertEqual(result.code, 1)
        self.assertEqual(
            result.err,
            "tx spawn: could not create worktree: could not find a free worktree name based on 'w'\n",
        )
        self.assertEqual(realpaths(checkout.worktrees()), [main])
        self.assertEqual(sorted(entry.name for entry in self.worktree_path(checkout, "w").parent.iterdir()), before)
        self.assertEqual(list(self.home.sessions_dir.iterdir()), [])

    def test_t_wt_04_exited_record_does_not_block(self):
        checkout = self.checkout("My Repo!", parent=self.root / "x")
        for taken in ("w", "w-2"):
            self.worktree_path(checkout, taken).mkdir(parents=True, exist_ok=True)
        self.spawn_process("w-3")
        self.assertEqual(self.tx(["kill", "w-3"]).code, 0)
        result = self.spawn_worker("w", checkout.path)
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, f"Spawned 'w-3' (cwd={self.worktree_path(checkout, 'w-3')}, tag=t)\n")

    # ----- T-WT-05 is_linked ---------------------------------------------------------------

    def test_t_wt_05_is_linked_gates_distiller_worktree_removal(self):
        checkout = self.checkout("r")
        self.assertEqual(self.spawn_worker("w", checkout.path).code, 0)
        worktree = self.worktree_path(checkout, "w")
        self.assertEqual(self.tx(["kill", "w"]).code, 0)
        notgit = self.root / "notgit"
        notgit.mkdir()
        (notgit / "keep.txt").write_text("keep\n")
        ops = []
        for distiller, cwd in (("d1", worktree), ("d2", checkout.path), ("d3", notgit)):
            self.records.llm(name=distiller, state="exited", cwd=str(cwd))
            ops.append(self.write_chat_op(distiller))
        before = len(self.log_lines())
        for op_id in ops:
            result = self.tx(["_chat-op-watch", op_id])
            self.assertEqual((result.code, result.out, result.err), (0, "", ""))
            self.assertFalse((self.home.chat_ops_dir / op_id).exists())
        self.assertFalse(worktree.exists())
        listed = realpaths(checkout.worktrees())
        self.assertNotIn(os.path.realpath(worktree), listed)
        self.assertEqual(listed, [os.path.realpath(checkout.path)])
        self.assertTrue((checkout.path / "README.md").is_file())
        self.assertTrue((notgit / "keep.txt").is_file())
        kills = [line for line in self.log_lines()[before:] if line["type"] == "kill"]
        self.assertEqual([line["msg"] for line in kills], ["d1", "d2", "d3"])

    # ----- T-WT-06 remove --force ----------------------------------------------------------

    def test_t_wt_06_remove_force_drops_dirty_worktree(self):
        checkout = self.checkout("r")
        self.assertEqual(self.spawn_worker("w", checkout.path).code, 0)
        worktree = self.worktree_path(checkout, "w")
        self.assertEqual(self.tx(["kill", "w"]).code, 0)
        (worktree / "dirty.txt").write_text("untracked\n")
        self.records.llm(name="d", state="exited", cwd=str(worktree))
        op_id = self.write_chat_op("d")
        result = self.tx(["_chat-op-watch", op_id])
        self.assertEqual(result.code, 0, result.err)
        self.assertFalse(worktree.exists())
        self.assertEqual(realpaths(checkout.worktrees()), [os.path.realpath(checkout.path)])

    def test_t_wt_06_pre_launch_failure_removes_fresh_worktree(self):
        checkout = self.checkout("r")
        main = os.path.realpath(checkout.path)
        (self.home.hooks_dir / "claude" / "start.sh").unlink()
        result = self.spawn_worker("w", checkout.path)
        self.assertEqual(result.code, 1)
        self.assertEqual(result.err, HOOKS_MISSING.format(home=self.home.path))
        self.assertEqual(realpaths(checkout.worktrees()), [main])
        key_dir = self.home.worktrees_dir / repository_key(checkout.path)
        self.assertFalse((key_dir / "r--w").exists())
        self.assertEqual(list(self.home.sessions_dir.iterdir()), [])

    # ----- T-WT-07 repository helpers ------------------------------------------------------

    def test_t_wt_07_non_git_cwd_refused(self):
        notgit = self.root / "notgit"
        notgit.mkdir()
        result = self.spawn_worker("q", notgit)
        self.assertEqual(result.code, 1)
        self.assertEqual(
            result.err,
            "tx spawn: could not create worktree: fatal: not a git repository (or any of the parent directories): .git\n",
        )
        self.assertEqual(result.out, "")
        self.assertEqual(list(self.home.sessions_dir.iterdir()), [])
        self.assertEqual(list(self.home.worktrees_dir.iterdir()), [])
        self.assertEqual(self.log_lines(), [])
        self.assertEqual(self.tmux.sessions(), [])
