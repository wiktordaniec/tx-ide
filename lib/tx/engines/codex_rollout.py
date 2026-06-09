from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

# Rollout records are `{type, timestamp, payload}`; the first line is always the session_meta.
SESSION_META_TYPE = "session_meta"
# A conversation turn (an OpenAI Responses item); other payload types (reasoning, token_count…) are skipped.
MESSAGE_TYPE = "message"
MESSAGE_ROLES = frozenset({"developer", "user", "assistant"})
# user/developer turns hold text in input_text, assistant turns in output_text.
TEXT_BLOCK_TYPES = frozenset({"input_text", "output_text"})
# Codex injects the first user turn as an <environment_context> block (cwd / sandbox / policy).
ENVIRONMENT_CONTEXT_TAG = "<environment_context>"


@dataclass(frozen=True)
class CodexMessage:
    role: str
    text: str
    is_environment_context: bool = False


def read_session_meta(path: Path | str) -> dict:
    """The first-line session_meta payload (session id, cwd, and on a fork the forked_from_id). A
    rollout that doesn't start with one is malformed — the one external boundary worth validating."""
    for record in _iter_records(path):
        if record["type"] != SESSION_META_TYPE:
            raise ValueError(
                f"{path}: first record is {record['type']!r}, expected {SESSION_META_TYPE!r}"
            )
        return record["payload"]
    raise ValueError(f"{path}: empty rollout — no {SESSION_META_TYPE} record")


def iter_messages(path: Path | str) -> Iterator[CodexMessage]:
    """Yield conversation turns in order. The first user turn is Codex's injected
    <environment_context>, flagged (not dropped) so the caller decides whether to keep it."""
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
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def _message_text(content: list) -> str:
    return "".join(block["text"] for block in content if block["type"] in TEXT_BLOCK_TYPES)
