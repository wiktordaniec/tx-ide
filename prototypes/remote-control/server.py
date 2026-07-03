#!/usr/bin/env python3.14
"""tx remote-control — a phone-first inbox over your live agent sessions, as its OWN service.

The sessions-graph dashboard grew an inbox tab, but a tab inside a desktop dashboard can't be
shaped freely for a phone. This server is that inbox extracted into a standalone service: one
page, one feed, one reply endpoint — nothing else. It shares the same source of truth (the
durable records under `$TX_IDE_HOME/sessions` + the engine transcripts) through the real
`lib/tx`, so what it shows can never drift from `tx ls` / the picker; it shares NO code or
lifecycle with sessions-graph, so the mobile view is fully controllable on its own.

The model is the Signal one: live llm sessions are conversations, ordered by newest activity
only (state is shown per row — never reorders). An item IS the session: reply from here, answer
in tmux, or let another agent unblock it, and the next push resolves it. Reason per item:

    blocked  WAITING with an unanswered tool_use in the transcript tail (permission prompt,
             plan approval, an AskUserQuestion). The tool_use names the tool + command; its
             answer options, when the transcript can't carry them, are read off the pane and
             merged in by `_blocked_state`, so the dialog is answerable from here — not just in
             tmux — via tappable options relayed with send-keys (see `answer_question`).
    ready    WAITING, turn done — the session reported back and waits on you
    working  mid-turn; a reply is typed in and queues as a steering message
    idle     alive, no active turn

Run:  python3.14 prototypes/remote-control/server.py [PORT] [HOST]

HOST defaults to loopback. Binding wider (0.0.0.0, a Tailscale IP) is an explicit opt-in —
this service can TYPE INTO your tmux sessions. For any non-loopback bind, set
`TX_REMOTE_TOKEN=<secret>`: every request must then carry it (`?token=` — the page asks once
and remembers it in localStorage), so a phone on your Wi-Fi is a bearer of the secret, not
just of the network.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Standalone entrypoint: resolve the repo from this file's location (two parents up) so `import
# tx` finds the bundled lib regardless of cwd — the same rule bin/tx and sessions-graph use.
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from tx import history                    # noqa: E402
from tx.palette import tag_cube           # noqa: E402
from tx.render import reltime             # noqa: E402
from tx.session import ChatRef, Kind, Role, Session, State  # noqa: E402
from tx.storage import tx_ide_home        # noqa: E402
from tx.store import SessionStore         # noqa: E402
from tx.tmux import Tmux                  # noqa: E402

HERE = Path(__file__).resolve().parent
# Two faces of the same app: the phone-first inbox (installed as the PWA) and the split-pane
# desktop view. `/` picks by User-Agent; /mobile and /desktop are the explicit overrides.
MOBILE_PAGE = HERE / "mobile.html"
DESKTOP_PAGE = HERE / "desktop.html"
DEFAULT_PORT = 8790
POLL_INTERVAL_SECONDS = 1.0
INBOX_TAIL_BYTES = 512 * 1024   # transcript tail window — plenty for the recent dialogue
THREAD_TURNS = 40               # dialogue turns shipped per thread
TOKEN = os.environ.get("TX_REMOTE_TOKEN", "")
# Phone-attached images land here; the reply typed into the session carries `[img:<abs path>]`,
# which the agent opens with its Read tool (Read handles images natively). Served back to the
# page at /uploads/<name> — by basename only, so this dir is the whole exposable surface.
UPLOADS_DIR = tx_ide_home() / "remote-uploads"
UPLOAD_MAX_BYTES = 15 * 1024 * 1024
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".gif": "image/gif", ".webp": "image/webp", ".heic": "image/heic"}


def cube_to_hex(cube_index: int) -> str:
    """xterm-256 index → #rrggbb, so tag chips match the terminal picker's colors."""
    if 16 <= cube_index <= 231:
        offset = cube_index - 16
        red, green, blue = offset // 36, (offset // 6) % 6, offset % 6

        def channel(step: int) -> int:
            return 0 if step == 0 else 55 + 40 * step

        return f"#{channel(red):02x}{channel(green):02x}{channel(blue):02x}"
    grey = 8 + 10 * (cube_index - 232)
    return f"#{grey:02x}{grey:02x}{grey:02x}"


def _tmux_name(session: Session) -> str:
    return session.id if session.kind == Kind.PROCESS else session.name


def _latest_chat(session: Session) -> ChatRef | None:
    """The session's live conversation: the last ChatRef carrying a real id, preferring one still
    open (forks/rollovers append in order, so the last is the thread)."""
    candidates = [chat for chat in session.chats if chat.id is not None]
    if not candidates:
        return None
    open_chats = [chat for chat in candidates if chat.ended_at is None]
    return (open_chats or candidates)[-1]


def _transcript_of(session: Session) -> Path | None:
    """The transcript of the session's recorded chat — and ONLY that. No guessing: a recorded id
    that resolves to nothing is chat-id drift (a resume/rollover outside tx chat-ops that capture
    never recorded), and surfacing that as an explicit error beats attributing the newest
    transcript in a shared project dir to the wrong session, which is how one session's dialogue
    showed up under another's thread. See `_drift_error`; repair the record to fix the thread."""
    chat = _latest_chat(session)
    if chat is None or chat.id is None:
        return None
    return history.resolve_transcript(chat.id, chat.cwd, chat.engine)


def _drift_error(session: Session, transcript: Path | None) -> str | None:
    """The user-facing drift diagnosis: set exactly when a recorded chat id resolves to no
    transcript on disk (a session with no recorded chat at all is just quiet, not broken)."""
    if transcript is not None:
        return None
    chat = _latest_chat(session)
    if chat is None:
        return None
    return (f"recorded chat {chat.id[:8]}… has no transcript on disk — the chat id drifted; "
            f"repair this session's record to restore the thread")


def _tail_entries(path: Path, max_bytes: int = INBOX_TAIL_BYTES) -> list[dict]:
    """The parsed main-chain entries of a transcript's tail: last `max_bytes` only (a long
    session's transcript can be tens of MB), first line dropped when the window started mid-line,
    tolerant parsing (a partial trailing line during a live turn is skipped), sidechains filtered
    (sub-agent turns are not this conversation)."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
            raw = handle.read().decode(errors="replace")
    except OSError:
        return []
    lines = raw.splitlines()
    if size > max_bytes and lines:
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
    tool activity collapsed to one `{who: "tools", count: N}` separator. Harness-injected user
    lines (tool_result contents, isMeta scaffolding) are not turns, and neither is the spawn
    priming — a user line whose text is embedded verbatim in the launch command."""
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
    return turns


def _blocked_on(entries: list[dict]) -> dict | None:
    """What a WAITING session is stalled on, or None when its turn simply ended: the last
    assistant entry's `tool_use` with no later matching `tool_result` means the engine holds a
    dialog for exactly that call (an AskUserQuestion — see the module note on permission
    prompts). Returns {tool, input} with a short input preview."""
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
            blocked = {
                "tool": use.get("name", "?"),
                "tool_use_id": use.get("id"),
                "input": raw_input[:200] + ("…" if len(raw_input) > 200 else ""),
                "questions": None,
                "answerable": False,
            }
            # AskUserQuestion carries its options structurally — the app renders them as tappable
            # buttons and answers via /api/answer. Only the single-question, single-select shape
            # answers cleanly from its transcript form; every other blocking dialog (a permission
            # prompt, plan approval, a multi-question step) is answered off the pane instead —
            # `_blocked_state` grafts the pane's options on when this leaves `answerable` False.
            if blocked["tool"] == "AskUserQuestion":
                questions = (use.get("input") or {}).get("questions")
                if (
                    isinstance(questions, list) and len(questions) == 1
                    and isinstance(questions[0], dict) and not questions[0].get("multiSelect")
                    and isinstance(questions[0].get("options"), list)
                ):
                    blocked["questions"] = [{
                        "question": str(questions[0].get("question", "")),
                        "options": [
                            {"label": str(o.get("label", "")), "description": str(o.get("description", ""))}
                            for o in questions[0]["options"] if isinstance(o, dict)
                        ],
                    }]
                    blocked["answerable"] = True
            return blocked
    return None


def _context_pct(entries: list[dict], cmd: str) -> int | None:
    """Context used as a percentage of the (guessed) window — the thread header's readout. The
    window guess self-corrects upward when observed usage already exceeds it."""
    meta = _transcript_meta(entries)
    tokens = meta["context_tokens"]
    if not tokens:
        return None
    window = 1_000_000 if "[1m]" in (cmd or "") else 200_000
    if tokens > window:
        window = 1_000_000
    return min(100, round(tokens / window * 100))


# A blocking dialog (permission prompt / plan approval / AskUserQuestion) renders on the pane as a
# numbered option list with a ❯ cursor. Parse it from `tmux capture-pane`; the answer endpoint
# re-captures and compares the content hash, so a stale tap sends nothing. This is the fallback for
# anything the transcript can't carry answerably — the tool_use IS flushed for a permission prompt,
# so `_blocked_on` still names the tool + command, but its options only live here on the pane.
_DIALOG_OPTION = re.compile(r"^\s*(?:❯\s*)?(\d+)\.\s+(.+?)\s*$")
_DIALOG_NOISE = re.compile(r"^[\s─╌═╭╮╰╯│┃▔▁]*$")
# An option label as rendered carries chrome the answer never needs: a trailing key hint
# ((esc), (shift+tab), …) and, in a multi-select dialog, a leading [ ] / [x] checkbox. Strip both
# for display; the presence of a checkbox is also how a multi-select dialog is recognized.
_DIALOG_KEYHINT = re.compile(r"\s*\([^)]*(?:esc|tab|ctrl|enter|space|↵)[^)]*\)\s*$", re.I)
_DIALOG_CHECKBOX = re.compile(r"^\[.\]\s*")
# A bare label/link line ("Security guide", "Learn more") that some dialogs render directly above
# their options — a couple of unpunctuated words. Skipped when it sits right on the options, so
# the real (blank-gap-separated) question above it is what's shown.
_DIALOG_BARE_LABEL = re.compile(r"^[\w][\w ]{0,22}$")


def _clean_option_label(text: str) -> str:
    return _DIALOG_CHECKBOX.sub("", _DIALOG_KEYHINT.sub("", text)).strip()


def _pane_dialog(tmux: Tmux, target: str) -> dict | None:
    """The numbered-option dialog currently rendered in the pane, or None. Signals required to
    avoid matching a numbered list in ordinary output: options are contiguous, start at 1, at
    least two of them, one carries the ❯ selection cursor, and the block sits in the bottom
    half of the pane."""
    try:
        out = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", target],
            capture_output=True, text=True, timeout=3,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = out.splitlines()
    blocks: list[tuple[int, list[tuple[int, str, bool]]]] = []
    current: list[tuple[int, str, bool]] = []
    start = 0
    for index, line in enumerate(lines):
        match = _DIALOG_OPTION.match(line)
        number = int(match.group(1)) if match else None
        if match and number == len(current) + 1:
            if not current:
                start = index
            current.append((number, match.group(2), "❯" in line))
        else:
            # tolerate a wrapped option's continuation line inside the block
            if current and line.strip() and not match:
                continue
            if len(current) >= 2:
                blocks.append((start, current))
            current = []
    if len(current) >= 2:
        blocks.append((start, current))
    for start, options in reversed(blocks):
        if start < len(lines) // 2 or not any(sel for _n, _t, sel in options):
            continue
        # the question: nearest non-noise lines above the block (up to 3). A blank line normally
        # ends the question — that keeps a permission dialog's "Do you want to proceed?" from
        # absorbing the command header above it. The exception: a bare label line sitting right on
        # the options (a "Security guide" link) is skipped, and the blank gap above it crossed, to
        # reach the actual question a paragraph higher.
        question_lines: list[str] = []
        skipped_label = False
        for line in reversed(lines[:start]):
            stripped = line.strip()
            if _DIALOG_NOISE.match(line):
                if question_lines and not skipped_label:
                    break
                continue
            if not question_lines and not skipped_label and _DIALOG_BARE_LABEL.match(stripped):
                skipped_label = True
                continue
            question_lines.insert(0, stripped)
            if len(question_lines) >= 3:
                break
        question = " ".join(question_lines).strip()
        raw_texts = [text for _n, text, _s in options]
        # Hash the RAW option text (pre-cleaning) so the id is stable across renders and matches
        # what the answer endpoint recomputes; a multi-select dialog answers by toggle+navigate,
        # which single-digit relaying can't drive, so it is surfaced but not answerable.
        content = question + "|" + "|".join(raw_texts)
        multiselect = any(_DIALOG_CHECKBOX.match(text) for text in raw_texts)
        return {
            "tool": "dialog",
            "tool_use_id": "pane:" + hashlib.sha1(content.encode()).hexdigest()[:16],
            "input": "",
            "answerable": not multiselect,
            "questions": [{
                "question": question or "The session is showing a dialog:",
                "options": [{"label": _clean_option_label(text), "description": ""} for text in raw_texts],
            }],
        }
    return None


def _blocked_state(session: Session, entries: list[dict], tmux: Tmux) -> dict | None:
    """What a WAITING session is blocked on, ready to answer from the app — or None when its turn
    simply ended. The transcript's unanswered tool_use names the real tool + command to show; when
    that isn't itself an answerable AskUserQuestion, the numbered dialog the engine is rendering on
    the pane (a permission prompt, plan approval, a multi-question step) supplies the tappable
    options and the answer target. Grafting the two keeps the Bash/Edit/Fetch command visible AND
    makes its Yes/No answerable — the gap the old either/or detection left for permission prompts."""
    if session.state != State.WAITING:
        return None
    blocked = _blocked_on(entries)
    if blocked and blocked.get("answerable"):
        return blocked
    dialog = _pane_dialog(tmux, _tmux_name(session))
    if blocked is None:
        return dialog
    if dialog is not None:
        blocked["questions"] = dialog["questions"]
        blocked["tool_use_id"] = dialog["tool_use_id"]
        blocked["answerable"] = dialog["answerable"]
    return blocked


def build_inbox_feed() -> dict:
    """One item per live llm session — record basics, why it needs you (`blocked` / `ready`) or
    what it's doing (`working` / `idle`), the agent's last words as the row preview, the recent
    dialogue as the thread. Signal-ordered: newest last-event first."""
    now = time.time()
    items = []
    tmux = Tmux()
    for session in SessionStore().all():
        if session.role != Role.LLM or not session.is_alive():
            continue
        transcript = _transcript_of(session)
        entries = _tail_entries(transcript) if transcript else []
        turns = _dialogue_turns(entries, session.cmd or "")[-THREAD_TURNS:]
        blocked = _blocked_state(session, entries, tmux)
        if session.state == State.WAITING:
            reason = "blocked" if blocked else "ready"
        else:
            reason = session.state.value  # working | idle
        agent_turns = [turn for turn in turns if turn["who"] == "agent"]
        preview = agent_turns[-1]["text"].split("\n", 1)[0] if agent_turns else ""
        turn_ts = max((turn["ts"] for turn in turns), default=0.0)
        last_ts = max(turn_ts, session.last_activity or 0.0, session.created_at or 0.0)
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
            "context_pct": _context_pct(entries, session.cmd or ""),
            "error": _drift_error(session, transcript),
        })
    items.sort(key=lambda item: item["last_ts"], reverse=True)
    return {"generated_at": now, "items": items}


def inbox_signature() -> str:
    """Stat-only change detector: each live llm session's id + state + transcript size/mtime.
    Changes exactly when a session changes state or its conversation grows, so the poll loop
    rebuilds the (transcript-reading) feed only then — idle pushes nothing."""
    parts = []
    tmux = Tmux()
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
        # A dialog appears/disappears WITHOUT the transcript changing (it only flushes on
        # resolution) — its pane hash must ride the signature or the qcard never pushes.
        if session.state == State.WAITING:
            dialog = _pane_dialog(tmux, _tmux_name(session))
            part += f":{dialog['tool_use_id'] if dialog else '-'}"
        parts.append(part)
    return hashlib.sha1("|".join(sorted(parts)).encode()).hexdigest()


def answer_question(session_id: str, tool_use_id: str, option: int) -> tuple[int, dict]:
    """Answer the dialog currently on a session's pane by relaying its option digit. Guarded
    against staleness: the session must still be WAITING and the pane must still show the SAME
    dialog (content hash) the client rendered — if it resolved (timeout, terminal answer) between
    render and tap, nothing is sent. After the digit, the pane is re-checked: if the dialog is
    still up (a dialog where digits only select), Enter submits; if it's gone, no Enter — never
    a stray keystroke into a live prompt box."""
    session = next((s for s in SessionStore().all() if s.id == session_id), None)
    if session is None:
        return 404, {"ok": False, "error": "no such session"}
    if session.state != State.WAITING:
        return 200, {"ok": False, "error": "the dialog is gone — the session moved on"}
    tmux = Tmux()
    target = _tmux_name(session)
    if not tmux.has_session(target):
        return 200, {"ok": False, "error": "session is not live"}
    transcript = _transcript_of(session)
    entries = _tail_entries(transcript) if transcript else []
    blocked = _blocked_state(session, entries, tmux)
    if not blocked or blocked.get("tool_use_id") != tool_use_id:
        return 200, {"ok": False, "error": "the dialog changed — reopen the thread"}
    if not blocked.get("answerable"):
        return 200, {"ok": False, "error": "this dialog can't be answered from here — use the terminal"}
    count = len((blocked.get("questions") or [{}])[0].get("options") or [])
    if not blocked.get("questions") or not (1 <= option <= count):
        return 400, {"ok": False, "error": "not an answerable question"}
    tmux.send_keys(target, str(option), literal=True)
    time.sleep(0.6)
    still_up = _pane_dialog(tmux, target)
    if still_up is not None and still_up.get("tool_use_id") == tool_use_id:
        tmux.send_keys(target, "Enter")
    return 200, {"ok": True, "name": session.name, "option": option}


def inbox_reply(session_id: str, text: str) -> tuple[int, dict]:
    """Deliver a reply as typed input into the session's pane — exactly what attaching and typing
    would do, via the proven send-keys + 0.3s + Enter sequence (the input box drops an Enter that
    arrives too fast). Collapsed to one line (send-keys is one line)."""
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


# ---- ask the assistant about selected sessions ---------------------------------------------------
# The phone-side sibling of the terminal's prefix+/ flow: the message goes to the tx-assistant,
# prefixed with one `<tx-about session='…' chat-id='…'/>` per selected session (the durable keys —
# the assistant resolves everything else itself; agents/TX-ASSISTANT.md documents the element).
ASSISTANT_NAME = "tx-assistant"
WARM_TIMEOUT_SECONDS = 30   # bin/tx-assistant --warm may spawn + wait for claude's input box (~10s)


def _attr_escape(raw: str) -> str:
    return (raw.replace("&", "&amp;").replace("'", "&apos;")
               .replace("<", "&lt;").replace(">", "&gt;"))


def _about_envelope(session: Session) -> str:
    chat = _latest_chat(session)
    chat_attr = f" chat-id='{_attr_escape(chat.id)}'" if chat is not None else ""
    return f"<tx-about session='{_attr_escape(session.name)}'{chat_attr}/>"


def ask_assistant(session_ids: list[str], text: str) -> tuple[int, dict]:
    """Send `text` to the tx-assistant, prefixed with an about-envelope per selected session.
    `bin/tx-assistant --warm` owns existence + priming (spawn through the real tx, priming prompt,
    input-box readiness wait) — this endpoint never re-implements that; it only resolves the warm
    assistant's pane and types into it with the proven send-keys sequence."""
    text = " ".join((text or "").split())
    if not text:
        return 400, {"ok": False, "error": "empty message"}
    sessions = [s for s in SessionStore().all() if s.id in set(session_ids) and s.is_alive()]
    if not sessions:
        return 404, {"ok": False, "error": "no live selected sessions"}
    try:
        subprocess.run(
            [str(REPO_ROOT / "bin" / "tx-assistant"), "--warm"],
            capture_output=True, timeout=WARM_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 200, {"ok": False, "error": "could not warm the tx-assistant session"}
    assistant = next(
        (s for s in SessionStore().all() if s.name == ASSISTANT_NAME and s.is_alive()), None
    )
    if assistant is None:
        return 200, {"ok": False, "error": "tx-assistant session did not come up"}
    tmux = Tmux()
    target = _tmux_name(assistant)
    if not tmux.has_session(target):
        return 200, {"ok": False, "error": "tx-assistant session is not live"}
    envelopes = " ".join(_about_envelope(session) for session in sessions)
    tmux.send_keys(target, f"{envelopes} {text}", literal=True)
    time.sleep(0.3)
    tmux.send_keys(target, "Enter")
    return 200, {
        "ok": True,
        "assistant_id": assistant.id,
        "about": [session.name for session in sessions],
    }


# History paging: the live feed carries only the last THREAD_TURNS per session (it rides every
# SSE push); older turns are fetched on demand as the user scrolls up. A page is index-sliced
# from the turns before `before` (the oldest timestamp the client already shows), parsed from a
# much deeper transcript window than the feed's.
HISTORY_TAIL_BYTES = 8 * 1024 * 1024
HISTORY_PAGE_TURNS = 60


def session_history(session_id: str, before: float) -> tuple[int, dict]:
    """One page of a session's earlier dialogue: the HISTORY_PAGE_TURNS turns immediately before
    the first turn at/after `before` (0 ⇒ from the end). `more` says whether further paging can
    yield anything — false once the page hits the front of the parsed window (which covers
    HISTORY_TAIL_BYTES of transcript; anything before that is out of this prototype's reach)."""
    session = next((s for s in SessionStore().all() if s.id == session_id), None)
    if session is None:
        return 404, {"ok": False, "error": "no such session"}
    transcript = _transcript_of(session)
    entries = _tail_entries(transcript, HISTORY_TAIL_BYTES) if transcript else []
    turns = _dialogue_turns(entries, session.cmd or "")
    if before > 0:
        cut = next(
            (i for i, turn in enumerate(turns) if turn["ts"] and turn["ts"] >= before),
            len(turns),
        )
    else:
        cut = len(turns)
    start = max(0, cut - HISTORY_PAGE_TURNS)
    return 200, {"ok": True, "turns": turns[start:cut], "more": start > 0}


def _transcript_meta(entries: list[dict]) -> dict:
    """Model / context / version off the transcript tail: the LAST assistant entry carries the
    model id and a `usage` whose input+cache tokens ARE the current context footprint; `version`
    is Claude Code's. Tolerant — absent fields stay None."""
    model = version = None
    usage: dict = {}
    for entry in entries:
        if entry.get("version"):
            version = entry["version"]
        if entry.get("type") != "assistant":
            continue
        message = entry.get("message")
        if isinstance(message, dict):
            model = message.get("model") or model
            if isinstance(message.get("usage"), dict):
                usage = message["usage"]
    context = sum(
        usage.get(key) or 0
        for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    )
    return {
        "model": model,
        "version": version,
        "context_tokens": context or None,
        "last_output_tokens": usage.get("output_tokens"),
    }


def _git_branch(cwd: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "branch", "--show-current"],
            capture_output=True, text=True, timeout=2,
        )
        return result.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def session_details(session_id: str) -> tuple[int, dict]:
    """The details sheet's payload: record facts + transcript-derived model/context + the git
    branch of the cwd. `context_window` is a guess from the launch command's model spec ([1m] ⇒
    the long-context beta, else the standard 200k) — good enough for the usage bar."""
    session = next((s for s in SessionStore().all() if s.id == session_id), None)
    if session is None:
        return 404, {"ok": False, "error": "no such session"}
    now = time.time()
    transcript = _transcript_of(session)
    entries = _tail_entries(transcript) if transcript else []
    meta = _transcript_meta(entries)
    transcript_bytes = None
    if transcript is not None:
        try:
            transcript_bytes = transcript.stat().st_size
        except OSError:
            pass
    parent = None
    if session.parent:
        by_id = SessionStore().load(session.parent)
        parent = by_id.name if by_id else session.parent
    chat = _latest_chat(session)
    # Window guess: [1m] in the launch command ⇒ the long-context beta; self-corrects upward when
    # the observed context already exceeds the guess (e.g. a model on 1M without the flag).
    window = 1_000_000 if "[1m]" in (session.cmd or "") else 200_000
    if meta["context_tokens"] and meta["context_tokens"] > window:
        window = 1_000_000
    return 200, {
        "ok": True,
        "id": session.id,
        "name": session.name,
        "state": session.state.value,
        "tags": session.tags,
        "tag_colors": {tag: cube_to_hex(tag_cube(tag)) for tag in session.tags},
        "cwd": session.cwd,
        "branch": _git_branch(session.cwd),
        "cmd": session.cmd,
        "engine": session.engine.value if session.engine else None,
        "parent": parent,
        "pid": session.pid,
        "created_rel": reltime(session.created_at, now) if session.created_at else None,
        "activity_rel": reltime(session.last_activity, now) if session.last_activity else None,
        "attached": [
            f"{loc.host} · {loc.window_index}:{loc.window_name} · p{loc.pane_index}"
            for loc in (session.attached_to or [])
        ],
        "chats": len(session.chats),
        "chat_id": chat.id if chat else None,
        "transcript_bytes": transcript_bytes,
        "context_window": window,
        **meta,
    }


def save_upload(name: str, data_url: str) -> tuple[int, dict]:
    """Persist a phone-attached image and return its absolute path (for the `[img:…]` reply tag)
    plus the /uploads URL the page renders it from. A request boundary: the filename is reduced to
    a safe basename (timestamp-prefixed against collisions), the payload is size-capped, and only
    a recognized image extension is accepted."""
    match = re.match(r"^data:[^;,]*;base64,(.*)$", data_url, re.S)
    payload = match.group(1) if match else data_url
    try:
        blob = base64.b64decode(payload, validate=False)
    except Exception:
        return 400, {"ok": False, "error": "bad base64 payload"}
    if not blob:
        return 400, {"ok": False, "error": "empty image"}
    if len(blob) > UPLOAD_MAX_BYTES:
        return 400, {"ok": False, "error": f"image over {UPLOAD_MAX_BYTES // (1024 * 1024)}MB"}
    stem = re.sub(r"[^a-zA-Z0-9._-]", "_", os.path.basename(name or "image"))
    suffix = Path(stem).suffix.lower()
    if suffix not in IMAGE_TYPES:
        suffix = ".png"
        stem += ".png"
    filename = f"{int(time.time() * 1000)}-{stem}"
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    path = UPLOADS_DIR / filename
    path.write_bytes(blob)
    return 200, {"ok": True, "path": str(path), "url": f"/uploads/{filename}"}


def serve_upload_name(name: str) -> tuple[int, bytes, str]:
    """One stored upload by BASENAME only — no path traversal out of UPLOADS_DIR."""
    filename = os.path.basename(name)
    path = UPLOADS_DIR / filename
    if filename != name or not path.is_file():
        return 404, b"not found\n", "text/plain; charset=utf-8"
    return 200, path.read_bytes(), IMAGE_TYPES.get(path.suffix.lower(), "application/octet-stream")


class FeedHub:
    """One shared poll loop fanning inbox changes out to every connected SSE client — the
    sessions-graph discipline, single-channel: rebuild only when the stat-only signature moved,
    idle entirely while no client is connected."""

    def __init__(self, interval: float = POLL_INTERVAL_SECONDS) -> None:
        self._interval = interval
        self._clients: set[queue.Queue] = set()
        self._condition = threading.Condition()
        self._last_sig: str | None = None

    def start(self) -> None:
        threading.Thread(target=self._poll_loop, name="inbox-poll", daemon=True).start()

    def subscribe(self) -> queue.Queue:
        client: queue.Queue = queue.Queue()
        with self._condition:
            self._clients.add(client)
            self._condition.notify()
        return client

    def unsubscribe(self, client: queue.Queue) -> None:
        with self._condition:
            self._clients.discard(client)

    def _poll_loop(self) -> None:
        while True:
            with self._condition:
                while not self._clients:
                    self._condition.wait()
            signature = inbox_signature()
            if signature != self._last_sig:
                self._last_sig = signature
                payload = json.dumps(build_inbox_feed())
                with self._condition:
                    clients = list(self._clients)
                for client in clients:
                    client.put(payload)
            time.sleep(self._interval)


hub = FeedHub()


class RemoteHandler(BaseHTTPRequestHandler):
    """Routes: `/` (mobile or desktop page by User-Agent; `/mobile` + `/desktop` override),
    `/api/inbox` (one-shot feed), `/api/stream` (SSE: an `inbox` frame on connect, then one
    per change), `POST /api/reply` (type a reply into a session), `POST /api/ask-assistant`
    (message the tx-assistant about selected sessions).
    With `TX_REMOTE_TOKEN` set, every route requires `?token=<secret>` — the page asks once and
    remembers it; EventSource can't set headers, hence the query param."""

    def _authorized(self) -> bool:
        if not TOKEN:
            return True
        supplied = (parse_qs(urlparse(self.path).query).get("token") or [""])[0]
        return hmac.compare_digest(supplied, TOKEN)

    def _respond(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        client = hub.subscribe()
        try:
            self._sse_send(json.dumps(build_inbox_feed()))
            while True:
                try:
                    self._sse_send(client.get(timeout=15))
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            hub.unsubscribe(client)

    def _sse_send(self, payload: str) -> None:
        self.wfile.write(b"event: inbox\ndata: " + payload.encode() + b"\n\n")
        self.wfile.flush()

    def _page_for_client(self) -> Path:
        # The installed PWA (and any phone browser) sends "Mobi" in its User-Agent; desktop
        # browsers don't. Guessed wrong? /mobile and /desktop serve either face explicitly.
        return MOBILE_PAGE if "Mobi" in self.headers.get("User-Agent", "") else DESKTOP_PAGE

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        # The pages + the PWA shell files are served without the token so the phone can install
        # the app and ASK for one; every data/action route is gated.
        if path in ("/", "/index.html"):
            self._respond(200, self._page_for_client().read_bytes(), "text/html; charset=utf-8")
            return
        if path in ("/mobile", "/mobile.html"):
            self._respond(200, MOBILE_PAGE.read_bytes(), "text/html; charset=utf-8")
            return
        if path in ("/desktop", "/desktop.html"):
            self._respond(200, DESKTOP_PAGE.read_bytes(), "text/html; charset=utf-8")
            return
        if path == "/shared.js":
            self._respond(200, (HERE / "shared.js").read_bytes(), "text/javascript; charset=utf-8")
            return
        if path == "/manifest.json":
            self._respond(200, (HERE / "manifest.json").read_bytes(), "application/manifest+json")
            return
        if path == "/icon.png":
            self._respond(200, (HERE / "icon.png").read_bytes(), "image/png")
            return
        if not self._authorized():
            self._respond(401, b'{"ok": false, "error": "bad or missing token"}', "application/json")
            return
        if path == "/api/stream":
            self._serve_stream()
        elif path == "/api/inbox":
            self._respond(200, json.dumps(build_inbox_feed()).encode(), "application/json")
        elif path == "/api/session":
            query = parse_qs(urlparse(self.path).query)
            status, payload = session_details((query.get("id") or [""])[0])
            self._respond(status, json.dumps(payload).encode(), "application/json")
        elif path == "/api/history":
            query = parse_qs(urlparse(self.path).query)
            try:
                before = float((query.get("before") or ["0"])[0])
            except ValueError:
                before = 0.0
            status, payload = session_history((query.get("id") or [""])[0], before)
            self._respond(status, json.dumps(payload).encode(), "application/json")
        elif path.startswith("/uploads/"):
            status, body, content_type = serve_upload_name(path[len("/uploads/"):])
            self._respond(status, body, content_type)
        else:
            self._respond(404, b"not found\n", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        if not self._authorized():
            self._respond(401, b'{"ok": false, "error": "bad or missing token"}', "application/json")
            return
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            body = {}
        if path == "/api/reply":
            status, payload = inbox_reply(str(body.get("id", "")), str(body.get("text", "")))
        elif path == "/api/ask-assistant":
            ids = body.get("ids")
            ids = [str(i) for i in ids] if isinstance(ids, list) else []
            status, payload = ask_assistant(ids, str(body.get("text", "")))
        elif path == "/api/answer":
            try:
                option = int(body.get("option", 0))
            except (TypeError, ValueError):
                option = 0
            status, payload = answer_question(
                str(body.get("id", "")), str(body.get("tool_use_id", "")), option
            )
        elif path == "/api/upload":
            status, payload = save_upload(str(body.get("name", "")), str(body.get("data", "")))
        else:
            self._respond(404, b"not found\n", "text/plain; charset=utf-8")
            return
        self._respond(status, json.dumps(payload).encode(), "application/json")

    def log_message(self, message_format: str, *args) -> None:
        sys.stderr.write(f"  {self.command} {urlparse(self.path).path}\n")


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    # Loopback by default; a wider HOST is an explicit opt-in (this service types into tmux).
    host = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
    if host != "127.0.0.1" and not TOKEN:
        print("WARNING: non-loopback bind with no TX_REMOTE_TOKEN — anyone who can reach this")
        print("         address can read AND message your sessions. Set TX_REMOTE_TOKEN.")
    server = ThreadingHTTPServer((host, port), RemoteHandler)
    hub.start()
    print(f"tx remote-control  →  http://{host}:{port}/")
    print(f"reading records under {os.environ.get('TX_IDE_HOME', '~/.tx-ide')}")
    print("Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye.")
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
