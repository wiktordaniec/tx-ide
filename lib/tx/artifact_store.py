"""`ArtifactStore` + `ArtifactContent` — persistence for the artifact subsystem (Plan 2).

Two collaborators, mirroring the `session.py` / `store.py` split one level down:

  - `ArtifactStore` owns the RECORD `artifacts/<id>.json`: atomic temp-file + `os.replace` writes
    and directory-`glob` reads, exactly like `SessionStore` (filesystem-direct, uuid-sharded, no
    global lock). **No `delete`** — nothing in the system removes durable content (settled);
    discarding an artifact is a manual `rm -r artifacts/<id>*`, outside the API on purpose.
  - `ArtifactContent` owns the FILES beside the record under `artifacts/<id>/`: the working copy
    `current.<ext>` and the immutable snapshots `revs/<n>.<ext>`. Content is bytes-clean — accept
    any bytes, no type/size gate (settled) — while the record stays utf-8 JSON so metadata is always
    LLM-readable regardless of content.

The rev write is the linearity lock: `claim_rev` stages the full content in a temp file INSIDE the
rev directory (same filesystem, so the link is atomic), then `os.link`s it onto `revs/<n>.<ext>` —
the link fails `FileExistsError` if the slot is taken (the O_EXCL-equivalent that serializes two
concurrent `modify`s), and staging-then-linking means a crash can only ever leave a stray temp
file, never a half-written rev. `current` and an orphan reclaim use the record's temp-file +
`os.replace` discipline.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from .artifact import Artifact, UnsupportedArtifactError
from .storage import artifacts_dir


class ArtifactStore:
    def __init__(self, directory: Path | None = None):
        self.directory = directory if directory is not None else artifacts_dir()

    def _record_path(self, artifact_id: str) -> Path:
        return self.directory / f"{artifact_id}.json"

    def save(self, artifact: Artifact) -> None:
        """Persist the record atomically (temp file + `os.replace`), like `SessionStore.save`. The
        record is authoritative — its `history` defines which revs exist — so the service writes it
        LAST, after the rev/current files are on disk. uuid-sharded filenames mean two artifacts
        never contend."""
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(artifact.to_dict(), indent=2)
        descriptor, temp_path = tempfile.mkstemp(
            dir=self.directory, prefix=f".{artifact.id}.", suffix=".tmp"
        )
        try:
            with os.fdopen(descriptor, "w") as handle:
                handle.write(payload)
            os.replace(temp_path, self._record_path(artifact.id))
        except BaseException:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise

    def load(self, artifact_id: str) -> Artifact | None:
        """Load one record by id, or None if there is no such file. Propagates
        `UnsupportedArtifactError` — the caller named this record, so a bad-version/invariant file is
        a real error, not something to swallow (mirrors `SessionStore.load`)."""
        path = self._record_path(artifact_id)
        if not path.exists():
            return None
        return self._read(path)

    def _read(self, path: Path) -> Artifact:
        with open(path) as handle:
            return Artifact.from_dict(json.load(handle))

    def all(self) -> list[Artifact]:
        """Every record in the store, id-sorted. Tolerant at this persistence boundary (mirrors
        `SessionStore.all`): a record with a bad version / malformed JSON / violated invariant is
        skipped with a one-line stderr warning rather than crashing `ls` / `doctor`. `glob("*.json")`
        matches only records — the `<id>/` content directories share the parent but never collide."""
        artifacts: list[Artifact] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                artifacts.append(self._read(path))
            except (UnsupportedArtifactError, json.JSONDecodeError, KeyError, ValueError) as error:
                print(f"tx: skipping unreadable artifact {path.name}: {error}", file=sys.stderr)
        return artifacts

    def query(self, predicate: Callable[[Artifact], bool]) -> list[Artifact]:
        """Records matching a predicate — backs `artifacts_for_session` (the reverse lookup) and the
        CLI `ls`. A predicate keeps the API flexible without denormalizing anything onto records."""
        return [artifact for artifact in self.all() if predicate(artifact)]


class ArtifactContent:
    """Working-copy + revision file I/O under `artifacts/<id>/` — bytes-clean.

    `current.<ext>` is the working copy (`open` edits it, reads return it); `revs/<n>.<ext>` are the
    immutable snapshots. The service composes this with `ArtifactStore`; splitting record I/O from
    content I/O keeps each collaborator small (plan Components)."""

    def __init__(self, directory: Path | None = None):
        self.directory = directory if directory is not None else artifacts_dir()

    def content_dir(self, artifact: Artifact) -> Path:
        return self.directory / artifact.id

    def revs_dir(self, artifact: Artifact) -> Path:
        return self.content_dir(artifact) / "revs"

    def current_path(self, artifact: Artifact) -> Path:
        return self.content_dir(artifact) / f"current{artifact.extension}"

    def rev_path(self, artifact: Artifact, rev: int) -> Path:
        return self.revs_dir(artifact) / f"{rev}{artifact.extension}"

    def read_current(self, artifact: Artifact) -> bytes:
        return self.current_path(artifact).read_bytes()

    def read_rev(self, artifact: Artifact, rev: int) -> bytes:
        return self.rev_path(artifact, rev).read_bytes()

    def write_current(self, artifact: Artifact, content: bytes) -> None:
        """Refresh the working copy atomically (temp file + `os.replace`). `current` is
        last-write-wins between simultaneous writers by design (the accepted quirk — revs are the
        safety net); the atomic replace only guarantees a reader never sees a torn write."""
        self.content_dir(artifact).mkdir(parents=True, exist_ok=True)
        self._atomic_write(self.current_path(artifact), content)

    def claim_rev(self, artifact: Artifact, rev: int, content: bytes) -> None:
        """Write `revs/<rev>.<ext>` as an EXCLUSIVE claim — the linearity lock. Stage the full
        content in a temp file INSIDE the rev directory (same filesystem, so the link is atomic and
        cheap), then `os.link` it onto the final path: the link raises `FileExistsError` if the slot
        is already taken, which is how two concurrent `modify`s serialize — the loser gets the error.
        Staging-then-linking means a crash can only ever leave a stray `.tmp`, never a half-written
        rev: the rev appears atomically, with its full content, or not at all."""
        revs = self.revs_dir(artifact)
        revs.mkdir(parents=True, exist_ok=True)
        target = self.rev_path(artifact, rev)
        descriptor, temp_path = tempfile.mkstemp(dir=revs, prefix=f".{rev}.", suffix=".tmp")
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
            os.link(temp_path, target)  # atomic exclusive claim — EEXIST => conflict
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def overwrite_rev(self, artifact: Artifact, rev: int, content: bytes) -> None:
        """Overwrite a rev slot unconditionally (temp file + `os.replace`) — used ONLY to reclaim a
        crash orphan: a rev file the authoritative record does not reference. Never used on a
        committed rev; `claim_rev` guards the concurrent-writer path."""
        self.revs_dir(artifact).mkdir(parents=True, exist_ok=True)
        self._atomic_write(self.rev_path(artifact, rev), content)

    def orphan_revs(self, artifact: Artifact) -> list[int]:
        """Rev files on disk that the record's history does NOT reference — crash orphans (a rev
        written before the record was saved). Ignored on read (the record defines what exists),
        reported by `doctor`, and overwritten by the next `modify` targeting that slot."""
        recorded = {touch.rev for touch in artifact.history}
        revs = self.revs_dir(artifact)
        if not revs.exists():
            return []
        found = [
            rev
            for path in revs.iterdir()
            if (rev := _rev_number(path)) is not None and rev not in recorded
        ]
        return sorted(found)

    def missing_revs(self, artifact: Artifact) -> list[int]:
        """History revs with no file on disk — the inverse corruption (should never happen, since the
        record is written after its rev). Reported by `doctor`."""
        return [
            touch.rev
            for touch in artifact.history
            if not self.rev_path(artifact, touch.rev).exists()
        ]

    def current_is_dirty(self, artifact: Artifact) -> bool:
        """Whether the working copy differs from the last snapshot — un-snapshotted edits made in the
        nvim view. A visible state, not an error (flagged by `show` / `doctor`; closed by a no-file
        `tx artifact modify`). Missing files are the caller's (`doctor`'s) concern, not this
        comparison's, so it reads both directly."""
        return self.read_current(artifact) != self.read_rev(artifact, artifact.latest_rev)

    def _atomic_write(self, path: Path, content: bytes) -> None:
        descriptor, temp_path = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
            os.replace(temp_path, path)
        except BaseException:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise


def _rev_number(path: Path) -> int | None:
    """The integer rev a `revs/<n>.<ext>` path encodes, or None for a staging temp (`.<n>.…tmp`,
    which starts with a dot) or any foreign file whose stem is not an integer. A rev file is always
    `<int><single-suffix>`, so the stem is the number."""
    if path.name.startswith("."):
        return None
    stem = path.stem
    return int(stem) if stem.isdigit() else None
