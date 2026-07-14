"""Git worktree creation for isolated agent worker spawns."""

from __future__ import annotations

import subprocess
from pathlib import Path


class WorktreeError(RuntimeError):
    """A requested worktree could not be created."""


class WorktreeManager:
    """Create tx-owned detached worktrees from a repository checkout."""

    def create(self, starting_directory: str, worktree_name: str) -> Path:
        if not worktree_name or Path(worktree_name).name != worktree_name:
            raise WorktreeError(f"invalid worktree name '{worktree_name}'")

        listing = self._git(starting_directory, "worktree", "list", "--porcelain")
        lines = listing.splitlines()
        if not lines or not lines[0].startswith("worktree "):
            raise WorktreeError("git did not report a main checkout")
        first_line = lines[0]
        main_checkout = Path(first_line.removeprefix("worktree ")).resolve()
        repository_name = main_checkout.name
        worktree_directory = (
            main_checkout
            / ".tx-ide"
            / "worktrees"
            / f"{repository_name}--{worktree_name}"
        )
        if worktree_directory.exists():
            raise WorktreeError(f"worktree path already exists: {worktree_directory}")

        worktree_directory.parent.mkdir(parents=True, exist_ok=True)
        self._git(
            starting_directory,
            "worktree",
            "add",
            "--detach",
            str(worktree_directory),
            "HEAD",
        )
        return worktree_directory

    @staticmethod
    def _git(starting_directory: str, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", starting_directory, *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise WorktreeError(detail or "git worktree command failed")
        return result.stdout
