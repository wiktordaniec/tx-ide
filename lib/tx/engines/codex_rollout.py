"""`codex_rollout` — a standalone parser for an OpenAI **Codex** rollout JSONL.

Codex records each interactive session as a newline-delimited JSON *rollout* file under
`~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<ts>-<session-id>.jsonl` (design §3; the on-disk shape
captured by the T2 spike — `docs/engine/verification.md` §"Rollout path + transcript shape"). This
module turns that file into the two primitives the Codex adapter (T6) will build
`CodexEngine.iter_messages` / `bundle` on, **without re-deriving the format**:

  - `read_session_meta(path)` — the first-line `session_meta` payload (the session `id`, the `cwd`,
    and, on a fork, the `forked_from_id` recording its parent — verification.md "Evidence 2").
  - `iter_messages(path)`     — the conversation in order, one `CodexMessage` per OpenAI *Responses*
    `message` item, with Codex's injected `<environment_context>` first-user turn flagged.

It is deliberately **standalone**: stdlib `json` only, no `tx` imports, no I/O beyond reading the
given path — so it can be authored and tested in isolation (task T7) and imported unchanged by the
adapter later (T6) without coupling to the engines-package wiring T4 owns.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

# Top-level record `type` of the rollout's first line — the outer envelope is `{type,timestamp,
# payload}` (design §3); the first record is always the session metadata.
SESSION_META_TYPE = "session_meta"

# `payload.type` of a conversation turn (an OpenAI Responses item). Non-message payloads
# (`reasoning`, tool calls, `token_count` events, …) carry other types and are skipped.
MESSAGE_TYPE = "message"

# The roles a real conversation turn carries (design §3). Anything else (e.g. `system`, `tool`) is
# not a turn this parser surfaces.
MESSAGE_ROLES = frozenset({"developer", "user", "assistant"})

# The content-block types that hold a turn's text: user/developer turns use `input_text`, assistant
# turns `output_text` (OpenAI Responses items). Other blocks (images, …) carry no text.
TEXT_BLOCK_TYPES = frozenset({"input_text", "output_text"})

# Codex injects the session's first `user` item as an `<environment_context>` block (the cwd /
# sandbox / approval policy), not a typed turn (verification.md). Its text opens with this tag.
ENVIRONMENT_CONTEXT_TAG = "<environment_context>"


@dataclass(frozen=True)
class CodexMessage:
    """One conversation turn parsed from a rollout: its `role` (developer/user/assistant) and the
    `text` of its concatenated `input_text`/`output_text` blocks. `is_environment_context` marks the
    injected first-user `<environment_context>` turn so a caller can drop it from the real
    conversation (`m for m in iter_messages(path) if not m.is_environment_context`)."""

    role: str
    text: str
    is_environment_context: bool = False


def read_session_meta(path: Path | str) -> dict:
    """Return the payload of the rollout's first-line `session_meta` record.

    The payload carries the session `id`, the `cwd`, the originator / `cli_version`, and — on a
    session created by `codex fork` — the `forked_from_id` naming the parent it branched from (the
    lineage tx reads for the fork/handover/rollover provenance DAG; verification.md "Evidence 2"). A
    fresh or resumed session has no `forked_from_id`. The first record of every Codex rollout is the
    `session_meta`; a file that does not start with one is malformed (the file is external Codex
    output — the one boundary worth validating)."""
    for record in _iter_records(path):
        if record["type"] != SESSION_META_TYPE:
            raise ValueError(
                f"{path}: first record is {record['type']!r}, expected {SESSION_META_TYPE!r}"
            )
        return record["payload"]
    raise ValueError(f"{path}: empty rollout — no {SESSION_META_TYPE} record")


def iter_messages(path: Path | str) -> Iterator[CodexMessage]:
    """Yield the rollout's conversation turns in order, one `CodexMessage` per Responses `message`
    item: records whose `payload.type == "message"` and whose `role` is developer/user/assistant,
    with `text` the concatenation of their `input_text`/`output_text` blocks (design §3). Every other
    record — the `session_meta`, `reasoning` / tool-call items, `event_msg` token counts — is skipped.

    The first `user` item is Codex's injected `<environment_context>` block, not a typed turn
    (verification.md); it is yielded with `is_environment_context=True` rather than dropped, so the
    caller decides whether to keep it and never has to re-detect it."""
    for record in _iter_records(path):
        payload = record["payload"]
        if payload.get("type") != MESSAGE_TYPE:
            continue
        role = payload["role"]
        if role not in MESSAGE_ROLES:
            continue
        text = _message_text(payload["content"])
        is_environment_context = role == "user" and text.lstrip().startswith(ENVIRONMENT_CONTEXT_TAG)
        yield CodexMessage(role=role, text=text, is_environment_context=is_environment_context)


def _iter_records(path: Path | str) -> Iterator[dict]:
    """Yield each rollout record as a decoded JSON object, one per non-empty line."""
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def _message_text(content: list) -> str:
    """Concatenate the text of a message's `input_text`/`output_text` blocks, in order. Non-text
    blocks carry no `text` and are skipped; a multi-block message rejoins to its full text."""
    return "".join(block["text"] for block in content if block["type"] in TEXT_BLOCK_TYPES)
