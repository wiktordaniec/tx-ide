#!/usr/bin/env python3.14
"""Standalone test for the peer-message envelope — BUILD neutral, PARSE both (engine-abstraction §4.8).

No pytest in this repo, so this is directly runnable:  python3.14 tests/test_envelope.py
Exits non-zero on the first failure.

T5's one behavioral change: `tx send-message` now BUILDS the engine-neutral `<from-agent>` envelope,
while the parser keeps accepting the legacy `<from-claude>` INDEFINITELY — a live Claude session
mid-rollover still emits the old tag, so back-compat is mandatory (nothing is dropped). This asserts:
  (1) the builder emits `<from-agent>` and never the legacy tag;
  (2) the parser classifies the OLD and the NEW envelope IDENTICALLY — kind / via / sender / body —
      across every sender-role case, including multi-line and embedded-close-tag bodies;
  (3) build → parse round-trips.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from tx.messages import KIND_AGENT, KIND_YOU, build_envelope, parse_message  # noqa: E402
from tx.session import Role  # noqa: E402

# wr-core is an llm peer (the agent↔agent channel); Views is a non-llm home (a `tx send-message` you
# ran yourself → kind=you); an unknown sender stays on the agent channel.
ROLES = {"wr-core": Role.LLM, "Views": Role.OTHER}


def role_of(name):
    return ROLES.get(name)


def turn(content):
    """A minimal external user-turn transcript line carrying `content` as its typed text."""
    return {
        "type": "user",
        "message": {"role": "user", "content": content},
        "userType": "external",
        "timestamp": "2026-06-02T06:24:08.020Z",
        "uuid": "u-default",
    }


def parse(text):
    return parse_message(
        turn(text), recipient_id="rid", recipient_name="fix-zoom-ux", recipient_cmd="",
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


# ----- (1) the builder emits the neutral <from-agent> --------------------------------------------

built = build_envelope("tx-assistant", "rebase onto main then push")
check("builder emits the <from-agent> open tag", built.startswith('<from-agent session="tx-assistant">'))
check("builder emits the </from-agent> close tag", built.endswith("</from-agent>"))
check("builder carries the body verbatim", "rebase onto main then push" in built)
check("builder never emits the legacy from-claude tag", "from-claude" not in built)


# ----- (2) the parser classifies OLD and NEW envelopes IDENTICALLY -------------------------------
# For each (sender, expected-kind) case the legacy `<from-claude …>` and the neutral `<from-agent …>`
# turn must produce the SAME kind / via / sender / body — old and new are interchangeable on the wire.

def both(sender, body):
    old = parse(f'<from-claude session="{sender}">{body}</from-claude>')
    new = parse(f'<from-agent session="{sender}">{body}</from-agent>')
    return old, new


def assert_identical(case, sender, body, expected_kind):
    old, new = both(sender, body)
    check(f"{case}: old (from-claude) classified", old is not None)
    check(f"{case}: new (from-agent) classified", new is not None)
    check(f"{case}: kind identical ({expected_kind})", old.kind == new.kind == expected_kind)
    check(f"{case}: via identical (send-message)", old.via == new.via == "send-message")
    check(f"{case}: sender identical", old.sender == new.sender == sender)
    check(f"{case}: body identical", old.body == new.body == body)


assert_identical("llm peer → agent channel", "wr-core", "rebase onto main then push", KIND_AGENT)
assert_identical("non-llm home → you", "Views", "ping from my home", KIND_YOU)
assert_identical("unknown sender → agent channel", "ghost-rm-d", "still the agent channel", KIND_AGENT)

# Multi-line bodies (DOTALL) survive identically across both tags.
old, new = both("wr-core", "line one\nline two")
check("multi-line: old body preserved", old.body == "line one\nline two")
check("multi-line: new body preserved", new.body == "line one\nline two")
check("multi-line: identical across tags", old.body == new.body)

# A body that itself mentions a close tag still closes at the LAST one (greedy) — for both tags, even
# when the body embeds the OTHER engine's close tag.
embedded = "see </from-claude> and </from-agent> inline"
old, new = both("wr-core", embedded)
check("embedded close tags: old body intact", old.body == embedded)
check("embedded close tags: new body intact", new.body == embedded)


# ----- (3) build → parse round-trip --------------------------------------------------------------
# What the builder emits, the parser reads back: same sender, same body, on the agent channel.

roundtrip = parse(build_envelope("wr-core", "deploy is green"))
check("round-trip: classified", roundtrip is not None)
check("round-trip: kind=agent", roundtrip.kind == KIND_AGENT)
check("round-trip: via=send-message", roundtrip.via == "send-message")
check("round-trip: sender preserved", roundtrip.sender == "wr-core")
check("round-trip: body preserved", roundtrip.body == "deploy is green")


print(f"OK — {PASSED} checks passed")
