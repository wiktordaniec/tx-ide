"""Role-file resolution for `tx spawn --role` — names → concatenated system-prompt contents.

Mirrors the documented override semantics (COMMON.md § Worker priming): for a role NAME the
base file is `user-agents/NAME.md` (replaces the shipped file) or `agents/NAME.md`, and
`user-agents/NAME.local.md` extends it. `COMMON` is always injected first — every session must
follow it — so callers pass only the roles a worker plays. The concatenation is baked into the
launch command by the engine adapters (additive system prompt), never sent via send-keys.
"""

from __future__ import annotations

from pathlib import Path

from .storage import agents_dir, user_agents_dir

COMMON_ROLE = "COMMON"
ROLE_SUFFIX = ".md"
LOCAL_SUFFIX = ".local.md"


class RoleError(RuntimeError):
    """A named role has no file — unknown roles fail loudly, never guessed (COMMON.md)."""


def resolve_role_files(names: list[str]) -> list[Path]:
    """The ordered files backing `names`: COMMON first (auto-prepended, deduplicated), then each
    name in the given order — per name the base file (user override replaces shipped) plus the
    `.local.md` extension when present."""
    ordered: list[str] = [COMMON_ROLE]
    for name in names:
        # A role is a bare file stem inside the role directories — a separator or a dot-name
        # would escape them (`--role ../secret`), so it is rejected, never resolved.
        if Path(name).name != name or name in (".", ".."):
            raise RoleError(f"invalid role name '{name}' (must be a bare name, no path components)")
        if name not in ordered:
            ordered.append(name)
    files: list[Path] = []
    for name in ordered:
        base = user_agents_dir() / f"{name}{ROLE_SUFFIX}"
        if not base.is_file():
            base = agents_dir() / f"{name}{ROLE_SUFFIX}"
        if not base.is_file():
            raise RoleError(
                f"unknown role '{name}' (no {ROLE_SUFFIX} file under "
                f"{user_agents_dir()} or {agents_dir()})"
            )
        files.append(base)
        local = user_agents_dir() / f"{name}{LOCAL_SUFFIX}"
        if local.is_file():
            files.append(local)
    return files


def load_role_priming(names: list[str]) -> str:
    """The concatenated contents to inject additively into the engine's system prompt. Files are
    joined by a blank line and carry their own `#` titles, so no synthetic headers are added."""
    return "\n\n".join(
        path.read_text().strip() for path in resolve_role_files(names)
    )
