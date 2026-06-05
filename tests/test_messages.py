#!/usr/bin/env python3.14
"""Standalone test for `tx.messages.parse_message` — the deterministic per-turn classifier.

No pytest in this repo, so this is directly runnable:  python3.14 tests/test_messages.py
Exits non-zero on the first failure. Covers each message class plus the noise that shares the same
marker strings (tool results that read a file documenting the envelope, assistant quotes, meta /
sidechain / non-external turns, spawn priming, harness notifications) — the cases that make a naïve
grep over-count and that the structural gate must reject.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from tx.messages import KIND_AGENT, KIND_YOU, parse_message  # noqa: E402
from tx.session import Role  # noqa: E402

ROLES = {"wr-core": Role.LLM, "tx-assistant": Role.LLM, "Views": Role.OTHER}


def role_of(name):
    return ROLES.get(name)


def turn(content, **overrides):
    """A user-turn transcript line with sane defaults; override any field per case."""
    base = {
        "type": "user",
        "message": {"role": "user", "content": content},
        "userType": "external",
        "timestamp": "2026-06-02T06:24:08.020Z",
        "uuid": "u-default",
    }
    base.update(overrides)
    return base


def parse(obj, *, cmd=""):
    return parse_message(
        obj, recipient_id="rid", recipient_name="fix-zoom-ux", recipient_cmd=cmd,
        chat_id="chat-1", sender_role=role_of,
    )


PASSED = 0


def check(label, condition):
    global PASSED
    if condition:
        PASSED += 1
        return
    print(f"FAIL: {label}")
    sys.exit(1)


# ----- the four message classes ------------------------------------------------------------------

m = parse(turn('<from-claude session="wr-core">rebase onto main then push</from-claude>'))
check("peer: classified", m is not None)
check("peer: kind=agent (llm sender)", m.kind == KIND_AGENT)
check("peer: via", m.via == "send-message")
check("peer: sender is the envelope attr", m.sender == "wr-core")
check("peer: recipient is the transcript owner", m.recipient == "fix-zoom-ux")
check("peer: body is the inner text", m.body == "rebase onto main then push")
check("peer: timestamp parsed to epoch", m.ts > 1_700_000_000)

m = parse(turn('<from-claude session="Views">ping from my home</from-claude>'))
check("peer from non-llm home → kind=you", m.kind == KIND_YOU and m.sender == "Views")

m = parse(turn('<from-claude session="ghost-rm-d">still the agent channel</from-claude>'))
check("peer from unknown sender stays agent", m.kind == KIND_AGENT)

m = parse(turn("<tx-command-prompt session-name='Views' pane-id='%46'/> spawn me a dev session"))
check("prompt: via", m is not None and m.via == "prompt")
check("prompt: kind=you", m.kind == KIND_YOU and m.sender == "you")
check("prompt: body is the text after the envelope", m.body == "spawn me a dev session")

m = parse(turn('Act on tx session id=abc name="x". User request: focus it'))
check("composer: classified as you/composer", m is not None and m.via == "composer" and m.kind == KIND_YOU)

m = parse(turn("This looks good, we want to have it on the main."))
check("typed human: via=typed", m is not None and m.via == "typed")
check("typed human: kind=you", m.kind == KIND_YOU and m.sender == "you")
check("typed human: body verbatim", m.body == "This looks good, we want to have it on the main.")

# ----- the noise that must be excluded ------------------------------------------------------------

file_read = turn([{"type": "tool_result", "tool_use_id": "t1",
                   "content": 'COMMON.md says use <from-claude session="X">...</from-claude>'}])
check("tool_result reading the envelope doc → excluded", parse(file_read) is None)

mixed = turn([{"type": "text", "text": "hi"}, {"type": "tool_result", "content": "x"}])
check("mixed text+tool_result block → excluded", parse(mixed) is None)

check("assistant turn → excluded", parse(turn("hello", type="assistant")) is None)
check("isMeta → excluded", parse(turn("<command-name>/clear</command-name>", isMeta=True)) is None)
check("isSidechain (sub-agent) → excluded", parse(turn("do the subtask", isSidechain=True)) is None)
check("non-external userType → excluded", parse(turn("synthetic", userType="internal")) is None)
check("task-notification → excluded", parse(turn("<task-notification>done</task-notification>")) is None)
check("slash-command scaffolding → excluded", parse(turn("<local-command-stdout>x</local-command-stdout>")) is None)
check("interrupt marker → excluded", parse(turn("[Request interrupted by user]")) is None)
check("role-file priming → excluded", parse(turn("Read ~/.tx-ide/agents/COMMON.md as your first actions")) is None)

brief = "Read .claude/notes/brief-wr-core.md and execute it. You are sub-worker wr-core."
launch_cmd = f'claude --dangerously-skip-permissions "{brief}"'
check("launch prompt embedded in cmd → excluded", parse(turn(brief), cmd=launch_cmd) is None)

# the same brief text in a DIFFERENT session (not its launch cmd) is a real message
check("same text, not the launch cmd → kept", parse(turn(brief), cmd="claude --model opus") is not None)

# ----- edges -------------------------------------------------------------------------------------

check("empty body after marker → excluded", parse(turn("<tx-command-prompt pane-id='%1'/>   ")) is None)
check("missing uuid falls back to a stable key",
      parse(turn("hi there", uuid=None)).uuid == "chat-1:2026-06-02T06:24:08.020Z:8")

multi = parse(turn('<from-claude session="wr-core">line one\nline two</from-claude>'))
check("multi-line peer body preserved", multi.body == "line one\nline two")

print(f"OK — {PASSED} checks passed")
