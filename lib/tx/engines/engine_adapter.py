from __future__ import annotations

from collections.abc import Iterator, Mapping
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..session import State


DEFAULT_EFFORT = 3
EFFORT_LEVELS = {
    1: "low",
    2: "medium",
    3: "high",
    4: "xhigh",
    5: "max",
}


class EngineError(RuntimeError):
    """An engine adapter could not honor a request (unsupported effort tier, failed chat surgery).
    Lives here — not in service.py — so adapters can raise it without a service import cycle; the
    CLI boundary catches it alongside ServiceError and maps it to a stderr line + exit 1."""


class StateSource(str, Enum):
    # Where an engine's turn-done → WAITING transition comes from. Claude/Codex emit a Stop hook
    # event; an engine without one (e.g. Gemini) is polled via its statusline agent_state.
    HOOK_EVENTS = "hook_events"
    STATUS_POLL = "status_poll"


@runtime_checkable
class EngineAdapter(Protocol):
    # The adapter surface for one coding-agent CLI; the rest of the core stays engine-blind. Named
    # EngineAdapter (not Engine) so it coexists with the Engine enum the registry keys on.

    # ----- identity -----

    @property
    def binary(self) -> str: ...

    def matches_binary(self, command: str) -> bool:
        """Whether `command` invokes this engine's binary (infer_role / reconcile use this)."""
        ...

    def capture_session_id(self, hook_payload: dict) -> tuple[str, str]:
        """`(session_id, transcript_path)` off a hook payload — captured, never minted."""
        ...

    # ----- launch / ops -----

    def build_launch_command(
        self,
        *,
        model: str | None = None,
        effort: int | None = None,
        initial_prompt: str | None = None,
        read_only: bool = False,
        role_priming: str | None = None,
        env: dict[str, str] | None = None,
    ) -> list[str]:
        """Argv for a fresh session. `role_priming` is injected additively (never replacing the base
        prompt) so chat ops inherit it — as a persona value-flag when the engine has one, else via a
        launch-env pointer the adapter ADDS to `env` (which is the session's launch environment,
        mutable for exactly that purpose; an engine consulting its own home must resolve it as the
        child will)."""
        ...

    def resume_command(
        self, chat_id: str, *, read_only: bool = False, source_cmd: str | None = None
    ) -> list[str]:
        """Argv to resume an existing chat in place; `source_cmd` carries the source persona onto
        the resumed record so later chat ops derived from it keep it."""
        ...

    def fork_command(
        self, source_cmd: str, chat_id: str, *, read_only: bool = False
    ) -> list[str]:
        """Argv to fork a chat onto its full history, carrying the source persona (parsed per-engine,
        preserving unknown value-flags) plus this fork's own identity."""
        ...

    def seed_command(
        self, source_cmd: str, seed: str, *, read_only: bool = False
    ) -> list[str]:
        """Argv for a fresh session carrying the source persona (no identity flag) + `seed` as the
        initial-prompt positional. Preserve unknown value-flags (the #50 safe default)."""
        ...

    def distiller_command(self, seed: str) -> list[str]:
        """Argv for the fixed distiller pass (no source persona) that summarizes a chat into a brief."""
        ...

    def prepare_chat_for_cwd(
        self, chat_id: str, source_cwd: str, target_cwd: str
    ) -> None:
        """Make an existing chat discoverable when a continuation moves to another cwd."""
        ...

    def prepare_workspace(
        self, command: str, cwd: str, env: Mapping[str, str]
    ) -> str:
        """Bind an engine command to its FINAL working directory and return the bound command.

        Launch commands are built before the worker's worktree exists (the CLI parse / chat-op
        derivation happens first; `spawn_worker` creates the checkout after), so an engine whose
        launch is cwd-dependent — workspace-binding argv, per-worktree hook/rules files — rebinds
        here, at the one seam where the final cwd is first known (`_prepare_worker_access`, and the
        rollover respawn which reuses the session's cwd). Idempotent: re-running against the same
        cwd must be safe (resume reuses a prepared worktree). Claude/Codex are cwd-independent and
        return `command` unchanged."""
        ...

    def is_read_only_command(self, command: str) -> bool:
        """Validate inner read-only controls once at launch, never on hooks or messages."""
        ...

    # ----- transcript -----

    def resolve_transcript(self, chat_id: str, cwd: str) -> Path:
        """Locate a chat's transcript on disk. May not exist yet — existence is the caller's check."""
        ...

    def iter_messages(self, transcript: Path) -> Iterator[dict]:
        """Yield each turn as an engine-neutral message dict, normalizing the on-disk schema."""
        ...

    def bundle_sidecars(self, src_transcript: Path, chat_id: str) -> list[Path]:
        """Pure (no I/O): sidecar dirs to copy into the history bundle beside the transcript. Claude
        returns its `<chat-id>/` dir (subagents/ + tool-results/); Codex returns [] (rollout inlines
        everything). history.py runs the actual copy."""
        ...

    # ----- hooks / state -----

    @property
    def event_to_state(self) -> Mapping[str, State]:
        """Hook-event-name → State table (e.g. UserPromptSubmit→WORKING, Stop→WAITING)."""
        ...

    @property
    def state_source(self) -> StateSource: ...
