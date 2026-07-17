"""Domain entities for the artifact subsystem — durable, versioned deliverables (Plan 2).

An `Artifact` is a record at `$TX_IDE_HOME/artifacts/<id>.json` plus its file(s) under
`artifacts/<id>/`. `history` is ONE list that is both the provenance log (who touched it, when) and
the version index (each touch produced a snapshot `revs/<rev>.<ext>`) — a `Touch` per entry. This
generalizes the `ChatRef`/`Origin` provenance prior art (`session.py`) from immutable chat
transcripts to mutable, versioned files.

Contracts honored here (docs/artifact-subsystem-plan.md + the decision record
docs/artifact-plan-questions.md):
  - `ARTIFACT_SCHEMA_VERSION = 1` with a strict `from_dict` boundary, INDEPENDENT of the session
    schema (mirrors `Session.from_dict`): a version mismatch raises `UnsupportedArtifactError`, and
    beyond the version it validates the structural invariants — non-empty history, entry 0 is the
    create (rev 0), rev numbers contiguous from 0 (F2).
  - `updated_at` is DERIVED from `history[-1].at`, never stored (F3 — no drift).
  - `Touch.session_id` may be the `USER_ACTOR` sentinel — a manual/human edit is a first-class touch
    (C1/C2), not a provenance gap.
  - `Touch.rev` is explicit (not positional) so a future annotation touch that produces no rev never
    breaks the history⇄revs coupling (C3).
  - `filename` preserves the source name so revs keep their extension (D1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Bumped only when the on-disk artifact record shape changes. The strict boundary means ANY change —
# a field added, removed, or re-meaned — bumps it; there are no tolerated unknown fields. Independent
# of the session `SCHEMA_VERSION`: artifacts are a separate store with their own version line.
# Planned bumps: none — the artifact schema stays 1 through all planned sequencing steps.
ARTIFACT_SCHEMA_VERSION = 1

# Sentinel actor for a touch made outside any tx session — a manual edit by the human, or a
# `tx artifact` call from a plain terminal (C1/C2). A first-class provenance value, not a gap.
USER_ACTOR = "user"

# The EXACT on-disk key set for each record type. The strict boundary tolerates NO unknown fields
# (Schema/versioning: any field added / removed / re-meaned bumps the version), so `from_dict`
# rejects a record whose keys are not exactly these — extras mean "not ours / a newer shape without a
# version bump", missing keys mean "malformed".
_ARTIFACT_KEYS = frozenset(
    {"artifact_schema_version", "id", "title", "filename", "created_at", "history"}
)
_TOUCH_KEYS = frozenset({"session_id", "at", "rev", "changes"})


class UnsupportedArtifactError(Exception):
    """A persisted record is not a current (v1) artifact record — the `from_dict` boundary guard
    (mirrors `session.UnsupportedRecordError`). Raised on an `artifact_schema_version` mismatch or a
    violated structural invariant. `ArtifactStore.all()` skips such records (one bad file must not
    crash `ls`/`doctor`); `ArtifactStore.load()` lets it propagate (the caller named that record).
    """


@dataclass
class Touch:
    """One entry in an artifact's `history` — a provenance edge AND the revision it produced.

    Every touch is a new snapshot: `rev` names the immutable `revs/<rev>.<ext>` copy this touch
    wrote (explicit, not positional — C3). `session_id` is the tx session id that did it, or the
    `USER_ACTOR` sentinel for a human/non-session edit (C1). `changes` is an optional free-text note
    of what changed — there is no `how`; a touch is just who + when + which snapshot (+ the note).
    """

    session_id: str
    at: float
    rev: int
    changes: str | None = None

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "at": self.at,
            "rev": self.rev,
            "changes": self.changes,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Touch:
        _require_exact_keys(data, _TOUCH_KEYS, "a history entry")
        return cls(
            session_id=data["session_id"],
            at=data["at"],
            rev=data["rev"],
            changes=data["changes"],
        )


@dataclass
class Artifact:
    """A durable, versioned deliverable — the record half; its file(s) live under `artifacts/<id>/`.

    `history` is non-empty (entry 0 is the create) and doubles as the version index: `history[i]`
    produced `revs/<history[i].rev>.<ext>`. `updated_at` is a derived property, never a stored field
    (F3). `filename` is the original source name; its extension is reused for every rev and the
    working copy so nvim filetype / a future mime survive (D1).
    """

    id: str  # uuid, primary key (the record file name)
    title: str | None
    filename: str
    created_at: float
    history: list[Touch] = field(default_factory=list)
    artifact_schema_version: int = ARTIFACT_SCHEMA_VERSION

    @property
    def updated_at(self) -> float:
        """Last-touched time — DERIVED from the final history entry, never stored (F3, no drift)."""
        return self.history[-1].at

    @property
    def extension(self) -> str:
        """The source filename's suffix (`.md`, or `""` when the source had none), reused for every
        `revs/<n><ext>` and `current<ext>` so filetype / a future mime survive (D1)."""
        return Path(self.filename).suffix

    @property
    def latest_rev(self) -> int:
        """The rev the working copy last snapshotted from — the final history entry's rev."""
        return self.history[-1].rev

    def to_dict(self) -> dict:
        """The v1 on-disk shape. `updated_at` is intentionally ABSENT (derived, F3), so
        `from_dict(to_dict())` round-trips exactly through the strict boundary."""
        return {
            "artifact_schema_version": self.artifact_schema_version,
            "id": self.id,
            "title": self.title,
            "filename": self.filename,
            "created_at": self.created_at,
            "history": [touch.to_dict() for touch in self.history],
        }

    @classmethod
    def from_dict(cls, data: dict) -> Artifact:
        """Deserialize a persisted record at the strict boundary (mirrors `Session.from_dict`). A
        non-v1 `artifact_schema_version` raises `UnsupportedArtifactError` — tx-ide does not
        back-migrate older artifact records on load. Beyond the version it validates the F2
        invariants (see `_validate_history`). Within a v1 record the shape is ours, so fields are
        read directly (no defensive defaults — DEVELOPER standard)."""
        version = data.get("artifact_schema_version")
        if version != ARTIFACT_SCHEMA_VERSION:
            raise UnsupportedArtifactError(
                f"record artifact_schema_version={version!r} is unsupported (expected "
                f"{ARTIFACT_SCHEMA_VERSION}); tx-ide does not back-migrate older artifact records"
            )
        _require_exact_keys(data, _ARTIFACT_KEYS, f"artifact {data.get('id')!r}")
        history = [Touch.from_dict(entry) for entry in data["history"]]
        _validate_history(history, data["id"])
        return cls(
            id=data["id"],
            title=data["title"],
            filename=data["filename"],
            created_at=data["created_at"],
            history=history,
            artifact_schema_version=version,
        )


def _require_exact_keys(data: dict, expected: frozenset[str], what: str) -> None:
    """Enforce that `data` carries EXACTLY `expected` keys (the strict boundary tolerates no unknown
    fields — Schema/versioning). A persistence boundary, so this is real validation: extras mean the
    record is not ours (or a newer shape shipped without a version bump), missing keys mean it is
    malformed. Either raises `UnsupportedArtifactError` naming what failed and how."""
    keys = set(data)
    if keys == expected:
        return
    parts = []
    if unexpected := keys - expected:
        parts.append(f"unexpected {sorted(unexpected)}")
    if missing := expected - keys:
        parts.append(f"missing {sorted(missing)}")
    raise UnsupportedArtifactError(f"{what} has an invalid key set: {'; '.join(parts)}")


def _validate_history(history: list[Touch], artifact_id: str) -> None:
    """Enforce the F2 structural invariants on a loaded history — this is a persistence boundary, so
    it is real validation (not an internal defensive check). Two checks: the history must be
    non-empty (entry 0 is the create), and — because a v1 artifact writes exactly one rev per touch —
    the rev sequence must equal `0..len-1` (entry 0 = rev 0, contiguous, no gaps). The empty case is
    called out explicitly because it would otherwise satisfy the sequence check vacuously. Raises
    `UnsupportedArtifactError` naming the artifact on any violation."""
    if not history:
        raise UnsupportedArtifactError(
            f"artifact {artifact_id!r} has an empty history (entry 0 must be the create)"
        )
    revs = [touch.rev for touch in history]
    if revs != list(range(len(history))):
        raise UnsupportedArtifactError(
            f"artifact {artifact_id!r} has non-contiguous rev numbers {revs} "
            f"(expected 0..{len(history) - 1}, one per touch)"
        )
