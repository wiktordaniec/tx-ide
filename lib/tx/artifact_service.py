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
import json
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
        group: str | None = None,
    ) -> Artifact:
        """Register a new artifact from `content`: write `revs/0.<ext>` + `current.<ext>`, persist
        the record with one `Touch(rev=0)` (the create), log one line. `filename` preserves the
        source name so revs keep their extension (D1); it defaults to a bare `artifact` when the
        caller has none. `session_id` is the creator — a tx session id or the `USER_ACTOR` sentinel.
        `group` is the optional explicit effort-group override (`--group`); left None the artifact
        groups under its creator's resolved group at read time (grouping.py). A fresh uuid means the
        rev-0 claim never contends, so create cannot conflict."""
        now = time.time()
        artifact = Artifact(
            id=str(uuid.uuid4()),
            title=title,
            filename=filename if filename is not None else "artifact",
            created_at=now,
            group=group,
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
        with self.store.locked(artifact_id):
            artifact = self._require(artifact_id)
            return self._apply_modify(artifact, session_id, content, changes)

    def snapshot_current(
        self, artifact_id: str, session_id: str, *, changes: str | None = None
    ) -> Artifact:
        """Snapshot the CURRENT working copy as the next revision — the no-file `tx artifact modify`
        that closes the loop after editing `current.<ext>` in the nvim view. Reads the working copy
        WITHOUT a read-log line (the read is internal to this mutation), then delegates to the same
        modify body — so an unchanged working copy is the same no-op skip (E3)."""
        with self.store.locked(artifact_id):
            artifact = self._require(artifact_id)
            return self._apply_modify(artifact, session_id, self.files.read_current(artifact), changes)

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

    def set_group(self, artifact_id: str, session_id: str, group: str | None) -> Artifact:
        """Set (or with None clear) the artifact's EXPLICIT effort-group override. Metadata only —
        no rev, no touch (`history` stays the content/version log; `doctor`'s touch⇄log cross-check
        is untouched) — but it IS a mutation, so it logs one line (D8). Runs under the record lock:
        this load→save rewrites the whole record, so unserialized against a concurrent `modify` the
        stale side would revert the other's write — a lost touch (the P0) or a lost override."""
        with self.store.locked(artifact_id):
            artifact = self._require(artifact_id)
            artifact.group = group
            self.store.save(artifact)
        self.log.append(
            "artifact-group",
            f"{artifact.id} {group if group is not None else '(cleared)'}",
            actor=session_id,
        )
        return artifact

    def _write_next_rev(self, artifact: Artifact, rev: int, content: bytes) -> None:
        """Claim `revs/<rev>` exclusively as the linearity lock. `claim_rev` fails `FileExistsError`
        when the slot is already taken — which ALWAYS means this modify lost, so it ALWAYS raises
        `ArtifactConflict` and NEVER overwrites (no record-reload heuristic, B2 amended after QA). At
        claim time a live in-flight writer is indistinguishable from crash debris, so overwriting
        either would be unsafe — overwriting a live claim is a lost touch (the P0). A genuine crash
        orphan is reclaimed OUT OF BAND by `tx artifact doctor --repair` (an explicit, non-racing
        step) which frees the slot, after which a re-read + retry claims it and succeeds."""
        try:
            self.files.claim_rev(artifact, rev, content)
        except FileExistsError:
            raise ArtifactConflict(
                f"artifact {artifact.id}: rev {rev} is already claimed — the artifact moved on (a "
                "concurrent write), or a crashed write left an orphan (`tx artifact doctor --repair` "
                "clears an orphan). Re-read and retry."
            )

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
        path accessor: it neither reads bytes nor logs; the `open` command logs via `opened`."""
        artifact = self._require(artifact_id)
        return str(self.files.current_path(artifact))

    def opened(self, artifact_id: str, session_id: str) -> None:
        """Record that a session opened the artifact's view — read-visibility in the EventLog (G2).
        The open is a read, so it stays OUT of `history` (no rev noise) but IS logged. Called by
        `tx artifact open` once the nvim companion is up and bound."""
        self.log.append("artifact-open", f"{artifact_id} → {session_id}", actor=session_id)

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
        """Invariant check across the store (backs `tx artifact doctor`, read-only). The record is
        authoritative, so this is the Enforcement detection layer — it flags out-of-band drift:
          - orphan rev files not in `history` (crash debris — ignored on read; they BLOCK the next
            modify of that slot until `--repair`, since a claimed slot is never overwritten);
          - history revs whose file is missing on disk;
          - a dirty working copy (`current` differs from the last snapshot — a visible state);
          - a content directory under `artifacts/` with no record (out-of-band creation);
          - a history touch with no matching EventLog mutation line (a SUSPECTED bypass — a truncated
            log is a legitimate cause too, so it is worded as suspicion, not proof).
        Returns human-readable problem lines; an empty list means clean. Reclaim (orphan removal) is
        the separate `repair_orphans` — `doctor` itself never mutates."""
        problems: list[str] = []
        artifacts = self.store.all()
        record_ids = {artifact.id for artifact in artifacts}
        logged = self._logged_mutations()
        for artifact in artifacts:
            for rev in self.files.orphan_revs(artifact):
                problems.append(
                    f"{artifact.id}: orphan rev file {rev} not in history "
                    "(crash debris — ignored on read; blocks that slot until `doctor --repair`)"
                )
            missing = self.files.missing_revs(artifact)
            for rev in missing:
                problems.append(f"{artifact.id}: history rev {rev} has no file on disk")
            problems.extend(self._current_health(artifact, missing))
            for touch in artifact.history:
                if self._mutation_signature(artifact.id, touch.rev) not in logged:
                    problems.append(
                        f"{artifact.id}: rev {touch.rev} touch has no matching EventLog mutation line "
                        "(suspected out-of-band write — a truncated log is also possible)"
                    )
        if self.store.directory.exists():
            for entry in sorted(self.store.directory.iterdir()):
                # Dot-entries are never content: a transient `.{id}.lock` mutex dir (store.locked)
                # or a staging temp must not read as an out-of-band content directory.
                if entry.is_dir() and not entry.name.startswith(".") and entry.name not in record_ids:
                    problems.append(
                        f"{entry.name}: content directory under artifacts/ has no record "
                        "(out-of-band creation — the record is authoritative)"
                    )
        return problems

    def repair_orphans(self) -> list[str]:
        """Remove every orphan rev file — a rev on disk the authoritative record does not reference
        (crash debris). Backs `tx artifact doctor --repair`: it frees an orphaned slot so the next
        `modify` (which would otherwise conflict on it forever) can claim it. An EXPLICIT maintenance
        step — run it when QUIESCENT; it must not race a live `modify` that has just claimed a slot it
        has not yet recorded (which is indistinguishable from an orphan). Returns the removed lines."""
        removed: list[str] = []
        for artifact in self.store.all():
            for rev in self.files.orphan_revs(artifact):
                self.files.remove_rev(artifact, rev)
                removed.append(f"{artifact.id}: removed orphan rev file {rev}")
        return removed

    def _mutation_signature(self, artifact_id: str, rev: int) -> tuple:
        """The EventLog signature a touch at `rev` should carry: a create (rev 0) or a modify (rev n).
        `doctor` cross-checks each `history` touch against `_logged_mutations`."""
        return ("create", artifact_id) if rev == 0 else ("modify", artifact_id, rev)

    def _logged_mutations(self) -> set:
        """The set of mutation signatures present in the EventLog — `("create", id)` from an
        `artifact-create` line and `("modify", id, rev)` from an `artifact-modify` line. Tolerant of
        a truncated / malformed log: a diagnostic must not itself crash on the very corruption it
        looks for, so an unparsable line is skipped."""
        signatures: set = set()
        if not self.log.path.exists():
            return signatures
        for line in self.log.path.read_text().splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind, fields = entry.get("type"), entry.get("msg", "").split()
            if kind == "artifact-create" and fields:
                signatures.add(("create", fields[0]))
            elif kind == "artifact-modify" and len(fields) >= 2 and fields[1].startswith("rev"):
                try:
                    signatures.add(("modify", fields[0], int(fields[1][3:])))
                except ValueError:
                    continue
        return signatures

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

    def resolve_id(self, token: str) -> str:
        """Resolve a full id OR a unique id prefix to a full artifact id (CLI convenience — the
        one place a caller passes a possibly-partial id by hand). An exact record wins immediately;
        otherwise match by prefix across the store — zero matches -> not found, more than one ->
        ambiguous."""
        if self.store.load(token) is not None:
            return token
        matches = [artifact.id for artifact in self.store.all() if artifact.id.startswith(token)]
        if not matches:
            raise ArtifactNotFound(f"artifact '{token}' not found")
        if len(matches) > 1:
            raise ArtifactError(
                f"artifact id prefix '{token}' is ambiguous ({len(matches)} matches) — use more characters"
            )
        return matches[0]

    def _require(self, artifact_id: str) -> Artifact:
        artifact = self.store.load(artifact_id)
        if artifact is None:
            raise ArtifactNotFound(f"artifact '{artifact_id}' not found")
        return artifact
