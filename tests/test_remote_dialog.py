#!/usr/bin/env python3.14
"""Remote-control pane-dialog parsing + blocked-state merge (prototypes/remote-control/server.py).

No pytest in this repo, so this is directly runnable:  python3.14 tests/test_remote_dialog.py
Exits non-zero on the first failure; prints "OK — N checks passed" (same convention as the other
gate tests).

This is the behavioral gate for making a session blocked on a terminal permission dialog
answerable from the phone. It pins two things against REAL captured panes (tests/fixtures/
remote-dialogs/*.txt — one `tmux capture-pane` snapshot per Claude Code dialog kind, taken from
the installed engine so a future engine restyle that breaks parsing shows up here):

  1. `_pane_dialog` reads each dialog's question + options off the pane, cleans option labels
     (trailing key hints and multi-select checkboxes stripped), and marks a multi-select dialog
     NOT answerable (single-digit relaying can't drive its toggle+submit).
  2. `_blocked_state` merges the transcript's blocked tool_use with the pane dialog — so a Bash
     permission prompt keeps its command visible AND becomes answerable, the gap the old
     either/or detection left. A stale answer target (pane hash moved) is rejected.

Hermetic: the pane captures are fixtures on disk; `subprocess.run` is monkeypatched so no live
tmux is touched. No `$TX_IDE_HOME`, no spawn, no network.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "lib"))
sys.path.insert(0, str(REPO_ROOT / "prototypes" / "remote-control"))

import server  # noqa: E402
from tx.session import State  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "remote-dialogs"

checks = 0


def check(condition: bool, message: str) -> None:
    global checks
    if not condition:
        print(f"FAIL: {message}")
        sys.exit(1)
    checks += 1


def with_pane(text: str) -> None:
    """Point server.subprocess.run at fixture text so `_pane_dialog` reads it instead of tmux."""
    server.subprocess = types.SimpleNamespace(
        run=lambda *a, _pane=text, **k: types.SimpleNamespace(stdout=_pane)
    )


def pane_dialog(name: str) -> dict | None:
    with_pane((FIXTURES / f"{name}.txt").read_text())
    return server._pane_dialog(None, "fake-target")


# ---- 1. per-dialog pane parsing ------------------------------------------------------------------
# question is matched as a substring (robust to the tail-only capture of a long prompt); options are
# matched exactly (the cleaning is the point).
EXPECTED = {
    "bash": {
        "answerable": True,
        "question": "Do you want to proceed?",
        "options": ["Yes", "Yes, and always allow access to bash/ from this project", "No"],
    },
    "write": {
        "answerable": True,
        "question": "Do you want to create notes.txt?",
        "options": ["Yes", "Yes, allow all edits during this session", "No"],  # (shift+tab) stripped
    },
    "edit": {
        "answerable": True,
        "question": "Do you want to make this edit to config.py?",
        "options": ["Yes", "Yes, allow all edits during this session", "No"],
    },
    "webfetch": {
        "answerable": True,
        "question": "Do you want to allow Claude to fetch this content?",
        # (esc) stripped from the last option
        "options": ["Yes", "Yes, and don't ask again for example.com",
                    "No, and tell Claude what to do differently"],
    },
    "plan": {
        "answerable": True,
        "question": "Would you like to proceed?",
        "options": ["Yes, and use auto mode", "Yes, manually approve edits",
                    "No, refine with Ultraplan on Claude Code on the web", "Tell Claude what to change"],
    },
    "ask1": {
        "answerable": True,
        "question": "Do you prefer red or blue?",
        "options": ["Red", "Blue", "Type something.", "Chat about this"],
    },
    "ask2q": {
        "answerable": True,
        "question": "What is your favorite color?",   # one active question of a multi-question dialog
        "options": ["Red", "Blue", "Type something.", "Chat about this"],
    },
    "askmulti": {
        "answerable": True,                           # multi-select: answerable via toggle + submit
        "question": "Which of these would you like?",
        "options": ["Pizza", "Pasta", "Salad", "Type something", "Chat about this"],  # [ ] stripped
    },
}

for name, want in EXPECTED.items():
    dialog = pane_dialog(name)
    check(dialog is not None, f"{name}: pane dialog parsed")
    q = dialog["questions"][0]
    check(want["question"] in q["question"], f"{name}: question ({q['question']!r})")
    labels = [o["label"] for o in q["options"]]
    check(labels == want["options"], f"{name}: options ({labels!r})")
    check(dialog["answerable"] is want["answerable"], f"{name}: answerable == {want['answerable']}")
    check(dialog["tool_use_id"].startswith("pane:"), f"{name}: pane-hash tool_use_id")

# The trust dialog is a special case: the real question sits a blank-gap above a bare "Security
# guide" link. Assert the link is NOT the question (the bare-label skip fired) — best-effort, so
# only the negative is pinned.
trust = pane_dialog("trust")
check(trust is not None and trust["questions"][0]["question"] != "Security guide",
      "trust: bare 'Security guide' label skipped, real question reached")
check([o["label"] for o in trust["questions"][0]["options"]] == ["Yes, I trust this folder", "No, exit"],
      "trust: options parsed")

# Multi-select structure: the flag is set, checkbox options are marked, and (a fresh dialog) all
# start unchecked; a single-select dialog carries no checkbox options.
multi = pane_dialog("askmulti")
check(multi["multiselect"] is True, "askmulti: multiselect flag set")
check(all(o["checkbox"] for o in multi["questions"][0]["options"][:4]), "askmulti: content options are checkboxes")
check(not any(o["checked"] for o in multi["questions"][0]["options"]), "askmulti: fresh dialog starts unchecked")
single = pane_dialog("bash")
check(single["multiselect"] is False, "bash: not multiselect")
check(not any(o["checkbox"] for o in single["questions"][0]["options"]), "bash: no checkbox options")


# ---- 2. _blocked_state merge (transcript tool_use × pane dialog) ---------------------------------
class FakeSession:
    def __init__(self, state):
        self.state = state
        self.kind = server.Kind.VIEW      # _tmux_name → session.name (not the PROCESS id form)
        self.name = "puppet"
        self.id = "puppet-id"


def assistant_use(tool: str, use_id: str, tool_input: dict) -> dict:
    return {"type": "assistant",
            "message": {"content": [{"type": "tool_use", "id": use_id, "name": tool, "input": tool_input}]}}


def tool_result(use_id: str) -> dict:
    return {"type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": use_id, "content": "ok"}]}}


# A Bash permission prompt: the tool_use IS flushed to the transcript (unanswered), and the pane
# shows the numbered dialog. The merge keeps the Bash command AND grafts the pane's answerable
# options + pane-hash target.
with_pane((FIXTURES / "bash.txt").read_text())
entries = [assistant_use("Bash", "toolu_1", {"command": "touch marker-file.txt"})]
merged = server._blocked_state(FakeSession(State.WAITING), entries, None)
check(merged is not None and merged["tool"] == "Bash", "merge: keeps the real tool name (Bash)")
check("touch marker-file.txt" in merged["input"], "merge: keeps the command as input")
check(merged["answerable"] is True, "merge: Bash prompt is answerable via the pane")
check(merged["tool_use_id"].startswith("pane:"), "merge: answer target is the pane hash")
check([o["label"] for o in merged["questions"][0]["options"]] == ["Yes", "Yes, and always allow access to bash/ from this project", "No"],
      "merge: options come from the pane")

# An answered tool_use with no pane dialog → not blocked (turn simply ended).
with_pane("just some ordinary output\nnothing to answer\n")
entries = [assistant_use("Bash", "toolu_2", {"command": "ls"}), tool_result("toolu_2")]
check(server._blocked_state(FakeSession(State.WAITING), entries, None) is None,
      "merge: answered tool_use with no pane dialog is not blocked")

# A single-select AskUserQuestion answers from its transcript form directly — the pane is never
# consulted (here the pane is blank, proving the transcript path stands alone).
with_pane("")
entries = [assistant_use("AskUserQuestion", "toolu_3",
                         {"questions": [{"question": "Ship it?",
                                         "options": [{"label": "Yes"}, {"label": "No"}]}]})]
ask = server._blocked_state(FakeSession(State.WAITING), entries, None)
check(ask is not None and ask["answerable"] is True and ask["tool_use_id"] == "toolu_3",
      "merge: single-select AskUserQuestion answers from the transcript, no pane needed")

# A non-WAITING session is never blocked, whatever the pane shows.
with_pane((FIXTURES / "bash.txt").read_text())
check(server._blocked_state(FakeSession(State.WORKING), [], None) is None,
      "merge: a non-WAITING session is not blocked")


# ---- 3. _answer_multiselect toggle + submit sequence --------------------------------------------
# Toggling is a flip, so only options whose current state differs from what's wanted get a digit;
# then Right opens the review screen and its Submit option is pressed. The review screen is
# re-parsed (not assumed), so this fake pane must render a real "Submit answers / Cancel" dialog.
REVIEW_PANE = """
──────────────────────────────────────────────────────────────────
 Review your answers
 ● Which of these would you like?
   → Pizza, Salad
 Ready to submit your answers?
 ❯ 1. Submit answers
   2. Cancel
──────────────────────────────────────────────────────────────────
"""


class FakeTmux:
    def __init__(self):
        self.keys = []

    def has_session(self, target):
        return True

    def send_keys(self, target, keys, literal=False):
        self.keys.append(keys)


server.time = types.SimpleNamespace(sleep=lambda *a, **k: None, time=lambda: 0.0)  # no real waits

q_options = pane_dialog("askmulti")["questions"][0]["options"]   # Pizza Pasta Salad [Type…] [Chat…]
with_pane(REVIEW_PANE)                                           # what _pane_dialog reads after Right
fake = FakeTmux()
code, resp = server._answer_multiselect(fake, "puppet", "pane:x", q_options, [1, 3], "puppet")
check(resp.get("ok") is True, "multiselect: submits successfully")
# want {1,3}: Pizza(1) and Salad(3) toggled on; Pasta(2)/Type-something(4) left; then Right, Submit(1), Enter
check(fake.keys == ["1", "3", "Right", "1", "Enter"], f"multiselect: key sequence ({fake.keys})")

# Nothing selected → refused, no keys sent.
fake2 = FakeTmux()
code, resp = server._answer_multiselect(fake2, "puppet", "pane:x", q_options, [], "puppet")
check(resp.get("ok") is not True and fake2.keys == [], "multiselect: empty selection refused, no keys sent")

print(f"OK — {checks} checks passed")
