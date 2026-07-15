"""Fail-closed OS boundary for an entire read-only agent process tree.

This stays separate from lifecycle orchestration in ``service.py`` and engine-specific permission
flags in ``engines/`` because it is a security-sensitive, engine-neutral platform boundary. The
reconciler also imports the wrapper binary names; putting them in ``service.py`` would create a
``service`` -> ``reconcile`` -> ``service`` cycle.
"""

from __future__ import annotations

import json
import platform
import shlex
import shutil
from collections.abc import Collection
from pathlib import Path

READ_ONLY_WRAPPER_BINARIES = frozenset({"sandbox-exec", "bwrap"})


class ReadOnlySandboxError(RuntimeError):
    """The host cannot enforce tx's whole-process repository write boundary."""


def wrap_read_only_command(
    command: str,
    workspace: str,
    repository_worktrees: Collection[str],
    git_common_directory: str,
) -> str:
    """Run an agent and every descendant with all repository-owned paths read-only.

    Engine permission controls remain enabled inside this boundary. The single outer sandbox also
    catches lifecycle hooks, MCP subprocesses, and other extension surfaces; nesting two native
    Seatbelt sandboxes on macOS would prevent the inner engine sandbox from starting.
    """
    arguments = shlex.split(command)
    if not arguments:
        raise ReadOnlySandboxError("cannot sandbox an empty agent command")
    boundaries = _minimal_boundaries(
        Path(workspace).parent,
        *(Path(path) for path in repository_worktrees),
        Path(git_common_directory),
    )
    system = platform.system()
    if system == "Darwin":
        executable = shutil.which("sandbox-exec")
        if executable is None:
            raise ReadOnlySandboxError("macOS sandbox-exec is unavailable")
        denied = " ".join(
            f"(subpath {json.dumps(str(boundary))})" for boundary in boundaries
        )
        profile = f"(version 1)\n(allow default)\n(deny file-write* {denied})"
        return shlex.join([executable, "-p", profile, *arguments])
    if system == "Linux":
        executable = shutil.which("bwrap")
        if executable is None:
            raise ReadOnlySandboxError(
                "Linux read-only sessions require bubblewrap (bwrap)"
            )
        wrapped = [executable, "--bind", "/", "/"]
        for boundary in boundaries:
            wrapped.extend(["--ro-bind", str(boundary), str(boundary)])
        wrapped.extend(["--chdir", str(Path(workspace).resolve()), "--", *arguments])
        return shlex.join(wrapped)
    raise ReadOnlySandboxError(
        f"read-only agent sessions are unsupported on {system or 'this platform'}"
    )


def _minimal_boundaries(*paths: Path) -> list[Path]:
    """Canonical non-overlapping paths; denying a parent already denies every child."""
    resolved = sorted(
        {path.resolve() for path in paths}, key=lambda path: len(path.parts)
    )
    if Path("/") in resolved:
        raise ReadOnlySandboxError(
            "refusing to apply a read-only boundary to filesystem root"
        )
    return [
        path
        for path in resolved
        if not any(
            path != parent and path.is_relative_to(parent) for parent in resolved
        )
    ]
