"""Read-time effort-group resolution (grouping design 73a934a5). Records store only explicit
overrides; the effective group is derived on read:

    resolved(session)  = session.group
                         ?? first override up the parent chain (stops at hubs and cycles)
                         ?? the bound artifact's group (nvim views from `tx artifact open`)
                         ?? tags[0]
                         ?? session.name

    resolved(artifact) = artifact.group
                         ?? resolved(creator = history[0].session_id)
                         ?? newest surviving toucher's resolved group
                         ?? "ungrouped"

Artifacts derive from their CREATOR (later touches never re-file; `USER_ACTOR` skipped). The two
cascades recurse into each other; the one true cycle runs through artifacts, so recursion is
guarded by the artifact ids under resolution. Build one resolver per read over full store
snapshots (`SessionStore.all()` + `ArtifactStore.all()`).
"""

from __future__ import annotations

from .artifact import USER_ACTOR, Artifact
from .session import OtherSession, Session

# Display fallback when no touch author survives. Never feeds back into session resolution.
UNGROUPED = "ungrouped"

# The parent walk stops AT a hub, so one effort's group never leaks through it into unrelated
# work spawned from it. Views are hubs too but are never records, so they end the walk anyway.
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
        """The session's effective group — always non-empty (name is the floor)."""
        return self._session_group(session, resolving_artifacts=set())

    def artifact_group(self, artifact: Artifact) -> str:
        """The artifact's effective group, `UNGROUPED` when nothing survives."""
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
        """First reachable override: own, up the parent chain, or the bound artifact's. Only
        overrides inherit — an ancestor's tags never leak down."""
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
        """Own override, else the creator's FULL resolution, else the newest surviving toucher's.
        A present author always resolves (name floor), so later touchers only matter when the
        creator is the user sentinel or gone."""
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
        """A `parent` value as a record: by id, else by display name (legacy pre-v4 shape). Names
        are reusable, so name matches are era-scoped — no candidate born after the referrer — and
        rank live-then-newest (mirrors `SessionService._resolve_name`)."""
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
