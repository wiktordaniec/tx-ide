"""Git worktree creation for isolated agent worker spawns."""

from __future__ import annotations

import subprocess
from collections.abc import Collection
from pathlib import Path


class WorktreeError(RuntimeError):
    """A requested worktree could not be created."""


class WorktreeManager:
    """Create tx-owned detached worktrees from a repository checkout."""

    def create(self, starting_directory: str, worktree_name: str) -> Path:
        if not worktree_name or Path(worktree_name).name != worktree_name:
            raise WorktreeError(f"invalid worktree name '{worktree_name}'")

        main_checkout, registered_paths = self._repository_context(starting_directory)
        worktree_directory = self._path_for(main_checkout, worktree_name)
        if worktree_directory.exists() or worktree_directory.resolve() in registered_paths:
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

    def next_name(
        self,
        starting_directory: str,
        base_name: str,
        unavailable_names: Collection[str] = (),
    ) -> str:
        """First `<base>`, `<base>-2`, … free in both the session namespace and Git paths."""
        main_checkout, registered_paths = self._repository_context(starting_directory)
        unavailable = set(unavailable_names)
        for suffix in range(1, 100):
            candidate = base_name if suffix == 1 else f"{base_name}-{suffix}"
            if not candidate or Path(candidate).name != candidate or candidate in unavailable:
                continue
            path = self._path_for(main_checkout, candidate)
            if not path.exists() and path.resolve() not in registered_paths:
                return candidate
        raise WorktreeError(f"could not find a free worktree name based on '{base_name}'")

    def create_unique(
        self,
        starting_directory: str,
        base_name: str,
        unavailable_names: Collection[str] = (),
    ) -> tuple[str, Path]:
        """Create a detached worktree using a name free across live sessions and Git paths."""
        name = self.next_name(starting_directory, base_name, unavailable_names)
        return name, self.create(starting_directory, name)

    def is_linked(self, directory: str) -> bool:
        """Whether `directory` belongs to a linked worktree rather than the main checkout."""
        try:
            git_directory = self._resolve_git_path(
                directory, self._git(directory, "rev-parse", "--git-dir").strip()
            )
            common_directory = self._resolve_git_path(
                directory, self._git(directory, "rev-parse", "--git-common-dir").strip()
            )
        except WorktreeError:
            return False
        return git_directory != common_directory

    def remove(self, directory: Path) -> None:
        """Remove a tx-created worktree after a pre-launch failure or temporary helper exit."""
        self._git(str(directory), "worktree", "remove", "--force", str(directory))

    def _repository_context(self, starting_directory: str) -> tuple[Path, set[Path]]:
        listing = self._git(starting_directory, "worktree", "list", "--porcelain")
        worktree_lines = [
            line.removeprefix("worktree ")
            for line in listing.splitlines()
            if line.startswith("worktree ")
        ]
        if not worktree_lines:
            raise WorktreeError("git did not report a main checkout")
        return Path(worktree_lines[0]).resolve(), {
            Path(path).resolve() for path in worktree_lines
        }

    @staticmethod
    def _path_for(main_checkout: Path, worktree_name: str) -> Path:
        return (
            main_checkout
            / ".tx-ide"
            / "worktrees"
            / f"{main_checkout.name}--{worktree_name}"
        )

    @staticmethod
    def _resolve_git_path(starting_directory: str, git_path: str) -> Path:
        path = Path(git_path)
        return (path if path.is_absolute() else Path(starting_directory) / path).resolve()

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
