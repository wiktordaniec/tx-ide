"""History ingest + the cross-project transcript resolver (stage S3).

Centralizes chat history by **COPY** into `$TX_IDE_HOME/history/<tx-id>/<chat-uuid>/` — a faithful,
self-contained **bundle** (`transcript.jsonl` + the sibling `subagents/` + `tool-results/` dirs that
a bare `.jsonl` omits, chat-ops §1 #3). There is **NO read API** (§11): the bundle is consumed by
grep (the HISTORIAN, S5). This module does exactly two things and parses no transcript content:

  1. the cross-project / worktree transcript **resolver** — find a chat's `.jsonl` under any
     munged-cwd project dir (a deleted/renamed worktree gets its own munged dir, chat-ops §3.4);
  2. the incremental **copy** (mirror) of the bundle.

Mechanism (chat-ops.md §3 / brief S3), off the hook's latency path:

  - **Incremental.** The transcript is append-only, so it is mirrored **by offset** (only the new
    tail is appended); the sidecar dirs are add-only, so they are copied **copy-if-absent** (an
    externalized tool-result, once written, is never re-copied). Never a multi-hundred-MB re-copy.
  - **`flock`-coalesced per (tx-id, chat).** If a mirror is already in flight for a chat, the next
    Stop's ingest simply **skips** it (non-blocking) — the mirror always targets the latest bytes,
    so a skipped intermediate is harmless and the next Stop (or SessionEnd) catches up. `tx archive`
    forces a **blocking** mirror so a retire always completes.
  - **Pure Python, not `rsync`.** The brief specifies "append/offset + copy-if-absent" precisely;
    the platform `rsync` here is openrsync (missing GNU flags); and the Claude engine
    (`engines.claude`) already owns `munge` + the transcript/bundle path rules, so this module reuses
    them rather than re-deriving paths.

`bundle_path` is the durable copy (F7); a successful ingest stamps it back onto the `ChatRef`.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import shutil
from collections.abc import Iterator
from pathlib import Path

from .engines import claude, get
from .session import Engine
from .store import SessionStore

# The per-(tx-id, chat) coalescing lock, alongside the bundle it guards. Hidden so a HISTORIAN grep
# of the tree never trips over it.
INGEST_LOCK_NAME = ".ingest.lock"

# How many trailing bytes of the existing copy to verify against the source before appending by
# offset. Cheap insurance that the destination really is a prefix of the source (append-only) — a
# rewritten/compacted transcript is detected and triggers a faithful full re-copy instead of a
# corrupt splice.
_PREFIX_CHECK_BYTES = 65536


# ----- cross-project resolver (chat-ops §3.4 — the only non-copy logic) ---------------------

def resolve_transcript(chat_id: str, cwd_hint: str | None, engine: Engine) -> Path | None:
    """Locate a chat's source transcript `.jsonl`, or None if it is not on disk yet.

    Engine-routed (design §2/§3, T4): the fast path asks the chat's engine to resolve the transcript
    from the cwd (`engine.resolve_transcript` — Claude's deterministic formula, Codex's rollout glob),
    then existence-checks it (the protocol leaves existence to the caller). Fallback (the cwd has moved
    — a deleted/renamed worktree, or a fork/handover launched elsewhere): glob the projects root for
    `*/<chat>.jsonl` and take the unique hit, preferring the `cwd_hint` munge when several match
    (chat-ids are unique, so >1 hit is not expected — prefer the hint defensively). The cross-project
    fallback is Claude's projects-root layout; the per-engine cross-project glob lands with Codex (T6).
    """
    if cwd_hint:
        fast = get(engine).resolve_transcript(chat_id, cwd_hint)
        if fast.exists():
            return fast
    matches = sorted(claude.projects_root().glob(f"*/{chat_id}{claude.TRANSCRIPT_SUFFIX}"))
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    if cwd_hint:
        preferred = claude.transcript_path(chat_id, cwd_hint)
        if preferred in matches:
            return preferred
    return matches[0]


# ----- ingest (the copy) --------------------------------------------------------------------

def ingest_session(store: SessionStore, session_id: str, *, wait: bool = False) -> list[str]:
    """Mirror every ingestable chat of one tx session into its history bundle (D6 — tx-tracked
    sessions only; the store is the source of truth for which chats exist). Returns the bundle paths
    that were touched. `wait=False` (the hot Stop/SessionEnd path) coalesces; `wait=True`
    (`tx archive`) blocks for a forced complete mirror.

    Chats are discovered from the record's `ChatRef`s, not from the Claude hook payload: the S2 shim
    drains the payload (`cat >/dev/null`), so the record is the only place the chat uuid + its cwd
    live. A `ChatRef` whose `id` is still None (a pending fork capture, S4) is skipped until the hook
    backfills it.
    """
    session = store.load(session_id)
    if session is None:
        return []  # D4: not a record this home tracks — nothing to ingest.
    ingested: dict[str, str] = {}
    for chat in session.chats:
        if chat.id is None:
            continue
        bundle = ingest_chat(session.id, chat.id, chat.cwd, chat.engine, wait=wait)
        if bundle is not None:
            ingested[chat.id] = str(bundle)
    if ingested:
        _stamp_bundle_paths(store, session_id, ingested)
    return list(ingested.values())


def ingest_chat(
    tx_id: str, chat_id: str, cwd_hint: str, engine: Engine, *, wait: bool = False
) -> Path | None:
    """Mirror one chat's bundle into `$TX_IDE_HOME/history/<tx-id>/<chat>/`, or None if the source
    transcript is not on disk yet. The chat's `engine` resolves the source transcript (T4); the copy
    itself is engine-neutral (the per-engine bundle LAYOUT — Codex's sidecar-free rollout — is T6).
    Returns the bundle dir (even when a concurrent mirror is skipped — the bundle exists either way)."""
    src_transcript = resolve_transcript(chat_id, cwd_hint, engine)
    if src_transcript is None:
        return None
    bundle = claude.bundle_dir(tx_id, chat_id)
    bundle.mkdir(parents=True, exist_ok=True)
    with _ingest_lock(bundle / INGEST_LOCK_NAME, wait=wait) as acquired:
        if not acquired:
            return bundle  # a mirror is already in flight for this chat — coalesce (skip).
        _mirror(src_transcript, chat_id, bundle)
    return bundle


def _mirror(src_transcript: Path, chat_id: str, bundle: Path) -> None:
    """The copy itself: the transcript by offset, then the entire sibling `<chat>/` dir
    copy-if-absent. The sidecar is taken relative to the RESOLVED transcript (so a moved cwd still
    finds its colocated sidecar), and copied wholesale — subagents/ + tool-results/ and anything
    else Claude externalizes — into the bundle root (chat-ops §3.1 layout). Save too much, parse
    nothing."""
    _append_by_offset(src_transcript, bundle / claude.BUNDLE_TRANSCRIPT_NAME)
    _copy_tree_if_absent(src_transcript.parent / chat_id, bundle)


def _append_by_offset(src: Path, dst: Path) -> None:
    """Mirror an append-only file by copying only the bytes past the destination's current length.

    Faithful + cheap: a fresh chat is copied whole; a grown transcript appends just the tail; an
    already-current copy is a no-op; and a transcript that SHRANK or whose overlap no longer matches
    (a rewrite/compaction — the append-only assumption broken) falls back to a full re-copy rather
    than splicing garbage. The prefix check reads only a trailing window, so the common append case
    stays O(new bytes)."""
    source_size = src.stat().st_size
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return
    destination_size = dst.stat().st_size
    if destination_size > source_size or not _is_prefix(src, dst, destination_size):
        shutil.copy2(src, dst)
        return
    if destination_size == source_size:
        return
    with open(src, "rb") as source, open(dst, "ab") as destination:
        source.seek(destination_size)
        shutil.copyfileobj(source, destination)


def _is_prefix(src: Path, dst: Path, destination_size: int) -> bool:
    """Whether `dst` is a prefix of `src` — verify the append-only assumption before appending by
    offset. Compares only the trailing window of the overlap (cheap), so a rewritten transcript is
    caught without re-reading the whole file."""
    if destination_size == 0:
        return True
    window = min(destination_size, _PREFIX_CHECK_BYTES)
    start = destination_size - window
    with open(dst, "rb") as destination:
        destination.seek(start)
        destination_tail = destination.read(window)
    with open(src, "rb") as source:
        source.seek(start)
        source_window = source.read(window)
    return destination_tail == source_window


def _copy_tree_if_absent(src_dir: Path, dst_dir: Path) -> None:
    """Recursively copy every file under `src_dir` into `dst_dir`, skipping any that already exist.

    Copy-if-absent (D5): externalized tool-results / subagent transcripts are immutable once
    written, so a present destination file is never re-copied — re-ingest only ships genuinely new
    sidecar files. A missing `src_dir` (a chat with no tool results) is a no-op."""
    if not src_dir.is_dir():
        return
    for source_file in src_dir.rglob("*"):
        if not source_file.is_file():
            continue
        destination_file = dst_dir / source_file.relative_to(src_dir)
        if destination_file.exists():
            continue
        destination_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, destination_file)


@contextlib.contextmanager
def _ingest_lock(lock_path: Path, *, wait: bool) -> Iterator[bool]:
    """A `flock` over the per-(tx-id, chat) lock file. Yields whether it was acquired: `wait=True`
    blocks until it is (the forced `tx archive` path), `wait=False` is non-blocking and yields False
    when another mirror already holds it (the coalescing hot path — the caller then skips)."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o644)
    acquired = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            acquired = False  # non-blocking lock already held → coalesce.
        yield acquired
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _stamp_bundle_paths(store: SessionStore, session_id: str, ingested: dict[str, str]) -> None:
    """Stamp `bundle_path` back onto each ingested `ChatRef` (F7).

    Re-load the record FRESH right before saving (the slow copy is already done): the detached
    Stop-path ingest runs alongside the state hooks, and reloading shrinks the lost-update window to
    the microseconds between read and `os.replace`. Save only when a `bundle_path` actually changes,
    so re-ingest of an already-stamped chat writes nothing — after the first ingest there is zero
    record contention from this path."""
    fresh = store.load(session_id)
    if fresh is None:
        return
    changed = False
    for chat in fresh.chats:
        target = ingested.get(chat.id) if chat.id is not None else None
        if target is not None and chat.bundle_path != target:
            chat.bundle_path = target
            changed = True
    if changed:
        store.save(fresh)
