"""Manual `tx sync` over the `Storage` copy boundary (tx-service-redesign.md §14, stage S7).

Local (`$TX_IDE_HOME` via `LocalStorage`) is ALWAYS the working set; sync is a manual archive
layer that mirrors the **reproducible corpus** — records (`sessions/`), history bundles
(`history/`), the provenance log (`log.jsonl`), and `config.json` — to/from a remote `Storage`.
Enough to rebuild the tx world on a fresh machine. NEVER on the hot path (§14): live ops hit
`SessionStore`/`EventLog` on local directly; nothing here runs unless the user types `tx sync`.

Conflict policy (§14): **last-writer-wins per uuid-sharded record key** by `ended_at` /
`last_activity`. Records are the only keys that can be edited independently on both sides, so a
record is overwritten only when the source copy is at least as new as the destination copy — a
newer record on the destination survives. History bundles are immutable once ingested and the log
is append-only, so for everything else the sync *direction* (push = local→remote, pull =
remote→local) decides. Sync is additive: it never deletes a key from the other side (no tombstones).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .storage import (
    LocalStorage,
    S3Storage,
    Storage,
    config_path,
    tx_ide_home,
)

# The reproducible corpus (§14): these prefixes + files, and nothing else under $TX_IDE_HOME. The
# `agents` symlink, `user-agents/`, and the baked `hooks/` shims are install artifacts, not state —
# they are rebuilt by the installer, so sync leaves them out.
CORPUS_PREFIXES = ("sessions/", "history/")
CORPUS_FILES = ("log.jsonl", "config.json")


def is_corpus_key(key: str) -> bool:
    return key.startswith(CORPUS_PREFIXES) or key in CORPUS_FILES


def is_record_key(key: str) -> bool:
    """A uuid-sharded record (`sessions/<uuid>.json`) — the only last-writer-wins key."""
    return key.startswith("sessions/") and key.endswith(".json")


def corpus_keys(storage: Storage) -> list[str]:
    """The corpus keys present in `storage`, sorted (filters out non-corpus install artifacts)."""
    return [key for key in storage.list() if is_corpus_key(key)]


def _record_clock(data: bytes) -> float:
    """The last-writer-wins clock for a record: `ended_at`, else `last_activity`, else
    `created_at` (a terminal record outranks a live one with the same activity; a fresh record
    with no activity yet still sorts by creation)."""
    record = json.loads(data)
    return record.get("ended_at") or record.get("last_activity") or record.get("created_at") or 0.0


@dataclass
class SyncResult:
    """The outcome of one `push`/`pull` direction over the corpus."""

    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)  # destination record was newer — LWW kept it


def _verdict(key: str, data: bytes, destination: Storage) -> str:
    """How copying `key` (carrying `data`) onto `destination` resolves — the single per-key rule
    shared by the live copy and the dry-count:

    - ``"add"``       — absent on the destination.
    - ``"unchanged"`` — present with identical bytes.
    - ``"kept"``      — a record the destination holds a strictly newer copy of (last-writer-wins).
    - ``"update"``    — differs and the source wins (any non-record, or a record at least as new).
    """
    if not destination.exists(key):
        return "add"
    current = destination.get(key)
    if current == data:
        return "unchanged"
    if is_record_key(key) and _record_clock(data) < _record_clock(current):
        return "kept"
    return "update"


def _copy_direction(source: Storage, destination: Storage) -> SyncResult:
    """Copy the corpus from `source` to `destination`, honoring per-record last-writer-wins.

    A differing record is overwritten only when the source clock is at least as new as the
    destination clock (otherwise the destination's newer copy is kept); every other differing key
    is overwritten (history bundles are immutable, the log is append-only, so direction decides)."""
    result = SyncResult()
    for key in corpus_keys(source):
        data = source.get(key)
        verdict = _verdict(key, data, destination)
        if verdict in ("add", "update"):
            destination.put(key, data)
        getattr(result, {"add": "added", "update": "updated"}.get(verdict, verdict)).append(key)
    return result


def sync_diff(source: Storage, destination: Storage) -> int:
    """How many corpus keys `_copy_direction(source, destination)` would add or update — a dry
    count (no writes) for `tx sync status`, using the same per-key `_verdict` rule."""
    return sum(
        _verdict(key, source.get(key), destination) in ("add", "update")
        for key in corpus_keys(source)
    )


def sync_push(local: Storage, remote: Storage) -> SyncResult:
    """Mirror the local corpus up to `remote` (local→remote; LWW keeps a newer remote record)."""
    return _copy_direction(local, remote)


def sync_pull(local: Storage, remote: Storage) -> SyncResult:
    """Mirror the remote corpus down to `local` (remote→local; LWW keeps a newer local record)."""
    return _copy_direction(remote, local)


@dataclass
class CorpusStatus:
    """A snapshot of one storage's corpus, for `tx sync status`."""

    records: int
    history_files: int
    has_log: bool
    has_config: bool

    @property
    def total(self) -> int:
        return self.records + self.history_files + int(self.has_log) + int(self.has_config)


def corpus_status(storage: Storage) -> CorpusStatus:
    keys = corpus_keys(storage)
    return CorpusStatus(
        records=sum(1 for key in keys if is_record_key(key)),
        history_files=sum(1 for key in keys if key.startswith("history/")),
        has_log="log.jsonl" in keys,
        has_config="config.json" in keys,
    )


def remote_from_config() -> Storage | None:
    """Resolve the remote backend from `config.json`'s `sync` section, or `None` if unconfigured.

    Reading config is a system boundary — a missing file or absent `sync` section just means "no
    remote configured" (the common case until the user sets one up). Shape:

        {"sync": {"backend": "s3",    "bucket": "my-bucket", "prefix": "tx/"}}
        {"sync": {"backend": "local", "path":   "~/tx-archive"}}
    """
    path = config_path()
    if not path.exists():
        return None
    sync_config = json.loads(path.read_text()).get("sync")
    if not sync_config:
        return None
    return remote_from_spec(sync_config)


def remote_from_spec(spec: dict) -> Storage:
    """Build a remote `Storage` from a `sync`-config dict (also the `--remote`/`--s3` CLI path)."""
    backend = spec["backend"]
    if backend == "s3":
        return S3Storage(spec["bucket"], spec.get("prefix", ""))
    if backend == "local":
        return LocalStorage(Path(spec["path"]).expanduser())
    raise ValueError(f"unknown sync backend '{backend}' (expected 's3' or 'local')")


def remote_label(remote: Storage) -> str:
    """A short human label for a remote `Storage` (for status / summary lines)."""
    if isinstance(remote, S3Storage):
        suffix = f"/{remote.prefix}" if remote.prefix else ""
        return f"s3://{remote.bucket}{suffix}"
    if isinstance(remote, LocalStorage):
        return str(remote.root)
    return remote.__class__.__name__


def local_storage() -> LocalStorage:
    """The local working-set corpus — `LocalStorage` rooted at `$TX_IDE_HOME`."""
    return LocalStorage(tx_ide_home())
