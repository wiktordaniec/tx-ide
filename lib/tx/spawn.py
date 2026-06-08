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

from .engines import claude
from .session import Kind, Role

# Nvim companion command. `tmux new-session -d` strips the terminal's OSC11 background hint, so
# nvim's auto-mode would land on the light variant — force dark + tokyonight-moon (COMMON.md).
NVIM_BASE_COMMAND = "nvim +'set background=dark | colorscheme tokyonight-moon'"
SHELL_COMMANDS = frozenset({"zsh", "bash", "sh", "fish", "dash"})


def infer_role(command: str) -> Role:
    """Derive the session role from its launch command's binary.

    Mirrors install-flip §6's inference: the agent binary → LLM, `nvim` → NVIM, a login shell →
    SHELL, everything else → OTHER. Pass an already-resolved command (the CLI defaults an omitted
    `--cmd` to the real `$SHELL`, so a shell spawn classifies as SHELL rather than OTHER).
    """
    parts = command.split()
    binary = os.path.basename(parts[0]) if parts else ""
    if binary == claude.CLAUDE_BIN:
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

    @classmethod
    def for_process(
        cls, *, name: str, tags: list[str], cwd: str, cmd: str,
        env: dict[str, str] | None = None,
    ) -> SpawnSpec:
        """A normal worker/agent/shell session (`tx spawn`). An llm command always gets a chat id
        minted + `--session-id`-injected by `SessionService._spawn` (mandatory, derived from the
        role) — there is no chat flag to pass."""
        return cls(
            name=name, kind=Kind.PROCESS, role=infer_role(cmd), cwd=cwd, cmd=cmd,
            tags=list(tags), env=dict(env or {}),
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
