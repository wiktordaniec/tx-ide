#!/usr/bin/env python3.14
"""A local dashboard that draws the tx-ide session forest as a parent/child graph.

Reads the durable records under `$TX_IDE_HOME/sessions/<uuid>.json` through the real
`SessionStore`, so the state model, the `needs_attention` gating, and the tag-chip colors
match the `tx` picker exactly — no second source of truth to drift. It then serves the
records to `index.html`, which lays them out as a force-directed graph (edges from the
record `parent` field, matched by id or name) you can pan, drag, and click for details.

Run it from anywhere:

    python3.14 prototypes/sessions-graph/server.py [PORT]

then open the printed URL. The records are re-read on every request, so a browser refresh
(or the page's own auto-refresh) always shows current state. `$TX_IDE_HOME` overrides the
home it reads, exactly as for `tx` itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# bin/tx points PYTHONPATH at <repo>/lib; this standalone script does the same from its own
# location (prototypes/sessions-graph/server.py → repo root is two parents up) so `import tx`
# resolves the bundled package regardless of the cwd it is launched from.
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from tx import history                   # noqa: E402
from tx.engines import claude as claude_engine  # noqa: E402
from tx.messages import collect_messages, source_signature  # noqa: E402
from tx.palette import tag_cube          # noqa: E402  — path is set on the line above
from tx.render import reltime            # noqa: E402
from tx.session import ChatRef, Role, Session, State  # noqa: E402
from tx.storage import sessions_dir, tx_ide_home   # noqa: E402
from tx.store import SessionStore        # noqa: E402
from tx.tmux import Tmux                 # noqa: E402

HERE = Path(__file__).resolve().parent
PAGE = HERE / "index.html"
DEFAULT_PORT = 8765
# A running dashboard advertises its port here so the tmux focus hook (bin/tx-graph-focus-poke) can
# POST /api/focus-changed to it — and finds nothing to poke when the dashboard is down (file absent).
# Written on startup, removed on exit. `$TX_IDE_HOME` is resolved exactly as `tx` resolves it.
ENDPOINT_FILE = tx_ide_home() / "sessions-graph.port"
# The positions the user drags nodes to are persisted here — one JSON map {session-id: {x, y}} — so
# the graph's layout is restored from the SERVER on every page load instead of from a single browser's
# localStorage (which never followed the user to another browser or machine). Keyed by the same id the
# records use; `$TX_IDE_HOME` is resolved exactly as `tx` resolves it, like ENDPOINT_FILE above.
LAYOUT_FILE = tx_ide_home() / "sessions-graph.layout.json"
# How often the shared poll loop rebuilds the feed and pushes any change to connected SSE clients.
# One tmux reconcile per tick total (not per tab); the loop idles entirely when no tab is connected.
POLL_INTERVAL_SECONDS = 1.0
# Terminal app raised to the foreground after a focus so the user can type into the just-focused
# pane right away (the double-click happened in the browser). Override with $TX_DASH_TERMINAL_APP.
TERMINAL_APP = os.environ.get("TX_DASH_TERMINAL_APP", "iTerm")


def cube_to_hex(cube_index: int) -> str:
    """Map an xterm-256 color index to `#rrggbb`, so the browser paints tag chips in the same
    colors the terminal picker uses. `palette.tag_cube` returns these indices; the 16–231 block
    is the 6×6×6 color cube, 232–255 is the grayscale ramp."""
    if 16 <= cube_index <= 231:
        offset = cube_index - 16
        red, green, blue = offset // 36, (offset // 6) % 6, offset % 6

        def channel(step: int) -> int:
            return 0 if step == 0 else 55 + 40 * step

        return f"#{channel(red):02x}{channel(green):02x}{channel(blue):02x}"
    grey = 8 + 10 * (cube_index - 232)
    return f"#{grey:02x}{grey:02x}{grey:02x}"


def session_payload(session: Session, now: float) -> dict:
    """The per-session bundle the page consumes: the on-disk record verbatim (the graph reads
    `parent`/`name`/`state` from it), a few derived display fields, and the pretty-printed JSON
    shown in the click-through preview."""
    record = session.to_dict()
    return {
        "record": record,
        "derived": {
            "needs_attention": session.needs_attention,
            "is_terminal": session.state.is_terminal,
            "started_rel": reltime(session.created_at, now),
            "idle_rel": reltime(session.activity_at, now),
            "tag_colors": {tag: cube_to_hex(tag_cube(tag)) for tag in session.tags},
        },
        "json": json.dumps(record, indent=2),
    }


def build_feed() -> dict:
    """Load every record and shape the JSON the page renders, newest-activity first (the picker's
    recency ordering — the page seeds newcomers into the force layout in this order)."""
    now = time.time()
    sessions = sorted(
        SessionStore().all(), key=lambda session: session.activity_at, reverse=True
    )
    # Attachment is compute-on-read (attachment-topology §4): a record's on-disk `attached_to` is
    # stamped only on an actual mutation (spawn / rename / state change), so it goes stale on a plain
    # attach or detach. Re-stamp every session from ONE live `attachment_map` sweep — exactly as
    # `SessionService.live_sessions` (and thus `tx ls` / the picker) does — so the graph's LOCATION
    # readout reflects where each session is surfaced right now, never the stale persisted value. A
    # terminal session isn't a key in the map, so it correctly resolves to [] (attached nowhere).
    attachment = Tmux().attachment_map()
    for session in sessions:
        session.attached_to = attachment.get(_tmux_name(session), [])
    return {
        "generated_at": now,
        "home": str(sessions_dir()),
        "sessions": [session_payload(session, now) for session in sessions],
    }


def build_messages_feed() -> dict:
    """The message stream the Messages tab renders — every inter-agent + user message reconstructed
    from `~/.tx-ide` chat history (`tx.messages`, no model), each with a relative-time string for the
    table. Served on demand and pushed over the `messages` SSE channel when a transcript changes."""
    now = time.time()
    return {
        "generated_at": now,
        "messages": [
            {**message.to_dict(), "rel": reltime(message.ts, now) if message.ts else ""}
            for message in collect_messages()
        ],
    }


# ----- inbox (the chat/inbox tab) -----------------------------------------------------------------
# The inbox is a DERIVED view, never a store: an item is a live llm session + why it's waiting +
# the dialogue reconstructed from its transcript tail. Because the item is the session, answering
# it anywhere (this inbox, tmux directly, another agent) resolves it — nothing to mark read,
# nothing to go stale. Signal-style ordering: newest activity first, state shown but never sorted.

INBOX_TAIL_BYTES = 512 * 1024   # transcript tail window — plenty for the recent dialogue
THREAD_TURNS = 40               # dialogue turns shipped per thread

def _latest_chat(session: Session) -> ChatRef | None:
    """The session's live conversation: the last ChatRef carrying a real id, preferring one still
    open (mirrors cli._latest_chat — forks/rollovers append in order, so the last is the thread)."""
    candidates = [chat for chat in session.chats if chat.id is not None]
    if not candidates:
        return None
    open_chats = [chat for chat in candidates if chat.ended_at is None]
    return (open_chats or candidates)[-1]


def _transcript_of(session: Session) -> Path | None:
    chat = _latest_chat(session)
    if chat is not None and chat.id is not None:
        resolved = history.resolve_transcript(chat.id, chat.cwd, chat.engine)
        if resolved is not None:
            return resolved
    return _drifted_transcript(session)


def _drifted_transcript(session: Session) -> Path | None:
    """Chat-id drift fallback (see remote-control/server.py): a session resumed/rolled over
    outside tx chat-ops carries a chat id whose transcript no longer exists — recover with the
    newest unclaimed transcript in the session's own project dir."""
    try:
        directory = claude_engine.transcript_path("_", session.cwd).parent
    except Exception:
        return None
    claimed = {
        chat.id
        for other in SessionStore().all() if other.id != session.id
        for chat in other.chats if chat.id
    }
    candidates = [p for p in directory.glob("*.jsonl") if p.stem not in claimed]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _tail_entries(path: Path) -> list[dict]:
    """The parsed main-chain entries of a transcript's tail. Reads only the last INBOX_TAIL_BYTES
    (a long session's transcript can be tens of MB), drops the first line when the window started
    mid-line, parses tolerantly (a partial trailing line during a live turn is skipped, not fatal),
    and filters sub-agent sidechains — their turns are not this conversation."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > INBOX_TAIL_BYTES:
                handle.seek(size - INBOX_TAIL_BYTES)
            raw = handle.read().decode(errors="replace")
    except OSError:
        return []
    lines = raw.splitlines()
    if size > INBOX_TAIL_BYTES and lines:
        lines = lines[1:]
    entries = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and not obj.get("isSidechain"):
            entries.append(obj)
    return entries


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


def _dialogue_turns(entries: list[dict], launch_cmd: str = "") -> list[dict]:
    """The transcript tail as chat turns: your typed lines, the agent's text replies, and runs of
    tool activity collapsed to one `{who: "tools", count: N}` separator (the thread shows
    `· N tool calls ·`, never the calls themselves). Harness-injected user lines (tool_result
    contents, isMeta scaffolding) are not turns, and neither is the spawn priming — a user line
    whose text is embedded verbatim in the session's launch command (the messages.py rule)."""
    turns: list[dict] = []
    pending_tools = 0

    def flush_tools() -> None:
        nonlocal pending_tools
        if pending_tools:
            turns.append({"who": "tools", "count": pending_tools, "text": "", "ts": 0.0})
            pending_tools = 0

    for entry in entries:
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        kind, ts = entry.get("type"), _entry_ts(entry)
        if kind == "assistant":
            content = message.get("content")
            text = _text_of(content)
            if isinstance(content, list):
                pending_tools += sum(
                    1 for block in content
                    if isinstance(block, dict) and block.get("type") == "tool_use"
                )
            if text:
                flush_tools()
                turns.append({"who": "agent", "text": text, "ts": ts})
        elif kind == "user" and not entry.get("isMeta"):
            text = _text_of(message.get("content"))
            if (
                text
                and not any(text.lstrip().startswith(p) for p in ("<task-notification", "<local-command", "<command-", "[Request interrupted"))
                and not (launch_cmd and text.strip() in launch_cmd)
            ):
                flush_tools()
                turns.append({"who": "you", "text": text, "ts": ts})
    flush_tools()
    return turns[-THREAD_TURNS:]


def _blocked_on(entries: list[dict]) -> dict | None:
    """What a WAITING session is stalled on, or None when its last turn simply ended. Heuristic:
    the last assistant entry's `tool_use` blocks with no later matching `tool_result` mean the
    engine is holding a dialog — a permission prompt or an AskUserQuestion — for exactly that
    call. Returns {tool, input} with a short input preview."""
    last_uses: list[dict] = []
    answered: set[str] = set()
    for entry in entries:
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if entry.get("type") == "assistant" and isinstance(content, list):
            uses = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
            if uses:
                last_uses = uses
        elif entry.get("type") == "user" and isinstance(content, list):
            answered.update(
                b.get("tool_use_id") for b in content
                if isinstance(b, dict) and b.get("type") == "tool_result"
            )
    for use in last_uses:
        if use.get("id") not in answered:
            raw_input = json.dumps(use.get("input", {}), ensure_ascii=False)
            return {
                "tool": use.get("name", "?"),
                "input": raw_input[:200] + ("…" if len(raw_input) > 200 else ""),
            }
    return None


def build_inbox_feed() -> dict:
    """The inbox tab's feed: one item per live llm session — its record basics, why it needs you
    (`blocked` / `ready`) or what it's doing (`working` / `idle`), the agent's last words as the
    row preview, and the recent dialogue as the thread. Signal-ordered: newest last-event first."""
    now = time.time()
    items = []
    for session in SessionStore().all():
        if session.role != Role.LLM or not session.is_alive():
            continue
        transcript = _transcript_of(session)
        entries = _tail_entries(transcript) if transcript else []
        turns = _dialogue_turns(entries, session.initial_cmd or "")
        blocked = _blocked_on(entries) if session.state == State.WAITING else None
        if session.state == State.WAITING:
            reason = "blocked" if blocked else "ready"
        else:
            reason = session.state.value  # working | idle
        agent_turns = [turn for turn in turns if turn["who"] == "agent"]
        preview = agent_turns[-1]["text"].split("\n", 1)[0] if agent_turns else ""
        turn_ts = max((turn["ts"] for turn in turns), default=0.0)
        last_ts = max(turn_ts, session.activity_at, session.created_at or 0.0)
        items.append({
            "id": session.id,
            "name": session.name,
            "tags": session.tags,
            "tag_colors": {tag: cube_to_hex(tag_cube(tag)) for tag in session.tags},
            "cwd": session.cwd,
            "state": session.state.value,
            "reason": reason,
            "blocked": blocked,
            "preview": preview[:160],
            "turns": turns,
            "last_ts": last_ts,
            "rel": reltime(last_ts, now) if last_ts else "",
        })
    items.sort(key=lambda item: item["last_ts"], reverse=True)
    return {"generated_at": now, "items": items}


def inbox_signature() -> str:
    """Stat-only change detector for the inbox channel: each live llm session's id + state + its
    transcript's size/mtime. Changes exactly when a session changes state or its conversation
    grows — the poll loop rebuilds the (transcript-reading) feed only then, the same
    idle-pushes-nothing discipline as messages/usage."""
    parts = []
    for session in SessionStore().all():
        if session.role != Role.LLM or not session.is_alive():
            continue
        part = f"{session.id}:{session.state.value}"
        transcript = _transcript_of(session)
        if transcript is not None:
            try:
                stat = transcript.stat()
                part += f":{stat.st_size}:{stat.st_mtime_ns}"
            except OSError:
                pass
        parts.append(part)
    return hashlib.sha1("|".join(sorted(parts)).encode()).hexdigest()


def inbox_reply(session_id: str, text: str) -> tuple[int, dict]:
    """Deliver an inbox reply as typed input into the session's pane — exactly what attaching and
    typing would do, via the proven send-keys + 0.3s + Enter sequence (the input box drops an Enter
    that arrives too fast). Collapsed to one line (send-keys is one line); a request boundary, so
    shape is validated here."""
    text = " ".join((text or "").split())
    if not text:
        return 400, {"ok": False, "error": "empty reply"}
    session = next((s for s in SessionStore().all() if s.id == session_id), None)
    if session is None:
        return 404, {"ok": False, "error": "no such session"}
    tmux = Tmux()
    target = _tmux_name(session)
    if not tmux.has_session(target):
        return 200, {"ok": False, "error": "session is not live"}
    tmux.send_keys(target, text, literal=True)
    time.sleep(0.3)
    tmux.send_keys(target, "Enter")
    return 200, {"ok": True, "name": session.name}


# ----- provider usage (rate limits) --------------------------------------------------------------
# Both Anthropic (Claude Code) and Codex expose the SAME two rolling windows — a 5-hour and a 7-day
# weekly limit, each a used-percentage (0–100) and a `resets_at` (epoch seconds); neither exposes a
# calendar-daily window. We surface both providers in one shared shape: {five_hour, seven_day} with
# `used_percentage` + `resets_at`, plus an `as_of` stamp. The two providers reach us very differently:
#
#   • Codex persists its snapshot itself — every `token_count` event in the JSONL rollout it writes
#     under `$CODEX_HOME/sessions` carries the current account `rate_limits`. We read the newest one
#     live, on each request. No file we own, no auth.
#   • Anthropic's percentages exist ONLY on Claude Code's ephemeral statusline stdin — never written
#     to disk anywhere — so the statusline POSTs them to `/api/anthropic-usage` on each render (the
#     same doorbell pattern as the focus hook) and we hold the latest in memory.

CODEX_HOME = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")

# The newest Anthropic snapshot a Claude Code statusline pushed (in memory only — see the module
# note above). Guarded by a lock because a POST thread writes it while a GET thread reads it.
_anthropic_lock = threading.Lock()
_anthropic_usage: dict | None = None


def _window(used_percentage, resets_at, window_minutes) -> dict | None:
    """One normalized window, or None when the percentage is absent (a provider/window that has
    reported nothing). `resets_at` is epoch seconds and `window_minutes` the window length (300 ⇒ 5h,
    10080 ⇒ weekly) — the page turns the former into a reset countdown and the latter into the window
    label, so a plan with different-sized windows labels itself correctly instead of a hardcoded
    name."""
    if used_percentage is None:
        return None
    return {"used_percentage": used_percentage, "resets_at": resets_at, "window_minutes": window_minutes}


def set_anthropic_usage(snapshot: dict) -> None:
    """Stash the Anthropic snapshot a Claude Code statusline just POSTed. This is the only place
    these window percentages exist — they ride Claude Code's ephemeral statusline stdin and are never
    written to disk — so each render pushes them here, mirroring how the focus hook pokes
    `/api/focus-changed`. A request boundary: the body is shaped here, not trusted downstream. A
    just-restarted server holds None until the next statusline render (seconds away while Claude Code
    is in use)."""
    global _anthropic_usage
    five_hour = snapshot.get("five_hour") or {}
    seven_day = snapshot.get("seven_day") or {}
    # Anthropic's two windows ARE 5h / weekly by name, so synthesize their lengths (300 / 10080 min)
    # to match Codex's window_minutes — the page then labels both providers the same way.
    usage = {
        "five_hour": _window(five_hour.get("used_percentage"), five_hour.get("resets_at"), 300),
        "seven_day": _window(seven_day.get("used_percentage"), seven_day.get("resets_at"), 10080),
        "as_of": time.time(),
    }
    with _anthropic_lock:
        _anthropic_usage = usage


def anthropic_usage() -> dict | None:
    with _anthropic_lock:
        return dict(_anthropic_usage) if _anthropic_usage else None


def read_codex_usage() -> dict | None:
    """The current Codex account rate-limit snapshot, read fresh on each call: the newest
    `token_count` event carrying `rate_limits` across the rollout transcripts Codex writes under
    `$CODEX_HOME/sessions`. Codex's `primary`/`secondary` windows are a 5-hour and a weekly limit
    (`window_minutes` 300 / 10080) — the same two windows Anthropic exposes — so we map them onto
    `five_hour`/`seven_day` for one shared cross-provider shape. None when Codex has reported nothing
    (not logged in, or no API call yet)."""
    sessions_root = CODEX_HOME / "sessions"
    if not sessions_root.is_dir():
        return None
    # Rate limits are account-global, so the freshest snapshot is the newest event — which lives in
    # the most-recently-written rollout. Walk newest-first and stop at the first file that has one;
    # cap the walk so a deep history never makes this scan unbounded.
    rollouts = sorted(
        sessions_root.glob("**/rollout-*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True
    )
    for path in rollouts[:5]:
        snapshot = _latest_codex_rate_limits(path)
        if snapshot is not None:
            return snapshot
    return None


def _latest_codex_rate_limits(path: Path) -> dict | None:
    """The last `token_count` event carrying rate limits in one rollout, normalized — or None. The
    rollout is an external boundary (Codex's format), so each line is parsed tolerantly: a partial
    trailing line during a live session is skipped, not fatal, and a window missing its fields drops
    to None rather than raising."""
    latest = None
    with path.open() as handle:
        for line in handle:
            if '"token_count"' not in line or '"rate_limits"' not in line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = event.get("payload") or {}
            rate_limits = payload.get("rate_limits")
            if payload.get("type") != "token_count" or not rate_limits:
                continue
            # primary = the short (5h) window, secondary = the weekly one; carry each window's
            # window_minutes so the page derives the label from the length, not a fixed name.
            primary = rate_limits.get("primary") or {}
            secondary = rate_limits.get("secondary") or {}
            five_hour = _window(primary.get("used_percent"), primary.get("resets_at"), primary.get("window_minutes"))
            seven_day = _window(secondary.get("used_percent"), secondary.get("resets_at"), secondary.get("window_minutes"))
            if five_hour is None and seven_day is None:
                continue
            latest = {
                "five_hour": five_hour,
                "seven_day": seven_day,
                "plan_type": rate_limits.get("plan_type"),
                "as_of": _iso_to_epoch(event.get("timestamp")),
            }
    return latest


def _iso_to_epoch(timestamp: str | None) -> float | None:
    """The rollout event's ISO-8601 `timestamp` (e.g. `2026-06-08T09:54:23.682Z`) as epoch seconds,
    for the page's "as of" / staleness read; None when absent or unparseable."""
    if not timestamp:
        return None
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def build_usage_feed() -> dict:
    """Both providers' current rate-limit standing — Anthropic from the in-memory statusline push,
    Codex read live from its rollouts. Built fresh per request (the page's on-connect SSE snapshot,
    the on-load fetch, the refresh button) and on each poll tick that `usage_signature` says changed.
    Either provider is null when it has reported nothing yet."""
    return {
        "generated_at": time.time(),
        "anthropic": anthropic_usage(),
        "codex": read_codex_usage(),
    }


def usage_signature() -> str:
    """A cheap stat-only fingerprint of both providers' sources: the Anthropic snapshot's `as_of` and
    the newest Codex rollout's mtime+size. It changes exactly when the statusline POSTs or Codex
    appends a turn, so the poll loop rebuilds + pushes the usage feed only on a real change — the same
    idle-pushes-nothing discipline the messages channel uses (`source_signature`), and far cheaper
    than reading a rollout every tick. The client ticks the reset countdowns itself from the absolute
    `resets_at`, so no per-second push is needed just for the clock."""
    snapshot = anthropic_usage()
    parts = [str(snapshot["as_of"]) if snapshot else "-"]
    sessions_root = CODEX_HOME / "sessions"
    if sessions_root.is_dir():
        newest = max(sessions_root.glob("**/rollout-*.jsonl"), key=lambda path: path.stat().st_mtime, default=None)
        if newest is not None:
            stat = newest.stat()
            parts.append(f"{newest.name}:{stat.st_mtime_ns}:{stat.st_size}")
    return "|".join(parts)


class FeedHub:
    """One shared poll loop that fans live feed changes out to every connected SSE client.

    The first cut had each browser tab poll `/api/sessions` on its own `setInterval`. Two problems:
    every poll reconciles via a `tmux list-sessions` subprocess, so N tabs cost N subprocesses per
    tick; and a browser throttles a backgrounded tab's timer to roughly once a minute, which is the
    "it lags" — the dashboard sat stale while the user worked in another window. Here a single daemon
    thread rebuilds the feed once per `POLL_INTERVAL_SECONDS`, hashes it, and pushes to all clients
    only when it actually changed. The tmux-call count is independent of how many tabs are open, an
    idle forest pushes nothing, and the loop blocks on a condition while no tab is connected so a
    closed dashboard spawns no subprocesses at all.

    Focus rides a SECOND, named SSE channel on the same connections (`event: focus`). It is NOT on
    the poll loop — terminal focus changes never touch a record, so there is nothing on disk to
    notice. Instead a tmux hook POSTs `/api/focus-changed`, which calls `push_focus`; the change is
    deduped and fanned out, so the ring moves the instant a pane is switched and a background tab —
    which can't run its own timers — still updates, because the server is the one pushing.

    Messages ride a THIRD, named channel (`event: messages`) on the same poll loop. Rebuilding the
    message feed scans transcripts (~170 ms), too heavy for every tick, so the loop first hashes a
    cheap stat-only `source_signature` and only rebuilds + pushes when a transcript actually grew —
    the same idle-pushes-nothing discipline as the record feed.

    Provider usage rides a FOURTH, named channel (`event: usage`) on the same poll loop, gated the
    same way on the stat-only `usage_signature` (the statusline's last POST + the newest Codex
    rollout). So the usage strip is current via the SAME stream as everything else — no bespoke
    timer; the client's per-second tick only advances the reset countdown from the absolute
    `resets_at`."""

    def __init__(self, interval: float = POLL_INTERVAL_SECONDS) -> None:
        self._interval = interval
        self._clients: set[queue.Queue] = set()
        self._condition = threading.Condition()
        self._last_hash: str | None = None
        self._last_focused_id: str | None = None
        self._last_messages_sig: str | None = None
        self._last_usage_sig: str | None = None
        self._last_inbox_sig: str | None = None

    def start(self) -> None:
        threading.Thread(target=self._poll_loop, name="feed-poll", daemon=True).start()

    def subscribe(self) -> queue.Queue:
        client: queue.Queue = queue.Queue()
        with self._condition:
            self._clients.add(client)
            self._condition.notify()           # wake the loop if it was idling with no clients
        return client

    def unsubscribe(self, client: queue.Queue) -> None:
        with self._condition:
            self._clients.discard(client)

    def _poll_loop(self) -> None:
        while True:
            with self._condition:
                while not self._clients:
                    self._condition.wait()     # nothing to serve — sleep until a tab connects
            payload = json.dumps(build_feed())
            digest = hashlib.sha1(payload.encode()).hexdigest()
            if digest != self._last_hash:
                self._last_hash = digest
                with self._condition:
                    clients = list(self._clients)
                for client in clients:
                    client.put(("feed", payload))
            self._push_messages_if_changed()
            self._push_usage_if_changed()
            self._push_inbox_if_changed()
            time.sleep(self._interval)

    def _push_messages_if_changed(self) -> None:
        """Rebuild + fan out the message feed only when a transcript actually changed — gated on the
        cheap stat-only signature so the heavy scan never runs on an idle tick."""
        signature = source_signature()
        if signature == self._last_messages_sig:
            return
        self._last_messages_sig = signature
        payload = json.dumps(build_messages_feed())
        with self._condition:
            clients = list(self._clients)
        for client in clients:
            client.put(("messages", payload))

    def _push_usage_if_changed(self) -> None:
        """Rebuild + fan out the usage feed on the `usage` channel only when a provider's source
        actually changed — gated on the cheap stat-only `usage_signature`, the same idle-pushes-
        nothing discipline as messages. This is how the usage strip stays current with no bespoke
        timer: it rides the same poll loop + SSE stream as the records feed."""
        signature = usage_signature()
        if signature == self._last_usage_sig:
            return
        self._last_usage_sig = signature
        payload = json.dumps(build_usage_feed())
        with self._condition:
            clients = list(self._clients)
        for client in clients:
            client.put(("usage", payload))

    def _push_inbox_if_changed(self) -> None:
        """Rebuild + fan out the inbox feed only when a live llm session changed state or its
        transcript grew — gated on the stat-only `inbox_signature`, the same idle-pushes-nothing
        discipline as messages/usage (the rebuild reads transcript tails, too heavy for every tick)."""
        signature = inbox_signature()
        if signature == self._last_inbox_sig:
            return
        self._last_inbox_sig = signature
        payload = json.dumps(build_inbox_feed())
        with self._condition:
            clients = list(self._clients)
        for client in clients:
            client.put(("inbox", payload))

    def push_focus(self) -> None:
        """Recompute the terminal's focused session and fan it out as a named `focus` frame — but
        only when it actually changed (deduped on `_last_focused_id`). Called by `/api/focus-changed`
        (the tmux hook); thread-safe, so concurrent pokes collapse to one broadcast. A just-connected
        tab gets the current focus directly in `_serve_stream`, so it never waits for the next
        change."""
        focused_id = compute_focused_id()
        with self._condition:
            if focused_id == self._last_focused_id:
                return
            self._last_focused_id = focused_id
            clients = list(self._clients)
        payload = json.dumps({"focused_id": focused_id})
        for client in clients:
            client.put(("focus", payload))


hub = FeedHub()


def _tmux_name(session: Session) -> str:
    """The live tmux session name — always the UUID `id`: every record is a process now (views left
    the store), so a session always lives in tmux under its id. (Mirrors `Session.tmux_name`.)"""
    return session.id


def compute_focused_id() -> str | None:
    """The record id the page should ring: the terminal's focused inner session
    (`Tmux.focused_session_name`) resolved to a record. tmux knows a session by its tmux-name (the
    id for a PROCESS, the display name otherwise — `_tmux_name`), while the page keys nodes by record
    id, so map across that. None when nothing is focused (detached / a plain pane / the picker) or
    the focused session has no record on disk."""
    inner_name = Tmux().focused_session_name()
    if inner_name is None:
        return None
    for session in SessionStore().all():
        if _tmux_name(session) == inner_name:
            return session.id
    return None


def raise_terminal() -> None:
    """Bring the terminal app to the foreground (macOS `open -a`). The double-click happened in the
    browser, so the OS foreground is the browser, not the terminal; raising it lets the user start
    typing into the just-focused pane. Best-effort — a wrong app name is a harmless no-op."""
    subprocess.run(["open", "-a", TERMINAL_APP], capture_output=True)


def focus_session(session_id: str) -> tuple[int, dict]:
    """Resolve a record id to its live pane and focus the user's tmux on it (backs the page's
    double-click-to-jump). Read-only on the store; the only state-changing tmux calls are
    select-window / select-pane — no switch-client, since the user's outer client already owns the
    `Views` host that holds the pane (so selecting the window+pane moves exactly that client)."""
    session = next((candidate for candidate in SessionStore().all() if candidate.id == session_id), None)
    if session is None:
        return 404, {"ok": False, "error": "no such session"}

    tmux = Tmux()
    target_name = _tmux_name(session)
    if not tmux.has_session(target_name):
        return 200, {"ok": False, "name": session.name, "error": "not live"}

    locations = tmux.attached_to(target_name)
    if not locations:
        return 200, {"ok": False, "name": session.name, "error": "open in no pane"}

    location = locations[0]                       # primary pane = what the picker would jump to
    target = f"{location.host}:{location.window_index}.{location.pane_index}"
    if not (tmux.select_window(target) and tmux.select_pane(target)):
        return 200, {"ok": False, "name": session.name, "error": "pane vanished"}

    raise_terminal()                              # bring the terminal forward, ready to type
    return 200, {"ok": True, "name": session.name, "location": location.to_dict()}


ASSISTANT_NAME = "tx-assistant"


def _live_tx_assistant() -> Session | None:
    """The LIVE tx-assistant, resolved by liveness — NOT by a name lookup. Two records share the
    name 'tx-assistant' (one exited); a name lookup returns the id-sorted first, which is not
    reliably the live one (the same two-records-by-name trap the focus feature hit)."""
    tmux = Tmux()
    for session in SessionStore().all():
        if session.name == ASSISTANT_NAME and session.is_alive() and tmux.has_session(_tmux_name(session)):
            return session
    return None


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
    the user's free-text request applied to all of them. Collapsed to a single line (send-keys is one
    line). One target reads as a sentence; many are enumerated as bracketed fragments."""
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


def message_assistant(target_ids: list[str], request: str) -> tuple[int, dict]:
    """Compose a context+request line for the dashboard-selected session(s) and type it into the LIVE
    tx-assistant's pane (send-keys + the 0.3s Enter pause, exactly as bin/tx-assistant does). It is
    delivered as a plain user line — not a <from-claude> peer message — so the one-shot assistant
    treats it as a command. Each target's id is embedded so the assistant's own `tx` calls are
    unambiguous despite the two-records-by-name collision."""
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
    assistant_target = _tmux_name(assistant)
    subprocess.run(["tmux", "send-keys", "-t", assistant_target, "-l", "--", line])
    time.sleep(0.3)                               # the input box drops an Enter that arrives too fast
    subprocess.run(["tmux", "send-keys", "-t", assistant_target, "Enter"])
    return 200, {"ok": True, "count": len(targets), "name": targets[0].name, "assistant": assistant.id}


# ----- graph layout persistence ------------------------------------------------------------------
# The page lets you drag session nodes around; those positions are saved here, server-side, and
# restored on the next load. One JSON object {id: {x, y}} keyed by session id — read on load, and
# rewritten whole on every drag / unpin / shuffle (the page always sends its complete pin set). A lock
# guards the file because this ThreadingHTTPServer can read it (a GET) and write it (a POST) at once.
_layout_lock = threading.Lock()


def read_layout() -> dict:
    """The saved node positions {id: {x, y}} — the graph's persisted layout, served for restore-on-
    load. The file is an external boundary: absent until the first save, and a reader can race a
    writer, so a missing or half-written/corrupt file reads as an empty layout rather than raising
    (the same tolerant-parse discipline the Codex rollout reader uses)."""
    with _layout_lock:
        if not LAYOUT_FILE.exists():
            return {}
        try:
            return json.loads(LAYOUT_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}


def write_layout(pins: dict) -> None:
    """Persist the node positions, replacing the file whole. Written atomically — a temp file swapped
    in with os.replace — so a concurrent reader (another tab's GET) never sees a partial write."""
    with _layout_lock:
        LAYOUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp = LAYOUT_FILE.with_name(LAYOUT_FILE.name + ".tmp")
        temp.write_text(json.dumps(pins))
        temp.replace(LAYOUT_FILE)


def set_layout(body: dict) -> tuple[int, dict]:
    """Persist the layout a drag / unpin / shuffle just POSTed. A request boundary, so the shape is
    sanitized here rather than trusted by the page that reads it back: only {id: {x, y}} entries with
    numeric coordinates survive."""
    pins = body.get("pins")
    if not isinstance(pins, dict):
        return 400, {"ok": False, "error": "pins must be an object"}
    clean = {
        str(node_id): {"x": position["x"], "y": position["y"]}
        for node_id, position in pins.items()
        if isinstance(position, dict)
        and isinstance(position.get("x"), (int, float))
        and isinstance(position.get("y"), (int, float))
    }
    write_layout(clean)
    return 200, {"ok": True, "count": len(clean)}


class DashboardHandler(BaseHTTPRequestHandler):
    """Routes: `/api/stream` (Server-Sent live feed + named `focus` channel — one shared loop fans
    changes out to every tab, so the graph stays current even while its tab is unfocused),
    `/api/sessions` (one-shot JSON feed for the initial paint, the manual refresh, and the on-focus
    resync), `/api/messages` (one-shot message feed for the Messages tab; also pushed live on the
    `messages` SSE channel), `/api/usage` (one-shot both-provider rate-limit feed for the header
    readout), `/api/layout` (GET the saved node positions for restore-on-load; POST the page's pin
    set to persist the dragged layout — see read_layout / set_layout), `POST /api/focus` (jump the user's tmux to a session's pane),
    `POST /api/focus-changed` (the tmux hook's poke — recompute & push the focused ring),
    `POST /api/anthropic-usage` (a Claude Code statusline pushing its ephemeral Anthropic
    rate-limit snapshot — see set_anthropic_usage), `POST /api/message` (type a request about one session — `id` — or a multi-select group — `ids` —
    into the live tx-assistant), and `/` (the page). Else 404s."""

    def _respond(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # A local dev dashboard whose page + feeds are edited live — never let the browser serve a
        # stale HTML/JSON copy (a plain reload would otherwise show yesterday's markup from cache).
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_stream(self) -> None:
        """Hold the connection open and stream Server-Sent Events on four channels: the default
        (unnamed) `feed` channel — one snapshot right away so a just-opened (or just-refocused, after
        EventSource reconnects) tab is current at once, then one each time the poll loop sees a change
        — a named `focus` channel pushed by the tmux hook, a named `messages` channel pushed by the
        poll loop when a transcript changes, and a named `usage` channel pushed by the poll loop when
        a provider's rate-limit standing changes. All three named snapshots are sent on connect too,
        so a reconnecting tab re-syncs without waiting for the next change. A comment line every 15s
        keeps the connection warm; a closed tab surfaces as a write error that ends this thread
        cleanly."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")   # don't let any proxy buffer the stream
        self.end_headers()
        client = hub.subscribe()
        try:
            self._sse_send("feed", json.dumps(build_feed()))
            self._sse_send("focus", json.dumps({"focused_id": compute_focused_id()}))
            self._sse_send("messages", json.dumps(build_messages_feed()))
            self._sse_send("usage", json.dumps(build_usage_feed()))
            self._sse_send("inbox", json.dumps(build_inbox_feed()))
            while True:
                try:
                    event, payload = client.get(timeout=15)
                    self._sse_send(event, payload)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass                                      # tab went away — fall through to unsubscribe
        finally:
            hub.unsubscribe(client)

    def _sse_send(self, event: str, payload: str) -> None:
        # SSE frames are newline-delimited; json.dumps emits a single physical line (inner newlines
        # in the pretty-printed `json` field are escaped), so one `data:` line carries the whole
        # payload. The default `feed` event stays unnamed so the page's `onmessage` receives it; any
        # other channel (`focus`) gets a leading `event:` line, read via an `addEventListener`.
        frame = b"" if event == "feed" else b"event: " + event.encode() + b"\n"
        frame += b"data: " + payload.encode() + b"\n\n"
        self.wfile.write(frame)
        self.wfile.flush()

    def do_GET(self) -> None:
        if self.path.startswith("/api/stream"):
            self._serve_stream()
        elif self.path.startswith("/api/sessions"):
            self._respond(200, json.dumps(build_feed()).encode(), "application/json")
        elif self.path.startswith("/api/messages"):
            self._respond(200, json.dumps(build_messages_feed()).encode(), "application/json")
        elif self.path.startswith("/api/usage"):
            self._respond(200, json.dumps(build_usage_feed()).encode(), "application/json")
        elif self.path.startswith("/api/inbox"):
            self._respond(200, json.dumps(build_inbox_feed()).encode(), "application/json")
        elif self.path.startswith("/api/layout"):
            self._respond(200, json.dumps({"pins": read_layout()}).encode(), "application/json")
        elif self.path in ("/", "/index.html"):
            self._respond(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        else:
            self._respond(404, b"not found\n", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}") if self.path.startswith("/api/") else {}
        if self.path.startswith("/api/anthropic-usage"):
            # A Claude Code statusline pushing its ephemeral Anthropic rate-limit snapshot — the only
            # place these percentages exist (see set_anthropic_usage). Stash in memory; the next
            # /api/usage serves it. Fire-and-forget on the statusline's side, like the focus poke.
            set_anthropic_usage(body)
            status, payload = 200, {"ok": True}
        elif self.path.startswith("/api/focus-changed"):
            # The tmux hook's fire-and-forget poke (no body). Recompute the focused session and push
            # it — checked BEFORE `/api/focus`, which would otherwise prefix-swallow this path.
            hub.push_focus()
            status, payload = 200, {"ok": True}
        elif self.path.startswith("/api/focus"):
            status, payload = focus_session(str(body.get("id", "")))
        elif self.path.startswith("/api/inbox/reply"):
            status, payload = inbox_reply(str(body.get("id", "")), str(body.get("text", "")))
        elif self.path.startswith("/api/message"):
            # `ids` (multi-select group) is preferred; `id` stays for the single-node path. This is a
            # request boundary, so the shape is validated here rather than trusted downstream.
            ids = body.get("ids")
            target_ids = [str(value) for value in ids] if isinstance(ids, list) else [str(body.get("id", ""))]
            status, payload = message_assistant(target_ids, str(body.get("request", "")))
        elif self.path.startswith("/api/layout"):
            # The page persisting the layout — its complete {id: {x, y}} pin set, rewritten whole.
            # A request boundary, so set_layout sanitizes the shape before it touches disk.
            status, payload = set_layout(body)
        else:
            self._respond(404, b"not found\n", "text/plain; charset=utf-8")
            return
        self._respond(status, json.dumps(payload).encode(), "application/json")

    def log_message(self, message_format: str, *args) -> None:
        # One quiet line per request — the default handler logs more than a prototype needs.
        sys.stderr.write(f"  {self.command} {self.path}\n")


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    # Default stays loopback-only. Pass a HOST as the second arg (e.g. 0.0.0.0, or a Tailscale IP)
    # to reach the dashboard from a phone — an explicit opt-in, because this server can TYPE INTO
    # your tmux sessions (/api/inbox/reply, /api/message): bind to a trusted network only.
    host = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    hub.start()
    ENDPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)
    ENDPOINT_FILE.write_text(str(port))           # advertise the port so the tmux focus hook can poke
    url = f"http://{host}:{port}/"
    print(f"tx-ide session graph  →  {url}")
    if host != "127.0.0.1":
        print(f"NOTE: bound to {host} — anyone who can reach this address can read AND message your sessions.")
    print(f"reading records from  {sessions_dir()}")
    print(f"focus-hook endpoint   {ENDPOINT_FILE}")
    print("Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye.")
    finally:
        ENDPOINT_FILE.unlink(missing_ok=True)     # stop advertising — the hook no-ops while we're down
        server.shutdown()


if __name__ == "__main__":
    main()
