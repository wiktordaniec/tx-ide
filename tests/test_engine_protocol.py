#!/usr/bin/env python3.14
"""Standalone protocol-conformance test for every registered `EngineAdapter` (task T1, Q-T1b).

No pytest in this repo, so this is directly runnable:  python3.14 tests/test_engine_protocol.py
Exits non-zero on the first failure (same convention as tests/test_messages.py). From T1 on this is a
global merge gate: every engine the registry holds is run through the one contract (design §2), so a
new adapter cannot land half-implementing the surface.

Two layers:
  - **structural, every registered engine** — `runtime_checkable` `isinstance` against
    `EngineAdapter`, then each required method/property present + callable with the right arity;
  - **pure-builder smoke (Claude)** — the launch/ops builders return argv with the binary first, on
    synthetic args, plus the capability flag + the event→state table.

The I/O methods (`capture_session_id` / `resolve_transcript` / `iter_messages` / `bundle`) are checked
present + callable ONLY — their behavior is tested per-engine elsewhere. So this test does NO real
spawn, disk, or network: importing the package registers the adapter (no live home touched) and the
builders are pure string assembly.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from tx.engines import EngineAdapter, StateSource, get, registered  # noqa: E402
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

for io_method in ("capture_session_id", "resolve_transcript", "iter_messages", "bundle"):
    check(f"{io_method}: present + callable (structural-only)", callable(getattr(claude, io_method)))

print(f"OK — {PASSED} checks passed")
