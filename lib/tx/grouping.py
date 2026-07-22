"""Read-time effort-group resolution for sessions AND artifacts (grouping design 73a934a5).

Derivations are never stored — records persist only the explicit `group` override — so the
effective group is computed here, on read, from the record graph:

    resolved(session)  = session.group
                         ?? first explicit group up the parent chain (cycle-guarded; the walk
                            STOPS at a hub — hubs isolate efforts, nothing inherits through them)
                         ?? the bound artifact's group (an nvim view opened via `tx artifact
                            open` files exactly where its artifact files)
                         ?? tags[0]
                         ?? session.name

    resolved(artifact) = artifact.group
                         ?? resolved(creator = history[0].session_id)
                         ?? newest surviving toucher's resolved group
                         ?? "ungrouped"

The artifact derives from its CREATOR, not "whoever modified it" — later touches from other
efforts must not re-file it; `USER_ACTOR` touches are skipped. The two cascades are mutually
recursive (a view consults its bound artifact; an artifact consults its authors), and the one true
cycle runs through artifacts (session → artifact → toucher session → same artifact), so recursion
is guarded by the set of artifact ids currently being resolved — never by excluding sessions,
whose tags/name floors must stay consultable from every rung.

One `GroupResolver` is built per read over full store snapshots (`SessionStore.all()` +
`ArtifactStore.all()`) — the same read-everything-per-request pattern every consumer already
follows — and resolves any number of records off its in-memory maps.
"""

from __future__ import annotations

from .artifact import USER_ACTOR, Artifact
from .session import OtherSession, Session

# The artifact-side terminal fallback: every touch author is gone (or the user sentinel) and no
# override was ever set. Display-only — it never feeds back into session resolution.
UNGROUPED = "ungrouped"

# Hub sessions are infrastructure, not lineage: the parent walk stops AT one, so an effort's group
# never leaks through a hub into unrelated work spawned from it (the assistant itself may carry a
# grouped parent — whoever first spawned it). Name-keyed, the same identification the dashboards
# settled on for the standing orchestrator; views are hubs too but are never records, so their
# absence from the store already ends the walk.
HUB_SESSION_NAMES = frozenset({"tx-assistant"})


class GroupResolver:
    def __init__(self, sessions: list[Session], artifacts: list[Artifact]):
        self._sessions_by_id: dict[str, Session] = {session.id: session for session in sessions}
        self._sessions_by_name: dict[str, list[Session]] = {}
        for session in sessions:
            self._sessions_by_name.setdefault(session.name, []).append(session)
        self._artifacts_by_id: dict[str, Artifact] = {artifact.id: artifact for artifact in artifacts}

    # ----- public ---------------------------------------------------------------------------

    def session_group(self, session: Session) -> str:
        """The session's effective group — the cascade above; always a non-empty string (the
        name rung is the floor, so an untagged, unparented session groups alone)."""
        return self._session_group(session, resolving_artifacts=set())

    def artifact_group(self, artifact: Artifact) -> str:
        """The artifact's effective group — its own override, else its creator lineage, else
        the `UNGROUPED` display fallback."""
        derived = self._artifact_override(artifact, resolving_artifacts=set())
        return derived if derived is not None else UNGROUPED

    # ----- the cascades ---------------------------------------------------------------------

    def _session_group(self, session: Session, resolving_artifacts: set[str]) -> str:
        derived = self._session_override(session, resolving_artifacts)
        if derived is not None:
            return derived
        if session.tags:
            return session.tags[0]
        return session.name

    def _session_override(self, session: Session, resolving_artifacts: set[str]) -> str | None:
        """The first explicit group visible from `session`: its own, one up the parent chain, or
        its bound artifact's resolution. None when no override is reachable — the caller falls
        through to the session's own tags[0]/name floor. Two hard stops in the walk: a CYCLE
        (visited ids) and a HUB (an ancestor's tags never leak down — only explicit overrides
        inherit — and a hub's whole lineage is fenced off so efforts never bleed through it)."""
        if session.group:
            return session.group
        visited = {session.id}
        if session.name not in HUB_SESSION_NAMES:
            ancestor = self._resolve_reference(session.parent, referrer=session)
            while ancestor is not None and ancestor.id not in visited:
                if ancestor.group:
                    return ancestor.group
                if ancestor.name in HUB_SESSION_NAMES:
                    break
                visited.add(ancestor.id)
                ancestor = self._resolve_reference(ancestor.parent, referrer=ancestor)
        artifact = (
            self._artifacts_by_id.get(session.artifact_id)
            if isinstance(session, OtherSession)
            else None
        )
        if artifact is not None and artifact.id not in resolving_artifacts:
            return self._artifact_override(artifact, resolving_artifacts)
        return None

    def _artifact_override(self, artifact: Artifact, resolving_artifacts: set[str]) -> str | None:
        """The artifact's derivable group: its own override, else the CREATOR's full session
        resolution, else the newest surviving toucher's. A present author always resolves (the
        name floor), so later touchers are consulted only when the creator is the user sentinel or
        gone from the store — later touches never re-file a creator-resolved artifact. Marking
        this artifact as in-resolution breaks the one true recursion cycle (a toucher that is an
        nvim view bound back to this same artifact)."""
        if artifact.group:
            return artifact.group
        resolving_artifacts = resolving_artifacts | {artifact.id}
        creator_first = [artifact.history[0], *reversed(artifact.history[1:])]
        for touch in creator_first:
            author = self._sessions_by_id.get(touch.session_id)
            if touch.session_id == USER_ACTOR or author is None:
                continue
            return self._session_group(author, resolving_artifacts)
        return None

    def _resolve_reference(self, reference: str | None, referrer: Session) -> Session | None:
        """A `parent` value as a record: by id first (a process executor / a chat-op source),
        then by display name — a LEGACY shape (pre-v4 records; new spawns persist only session
        ids or nothing). Names are reusable, so a name written into `referrer` can only have
        meant a session that already existed when the referrer was born: later same-named
        records are excluded, or a fresh spawn would retroactively capture old lineage and
        re-file its descendants. Among the era-valid candidates, prefer live then newest
        (mirrors `SessionService._resolve_name`). An unknown name — e.g. a dead view's —
        resolves to nothing, which is exactly how the walk ends below a legacy view-hosted
        root."""
        if reference is None:
            return None
        by_id = self._sessions_by_id.get(reference)
        if by_id is not None:
            return by_id
        born = referrer.created_at
        candidates = [
            candidate
            for candidate in self._sessions_by_name.get(reference, [])
            if born is None or (candidate.created_at or 0.0) <= born
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda session: (session.is_alive(), session.created_at or 0.0))
