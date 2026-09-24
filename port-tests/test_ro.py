"""T-RO — `lib/tx/read_only.py` through `tx spawn --engine claude --read-only` (spec 02).

D13: the kit's fake `bwrap` (first on PATH) dumps its argv to `$FAKE_OUT` and execs the command
after `--`, so the boundary math is read off the dump; the real sandbox is a hand-run smoke check.
T-RO-03 (sandbox-exec) is Darwin-only and skips elsewhere; T-RO-04 is DROPPED.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

from txkit import GitFixture, Result, TxCase, platform_only

PLAIN_COMMON = "# COMMON\n\nBe concise.\n"
READ_ONLY_COMMAND = (
    "claude --effort high --no-chrome --allowedTools Bash --disallowedTools Edit Write NotebookEdit "
    "--permission-mode dontAsk --setting-sources user"
)
READ_ONLY_ARGV = READ_ONLY_COMMAND.split()
NO_BWRAP = (
    "tx spawn: could not enforce read-only process sandbox: Linux read-only sessions require "
    "bubblewrap (bwrap)\n"
)
NO_SANDBOX_EXEC = "tx spawn: could not enforce read-only process sandbox: macOS sandbox-exec is unavailable\n"

# A fake `sandbox-exec` (T-RO-03): dump argv + env, then exec the command after `-p <profile>`.
SANDBOX_EXEC_FAKE = f'''#!{sys.executable}
import json, os, sys
out_dir = os.environ["FAKE_OUT"]
key = os.environ.get("TX_SESSION_ID") or str(os.getpid())
final = os.path.join(out_dir, f"sandbox-exec-{{key}}.json")
with open(final + ".tmp", "w") as handle:
    json.dump({{"name": "sandbox-exec", "argv": sys.argv, "env": dict(os.environ), "cwd": os.getcwd()}}, handle)
os.replace(final + ".tmp", final)
inner = sys.argv[sys.argv.index("-p") + 2:]
os.execvp(inner[0], inner)
'''


def repository_key(checkout: Path) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", checkout.name).strip("-._") or "repository"
    digest = hashlib.sha256(str((checkout / ".git").resolve()).encode()).hexdigest()[:8]
    return f"{slug}-{digest}"


def ro_bind_pairs(argv: list[str]) -> list[tuple[str, str]]:
    return [(argv[index + 1], argv[index + 2]) for index, token in enumerate(argv) if token == "--ro-bind"]


def path_without_binary(case: TxCase, binary: str) -> str:
    """The kit's PATH (wrapper, fakes, helper copies first) with the inherited tail replaced by ONE
    symlink dir holding every executable of the inherited PATH except `binary` — first hit wins,
    as PATH lookup would. `shutil.which(binary)` then fails wherever the host keeps it
    (`/usr/bin/bwrap` on an apt host, linuxbrew, `/usr/bin/sandbox-exec`) while bash, git, python
    and everything else stay reachable; dropping whole PATH dirs would take those with it. (Kit
    candidate — T-SPAWN-16 imports it from here.)"""
    tools = case.root / f"path-without-{binary}"
    tools.mkdir()
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory or not os.path.isdir(directory):
            continue
        for entry in os.scandir(directory):
            link = tools / entry.name
            if entry.name == binary or os.path.lexists(link) or entry.is_dir() or not os.access(entry.path, os.X_OK):
                continue
            link.symlink_to(entry.path)
    return os.pathsep.join([str(case.tmux.bin_dir), str(case.fakes.bin_dir), str(case.fakes.helpers_dir), str(tools)])


class TestRo(TxCase):
    home_options = {"link_agents": False}

    def setUp(self) -> None:
        super().setUp()
        self.home.agents.mkdir()
        (self.home.agents / "COMMON.md").write_text(PLAIN_COMMON)

    def spawn_read_only(self, name: str, cwd: Path, env: dict[str, str | None] | None = None) -> Result:
        return self.tx(
            ["spawn", name, "--tag", "t", "--engine", "claude", "--cwd", str(cwd), "--read-only"], env=env
        )

    def record(self, name: str) -> dict:
        return json.loads(self.tx(["show", name]).out)

    def key_dir(self, checkout: GitFixture) -> Path:
        return self.home.worktrees_dir / repository_key(checkout.path)

    def assert_refused_and_cleaned(self, result: Result, message: str, checkout: GitFixture) -> None:
        self.assertEqual(result.code, 1)
        self.assertEqual(result.err, message)
        self.assertEqual(result.out, "")
        self.assertEqual(
            [os.path.realpath(path) for path in checkout.worktrees()], [os.path.realpath(checkout.path)]
        )
        self.assertFalse((self.key_dir(checkout) / "r--w").exists())
        self.assertEqual(list(self.home.sessions_dir.iterdir()), [])
        self.assertEqual(self.log_lines(), [])
        self.assertEqual(self.tmux.sessions(), [])

    # ----- T-RO-01 boundary minimisation ---------------------------------------------------

    @platform_only("linux")
    def test_t_ro_01_boundary_minimisation(self):
        checkout = GitFixture(self.root / "x", name="r")
        checkout.git("worktree", "add", "--detach", str(checkout.path / "sub"), "HEAD")
        self.assertEqual(
            self.tx(["spawn", "other", "--tag", "t", "--engine", "claude", "--cwd", str(checkout.path)]).code, 0
        )
        result = self.spawn_read_only("w", checkout.path)
        self.assertEqual(result.code, 0, result.err)
        record = self.record("w")
        pairs = ro_bind_pairs(self.fakes.wait_dump("bwrap", record["id"])["argv"])
        repo = str(checkout.path.resolve())
        key_dir = str(self.key_dir(checkout).resolve())
        self.assertEqual(len(pairs), 2)
        self.assertEqual(set(pairs), {(repo, repo), (key_dir, key_dir)})
        depths = [len(Path(source).parts) for source, _ in pairs]
        self.assertEqual(depths, sorted(depths))  # shallower first
        sources = [source for source, _ in pairs]
        for dropped_child in (
            checkout.path / "sub",
            checkout.path / ".git",
            self.key_dir(checkout) / "r--other",
            self.key_dir(checkout) / "r--w",
        ):
            self.assertNotIn(str(dropped_child.resolve()), sources)

    # ----- T-RO-02 Linux bwrap argv --------------------------------------------------------

    @platform_only("linux")
    def test_t_ro_02_linux_bwrap_argv(self):
        checkout = GitFixture(self.root, name="r")
        key_dir = self.key_dir(checkout)
        worktree = key_dir / "r--w"
        bwrap = str(self.fakes.bin_dir / "bwrap")
        result = self.spawn_read_only("w", checkout.path)
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, f"Spawned 'w' (cwd={worktree}, tag=t)\n")
        record = self.record("w")
        dump = self.fakes.wait_dump("bwrap", record["id"])
        self.assertEqual(
            dump["argv"],
            [
                bwrap, "--bind", "/", "/",
                "--ro-bind", str(checkout.path), str(checkout.path),
                "--ro-bind", str(key_dir), str(key_dir),
                "--chdir", str(worktree), "--", *READ_ONLY_ARGV,
            ],
        )
        # tmux 3.4 renders the (space-carrying) start command wrapped in double quotes.
        start_command = self.tmux.display(record["id"], "#{pane_start_command}")
        self.assertRegex(start_command, rf'^"?{re.escape(bwrap)} --bind / / --ro-bind ')
        self.assertEqual(record["cmd"], READ_ONLY_COMMAND)
        self.assertEqual(record["env"], {"TX_READ_ONLY": "1"})
        engine = self.fakes.wait_dump("claude", record["id"])
        self.assertEqual(engine["env"]["TX_READ_ONLY"], "1")
        self.assertNotIn("TX_REQUIRE_WORKTREE", engine["env"])
        self.assertEqual(engine["env"]["PWD"], str(worktree))

    @platform_only("linux")
    def test_t_ro_02_no_bwrap_refused_and_worktree_removed(self):
        checkout = GitFixture(self.root, name="r")
        self.fakes.remove("bwrap")
        result = self.spawn_read_only("w", checkout.path, env={"PATH": path_without_binary(self, "bwrap")})
        self.assert_refused_and_cleaned(result, NO_BWRAP, checkout)

    # ----- T-RO-03 macOS sandbox-exec profile (D13: run by hand on Darwin) ------------------

    @platform_only("darwin")
    def test_t_ro_03_macos_sandbox_exec_profile(self):
        checkout = GitFixture(self.root, name="r")
        key_dir = self.key_dir(checkout)
        worktree = key_dir / "r--w"
        sandbox_exec = str(self.fakes.write_script("sandbox-exec", SANDBOX_EXEC_FAKE))
        result = self.spawn_read_only("w", checkout.path)
        self.assertEqual(result.code, 0, result.err)
        record = self.record("w")
        dump = self.fakes.wait_dump("sandbox-exec", record["id"])
        denied = " ".join(
            f"(subpath {json.dumps(str(boundary.resolve()))})" for boundary in (checkout.path, key_dir)
        )
        profile = f"(version 1)\n(allow default)\n(deny file-write* {denied})"
        self.assertEqual(dump["argv"], [sandbox_exec, "-p", profile, *READ_ONLY_ARGV])
        start_command = self.tmux.display(record["id"], "#{pane_start_command}")
        self.assertRegex(start_command, rf'^"?{re.escape(sandbox_exec)} -p ')
        self.assertEqual(record["cmd"], READ_ONLY_COMMAND)
        self.assertEqual(record["env"], {"TX_READ_ONLY": "1"})
        self.assertEqual(os.path.realpath(self.fakes.wait_dump("claude", record["id"])["cwd"]), os.path.realpath(worktree))

    @platform_only("darwin")
    def test_t_ro_03_no_sandbox_exec_refused_and_worktree_removed(self):
        checkout = GitFixture(self.root, name="r")
        result = self.spawn_read_only("w", checkout.path, env={"PATH": path_without_binary(self, "sandbox-exec")})
        self.assert_refused_and_cleaned(result, NO_SANDBOX_EXEC, checkout)
