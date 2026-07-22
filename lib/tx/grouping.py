"""Read-time effort-group resolution for sessions AND artifacts (grouping design 73a934a5).

Derivations are never stored — records persist only the explicit `group` override — so the
effective group is computed here, on read, from the record graph:

    resolved(session)  = session.group
                         ?? first explicit group up the parent chain (cycle-guarded; hub
                            sessions carry none by convention, so nothing inherits from them)
                         ?? the bound artifact's group (an nvim view opened via `tx artifact
                            open` files where its artifact files)
                         ?? tags[0]
                         ?? session.name

    resolved(artifact) = artifact.group
                         ?? resolved(creator = history[0].session_id)
                         ?? newest surviving toucher's resolved group
                         ?? "ungrouped"

The artifact derives from its CREATOR, not "whoever modified it" — later touches from other
efforts must not re-file it; `USER_ACTOR` touches are skipped. Both walks are mutually
recursive (session → bound artifact → creator session), so one module owns both with a shared
visited set of session ids as the single cycle guard.

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
        derived = self._session_override(session, visited=set())
        if derived is not None:
            return derived
        if session.tags:
            return session.tags[0]
        return session.name

    def artifact_group(self, artifact: Artifact) -> str:
        """The artifact's effective group — its own override, else its creator lineage, else
        the `UNGROUPED` display fallback."""
        derived = self._artifact_override(artifact, visited=set())
        return derived if derived is not None else UNGROUPED

    # ----- the override walks (nullable — the callers append their own display floors) -------

    def _session_override(self, session: Session, visited: set[str]) -> str | None:
        """The first explicit group visible from `session`: its own, one up the parent chain,
        or its bound artifact's resolution. None when no override is reachable — the caller
        falls through to the session's own tags[0]/name floor (an ancestor's tags never leak
        down; only explicit overrides inherit, which is what keeps hubs inert)."""
        if session.group:
            return session.group
        visited.add(session.id)
        ancestor = self._resolve_reference(session.parent)
        while ancestor is not None and ancestor.id not in visited:
            if ancestor.group:
                return ancestor.group
            visited.add(ancestor.id)
            ancestor = self._resolve_reference(ancestor.parent)
        if isinstance(session, OtherSession) and session.artifact_id in self._artifacts_by_id:
            return self._artifact_override(self._artifacts_by_id[session.artifact_id], visited)
        return None

    def _artifact_override(self, artifact: Artifact, visited: set[str]) -> str | None:
        """The artifact's derivable group: its own override, else the creator's FULL session
        resolution, else the newest surviving toucher's. Touch authors that are the user
        sentinel, already on the visited walk (recursion guard), or gone from the store
        contribute nothing. None when nothing survives."""
        if artifact.group:
            return artifact.group
        creator_first = [artifact.history[0], *reversed(artifact.history[1:])]
        for touch in creator_first:
            author = self._sessions_by_id.get(touch.session_id)
            if touch.session_id == USER_ACTOR or author is None or author.id in visited:
                continue
            derived = self._session_override(author, visited)
            if derived is not None:
                return derived
            if author.tags:
                return author.tags[0]
            return author.name
        return None

    def _resolve_reference(self, reference: str | None) -> Session | None:
        """A `parent` value as a record: by id first (a process executor / a chat-op source),
        then by display name (a pre-v4 record; live-preferred, then newest — mirrors
        `SessionService._resolve_name`). A view name resolves to nothing (views are not
        records), which is exactly how the walk ends below a view-hosted root."""
        if reference is None:
            return None
        by_id = self._sessions_by_id.get(reference)
        if by_id is not None:
            return by_id
        candidates = self._sessions_by_name.get(reference)
        if not candidates:
            return None
        return max(candidates, key=lambda session: (session.is_alive(), session.created_at or 0.0))
