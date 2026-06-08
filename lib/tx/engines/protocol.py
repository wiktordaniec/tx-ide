"""The `Engine` adapter protocol — the seam tx calls instead of any one agent CLI (design §2).

T0 establishes the **surface only**: one `typing.Protocol` capturing the methods + the single
capability flag every engine renders its own way. No concrete adapter lives here yet — `ClaudeEngine`
(the extraction of `lib/tx/claude.py`) is authored in T1, which also adds the protocol-conformance
test that becomes a global merge gate from then on.

Naming: design §2 calls this "the `Engine` protocol". The class is named **`EngineAdapter`** (not
`Engine`) so it can coexist with the `Engine` *enum* (`tx.session.Engine` — the value claude/codex/
gemini): the registry keys the enum to an adapter, so importing two same-named symbols into one
module is the collision this rename avoids. Read `EngineAdapter` as "an implementation of the Engine
protocol".

The surface mirrors `lib/tx/claude.py`'s existing functions so T1's extraction is a thin wrap:
  - identity      — which binary this is, whether a command is ours, capture id+path from a hook
  - launch / ops  — build the argv for a fresh / resumed / forked / seeded / distiller run
  - transcript    — resolve the on-disk transcript, iterate its messages, copy it into a bundle
  - hooks / state — the hook-event → `State` table, and where turn-done state comes from (the flag)

Every method renders the abstract `(model, effort)` pair its own way (Claude `--effort`, Codex
`-c model_reasoning_effort=`, Gemini effort-in-model-id — design §3); callers stay engine-blind.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..session import ChatRef, State


class StateSource(str, Enum):
    """Where an engine's turn-done → WAITING transition comes from — the one capability flag that
    really varies across engines (design §2). Claude and Codex emit a `Stop` hook event; a future
    engine without a turn-done hook (e.g. Gemini Antigravity) is polled via its statusline
    `agent_state`. The core reads this to decide whether to listen for a hook or schedule a poll."""

    HOOK_EVENTS = "hook_events"  # turn-done arrives as a hook event (Claude, Codex)
    STATUS_POLL = "status_poll"  # no turn-done hook — poll the statusline (e.g. Antigravity)


@runtime_checkable
class EngineAdapter(Protocol):
    """The adapter surface for one coding-agent CLI (design §2). One implementation per engine,
    called by the otherwise engine-blind core (tmux liveness, the durable record, chat-op
    orchestration, the state machine, and history all stay shared). A new engine implements these
    members + ships one `setup/engines/<name>.sh`; everything else is free.

    Surface only at T0 — every body is `...`; T1's `ClaudeEngine` is the first concrete
    implementation and authors the conformance test that exercises this contract.
    """

    # ----- identity ----------------------------------------------------------------------------

    @property
    def binary(self) -> str:
        """The launch binary this engine drives (`claude`, `codex`, `agy`)."""
        ...

    def matches_binary(self, command: str) -> bool:
        """Whether `command` (a launch command string) invokes this engine's binary — the test
        `infer_role`/reconcile use to recognize an engine's sessions from their command (T1+)."""
        ...

    def capture_session_id(self, hook_payload: dict) -> tuple[str, str]:
        """Read `(session_id, transcript_path)` off a hook payload. The id is **captured, never
        minted** (design §2): every engine's hook carries its own id + transcript path, so this one
        post-launch path replaces any pre-mint special-case."""
        ...

    # ----- launch / ops ------------------------------------------------------------------------

    def build_launch_command(
        self,
        *,
        model: str | None = None,
        effort: str | None = None,
        initial_prompt: str | None = None,
    ) -> list[str]:
        """Assemble the argv for a fresh session — rendering `(model, effort)` this engine's way and
        baking in its own yolo flags (design §4.3: tx builds the command, callers don't)."""
        ...

    def resume_command(self, chat_id: str) -> list[str]:
        """The argv to resume an existing chat in place (Claude `--resume <id>`, Codex
        `codex resume <id>`)."""
        ...

    def fork_command(self, chat_id: str) -> list[str]:
        """The argv to fork an existing chat into a new session that opens on its full history."""
        ...

    def seed_command(self, prompt: str) -> list[str]:
        """The argv for a fresh session carrying a seed prompt — handover/rollover's new worker."""
        ...

    def distiller_command(self) -> list[str]:
        """The argv for the distiller pass that summarizes a chat into a handover/rollover brief."""
        ...

    # ----- transcript --------------------------------------------------------------------------

    def resolve_transcript(self, chat_id: str, cwd: str) -> Path:
        """Locate a chat's on-disk transcript (Claude derives it by formula from the cwd; Codex
        globs its rollout dir — design §3). The path may not exist yet; existence is the caller's
        check."""
        ...

    def iter_messages(self, transcript: Path) -> Iterator[dict]:
        """Yield each turn of a transcript as an engine-neutral message dict, normalizing the
        engine's on-disk schema (Anthropic vs OpenAI Responses items — design §3) for the shared
        history / messages layer."""
        ...

    def bundle(self, chat: ChatRef) -> Path:
        """Ingest a chat into its durable history bundle and return the bundle dir. Claude copies the
        transcript + its sidecar dir; Codex copies the rollout JSONL alone (design §5)."""
        ...

    # ----- hooks / state -----------------------------------------------------------------------

    @property
    def event_to_state(self) -> Mapping[str, State]:
        """The hook-event-name → `State` table for this engine (design §2/§3): e.g.
        `UserPromptSubmit` → WORKING, `Stop` → WAITING. The hook dispatcher reads it to drive the
        record's activity state; the same hook also returns the captured id/path."""
        ...

    @property
    def state_source(self) -> StateSource:
        """Whether turn-done state arrives as a hook event or must be polled — the capability flag
        above. Lets the core wire the right liveness path without knowing the engine."""
        ...
