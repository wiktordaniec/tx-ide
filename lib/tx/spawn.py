"""`SpawnSpec` — the single validated value object for a spawn (stage S1a).

Collapses the three near-identical bash parsers (`cmd_spawn` / `cmd_spawn_nvim` /
`cmd_spawn_view` in the old `bin/tx`) into one value object with builders. The CLI parses argv
into one of these; `SessionService._spawn` consumes it. **The role is derived from the launch
command** — the robust signal (DEVELOPER "no redundant parameters"): a bare `tx spawn` infers
`llm`/`nvim`/`shell`/`other` from the binary rather than carrying a `--role` flag. This is the
same mapping the Flip re-derivation documents (install-flip.md §6).

See tx-service-redesign.md §1 (SpawnSpec). `agent` is intentionally absent (D9 — no `agent` field).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# Import the bundled adapters for their registry SIDE-EFFECT only: each `engines/<name>.py` calls
# `register(...)` at import, so importing them here is what makes `registered()` (used by
# `infer_role`) see EVERY engine — not just Claude. The module names are otherwise unused (the
# lookup goes through `get`/`registered`), hence the noqa. Mirrors the registration import in
# tests/test_engine_protocol.py.
from .engines import claude, codex  # noqa: F401
from .engines import get, registered
from .session import Engine, Kind, Role

# Nvim companion command. `tmux new-session -d` strips the terminal's OSC11 background hint, so
# nvim's auto-mode would land on the light variant — force dark + tokyonight-moon (COMMON.md).
NVIM_BASE_COMMAND = "nvim +'set background=dark | colorscheme tokyonight-moon'"
SHELL_COMMANDS = frozenset({"zsh", "bash", "sh", "fish", "dash"})


def infer_role(command: str) -> Role:
    """Derive the session role from its launch command's binary.

    Mirrors install-flip §6's inference: ANY registered engine's binary → LLM, `nvim` → NVIM, a
    login shell → SHELL, everything else → OTHER. Engine-agnostic by design (T8): the LLM test asks
    every registered `EngineAdapter` whether the command is its binary (`claude`, `codex`, …) rather
    than hard-coding `claude`, so a `codex …` command classifies as LLM exactly like `claude …`. This
    is **role only** — it deliberately does NOT return WHICH engine matched (the engine is set from
    `--engine` at spawn and stored on the record, never inferred from a command — design §1).
    Pass an already-resolved command (the CLI defaults an omitted `--cmd` to the real `$SHELL`, so a
    shell spawn classifies as SHELL rather than OTHER).
    """
    parts = command.split()
    binary = os.path.basename(parts[0]) if parts else ""
    if any(get(engine).matches_binary(command) for engine in registered()):
        return Role.LLM
    if binary == "nvim":
        return Role.NVIM
    if binary in SHELL_COMMANDS:
        return Role.SHELL
    return Role.OTHER


@dataclass
class SpawnSpec:
    """Everything needed to bring one session into being. Built via the classmethods below; the
    `cmd` is always the final, resolved command string that tmux will run (and that `infer_role`
    classifies), never a shell-expansion placeholder."""

    name: str
    kind: Kind
    role: Role
    cwd: str
    cmd: str
    tags: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    # The agent engine this session runs (v3, design §1) — set from `tx spawn --engine` and stamped
    # onto `Session.engine` by `service._spawn`. `None` means "unspecified": `_spawn` defaults an llm
    # session to Claude (the engine default) and leaves a non-llm session engine-less. NEVER inferred
    # from `cmd` post-spawn — the engine is declared at spawn and read off the record thereafter (T8).
    engine: Engine | None = None
    # A chat-op that records its OWN `ChatRef` (fork / handover / resume) sets this so `_spawn` does
    # not also write a pending `original` ref (T4 capture-after-launch). A plain spawn leaves it False
    # and `_spawn` writes the pending original ref the first hook will fill. Not derivable from `cmd`:
    # post-pre-mint a handover worker's command is an ordinary `claude …`, indistinguishable from an
    # original, so the caller signals ownership explicitly.
    records_own_chat: bool = False

    @classmethod
    def for_process(
        cls, *, name: str, tags: list[str], cwd: str, cmd: str,
        env: dict[str, str] | None = None, records_own_chat: bool = False,
        engine: Engine | None = None,
    ) -> SpawnSpec:
        """A normal worker/agent/shell session (`tx spawn`). A plain llm spawn gets a pending
        `original` `ChatRef` from `SessionService._spawn`; its chat id is captured from the first hook
        payload, not minted (T4). A chat-op that records its own ref passes `records_own_chat=True`.
        `engine` carries the `--engine` choice (default Claude resolved in `_spawn`); leave it `None`
        for a shell/nvim/other spawn or to take the engine default."""
        return cls(
            name=name, kind=Kind.PROCESS, role=infer_role(cmd), cwd=cwd, cmd=cmd,
            tags=list(tags), env=dict(env or {}), records_own_chat=records_own_chat,
            engine=engine,
        )

    @classmethod
    def for_nvim(
        cls, *, name: str, tags: list[str], cwd: str,
        env: dict[str, str] | None = None, diff_base: str | None = None,
    ) -> SpawnSpec:
        """An nvim companion (`tx spawn-nvim`). When `diff_base` is given, open straight into a
        diffview against it (`--diff` defaults the base to `main` at the CLI boundary)."""
        command = NVIM_BASE_COMMAND
        if diff_base is not None:
            command += f" +'DiffviewOpen {diff_base}'"
        return cls(
            name=name, kind=Kind.PROCESS, role=Role.NVIM, cwd=cwd, cmd=command,
            tags=list(tags), env=dict(env or {}),
        )

    @classmethod
    def for_view(
        cls, *, name: str, tags: list[str], cwd: str, cmd: str,
        env: dict[str, str] | None = None,
    ) -> SpawnSpec:
        """A `kind=view` home base (`tx spawn-view`) — surfaces under VIEWS and is filtered out of
        the picker. Role is still inferred from `cmd` (a view runs a shell → SHELL)."""
        return cls(
            name=name, kind=Kind.VIEW, role=infer_role(cmd), cwd=cwd, cmd=cmd,
            tags=list(tags), env=dict(env or {}),
        )
