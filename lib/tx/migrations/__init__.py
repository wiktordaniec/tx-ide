from __future__ import annotations

import json
from pathlib import Path

from ..artifact import ARTIFACT_SCHEMA_VERSION, Artifact, UnsupportedArtifactError
from ..artifact_store import ArtifactStore
from ..session import SCHEMA_VERSION, Session, UnsupportedRecordError
from ..store import SessionStore
from ..tmux import Tmux

# `tx migrate` — the explicit, idempotent migrator that upgrades an older `$TX_IDE_HOME` session
# store to the CURRENT schema, chaining every intermediate step in ONE run. A record is re-saved
# through `Session.from_dict` + each subtype's `to_dict`, which canonicalizes it directly to the
# current shape regardless of how old the source is: from_dict reads only the keys it needs (extra
# older keys are dropped, genuinely-new fields default via `.get()`) and the current serializer
# emits exactly the current shape. So one from_dict+to_dict does the whole chain — no per-version
# intermediate pass.
#   - v3 -> current: a PROCESS record (v3 `kind` != "view") canonicalizes — the dead "kind" key
#     drops, a non-llm record sheds the now-absent `engine`/`chats`/`last_activity` keys, an llm
#     record gains `turn_started_at`, a non-llm record gains a null `artifact_id`, every record gains
#     a null `group`, and `schema_version` becomes the current one. A VIEW record (v3 `kind` ==
#     "view") leaves the store entirely: its durable
#     identity becomes the live `@tx_view` tmux marker, so migration stamps that marker on the
#     matching live tmux session (record name <-> tmux name — views are human-named) and DELETES the
#     record file. Deletion is mandatory and ordering-sensitive: left behind, the role-dispatching
#     factory would load a view as a terminal OtherSession and pollute `tx history` with phantoms.
#   - v4 -> v5: a v4 record (no `kind`, no view records) simply gains the nullable `artifact_id` on a
#     non-llm record; everything else is already current.
#   - v5 -> v6: every record gains the nullable `group` override on the shared base (grouping design
#     73a934a5) — an add-default step, no records leave the store.
# The loader refuses any non-current schema (no auto-upgrade-on-load), so pre-v3 records must be
# upgraded out-of-band first. `tx migrate` also runs `migrate_artifacts` (below) — the artifact store
# has its own version line with the same strict boundary, so its v1 -> v2 step (add the null `group`)
# rides the same deploy. SAFETY: run ONCE at deploy against the real $TX_IDE_HOME; a newer checkout
# must never migrate a live older home (it would brick the running crew). Sandbox run:
# `TX_IDE_HOME=$(mktemp -d) tx migrate`.

_UPGRADABLE_FROM = frozenset({3, 4, 5})  # source versions the migrator chains to the current schema
_ARTIFACT_UPGRADABLE_FROM = frozenset({1})
_VIEW_KIND = "view"


def _skip_reason(raw: dict) -> str:
    version = raw.get("schema_version")
    if version == SCHEMA_VERSION:
        return f"already v{SCHEMA_VERSION}"
    return f"not an upgradable record (schema_version={version!r})"


def migrate_sessions(
    directory: Path, tmux: Tmux | None = None
) -> tuple[list[str], list[str], list[tuple[str, str]]]:
    """Migrate every upgradable record under `directory` to the CURRENT schema in place, chaining
    intermediate versions in one pass; return (migrated, views_removed, skipped). The target is
    explicit (not `sessions_dir()`) and `tmux` is injectable so a test can point at a temp fixture +
    a fake tmux. Idempotent: a current record is skipped, and a view whose record was already deleted
    is simply absent on a re-run. A single unreadable/malformed file is skipped with its error rather
    than aborting the run."""
    tmux = tmux if tmux is not None else Tmux()
    store = SessionStore(directory=directory)
    migrated: list[str] = []
    views_removed: list[str] = []
    skipped: list[tuple[str, str]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            with open(path) as handle:
                raw = json.load(handle)
            if raw.get("schema_version") not in _UPGRADABLE_FROM:
                skipped.append((path.name, _skip_reason(raw)))
                continue
            if raw.get("kind") == _VIEW_KIND:
                # A view leaves the store: stamp its live tmux session with @tx_view (name-matched,
                # since views are human-named) so nest-attach + border chrome keep working through
                # the cutover, then delete the record. A dead view (no live session) is just deleted.
                if tmux.has_session(raw["name"]):
                    tmux.set_tx_view(raw["name"])
                path.unlink()
                views_removed.append(raw["name"])
                continue
            # A process record: re-save through the current factory + serializer, which drops any
            # dead older keys, defaults genuinely-new fields, and stamps the current schema_version
            # (see the module note) — a v3 or a v4 source lands on the current shape identically.
            store.save(Session.from_dict({**raw, "schema_version": SCHEMA_VERSION}))
        # AttributeError / TypeError cover structurally malformed JSON — a non-object record (a
        # bare list/string parses fine but has no `.get`) or a null where a list belongs — so one
        # corrupt file is skipped with its error, per the contract, instead of aborting the run.
        except (
            OSError, json.JSONDecodeError, AttributeError, TypeError, KeyError, ValueError,
            UnsupportedRecordError,
        ) as error:
            skipped.append((path.name, f"{type(error).__name__}: {error}"))
            continue
        migrated.append(path.name)
    return migrated, views_removed, skipped


def _artifact_skip_reason(raw: dict) -> str:
    version = raw.get("artifact_schema_version")
    if version == ARTIFACT_SCHEMA_VERSION:
        return f"already v{ARTIFACT_SCHEMA_VERSION}"
    return f"not an upgradable artifact record (artifact_schema_version={version!r})"


def migrate_artifacts(directory: Path) -> tuple[list[str], list[tuple[str, str]]]:
    """Migrate every upgradable artifact record under `directory` to the CURRENT artifact schema in
    place; return (migrated, skipped). v1 -> v2 is an add-default step: the record gains a null
    `group` and the version stamp. The strict `Artifact.from_dict` tolerates no missing key, so the
    default is injected BEFORE the boundary re-validates the record and the current serializer
    writes it back. Same discipline as `migrate_sessions`: explicit target directory, idempotent
    (a current record is skipped), and one bad file is skipped with its error, never fatal."""
    store = ArtifactStore(directory=directory)
    migrated: list[str] = []
    skipped: list[tuple[str, str]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            with open(path) as handle:
                raw = json.load(handle)
            if raw.get("artifact_schema_version") not in _ARTIFACT_UPGRADABLE_FROM:
                skipped.append((path.name, _artifact_skip_reason(raw)))
                continue
            store.save(Artifact.from_dict({
                **raw,
                "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
                "group": raw.get("group"),
            }))
        # Same malformed-shape tolerance as migrate_sessions: AttributeError / TypeError catch a
        # non-object record or a null field where the boundary walks a list.
        except (
            OSError, json.JSONDecodeError, AttributeError, TypeError, KeyError, ValueError,
            UnsupportedArtifactError,
        ) as error:
            skipped.append((path.name, f"{type(error).__name__}: {error}"))
            continue
        migrated.append(path.name)
    return migrated, skipped
