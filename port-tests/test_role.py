"""ROLE — roles.py + skills.py through `tx spawn --role` / `--env TX_SKILLS=…` (spec section 04).

Fixture R: a plain `agents/` directory (no installer symlink) holding `COMMON.md` (grants
`tx-sessions`) and the valid skill `agents/skills/tx-sessions/SKILL.md`; fake `claude` / `codex` on
PATH; the H4 repo as `--cwd`. Observables: the record (`cmd`, `env`, `cwd`), the fake dump (argv +
env), the worktree's `.claude/skills/*` links, and `<repo>/.git/info/exclude`.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path

from txkit import GitFixture, Result, TxCase, expected_failure_on_python

COMMON_TEXT = "---\ntx:\n  skills: [tx-sessions]\n---\n# Common\nbody\n"
PRIMING_FLAG = "--append-system-prompt"
CODEX_INSTRUCTIONS_PREFIX = "developer_instructions="
CLAUDE_SKILLS_DIR = ".claude/skills"
CODEX_SKILLS_DIR = ".agents/skills"


def skill_text(name: str, description: str = "d") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\nbody\n"


def priming_of(argv: list[str]) -> str | None:
    """The token after `--append-system-prompt`, or None when the flag is absent."""
    if PRIMING_FLAG not in argv:
        return None
    return argv[argv.index(PRIMING_FLAG) + 1]


def codex_priming_of(argv: list[str]) -> str | None:
    """The value of the `-c developer_instructions=<priming>` pair, or None when absent."""
    for index, token in enumerate(argv[:-1]):
        if token == "-c" and argv[index + 1].startswith(CODEX_INSTRUCTIONS_PREFIX):
            return argv[index + 1][len(CODEX_INSTRUCTIONS_PREFIX):]
    return None


class TestRole(TxCase):
    home_options = {"link_agents": False}

    def setUp(self) -> None:
        super().setUp()
        self.home.agents.mkdir()
        self.write_role("COMMON", COMMON_TEXT)
        self.write_skill("tx-sessions")

    # ----- fixture helpers ---------------------------------------------------------------

    def write_role(self, name: str, text: str, *, user: bool = False, local: bool = False) -> Path:
        directory = self.home.user_agents_dir if user else self.home.agents
        path = directory / f"{name}{'.local' if local else ''}.md"
        path.write_text(text)
        return path

    def write_skill(self, name: str, text: str | None = None, *, user: bool = False) -> Path:
        directory = (self.home.user_agents_dir if user else self.home.agents) / "skills" / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "SKILL.md").write_text(skill_text(name) if text is None else text)
        return directory

    def spawn(self, *args: str, name: str = "w", cwd: Path | None = None) -> Result:
        repo = cwd if cwd is not None else self.git.path
        return self.tx(["spawn", name, "--tag", "t", "--cwd", str(repo), *args])

    def spawned(self, result: Result) -> dict:
        """The record behind a successful spawn (looked up by the printed name)."""
        self.assertEqual(result.code, 0, result.err)
        match = re.fullmatch(r"Spawned '([^']+)' \(cwd=(.+), tag=t\)\n", result.out)
        self.assertIsNotNone(match, result.out)
        record = json.loads(self.tx(["show", match.group(1)]).out)
        self.assertEqual(record["cwd"], match.group(2))
        self.assertTrue(record["cwd"].startswith(str(self.home.worktrees_dir)), record["cwd"])
        return record

    def worker(self, *args: str, engine: str = "claude", name: str = "w", cwd: Path | None = None):
        """Spawn, then return `(record, fake dump)`."""
        record = self.spawned(self.spawn(*args, name=name, cwd=cwd))
        dump = self.fakes.wait_dump(engine, record["id"])
        return record, dump

    def exclude_path(self, repo: GitFixture | None = None) -> Path:
        return (repo or self.git).path / ".git" / "info" / "exclude"

    def assert_role_error(self, result: Result, message: str) -> None:
        self.assertEqual(result.code, 2, result.err)
        self.assertEqual(result.out, "")
        self.assertTrue(result.err.endswith(f"tx spawn: error: {message}\n"), result.err)
        self.assert_nothing_created()

    def assert_nothing_created(self) -> None:
        self.assertEqual(sorted(self.home.sessions_dir.iterdir()), [])
        self.assertEqual(
            [os.path.realpath(path) for path in self.git.worktrees()],
            [os.path.realpath(self.git.path)],
        )
        self.assertEqual(self.tmux.sessions(), [])
        self.assertFalse(self.home.log_path.exists())

    def assert_link_failure(self, result: Result, fragment: str) -> None:
        """A grant that gets past the CLI and fails at link time: exit 1, worktree removed, no
        record (both legs); the FIX leg additionally asserts no traceback."""
        self.assertEqual(result.code, 1, result.err)
        self.assertEqual(result.out, "")
        self.assertIn(fragment, result.err)
        self.assertEqual(sorted(self.home.sessions_dir.iterdir()), [])
        self.assertEqual(
            [os.path.realpath(path) for path in self.git.worktrees()],
            [os.path.realpath(self.git.path)],
        )
        self.assertEqual(self.tmux.sessions(), [])

    # ----- T-ROLE-01 -----------------------------------------------------------------------

    def test_t_role_01_common_prepended_duplicates_dropped(self):
        self.write_role("DEVELOPER", "# Developer\n")
        self.write_role("HISTORIAN", "# Historian\n")
        record, dump = self.worker(
            "--role", "DEVELOPER,HISTORIAN", "--role", "DEVELOPER", "--role", "COMMON"
        )
        expected = "# Common\nbody\n\n# Developer\n\n# Historian"
        self.assertEqual(priming_of(shlex.split(record["cmd"])), expected)
        self.assertEqual(priming_of(dump["argv"]), expected)

    def test_t_role_01_no_role_still_grants_common_skills(self):
        record, dump = self.worker("--engine", "claude")
        self.assertNotIn(PRIMING_FLAG, shlex.split(record["cmd"]))
        self.assertNotIn(PRIMING_FLAG, dump["argv"])
        self.assertEqual(record["env"]["TX_SKILLS"], "tx-sessions")
        self.assertEqual(dump["env"]["TX_SKILLS"], "tx-sessions")

    def test_t_role_01_empty_role_rejected(self):
        self.assert_role_error(self.spawn("--role", ""), "--role requires at least one role name")

    # ----- T-ROLE-02 -----------------------------------------------------------------------

    def test_t_role_02_user_override_replaces_local_extends(self):
        self.write_role("DEVELOPER", "# Shipped\n")
        self.write_role("DEVELOPER", "# UserDev\n", user=True)
        self.write_role("DEVELOPER", "# DevLocal\n", user=True, local=True)
        self.write_role("COMMON", "# CommonLocal\n", user=True, local=True)
        record, dump = self.worker("--role", "DEVELOPER")
        expected = "# Common\nbody\n\n# CommonLocal\n\n# UserDev\n\n# DevLocal"
        self.assertEqual(priming_of(shlex.split(record["cmd"])), expected)
        self.assertEqual(priming_of(dump["argv"]), expected)
        self.assertNotIn("# Shipped", record["cmd"])

    def test_t_role_02_local_never_stands_alone(self):
        self.write_role("LONE", "# Lone\n", user=True, local=True)
        self.assert_role_error(
            self.spawn("--role", "LONE"),
            f"unknown role 'LONE' (no .md file under {self.home.user_agents_dir} or {self.home.agents})",
        )

    # ----- T-ROLE-03 -----------------------------------------------------------------------

    def test_t_role_03_path_components_rejected(self):
        for name in ("../secret", "a/b", ".", ".."):
            with self.subTest(role=name):
                self.assert_role_error(
                    self.spawn("--role", name),
                    f"invalid role name '{name}' (must be a bare name, no path components)",
                )

    def test_t_role_03_validation_precedes_file_lookup(self):
        (self.home.agents / "COMMON.md").unlink()
        result = self.spawn("--role", "../secret")
        self.assert_role_error(
            result, "invalid role name '../secret' (must be a bare name, no path components)"
        )
        self.assertNotIn("unknown role 'COMMON'", result.err)

    # ----- T-ROLE-04 -----------------------------------------------------------------------

    def test_t_role_04_unknown_role_error_text(self):
        self.assert_role_error(
            self.spawn("--role", "NOPE"),
            f"unknown role 'NOPE' (no .md file under {self.home.user_agents_dir} or {self.home.agents})",
        )

    def test_t_role_04_missing_common_surfaces_without_role_flag(self):
        (self.home.agents / "COMMON.md").unlink()
        self.assert_role_error(
            self.spawn("--engine", "claude"),
            f"unknown role 'COMMON' (no .md file under {self.home.user_agents_dir} or {self.home.agents})",
        )

    # ----- T-ROLE-05 -----------------------------------------------------------------------

    def test_t_role_05_priming_strips_frontmatter_joins_with_blank_line(self):
        self.write_role("DEV", "\n# Dev\n\nmore\n\n")
        record, dump = self.worker("--role", "DEV")
        expected = "# Common\nbody\n\n# Dev\n\nmore"
        self.assertEqual(priming_of(dump["argv"]), expected)
        self.assertEqual(priming_of(shlex.split(record["cmd"])), expected)
        self.assertIn(shlex.quote(expected), record["cmd"])

    def test_t_role_05_padded_opening_delimiter_is_not_frontmatter(self):
        self.write_role("SP", " ---\nx\n---\n# Body\n")
        record, dump = self.worker("--role", "SP")
        expected = "# Common\nbody\n\n---\nx\n---\n# Body"
        self.assertEqual(priming_of(dump["argv"]), expected)
        self.assertEqual(priming_of(shlex.split(record["cmd"])), expected)

    def test_t_role_05_codex_priming_travels_as_developer_instructions(self):
        self.write_role("DEV", "\n# Dev\n\nmore\n\n")
        record, dump = self.worker("--role", "DEV", "--engine", "codex", engine="codex")
        expected = "# Common\nbody\n\n# Dev\n\nmore"
        self.assertEqual(codex_priming_of(dump["argv"]), expected)
        self.assertEqual(codex_priming_of(shlex.split(record["cmd"])), expected)
        self.assertNotIn(PRIMING_FLAG, dump["argv"])

    # ----- T-ROLE-06 -----------------------------------------------------------------------

    def test_t_role_06_unclosed_frontmatter(self):
        self.write_role("COMMON", "---\ntx:\n  skills: [a]\n# no close")
        self.assert_role_error(
            self.spawn("--engine", "claude"),
            f"{self.home.agents / 'COMMON.md'}: unclosed frontmatter block",
        )

    def test_t_role_06_padded_closing_delimiter_closes(self):
        self.write_skill("a")
        self.write_role("BAD2", "---\ntx:\n  skills: [a]\n  ---  \n# Bad2 body\n")
        record, dump = self.worker("--role", "BAD2")
        self.assertEqual(priming_of(dump["argv"]), "# Common\nbody\n\n# Bad2 body")
        self.assertEqual(record["env"]["TX_SKILLS"], "tx-sessions,a")

    # ----- T-ROLE-07 -----------------------------------------------------------------------

    def test_t_role_07_skill_grant_line_parsing(self):
        for name in ("tx-artifacts", "a", "b"):
            self.write_skill(name)
        self.write_role("R", "---\ntx:\n  skills: [ tx-artifacts , tx-sessions,, ]\n---\n# R\n")
        record, dump = self.worker("--role", "R")
        self.assertEqual(record["env"]["TX_SKILLS"], "tx-sessions,tx-artifacts")
        self.assertEqual(dump["env"]["TX_SKILLS"], "tx-sessions,tx-artifacts")
        skills = Path(record["cwd"]) / CLAUDE_SKILLS_DIR
        self.assertTrue((skills / "tx-sessions").is_symlink())
        self.assertTrue((skills / "tx-artifacts").is_symlink())

    def test_t_role_07_grant_line_edges(self):
        for name in ("a", "b"):
            self.write_skill(name)
        variants = [
            ("empty", "---\ntx:\n  skills: []\n---\n# R\n", "tx-sessions"),
            ("nofm", "# R\n", "tx-sessions"),
            ("twolines", "---\ntx:\n  skills: [a]  \n  skills: [b]\n---\n# R\n", "tx-sessions,a"),
            ("nobrackets", "---\ntx:\n  skills: a, b\n---\n# R\n", "tx-sessions"),
        ]
        for index, (label, text, expected) in enumerate(variants, start=1):
            with self.subTest(variant=label):
                self.write_role("R", text)
                record, _ = self.worker("--role", "R", name=f"w{index}")
                self.assertEqual(record["env"]["TX_SKILLS"], expected)

    # ----- T-ROLE-08 -----------------------------------------------------------------------

    def test_t_role_08_skill_directory_resolution_user_override(self):
        self.write_skill("foo", "---\nname: foo\ndescription: does foo\n---\nbody")
        self.write_skill("foo", "---\nname: foo\ndescription: does foo\n---\nbody", user=True)
        record, _ = self.worker("--cmd", "claude", "--env", "TX_SKILLS=foo", name="w1")
        link = Path(record["cwd"]) / CLAUDE_SKILLS_DIR / "foo"
        self.assertEqual(link.readlink(), self.home.user_agents_dir / "skills" / "foo")
        (self.home.user_agents_dir / "skills" / "foo" / "SKILL.md").unlink()
        (self.home.user_agents_dir / "skills" / "foo").rmdir()
        record, _ = self.worker("--cmd", "claude", "--env", "TX_SKILLS=foo", name="w2")
        link = Path(record["cwd"]) / CLAUDE_SKILLS_DIR / "foo"
        self.assertEqual(link.readlink(), self.home.agents / "skills" / "foo")

    # ----- T-ROLE-09 -----------------------------------------------------------------------

    def test_t_role_09_skill_validation_errors(self):
        (self.home.agents / "skills" / "nofile").mkdir()
        self.write_skill("bad", "---\nname: other\ndescription: x\n---\n")
        self.write_skill("nodesc", "---\nname: nodesc\n---\n")
        skills_roots = f"{self.home.user_agents_dir / 'skills'} or {self.home.agents / 'skills'}"
        expectations = {
            "nofile": f"unknown skill 'nofile' (no SKILL.md under {skills_roots})",
            "bad": "skill 'bad': frontmatter name 'other' must equal the directory name "
            "(the engines' discovery contract)",
            "nodesc": "skill 'nodesc': frontmatter needs a description — it is the only part "
            "always in an agent's context, and the entire routing signal",
        }
        for name, message in expectations.items():
            with self.subTest(skill=name):
                self.write_role("R", f"---\ntx:\n  skills: [{name}]\n---\n# R\n")
                self.assert_role_error(self.spawn("--role", "R"), message)

    def test_t_role_09_frontmatter_edges(self):
        self.write_skill("nofm", "# just a body\n")
        self.write_skill("unclosed", "---\nname: unclosed\ndescription: d\n")
        self.write_skill("indented", "---\n  name: indented\n  description: d\n---\n")
        self.write_skill("emptydesc", "---\nname: emptydesc\ndescription:\n---\n")
        expectations = {
            "nofm": "skill 'nofm': frontmatter name '' must equal the directory name "
            "(the engines' discovery contract)",
            "unclosed": f"{self.home.agents / 'skills' / 'unclosed' / 'SKILL.md'}: unclosed frontmatter block",
            "indented": "skill 'indented': frontmatter name '' must equal the directory name "
            "(the engines' discovery contract)",
            "emptydesc": "skill 'emptydesc': frontmatter needs a description — it is the only part "
            "always in an agent's context, and the entire routing signal",
        }
        for name, message in expectations.items():
            with self.subTest(skill=name):
                self.write_role("R", f"---\ntx:\n  skills: [{name}]\n---\n# R\n")
                self.assert_role_error(self.spawn("--role", "R"), message)

    def test_t_role_09_parity_hand_grant_fails_at_link_time(self):
        (self.home.agents / "skills" / "nofile").mkdir()
        result = self.spawn("--cmd", "claude", "--env", "TX_SKILLS=nofile")
        self.assert_link_failure(result, "unknown skill 'nofile'")

    @expected_failure_on_python
    def test_t_role_09_fixed_hand_grant_link_failure_has_no_traceback(self):
        (self.home.agents / "skills" / "nofile").mkdir()
        result = self.spawn("--cmd", "claude", "--env", "TX_SKILLS=nofile")
        self.assert_link_failure(result, "unknown skill 'nofile'")
        self.assertNotIn("Traceback", result.err)

    # ----- T-ROLE-10 -----------------------------------------------------------------------

    def test_t_role_10_role_skill_union(self):
        self.write_skill("tx-artifacts")
        self.write_role("DEV", "---\ntx:\n  skills: [tx-artifacts, tx-sessions]\n---\n# Dev\n")
        record, dump = self.worker("--role", "DEV")
        self.assertEqual(record["env"]["TX_SKILLS"], "tx-sessions,tx-artifacts")
        self.assertEqual(dump["env"]["TX_SKILLS"], "tx-sessions,tx-artifacts")
        skills = Path(record["cwd"]) / CLAUDE_SKILLS_DIR
        self.assertEqual(
            skills.joinpath("tx-sessions").readlink(), self.home.agents / "skills" / "tx-sessions"
        )
        self.assertEqual(
            skills.joinpath("tx-artifacts").readlink(), self.home.agents / "skills" / "tx-artifacts"
        )

    def test_t_role_10_ghost_grant_fails_spawn(self):
        self.write_role("DEV", "---\ntx:\n  skills: [ghost]\n---\n# Dev\n")
        self.assert_role_error(
            self.spawn("--role", "DEV"),
            f"unknown skill 'ghost' (no SKILL.md under {self.home.user_agents_dir / 'skills'} "
            f"or {self.home.agents / 'skills'})",
        )

    # ----- T-ROLE-11 -----------------------------------------------------------------------

    def test_t_role_11_granted_skills_env_semantics(self):
        self.write_skill("a")
        self.write_skill("b")
        record, _ = self.worker("--cmd", "claude", name="w1")
        self.assertNotIn("TX_SKILLS", record["env"])
        self.assertTrue((Path(record["cwd"]) / CLAUDE_SKILLS_DIR / "tx-sessions").is_symlink())

        record, _ = self.worker("--cmd", "claude", "--env", "TX_SKILLS=", name="w2")
        self.assertEqual(record["env"]["TX_SKILLS"], "")
        self.assertFalse((Path(record["cwd"]) / ".claude").exists())

        record, _ = self.worker("--cmd", "claude", "--env", "TX_SKILLS=a,,b", name="w3")
        self.assertEqual(record["env"]["TX_SKILLS"], "a,,b")
        skills = Path(record["cwd"]) / CLAUDE_SKILLS_DIR
        self.assertEqual(sorted(entry.name for entry in skills.iterdir()), ["a", "b"])
        self.assertTrue((skills / "a").is_symlink())
        self.assertTrue((skills / "b").is_symlink())

    def test_t_role_11_empty_grant_writes_no_exclude_line(self):
        exclude = self.exclude_path()
        before = exclude.read_text() if exclude.is_file() else None
        self.worker("--cmd", "claude", "--env", "TX_SKILLS=")
        after = exclude.read_text() if exclude.is_file() else None
        self.assertEqual(after, before)
        self.assertNotIn(f"{CLAUDE_SKILLS_DIR}/", (after or "").splitlines())

    def test_t_role_11_parity_unvalidated_list_fails_at_link_time(self):
        result = self.spawn("--cmd", "claude", "--env", "TX_SKILLS=ghost")
        self.assert_link_failure(result, "unknown skill 'ghost'")

    @expected_failure_on_python
    def test_t_role_11_fixed_unvalidated_list_link_failure_has_no_traceback(self):
        result = self.spawn("--cmd", "claude", "--env", "TX_SKILLS=ghost")
        self.assert_link_failure(result, "unknown skill 'ghost'")
        self.assertNotIn("Traceback", result.err)

    # ----- T-ROLE-12 -----------------------------------------------------------------------

    def test_t_role_12_link_skills_creates_symlinks_and_git_exclude(self):
        self.write_skill("a")
        self.write_skill("b")
        record, _ = self.worker("--cmd", "claude", "--env", "TX_SKILLS=a,b")
        worktree = Path(record["cwd"])
        self.assertTrue((worktree / ".git").is_file())
        skills = worktree / CLAUDE_SKILLS_DIR
        self.assertTrue((skills / "a").is_symlink())
        self.assertTrue((skills / "b").is_symlink())
        self.assertEqual((skills / "a").readlink(), self.home.agents / "skills" / "a")
        self.assertEqual((skills / "b").readlink(), self.home.agents / "skills" / "b")
        self.assertIn(f"{CLAUDE_SKILLS_DIR}/", self.exclude_path().read_text().splitlines())

    def test_t_role_12_codex_links_under_agents_skills(self):
        self.write_role("DEV", "# Dev\n")
        record, _ = self.worker("--engine", "codex", "--role", "DEV", engine="codex")
        skills = Path(record["cwd"]) / CODEX_SKILLS_DIR
        self.assertEqual(
            (skills / "tx-sessions").readlink(), self.home.agents / "skills" / "tx-sessions"
        )
        self.assertFalse((Path(record["cwd"]) / ".claude").exists())
        lines = self.exclude_path().read_text().splitlines()
        self.assertIn(f"{CODEX_SKILLS_DIR}/", lines)
        self.assertNotIn(f"{CLAUDE_SKILLS_DIR}/", lines)

    # ----- T-ROLE-13 -----------------------------------------------------------------------

    def commit_skill_links(self) -> None:
        """Commit `.claude/skills/{a,b}` symlinks into the H4 repo so git checks them out into
        every worktree before tx links."""
        skills = self.git.path / CLAUDE_SKILLS_DIR
        skills.mkdir(parents=True)
        for name in ("a", "b"):
            (skills / name).symlink_to(self.home.agents / "skills" / name)
        self.git.git("add", "-A")
        self.git.git("commit", "-q", "-m", "committed skill links")

    def test_t_role_13_link_skills_idempotent_and_repoints(self):
        self.write_skill("a")
        self.write_skill("b")
        self.commit_skill_links()
        (self.home.user_agents_dir / "skills").mkdir()
        (self.home.agents / "skills" / "a").rename(self.home.user_agents_dir / "skills" / "a")
        for name in ("w1", "w2"):
            record, _ = self.worker("--cmd", "claude", "--env", "TX_SKILLS=a,b", name=name)
            skills = Path(record["cwd"]) / CLAUDE_SKILLS_DIR
            self.assertEqual((skills / "a").readlink(), self.home.user_agents_dir / "skills" / "a")
            self.assertEqual((skills / "b").readlink(), self.home.agents / "skills" / "b")
        lines = self.exclude_path().read_text().splitlines()
        self.assertEqual(lines.count(f"{CLAUDE_SKILLS_DIR}/"), 1)

    def commit_regular_skill_dir(self) -> None:
        directory = self.git.path / CLAUDE_SKILLS_DIR / "b"
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(skill_text("b"))
        self.git.git("add", "-A")
        self.git.git("commit", "-q", "-m", "committed regular skill dir")

    def test_t_role_13_parity_committed_regular_dir_fails_spawn(self):
        self.write_skill("b")
        self.commit_regular_skill_dir()
        result = self.spawn("--cmd", "claude", "--env", "TX_SKILLS=b")
        self.assert_link_failure(result, f"{CLAUDE_SKILLS_DIR}/b")

    @expected_failure_on_python
    def test_t_role_13_fixed_committed_regular_dir_failure_has_no_traceback(self):
        self.write_skill("b")
        self.commit_regular_skill_dir()
        result = self.spawn("--cmd", "claude", "--env", "TX_SKILLS=b")
        self.assert_link_failure(result, f"{CLAUDE_SKILLS_DIR}/b")
        self.assertNotIn("Traceback", result.err)

    # ----- T-ROLE-14 -----------------------------------------------------------------------

    def test_t_role_14_exclude_append_rules(self):
        variants = [
            ("a", None, f"{CLAUDE_SKILLS_DIR}/\n"),
            ("b", "foo\n", f"foo\n{CLAUDE_SKILLS_DIR}/\n"),
            ("c", "foo", f"foo\n{CLAUDE_SKILLS_DIR}/\n"),
            ("d", f"foo\n{CLAUDE_SKILLS_DIR}/\nbar\n", f"foo\n{CLAUDE_SKILLS_DIR}/\nbar\n"),
        ]
        for label, before, expected in variants:
            with self.subTest(variant=label):
                repo = GitFixture(self.root, name=f"repo-{label}")
                exclude = self.exclude_path(repo)
                if before is None:
                    exclude.unlink()
                else:
                    exclude.write_text(before)
                self.spawned(self.spawn("--cmd", "claude", name=f"w{label}", cwd=repo.path))
                self.assertEqual(exclude.read_text(), expected)

    def test_t_role_14_linked_worktree_cwd_writes_main_repo_exclude(self):
        linked = self.git.as_linked_worktree()
        exclude = self.exclude_path()
        exclude.write_text("foo\n")
        record = self.spawned(self.spawn("--cmd", "claude", cwd=linked))
        self.assertEqual(exclude.read_text(), f"foo\n{CLAUDE_SKILLS_DIR}/\n")
        self.assertFalse((linked / ".git" / "info" / "exclude").exists())
        self.assertTrue((Path(record["cwd"]) / CLAUDE_SKILLS_DIR / "tx-sessions").is_symlink())
