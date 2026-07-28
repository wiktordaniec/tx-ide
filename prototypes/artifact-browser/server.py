#!/usr/bin/env python3.14
"""A local browser for tx-ide chat history, durable artifacts, and the session timeline.

Two halves, one page (`index.html`), one server:

  - **Chats** — every llm session that hosted at least one conversation, ordered by last
    interaction. The detail view surfaces everything the stores know about a session: the record
    fields, each `ChatRef` with its provenance edge (`origin`), the sessions derived FROM it
    (forks / handovers / resumes), the rollover/handover notes under `$TX_IDE_HOME/history/<tx>/`,
    the artifacts its touches produced, and the FULL dialogue reconstructed from the transcript
    (live source preferred, ingested bundle as fallback).
  - **Artifacts** — the durable records under `$TX_IDE_HOME/artifacts/`, each with its touch
    history resolved to the tx session that made it, linking straight back into the chat view.

Reads go through the real `SessionStore` / `ArtifactStore` / `ArtifactContent` primitives — the
same ones `tx` uses, so there is no second source of truth to drift; the store is re-read on every
request, and reads do NOT go through `ArtifactService`, so they never append to the EventLog (a
polling dashboard would be noise). The stores are never written; the only writes are the timeline
POSTs — the two pane sends (keystrokes into tmux) and the dragged group order.

Run it from anywhere:

    python3.14 prototypes/artifact-browser/server.py [PORT [HOST]]

then open the printed URL. `$TX_IDE_HOME` overrides the home it reads, exactly as for `tx` itself.
"""

from __future__ import annotations

import difflib
import json
import subprocess
import re
import sys
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# bin/tx points PYTHONPATH at <repo>/lib; this standalone script does the same from its own location
# (prototypes/artifact-browser/server.py → repo root is two parents up) so `import tx` resolves the
# bundled package regardless of the cwd it is launched from.
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from tx import history  # noqa: E402
from tx.artifact import Artifact  # noqa: E402
from tx.artifact_store import ArtifactContent, ArtifactStore  # noqa: E402
from tx.engines import claude as claude_engine  # noqa: E402
from tx.engines import codex_rollout  # noqa: E402
from tx.grouping import GroupResolver  # noqa: E402
from tx.palette import tag_cube  # noqa: E402
from tx.render import actor_label, reltime  # noqa: E402
from tx.session import ChatRef, Engine, LlmSession, Role, Session, UnsupportedRecordError  # noqa: E402
from tx.storage import artifacts_dir, history_dir, sessions_dir  # noqa: E402
from tx.tmux import Tmux  # noqa: E402
from tx.store import SessionStore  # noqa: E402

import timeline as timeline_view  # noqa: E402  (sibling module: timeline swimlane data)

HERE = Path(__file__).resolve().parent
PAGE = HERE / "index.html"
DEFAULT_PORT = 8766

# Extensions whose working copy the page renders as markdown, and those it renders as a PAGE in a
# sandboxed frame (`/render`); anything else utf-8 is preformatted text, and anything that does not
# decode as utf-8 is offered as a binary download.
MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})
HTML_SUFFIXES = frozenset({".html", ".htm"})

# Artifact HTML is agent-authored — untrusted markup — and THIS origin can POST keystrokes into live
# tmux panes (`message_session`). Rendered markup must therefore never execute here: the reader frames
# `/render` with `sandbox="allow-same-origin"` (no `allow-scripts`), and these headers make the
# response inert on its own too, so the URL opened directly in a tab is still script-free and offline.
# `allow-same-origin` is what lets the reader reach into the frame for its outline and find, and it is
# safe precisely BECAUSE scripts are withheld — the combination that defeats a sandbox is
# allow-same-origin WITH allow-scripts. Subresources are `data:`-only: an artifact must not be able to
# beacon whether (or when) it was read.
RENDER_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; "
        "frame-ancestors 'self'; sandbox allow-same-origin"
    ),
    "X-Content-Type-Options": "nosniff",
}

# A canonical id (artifact or tx session) is a uuid. The stores join ids into filesystem paths, so
# the browser accepts ONLY that exact token in a route — a crafted `..` / `/` (literal OR
# percent-encoded) can then never escape the home to read an arbitrary file (QA P1: path traversal).
# Deliberately UNANCHORED + used with `fullmatch`: `match` with a trailing `$` would accept a
# canonical uuid followed by one newline (Python's `$` also matches just before a final `\n`), which
# would let a control character survive into a record-derived path (QA P2b). `fullmatch` requires the
# WHOLE string to be the token — no trailing newline, no leading or trailing anything.
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _safe_artifact_id(raw: str) -> str | None:
    """Validate a URL path segment as a canonical uuid BEFORE it reaches a store — boundary
    validation at the HTTP edge, so no raw route input is ever joined into a filesystem path. Decode
    percent-encoding ONCE, then require the exact uuid shape; a literal or encoded `..`/`/`, or any
    other non-uuid token, returns None (the route 404s without touching the store). Shared by the
    artifact AND session routes — both stores are uuid-keyed."""
    decoded = urllib.parse.unquote(raw)
    return decoded if _UUID_RE.fullmatch(decoded) else None


# Everything outside this set is dropped from the quoted Content-Disposition fallback. A record's
# filename is untrusted at the HTTP edge: a POSIX basename may legally contain CR/LF (which would
# inject response headers — QA P1b), quotes (which truncate the parameter), or backslashes.
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def _content_disposition(filename: str) -> str:
    """A header-safe `Content-Disposition` for a download. The quoted fallback is reduced to a
    conservative ASCII token (no CR/LF, quote or backslash can survive), and the exact name rides in
    the RFC 5987 `filename*` parameter, whose percent-encoding makes a header break impossible. The
    store keeps the real filename untouched — this sanitizing belongs at the boundary that emits it."""
    fallback = _SAFE_FILENAME_RE.sub("_", filename).strip("._") or "artifact"
    encoded = urllib.parse.quote(filename, safe="")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"


# ----- transcript reading (dialogue + titles) ----------------------------------------------------
# The transcript is Claude's JSONL — an EXTERNAL boundary, parsed tolerantly line by line (a partial
# trailing line during a live turn is skipped, not fatal). The dialogue reconstruction mirrors the
# proven sessions-graph inbox parser: your typed lines, the agent's text replies, and runs of tool
# activity collapsed to one separator — here over the FULL file, not a tail window (the chat detail
# is the archive reader; seeing the whole conversation is the point).

# User lines that are harness scaffolding, not dialogue (mirrors sessions-graph + adds the
# peer-message envelope, which IS shown in dialogue but never used as a title).
_NOISE_PREFIXES = ("<task-notification", "<local-command", "<command-", "[Request interrupted")
_TITLE_NOISE_PREFIXES = _NOISE_PREFIXES + ("<from-claude", "<from-agent")

# Characters shell quoting injects into a recorded launch command (`shlex.join` wraps the priming
# prompt in quotes and mangles embedded apostrophes to `'\"'\"'`), removed before the is-this-the-
# priming containment check — a verbatim `in` would miss any priming containing a quote.
_QUOTING_CHARS = str.maketrans("", "", "'\"\\")


def _is_priming(text: str, launch_cmd: str) -> bool:
    """Whether a user line is the spawn priming — its text embedded in the session's launch command
    (the sessions-graph rule, made quoting-insensitive so shlex-mangled apostrophes still match)."""
    if not launch_cmd:
        return False
    return text.strip().translate(_QUOTING_CHARS) in launch_cmd.translate(_QUOTING_CHARS)


def _iter_entries(path: Path):
    """Parsed main-chain entries of a transcript, streamed. Sub-agent sidechains are filtered —
    their turns are not this conversation."""
    try:
        handle = path.open("rb")
    except OSError:
        return
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict) and not entry.get("isSidechain"):
                yield entry


def _entry_ts(entry: dict) -> float:
    timestamp = entry.get("timestamp")
    if not isinstance(timestamp, str) or not timestamp:
        return 0.0
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _text_of(content: object) -> str:
    """The visible text of a message's content — a plain string, or the joined `text` blocks
    (thinking / tool_use / tool_result blocks are not dialogue text)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ).strip()
    return ""


class _TurnCollector:
    """Shared turn assembly for both engine parsers: `you` / `agent` text turns and runs of tool
    activity collapsed to one `{who: "tools", count, names}` separator."""

    def __init__(self) -> None:
        self.turns: list[dict] = []
        self._pending_tools = 0
        self._pending_names: list[str] = []

    def tool(self, name: str) -> None:
        self._pending_tools += 1
        if name not in self._pending_names:
            self._pending_names.append(name)

    def text(self, who: str, text: str, ts: float) -> None:
        self.flush()
        self.turns.append({"who": who, "text": text, "ts": ts})

    def flush(self) -> None:
        if self._pending_tools:
            self.turns.append({
                "who": "tools", "count": self._pending_tools,
                "names": self._pending_names[:], "ts": 0.0,
            })
            self._pending_tools = 0
            self._pending_names = []


def _dialogue_turns(path: Path, launch_cmd: str, engine: Engine | None) -> list[dict]:
    """The FULL transcript as chat turns, engine-routed (Claude JSONL vs Codex rollout)."""
    if engine == Engine.CODEX:
        return _codex_dialogue_turns(path, launch_cmd)
    return _claude_dialogue_turns(path, launch_cmd)


def _claude_dialogue_turns(path: Path, launch_cmd: str) -> list[dict]:
    """A Claude transcript as chat turns. Harness-injected user lines (tool_result contents, isMeta
    scaffolding, command output) are not turns, and neither is the spawn priming — a user line whose
    text is embedded verbatim in the session's launch command."""
    collector = _TurnCollector()
    for entry in _iter_entries(path):
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        kind, ts = entry.get("type"), _entry_ts(entry)
        if kind == "assistant":
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        collector.tool(block.get("name", "?"))
            text = _text_of(content)
            if text:
                collector.text("agent", text, ts)
        elif kind == "user" and not entry.get("isMeta"):
            text = _text_of(message.get("content"))
            if (
                text
                and not any(text.lstrip().startswith(p) for p in _NOISE_PREFIXES)
                and not _is_priming(text, launch_cmd)
            ):
                collector.text("you", text, ts)
    collector.flush()
    return collector.turns


# Codex rollouts are `{timestamp, type, payload}` lines: dialogue rides `payload.type == "message"`
# response items (roles user/assistant/developer), tool activity rides the `*_call` payload types.
# The `user_message`/`agent_message` event duplicates are ignored — counting both would double every
# turn (the same rule `codex_rollout.iter_messages` follows).
_CODEX_TOOL_CALL_TYPES = frozenset(
    {"function_call", "custom_tool_call", "local_shell_call", "web_search_call"}
)


def _codex_records(path: Path):
    """Parsed `{timestamp, type, payload}` rollout lines, streamed tolerantly (an external
    boundary: a partial trailing line during a live session is skipped, not fatal)."""
    try:
        handle = path.open("rb")
    except OSError:
        return
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and isinstance(record.get("payload"), dict):
                yield record


def _codex_text(content: object) -> str:
    """The visible text of a codex message payload — its input_text/output_text blocks joined."""
    if not isinstance(content, list):
        return ""
    return "".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") in ("input_text", "output_text")
    ).strip()


def _iso_epoch(timestamp: object) -> float:
    if not isinstance(timestamp, str) or not timestamp:
        return 0.0
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _codex_dialogue_turns(path: Path, launch_cmd: str) -> list[dict]:
    """A Codex rollout as chat turns. The injected `<environment_context>` first user turn and the
    `developer` instruction turns are scaffolding, not dialogue; the spawn priming is excluded by
    the same launch-command rule as Claude's."""
    collector = _TurnCollector()
    for record in _codex_records(path):
        payload = record["payload"]
        kind = payload.get("type")
        if kind in _CODEX_TOOL_CALL_TYPES:
            collector.tool(payload.get("name") or kind.removesuffix("_call"))
        elif kind == codex_rollout.MESSAGE_TYPE:
            role = payload.get("role")
            text = _codex_text(payload.get("content"))
            if not text:
                continue
            if role == "assistant":
                collector.text("agent", text, _iso_epoch(record.get("timestamp")))
            elif role == "user" and not _codex_scaffolding(text) and not _is_priming(text, launch_cmd):
                collector.text("you", text, _iso_epoch(record.get("timestamp")))
    collector.flush()
    return collector.turns


def _codex_scaffolding(text: str) -> bool:
    return text.lstrip().startswith(codex_rollout.ENVIRONMENT_CONTEXT_TAG)


# Titles are derived, not stored (`ChatRef.summary` is never populated): the first REAL user prompt
# of a session's first chat. The head-read is cached by (path, size-of-head-window outcome) — the
# first prompt never changes once found, so a hit is cached by path forever; a miss is retried when
# the file grows.
_TITLE_HEAD_LINES = 80
_title_lock = threading.Lock()
_title_cache: dict[str, str] = {}
_title_miss: dict[str, int] = {}  # path -> file size at the last failed attempt


def _strip_priming_boilerplate(text: str) -> str:
    """The task inside a standard worker priming. The role-file instruction sentences ("Read
    ~/.tx-ide/agents/COMMON.md … Follow all of these for the duration of this session.") are launch
    mechanics, not the task — drop the fragments referencing the role files (and the follow-them
    closer) and keep the rest. Filtering (never reordering) keeps an imperfect sentence split
    harmless. Empty means the whole text was boilerplate — the caller picks its own fallback."""
    fragments = re.split(r"(?<=\.)\s+", text)
    kept = [
        fragment
        for fragment in fragments
        if ".tx-ide/agents/" not in fragment
        and ".tx-ide/user-agents" not in fragment
        and not fragment.startswith("Follow all")
    ]
    return " ".join(kept).strip()


def _head_user_texts(path: Path, engine: Engine | None):
    """The user-authored texts in a transcript's head, in order, engine-routed. Bounded to
    `_TITLE_HEAD_LINES` parsed records so a title probe never reads a whole multi-MB transcript."""
    if engine == Engine.CODEX:
        for index, record in enumerate(_codex_records(path)):
            if index >= _TITLE_HEAD_LINES:
                break
            payload = record["payload"]
            if payload.get("type") != codex_rollout.MESSAGE_TYPE or payload.get("role") != "user":
                continue
            text = _codex_text(payload.get("content"))
            if text and not _codex_scaffolding(text):
                yield text
        return
    for index, entry in enumerate(_iter_entries(path)):
        if index >= _TITLE_HEAD_LINES:
            break
        if entry.get("type") != "user" or entry.get("isMeta"):
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        text = _text_of(message.get("content"))
        if text:
            yield text


def _derive_title(path: Path, launch_cmd: str, engine: Engine | None) -> str:
    """The first real user prompt in the transcript head — the human's opening ask. Falls back to
    the spawn priming (the launch-command-embedded prompt, boilerplate stripped) when the human
    never typed, since for a worker that priming is the most descriptive line there is. Empty when
    neither exists yet."""
    key = str(path)
    with _title_lock:
        if key in _title_cache:
            return _title_cache[key]
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    with _title_lock:
        if _title_miss.get(key) == size:
            return ""
    priming = ""
    for text in _head_user_texts(path, engine):
        if any(text.lstrip().startswith(p) for p in _TITLE_NOISE_PREFIXES):
            continue
        if _is_priming(text, launch_cmd):
            priming = priming or text.strip()
            continue
        # Boilerplate-strip even a "real" prompt: a priming delivered by send-keys (not the launch
        # command) is indistinguishable from typed input, and the strip is a no-op for human text.
        # An ALL-boilerplate prompt is such a priming — hold it as the fallback and keep scanning
        # for the first genuine message.
        stripped = " ".join(_strip_priming_boilerplate(text).split())
        if not stripped:
            priming = priming or text.strip()
            continue
        with _title_lock:
            _title_cache[key] = stripped[:200]
        return stripped[:200]
    if priming:
        stripped = " ".join(_strip_priming_boilerplate(priming).split()) or " ".join(priming.split())
        title = stripped[:200]
        with _title_lock:
            _title_cache[key] = title
        return title
    with _title_lock:
        _title_miss[key] = size
    return ""


# ----- chat-side payloads ------------------------------------------------------------------------


def cube_to_hex(cube_index: int) -> str:
    """Map an xterm-256 color index to `#rrggbb`, so the browser paints tag chips in the same
    colors the terminal picker (and the sessions-graph dashboard) uses. `palette.tag_cube` returns
    these indices; 16–231 is the 6×6×6 color cube, 232–255 the grayscale ramp."""
    if 16 <= cube_index <= 231:
        offset = cube_index - 16
        red, green, blue = offset // 36, (offset // 6) % 6, offset % 6

        def channel(step: int) -> int:
            return 0 if step == 0 else 55 + 40 * step

        return f"#{channel(red):02x}{channel(green):02x}{channel(blue):02x}"
    grey = 8 + 10 * (cube_index - 232)
    return f"#{grey:02x}{grey:02x}{grey:02x}"


def _tag_colors(tags: list[str]) -> dict[str, str]:
    return {tag: cube_to_hex(tag_cube(tag)) for tag in tags}


def _group_resolver() -> GroupResolver:
    """Per-request read-time resolver over full store snapshots (store reads only, never logs)."""
    return GroupResolver(SessionStore().all(), ArtifactStore().all())


def _group_fields(resolved_group: str, explicit: str | None) -> dict:
    """The group triple a payload row carries: override (null when derived), resolution, colour."""
    return {
        "group": explicit,
        "resolved_group": resolved_group,
        "group_color": cube_to_hex(tag_cube(resolved_group)),
    }


def _load_llm_session(session_id: str) -> LlmSession | None:
    """Resolve a route-supplied id to an llm record, or None (→ 404). A persistence boundary read
    driven by user input: a missing record, an unreadable older-schema record, and a non-llm record
    (which hosts no chats) all resolve to "no such chat session" rather than a 500."""
    try:
        session = SessionStore().load(session_id)
    except UnsupportedRecordError:
        return None
    if session is None or session.role != Role.LLM:
        return None
    return session


def _chat_sessions(store: SessionStore) -> list[LlmSession]:
    """Every llm session that hosted at least one conversation — the chat browser's population.
    Includes exited/archived records: this is the archive, not the live picker."""
    return [
        session
        for session in store.all()
        if session.role == Role.LLM and session.chats
    ]


def _transcript_candidates(session: LlmSession, chat) -> list[Path]:
    """Where a chat's transcript may live, preference-ordered: the recorded live source path, then
    the ingested bundle copy. Stat/stored paths only — no directory glob (the cross-project
    resolver) on the hot list path; the detail/dialogue routes use the resolver."""
    candidates = []
    if chat.transcript_path:
        candidates.append(Path(chat.transcript_path))
    if chat.id is not None:
        if chat.bundle_path:
            candidates.append(Path(chat.bundle_path) / claude_engine.BUNDLE_TRANSCRIPT_NAME)
        else:
            candidates.append(claude_engine.bundle_transcript_path(session.id, chat.id))
    return candidates


def _first_existing(paths: list[Path]) -> Path | None:
    for path in paths:
        if path.exists():
            return path
    return None


# How far back from the end of a transcript to look for the last stamped entry. A window bounds the
# read on multi-MB files; an entry line longer than this (a giant single-line tool result at the very
# end) falls back to mtime.
_TAIL_WINDOW_BYTES = 65536
_tail_ts_lock = threading.Lock()
_tail_ts_cache: dict[str, tuple[int, int, float]] = {}  # path -> (size, mtime_ns, entry ts)


def _last_entry_ts(path: Path) -> float:
    """The timestamp of the newest transcript entry — the honest "last message" time. File mtime is
    NOT trusted as the primary signal: Claude Code touches old transcript files during its own
    housekeeping (observed: a 3-day-idle chat whose file mtime was minutes old), so the tail's own
    stamp decides and mtime is only the fallback when no entry in the window parses. Cached by
    (size, mtime), so an unchanged file is a stat, not a read. 0.0 when the file is absent."""
    try:
        stat = path.stat()
    except OSError:
        return 0.0
    key = str(path)
    with _tail_ts_lock:
        cached = _tail_ts_cache.get(key)
        if cached is not None and cached[0] == stat.st_size and cached[1] == stat.st_mtime_ns:
            return cached[2]
    timestamp = 0.0
    try:
        with path.open("rb") as handle:
            if stat.st_size > _TAIL_WINDOW_BYTES:
                handle.seek(stat.st_size - _TAIL_WINDOW_BYTES)
            lines = handle.read().decode(errors="replace").splitlines()
    except OSError:
        return 0.0
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            timestamp = _iso_epoch(entry.get("timestamp"))
            if timestamp:
                break
    if timestamp == 0.0:
        timestamp = stat.st_mtime
    with _tail_ts_lock:
        _tail_ts_cache[key] = (stat.st_size, stat.st_mtime_ns, timestamp)
    return timestamp


def _last_interaction(session: LlmSession) -> float:
    """The session's most recent sign of life: the record's activity stamp or the newest LAST-ENTRY
    timestamp of any chat's transcript — whichever is later. The bundle is a prefix mirror of the
    live transcript, so the first candidate that exists decides for its chat."""
    latest = session.activity_at
    for chat in session.chats:
        for path in _transcript_candidates(session, chat):
            timestamp = _last_entry_ts(path)
            if timestamp:
                latest = max(latest, timestamp)
                break
    return latest


def _session_title(session: LlmSession) -> str:
    """The derived display title: the first real user prompt of the first titled chat."""
    for chat in session.chats:
        source = _first_existing(_transcript_candidates(session, chat))
        if source is None:
            continue
        title = _derive_title(source, session.initial_cmd or "", chat.engine or session.engine)
        if title:
            return title
    return ""


def _artifact_touch_counts(artifacts: list[Artifact]) -> dict[str, int]:
    """session id -> number of artifacts that session touched (any touch), for the list badge."""
    counts: dict[str, int] = {}
    for artifact in artifacts:
        for session_id in {touch.session_id for touch in artifact.history}:
            counts[session_id] = counts.get(session_id, 0) + 1
    return counts


# The standing orchestrator. It spawns most sessions, so as a list parent it would fold nearly the
# whole forest under one node — review round 2 settled that it is infrastructure, not lineage: its
# rows stay OUT of the list feed and sessions it spawned render as roots. Its detail/dialogue routes
# still serve (origin edges keep linking to it); only the list treats it as invisible plumbing.
ASSISTANT_NAME = "tx-assistant"


def chats_payload() -> dict:
    """The chats list feed — one row per session that hosted a conversation, newest interaction
    first. Everything the grid needs; the heavy dialogue stays on the detail routes. The
    tx-assistant is infrastructure (see `ASSISTANT_NAME`): no row, and never a parent."""
    now = time.time()
    store = SessionStore()
    sessions = _chat_sessions(store)
    assistant_ids = {session.id for session in sessions if session.name == ASSISTANT_NAME}
    artifact_counts = _artifact_touch_counts(ArtifactStore().all())
    resolver = _group_resolver()
    rows = []
    for session in sessions:
        if session.name == ASSISTANT_NAME:
            continue
        last = _last_interaction(session)
        rows.append({
            "id": session.id,
            "name": session.name,
            "parent": None if session.parent in assistant_ids else session.parent,
            "title": _session_title(session),
            "tags": session.tags,
            "tag_colors": _tag_colors(session.tags),
            **_group_fields(resolver.session_group(session), session.group),
            "state": session.state.value,
            "alive": session.is_alive(),
            "engine": session.engine.value,
            "read_only": session.read_only,
            "cwd": session.cwd,
            "chats": len(session.chats),
            "artifacts": artifact_counts.get(session.id, 0),
            "started_ago": reltime(session.created_at, now),
            "last_epoch": last,
            "last_ago": reltime(last, now),
        })
    rows.sort(key=lambda row: row["last_epoch"], reverse=True)
    return {"sessions": rows}


def _chat_entry(session: LlmSession, chat: ChatRef, names: dict[str, str], now: float) -> dict:
    """One `ChatRef` with everything the detail view shows: the ref verbatim plus resolved
    source/bundle availability and the origin edge labeled with the parent session's name."""
    transcript_size = None
    if chat.transcript_path:
        try:
            transcript_size = Path(chat.transcript_path).stat().st_size
        except OSError:
            pass  # recorded path gone stale — shown as the path with no size
    bundle = None
    bundle_size = None
    if chat.id is not None:
        bundle = (
            Path(chat.bundle_path) / claude_engine.BUNDLE_TRANSCRIPT_NAME
            if chat.bundle_path
            else claude_engine.bundle_transcript_path(session.id, chat.id)
        )
        try:
            bundle_size = bundle.stat().st_size
        except OSError:
            bundle_size = None
    origin_name = actor_label(chat.origin.session_id, names)
    return {
        "id": chat.id,
        "role": chat.role,
        "engine": chat.engine.value if chat.engine is not None else None,
        "cwd": chat.cwd,
        "started_at": chat.started_at,
        "started_ago": reltime(chat.started_at, now),
        "ended_at": chat.ended_at,
        "ended_ago": reltime(chat.ended_at, now) if chat.ended_at else None,
        "open": chat.ended_at is None,
        "origin": {
            "how": chat.origin.how,
            "session_id": chat.origin.session_id,
            "session_name": origin_name,
            "session_exists": chat.origin.session_id in names,
            "chat_id": chat.origin.chat_id,
        },
        "transcript_path": chat.transcript_path or None,
        "transcript_size": transcript_size,
        "bundle_path": str(bundle.parent) if bundle is not None else None,
        "bundle_size": bundle_size,
    }


def _derived_sessions(all_sessions: list[Session], session: LlmSession, now: float) -> list[dict]:
    """Sessions whose chats derive FROM this one — the outgoing provenance edges (fork / handover /
    resume land a `ChatRef` on the NEW session whose origin points back here, by session id or by
    one of this session's chat ids). Rollover self-edges are excluded (same record)."""
    own_chat_ids = {chat.id for chat in session.chats if chat.id is not None}
    derived = []
    for other in all_sessions:
        if other.id == session.id:
            continue
        for chat in other.chats:
            from_here = (
                chat.origin.session_id == session.id
                or (chat.origin.chat_id is not None and chat.origin.chat_id in own_chat_ids)
            )
            if from_here:
                derived.append({
                    "id": other.id,
                    "name": other.name,
                    "how": chat.origin.how,
                    "state": other.state.value,
                    "started_ago": reltime(chat.started_at, now),
                    "started_at": chat.started_at or 0.0,
                })
                break
    derived.sort(key=lambda item: item["started_at"], reverse=True)
    return derived


def _touched_artifacts(session_id: str, now: float) -> list[dict]:
    """The artifacts this session created or touched (the `artifacts_for_session` reverse lookup,
    computed here against the store so the read never logs), with the revs THIS session wrote."""
    touched = []
    for artifact in ArtifactStore().all():
        own = [touch for touch in artifact.history if touch.session_id == session_id]
        if not own:
            continue
        touched.append({
            "id": artifact.id,
            "title": artifact.title,
            "filename": artifact.filename,
            "revs": len(artifact.history),
            "own_revs": [touch.rev for touch in own],
            "last_touch_ago": reltime(max(touch.at for touch in own), now),
            "updated_at": artifact.updated_at,
        })
    touched.sort(key=lambda item: item["updated_at"], reverse=True)
    return touched


def _notes(session_id: str, now: float) -> list[dict]:
    """The distilled hand-off documents under `history/<tx>/*.md` — rollover notes + handover
    briefs. Raw markdown text; the page renders it."""
    directory = history_dir() / session_id
    if not directory.is_dir():
        return []
    notes = []
    for path in sorted(directory.glob("*.md"), key=lambda p: p.stat().st_mtime):
        try:
            text = path.read_text()
        except OSError:
            continue
        notes.append({
            "name": path.name,
            "ago": reltime(path.stat().st_mtime, now),
            "text": text,
        })
    return notes


def chat_detail_payload(session_id: str) -> dict | None:
    """One session's full chat-side picture — the record, its chats with provenance, the sessions
    derived from it, its artifacts, and its notes. The dialogue is the separate (heavy) route."""
    now = time.time()
    session = _load_llm_session(session_id)
    if session is None:
        return None
    store = SessionStore()
    all_sessions = store.all()
    names = {record.id: record.name for record in all_sessions}
    parent = session.parent
    return {
        "id": session.id,
        "name": session.name,
        "title": _session_title(session),
        "tags": session.tags,
        "tag_colors": _tag_colors(session.tags),
        **_group_fields(_group_resolver().session_group(session), session.group),
        "state": session.state.value,
        "alive": session.is_alive(),
        "engine": session.engine.value,
        "read_only": session.read_only,
        "cwd": session.cwd,
        "initial_cmd": session.initial_cmd,
        "parent": parent,
        "parent_name": names.get(parent) if parent else None,
        "parent_is_session": parent in names if parent else False,
        "created_ago": reltime(session.created_at, now),
        "ended_ago": reltime(session.ended_at, now) if session.ended_at else None,
        "last_ago": reltime(_last_interaction(session), now),
        "chats": [_chat_entry(session, chat, names, now) for chat in session.chats],
        "derived": _derived_sessions(all_sessions, session, now),
        "artifacts": _touched_artifacts(session.id, now),
        "notes": _notes(session.id, now),
    }


def dialogue_payload(session_id: str) -> dict | None:
    """The FULL dialogue of every chat the session hosted, in ChatRef order — the archive reader.
    Each chat is parsed from its best available source: the live transcript (resolved through the
    cross-project resolver, so a moved worktree still finds it) or the ingested bundle."""
    session = _load_llm_session(session_id)
    if session is None:
        return None
    launch_cmd = session.initial_cmd or ""
    chats = []
    for chat in session.chats:
        if chat.id is None:
            chats.append({"id": None, "role": chat.role, "source": None, "turns": []})
            continue
        live = history.resolve_transcript(chat.id, chat.cwd, chat.engine)
        bundle = (
            Path(chat.bundle_path) / claude_engine.BUNDLE_TRANSCRIPT_NAME
            if chat.bundle_path
            else claude_engine.bundle_transcript_path(session.id, chat.id)
        )
        source, kind = (live, "live") if live is not None else (bundle, "bundle")
        if not source.exists():
            chats.append({"id": chat.id, "role": chat.role, "source": None, "turns": []})
            continue
        chats.append({
            "id": chat.id,
            "role": chat.role,
            "source": kind,
            "turns": _dialogue_turns(source, launch_cmd, chat.engine or session.engine),
        })
    return {"id": session.id, "chats": chats}


# ----- artifact payloads — read through the store + content primitives, NEVER the logging service -


def _author_ids(artifact: Artifact) -> list[str]:
    """The distinct touch author ids in first-touch order (creator first) — straight off history."""
    seen: list[str] = []
    for touch in artifact.history:
        if touch.session_id not in seen:
            seen.append(touch.session_id)
    return seen


def _authors(artifact: Artifact, names: dict[str, str]) -> list[dict]:
    """Distinct authors as `{id, name, session_exists}` — the id stays the durable identity, `name`
    is the session name resolved at request time, and `session_exists` tells the page whether the
    author links into the chat view (the `user` sentinel and vanished records do not)."""
    return [
        {"id": author, "name": actor_label(author, names), "session_exists": author in names}
        for author in _author_ids(artifact)
    ]


def list_payload() -> dict:
    """The artifact list feed — every artifact with the settled columns (title, id, filename, #revs,
    last touched, touch authors, resolved effort group), newest-touched first."""
    artifacts = sorted(ArtifactStore().all(), key=lambda artifact: artifact.updated_at, reverse=True)
    names = SessionStore().names_for(
        touch.session_id for artifact in artifacts for touch in artifact.history
    )
    resolver = _group_resolver()
    return {
        "artifacts": [
            {
                "id": artifact.id,
                "title": artifact.title,
                "filename": artifact.filename,
                "revs": len(artifact.history),
                "updated_ago": reltime(artifact.updated_at),
                "authors": _authors(artifact, names),
                **_group_fields(resolver.artifact_group(artifact), artifact.group),
            }
            for artifact in artifacts
        ]
    }


def detail_payload(artifact_id: str) -> dict | None:
    """One artifact's detail — metadata, the full touch/version log (each touch resolved to the tx
    session that made it, linkable into the chat view), and the rendered working copy."""
    artifact = ArtifactStore().load(artifact_id)
    if artifact is None:
        return None
    content = ArtifactContent()
    current_bytes = content.read_current(artifact)
    try:
        dirty = current_bytes != content.read_rev(artifact, artifact.latest_rev)
    except FileNotFoundError:
        dirty = False  # a missing last-rev file is corruption `tx artifact doctor` owns, not a 500
    names = SessionStore().names_for(touch.session_id for touch in artifact.history)
    return {
        "id": artifact.id,
        "title": artifact.title,
        "filename": artifact.filename,
        "created_ago": reltime(artifact.created_at),
        "updated_ago": reltime(artifact.updated_at),
        "revs": len(artifact.history),
        "dirty": dirty,
        **_group_fields(_group_resolver().artifact_group(artifact), artifact.group),
        "history": [
            {
                "rev": touch.rev,
                "ago": reltime(touch.at),
                # The raw stamp too, not just the relative one: the reader anchors a link into the
                # writing session's transcript at the moment the revision was taken, so a reader can
                # go from "this changed" to the conversation that changed it.
                "at": touch.at,
                "session_id": touch.session_id,
                "actor": actor_label(touch.session_id, names),
                "session_exists": touch.session_id in names,
                "changes": touch.changes,
            }
            for touch in artifact.history
        ],
        "working": _render_working_copy(artifact, current_bytes),
    }


def _render_working_copy(artifact: Artifact, raw: bytes) -> dict:
    """Classify the working copy for the detail view: markdown (rendered client-side), html (rendered
    as a page in the reader's sandboxed frame), plain utf-8 text (preformatted), or binary (a download
    link only). Classification is by extension + a utf-8 decode probe; the store's bytes are
    authoritative. `text` rides along for html too — it is what the reader's source toggle shows."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {"encoding": "binary", "size": len(raw)}
    return {
        "encoding": _text_encoding(artifact.extension.lower()),
        "text": text,
        "size": len(raw),
    }


def _text_encoding(suffix: str) -> str:
    """Which viewer a decodable working copy belongs to, by extension alone — the one place the
    reader's kinds are decided."""
    if suffix in MARKDOWN_SUFFIXES:
        return "markdown"
    if suffix in HTML_SUFFIXES:
        return "html"
    return "text"


def raw_bytes(artifact_id: str, rev: int | None) -> tuple[bytes, str] | None:
    """The raw bytes of a rev (or the working copy when `rev` is None) + a download filename — backs
    the binary download link. None when the artifact/rev is absent."""
    artifact = ArtifactStore().load(artifact_id)
    if artifact is None:
        return None
    content = ArtifactContent()
    try:
        data = content.read_current(artifact) if rev is None else content.read_rev(artifact, rev)
    except FileNotFoundError:
        return None
    return data, artifact.filename


def render_bytes(artifact_id: str, rev: int | None) -> bytes | None:
    """The bytes to serve AS A PAGE (the working copy, or a frozen rev) — what the reader's sandboxed
    frame loads. Only an html-suffixed artifact is served this way: this route is the one place bytes
    from the store reach a real HTML parser, so it stays narrowed to the artifacts that are meant to
    be parsed as one rather than becoming a general serve-any-artifact-as-a-page gadget. None when the
    artifact is absent, is not html, or has no such rev."""
    artifact = ArtifactStore().load(artifact_id)
    if artifact is None or artifact.extension.lower() not in HTML_SUFFIXES:
        return None
    content = ArtifactContent()
    try:
        return content.read_current(artifact) if rev is None else content.read_rev(artifact, rev)
    except FileNotFoundError:
        return None


def diff_payload(artifact_id: str, rev_a: int, rev_b: int) -> dict | None:
    """An on-demand `difflib` unified diff between two revs — computed HERE (never via
    `ArtifactService.diff`, which would log). Refuses cleanly when either rev is not utf-8 text.
    None when the artifact is absent."""
    artifact = ArtifactStore().load(artifact_id)
    if artifact is None:
        return None
    content = ArtifactContent()
    try:
        left = content.read_rev(artifact, rev_a).decode("utf-8")
        right = content.read_rev(artifact, rev_b).decode("utf-8")
    except FileNotFoundError:
        return {"error": f"no such revision (have 0..{artifact.latest_rev})"}
    except UnicodeDecodeError:
        return {"error": "a revision is not utf-8 text — cannot diff"}
    diff = "".join(
        difflib.unified_diff(
            left.splitlines(keepends=True),
            right.splitlines(keepends=True),
            fromfile=f"rev{rev_a}",
            tofile=f"rev{rev_b}",
        )
    )
    return {"a": rev_a, "b": rev_b, "diff": diff}


# Returned by `_int_param` when a query parameter is PRESENT but not a plain integer. It must stay
# distinct from `None` (absent): collapsing the two let a malformed `?rev=` silently fall back to the
# default instead of being rejected (QA P2).
_MALFORMED_PARAM = object()


# ----- HTTP — reads are GET; the only writes are the timeline sends + the group order ------------


# ----- timeline edit-mode messaging (mirrors sessions-graph's message_assistant) -----------------

def _describe_target(target: Session) -> str:
    """The unambiguous, id-led context fragment for one session — carried in the instruction so the
    assistant's own `tx` calls target the right record despite the two-records-by-name collision."""
    tags = ",".join(target.tags) or "-"
    return (
        f'id={target.id} name="{target.name}" '
        f'role={target.role.value} state={target.state.value} tags={tags} '
        f'cwd={target.cwd} parent={target.parent or "-"}'
    )


def _compose_message(targets: list[Session], request: str) -> str:
    """One single-line user instruction for tx-assistant: the id-led context for every target, then
    the user's free-text request applied to all of them."""
    request = " ".join(request.split())
    if len(targets) == 1:
        return (
            f'Act on tx session {_describe_target(targets[0])}. '
            f'Use the id (not the name) as the tx target. User request: {request}'
        )
    described = "; ".join(f"[{_describe_target(target)}]" for target in targets)
    return (
        f'Act on these {len(targets)} tx sessions: {described}. '
        f'Use each id (not the name) as the tx target. User request: {request}'
    )


def _live_tx_assistant() -> Session | None:
    """The LIVE tx-assistant, resolved by liveness — NOT by a name lookup (two records share the
    name; the id-sorted first is not reliably the live one)."""
    tmux = Tmux()
    for session in SessionStore().all():
        if session.name == ASSISTANT_NAME and session.is_alive() and tmux.has_session(session.id):
            return session
    return None


def message_assistant(target_ids: list[str], request: str) -> tuple[int, dict]:
    """Compose a context+request line for the selected session(s) and type it into the LIVE
    tx-assistant's pane (send-keys + the 0.3s Enter pause). Delivered as a plain user line — not a
    <from-claude> peer message — so the assistant treats it as a command."""
    if not (request or "").strip():
        return 400, {"ok": False, "error": "empty request"}
    if not target_ids:
        return 400, {"ok": False, "error": "no sessions selected"}
    by_id = {session.id: session for session in SessionStore().all()}
    targets = [by_id[target_id] for target_id in target_ids if target_id in by_id]
    if not targets:
        return 404, {"ok": False, "error": "no such session"}
    assistant = _live_tx_assistant()
    if assistant is None:
        return 200, {"ok": False, "error": "tx-assistant is not live"}
    line = _compose_message(targets, request)
    subprocess.run(["tmux", "send-keys", "-t", assistant.id, "-l", "--", line])
    time.sleep(0.3)                               # the input box drops an Enter that arrives too fast
    subprocess.run(["tmux", "send-keys", "-t", assistant.id, "Enter"])
    return 200, {"ok": True, "count": len(targets), "assistant": assistant.id}


def message_session(session_id: str, request: str) -> tuple[int, dict]:
    """The drawer's direct reply: type the line into the TARGET session's own pane. Plain text,
    no peer envelope — it is the user speaking, so the target's transcript records a `u` row and
    the lane grows a "you spoke" dot on the next poll."""
    request = " ".join((request or "").split())
    if not request:
        return 400, {"ok": False, "error": "empty request"}
    target = next((s for s in SessionStore().all() if s.id == session_id), None)
    if target is None:
        return 404, {"ok": False, "error": "no such session"}
    if not (target.is_alive() and Tmux().has_session(target.id)):
        return 200, {"ok": False, "error": "session is not live"}
    subprocess.run(["tmux", "send-keys", "-t", target.id, "-l", "--", request])
    time.sleep(0.3)                               # the input box drops an Enter that arrives too fast
    subprocess.run(["tmux", "send-keys", "-t", target.id, "Enter"])
    return 200, {"ok": True, "target": target.id}


class BrowserHandler(BaseHTTPRequestHandler):
    """The HTTP surface — reads are GET; the only writes are the timeline POSTs (`do_POST`).

      chats:      `/api/chats` (list) · `/api/chats/<txid>` (detail) ·
                  `/api/chats/<txid>/dialogue` (full conversation turns)
      artifacts:  `/api/artifacts` (list) · `/api/artifacts/<id>` (detail) ·
                  `/api/artifacts/<id>/raw?rev=N` (bytes) · `/api/artifacts/<id>/diff?a=X&b=Y` ·
                  `/api/artifacts/<id>/render?rev=N` (an html artifact AS a page, sandboxed)
      timeline:   `/api/timeline?days=N` (swimlanes) · `/api/timeline/<txid>/dialogue` (drawer
                  feed) · POST `/api/timeline/message` (edit-mode send to tx-assistant) ·
                  POST `/api/timeline/<txid>/message` (drawer reply) · POST `/api/timeline/order`
      page:       `/`
    """

    def _respond(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")  # a live render — never serve a stale copy
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._respond(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def do_POST(self) -> None:
        """The timeline writes. `/api/timeline/message` — the edit-mode send to tx-assistant,
        body `{ids: [uuid...], request: str}` (the exact path wins over the per-session pattern).
        `/api/timeline/<txid>/message` — the drawer's direct reply, body `{request: str}`.
        `/api/timeline/order` — the dragged group sequence. Ids are boundary-validated to
        canonical uuids exactly like the GET routes."""
        path = urllib.parse.urlparse(self.path).path
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 65536)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            self._json(400, {"ok": False, "error": "malformed JSON body"})
            return
        if path == "/api/timeline/message":
            ids = payload.get("ids")
            if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
                self._json(400, {"ok": False, "error": "ids must be a list of strings"})
                return
            safe_ids = [i for i in ids if _UUID_RE.fullmatch(i)]
            status, body = message_assistant(safe_ids, str(payload.get("request") or ""))
            self._json(status, body)
        elif path.startswith("/api/timeline/") and path.endswith("/message"):
            session_id = _safe_artifact_id(path[len("/api/timeline/") : -len("/message")])
            if session_id is None:
                self._respond(404, b"not found\n", "text/plain; charset=utf-8")
                return
            status, body = message_session(session_id, str(payload.get("request") or ""))
            self._json(status, body)
        elif path == "/api/timeline/order":
            order = payload.get("order")
            ok = (isinstance(order, list) and len(order) <= 300
                  and all(isinstance(k, str) and 0 < len(k) <= 200 for k in order))
            if not ok:
                self._json(400, {"ok": False, "error": "order must be a list of group names"})
                return
            timeline_view.save_group_order(order)
            self._json(200, {"ok": True, "count": len(order)})
        else:
            self._respond(404, b"not found\n", "text/plain; charset=utf-8")

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        # keep_blank_values so `?rev=` counts as PRESENT-but-empty (a malformed value to reject),
        # not as absent — presence and parse must stay distinguishable (QA P2).
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        if path in ("/", "/index.html"):
            self._respond(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/chats":
            self._json(200, chats_payload())
        elif path.startswith("/api/chats/"):
            self._route_chat(path[len("/api/chats/") :])
        elif path == "/api/artifacts":
            self._json(200, list_payload())
        elif path.startswith("/api/artifacts/"):
            self._route_artifact(path[len("/api/artifacts/") :], query)
        elif path == "/api/timeline":
            days = self._timeline_days(query)
            if days is not None:
                self._json(200, timeline_view.timeline_payload(days))
        elif path.startswith("/api/timeline/"):
            self._route_timeline(path[len("/api/timeline/") :], query)
        else:
            self._respond(404, b"not found\n", "text/plain; charset=utf-8")

    def _timeline_days(self, query: dict) -> float | None:
        """The timeline window in days: absent -> 7, malformed -> 400 (returns None after
        responding), silly values clamped to [0.25, 90]."""
        if "days" not in query:
            return 7.0
        try:
            return min(90.0, max(0.25, float(query["days"][0])))
        except ValueError:
            self._respond(400, b"days must be a number\n", "text/plain; charset=utf-8")
            return None

    def _route_timeline(self, rest: str, query: dict) -> None:
        """`<txid>/dialogue` — the drawer feed for one lane. Same uuid boundary validation as the
        chat/artifact routes: a non-uuid 404s before the store is touched."""
        if not rest.endswith("/dialogue"):
            self._respond(404, b"not found\n", "text/plain; charset=utf-8")
            return
        session_id = _safe_artifact_id(rest[: -len("/dialogue")])
        days = self._timeline_days(query)
        if days is None:
            return
        payload = timeline_view.lane_dialogue(session_id, days) if session_id is not None else None
        self._json(200 if payload is not None else 404, payload or {"error": "no such lane"})

    def _route_chat(self, rest: str) -> None:
        """`<txid>` (detail) | `<txid>/dialogue` (full turns). The id is validated to a canonical
        uuid at this boundary, exactly like the artifact routes — a non-uuid 404s before any store
        or filesystem path is touched."""
        if rest.endswith("/dialogue"):
            session_id = _safe_artifact_id(rest[: -len("/dialogue")])
            payload = dialogue_payload(session_id) if session_id is not None else None
        else:
            session_id = _safe_artifact_id(rest)
            payload = chat_detail_payload(session_id) if session_id is not None else None
        self._json(200 if payload is not None else 404, payload or {"error": "no such session"})

    def _route_artifact(self, rest: str, query: dict) -> None:
        """`<id>` (detail) | `<id>/raw` (bytes) | `<id>/render` (an html artifact as a page) |
        `<id>/diff` (difflib). The id is validated to a canonical uuid at this boundary — a non-uuid
        (a traversal payload) 404s before the store is ever touched; rev params must be plain
        integers (`_int_param`).

        `/raw` and `/render` serve the same bytes under deliberately opposite contracts: `/raw` is
        `application/octet-stream` + `attachment`, so a browser SAVES it and never parses it, while
        `/render` is the one route that hands artifact bytes to the HTML parser — and pays for that
        with `RENDER_HEADERS` and the reader's sandboxed frame."""
        if rest.endswith("/raw"):
            artifact_id = _safe_artifact_id(rest[: -len("/raw")])
            rev = self._rev_param(query)   # absent => the working copy; malformed => already answered
            if rev is _MALFORMED_PARAM:
                return
            result = raw_bytes(artifact_id, rev) if artifact_id else None
            if result is None:
                self._respond(404, b"not found\n", "text/plain; charset=utf-8")
                return
            data, filename = result
            self._respond(
                200, data, "application/octet-stream",
                {"Content-Disposition": _content_disposition(filename)},
            )
        elif rest.endswith("/render"):
            artifact_id = _safe_artifact_id(rest[: -len("/render")])
            rev = self._rev_param(query)
            if rev is _MALFORMED_PARAM:
                return
            data = render_bytes(artifact_id, rev) if artifact_id else None
            if data is None:
                self._respond(404, b"not found\n", "text/plain; charset=utf-8")
                return
            self._respond(200, data, "text/html; charset=utf-8", RENDER_HEADERS)
        elif rest.endswith("/diff"):
            artifact_id = _safe_artifact_id(rest[: -len("/diff")])
            if artifact_id is None:
                self._json(404, {"error": "no such artifact"})
                return
            # Both revs are REQUIRED here, so absent (None) and malformed are equally a 400 — an
            # `isinstance` check covers both without collapsing them upstream.
            rev_a, rev_b = self._int_param(query, "a"), self._int_param(query, "b")
            if not isinstance(rev_a, int) or not isinstance(rev_b, int):
                self._json(400, {"error": "diff needs integer rev params a and b"})
                return
            payload = diff_payload(artifact_id, rev_a, rev_b)
            self._json(200 if payload is not None else 404, payload or {"error": "no such artifact"})
        else:
            artifact_id = _safe_artifact_id(rest)
            payload = detail_payload(artifact_id) if artifact_id is not None else None
            self._json(200 if payload is not None else 404, payload or {"error": "no such artifact"})

    def _rev_param(self, query: dict):
        """The `rev` parameter shared by the two byte routes: the int, `None` when absent (the caller
        serves the working copy), or `_MALFORMED_PARAM` — after this method has ALREADY answered 400,
        the same respond-then-signal shape as `_timeline_days`."""
        rev = self._int_param(query, "rev")
        if rev is _MALFORMED_PARAM:
            self._respond(400, b"rev must be a plain integer\n", "text/plain; charset=utf-8")
        return rev

    def _int_param(self, query: dict, name: str):
        """Three-state, because PRESENCE and PARSE are different questions (QA P2): `None` when the
        key is ABSENT (the caller supplies its own default), `_MALFORMED_PARAM` when it is PRESENT
        but not a plain integer (the caller must reject it), else the int. Collapsing the two let a
        bad `?rev=` masquerade as "no rev given". `int()` is the parse, so `--5`, `1.5`, `0x1` and an
        empty value are all refused rather than crashing or silently passing."""
        values = query.get(name)
        if not values:
            return None
        try:
            return int(values[0])
        except ValueError:
            return _MALFORMED_PARAM

    def log_message(self, message_format: str, *args) -> None:
        # One quiet line per request — the default handler logs more than a prototype needs.
        sys.stderr.write(f"  {self.command} {self.path}\n")


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    # Default stays loopback-only; pass a HOST (e.g. a Tailscale IP) to read it from another device.
    host = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
    try:
        server = ThreadingHTTPServer((host, port), BrowserHandler)
    except OSError as error:
        raise SystemExit(
            f"cannot bind {host}:{port} — {error}.\n"
            f"pass a free port: python3.14 {Path(__file__).name} <PORT>"
        )
    url = f"http://{host}:{port}/"
    print(f"tx-ide chats + artifacts  →  {url}")
    if host != "127.0.0.1":
        print(f"NOTE: bound to {host} — anyone who can reach this address can read your chats and artifacts.")
    print(f"reading sessions from   {sessions_dir()}")
    print(f"reading history from    {history_dir()}")
    print(f"reading artifacts from  {artifacts_dir()}")
    print("stores are read-only (no EventLog); writes = timeline pane sends + group order. Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye.")
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
