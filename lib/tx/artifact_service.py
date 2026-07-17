"""`ArtifactService` — the use-case core for the artifact subsystem (Plan 2).

Parallels `SessionService`: it composes `ArtifactStore` (records) + `ArtifactContent` (files) +
`EventLog`, and **every mutation logs exactly one line** (the D8 chokepoint, mirrored). It is a
SEPARATE service from `SessionService` — artifacts are their own store; the one touch-point with
sessions is the `artifact_id` back-link added in sequencing step 3.

Provenance is bidirectional and both directions are first-class:
  - artifact -> sessions: straight off `artifact.history` (the writers).
  - session -> artifacts: `artifacts_for_session` — a reverse lookup BY QUERY across the store, not
    denormalized onto the session record (keeps session writes non-chatty, the same aversion as
    `SessionService.live_sessions`).

Content is bytes-clean: `create` / `modify` accept any bytes (no type/size gate, settled), `content`
returns bytes, and only `diff` requires utf-8 (it decodes both revs and refuses cleanly otherwise).
Mutations log; `content` reads and (step 3) `open` log too — read-visibility WITHOUT polluting the
version history (G2). Cross-store queries (`artifacts_for_session`, `doctor`) do not log — they are
list reads, like `tx ls`.
"""

from __future__ import annotations

import difflib
import time
import uuid

from .artifact import Artifact, Touch
from .artifact_store import ArtifactContent, ArtifactStore
from .events import EventLog
from .service import ServiceError


class ArtifactError(ServiceError):
    """An artifact use-case precondition failed. Subclasses `ServiceError` so the existing CLI
    boundary (`except (ServiceError, TmuxError)`) surfaces it as a message + exit 1 with no
    special-casing."""


class ArtifactNotFound(ArtifactError):
    pass


class ArtifactConflict(ArtifactError):
    """A `modify` lost the linearity race — the rev slot it targeted was claimed by a concurrent,
    committed modify. The caller must re-read the artifact and retry against the new head."""


class ArtifactService:
    def __init__(
        self,
        store: ArtifactStore | None = None,
        content: ArtifactContent | None = None,
        log: EventLog | None = None,
    ):
        self.store = store if store is not None else ArtifactStore()
        self.files = content if content is not None else ArtifactContent()
        self.log = log if log is not None else EventLog()

    # ----- mutations -----------------------------------------------------------------------

    def create(
        self,
        session_id: str,
        content: bytes,
        *,
        title: str | None = None,
        filename: str | None = None,
    ) -> Artifact:
        """Register a new artifact from `content`: write `revs/0.<ext>` + `current.<ext>`, persist
        the record with one `Touch(rev=0)` (the create), log one line. `filename` preserves the
        source name so revs keep their extension (D1); it defaults to a bare `artifact` when the
        caller has none. `session_id` is the creator — a tx session id or the `USER_ACTOR` sentinel.
        A fresh uuid means the rev-0 claim never contends, so create cannot conflict."""
        now = time.time()
        artifact = Artifact(
            id=str(uuid.uuid4()),
            title=title,
            filename=filename if filename is not None else "artifact",
            created_at=now,
            history=[Touch(session_id=session_id, at=now, rev=0, changes=None)],
        )
        self.files.claim_rev(artifact, 0, content)
        self.files.write_current(artifact, content)
        self.store.save(artifact)
        self.log.append("artifact-create", f"{artifact.id} {artifact.filename}", actor=session_id)
        return artifact

    def modify(
        self,
        artifact_id: str,
        session_id: str,
        content: bytes,
        *,
        changes: str | None = None,
    ) -> Artifact:
        """Snapshot `content` as the artifact's next revision and append a touch. Loads the
        authoritative record, then applies the modify. Content identical to the last rev is a no-op —
        no rev, no touch (E3); the CLI prints the notice. Returns the (possibly unchanged) artifact.
        """
        artifact = self._require(artifact_id)
        return self._apply_modify(artifact, session_id, content, changes)

    def _apply_modify(
        self, artifact: Artifact, session_id: str, content: bytes, changes: str | None
    ) -> Artifact:
        """The modify body, taking an already-loaded `artifact` so a test can drive two stale copies
        through it to exercise the concurrency race. No-op guard first (identical to the last rev),
        then claim the next rev slot as the linearity lock (`_write_next_rev` distinguishes a lost
        race from a crash orphan), refresh `current`, append the touch, save the authoritative record
        LAST, log one line."""
        if content == self.files.read_rev(artifact, artifact.latest_rev):
            return artifact  # no-op: identical to the last snapshot (E3)
        next_rev = artifact.latest_rev + 1
        self._write_next_rev(artifact, next_rev, content)
        self.files.write_current(artifact, content)
        now = time.time()
        artifact.history.append(Touch(session_id=session_id, at=now, rev=next_rev, changes=changes))
        self.store.save(artifact)
        self.log.append("artifact-modify", f"{artifact.id} rev{next_rev}", actor=session_id)
        return artifact

    def _write_next_rev(self, artifact: Artifact, rev: int, content: bytes) -> None:
        """Claim `revs/<rev>` exclusively; on collision, distinguish a lost race from a crash orphan.
        `claim_rev` fails `FileExistsError` when the slot is taken. Reloading the authoritative
        record tells the two apart: if it has advanced to reference `rev`, a concurrent modify
        committed here -> `ArtifactConflict`; if it is unchanged, the file is an orphan a crash left
        (the record never referenced it) -> overwrite it and proceed (plan test 6, both branches).
        The residual window — a live concurrent claimer that has linked but not yet saved — is the
        accepted 'concurrent editing stays rare' tradeoff the plan makes; the record-save follows the
        claim immediately, so the window is microseconds."""
        try:
            self.files.claim_rev(artifact, rev, content)
        except FileExistsError:
            head = self.store.load(artifact.id)
            if head is not None and head.latest_rev >= rev:
                raise ArtifactConflict(
                    f"artifact {artifact.id} moved on (rev {rev} already committed) — re-read and retry"
                )
            self.files.overwrite_rev(artifact, rev, content)

    # ----- reads ---------------------------------------------------------------------------

    def content(self, artifact_id: str, rev: int | None = None) -> bytes:
        """The artifact's bytes: the working copy by default (what `open` edits and a reader wants),
        or a specific immutable snapshot when `rev` is given — one reader, not
        latest_content+revision_content. Logs a read-visibility line; reads stay OUT of `history`
        (no rev noise) but ARE visible in the EventLog (G2)."""
        artifact = self._require(artifact_id)
        data = (
            self.files.read_current(artifact)
            if rev is None
            else self.files.read_rev(artifact, rev)
        )
        self.log.append(
            "artifact-read", f"{artifact_id} {'current' if rev is None else f'rev{rev}'}"
        )
        return data

    def content_path(self, artifact_id: str) -> str:
        """The `current.<ext>` path — what `tx artifact open` hands nvim (never a frozen rev). A pure
        path accessor: it neither reads bytes nor logs; the `open` command logs its own line."""
        artifact = self._require(artifact_id)
        return str(self.files.current_path(artifact))

    def diff(self, artifact_id: str, rev_a: int | None = None, rev_b: int | None = None) -> str:
        """A unified `difflib` diff between two revisions (default: the last two). We store versions,
        not diffs, so this is computed on demand. Refuses cleanly when either rev is not utf-8 text —
        content is bytes-clean, but a textual diff is not meaningful over binary."""
        artifact = self._require(artifact_id)
        left_rev, right_rev = self._default_diff_revs(artifact, rev_a, rev_b)
        left = self._decoded_rev(artifact, left_rev)
        right = self._decoded_rev(artifact, right_rev)
        self.log.append("artifact-diff", f"{artifact_id} rev{left_rev}..rev{right_rev}")
        return "".join(
            difflib.unified_diff(
                left.splitlines(keepends=True),
                right.splitlines(keepends=True),
                fromfile=f"rev{left_rev}",
                tofile=f"rev{right_rev}",
            )
        )

    def artifacts_for_session(self, session_id: str) -> list[Artifact]:
        """Every artifact `session_id` created or touched — the session->artifacts reverse lookup, by
        query across the store (NOT denormalized onto the session record; keeps session writes
        non-chatty, mirroring `SessionService.live_sessions`)."""
        return self.store.query(
            lambda artifact: any(touch.session_id == session_id for touch in artifact.history)
        )

    # ----- diagnostics ---------------------------------------------------------------------

    def doctor(self) -> list[str]:
        """Invariant check across the store (backs `tx artifact doctor`, run in tests). The record is
        authoritative, so this flags out-of-band drift: orphan rev files not in `history` (crash
        orphans — ignored on read), history revs missing their file, and a working copy that differs
        from the last snapshot (a dirty `current` — a visible state, not an error). Returns
        human-readable problem lines; an empty list means clean."""
        problems: list[str] = []
        for artifact in self.store.all():
            for rev in self.files.orphan_revs(artifact):
                problems.append(
                    f"{artifact.id}: orphan rev file {rev} not in history "
                    "(crash orphan — ignored on read, overwritten by the next modify)"
                )
            missing = self.files.missing_revs(artifact)
            for rev in missing:
                problems.append(f"{artifact.id}: history rev {rev} has no file on disk")
            problems.extend(self._current_health(artifact, missing))
        return problems

    def _current_health(self, artifact: Artifact, missing_revs: list[int]) -> list[str]:
        """The working-copy line for `doctor`: dirty (differs from the last snapshot), or a missing
        `current` file. Skipped when the last rev's own file is missing (that corruption is already
        reported) so the dirty comparison does not raise on it."""
        if artifact.latest_rev in missing_revs:
            return []
        if not self.files.current_path(artifact).exists():
            return [f"{artifact.id}: working copy current{artifact.extension} is missing"]
        if self.files.current_is_dirty(artifact):
            return [
                f"{artifact.id}: working copy differs from last rev {artifact.latest_rev} "
                "(dirty — un-snapshotted edits; close with `tx artifact modify`)"
            ]
        return []

    # ----- resolution ----------------------------------------------------------------------

    def _default_diff_revs(
        self, artifact: Artifact, rev_a: int | None, rev_b: int | None
    ) -> tuple[int, int]:
        """Resolve the diff endpoints: an explicit pair, or the last two revs by default. A
        single-rev artifact has nothing to diff against."""
        if rev_a is not None and rev_b is not None:
            return rev_a, rev_b
        if artifact.latest_rev == 0:
            raise ArtifactError(f"artifact {artifact.id} has only one revision — nothing to diff")
        return artifact.latest_rev - 1, artifact.latest_rev

    def _decoded_rev(self, artifact: Artifact, rev: int) -> str:
        try:
            return self.files.read_rev(artifact, rev).decode("utf-8")
        except FileNotFoundError:
            raise ArtifactError(f"artifact {artifact.id} has no revision {rev}")
        except UnicodeDecodeError:
            raise ArtifactError(
                f"artifact {artifact.id} revision {rev} is not utf-8 text — cannot diff"
            )

    def _require(self, artifact_id: str) -> Artifact:
        artifact = self.store.load(artifact_id)
        if artifact is None:
            raise ArtifactNotFound(f"artifact '{artifact_id}' not found")
        return artifact
