"""Skill resolution + per-worktree materialisation for `tx spawn`.

A skill is a procedure with a self-evident trigger — `agents/skills/<name>/SKILL.md`, the format
all three engines index natively (frontmatter `name` + `description` always in context, body read
on activation). Role files grant skills via frontmatter (`skills: [...]`, see roles.py); the
resolved grant travels on the spawn environment (SKILLS_ENV) so `prepare_workspace` can symlink
each skill into the engine's per-worktree discovery directory once the final cwd exists. Symlinks,
not copies — a skill edit reaches every running session on its next turn, unlike role priming,
which is frozen into argv at spawn."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from .roles import RoleError, parse_skill_grants, resolve_role_files
from .storage import agents_dir, user_agents_dir
from .worktree import WorktreeError, WorktreeManager

SKILLS_ENV = "TX_SKILLS"
SKILLS_SUBDIR = "skills"
SKILL_FILE_NAME = "SKILL.md"


def resolve_skill_directory(name: str) -> Path:
    """The canonical directory backing a skill name — `user-agents/skills/<name>` replaces
    `agents/skills/<name>` (the roles.py override semantics). Validates the engine contract every
    spawn: a SKILL.md whose frontmatter `name` matches the directory and carries a description."""
    directory = user_agents_dir() / SKILLS_SUBDIR / name
    if not directory.is_dir():
        directory = agents_dir() / SKILLS_SUBDIR / name
    skill_file = directory / SKILL_FILE_NAME
    if not skill_file.is_file():
        raise RoleError(
            f"unknown skill '{name}' (no {SKILL_FILE_NAME} under "
            f"{user_agents_dir() / SKILLS_SUBDIR} or {agents_dir() / SKILLS_SUBDIR})"
        )
    frontmatter = _skill_frontmatter(skill_file)
    if frontmatter.get("name") != name:
        raise RoleError(
            f"skill '{name}': frontmatter name '{frontmatter.get('name', '')}' must equal "
            f"the directory name (the engines' discovery contract)"
        )
    if not frontmatter.get("description"):
        raise RoleError(
            f"skill '{name}': frontmatter needs a description — it is the only part always "
            "in an agent's context, and the entire routing signal"
        )
    return directory


def _skill_frontmatter(skill_file: Path) -> dict[str, str]:
    """Top-level `key: value` pairs of a SKILL.md frontmatter block."""
    text = skill_file.read_text()
    if not text.startswith("---\n"):
        return {}
    pairs: dict[str, str] = {}
    for line in text.split("\n")[1:]:
        if line.strip() == "---":
            return pairs
        key, separator, value = line.partition(":")
        if separator and not key.startswith((" ", "\t")):
            pairs[key.strip()] = value.strip()
    raise RoleError(f"{skill_file}: unclosed frontmatter block")


def resolve_role_skills(role_names: list[str]) -> list[str]:
    """The ordered skill grant for a `--role` list: the union of every resolved role file's
    frontmatter `skills:` list (COMMON seeded first, as with the files themselves), deduplicated,
    each name validated so a bad grant fails the spawn rather than a worker's first pull."""
    skills: list[str] = []
    for path in resolve_role_files(role_names):
        for name in parse_skill_grants(path):
            if name not in skills:
                skills.append(name)
    for name in skills:
        resolve_skill_directory(name)
    return skills


def granted_skills(env: Mapping[str, str]) -> list[str]:
    """The skill names to materialise for a launch. Engine-built spawns carry the resolved grant
    in SKILLS_ENV; a hand-written `--cmd` worker (no env entry) gets the COMMON grant, so skill
    discovery is opt-out — a role-less worker degrades to "has the shared skills", not "has
    nothing"."""
    value = env.get(SKILLS_ENV)
    if value is None:
        return resolve_role_skills([])
    return [name for name in value.split(",") if name]


def link_skills(cwd: str, env: Mapping[str, str], engine_directory: str) -> None:
    """Symlink each granted skill into `<cwd>/<engine_directory>/<name>` — the engine's
    per-worktree discovery dir (`.claude/skills` for claude, `.agents/skills` for codex and
    antigravity) — and hide the dir from `git status`. Idempotent: resume re-runs it on a
    prepared worktree, and a moved canonical store just re-points the links."""
    names = granted_skills(env)
    if not names:
        return
    target_root = Path(cwd) / engine_directory
    target_root.mkdir(parents=True, exist_ok=True)
    for name in names:
        source = resolve_skill_directory(name)
        link = target_root / name
        if link.is_symlink():
            if link.readlink() == source:
                continue
            link.unlink()
        link.symlink_to(source)
    exclude_from_git(cwd, f"{engine_directory}/")


def exclude_from_git(cwd: str, line: str) -> None:
    """Hide a generated path from `git status` via the repository's shared info/exclude (a linked
    worktree has no per-worktree exclude — info/ lives in the common git dir). A non-git cwd
    (scratch dir) has nothing to hide."""
    try:
        common_directory = WorktreeManager().git_common_directory(cwd)
    except WorktreeError:
        return
    exclude = common_directory / "info" / "exclude"
    existing = exclude.read_text() if exclude.is_file() else ""
    if line in existing.splitlines():
        return
    exclude.parent.mkdir(exist_ok=True)
    separator = "" if existing.endswith("\n") or not existing else "\n"
    exclude.write_text(f"{existing}{separator}{line}\n")
