"""Artifacts — the durable record of what agents deliver (plans, diffs, walkthroughs, reports).

An artifact is a pointer to a reviewable deliverable plus enough metadata to open it later:
`tx artifact add` registers one, `tx artifacts` is the browsable picker, and Enter opens it in an
nvim companion (`--open` for a file, `--diff` for a branch diff). Records live at
`$TX_IDE_HOME/artifacts/<uuid>.json`, one file per artifact, with the same atomic-write /
tolerant-read contract as `SessionStore` (OPEN-0a: filesystem-direct, never the `Storage` KV).

**Snapshot + track source.** A file artifact is copied into `$TX_IDE_HOME/artifacts/<uuid>/` at
register time, and the original path is kept: opening prefers the live source (the agent may still
be editing the plan) and falls back to the snapshot when the source is gone — a plan inside
`.tx-ide/worktrees/<name>` dies with its worktree, but the artifact must not. A diff artifact has
no file to snapshot; it records the repo + base and opens live git state, so it degrades naturally
when the branch is gone.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .events import EventLog
from .storage import artifacts_dir

SCHEMA_VERSION = 1


class UnsupportedArtifactError(ValueError):
    """A record whose schema this code does not read."""


class ArtifactType(Enum):
    PLAN = "plan"
    DIFF = "diff"
    WALKTHROUGH = "walkthrough"
    REPORT = "report"
    DOC = "doc"


@dataclass
class Artifact:
    """One deliverable. `source_path`/`snapshot_path` carry a file artifact's open target
    (resolved by `open_path`); `diff_base` carries a diff artifact's. `session_id`/`session_name`
    are the producer (display + provenance — the name is denormalized so listing never joins
    against the session store)."""

    id: str
    type: ArtifactType
    title: str
    repo: str
    tags: list[str] = field(default_factory=list)
    session_id: str | None = None
    session_name: str | None = None
    source_path: str | None = None
    snapshot_path: str | None = None
    diff_base: str | None = None
    created_at: float = 0.0
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type.value,
            "title": self.title,
            "repo": self.repo,
            "tags": list(self.tags),
            "session_id": self.session_id,
            "session_name": self.session_name,
            "source_path": self.source_path,
            "snapshot_path": self.snapshot_path,
            "diff_base": self.diff_base,
            "created_at": self.created_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Artifact:
        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            raise UnsupportedArtifactError(
                f"artifact record schema v{version} is not v{SCHEMA_VERSION}"
            )
        return cls(
            id=data["id"],
            type=ArtifactType(data["type"]),
            title=data["title"],
            repo=data["repo"],
            tags=list(data["tags"]),
            session_id=data["session_id"],
            session_name=data["session_name"],
            source_path=data["source_path"],
            snapshot_path=data["snapshot_path"],
            diff_base=data["diff_base"],
            created_at=data["created_at"],
        )


def open_path(artifact: Artifact) -> str | None:
    """The path Enter opens for a file artifact: the live source while it exists (the producer may
    still be editing it), else the snapshot taken at register time, else None (both gone — the
    record outlived its content). A diff artifact has no path; its open target is `diff_base`."""
    if artifact.source_path and os.path.exists(artifact.source_path):
        return artifact.source_path
    if artifact.snapshot_path and os.path.exists(artifact.snapshot_path):
        return artifact.snapshot_path
    return None


class ArtifactStore:
    """The repository over `$TX_IDE_HOME/artifacts/<uuid>.json` — the `SessionStore` contract:
    atomic temp-file + `os.replace` writes, tolerant directory-glob reads. Snapshot copies live in
    a sibling `<uuid>/` directory per artifact, so the `*.json` glob never sees them."""

    def __init__(self, directory: Path | None = None):
        self.directory = directory if directory is not None else artifacts_dir()

    def _path(self, artifact_id: str) -> Path:
        return self.directory / f"{artifact_id}.json"

    def snapshot_dir(self, artifact_id: str) -> Path:
        return self.directory / artifact_id

    def save(self, artifact: Artifact) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(artifact.to_dict(), indent=2)
        descriptor, temp_path = tempfile.mkstemp(
            dir=self.directory, prefix=f".{artifact.id}.", suffix=".tmp"
        )
        try:
            with os.fdopen(descriptor, "w") as handle:
                handle.write(payload)
            os.replace(temp_path, self._path(artifact.id))
        except BaseException:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise

    def load(self, artifact_id: str) -> Artifact | None:
        path = self._path(artifact_id)
        if not path.exists():
            return None
        return self._read(path)

    def _read(self, path: Path) -> Artifact:
        with open(path) as handle:
            return Artifact.from_dict(json.load(handle))

    def all(self) -> list[Artifact]:
        artifacts: list[Artifact] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                artifacts.append(self._read(path))
            except (UnsupportedArtifactError, json.JSONDecodeError, KeyError, ValueError) as error:
                print(f"tx: skipping unreadable artifact {path.name}: {error}", file=sys.stderr)
        return artifacts

    def find(self, target: str) -> Artifact | None:
        """Resolve an id or unique id prefix (the picker carries full ids; a human types a short
        one). None when nothing or more than one record matches."""
        exact = self.load(target)
        if exact is not None:
            return exact
        matches = [artifact for artifact in self.all() if artifact.id.startswith(target)]
        return matches[0] if len(matches) == 1 else None

    def remove(self, artifact: Artifact) -> None:
        """Delete the record and its snapshot directory."""
        self._path(artifact.id).unlink(missing_ok=True)
        shutil.rmtree(self.snapshot_dir(artifact.id), ignore_errors=True)


def register(
    store: ArtifactStore,
    *,
    type: ArtifactType,
    title: str,
    repo: str,
    tags: list[str],
    session_id: str | None = None,
    session_name: str | None = None,
    source_path: str | None = None,
    diff_base: str | None = None,
    log: EventLog | None = None,
) -> Artifact:
    """Create + persist one artifact; snapshot a file artifact's source into the store (see the
    module docstring for why both paths are kept). One provenance line per mutation (D8)."""
    artifact = Artifact(
        id=str(uuid.uuid4()),
        type=type,
        title=title,
        repo=repo,
        tags=list(tags),
        session_id=session_id,
        session_name=session_name,
        source_path=source_path,
        diff_base=diff_base,
        created_at=time.time(),
    )
    if source_path is not None:
        snapshot_directory = store.snapshot_dir(artifact.id)
        snapshot_directory.mkdir(parents=True, exist_ok=True)
        snapshot = snapshot_directory / os.path.basename(source_path)
        shutil.copy2(source_path, snapshot)
        artifact.snapshot_path = str(snapshot)
    store.save(artifact)
    (log or EventLog()).append(
        "artifact", f"add {type.value} '{title}' ({artifact.id[:8]})"
    )
    return artifact
