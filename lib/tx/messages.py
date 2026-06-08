"""Reconstruct the inter-agent + user message stream from chat transcripts (read-only, no LLM).

A message in tx is **typed into the recipient's input**: `tx send-message` types a
`<from-claude session="…">…</from-claude>` envelope into the target pane; the prefix+/ forward and
the viewer composer type a line into the tx-assistant; and you typing into a session is plain
input. Claude Code records every one of these verbatim as a `user` turn in the recipient's
transcript, which tx already mirrors into `$TX_IDE_HOME/history/<tx-id>/<chat>/transcript.jsonl`.
So the whole message stream is re-derivable from those transcripts by **structure alone** — no
model, no second persistence layer (D8's log stays body-less provenance).

The **recipient** is the session that owns the transcript; the **sender** is the envelope attribute
(peer messages) or you (everything else). Classification keys on the leading marker of a genuine
typed turn:

    <from-claude session="X">…</from-claude>   peer message — the agent↔agent channel (COMMON.md)
    <tx-command-prompt …/> <text>              you → tx-assistant via prefix+/ (bin/tx-assistant)
    Act on tx session… / Act on these N…       you → tx-assistant via the viewer composer
    (no marker, plain text)                     you → this session, typed directly

Only genuine input turns count: `type==user`, `message.role==user`, `userType==external`, content
is plain text (a string or all-`text` blocks — a `tool_result` line is the harness, not a turn),
not `isMeta`, not a sub-agent `isSidechain`. That structural gate is what excludes the noise that
*shares* these markers — a tool result that read a file documenting the envelope, an assistant
quoting it (`type==assistant`) — which a naïve grep would over-count ~40:1 (measured). Spawn
priming (the launch prompt, found verbatim in the session's `cmd`, or the role-file read template),
harness `<task-notification>` / slash-command scaffolding, and `[Request interrupted]` markers are
dropped: nobody *sent* those.

`collect_messages` is the only entry point that touches the filesystem; `parse_message` is the pure
per-turn classifier (no I/O) the test drives directly.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from . import history
from .session import Role, Session
from .storage import history_dir
from .store import SessionStore

# A genuine peer message: the whole turn IS the envelope (greedy body runs to the LAST close tag, so
# a body that itself mentions `</from-claude>` still closes correctly). DOTALL — bodies are one
# logical line but may carry escaped newlines.
_PEER = re.compile(r'^<from-claude session="([^"]*)">(.*)</from-claude>\s*\Z', re.S)
# The prefix+/ forward: a self-closing focus envelope, then the user's actual message after it.
_COMMAND_PROMPT = re.compile(r"^<tx-command-prompt\b[^>]*/>\s*(.*)\Z", re.S)
# The viewer composer's fixed template (server.py `_compose_message`).
_COMPOSER = re.compile(r"^Act on (?:tx session|these \d+ tx sessions)\b", re.S)
# Harness-injected user turns that are not messages anyone sent.
_HARNESS_PREFIXES = ("<task-notification", "<local-command", "<command-", "[Request interrupted")

SENDER_YOU = "you"
KIND_AGENT = "agent"   # an llm session sent it (the inter-agent channel)
KIND_YOU = "you"       # you sent it (typed, prefix+/, composer, or send-message from your home)


@dataclass
class Message:
    """One reconstructed message. `kind` drives the table's Kind column + color; `via` records how
    it was captured (provenance, surfaced in the detail drawer); `uuid` is the transcript line id —
    a stable React-ish key and the dedup key across the bundle∪live tail and forked transcripts."""

    ts: float          # epoch seconds — the sort key
    iso: str           # the original ISO-8601 timestamp, verbatim
    kind: str          # KIND_AGENT | KIND_YOU
    via: str           # "send-message" | "prompt" | "composer" | "typed"
    sender: str        # peer sender name, or "you"
    recipient: str     # recipient session display name (its id when the record is gone)
    recipient_id: str  # the tx session id that owns the transcript
    body: str          # the message text
    chat_id: str       # the chat uuid the turn lives in
    uuid: str          # the transcript line uuid

    def to_dict(self) -> dict:
        return asdict(self)


def _is_priming(text: str) -> bool:
    """Whether this plain turn is spawn priming, not a message. The role-file read instruction every
    worker/assistant opens with (COMMON.md / bin/tx-assistant) — matched near the start so a later
    quote of it in a real message does not trip."""
    head = text[:400]
    return (head.startswith("Read ") and ".tx-ide/agents/" in head) or "as your first actions" in head


def _plain_text(content: object) -> str | None:
    """The typed text of a user turn, or None if this is not a plain-text turn. A string is the
    text; an all-`text` block list is joined; ANY non-text block (a `tool_result`, an image) means
    this is harness machinery, not something a human/peer typed — so it is excluded."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        blocks = [block for block in content if isinstance(block, dict)]
        if blocks and all(block.get("type") == "text" for block in blocks):
            return "\n".join(block.get("text", "") for block in blocks)
    return None


def _parse_timestamp(iso: object) -> tuple[float, str]:
    """An ISO-8601 transcript timestamp → (epoch seconds, original string). `Z` → `+00:00` for
    `fromisoformat`. Unparseable / missing sorts to the epoch but keeps whatever string was there."""
    if not isinstance(iso, str) or not iso:
        return 0.0, ""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp(), iso
    except ValueError:
        return 0.0, iso


def parse_message(
    obj: dict,
    *,
    recipient_id: str,
    recipient_name: str,
    recipient_cmd: str,
    chat_id: str,
    sender_role: Callable[[str], Role | None],
) -> Message | None:
    """Classify ONE transcript line into a `Message`, or None if it is not a message anyone sent.
    Pure — no filesystem, no store — so the test drives it with synthetic turns.

    `sender_role(name)` resolves a peer sender's session role (to split the agent↔agent channel from
    a `tx send-message` you ran from your own home). `recipient_cmd` is the recipient session's
    launch command — a plain turn whose text is embedded there is the spawn prompt, not a message."""
    if obj.get("type") != "user":
        return None
    message = obj.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return None
    if obj.get("isMeta") or obj.get("isSidechain"):
        return None
    user_type = obj.get("userType")
    if user_type is not None and user_type != "external":
        return None
    text = _plain_text(message.get("content"))
    if text is None:
        return None
    classified = _classify(text.lstrip(), recipient_cmd, sender_role)
    if classified is None:
        return None
    kind, via, sender, body = classified
    if not body:
        return None
    ts, iso = _parse_timestamp(obj.get("timestamp"))
    uuid = obj.get("uuid") or f"{chat_id}:{iso}:{len(body)}"
    return Message(
        ts=ts, iso=iso, kind=kind, via=via, sender=sender,
        recipient=recipient_name or recipient_id, recipient_id=recipient_id,
        body=body, chat_id=chat_id, uuid=uuid,
    )


def _classify(
    text: str, recipient_cmd: str, sender_role: Callable[[str], Role | None]
) -> tuple[str, str, str, str] | None:
    """(kind, via, sender, body) for a genuine plain-text turn, or None if it is not a message.
    `text` is already left-stripped."""
    peer = _PEER.match(text)
    if peer:
        sender = peer.group(1)
        role = sender_role(sender)
        # The envelope IS the inter-agent channel; only a send from a non-llm home (you ran
        # `tx send-message`) flips it to "you". An unknown sender stays on the agent channel.
        kind = KIND_YOU if role is not None and role != Role.LLM else KIND_AGENT
        return kind, "send-message", sender, peer.group(2).strip()
    prompt = _COMMAND_PROMPT.match(text)
    if prompt:
        return KIND_YOU, "prompt", SENDER_YOU, prompt.group(1).strip()
    if _COMPOSER.match(text):
        return KIND_YOU, "composer", SENDER_YOU, text.strip()
    if any(text.startswith(prefix) for prefix in _HARNESS_PREFIXES):
        return None
    if _is_priming(text):
        return None
    stripped = text.strip()
    if stripped and recipient_cmd and stripped in recipient_cmd:
        return None  # the launch prompt embedded in the spawn command — not a message
    return KIND_YOU, "typed", SENDER_YOU, stripped


# ----- collection (the only filesystem-touching path) --------------------------------------------

def collect_messages(store: SessionStore | None = None) -> list[Message]:
    """Every message across all chat history, oldest-first.

    The base source is the durable `~/.tx-ide/history` bundles — every transcript tx has ever
    ingested — so a message survives a `tx rm` of the session that received it (the record is gone,
    the bundle is not). For a still-live session the live engine transcript is read
    too, contributing the tail not yet ingested (deduped by line uuid against the bundle). The
    recipient is the session that owns the transcript; its display name + launch cmd come from the
    record when it still exists, else the bundle's own tx-id stands in for the name."""
    store = store if store is not None else SessionStore()
    sessions = store.all()
    role_of = _role_resolver(sessions)
    session_by_id = {session.id: session for session in sessions}

    messages: list[Message] = []
    seen: set[str] = set()
    for path, recipient_id, recipient_name, recipient_cmd, chat_id in _iter_sources(
        sessions, session_by_id
    ):
        _collect_from_file(
            path, recipient_id, recipient_name, recipient_cmd, chat_id, role_of, messages, seen
        )
    messages.sort(key=lambda message: message.ts)
    return messages


def source_signature(store: SessionStore | None = None) -> str:
    """A cheap, stat-only digest of every message source — (path, size, mtime). It changes exactly
    when a transcript grows or a new one appears, so the dashboard rebuilds the (≈170 ms) message
    feed only on a real change instead of rescanning every poll tick."""
    store = store if store is not None else SessionStore()
    sessions = store.all()
    session_by_id = {session.id: session for session in sessions}
    parts: list[str] = []
    for path, *_recipient in _iter_sources(sessions, session_by_id):
        try:
            status = path.stat()
        except OSError:
            continue
        parts.append(f"{path}:{status.st_size}:{status.st_mtime_ns}")
    return hashlib.sha1("\n".join(parts).encode()).hexdigest()


def _iter_sources(
    sessions: list[Session], session_by_id: dict[str, Session]
) -> Iterator[tuple[Path, str, str, str, str]]:
    """Yield (path, recipient_id, recipient_name, recipient_cmd, chat_id) for every transcript to
    read — the durable `~/.tx-ide/history` bundles first (the base), then live transcripts of
    still-alive sessions (the not-yet-ingested tail). One source of truth for both the collector and
    the change signature, so they can never drift on which files are in scope."""
    for bundle in sorted(history_dir().glob("*/*/transcript.jsonl")):
        recipient_id = bundle.parent.parent.name
        chat_id = bundle.parent.name
        session = session_by_id.get(recipient_id)
        yield (
            bundle, recipient_id,
            session.name if session else recipient_id,
            session.cmd if session else "",
            chat_id,
        )
    for session in sessions:
        if not session.is_alive():
            continue
        for chat in session.chats:
            if chat.id is None:
                continue
            live = history.resolve_transcript(chat.id, chat.cwd)
            if live is not None:
                yield (live, session.id, session.name, session.cmd, chat.id)


def _role_resolver(sessions: list[Session]) -> Callable[[str], Role | None]:
    """name → Role for the most-recently-active record of that name (names are reusable, D7, so
    prefer the freshest). Returns a lookup that yields None for an unknown name."""
    best: dict[str, tuple[float, Role]] = {}
    for session in sessions:
        activity = session.last_activity or 0.0
        current = best.get(session.name)
        if current is None or activity > current[0]:
            best[session.name] = (activity, session.role)
    role_by_name = {name: role for name, (_activity, role) in best.items()}
    return role_by_name.get


def _collect_from_file(
    path: Path,
    recipient_id: str,
    recipient_name: str,
    recipient_cmd: str,
    chat_id: str,
    role_of: Callable[[str], Role | None],
    out: list[Message],
    seen: set[str],
) -> None:
    """Parse one transcript, appending each first-seen message. Dedup is global by line uuid, so a
    bundle/live overlap or a forked transcript copied under two tx-ids never doubles a message."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = parse_message(
            obj,
            recipient_id=recipient_id,
            recipient_name=recipient_name,
            recipient_cmd=recipient_cmd,
            chat_id=chat_id,
            sender_role=role_of,
        )
        if message is None or message.uuid in seen:
            continue
        seen.add(message.uuid)
        out.append(message)
