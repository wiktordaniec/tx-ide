"""Git worktree creation for isolated agent worker spawns."""

from __future__ import annotations

import hashlib
import re
import subprocess
from collections.abc import Collection
from pathlib import Path

from .storage import worktrees_dir


class WorktreeError(RuntimeError):
    """A requested worktree could not be created."""


class WorktreeManager:
    """Create tx-owned detached worktrees from a repository checkout."""

    def __init__(self, root: Path | None = None):
        self._root = root

    @property
    def root(self) -> Path:
        return self._root if self._root is not None else worktrees_dir()

    def create(self, starting_directory: str, worktree_name: str) -> Path:
        if (
            not worktree_name
            or worktree_name in {".", ".."}
            or Path(worktree_name).name != worktree_name
        ):
            raise WorktreeError(f"invalid worktree name '{worktree_name}'")

        main_checkout, common_directory, registered_paths = self._repository_context(
            starting_directory
        )
        worktree_directory = self._path_for(
            main_checkout, common_directory, worktree_name
        )
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
        main_checkout, common_directory, registered_paths = self._repository_context(
            starting_directory
        )
        unavailable = set(unavailable_names)
        for suffix in range(1, 100):
            candidate = base_name if suffix == 1 else f"{base_name}-{suffix}"
            if (
                not candidate
                or candidate in {".", ".."}
                or Path(candidate).name != candidate
                or candidate in unavailable
            ):
                continue
            path = self._path_for(main_checkout, common_directory, candidate)
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

    def repository_key(self, starting_directory: str) -> str:
        """Stable readable key used for this repository under the global tx worktree root."""
        main_checkout, common_directory, _ = self._repository_context(starting_directory)
        return self._repository_key(main_checkout, common_directory)

    def repository_worktrees(self, directory: str) -> tuple[Path, ...]:
        """Every registered checkout whose contents a read-only worker must not modify."""
        _, _, registered_paths = self._repository_context(directory)
        return tuple(sorted(registered_paths, key=str))

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

    def git_common_directory(self, directory: str) -> Path:
        """Canonical shared Git metadata directory for a checkout or linked worktree."""
        return self._resolve_git_path(
            directory, self._git(directory, "rev-parse", "--git-common-dir").strip()
        )

    def remove(self, directory: Path) -> None:
        """Remove a tx-created worktree after a pre-launch failure or temporary helper exit."""
        self._git(str(directory), "worktree", "remove", "--force", str(directory))

    def _repository_context(
        self, starting_directory: str
    ) -> tuple[Path, Path, set[Path]]:
        listing = self._git(starting_directory, "worktree", "list", "--porcelain")
        worktree_lines = [
            line.removeprefix("worktree ")
            for line in listing.splitlines()
            if line.startswith("worktree ")
        ]
        if not worktree_lines:
            raise WorktreeError("git did not report a main checkout")
        return (
            Path(worktree_lines[0]).resolve(),
            self.git_common_directory(starting_directory),
            {Path(path).resolve() for path in worktree_lines},
        )

    def _path_for(
        self, main_checkout: Path, common_directory: Path, worktree_name: str
    ) -> Path:
        label = f"{self._repository_slug(main_checkout)}--{worktree_name}"
        return self.root / self._repository_key(main_checkout, common_directory) / label

    @staticmethod
    def _repository_key(main_checkout: Path, common_directory: Path) -> str:
        # The readable prefix makes the global root browsable; hashing the canonical shared Git
        # directory keeps two unrelated repositories with the same basename collision-free.
        slug = WorktreeManager._repository_slug(main_checkout)
        digest = hashlib.sha256(str(common_directory).encode()).hexdigest()[:8]
        return f"{slug}-{digest}"

    @staticmethod
    def _repository_slug(main_checkout: Path) -> str:
        """Filesystem-safe repository label shared by the worktree path and agent footers."""
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", main_checkout.name).strip("-._")
        return slug or "repository"

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
