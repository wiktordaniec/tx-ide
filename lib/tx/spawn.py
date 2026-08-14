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
import shlex
from dataclasses import dataclass, field

# Side-effect import: each adapter self-registers at import, so registered() sees every engine.
from .engines import antigravity, claude, codex  # noqa: F401
from .engines import registry
from .session import Engine, Role

# Nvim companion command. `tmux new-session -d` strips the terminal's OSC11 background hint, so
# nvim's auto-mode would land on the light variant — force dark + tokyonight-moon (COMMON.md).
NVIM_BASE_COMMAND = "nvim +'set background=dark | colorscheme tokyonight-moon'"
SHELL_COMMANDS = frozenset({"zsh", "bash", "sh", "fish", "dash"})


def infer_role(command: str) -> Role:
    """Role (not engine) from a launch command's binary: any registered engine → LLM, nvim → NVIM,
    a login shell → SHELL, else OTHER. Pass an already-resolved command (an omitted `--cmd` is the
    real `$SHELL`, so it classifies as SHELL not OTHER)."""
    parts = command.split()
    binary = os.path.basename(parts[0]) if parts else ""
    if any(registry.get(engine).matches_binary(command) for engine in registry.registered()):
        return Role.LLM
    if binary == "nvim":
        return Role.NVIM
    if binary in SHELL_COMMANDS:
        return Role.SHELL
    return Role.OTHER


@dataclass
class SpawnSpec:
    """Everything needed to bring one session into being. Built via the classmethods below; the
    `cmd` is always the final, resolved engine command that is persisted (and that `infer_role`
    classifies), never a shell-expansion placeholder. `launch_cmd`, when set, is what tmux runs."""

    name: str
    role: Role
    cwd: str
    cmd: str
    tags: list[str] = field(default_factory=list)
    # Explicit effort-group override (`--group`); None = derived at read time.
    group: str | None = None
    # Work-ancestor override: chat-ops pass their SOURCE session id; None = plain spawn
    # (`_spawn` records the managed executor).
    parent: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    # Every agent worker is placed in a linked tx-owned worktree by SessionService.spawn_worker.
    # Explicit read-only mode binds tx's whole-process filesystem sandbox to repository paths.
    read_only: bool = False
    # The agent engine this session runs (v3, design §1) — set from `tx spawn --engine` and stamped
    # onto `Session.engine` by `service._spawn`. `None` is only valid for a non-llm session: a worker
    # spawn with no engine is refused loudly (`_prepare_worker_access`); the claude default for a bare
    # agent spawn is applied at the CLI boundary, not here. NEVER inferred from `cmd` post-spawn —
    # the engine is declared at spawn and read off the record thereafter (T8).
    engine: Engine | None = None
    # A chat-op that records its OWN `ChatRef` (fork / handover / resume) sets this so `_spawn` does
    # not also write a pending `original` ref (T4 capture-after-launch). A plain spawn leaves it False
    # and `_spawn` writes the pending original ref the first hook will fill. Not derivable from `cmd`:
    # post-pre-mint a handover worker's command is an ordinary `claude …`, indistinguishable from an
    # original, so the caller signals ownership explicitly.
    records_own_chat: bool = False
    # Internal execution-only wrapper. Session.cmd persists the engine command for chat-op
    # derivation; `_spawn` gives tmux this outer sandbox command when present.
    launch_cmd: str | None = None

    @classmethod
    def for_process(
        cls,
        *,
        name: str,
        tags: list[str],
        cwd: str,
        cmd: str,
        env: dict[str, str] | None = None,
        read_only: bool = False,
        records_own_chat: bool = False,
        engine: Engine | None = None,
        group: str | None = None,
        parent: str | None = None,
    ) -> SpawnSpec:
        """A normal worker/agent/shell session (`tx spawn`). A plain llm spawn gets a pending
        `original` `ChatRef` from `SessionService._spawn`; its chat id is captured from the first hook
        payload, not minted (T4). A chat-op that records its own ref passes `records_own_chat=True`
        (and its SOURCE session id as `parent`). `engine` carries the `--engine` choice; leave it
        `None` only for a shell/nvim/other spawn — an agent spawn without one is refused at
        `_prepare_worker_access`."""
        return cls(
            name=name,
            role=infer_role(cmd),
            cwd=cwd,
            cmd=cmd,
            tags=list(tags),
            group=group,
            parent=parent,
            env=dict(env or {}),
            read_only=read_only,
            records_own_chat=records_own_chat,
            engine=engine,
        )

    @classmethod
    def for_nvim(
        cls,
        *,
        name: str,
        tags: list[str],
        cwd: str,
        env: dict[str, str] | None = None,
        diff_base: str | None = None,
        open_file: str | None = None,
        group: str | None = None,
    ) -> SpawnSpec:
        """An nvim companion (`tx spawn-nvim`). `diff_base` opens straight into a diffview against it
        (`--diff` defaults the base to `main` at the CLI boundary); `open_file` opens a file
        (`--open`) — e.g. a plan handed to a worker for review."""
        command = NVIM_BASE_COMMAND
        if diff_base is not None:
            command += f" +'DiffviewOpen {diff_base}'"
        if open_file is not None:
            command += f" {shlex.quote(open_file)}"
        return cls(
            name=name,
            role=Role.NVIM,
            cwd=cwd,
            cmd=command,
            tags=list(tags),
            group=group,
            env=dict(env or {}),
        )

    @classmethod
    def for_view(
        cls,
        *,
        name: str,
        cwd: str,
        cmd: str,
        env: dict[str, str] | None = None,
    ) -> SpawnSpec:
        """A view home base (`tx spawn-view`). `service.spawn_view` realizes this spec as a live
        `@tx_view` tmux session (marker + chrome), never a store record. Role is still inferred from
        `cmd` (a view runs a shell → SHELL), but a view carries **no tags** (Q4) — the store never
        sees it, so there is nothing to tag."""
        return cls(
            name=name,
            role=infer_role(cmd),
            cwd=cwd,
            cmd=cmd,
            env=dict(env or {}),
        )
