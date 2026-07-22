"""Role-file resolution for `tx spawn --role`: names → concatenated system-prompt contents,
following COMMON.md's override semantics (user-agents/NAME.md replaces, NAME.local.md extends)."""

from __future__ import annotations

from pathlib import Path

from .storage import agents_dir, user_agents_dir

COMMON_ROLE = "COMMON"
ROLE_SUFFIX = ".md"
LOCAL_SUFFIX = ".local.md"


class RoleError(RuntimeError):
    """A bad role name — fail loudly, never guess (COMMON.md)."""


def resolve_role_files(names: list[str]) -> list[Path]:
    """The ordered files backing `names`: COMMON auto-prepended, deduplicated, user override
    replaces shipped, `.local.md` extends."""
    ordered: list[str] = [COMMON_ROLE]
    for name in names:
        # A separator or dot-name would escape the role directories (`--role ../secret`).
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
    """The concatenated contents to inject additively into the engine's system prompt (files carry
    their own `#` titles, so no synthetic headers)."""
    return "\n\n".join(
        path.read_text().strip() for path in resolve_role_files(names)
    )
