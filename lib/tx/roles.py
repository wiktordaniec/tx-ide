"""Role-file resolution for `tx spawn --role`: names → concatenated system-prompt contents,
following COMMON.md's override semantics (user-agents/NAME.md replaces, NAME.local.md extends).

A role file may open with a frontmatter block granting skills (see skills.py):

    ---
    tx:
      skills: [tx-sessions, tx-artifacts]
    ---

The block is metadata for tx alone — `load_role_priming` injects only the body, and the `tx:`
namespacing keeps the keys inert should the files ever land in an engine-scanned directory."""

from __future__ import annotations

import re
from pathlib import Path

from .storage import agents_dir, user_agents_dir

COMMON_ROLE = "COMMON"
ROLE_SUFFIX = ".md"
LOCAL_SUFFIX = ".local.md"
FRONTMATTER_DELIMITER = "---"
SKILLS_PATTERN = re.compile(r"^\s*skills:\s*\[(.*)\]\s*$")


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
    their own `#` titles, so no synthetic headers). Frontmatter is tx metadata, never priming."""
    return "\n\n".join(
        _split_frontmatter(path)[1].strip() for path in resolve_role_files(names)
    )


def parse_skill_grants(path: Path) -> list[str]:
    """The skill names a role file grants: its frontmatter `skills: [a, b]` line, empty when the
    file has no frontmatter or no grant."""
    for line in _split_frontmatter(path)[0]:
        match = SKILLS_PATTERN.match(line)
        if match:
            return [name.strip() for name in match.group(1).split(",") if name.strip()]
    return []


def _split_frontmatter(path: Path) -> tuple[list[str], str]:
    """A role file's `(frontmatter lines, body)` — `([], whole text)` when it has none."""
    text = path.read_text()
    if not text.startswith(f"{FRONTMATTER_DELIMITER}\n"):
        return [], text
    lines = text.split("\n")
    for index in range(1, len(lines)):
        if lines[index].strip() == FRONTMATTER_DELIMITER:
            return lines[1:index], "\n".join(lines[index + 1 :])
    raise RoleError(f"{path}: unclosed frontmatter block")
