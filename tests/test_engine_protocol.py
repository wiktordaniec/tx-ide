#!/usr/bin/env python3.14
"""Standalone protocol-conformance test for every registered `EngineAdapter` (task T1, Q-T1b).

No pytest in this repo, so this is directly runnable:  python3.14 tests/test_engine_protocol.py
Exits non-zero on the first failure (same convention as tests/test_messages.py). From T1 on this is a
global merge gate: every engine the registry holds is run through the one contract (design §2), so a
new adapter cannot land half-implementing the surface.

Three layers:
  - **structural, every registered engine** — `runtime_checkable` `isinstance` against
    `EngineAdapter`, then each required method/property present + callable with the right arity;
  - **pure-builder smoke (Claude + Codex)** — the launch/ops builders return argv with the binary
    first, on synthetic args, plus the capability flag + the event→state table;
  - **dual-layout bundle (Claude + Codex, T6 §4)** — the one disk-backed layer: each engine's history
    bundle LAYOUT, run through history.py's shared copy, asserting Claude mirrors transcript + sidecar
    dirs and Codex mirrors the rollout JSONL alone (design §6.4).

The remaining I/O methods (`capture_session_id` / `resolve_transcript` / `iter_messages`) are checked
present + callable ONLY — their behavior is tested per-engine (`tests/test_codex_engine.py`,
`tests/test_capture.py`). The dual-layout layer is disk-backed but **hermetic** — every home is a temp
dir, so no live ~/.claude / ~/.codex / $TX_IDE_HOME is touched.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from tx.engines import EngineAdapter, StateSource, get, registered  # noqa: E402
from tx.engines import claude as claude_engine, codex as codex_engine  # noqa: E402,F401
from tx.session import Engine, State  # noqa: E402

PASSED = 0


def check(label, condition):
    global PASSED
    if condition:
        PASSED += 1
        return
    print(f"FAIL: {label}")
    sys.exit(1)


def positional_arity(method) -> int:
    """How many positional parameters a bound method accepts (`self` already excluded). The launch
    builders are keyword-only, so their positional arity is 0."""
    kinds = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    return sum(1 for p in inspect.signature(method).parameters.values() if p.kind in kinds)


# The required surface (design §2): method name → expected positional arity (self excluded). The
# launch builders take their `(model, effort, …)` keyword-only, so 0 positional. Properties are
# checked separately (accessed, not called).
REQUIRED_METHODS = {
    "matches_binary": 1,
    "capture_session_id": 1,
    "build_launch_command": 0,
    "resume_command": 1,
    "fork_command": 1,
    "seed_command": 1,
    "distiller_command": 0,
    "resolve_transcript": 2,
    "iter_messages": 1,
    "bundle": 1,
}
REQUIRED_PROPERTIES = ("binary", "event_to_state", "state_source")


# ----- structural: every registered engine conforms ---------------------------------------------

engines = registered()
check("registry is populated (Claude registered at import)", len(engines) >= 1)
check("Engine.CLAUDE is registered", Engine.CLAUDE in engines)

for engine_key in engines:
    adapter = get(engine_key)
    name = engine_key.value
    check(f"{name}: isinstance EngineAdapter (runtime_checkable)", isinstance(adapter, EngineAdapter))
    for method_name, expected_arity in REQUIRED_METHODS.items():
        method = getattr(adapter, method_name, None)
        check(f"{name}: {method_name} present + callable", callable(method))
        check(f"{name}: {method_name} positional arity == {expected_arity}",
              positional_arity(method) == expected_arity)
    for prop in REQUIRED_PROPERTIES:
        check(f"{name}: property {prop} present", hasattr(adapter, prop))


# ----- pure-builder smoke (Claude) — no I/O -----------------------------------------------------

claude = get(Engine.CLAUDE)

check("binary == 'claude'", claude.binary == "claude")
check("matches_binary('claude') is True", claude.matches_binary("claude") is True)
check("matches_binary('/usr/local/bin/claude --x') is True",
      claude.matches_binary("/usr/local/bin/claude --x") is True)
check("matches_binary('nvim --diff') is False", claude.matches_binary("nvim --diff") is False)
check("matches_binary('') is False", claude.matches_binary("") is False)

fresh = claude.build_launch_command(model="opus", effort="high", initial_prompt="do the thing")
check("build_launch_command: binary first", fresh[0] == "claude")
check("build_launch_command: renders --model/--effort",
      fresh[:5] == ["claude", "--model", "opus", "--effort", "high"])
check("build_launch_command: bakes the yolo flag", "--dangerously-skip-permissions" in fresh)
check("build_launch_command: prompt is the positional tail", fresh[-1] == "do the thing")
check("build_launch_command: bare call is just the binary + yolo",
      claude.build_launch_command() == ["claude", "--dangerously-skip-permissions"])

resume = claude.resume_command("CID-1")
check("resume_command: binary first", resume[0] == "claude")
check("resume_command: --resume <id>", resume[1:3] == ["--resume", "CID-1"])

fork = claude.fork_command("CID-2")
check("fork_command: binary first", fork[0] == "claude")
check("fork_command: resumes + forks the source",
      "--resume" in fork and "--fork-session" in fork and "CID-2" in fork)

seed = claude.seed_command("read the brief")
check("seed_command: binary first", seed[0] == "claude")
check("seed_command: seed is the positional tail", seed[-1] == "read the brief")

distill = claude.distiller_command()
check("distiller_command: binary first", distill[0] == "claude")
check("distiller_command: opus at medium effort", distill[1:5] == ["--model", "opus", "--effort", "medium"])

# ----- capability flag + event→state table ------------------------------------------------------

check("state_source is HOOK_EVENTS (Claude emits a Stop hook)",
      claude.state_source is StateSource.HOOK_EVENTS)
check("event_to_state: UserPromptSubmit → WORKING", claude.event_to_state["UserPromptSubmit"] == State.WORKING)
check("event_to_state: Stop → WAITING", claude.event_to_state["Stop"] == State.WAITING)
check("event_to_state: SessionEnd → IDLE", claude.event_to_state["SessionEnd"] == State.IDLE)

# ----- I/O methods: structural-only (present + callable; behavior tested per-engine elsewhere) ---
# `bundle` drops out of structural-only here — it is behaviorally exercised by the dual-layout layer
# below; `iter_messages` / `resolve_transcript` get their behavior in tests/test_codex_engine.py.

for io_method in ("capture_session_id", "resolve_transcript", "iter_messages"):
    check(f"{io_method}: present + callable (structural-only)", callable(getattr(claude, io_method)))


# ----- pure-builder smoke (Codex) — no I/O ------------------------------------------------------

codex = get(Engine.CODEX)

check("codex: binary == 'codex'", codex.binary == "codex")
check("codex: matches_binary('codex') is True", codex.matches_binary("codex") is True)
check("codex: matches_binary('/opt/homebrew/bin/codex -m x') is True",
      codex.matches_binary("/opt/homebrew/bin/codex -m x") is True)
check("codex: matches_binary('claude --x') is False", codex.matches_binary("claude --x") is False)
check("codex: matches_binary('') is False", codex.matches_binary("") is False)

codex_fresh = codex.build_launch_command(initial_prompt="do the thing")
check("codex build_launch_command: binary first", codex_fresh[0] == "codex")
check("codex build_launch_command: renders -m gpt-5.5 -c model_reasoning_effort=high",
      codex_fresh[1:5] == ["-m", "gpt-5.5", "-c", "model_reasoning_effort=high"])
check("codex build_launch_command: bakes BOTH bypass flags",
      "--dangerously-bypass-approvals-and-sandbox" in codex_fresh
      and "--dangerously-bypass-hook-trust" in codex_fresh)
check("codex build_launch_command: prompt is the positional tail", codex_fresh[-1] == "do the thing")
check("codex build_launch_command: bare call has no positional prompt (ends on the yolo flags)",
      codex.build_launch_command()[-1] == "--dangerously-bypass-hook-trust")

codex_resume = codex.resume_command("CID-1")
check("codex resume_command: native subcommand + id", codex_resume[:3] == ["codex", "resume", "CID-1"])
check("codex resume_command: keeps the hook-trust bypass so hooks fire",
      "--dangerously-bypass-hook-trust" in codex_resume)

codex_fork = codex.fork_command("CID-2")
check("codex fork_command: native subcommand + id", codex_fork[:3] == ["codex", "fork", "CID-2"])
check("codex fork_command: keeps the hook-trust bypass",
      "--dangerously-bypass-hook-trust" in codex_fork)

codex_seed = codex.seed_command("read the brief")
check("codex seed_command: binary first", codex_seed[0] == "codex")
check("codex seed_command: seed is the positional tail", codex_seed[-1] == "read the brief")

codex_distill = codex.distiller_command()
check("codex distiller_command: gpt-5.5 at high effort, no positional prompt",
      codex_distill[1:5] == ["-m", "gpt-5.5", "-c", "model_reasoning_effort=high"]
      and codex_distill[-1] == "--dangerously-bypass-hook-trust")

check("codex state_source is HOOK_EVENTS (Codex emits a Stop hook)",
      codex.state_source is StateSource.HOOK_EVENTS)
check("codex event_to_state: UserPromptSubmit → WORKING",
      codex.event_to_state["UserPromptSubmit"] == State.WORKING)
check("codex event_to_state: Stop → WAITING", codex.event_to_state["Stop"] == State.WAITING)
check("codex event_to_state: PermissionRequest → WAITING",
      codex.event_to_state["PermissionRequest"] == State.WAITING)
check("codex event_to_state: no SessionEnd edge (Codex has no session-end event)",
      "SessionEnd" not in codex.event_to_state)


# ----- dual-layout bundle (required, T6 §4) — hermetic, temp homes ------------------------------
# The per-engine history bundle LAYOUT (design §6.4): Claude mirrors the transcript + its sibling
# sidecar dirs; Codex mirrors the rollout JSONL ALONE. Both run the SAME shared copy machinery in
# history.py (append-by-offset + copy-if-absent + the coalescing lock) — only which paths get
# mirrored differs. Disk-backed but hermetic: every home is a temp dir (T6 §7 — never touch the live
# ~/.claude / ~/.codex / $TX_IDE_HOME). The homes are read at call time, so setting them here (after
# the pure checks) is enough.

import os  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402

os.environ["TX_IDE_HOME"] = tempfile.mkdtemp()
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp()
os.environ["CODEX_HOME"] = tempfile.mkdtemp()

from tx import history  # noqa: E402
from tx.storage import ensure_home  # noqa: E402

ensure_home()
FIXTURES_CODEX = Path(__file__).resolve().parent / "fixtures" / "codex"

# Claude source: a transcript with a sibling <id>/ sidecar (subagents/ + tool-results/, design §5).
claude_cwd, claude_chat = "/Users/me/claude-proj", "CLAUDE-CHAT-1"
claude_project = claude_engine.project_dir(claude_cwd)
(claude_project / claude_chat / "subagents").mkdir(parents=True, exist_ok=True)
(claude_project / claude_chat / "tool-results").mkdir(parents=True, exist_ok=True)
(claude_project / f"{claude_chat}.jsonl").write_text('{"type":"user"}\n')
(claude_project / claude_chat / "subagents" / "sub.jsonl").write_text("{}\n")
(claude_project / claude_chat / "tool-results" / "out.txt").write_text("result-bytes")

claude_bundle = history.ingest_chat("tx-claude", claude_chat, claude_cwd, Engine.CLAUDE, wait=True)
check("dual-layout/claude: transcript.jsonl mirrored",
      (claude_bundle / "transcript.jsonl").exists())
check("dual-layout/claude: subagents/ sidecar dir mirrored",
      (claude_bundle / "subagents" / "sub.jsonl").exists())
check("dual-layout/claude: tool-results/ sidecar dir mirrored",
      (claude_bundle / "tool-results" / "out.txt").read_text() == "result-bytes")

# Codex source: a rollout JSONL alone (a T7 fixture), NO sibling dir — the sidecar-free layout.
codex_chat = "CODEX-CHAT-1"
rollout = (codex_engine.sessions_root() / "2026" / "06" / "08"
           / f"rollout-2026-06-08T18-03-15-{codex_chat}.jsonl")
rollout.parent.mkdir(parents=True, exist_ok=True)
shutil.copy(FIXTURES_CODEX / "fresh.jsonl", rollout)

codex_bundle = history.ingest_chat("tx-codex", codex_chat, "/Users/me/codex-proj", Engine.CODEX, wait=True)
check("dual-layout/codex: rollout mirrored to transcript.jsonl",
      (codex_bundle / "transcript.jsonl").exists())
check("dual-layout/codex: transcript.jsonl is the whole rollout, byte-for-byte",
      (codex_bundle / "transcript.jsonl").read_bytes() == rollout.read_bytes())
check("dual-layout/codex: the bundle is the single rollout .jsonl — NO sidecar dirs created",
      [child.name for child in codex_bundle.iterdir() if child.is_dir()] == [])


print(f"OK — {PASSED} checks passed")
