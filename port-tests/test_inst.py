"""INST — install / uninstall / engine wiring (spec §INST, T-INST-01..52).

The bash scripts are the unit under test; they are run as subprocesses under the audit-note harness
(`Installer`): `HOME=<root>/user-home`, `TX_IDE_HOME=<root>/home`, `CLAUDE_CONFIG_DIR`, `CODEX_HOME`
under the same root, `$HOME/.local/bin` already on PATH (so the trailer never re-execs a login
shell), the fake `brew` + the private-socket `tmux` wrapper first on PATH, and only the engine CLIs a
case asks for (`agy` is always off PATH — antigravity is deferred, D3; `codex` is off unless the
case turns it on so `install.sh` drives claude alone by default).
"""

from __future__ import annotations

import glob
import json
import os
import re
import shlex
import subprocess
import sys
import time
import unittest
import uuid
from pathlib import Path

from txkit import (
    HOME_DIRS,
    REPO,
    TX_ENGINE_SETUP,
    TX_INSTALLER,
    TX_UNINSTALLER,
    Result,
    TxCase,
    expected_failure_on_python,
    python_reference_only,
    requires_tmux,
    scrubbed_env,
    strip_ansi,
    tmux_version,
)

# Entry points (H9 / D16): never the repo's scripts by path — a port names its own installer,
# uninstaller and engine-setup dir, and the defaults are the reference's files.
INSTALL = Path(TX_INSTALLER)
UNINSTALL = Path(TX_UNINSTALLER)
INSTALL_SH = Path(TX_ENGINE_SETUP)
ENGINES_DIR = INSTALL_SH.parent
CLAUDE_SH = ENGINES_DIR / "claude.sh"
CODEX_SH = ENGINES_DIR / "codex.sh"
# Only for the expected strings baked into shims / markers (the reference's exec line).
LIB_DIR = REPO / "lib"

CLI_TOOLS = ("tx", "tx-assistant", "tmux-pane-session-name", "tmux-system-resources")
RC_FILES = (".zshrc", ".bash_profile", ".bashrc")
STATE_DIRS = ("sessions", "history", "user-agents")
DEFAULT_TMUX_CONF = REPO / "tmux" / "tmux.conf"

PATH_BLOCK = (
    "\n# === BEGIN tx-ide PATH ===\n"
    'case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac\n'
    "# === END tx-ide PATH ===\n"
)

CLAUDE_SHIMS = {
    "start": ("session-start", True),
    "pre": ("prompt-submit", True),
    "work": ("working", False),
    "post": ("stop", False),
    "notify": ("notification", True),
    "end": ("session-end", False),
}
CODEX_SHIMS = {"start": "session-start", "pre": "prompt-submit", "work": "working", "post": "stop"}

CLAUDE_EVENTS = {
    "SessionStart": "start",
    "UserPromptSubmit": "pre",
    "PreToolUse": "work",
    "PostToolUse": "work",
    "PostToolUseFailure": "work",
    "SubagentStart": "work",
    "PreCompact": "work",
    "Stop": "post",
    "StopFailure": "post",
    "PermissionRequest": "post",
    "Notification": "notify",
    "SessionEnd": "end",
}
CODEX_EVENTS = {
    "SessionStart": "start",
    "UserPromptSubmit": "pre",
    "PreToolUse": "work",
    "PostToolUse": "work",
    "PreCompact": "work",
    "PostCompact": "work",
    "SubagentStart": "work",
    "Stop": "post",
    "PermissionRequest": "post",
}

CONTEXT_PROFILE = {
    "disableWorkflows": True,
    "disableArtifact": True,
    "includeGitInstructions": False,
    "skillOverrides": {
        "artifact-design": "off",
        "artifact-diagramming": "off",
        "artifact-capabilities": "off",
        "claude-in-chrome": "off",
        "init": "off",
        "fewer-permission-prompts": "off",
        "code-review": "user-invocable-only",
        "security-review": "user-invocable-only",
        "simplify": "user-invocable-only",
        "dataviz": "user-invocable-only",
        "claude-api": "user-invocable-only",
        "update-config": "user-invocable-only",
        "keybindings-help": "user-invocable-only",
        "schedule": "user-invocable-only",
        "loop": "user-invocable-only",
        "run": "user-invocable-only",
    },
    "permissions.deny": ["ReportFindings", "ListAgents"],
}


def backups(path: Path) -> list[Path]:
    """Every `<path>.bak.<STAMP>` beside `path`, oldest first."""
    return sorted(Path(candidate) for candidate in glob.glob(f"{path}.bak.*"))


def tmux_block(shim: Path) -> str:
    """What `install::append_block` appends to `~/.tmux.conf`."""
    return f"\n# === BEGIN tx-ide ===\nsource-file {shim}\n# === END tx-ide ===\n"


# `ok` / `warn` / `fail` / `skip` print `  → <subject padded to 48> <status>`.
STATUS_LINE = re.compile(r"^  → (?P<subject>\S+) +(?P<status>\S.*)$", re.M)


def statuses(result: Result, subject: Path | str) -> list[str]:
    """Every status printed for `subject` on a `→` line, in order."""
    return [match["status"] for match in STATUS_LINE.finditer(result.out) if match["subject"] == str(subject)]


class Installer:
    """Runs `install`, `uninstall` and the `setup/engines/*.sh` scripts under the harness env."""

    def __init__(self, case: TxCase, *, shell: str = "/bin/zsh", codex: bool = False):
        self.case = case
        self.shell = shell
        self.user_home = case.home.user_home
        self.local_bin = self.user_home / ".local" / "bin"
        self.tx_home = case.home.path
        self.claude_dir = case.home.claude_config_dir
        self.codex_home = case.home.codex_home
        case.fakes.remove("agy")
        if not codex:
            case.fakes.remove("codex")
        # The fake fzf drains stdin by default; `fzf --version` in the dependency check would eat
        # the answers meant for the later [y/N] prompts.
        case.fakes.configure("fzf", read_stdin=False)
        # The pre-flight banner's `$(tx)` / `$(tx start)` are unescaped command substitutions: a
        # silent stub keeps that from reaching whatever `tx` the operator has installed.
        stub = case.fakes.bin_dir / "tx"
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)

    # paths the scripts touch
    @property
    def settings(self) -> Path:
        return self.claude_dir / "settings.json"

    @property
    def marker(self) -> Path:
        return self.tx_home / "claude-managed.json"

    @property
    def tmux_conf_shim(self) -> Path:
        return self.tx_home / "tmux.conf"

    @property
    def user_tmux_conf(self) -> Path:
        return self.user_home / ".tmux.conf"

    def rc_file(self, name: str = ".zshrc") -> Path:
        return self.user_home / name

    def claude_shim(self, basename: str) -> Path:
        return self.tx_home / "hooks" / "claude" / f"{basename}.sh"

    def codex_shim(self, basename: str) -> Path:
        return self.tx_home / "hooks" / "codex" / f"{basename}.sh"

    def env(self, extra: dict[str, str | None] | None = None) -> dict[str, str]:
        """The scrubbed env with a RESTRICTED PATH: wrapper + fakes + `$HOME/.local/bin` + the
        system dirs (+ the test interpreter's dir for `python3.14`). The operator's own engine CLIs
        must stay invisible so `install.sh` drives exactly the fakes a case put on PATH."""
        env = scrubbed_env(self.case.home, self.case.tmux, self.case.fakes, {"SHELL": self.shell})
        env["PATH"] = os.pathsep.join(
            [
                str(self.case.tmux.bin_dir),
                str(self.case.fakes.bin_dir),
                str(self.local_bin),
                os.path.dirname(sys.executable),
                "/usr/local/bin",
                "/usr/bin",
                "/bin",
            ]
        )
        for key, value in (extra or {}).items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    def run(
        self,
        script: Path,
        *args: str,
        stdin: str = "y\n",
        env: dict[str, str | None] | None = None,
        cwd: Path | None = None,
        timeout: float = 120.0,
    ) -> Result:
        completed = subprocess.run(
            [str(script), *args],
            input=stdin,
            capture_output=True,
            text=True,
            env=self.env(env),
            cwd=str(cwd or self.case.root),
            timeout=timeout,
        )
        return Result(
            code=completed.returncode,
            out=strip_ansi(completed.stdout),
            err=strip_ansi(completed.stderr),
            raw_out=completed.stdout,
            raw_err=completed.stderr,
        )

    def install(self, stdin: str = "y\n", **kwargs) -> Result:
        return self.run(INSTALL, stdin=stdin, **kwargs)

    def uninstall(self, *args: str, stdin: str = "y\n", **kwargs) -> Result:
        return self.run(UNINSTALL, *args, stdin=stdin, **kwargs)

    def claude_sh(self, *args: str, **kwargs) -> Result:
        return self.run(CLAUDE_SH, *args, stdin="", **kwargs)

    def codex_sh(self, *args: str, **kwargs) -> Result:
        return self.run(CODEX_SH, *args, stdin="", **kwargs)

    def install_sh(self, *args: str, **kwargs) -> Result:
        return self.run(INSTALL_SH, *args, stdin="", **kwargs)

    def write_settings(self, data: dict | str, path: Path | None = None) -> Path:
        target = path or self.settings
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(data if isinstance(data, str) else json.dumps(data, indent=2) + "\n")
        return target

    def read_json(self, path: Path) -> dict:
        return json.loads(path.read_text())

    def claude_shim_text(self, event: str, keep_stdin: bool, *, home: Path | None = None, python: str = "python3.14") -> str:
        drain = (
            "# stdin left connected — tx hook reads the JSON payload" if keep_stdin else "cat >/dev/null"
        )
        return (
            "#!/bin/bash\n"
            "# tx-ide hook shim — GENERATED by setup/engines/claude.sh (stage S2). Do NOT edit; re-run the\n"
            "# installer to regenerate. C9: $TX_IDE_HOME and the package lib are baked in as literals because\n"
            "# Claude runs hooks with a minimal env (no shell rc). Drain (or pass) the payload, then drive state.\n"
            f"{drain}\n"
            f'exec env TX_IDE_HOME="{home or self.tx_home}" PYTHONPATH="{LIB_DIR}" "{python}" -m tx hook {event}\n'
        )

    def tmux_session_closed(self, *, home: Path | None = None) -> str:
        return (
            f'run-shell -b "env TX_IDE_HOME={home or self.tx_home} PYTHONPATH={LIB_DIR} python3.14 -m tx hook session-closed"'
        )


def snapshot(root: Path) -> dict[str, bytes]:
    """`{relative path: bytes}` for every file under `root` (symlinks as their target string)."""
    tree: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            tree[relative] = os.readlink(path).encode()
        elif path.is_file():
            tree[relative] = path.read_bytes()
    return tree


# claude.sh's server-liveness probe is `tmux info`; on tmux 3.4 `show-messages` lacks
# CMD_CLIENT_CANFAIL, so from an unattached client it always fails with `no current client` and the
# hook step degrades to `no tmux server — will apply on next start`. tmux ≥ 3.5 adds the flag (the
# CI floor is 3.6, D13), so the hook-applied cases are version-gated like Q21.
hook_step_reachable = requires_tmux(min="3.6")

CODEX_BLOCK = (
    "# === BEGIN tx-ide (codex) ===\n"
    "[tui]\n"
    'status_line = ["model", "reasoning", "project-name", "context-used"]\n'
    "status_line_use_colors = true\n"
    "# === END tx-ide (codex) ===\n"
)

STAMP_RE = re.compile(r"\.bak\.(\d{14})$")


def engines_codex_shim_text(home: Path, event: str, *, python: str = "python3.14") -> str:
    """The exact 6-line body `codex.sh::write_shim` writes."""
    return (
        "#!/bin/bash\n"
        "# tx-ide Codex hook shim — GENERATED by setup/engines/codex.sh. Do NOT edit; re-run the installer to\n"
        "# regenerate. $TX_IDE_HOME and the package lib are baked in as literals because Codex runs hooks with\n"
        "# a minimal env. stdin is left CONNECTED so `tx hook` can read session_id/transcript_path off the\n"
        "# JSON payload (the universal capture path); --engine codex selects the Codex adapter.\n"
        f'exec env TX_IDE_HOME="{home}" PYTHONPATH="{LIB_DIR}" "{python}" -m tx hook {event} --engine codex\n'
    )


def engines_codex_hooks_json(home: Path) -> dict:
    return {
        "hooks": {
            event: [{"hooks": [{"type": "command", "command": str(home / "hooks" / "codex" / f"{shim}.sh")}]}]
            for event, shim in CODEX_EVENTS.items()
        }
    }


def engines_plan_line(event: str, action: str) -> str:
    """A `run_settings_py` plan line: `  → <event padded to 18> <action>`."""
    return f"  → {event:<18} {action}"


def engines_status_line(label: str, status: str) -> str:
    """An `ok` / `warn` line: `  → <label padded to 46> <status>`."""
    return f"  → {label:<46} {status}"


def engines_stamp(backup: Path) -> str:
    return STAMP_RE.search(backup.name).group(1)


def engines_executable(path: Path) -> bool:
    return path.stat().st_mode & 0o111 == 0o111


class TestInst(TxCase):
    home_options = {"skeleton": False, "link_agents": False, "hooks": ()}

    def setUp(self) -> None:
        super().setUp()
        self.installer = Installer(self)

    # ----- helpers ---------------------------------------------------------------------------

    def full_install(self) -> Result:
        """The T-INST-14 fixture: fake `claude` on PATH, settings.json with a foreign statusLine,
        `~/.zshrc` + `~/.tmux.conf` as T-INST-05 / T-INST-11, one `install` (decline the replace)."""
        installer = self.installer
        installer.write_settings(
            {"theme": "dark", "statusLine": {"type": "command", "command": "old.sh", "padding": 0}}
        )
        installer.rc_file().write_text("alias x=y\n")
        installer.user_tmux_conf.write_text("set -g mouse on\n")
        result = installer.install(stdin="y\nn\n")
        self.assertEqual(result.code, 0, result.err)
        return result

    def state_fixture(self) -> None:
        """T-INST-21: three state dirs with a file each + `config.json`, `log.jsonl`, `tmux.conf`."""
        home = self.installer.tx_home
        for state in STATE_DIRS:
            (home / state).mkdir(parents=True)
            (home / state / "keep.txt").write_text(f"{state}\n")
        (home / "config.json").write_text("{}\n")
        (home / "log.jsonl").write_text('{"type": "spawn"}\n')
        self.installer.tmux_conf_shim.write_text("run-shell x\n")

    def shim_content(self) -> str:
        repo = REPO
        return (
            "# Written by tx-ide install — do not edit, re-run install to update.\n"
            f"# Repo: {repo}\n"
            f"run-shell '{repo}/tmux/tx-ide.tmux'\n"
        )

    def assert_in_order(self, result: Result, *snippets: str) -> None:
        """Each snippet occurs in `result.out`, and after the previous one."""
        position = 0
        for snippet in snippets:
            found = result.out.find(snippet, position)
            self.assertNotEqual(found, -1, f"{snippet!r} not found after offset {position} in:\n{result.out}")
            position = found + len(snippet)

    def assert_status(self, result: Result, subject: Path | str, status: str) -> None:
        self.assertEqual(statuses(result, subject), [status], result.out)

    def assert_status_line(self, result: Result, subject: str, status: str) -> None:
        """`assert_status` for a subject that itself contains spaces (`shell rc_file`, `claude.sh uninstall`)."""
        self.assertRegex(result.out, re.compile(rf"^  → {re.escape(subject)}\s+{re.escape(status)}$", re.M))

    def section(self, result: Result, header: str, next_header: str) -> str:
        """`result.out` between two section headers (bold lines, ANSI already stripped)."""
        lines = result.lines
        return "\n".join(lines[lines.index(header) : lines.index(next_header)])

    # ----- install: CLI symlinks -------------------------------------------------------------

    def test_t_inst_01_link_fresh_symlink(self):
        installer = self.installer
        self.assertFalse(installer.local_bin.exists())
        result = installer.install()
        self.assertEqual(result.code, 0, result.err)
        for tool in CLI_TOOLS:
            link = installer.local_bin / tool
            self.assertTrue(link.is_symlink(), link)
            self.assertEqual(os.readlink(link), str(REPO / "bin" / tool))
            self.assert_status(result, link, "linked")

    def test_t_inst_02_link_already_linked_is_a_no_op(self):
        installer = self.installer
        installer.local_bin.mkdir(parents=True)
        link = installer.local_bin / "tx"
        link.symlink_to(REPO / "bin" / "tx")
        before = os.lstat(link)
        result = installer.install()
        self.assertEqual(result.code, 0, result.err)
        after = os.lstat(link)
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))
        self.assertEqual(os.readlink(link), str(REPO / "bin" / "tx"))
        self.assert_status(result, link, "already linked")

    def test_t_inst_03_link_stale_symlink_is_repointed(self):
        installer = self.installer
        installer.local_bin.mkdir(parents=True)
        link = installer.local_bin / "tx"
        link.symlink_to("/old/repo/bin/tx")
        result = installer.install()
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(os.readlink(link), str(REPO / "bin" / "tx"))
        self.assert_status(result, link, "linked")

    def test_t_inst_04_link_refuses_to_clobber_a_regular_file(self):
        installer = self.installer
        installer.local_bin.mkdir(parents=True)
        regular = installer.local_bin / "tx"
        regular.write_text("X")
        result = installer.install()
        self.assertEqual(result.code, 0, result.err)
        self.assertFalse(regular.is_symlink())
        self.assertEqual(regular.read_text(), "X")
        self.assert_status(result, regular, "exists (not a symlink) — refusing to clobber")
        # the installer continues past the refusal: the next tool is still linked
        self.assert_status(result, installer.local_bin / "tx-assistant", "linked")

    def test_t_inst_04_link_refuses_to_clobber_a_directory(self):
        installer = self.installer
        directory = installer.local_bin / "tx"
        directory.mkdir(parents=True)
        result = installer.install()
        self.assertEqual(result.code, 0, result.err)
        self.assertTrue(directory.is_dir())
        self.assertFalse(directory.is_symlink())
        self.assert_status(result, directory, "exists (not a symlink) — refusing to clobber")
        self.assert_status(result, installer.local_bin / "tx-assistant", "linked")

    # ----- install: PATH block ---------------------------------------------------------------

    def test_t_inst_05_path_block_appended_to_zshrc(self):
        installer = self.installer
        rc_file = installer.rc_file(".zshrc")
        rc_file.write_text("alias x=y\n")
        result = installer.install()
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual([backup.read_text() for backup in backups(rc_file)], ["alias x=y\n"])
        self.assertEqual(rc_file.read_text(), "alias x=y\n" + PATH_BLOCK)
        self.assertEqual(statuses(result, rc_file), [f"PATH block added (open a new shell, or: source {rc_file})"])

    def test_t_inst_05_path_block_creates_absent_zshrc(self):
        installer = self.installer
        rc_file = installer.rc_file(".zshrc")
        result = installer.install()
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(rc_file.read_text(), PATH_BLOCK)
        self.assertEqual(backups(rc_file), [])
        self.assertEqual(statuses(result, rc_file), [f"PATH block added (open a new shell, or: source {rc_file})"])

    def test_t_inst_05_path_block_bash_targets_bash_profile(self):
        installer = self.installer
        installer.shell = "/bin/bash"
        rc_file = installer.rc_file(".bash_profile")
        rc_file.write_text("alias x=y\n")
        result = installer.install()
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual([backup.read_text() for backup in backups(rc_file)], ["alias x=y\n"])
        self.assertEqual(rc_file.read_text(), "alias x=y\n" + PATH_BLOCK)
        self.assertEqual(statuses(result, rc_file), [f"PATH block added (open a new shell, or: source {rc_file})"])
        self.assertFalse(installer.rc_file(".zshrc").exists())

    def test_t_inst_05_path_block_fish_warns_and_touches_nothing(self):
        installer = self.installer
        installer.shell = "/bin/fish"
        result = installer.install()
        self.assertEqual(result.code, 0, result.err)
        self.assert_status_line(result, "shell rc", f"SHELL='/bin/fish' — add {installer.local_bin} to PATH yourself")
        for name in RC_FILES:
            self.assertFalse(installer.rc_file(name).exists(), name)

    def test_t_inst_06_path_block_already_present(self):
        installer = self.installer
        rc_file = installer.rc_file(".zshrc")
        original = "alias x=y\n# === BEGIN tx-ide PATH ===\n"
        rc_file.write_text(original)
        result = installer.install()
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(rc_file.read_text(), original)
        self.assertEqual(backups(rc_file), [])
        self.assert_status(result, rc_file, "tx-ide PATH block already present")

    def test_t_inst_06_path_block_expanded_reference_left_as_is(self):
        installer = self.installer
        rc_file = installer.rc_file(".zshrc")
        original = f"export PATH={installer.local_bin}:$PATH\n"
        rc_file.write_text(original)
        result = installer.install()
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(rc_file.read_text(), original)
        self.assertEqual(backups(rc_file), [])
        self.assert_status(result, rc_file, f"already references {installer.local_bin} (left as-is)")

    def test_t_inst_06_path_block_literal_home_reference_does_not_count(self):
        installer = self.installer
        rc_file = installer.rc_file(".zshrc")
        original = 'export PATH="$HOME/.local/bin:$PATH"\n'
        rc_file.write_text(original)
        result = installer.install()
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(rc_file.read_text(), original + PATH_BLOCK)
        self.assertEqual([backup.read_text() for backup in backups(rc_file)], [original])
        self.assertEqual(statuses(result, rc_file), [f"PATH block added (open a new shell, or: source {rc_file})"])

    # ----- install: home skeleton + agents ---------------------------------------------------

    def test_t_inst_07_init_home_skeleton(self):
        home = self.installer.tx_home
        self.assertFalse(home.exists())
        result = self.tx(["_init-home"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, f"initialized $TX_IDE_HOME skeleton at {home}\n")
        self.assertEqual(self.home.entries(), sorted(HOME_DIRS))
        for name in HOME_DIRS:
            self.assertTrue((home / name).is_dir(), name)

    def test_t_inst_07_init_home_rerun_changes_nothing(self):
        home = self.installer.tx_home
        self.tx(["_init-home"])
        before = {name: os.stat(home / name).st_mtime_ns for name in self.home.entries()}
        result = self.tx(["_init-home"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.err, "")
        self.assertEqual({name: os.stat(home / name).st_mtime_ns for name in self.home.entries()}, before)

    def test_t_inst_07_init_home_expands_tilde(self):
        expanded = self.installer.user_home / "x"
        result = self.tx(["_init-home"], env={"TX_IDE_HOME": "~/x"})
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, f"initialized $TX_IDE_HOME skeleton at {expanded}\n")
        self.assertEqual(sorted(entry.name for entry in expanded.iterdir()), sorted(HOME_DIRS))
        self.assertFalse((self.root / "~").exists())

    def test_t_inst_08_agents_symlink(self):
        installer = self.installer
        self.tx(["_init-home"])
        agents = installer.tx_home / "agents"
        result = installer.install()
        self.assertEqual(result.code, 0, result.err)
        self.assertTrue(agents.is_symlink())
        self.assertEqual(os.readlink(agents), str(REPO / "agents"))
        self.assert_status(result, agents, "linked")
        rerun = installer.install()
        self.assertEqual(rerun.code, 0, rerun.err)
        self.assertEqual(os.readlink(agents), str(REPO / "agents"))
        self.assert_status(rerun, agents, "already linked")

    def test_t_inst_08_agents_real_dir_refused(self):
        installer = self.installer
        agents = installer.tx_home / "agents"
        agents.mkdir(parents=True)
        (agents / "MINE.md").write_text("mine\n")
        result = installer.install()
        self.assertEqual(result.code, 0, result.err)
        self.assertTrue(agents.is_dir())
        self.assertFalse(agents.is_symlink())
        self.assertEqual((agents / "MINE.md").read_text(), "mine\n")
        self.assert_status(result, agents, "exists (not a symlink) — refusing to clobber")

    # ----- install: $TX_IDE_HOME/tmux.conf shim ----------------------------------------------

    def test_t_inst_09_tmux_conf_shim_written(self):
        installer = self.installer
        shim = installer.tmux_conf_shim
        result = installer.install()
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(shim.read_text(), self.shim_content())
        self.assertEqual(backups(shim), [])
        self.assert_status(result, shim, "written")

    def test_t_inst_09_tmux_conf_shim_different_content_backed_up(self):
        installer = self.installer
        shim = installer.tmux_conf_shim
        shim.parent.mkdir(parents=True)
        shim.write_text("run-shell '/old/tx-ide.tmux'\n")
        result = installer.install()
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(shim.read_text(), self.shim_content())
        self.assertEqual([backup.read_text() for backup in backups(shim)], ["run-shell '/old/tx-ide.tmux'\n"])
        self.assert_status(result, shim, "written")

    @python_reference_only
    def test_t_inst_09_parity_identical_content_rewritten_with_bak(self):
        # Q22: `$(cat)` strips the trailing newline, so `already current` is unreachable.
        installer = self.installer
        shim = installer.tmux_conf_shim
        shim.parent.mkdir(parents=True)
        shim.write_text(self.shim_content())
        result = installer.install()
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(shim.read_text(), self.shim_content())
        self.assertEqual([backup.read_text() for backup in backups(shim)], [self.shim_content()])
        self.assert_status(result, shim, "written")

    @expected_failure_on_python
    def test_t_inst_09_fixed_identical_content_already_current(self):
        installer = self.installer
        shim = installer.tmux_conf_shim
        shim.parent.mkdir(parents=True)
        shim.write_text(self.shim_content())
        result = installer.install()
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(shim.read_text(), self.shim_content())
        self.assertEqual(backups(shim), [])
        self.assert_status(result, shim, "already current")

    # ----- install: ~/.tmux.conf -------------------------------------------------------------

    def test_t_inst_10_user_tmux_conf_absent_default_installed(self):
        installer = self.installer
        tmux_conf = installer.user_tmux_conf
        result = installer.install()
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(tmux_conf.read_bytes(), DEFAULT_TMUX_CONF.read_bytes())
        self.assertEqual(backups(tmux_conf), [])
        self.assert_status(result, tmux_conf, "installed tx-ide default")

    def test_t_inst_11_user_tmux_conf_decline_replace_block_appended(self):
        installer = self.installer
        tmux_conf = installer.user_tmux_conf
        tmux_conf.write_text("set -g mouse on\n")
        result = installer.install(stdin="y\nn\n")
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual([backup.read_text() for backup in backups(tmux_conf)], ["set -g mouse on\n"])
        self.assertEqual(tmux_conf.read_text(), "set -g mouse on\n" + tmux_block(installer.tmux_conf_shim))
        self.assert_status(result, tmux_conf, "block added")
        self.assertIn(f"  backup: {backups(tmux_conf)[0]}\n", result.out)

    def test_t_inst_12_user_tmux_conf_accept_replace(self):
        installer = self.installer
        tmux_conf = installer.user_tmux_conf
        tmux_conf.write_text("set -g mouse on\n")
        result = installer.install(stdin="y\ny\n")
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual([backup.read_text() for backup in backups(tmux_conf)], ["set -g mouse on\n"])
        self.assertEqual(tmux_conf.read_bytes(), DEFAULT_TMUX_CONF.read_bytes())
        self.assert_status(result, tmux_conf, "replaced with tx-ide default")
        self.assertIn(f"  backup: {backups(tmux_conf)[0]}\n", result.out)

    def test_t_inst_13_user_tmux_conf_block_present_untouched(self):
        installer = self.installer
        tmux_conf = installer.user_tmux_conf
        original = "set -g mouse on\n# === BEGIN tx-ide ===\nsource-file /x\n# === END tx-ide ===\n"
        tmux_conf.write_text(original)
        # `read -p` prints nothing on a pipe, so "no prompt consumed" is only visible through the
        # NEXT reader: the second `y` must reach the nvim [y/N] prompt (setup/nvim.sh install links
        # $XDG_CONFIG_HOME/nvim, pinned inside the temp HOME) instead of the replace prompt.
        config_dir = installer.user_home / ".config"
        result = installer.install(stdin="y\ny\n", env={"XDG_CONFIG_HOME": str(config_dir)})
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(tmux_conf.read_text(), original)
        self.assertEqual(backups(tmux_conf), [])
        self.assert_status(result, tmux_conf, "block present")
        self.assertEqual(os.readlink(config_dir / "nvim"), str(REPO / "nvim"))

    # ----- install: statusline (C10) ---------------------------------------------------------

    def test_t_inst_14_statusline_copy_and_settings_edit(self):
        installer = self.installer
        result = self.full_install()
        home = installer.tx_home
        statusline = home / "statusline.sh"
        self.assertEqual(statusline.read_bytes(), (REPO / "claude" / "statusline.sh").read_bytes())
        self.assertTrue(os.access(statusline, os.X_OK))
        status_command = f"bash {home}/statusline.sh"
        text = installer.settings.read_text()
        settings = json.loads(text)
        self.assertEqual(settings["statusLine"], {"type": "command", "command": status_command, "padding": 0})
        self.assertEqual(settings["theme"], "dark")
        self.assertEqual(text, json.dumps(settings, indent=2) + "\n")
        newest = max(backups(installer.settings), key=lambda backup: backup.stat().st_mtime_ns)
        self.assertEqual(json.loads(newest.read_text())["statusLine"]["command"], "old.sh")
        marker = installer.read_json(installer.marker)
        self.assertEqual(marker["mode"], "installed")
        self.assertEqual(marker["statusLine_command"], status_command)
        lines = result.lines
        index = lines.index(f"  → statusLine.command → {status_command}  (marker mode=installed)")
        match = re.fullmatch(r"  backup: (.+)", lines[index + 1])
        self.assertIsNotNone(match, lines[index + 1])
        self.assertRegex(match.group(1), rf"^{re.escape(str(installer.settings))}\.bak\.\d{{14}}$")
        self.assertTrue(Path(match.group(1)).exists())

    def test_t_inst_14_statusline_non_dict_replaced(self):
        installer = self.installer
        installer.write_settings({"theme": "dark", "statusLine": "x"})
        result = installer.install()
        self.assertEqual(result.code, 0, result.err)
        settings = installer.read_json(installer.settings)
        self.assertEqual(
            settings["statusLine"], {"type": "command", "command": f"bash {installer.tx_home}/statusline.sh"}
        )
        self.assertEqual(settings["theme"], "dark")

    def test_t_inst_14_statusline_absent_added(self):
        installer = self.installer
        installer.write_settings({"theme": "dark"})
        result = installer.install()
        self.assertEqual(result.code, 0, result.err)
        settings = installer.read_json(installer.settings)
        self.assertEqual(
            settings["statusLine"], {"type": "command", "command": f"bash {installer.tx_home}/statusline.sh"}
        )

    def test_t_inst_14_statusline_settings_symlink_edits_realpath(self):
        installer = self.installer
        real_settings = self.root / "dotfiles" / "settings.json"
        installer.write_settings({"theme": "dark", "statusLine": {"type": "command", "command": "old.sh"}}, real_settings)
        installer.settings.symlink_to(real_settings)
        result = installer.install()
        self.assertEqual(result.code, 0, result.err)
        self.assertTrue(installer.settings.is_symlink())
        self.assertEqual(os.readlink(installer.settings), str(real_settings))
        self.assertEqual(
            json.loads(real_settings.read_text())["statusLine"],
            {"type": "command", "command": f"bash {installer.tx_home}/statusline.sh"},
        )
        self.assertEqual(backups(installer.settings), [])
        self.assertTrue(backups(real_settings))
        self.assertEqual(list(real_settings.parent.glob(".tx-settings.*")), [])
        self.assertIn(f"  backup: {real_settings}.bak.", result.out)

    def test_t_inst_14_no_claude_and_no_settings(self):
        installer = self.installer
        self.fakes.remove("claude")
        result = installer.install()
        self.assertEqual(result.code, 0, result.err)
        self.assertIn(f"  ! {installer.settings} absent — run claude.sh install first\n", result.out)
        self.assertFalse(installer.settings.exists())
        self.assertFalse(installer.marker.exists())
        # the installer continues past the failed edit
        self.assertIn("\nNvim config (optional)\n", result.out)
        self.assertIn("\n=== Done ===\n", result.out)

    def test_t_inst_14_no_claude_settings_present_marker_absent(self):
        installer = self.installer
        self.fakes.remove("claude")
        installer.write_settings({"theme": "dark", "statusLine": {"type": "command", "command": "old.sh"}})
        before = installer.settings.read_bytes()
        result = installer.install()
        self.assertEqual(result.code, 0, result.err)
        self.assertIn(str(installer.marker), result.err)
        self.assertEqual(installer.settings.read_bytes(), before)
        self.assertEqual(backups(installer.settings), [])
        self.assertFalse(installer.marker.exists())
        self.assertIn("\nNvim config (optional)\n", result.out)
        self.assertIn("\n=== Done ===\n", result.out)

    # ----- install: idempotence --------------------------------------------------------------

    def _rerun_fixture(self) -> tuple[dict[str, bytes], dict[str, bytes], dict[str, bytes]]:
        """Full install, then snapshots of $HOME, $TX_IDE_HOME and $CLAUDE_CONFIG_DIR."""
        self.full_install()
        installer = self.installer
        return snapshot(installer.user_home), snapshot(installer.tx_home), snapshot(installer.claude_dir)

    def _assert_rerun_idempotent(self, result: Result, user_home_before, tx_home_before, claude_before) -> None:
        installer = self.installer
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out.count("already linked"), 5)
        for tool in CLI_TOOLS:
            self.assert_status(result, installer.local_bin / tool, "already linked")
        self.assert_status(result, installer.tx_home / "agents", "already linked")
        self.assert_status(result, installer.rc_file(), "tx-ide PATH block already present")
        self.assert_status(result, installer.user_tmux_conf, "block present")
        for event in CLAUDE_EVENTS:
            self.assert_status(result, event, "already current")
        for key in CONTEXT_PROFILE:
            self.assert_status(result, key, "already current")
        # $HOME: rc_file + tmux.conf bytes and their .bak inventory unchanged
        self.assertEqual(snapshot(installer.user_home), user_home_before)
        # $TX_IDE_HOME: shims, marker, tmux.conf shim byte-identical (marker rewritten, same bytes)
        tx_home_after = snapshot(installer.tx_home)
        for basename in CLAUDE_SHIMS:
            relative = str(installer.claude_shim(basename).relative_to(installer.tx_home))
            self.assertEqual(tx_home_after[relative], tx_home_before[relative], relative)
        self.assertEqual(tx_home_after["claude-managed.json"], tx_home_before["claude-managed.json"])
        self.assertEqual(installer.read_json(installer.marker)["mode"], "installed")
        self.assertEqual(tx_home_after["tmux.conf"], tx_home_before["tmux.conf"])
        # settings.json byte-identical; its .bak is written again (claude.sh + the C10 edit)
        claude_after = snapshot(installer.claude_dir)
        self.assertEqual(claude_after["settings.json"], claude_before["settings.json"])
        newest = max(backups(installer.settings), key=lambda backup: backup.stat().st_mtime_ns)
        # one second of slack: the .bak's mtime and time.time() come from different clocks/resolutions
        self.assertGreaterEqual(newest.stat().st_mtime, self.rerun_started_at - 1)

    @python_reference_only
    def test_t_inst_15_parity_install_idempotent_end_to_end(self):
        user_home_before, tx_home_before, claude_before = self._rerun_fixture()
        installer = self.installer
        self.rerun_started_at = time.time()
        result = installer.install()
        self._assert_rerun_idempotent(result, user_home_before, tx_home_before, claude_before)
        # Q22 (reference): the shim is rewritten and a byte-identical .bak dropped on every run
        self.assertEqual(result.out.count("already current"), 17)
        self.assert_status(result, installer.tmux_conf_shim, "written")
        self.assertEqual([backup.read_text() for backup in backups(installer.tmux_conf_shim)], [self.shim_content()])

    @expected_failure_on_python
    def test_t_inst_15_fixed_rerun_leaves_tmux_conf_shim_current(self):
        user_home_before, tx_home_before, claude_before = self._rerun_fixture()
        installer = self.installer
        self.rerun_started_at = time.time()
        result = installer.install()
        self._assert_rerun_idempotent(result, user_home_before, tx_home_before, claude_before)
        self.assertEqual(result.out.count("already current"), 18)
        self.assert_status(result, installer.tmux_conf_shim, "already current")
        self.assertEqual(backups(installer.tmux_conf_shim), [])

    # ----- uninstall -------------------------------------------------------------------------

    def test_t_inst_16_uninstall_unknown_argument(self):
        installer = self.installer
        user_home_before = snapshot(installer.user_home)
        result = installer.uninstall("--bogus")
        self.assertEqual(result.code, 2)
        self.assertEqual(result.err, "uninstall: unknown argument: --bogus\n")
        self.assertEqual(result.out, "")
        self.assertEqual(snapshot(installer.user_home), user_home_before)
        self.assertFalse(installer.tx_home.exists())
        self.assertEqual(snapshot(installer.claude_dir), {})

    def test_t_inst_16_uninstall_help(self):
        result = self.installer.uninstall("--help")
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, "".join(UNINSTALL.read_text().splitlines(keepends=True)[1:9]))
        self.assertEqual(result.err, "")

    def test_t_inst_16_uninstall_declined(self):
        installer = self.installer
        installer.local_bin.mkdir(parents=True)
        (installer.local_bin / "tx").symlink_to(REPO / "bin" / "tx")
        user_home_before = snapshot(installer.user_home)
        result = installer.uninstall(stdin="n\n")
        self.assertEqual(result.code, 0, result.err)
        self.assertTrue(result.out.endswith("\nAborted.\n"), result.out)
        self.assertEqual(snapshot(installer.user_home), user_home_before)
        self.assertFalse(installer.tx_home.exists())

    def test_t_inst_17_uninstall_ordering_and_statusline_removal(self):
        installer = self.installer
        self.full_install()
        home = installer.tx_home
        result = installer.uninstall(env={"TX_TMUX_SOCKET": self.tmux.socket})
        self.assertEqual(result.code, 0, result.err)
        real_settings = os.path.realpath(installer.settings)
        self.assert_in_order(
            result,
            "\nClaude statusLine (C10 reverse)\n",
            f"  → statusLine removed  (bash {home}/statusline.sh)\n",
            f"  backup: {real_settings}.bak.",
            "\nEngine hooks (setup/engines/install.sh uninstall)\n",
            f"  → {home}/statusline.sh",
            "\nCLI symlinks\n",
            *(f"  → {installer.local_bin / tool}" for tool in CLI_TOOLS),
            f"\n{home}\n",
            f"  → {home}/agents",
            f"  → {home}/tmux.conf",
            f"  → {home}/hooks",
            *(f"  → {home}/{state}" for state in STATE_DIRS),
            f"  → {home} ",
            "\n~/.tmux.conf block\n",
            f"  → {installer.user_tmux_conf}",
            "\nShell PATH block\n",
            f"  → {installer.rc_file('.zshrc')}",
            f"  → {installer.rc_file('.bash_profile')}",
            f"  → {installer.rc_file('.bashrc')}",
        )
        self.assert_status(result, home / "statusline.sh", "removed")
        for tool in CLI_TOOLS:
            self.assert_status(result, installer.local_bin / tool, "removed")
        self.assert_status(result, home / "agents", "removed")
        self.assert_status(result, home / "tmux.conf", "removed")
        self.assert_status(result, home / "hooks", "not empty, left alone")
        for state in STATE_DIRS:
            self.assert_status(result, home / state, "state, left alone (use --purge)")
        self.assert_status(result, home, "not empty, left alone")
        self.assertRegex(
            statuses(result, installer.user_tmux_conf)[0],
            rf"^tx-ide tmux removed \(backup: {re.escape(str(installer.user_tmux_conf))}\.bak\.\d{{14}}\)$",
        )
        self.assertRegex(
            statuses(result, installer.rc_file(".zshrc"))[0],
            rf"^tx-ide PATH removed \(backup: {re.escape(str(installer.rc_file('.zshrc')))}\.bak\.\d{{14}}\)$",
        )
        self.assert_status(result, installer.rc_file(".bash_profile"), "no tx-ide PATH block found")
        self.assert_status(result, installer.rc_file(".bashrc"), "no tx-ide PATH block found")
        backup_line = re.search(rf"^  backup: ({re.escape(real_settings)}\.bak\.\d{{14}})$", result.out, re.M)
        self.assertIsNotNone(backup_line)
        self.assertTrue(Path(backup_line.group(1)).exists())
        # files
        settings = installer.read_json(installer.settings)
        self.assertNotIn("statusLine", settings)
        self.assertNotIn("hooks", settings)
        self.assertEqual(settings["theme"], "dark")
        for key in ("disableWorkflows", "disableArtifact", "includeGitInstructions", "skillOverrides"):
            self.assertNotIn(key, settings)
        self.assertNotIn("deny", settings.get("permissions", {}))
        self.assertFalse(installer.marker.exists())
        for basename in CLAUDE_SHIMS:
            self.assertFalse(installer.claude_shim(basename).exists(), basename)
        self.assertTrue((home / "hooks" / "claude").is_dir())
        for tool in CLI_TOOLS:
            self.assertFalse((installer.local_bin / tool).is_symlink(), tool)
        self.assertFalse((home / "agents").is_symlink())
        self.assertFalse((home / "statusline.sh").exists())
        self.assertFalse(installer.tmux_conf_shim.exists())
        self.assertEqual(installer.user_tmux_conf.read_text(), "set -g mouse on\n")
        self.assertEqual(installer.rc_file(".zshrc").read_text(), "alias x=y\n")

    def test_t_inst_18_statusline_ownership_check(self):
        installer = self.installer
        installer.tx_home.mkdir()
        installer.marker.write_text(
            json.dumps({"statusLine_command": f"bash {installer.tx_home}/statusline.sh"}, indent=2) + "\n"
        )
        installer.write_settings({"theme": "dark", "statusLine": {"type": "command", "command": "bash /elsewhere.sh"}})
        before = installer.settings.read_bytes()
        result = installer.uninstall()
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(installer.settings.read_bytes(), before)
        statusline_section = self.section(
            result, "Claude statusLine (C10 reverse)", "Engine hooks (setup/engines/install.sh uninstall)"
        )
        self.assertIn("\n  → statusLine not ours / absent — left alone", statusline_section)
        self.assertNotIn("backup:", statusline_section)
        # The statusLine step wrote no .bak; every .bak on disk is one some later step announced
        # (the reference's claude.sh rewrites settings.json whenever a marker exists — see NOTES).
        announced = re.findall(r"^  backup: (.+)$", result.out, re.M)
        self.assertEqual(sorted(str(backup) for backup in backups(installer.settings)), sorted(announced))

    def test_t_inst_18_legacy_in_settings_marker_fallback(self):
        installer = self.installer
        installer.tx_home.mkdir()
        status_command = f"bash {installer.tx_home}/statusline.sh"
        installer.write_settings(
            {
                "theme": "dark",
                "statusLine": {"type": "command", "command": status_command},
                "_tx_ide_managed": {"statusLine_command": status_command},
            }
        )
        result = installer.uninstall()
        self.assertEqual(result.code, 0, result.err)
        self.assertIn(f"  → statusLine removed  ({status_command})\n  backup: {installer.settings}.bak.", result.out)
        settings = installer.read_json(installer.settings)
        self.assertNotIn("statusLine", settings)
        self.assertEqual(settings["theme"], "dark")
        self.assertTrue(backups(installer.settings))

    def test_t_inst_18_settings_absent(self):
        installer = self.installer
        result = installer.uninstall()
        self.assertEqual(result.code, 0, result.err)
        self.assert_status(result, installer.settings, "not present")
        self.assertFalse(installer.settings.exists())
        self.assertEqual(snapshot(installer.claude_dir), {})

    def test_t_inst_19_keep_agent(self):
        installer = self.installer
        self.full_install()
        home = installer.tx_home
        settings_before = installer.settings.read_bytes()
        marker_before = installer.marker.read_bytes()
        hooks_before = snapshot(home / "hooks")
        statusline_before = (home / "statusline.sh").read_bytes()
        claude_before = snapshot(installer.claude_dir)
        result = installer.uninstall("--keep-agent")
        self.assertEqual(result.code, 0, result.err)
        self.assert_status_line(result, "claude.sh uninstall", "--keep-agent — hooks + statusline left in place")
        self.assertNotIn("Claude statusLine (C10 reverse)", result.out)
        self.assertEqual(installer.settings.read_bytes(), settings_before)
        self.assertEqual(snapshot(installer.claude_dir), claude_before)
        self.assertEqual(installer.marker.read_bytes(), marker_before)
        self.assertEqual(snapshot(home / "hooks"), hooks_before)
        self.assertEqual((home / "statusline.sh").read_bytes(), statusline_before)
        for tool in CLI_TOOLS:
            self.assertFalse((installer.local_bin / tool).is_symlink(), tool)
            self.assert_status(result, installer.local_bin / tool, "removed")
        self.assertFalse((home / "agents").is_symlink())
        self.assertFalse(installer.tmux_conf_shim.exists())
        self.assertEqual(installer.user_tmux_conf.read_text(), "set -g mouse on\n")
        self.assertEqual(installer.rc_file(".zshrc").read_text(), "alias x=y\n")

    def test_t_inst_20_symlink_and_agents_ownership(self):
        installer = self.installer
        installer.local_bin.mkdir(parents=True)
        tx_link = installer.local_bin / "tx"
        tx_link.symlink_to("/other/bin/tx")
        agents = installer.tx_home / "agents"
        agents.mkdir(parents=True)
        result = installer.uninstall()
        self.assertEqual(result.code, 0, result.err)
        self.assert_status(result, tx_link, "points elsewhere, left alone")
        self.assertEqual(os.readlink(tx_link), "/other/bin/tx")
        self.assert_status(result, installer.local_bin / "tx-assistant", "not present")
        self.assert_status(result, agents, "not a symlink, left alone")
        self.assertTrue(agents.is_dir())
        self.assertFalse(agents.is_symlink())

    @python_reference_only
    def test_t_inst_21_parity_plain_uninstall_keeps_state_deletes_config_and_log(self):
        # Q10 (reference): `uninstall` removes config.json AND log.jsonl unconditionally.
        installer = self.installer
        self.state_fixture()
        home = installer.tx_home
        result = installer.uninstall()
        self.assertEqual(result.code, 0, result.err)
        for state in STATE_DIRS:
            self.assertEqual((home / state / "keep.txt").read_text(), f"{state}\n")
            self.assert_status(result, home / state, "state, left alone (use --purge)")
        self.assertEqual(result.out.count("state, left alone (use --purge)"), 3)
        self.assertFalse(installer.tmux_conf_shim.exists())
        self.assertFalse((home / "config.json").exists())
        self.assert_status(result, home / "tmux.conf", "removed")
        self.assert_status(result, home / "config.json", "removed")
        self.assertFalse((home / "log.jsonl").exists())
        self.assert_status(result, home / "log.jsonl", "removed")
        self.assertTrue(home.is_dir())
        self.assert_status(result, home, "not empty, left alone")

    @expected_failure_on_python
    def test_t_inst_21_fixed_plain_uninstall_keeps_log(self):
        installer = self.installer
        self.state_fixture()
        home = installer.tx_home
        result = installer.uninstall()
        self.assertEqual(result.code, 0, result.err)
        for state in STATE_DIRS:
            self.assertEqual((home / state / "keep.txt").read_text(), f"{state}\n")
            self.assert_status(result, home / state, "state, left alone (use --purge)")
        self.assertEqual(result.out.count("state, left alone (use --purge)"), 3)
        self.assertFalse(installer.tmux_conf_shim.exists())
        self.assertFalse((home / "config.json").exists())
        self.assert_status(result, home / "tmux.conf", "removed")
        self.assert_status(result, home / "config.json", "removed")
        self.assertEqual((home / "log.jsonl").read_text(), '{"type": "spawn"}\n')
        self.assertEqual(statuses(result, home / "log.jsonl"), [])
        self.assertTrue(home.is_dir())
        self.assert_status(result, home, "not empty, left alone")

    def test_t_inst_21_purge_removes_state(self):
        installer = self.installer
        self.state_fixture()
        home = installer.tx_home
        result = installer.uninstall("--purge")
        self.assertEqual(result.code, 0, result.err)
        for state in STATE_DIRS:
            self.assertFalse((home / state).exists(), state)
            self.assert_status(result, home / state, "purged")
        self.assertEqual(len(re.findall(r"^  → \S+ +purged$", result.out, re.M)), 3)
        self.assert_status(result, home / "config.json", "removed")
        self.assert_status(result, home / "tmux.conf", "removed")
        self.assertFalse(home.exists())
        self.assert_status(result, home, "removed (empty)")

    def test_t_inst_21_purge_after_plain_uninstall(self):
        installer = self.installer
        self.state_fixture()
        home = installer.tx_home
        first = installer.uninstall()
        self.assertEqual(first.code, 0, first.err)
        self.assert_status(first, home, "not empty, left alone")
        result = installer.uninstall("--purge")
        self.assertEqual(result.code, 0, result.err)
        for state in STATE_DIRS:
            self.assert_status(result, home / state, "purged")
        self.assertEqual(len(re.findall(r"^  → \S+ +purged$", result.out, re.M)), 3)
        self.assertFalse(home.exists())
        self.assert_status(result, home, "removed (empty)")

    def test_t_inst_22_strip_block_semantics(self):
        installer = self.installer
        tmux_conf = installer.user_tmux_conf
        tmux_original = "a\n\n# === BEGIN tx-ide ===\nsource-file X\n# === END tx-ide ===\n"
        tmux_conf.write_text(tmux_original)
        rc_file = installer.rc_file(".zshrc")
        rc_original = "alias x=y\n" + PATH_BLOCK
        rc_file.write_text(rc_original)
        result = installer.uninstall()
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual([backup.read_text() for backup in backups(tmux_conf)], [tmux_original])
        self.assertEqual(tmux_conf.read_text(), "a\n")
        self.assertEqual(statuses(result, tmux_conf), [f"tx-ide tmux removed (backup: {backups(tmux_conf)[0]})"])
        self.assertEqual([backup.read_text() for backup in backups(rc_file)], [rc_original])
        self.assertEqual(rc_file.read_text(), "alias x=y\n")
        self.assertEqual(statuses(result, rc_file), [f"tx-ide PATH removed (backup: {backups(rc_file)[0]})"])

    def test_t_inst_22_strip_block_no_marker(self):
        installer = self.installer
        tmux_conf = installer.user_tmux_conf
        tmux_conf.write_text("set -g mouse on\n")
        result = installer.uninstall()
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(tmux_conf.read_text(), "set -g mouse on\n")
        self.assertEqual(backups(tmux_conf), [])
        self.assert_status(result, tmux_conf, "no tx-ide tmux block found")

    def test_t_inst_22_strip_block_runs_on_all_three_rc_files(self):
        installer = self.installer
        present = {".zshrc": "alias x=y\n" + PATH_BLOCK, ".bashrc": "export A=1\n" + PATH_BLOCK}
        for name, content in present.items():
            installer.rc_file(name).write_text(content)
        result = installer.uninstall()
        self.assertEqual(result.code, 0, result.err)
        for name, content in present.items():
            rc_file = installer.rc_file(name)
            self.assertEqual([backup.read_text() for backup in backups(rc_file)], [content])
            self.assertEqual(rc_file.read_text(), content[: -len(PATH_BLOCK)])
            self.assertEqual(statuses(result, rc_file), [f"tx-ide PATH removed (backup: {backups(rc_file)[0]})"])
        self.assertFalse(installer.rc_file(".bash_profile").exists())
        self.assert_status(result, installer.rc_file(".bash_profile"), "no tx-ide PATH block found")

    def test_t_inst_22_strip_block_detects_by_substring_strips_by_whole_line(self):
        # Q23 PARITY
        installer = self.installer
        tmux_conf = installer.user_tmux_conf
        original = "a\n# === BEGIN tx-ide === x\nfoo\n# === END tx-ide ===\n"
        tmux_conf.write_text(original)
        result = installer.uninstall()
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual([backup.read_text() for backup in backups(tmux_conf)], [original])
        self.assertEqual(tmux_conf.read_text(), "a\n# === BEGIN tx-ide === x\nfoo\n")
        self.assertEqual(statuses(result, tmux_conf), [f"tx-ide tmux removed (backup: {backups(tmux_conf)[0]})"])

    def test_t_inst_22_strip_block_collapses_at_most_one_trailing_blank(self):
        # Q23 PARITY
        installer = self.installer
        tmux_conf = installer.user_tmux_conf
        original = "a\n\n# === BEGIN tx-ide ===\nsource-file X\n# === END tx-ide ===\n\n\n"
        tmux_conf.write_text(original)
        result = installer.uninstall()
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual([backup.read_text() for backup in backups(tmux_conf)], [original])
        self.assertEqual(tmux_conf.read_text(), "a\n\n\n")
        self.assertEqual(statuses(result, tmux_conf), [f"tx-ide tmux removed (backup: {backups(tmux_conf)[0]})"])

    # ===== setup/engines/*.sh (T-INST-23..52) ==================================================

    # ----- helpers -----------------------------------------------------------------------------

    def engines_copy(self, data: dict | str | None, name: str = "settings.json") -> Path:
        """A settings copy under `<root>/sandbox/`; `None` leaves the file absent."""
        path = self.root / "sandbox" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if data is not None:
            self.installer.write_settings(data, path)
        return path

    def engines_sidecar(self, copy: Path) -> Path:
        return Path(f"{copy}.tx-managed.json")

    def engines_read(self, path: Path) -> dict:
        return json.loads(path.read_text())

    def engines_tx_block(self, shim: str) -> dict:
        return {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": str(self.installer.claude_shim(shim)),
                    "timeout": 10,
                    "async": True,
                }
            ],
        }

    def engines_hook_commands(self, home: Path | None = None) -> dict[str, str]:
        home = home or self.installer.tx_home
        return {event: str(home / "hooks" / "claude" / f"{shim}.sh") for event, shim in CLAUDE_EVENTS.items()}

    def engines_expected_marker(
        self,
        *,
        context_profile_previous: dict | None,
        previous: dict | None = None,
        context_profile: dict | None = CONTEXT_PROFILE,
        status_line_command: str | None = None,
    ) -> dict:
        return {
            "version": 3,
            "mode": "coexist",
            "home": str(self.installer.tx_home),
            "hook_commands": self.engines_hook_commands(),
            "context_profile": context_profile,
            "context_profile_previous": context_profile_previous,
            "statusLine_command": status_line_command,
            "tmux_session_closed": self.installer.tmux_session_closed(),
            "previous": previous,
        }

    def engines_all_absent(self) -> dict:
        return {key: {"absent": True} for key in CONTEXT_PROFILE}

    def engines_session_closed_hook(self) -> str:
        """The private server's global session-closed hook, `session-closed[N] ` prefix stripped."""
        output = self.tmux.run("show-hooks", "-g", "session-closed").stdout.rstrip("\n")
        return re.sub(r"^session-closed(\[[0-9]+\])? *", "", output)

    def engines_socket_env(self, **extra: str | None) -> dict[str, str | None]:
        return {"TX_TMUX_SOCKET": self.tmux.socket, **extra}

    def engines_sleeper(self) -> subprocess.Popen:
        child = subprocess.Popen(["sleep", "300"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        return child

    def engines_backups(self, path: Path) -> list[tuple[str, str]]:
        return [(backup.name, backup.read_text()) for backup in backups(path)]

    def engines_pid_file(self, pid: int | None) -> Path:
        path = self.installer.claude_dir / "mailbox" / "mx-speaker.pid"
        if pid is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{pid}\n")
        return path

    def engines_prev_hook_file(self) -> Path:
        return self.installer.tx_home / "hooks" / ".session-closed.prev"

    def engines_assert_claude_shims(self, present: bool) -> None:
        for shim in CLAUDE_SHIMS:
            self.assertEqual(self.installer.claude_shim(shim).exists(), present, shim)

    def engines_assert_no_temp_files(self, directory: Path, prefix: str) -> None:
        self.assertEqual([], [path.name for path in directory.iterdir() if path.name.startswith(prefix)])

    # ----- T-INST-23 ---------------------------------------------------------------------------

    def test_t_inst_23_argument_parsing(self) -> None:
        usage = "usage: claude.sh {install|uninstall|status} [--dry-run] [--settings PATH] [--no-context-profile]"

        result = self.installer.claude_sh()
        self.assertEqual(2, result.code)
        self.assertIn(usage, result.err)
        self.assertEqual("", result.out)

        result = self.installer.claude_sh("frob")
        self.assertEqual(2, result.code)
        self.assertEqual("claude.sh: unknown argument: frob", result.err.splitlines()[0])
        self.assertIn(usage, result.err)

        result = self.installer.claude_sh("install", "--settings")
        self.assertNotEqual(0, result.code)
        self.assertIn("--settings needs a PATH", result.err)

        copy = self.engines_copy({})
        result = self.installer.claude_sh("install", "--dry-run", f"--settings={copy}")
        self.assertEqual(0, result.code)
        self.assertIn(f"== claude.sh install ==  (dry-run)\n(sandbox: {copy})", result.out)

        result = self.installer.claude_sh("-h")
        self.assertEqual(0, result.code)
        self.assertIn(usage, result.err)
        self.assertEqual("", result.out)

    # ----- T-INST-24 ---------------------------------------------------------------------------

    def test_t_inst_24_six_shims_exact_content(self) -> None:
        result = self.installer.claude_sh("install", "--settings", str(self.engines_copy({})))
        self.assertEqual(0, result.code)
        self.assertEqual(
            sorted(f"{shim}.sh" for shim in CLAUDE_SHIMS),
            sorted(path.name for path in (self.installer.tx_home / "hooks" / "claude").iterdir()),
        )
        for shim, (event, keep_stdin) in CLAUDE_SHIMS.items():
            path = self.installer.claude_shim(shim)
            self.assertTrue(engines_executable(path), shim)
            self.assertEqual(self.installer.claude_shim_text(event, keep_stdin), path.read_text(), shim)
            self.assertIn(engines_status_line(f"shim {path}", event), result.out)

    def test_t_inst_24_tx_python_baked(self) -> None:
        result = self.installer.claude_sh(
            "install", "--settings", str(self.engines_copy({})), env={"TX_PYTHON": "python3"}
        )
        self.assertEqual(0, result.code)
        for shim, (event, keep_stdin) in CLAUDE_SHIMS.items():
            self.assertEqual(
                self.installer.claude_shim_text(event, keep_stdin, python="python3"),
                self.installer.claude_shim(shim).read_text(),
                shim,
            )

    def test_t_inst_24_tilde_home_expanded(self) -> None:
        expanded = self.installer.user_home / "x"
        result = self.installer.claude_sh(
            "install", "--settings", str(self.engines_copy({})), env={"TX_IDE_HOME": "~/x"}
        )
        self.assertEqual(0, result.code)
        for shim, (event, keep_stdin) in CLAUDE_SHIMS.items():
            path = expanded / "hooks" / "claude" / f"{shim}.sh"
            self.assertEqual(self.installer.claude_shim_text(event, keep_stdin, home=expanded), path.read_text(), shim)
        self.assertFalse(self.installer.tx_home.exists())

    # ----- T-INST-25 ---------------------------------------------------------------------------

    def test_t_inst_25_twelve_events_fresh_add(self) -> None:
        copy = self.engines_copy(None)
        result = self.installer.claude_sh("install", "--settings", str(copy))
        self.assertEqual(0, result.code)
        data = self.engines_read(copy)
        self.assertEqual(sorted(CLAUDE_EVENTS), sorted(data["hooks"]))
        for event, shim in CLAUDE_EVENTS.items():
            self.assertEqual([self.engines_tx_block(shim)], data["hooks"][event], event)
            self.assertIn(engines_plan_line(event, f"add      {self.installer.claude_shim(shim)}"), result.out)
        self.assertEqual([], backups(copy))
        self.assertEqual(json.dumps(data, indent=2) + "\n", copy.read_text())

    # ----- T-INST-26 ---------------------------------------------------------------------------

    def test_t_inst_26_add_beside_foreign_hooks(self) -> None:
        peon = {"matcher": "", "hooks": [{"type": "command", "command": "peon-ping"}]}
        copy = self.engines_copy({"hooks": {"Stop": [peon]}})
        original = copy.read_text()
        result = self.installer.claude_sh("install", "--settings", str(copy))
        self.assertEqual(0, result.code)
        data = self.engines_read(copy)
        self.assertEqual([peon, self.engines_tx_block("post")], data["hooks"]["Stop"])
        self.assertEqual(1, len(backups(copy)))
        self.assertEqual(original, backups(copy)[0].read_text())
        self.assertIn(f"  backup: {backups(copy)[0]}", result.out)

    # ----- T-INST-27 ---------------------------------------------------------------------------

    def test_t_inst_27_repoint_by_marker(self) -> None:
        old_command = "/old/hooks/claude/post.sh"
        new_command = str(self.installer.claude_shim("post"))
        foreign = {"type": "command", "command": "require-worktree"}
        old_entry = {"type": "command", "command": old_command, "timeout": 10, "async": True}
        copy = self.engines_copy({"hooks": {"Stop": [{"matcher": "", "hooks": [foreign, old_entry, foreign]}]}})
        self.engines_sidecar(copy).write_text(json.dumps({"version": 3, "hook_commands": {"Stop": old_command}}))
        result = self.installer.claude_sh("install", "--settings", str(copy))
        self.assertEqual(0, result.code)
        data = self.engines_read(copy)
        repointed = {"type": "command", "command": new_command, "timeout": 10, "async": True}
        self.assertEqual([{"matcher": "", "hooks": [foreign, repointed, foreign]}], data["hooks"]["Stop"])
        self.assertIn(engines_plan_line("Stop", f"repoint  {old_command}  →  {new_command}"), result.out)

    def test_t_inst_27_already_current_edges(self) -> None:
        new_command = str(self.installer.claude_shim("post"))
        # recorded command == new command
        recorded = self.engines_copy({}, "recorded.json")
        self.engines_sidecar(recorded).write_text(json.dumps({"version": 3, "hook_commands": {"Stop": new_command}}))
        result = self.installer.claude_sh("install", "--settings", str(recorded))
        self.assertEqual(0, result.code)
        self.assertIn(engines_plan_line("Stop", "already current"), result.out)
        self.assertNotIn("Stop", self.engines_read(recorded).get("hooks", {}))
        # recorded absent, settings already holds the new command
        present = self.engines_copy({"hooks": {"Stop": [self.engines_tx_block("post")]}}, "present.json")
        result = self.installer.claude_sh("install", "--settings", str(present))
        self.assertEqual(0, result.code)
        self.assertIn(engines_plan_line("Stop", "already current"), result.out)
        self.assertEqual([self.engines_tx_block("post")], self.engines_read(present)["hooks"]["Stop"])

    # ----- T-INST-28 ---------------------------------------------------------------------------

    def test_t_inst_28_marker_sidecar_v3_fields(self) -> None:
        expected = self.engines_expected_marker(context_profile_previous=self.engines_all_absent())
        expected_text = json.dumps(expected, indent=2) + "\n"
        # sandbox → beside the copy
        copy = self.engines_copy({})
        result = self.installer.claude_sh("install", "--settings", str(copy))
        self.assertEqual(0, result.code)
        self.assertEqual(expected_text, self.engines_sidecar(copy).read_text())
        self.assertIn(f"  ✓ marker sidecar written  ({self.engines_sidecar(copy)})", result.out)
        self.engines_assert_no_temp_files(copy.parent, ".tx-managed.")
        self.assertFalse(self.installer.marker.exists())
        # non-sandbox → <HOME>/claude-managed.json
        self.installer.write_settings({})
        result = self.installer.claude_sh("install")
        self.assertEqual(0, result.code)
        self.assertEqual(expected_text, self.installer.marker.read_text())
        self.assertIn(f"  ✓ marker sidecar written  ({self.installer.marker})", result.out)
        self.engines_assert_no_temp_files(self.installer.tx_home, ".tx-managed.")

    # ----- T-INST-29 ---------------------------------------------------------------------------

    def test_t_inst_29_previous_preserved_across_reinstalls(self) -> None:
        foreign_marker = {
            "version": 2,
            "hook_commands": {"Stop": "/mailbox/stop.sh"},
            "statusLine_command": "/mailbox/statusline.sh",
        }
        copy = self.engines_copy({})
        self.engines_sidecar(copy).write_text(json.dumps(foreign_marker))
        self.assertEqual(0, self.installer.claude_sh("install", "--settings", str(copy)).code)
        first = self.engines_read(self.engines_sidecar(copy))
        self.assertEqual(foreign_marker, first["previous"])
        self.assertEqual("/mailbox/statusline.sh", first["statusLine_command"])
        self.assertEqual(self.engines_hook_commands(), first["hook_commands"])
        self.assertEqual(0, self.installer.claude_sh("install", "--settings", str(copy)).code)
        second = self.engines_read(self.engines_sidecar(copy))
        self.assertEqual(foreign_marker, second["previous"])
        self.assertEqual("/mailbox/statusline.sh", second["statusLine_command"])

    # ----- T-INST-30 ---------------------------------------------------------------------------

    def test_t_inst_30_context_profile_keys_and_previous(self) -> None:
        copy = self.engines_copy({"disableWorkflows": False, "permissions": {"allow": ["Bash"]}})
        result = self.installer.claude_sh("install", "--settings", str(copy))
        self.assertEqual(0, result.code)
        data = self.engines_read(copy)
        self.assertIs(True, data["disableWorkflows"])
        self.assertIs(True, data["disableArtifact"])
        self.assertIs(False, data["includeGitInstructions"])
        self.assertEqual(CONTEXT_PROFILE["skillOverrides"], data["skillOverrides"])
        self.assertEqual({"allow": ["Bash"], "deny": ["ReportFindings", "ListAgents"]}, data["permissions"])
        expected_previous = {
            "disableWorkflows": {"value": False},
            "disableArtifact": {"absent": True},
            "includeGitInstructions": {"absent": True},
            "skillOverrides": {"absent": True},
            "permissions.deny": {"absent": True},
        }
        self.assertEqual(expected_previous, self.engines_read(self.engines_sidecar(copy))["context_profile_previous"])
        self.assertIn(engines_plan_line("disableWorkflows", "set"), result.out)
        for key in ("disableArtifact", "includeGitInstructions", "skillOverrides", "permissions.deny"):
            self.assertIn(engines_plan_line(key, "add"), result.out)
        # Edge: re-install neither re-records nor re-applies
        result = self.installer.claude_sh("install", "--settings", str(copy))
        self.assertEqual(0, result.code)
        for key in CONTEXT_PROFILE:
            self.assertIn(engines_plan_line(key, "already current"), result.out)
        self.assertEqual(expected_previous, self.engines_read(self.engines_sidecar(copy))["context_profile_previous"])

    def test_t_inst_30_new_profile_key_recorded_on_introducing_run(self) -> None:
        copy = self.engines_copy({})
        older_marker = self.engines_expected_marker(context_profile_previous={"disableWorkflows": {"value": False}})
        self.engines_sidecar(copy).write_text(json.dumps(older_marker))
        result = self.installer.claude_sh("install", "--settings", str(copy))
        self.assertEqual(0, result.code)
        expected_previous = {**self.engines_all_absent(), "disableWorkflows": {"value": False}}
        self.assertEqual(expected_previous, self.engines_read(self.engines_sidecar(copy))["context_profile_previous"])

    # ----- T-INST-31 ---------------------------------------------------------------------------

    def test_t_inst_31_no_context_profile(self) -> None:
        copy = self.engines_copy({})
        result = self.installer.claude_sh("install", "--no-context-profile", "--settings", str(copy))
        self.assertEqual(0, result.code)
        self.assertEqual(["hooks"], list(self.engines_read(copy)))
        self.assertIn(engines_plan_line("context profile", "skipped (--no-context-profile)"), result.out)
        marker = self.engines_read(self.engines_sidecar(copy))
        self.assertIsNone(marker["context_profile"])
        self.assertIsNone(marker["context_profile_previous"])

    def test_t_inst_31_no_context_profile_carries_existing_record(self) -> None:
        copy = self.engines_copy({"disableWorkflows": False})
        self.assertEqual(0, self.installer.claude_sh("install", "--settings", str(copy)).code)
        recorded = self.engines_read(self.engines_sidecar(copy))["context_profile_previous"]
        result = self.installer.claude_sh("install", "--no-context-profile", "--settings", str(copy))
        self.assertEqual(0, result.code)
        marker = self.engines_read(self.engines_sidecar(copy))
        self.assertEqual(CONTEXT_PROFILE, marker["context_profile"])
        self.assertEqual(recorded, marker["context_profile_previous"])
        self.assertIs(True, self.engines_read(copy)["disableWorkflows"])
        # a later uninstall still restores (the emptied `permissions` parent is left behind)
        self.assertEqual(0, self.installer.claude_sh("uninstall", "--settings", str(copy)).code)
        self.assertEqual({"disableWorkflows": False, "permissions": {}}, self.engines_read(copy))

    # ----- T-INST-32 ---------------------------------------------------------------------------

    def test_t_inst_32_legacy_marker_migration(self) -> None:
        old_command = "/old/hooks/claude/post.sh"
        legacy = {"version": 2, "hook_commands": {"Stop": old_command}}
        copy = self.engines_copy(
            {
                "_tx_ide_managed": legacy,
                "hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": old_command}]}]},
            }
        )
        result = self.installer.claude_sh("install", "--settings", str(copy))
        self.assertEqual(0, result.code)
        data = self.engines_read(copy)
        self.assertNotIn("_tx_ide_managed", data)
        self.assertIn(engines_plan_line("marker", "migrated out of settings.json → sidecar"), result.out)
        new_command = str(self.installer.claude_shim("post"))
        self.assertEqual([{"type": "command", "command": new_command}], data["hooks"]["Stop"][0]["hooks"])
        self.assertIn(engines_plan_line("Stop", f"repoint  {old_command}  →  {new_command}"), result.out)
        self.assertEqual(legacy, self.engines_read(self.engines_sidecar(copy))["previous"])

    def test_t_inst_32_status_with_legacy_marker(self) -> None:
        copy = self.engines_copy({"_tx_ide_managed": {"version": 2, "mode": "coexist", "home": "/old", "hook_commands": {}}})
        result = self.installer.claude_sh("status", "--settings", str(copy))
        self.assertEqual(0, result.code)
        self.assertIn("  marker: version=2  mode=coexist  home=/old", result.out)
        self.assertIn(
            f"    {'location':<18} LEGACY — embedded in settings.json (Claude ≥2.1.257 rejects the whole file; re-run install to migrate)",
            result.out,
        )

    # ----- T-INST-33 ---------------------------------------------------------------------------

    def engines_installed_copy(self, data: dict, name: str = "settings.json") -> Path:
        copy = self.engines_copy(data, name)
        self.assertEqual(0, self.installer.claude_sh("install", "--settings", str(copy)).code)
        return copy

    def test_t_inst_33_uninstall_restore_or_strip(self) -> None:
        foreign_notify = {"matcher": "", "hooks": [{"type": "command", "command": "foreign-notify"}]}
        copy = self.engines_installed_copy({"hooks": {"Notification": [foreign_notify]}})
        data = self.engines_read(copy)
        peon = {"type": "command", "command": "peon-ping"}
        data["hooks"]["Stop"][0]["hooks"].insert(0, peon)
        self.installer.write_settings(data, copy)
        before = copy.read_text()
        sidecar = self.engines_sidecar(copy)
        marker = self.engines_read(sidecar)
        marker["previous"] = {"hook_commands": {"Stop": "/mailbox/stop.sh"}}
        sidecar.write_text(json.dumps(marker))

        result = self.installer.claude_sh("uninstall", "--settings", str(copy))
        self.assertEqual(0, result.code)
        restored = {"type": "command", "command": "/mailbox/stop.sh", "timeout": 10, "async": True}
        self.assertEqual(
            {
                "hooks": {
                    "Notification": [foreign_notify],
                    "Stop": [{"matcher": "", "hooks": [peon, restored]}],
                },
                "permissions": {},
                "_tx_ide_managed": {"hook_commands": {"Stop": "/mailbox/stop.sh"}},
            },
            self.engines_read(copy),
        )
        post_command = str(self.installer.claude_shim("post"))
        self.assertIn(engines_plan_line("Stop", f"restore  {post_command}  →  /mailbox/stop.sh"), result.out)
        for event, shim in CLAUDE_EVENTS.items():
            if event != "Stop":
                self.assertIn(engines_plan_line(event, f"strip    {self.installer.claude_shim(shim)}"), result.out)
        self.assertIn(before, [backup.read_text() for backup in backups(copy)])
        self.assertFalse(sidecar.exists())
        self.assertIn(f"  ✓ marker sidecar removed  ({sidecar})", result.out)
        self.engines_assert_claude_shims(False)

    def test_t_inst_33_hooks_dropped_when_empty(self) -> None:
        copy = self.engines_installed_copy({})
        result = self.installer.claude_sh("uninstall", "--settings", str(copy))
        self.assertEqual(0, result.code)
        self.assertEqual({"permissions": {}}, self.engines_read(copy))

    def test_t_inst_33_drift_skipped(self) -> None:
        copy = self.engines_installed_copy({})
        data = self.engines_read(copy)
        del data["hooks"]["PreCompact"]
        self.installer.write_settings(data, copy)
        result = self.installer.claude_sh("uninstall", "--settings", str(copy))
        self.assertEqual(0, result.code)
        self.assertIn(engines_plan_line("PreCompact", "missing (drift) — skipped"), result.out)
        self.assertNotIn("hooks", self.engines_read(copy))

    def test_t_inst_33_no_marker_leaves_settings_alone(self) -> None:
        self.engines_installed_copy({}, "installed.json")
        self.engines_assert_claude_shims(True)
        foreign = self.engines_copy({"hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "peon-ping"}]}]}}, "foreign.json")
        before = foreign.read_text()
        result = self.installer.claude_sh("uninstall", "--settings", str(foreign))
        self.assertEqual(0, result.code)
        self.assertIn("  ! no _tx_ide_managed marker — settings.json left alone", result.out)
        self.assertEqual(before, foreign.read_text())
        self.assertEqual([], backups(foreign))
        self.engines_assert_claude_shims(False)

    # ----- T-INST-34 ---------------------------------------------------------------------------

    def engines_profile_installed_copy(self, previous_marker: dict | None, name: str = "settings.json") -> Path:
        copy = self.engines_copy({"disableWorkflows": False, "permissions": {"allow": ["Bash"]}}, name)
        if previous_marker is not None:
            self.engines_sidecar(copy).write_text(json.dumps(previous_marker))
        self.assertEqual(0, self.installer.claude_sh("install", "--settings", str(copy)).code)
        return copy

    def test_t_inst_34_uninstall_profile_restore_and_previous_reembedded(self) -> None:
        foreign_marker = {"version": 2, "hook_commands": {"Stop": "/mailbox/stop.sh"}}
        copy = self.engines_profile_installed_copy(foreign_marker)
        result = self.installer.claude_sh("uninstall", "--settings", str(copy))
        self.assertEqual(0, result.code)
        data = self.engines_read(copy)
        self.assertIs(False, data["disableWorkflows"])
        for key in ("disableArtifact", "includeGitInstructions", "skillOverrides"):
            self.assertNotIn(key, data)
        self.assertEqual({"allow": ["Bash"]}, data["permissions"])
        self.assertEqual(foreign_marker, data["_tx_ide_managed"])
        self.assertIn(engines_plan_line("disableWorkflows", "restore  → false"), result.out)
        self.assertIn(engines_plan_line("permissions.deny", "strip    (was absent before tx)"), result.out)
        # previous null → no marker re-embedded
        plain = self.engines_profile_installed_copy(None, "plain.json")
        self.assertEqual(0, self.installer.claude_sh("uninstall", "--settings", str(plain)).code)
        data = self.engines_read(plain)
        self.assertNotIn("_tx_ide_managed", data)
        self.assertEqual({"disableWorkflows": False, "permissions": {"allow": ["Bash"]}}, data)

    def engines_missing_parent_copy(self) -> tuple[Path, str, list[tuple[str, str]]]:
        """An installed copy whose `permissions` object was removed by hand; returns the copy, its
        bytes and the `.bak` inventory the install left (the uninstall must add none)."""
        copy = self.engines_profile_installed_copy(None)
        data = self.engines_read(copy)
        del data["permissions"]
        self.installer.write_settings(data, copy)
        return copy, copy.read_text(), self.engines_backups(copy)

    @expected_failure_on_python
    def test_t_inst_34_fixed_missing_parent_key(self) -> None:
        copy, before, backups_before = self.engines_missing_parent_copy()
        result = self.installer.claude_sh("uninstall", "--settings", str(copy))
        self.assertEqual(1, result.code)
        self.assertEqual(1, len(result.err.splitlines()))
        self.assertIn("permissions.deny", result.err)
        self.assertNotIn("Traceback", result.err)
        self.assertEqual(before, copy.read_text())
        self.assertEqual(backups_before, self.engines_backups(copy))
        self.assertTrue(self.engines_sidecar(copy).exists())

    @python_reference_only
    def test_t_inst_34_parity_missing_parent_key(self) -> None:
        copy, before, backups_before = self.engines_missing_parent_copy()
        result = self.installer.claude_sh("uninstall", "--settings", str(copy))
        self.assertEqual(1, result.code)
        self.assertIn("Traceback", result.err)
        self.assertIn("KeyError: 'permissions'", result.err)
        self.assertEqual(before, copy.read_text())
        self.assertEqual(backups_before, self.engines_backups(copy))
        self.assertTrue(self.engines_sidecar(copy).exists())
        self.engines_assert_claude_shims(True)

    # ----- T-INST-35 ---------------------------------------------------------------------------

    @hook_step_reachable
    def test_t_inst_35_tmux_hook_set_and_foreign_stash(self) -> None:
        self.tmux.new_session("keep", "sleep 300")
        self.tmux.run("set-hook", "-g", "session-closed", 'run-shell "echo user"', check=True)
        copy = self.engines_copy({})
        result = self.installer.claude_sh("install", "--settings", str(copy), env=self.engines_socket_env())
        self.assertEqual(0, result.code)
        self.assertEqual('run-shell "echo user"', self.engines_prev_hook_file().read_text())
        self.assertEqual(self.installer.tmux_session_closed(), self.engines_session_closed_hook())
        stashed = engines_status_line("tmux global session-closed", "stashed an existing hook (restored on uninstall)")
        applied = engines_status_line("tmux global session-closed", "set (→ reconcile)")
        self.assertLess(result.out.index(stashed), result.out.index(applied))

    @hook_step_reachable
    def test_t_inst_35_own_hook_not_stashed(self) -> None:
        self.tmux.new_session("keep", "sleep 300")
        prior = 'run-shell "env TX_IDE_HOME=/elsewhere python3.14 -m tx hook session-closed"'
        self.tmux.run("set-hook", "-g", "session-closed", prior, check=True)
        result = self.installer.claude_sh("install", "--settings", str(self.engines_copy({})), env=self.engines_socket_env())
        self.assertEqual(0, result.code)
        self.assertFalse(self.engines_prev_hook_file().exists())
        self.assertNotIn("stashed an existing hook", result.out)
        self.assertEqual(self.installer.tmux_session_closed(), self.engines_session_closed_hook())

    def test_t_inst_35_no_tmux_server(self) -> None:
        result = self.installer.claude_sh("install", "--settings", str(self.engines_copy({})), env=self.engines_socket_env())
        self.assertEqual(0, result.code)
        self.assertIn(
            engines_status_line("tmux global session-closed", "no tmux server — will apply on next start"), result.out
        )
        self.assertEqual([], self.tmux.sessions())

    # ----- T-INST-36 ---------------------------------------------------------------------------

    @hook_step_reachable
    def test_t_inst_36_uninstall_restores_stashed_hook(self) -> None:
        self.tmux.new_session("keep", "sleep 300")
        self.tmux.run("set-hook", "-g", "session-closed", 'run-shell "echo user"', check=True)
        copy = self.engines_copy({})
        self.assertEqual(0, self.installer.claude_sh("install", "--settings", str(copy), env=self.engines_socket_env()).code)
        self.assertTrue(self.engines_prev_hook_file().exists())
        result = self.installer.claude_sh("uninstall", "--settings", str(copy), env=self.engines_socket_env())
        self.assertEqual(0, result.code)
        self.assertEqual('run-shell "echo user"', self.engines_session_closed_hook())
        self.assertFalse(self.engines_prev_hook_file().exists())
        self.assertIn(engines_status_line("tmux global session-closed", "restored prior hook"), result.out)

    @hook_step_reachable
    def test_t_inst_36_uninstall_unsets_without_stash(self) -> None:
        self.tmux.new_session("keep", "sleep 300")
        copy = self.engines_copy({})
        self.assertEqual(0, self.installer.claude_sh("install", "--settings", str(copy), env=self.engines_socket_env()).code)
        self.assertEqual(self.installer.tmux_session_closed(), self.engines_session_closed_hook())
        self.assertFalse(self.engines_prev_hook_file().exists())
        result = self.installer.claude_sh("uninstall", "--settings", str(copy), env=self.engines_socket_env())
        self.assertEqual(0, result.code)
        self.assertEqual("", self.engines_session_closed_hook())
        self.assertIn(engines_status_line("tmux global session-closed", "unset"), result.out)

    # ----- T-INST-37 ---------------------------------------------------------------------------

    def test_t_inst_37_sandbox_skips_live_globals(self) -> None:
        self.tmux.new_session("keep", "sleep 300")
        self.tmux.run("set-hook", "-g", "session-closed", 'run-shell "echo user"', check=True)
        child = self.engines_sleeper()
        pid_file = self.engines_pid_file(child.pid)
        copy = self.engines_copy({})
        result = self.installer.claude_sh("install", "--settings", str(copy), env={"TX_TMUX_SOCKET": None})
        self.assertEqual(0, result.code)
        self.assertEqual('run-shell "echo user"', self.engines_session_closed_hook())
        self.assertFalse(self.engines_prev_hook_file().exists())
        self.assertIsNone(child.poll())
        self.assertIn(
            f"  live-only (skipped here): tmux set-hook -g session-closed '{self.installer.tmux_session_closed()}'",
            result.out,
        )
        self.assertIn(
            f"  live-only (skipped here): stop mx-speaker via {pid_file} (mailbox left on disk, dark)", result.out
        )
        self.assertTrue(self.engines_sidecar(copy).exists())
        self.assertFalse(self.installer.marker.exists())

    # ----- T-INST-38 ---------------------------------------------------------------------------

    def test_t_inst_38_dry_run_install_changes_nothing(self) -> None:
        peon = {"matcher": "", "hooks": [{"type": "command", "command": "peon-ping"}]}
        copy = self.engines_copy({"hooks": {"Stop": [peon]}})
        before = snapshot(self.root)
        result = self.installer.claude_sh("install", "--dry-run", "--settings", str(copy))
        self.assertEqual(0, result.code)
        self.assertEqual(before, snapshot(self.root))
        self.assertEqual(6, result.out.count("  would generate shim "))
        for shim, (event, _) in CLAUDE_SHIMS.items():
            self.assertIn(
                f"  would generate shim {self.installer.claude_shim(shim)}  ({event}, home baked = {self.installer.tx_home})",
                result.out,
            )
        for event, shim in CLAUDE_EVENTS.items():
            self.assertIn(engines_plan_line(event, f"add      {self.installer.claude_shim(shim)}"), result.out)
        self.assertIn(f"  (dry-run — {copy.name} and the marker sidecar unchanged)", result.out)
        self.assertEqual(2, result.out.count("live-only (skipped here)"))

    def test_t_inst_38_dry_run_uninstall_changes_nothing(self) -> None:
        copy = self.engines_installed_copy({"hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "peon-ping"}]}]}})
        before = snapshot(self.root)
        result = self.installer.claude_sh("uninstall", "--dry-run", "--settings", str(copy))
        self.assertEqual(0, result.code)
        self.assertEqual(before, snapshot(self.root))
        self.assertEqual(6, result.out.count("  would remove shim "))
        for shim in CLAUDE_SHIMS:
            self.assertIn(f"  would remove shim {self.installer.claude_shim(shim)}", result.out)
        for event, shim in CLAUDE_EVENTS.items():
            self.assertIn(engines_plan_line(event, f"strip    {self.installer.claude_shim(shim)}"), result.out)
        self.assertIn(f"  (dry-run — {copy.name} and the marker sidecar unchanged)", result.out)
        self.assertEqual(1, result.out.count("live-only (skipped here)"))
        self.assertIn("  mx-speaker is NOT restarted (manual rollback step — install-flip §7)", result.out)

    # ----- T-INST-39 ---------------------------------------------------------------------------

    def test_t_inst_39_status_drift_report(self) -> None:
        copy = self.engines_installed_copy({})
        data = self.engines_read(copy)
        del data["hooks"]["Stop"]
        data["disableWorkflows"] = False
        self.installer.write_settings(data, copy)
        self.installer.claude_shim("work").unlink()
        result = self.installer.claude_sh("status", "--settings", str(copy))
        self.assertEqual(0, result.code)
        self.assertIn(f"  marker: version=3  mode=coexist  home={self.installer.tx_home}", result.out)
        self.assertIn(f"    {'location':<18} {self.engines_sidecar(copy)}", result.out)
        self.assertIn(f"    {'Stop':<18} DRIFT — not in settings", result.out)
        for event, shim in CLAUDE_EVENTS.items():
            if event != "Stop":
                self.assertIn(f"    {event:<18} in sync\n    {'':<18} {self.installer.claude_shim(shim)}", result.out)
        self.assertIn(f"    {'session-closed':<18} (tmux global) {self.installer.tmux_session_closed()}", result.out)
        self.assertIn(f"    {'disableWorkflows':<18} DRIFT — settings differ", result.out)
        for key in ("disableArtifact", "includeGitInstructions", "skillOverrides", "permissions.deny"):
            self.assertIn(f"    {key:<18} in sync", result.out)
        self.assertIn(engines_status_line(str(self.installer.claude_shim("work")), "missing"), result.out)
        for shim in ("start", "pre", "post", "notify", "end"):
            self.assertIn(engines_status_line(str(self.installer.claude_shim(shim)), "present"), result.out)

    def test_t_inst_39_status_without_marker(self) -> None:
        result = self.installer.claude_sh("status", "--settings", str(self.engines_copy({})))
        self.assertEqual(0, result.code)
        self.assertIn("  ! no _tx_ide_managed marker — Claude integration not installed", result.out)

    def test_t_inst_39_status_profile_not_managed(self) -> None:
        copy = self.engines_copy({})
        self.assertEqual(0, self.installer.claude_sh("install", "--no-context-profile", "--settings", str(copy)).code)
        result = self.installer.claude_sh("status", "--settings", str(copy))
        self.assertEqual(0, result.code)
        self.assertIn(f"    {'context profile':<18} not managed", result.out)

    # ----- T-INST-40 ---------------------------------------------------------------------------

    def test_t_inst_40_mx_speaker_stopped(self) -> None:
        self.tmux.new_session("keep", "sleep 300")
        self.installer.write_settings({})
        child = self.engines_sleeper()
        pid_file = self.engines_pid_file(child.pid)
        result = self.installer.claude_sh("install", env=self.engines_socket_env())
        self.assertEqual(0, result.code)
        self.assertEqual(-15, child.wait(timeout=5))
        with self.assertRaises(ProcessLookupError):
            os.kill(child.pid, 0)
        self.assertIn(engines_status_line("mx-speaker", f"stopped (pid {child.pid})"), result.out)
        self.assertEqual(f"{child.pid}\n", pid_file.read_text())
        self.assertTrue(self.installer.marker.exists())

    def test_t_inst_40_mx_speaker_stale_or_absent(self) -> None:
        self.installer.write_settings({})
        exited = subprocess.Popen(["true"])
        exited.wait()
        self.engines_pid_file(exited.pid)
        result = self.installer.claude_sh("install")
        self.assertEqual(0, result.code)
        self.assertIn("  mx-speaker not running (stale pid file left in place)", result.out)
        self.engines_pid_file(None).unlink()
        result = self.installer.claude_sh("install")
        self.assertEqual(0, result.code)
        self.assertIn("  no mx-speaker pid file (already dark)", result.out)

    # ----- T-INST-41 ---------------------------------------------------------------------------

    def test_t_inst_41_codex_shims_content(self) -> None:
        result = self.installer.codex_sh("install")
        self.assertEqual(0, result.code)
        self.assertEqual(
            sorted(f"{shim}.sh" for shim in CODEX_SHIMS),
            sorted(path.name for path in (self.installer.tx_home / "hooks" / "codex").iterdir()),
        )
        for shim, event in CODEX_SHIMS.items():
            path = self.installer.codex_shim(shim)
            self.assertTrue(engines_executable(path), shim)
            text = path.read_text()
            self.assertEqual(engines_codex_shim_text(self.installer.tx_home, event), text, shim)
            self.assertEqual(6, len(text.splitlines()))
            self.assertEqual(
                f'exec env TX_IDE_HOME="{self.installer.tx_home}" PYTHONPATH="{LIB_DIR}" "python3.14" -m tx hook {event} --engine codex',
                text.splitlines()[-1],
            )
            self.assertIn(engines_status_line(f"shim {path}", event), result.out)

    # ----- T-INST-42 ---------------------------------------------------------------------------

    def engines_hooks_json(self) -> Path:
        return self.installer.codex_home / "hooks.json"

    def engines_hooks_json_text(self) -> str:
        return json.dumps(engines_codex_hooks_json(self.installer.tx_home), indent=2) + "\n"

    def test_t_inst_42_hooks_json_written_wholesale(self) -> None:
        self.assertFalse(self.engines_hooks_json().exists())
        result = self.installer.codex_sh("install")
        self.assertEqual(0, result.code)
        self.assertEqual(self.engines_hooks_json_text(), self.engines_hooks_json().read_text())
        self.assertEqual([], backups(self.engines_hooks_json()))
        self.assertIn(f"  → hooks.json written  {self.engines_hooks_json()}", result.out)

    def test_t_inst_42_hooks_json_replaced_with_backup(self) -> None:
        for old in ('{"hooks": {"Stop": []}}\n', "not json {\n"):
            self.engines_hooks_json().write_text(old)
            result = self.installer.codex_sh("install")
            self.assertEqual(0, result.code)
            self.assertEqual(self.engines_hooks_json_text(), self.engines_hooks_json().read_text())
            backup = backups(self.engines_hooks_json())[-1]
            self.assertEqual(old, backup.read_text())
            self.assertIn(f"  → hooks.json replaced (backup .bak.{engines_stamp(backup)})  {self.engines_hooks_json()}", result.out)

    def test_t_inst_42_hooks_json_already_current(self) -> None:
        self.engines_hooks_json().write_text(self.engines_hooks_json_text())
        inode = self.engines_hooks_json().stat().st_ino
        result = self.installer.codex_sh("install")
        self.assertEqual(0, result.code)
        self.assertIn(f"  → hooks.json already current  {self.engines_hooks_json()}", result.out)
        self.assertEqual(inode, self.engines_hooks_json().stat().st_ino)
        self.assertEqual([], backups(self.engines_hooks_json()))

    def test_t_inst_42_hooks_json_symlink_rewritten_at_realpath(self) -> None:
        target = self.root / "dotfiles" / "codex-hooks.json"
        target.parent.mkdir()
        target.write_text("{}\n")
        self.engines_hooks_json().symlink_to(target)
        result = self.installer.codex_sh("install")
        self.assertEqual(0, result.code)
        self.assertTrue(self.engines_hooks_json().is_symlink())
        self.assertEqual(target, Path(os.readlink(self.engines_hooks_json())))
        self.assertEqual(self.engines_hooks_json_text(), target.read_text())
        self.assertEqual(["{}\n"], [backup.read_text() for backup in backups(target)])
        self.assertEqual([], backups(self.engines_hooks_json()))
        self.engines_assert_no_temp_files(target.parent, ".tx-codex.")

    # ----- T-INST-43 ---------------------------------------------------------------------------

    def test_t_inst_43_hooks_json_uninstall_only_if_identical(self) -> None:
        ours = self.root / "ours"
        ours.mkdir()
        (ours / "hooks.json").write_text(self.engines_hooks_json_text())
        result = self.installer.codex_sh("uninstall", env={"CODEX_HOME": str(ours)})
        self.assertEqual(0, result.code)
        self.assertFalse((ours / "hooks.json").exists())
        self.assertIn(f"  → hooks.json removed  {ours / 'hooks.json'}", result.out)

        drifted = self.root / "drifted"
        drifted.mkdir()
        content = engines_codex_hooks_json(self.installer.tx_home)
        content["extra"] = True
        (drifted / "hooks.json").write_text(json.dumps(content, indent=2) + "\n")
        result = self.installer.codex_sh("uninstall", env={"CODEX_HOME": str(drifted)})
        self.assertEqual(0, result.code)
        self.assertEqual(json.dumps(content, indent=2) + "\n", (drifted / "hooks.json").read_text())
        self.assertIn(f"  → hooks.json present but not ours (drift) — left alone  {drifted / 'hooks.json'}", result.out)

        absent = self.root / "absent"
        absent.mkdir()
        result = self.installer.codex_sh("uninstall", env={"CODEX_HOME": str(absent)})
        self.assertEqual(0, result.code)
        self.assertIn(f"  hooks.json (already absent) {absent / 'hooks.json'}", result.out)

    # ----- T-INST-44 ---------------------------------------------------------------------------

    def engines_config_toml(self) -> Path:
        return self.installer.codex_home / "config.toml"

    def test_t_inst_44_config_toml_block_added(self) -> None:
        self.engines_config_toml().write_text('model = "o3"\n')
        result = self.installer.codex_sh("install")
        self.assertEqual(0, result.code)
        backup = backups(self.engines_config_toml())
        self.assertEqual(1, len(backup))
        self.assertEqual('model = "o3"\n', backup[0].read_text())
        self.assertEqual('model = "o3"\n\n' + CODEX_BLOCK, self.engines_config_toml().read_text())
        self.assertIn(
            f"  → config.toml [tui] block appended  {self.engines_config_toml()}  (backup .bak.{engines_stamp(backup[0])})",
            result.out,
        )

    def test_t_inst_44_config_toml_absent_or_empty(self) -> None:
        result = self.installer.codex_sh("install")
        self.assertEqual(0, result.code)
        self.assertEqual("\n" + CODEX_BLOCK, self.engines_config_toml().read_text())
        self.assertEqual([], backups(self.engines_config_toml()))
        self.assertIn(f"  → config.toml [tui] block appended  {self.engines_config_toml()}\n", result.out)
        self.engines_config_toml().write_text("")
        result = self.installer.codex_sh("install")
        self.assertEqual(0, result.code)
        self.assertEqual("\n" + CODEX_BLOCK, self.engines_config_toml().read_text())
        self.assertEqual([], backups(self.engines_config_toml()))

    def test_t_inst_44_config_toml_block_already_present(self) -> None:
        self.engines_config_toml().write_text('model = "o3"\n\n' + CODEX_BLOCK)
        inode = self.engines_config_toml().stat().st_ino
        result = self.installer.codex_sh("install")
        self.assertEqual(0, result.code)
        self.assertIn(f"  → config.toml [tui] block already present  {self.engines_config_toml()}", result.out)
        self.assertEqual(inode, self.engines_config_toml().stat().st_ino)
        self.assertEqual([], backups(self.engines_config_toml()))

    def test_t_inst_44_config_toml_existing_tui_table_warns(self) -> None:
        existing = '[tui]\nstatus_line = ["model"]\n'
        self.engines_config_toml().write_text(existing)
        result = self.installer.codex_sh("install")
        self.assertEqual(0, result.code)
        self.assertIn(
            f"  ! config.toml already has a [tui] table — our marked block would DUPLICATE it; review {self.engines_config_toml()}",
            result.out,
        )
        self.assertEqual(existing + "\n" + CODEX_BLOCK, self.engines_config_toml().read_text())

    # ----- T-INST-45 ---------------------------------------------------------------------------

    def test_t_inst_45_config_toml_block_stripped(self) -> None:
        installed = 'model = "o3"\n\n' + CODEX_BLOCK
        self.engines_config_toml().write_text(installed)
        result = self.installer.codex_sh("uninstall")
        self.assertEqual(0, result.code)
        self.assertEqual('model = "o3"\n', self.engines_config_toml().read_text())
        backup = backups(self.engines_config_toml())
        self.assertEqual(1, len(backup))
        self.assertEqual(installed, backup[0].read_text())
        self.assertIn(
            f"  → config.toml [tui] block stripped  {self.engines_config_toml()}  (backup .bak.{engines_stamp(backup[0])})",
            result.out,
        )
        # block-first variant, no leading newline
        first = self.root / "block-first"
        first.mkdir()
        (first / "config.toml").write_text(CODEX_BLOCK)
        result = self.installer.codex_sh("uninstall", env={"CODEX_HOME": str(first)})
        self.assertEqual(0, result.code)
        self.assertEqual("", (first / "config.toml").read_text())
        # only the first match is stripped
        twice = self.root / "twice"
        twice.mkdir()
        (twice / "config.toml").write_text('model = "o3"\n\n' + CODEX_BLOCK + "\n" + CODEX_BLOCK)
        result = self.installer.codex_sh("uninstall", env={"CODEX_HOME": str(twice)})
        self.assertEqual(0, result.code)
        self.assertEqual('model = "o3"\n\n' + CODEX_BLOCK, (twice / "config.toml").read_text())

    def test_t_inst_45_config_toml_without_block(self) -> None:
        self.engines_config_toml().write_text('model = "o3"\n')
        inode = self.engines_config_toml().stat().st_ino
        result = self.installer.codex_sh("uninstall")
        self.assertEqual(0, result.code)
        self.assertIn(f"  config.toml — no tx-ide block {self.engines_config_toml()}", result.out)
        self.assertEqual('model = "o3"\n', self.engines_config_toml().read_text())
        self.assertEqual(inode, self.engines_config_toml().stat().st_ino)
        self.assertEqual([], backups(self.engines_config_toml()))

    # ----- T-INST-46 ---------------------------------------------------------------------------

    def test_t_inst_46_settings_refuses_live_codex_home(self) -> None:
        copy = self.root / "copy.toml"
        copy.write_text('model = "o3"\n')
        before = snapshot(self.root)
        result = self.installer.codex_sh("install", "--settings", str(copy), env={"CODEX_HOME": None})
        self.assertEqual(1, result.code)
        self.assertEqual(
            "codex.sh: --settings (sandbox) requires a throwaway CODEX_HOME (e.g. CODEX_HOME=$(mktemp -d)) — refusing to write the live ~/.codex/hooks.json\n",
            result.err,
        )
        self.assertEqual("", result.out)
        self.assertEqual(before, snapshot(self.root))
        self.assertFalse((self.installer.user_home / ".codex").exists())

    def test_t_inst_46_settings_with_throwaway_codex_home(self) -> None:
        copy = self.root / "copy.toml"
        copy.write_text('model = "o3"\n')
        result = self.installer.codex_sh("install", "--settings", str(copy))
        self.assertEqual(0, result.code)
        self.assertIn(f"== codex.sh install ==  (sandbox: {copy})", result.out)
        self.assertEqual('model = "o3"\n\n' + CODEX_BLOCK, copy.read_text())
        self.assertEqual(self.engines_hooks_json_text(), self.engines_hooks_json().read_text())
        self.assertFalse(self.engines_config_toml().exists())

    # ----- T-INST-47 ---------------------------------------------------------------------------

    def test_t_inst_47_codex_status(self) -> None:
        self.assertEqual(0, self.installer.codex_sh("install").code)
        update_log = self.installer.tx_home / "codex-update" / "update.log"
        update_log.parent.mkdir(parents=True)
        update_log.write_text("".join(f"line {number}\n" for number in range(1, 13)))
        result = self.installer.codex_sh("status")
        self.assertEqual(0, result.code)
        self.assertIn(f"== codex.sh status ==  CODEX_HOME={self.installer.codex_home}", result.out)
        self.assertIn(f"  hooks.json:              ours  {self.engines_hooks_json()}", result.out)
        self.assertIn(f"  config.toml [tui] block: present  {self.engines_config_toml()}", result.out)
        self.assertIn("  [hooks.state] trust:     none — bypass-first (verification §4)", result.out)
        tail = "".join(f"    line {number}\n" for number in range(3, 13))
        self.assertIn(f"  automatic update log:    {update_log}\n{tail}", result.out)
        self.assertNotIn("line 2\n", result.out)
        for shim in CODEX_SHIMS:
            self.assertIn(engines_status_line(str(self.installer.codex_shim(shim)), "present"), result.out)

    def test_t_inst_47_codex_dry_run(self) -> None:
        before = snapshot(self.root)
        result = self.installer.codex_sh("install", "--dry-run")
        self.assertEqual(0, result.code)
        self.assertEqual(before, snapshot(self.root))
        self.assertIn("== codex.sh install ==  (dry-run)", result.out)
        self.assertEqual(4, result.out.count("  would generate shim "))
        for shim, event in CODEX_SHIMS.items():
            self.assertIn(
                f"  would generate shim {self.installer.codex_shim(shim)}  ({event} --engine codex, home baked = {self.installer.tx_home})",
                result.out,
            )
        self.assertIn(f"  would write {self.engines_hooks_json()} (written)", result.out)
        self.assertIn(f"  would append the marked [tui] block to {self.engines_config_toml()}", result.out)

    # ----- T-INST-50 ---------------------------------------------------------------------------

    def test_t_inst_50_engine_list_filtered_by_binary(self) -> None:
        # setUp's Installer took the fake `codex` off PATH; `install.sh install` only asks
        # `command -v codex`, so a silent stub puts it back (dry-run never executes it).
        codex = self.fakes.bin_dir / "codex"
        codex.write_text("#!/bin/sh\nexit 0\n")
        codex.chmod(0o755)
        result = self.installer.install_sh("install", "--dry-run")
        self.assertEqual(0, result.code, result.err)
        self.assertIn("== engines install ==  driving: claude codex\n", result.out)
        claude_header = result.out.index("\nengine: claude  (claude.sh install)\n")
        claude_banner = result.out.index("== claude.sh install ==")
        codex_header = result.out.index("\nengine: codex  (codex.sh install)\n")
        codex_banner = result.out.index("== codex.sh install ==")
        self.assertLess(claude_header, claude_banner)
        self.assertLess(claude_banner, codex_header)
        self.assertLess(codex_header, codex_banner)
        self.assertNotIn("antigravity", result.out)
        self.assertNotIn("antigravity", result.err)

    def test_t_inst_50_no_engine_binary(self) -> None:
        self.fakes.remove("claude")
        result = self.installer.install_sh("install", "--dry-run")
        self.assertEqual(0, result.code)
        self.assertEqual("install.sh: no engine CLI found on PATH — nothing to install\n", result.err)
        self.assertNotIn("driving:", result.out)
        self.assertEqual("", result.out)

    def test_t_inst_50_registry_unreadable(self) -> None:
        result = self.installer.install_sh("install", "--dry-run", env={"TX_PYTHON": "/nonexistent"})
        self.assertEqual(2, result.code)
        self.assertIn(
            "install.sh: could not read the engine registry with /nonexistent — pass --engine NAME\n", result.err
        )
        self.assertEqual("", result.out)

    # ----- T-INST-51 ---------------------------------------------------------------------------

    def test_t_inst_51_uninstall_and_status_drive_every_engine(self) -> None:
        self.fakes.remove("claude")
        for op in ("uninstall", "status"):
            result = self.installer.install_sh(op, env=self.engines_socket_env())
            self.assertEqual(0, result.code, result.err)
            self.assertIn(f"== engines {op} ==  driving: antigravity claude codex\n", result.out)
            positions = []
            for engine in ("antigravity", "claude", "codex"):
                header = result.out.index(f"\nengine: {engine}  ({engine}.sh {op})\n")
                banner = result.out.index(f"== {engine}.sh {op} ==")
                self.assertLess(header, banner, engine)
                positions.append((header, banner))
            self.assertEqual(sorted(positions), positions)
            self.assertLess(positions[0][1], positions[1][0])
            self.assertLess(positions[1][1], positions[2][0])

    # ----- T-INST-52 ---------------------------------------------------------------------------

    def test_t_inst_52_engine_forcing_and_forwarding(self) -> None:
        claude_copy = self.root / "A.json"
        claude_copy.write_text("{}\n")
        codex_copy = self.root / "B.toml"
        codex_copy.write_text('model = "o3"\n')
        before = snapshot(self.root)
        result = self.installer.install_sh(
            "install",
            "--engine", "codex",
            "--engine", "claude",
            "--dry-run",
            "--claude-settings", str(claude_copy),
            "--codex-settings", str(codex_copy),
        )
        self.assertEqual(0, result.code, result.err)
        self.assertEqual(before, snapshot(self.root))
        lines = result.lines
        self.assertIn("== engines install ==  driving: codex claude", lines)
        codex_header = lines.index("engine: codex  (codex.sh install)")
        codex_banner = lines.index("== codex.sh install ==  (dry-run)")
        self.assertEqual(f"(sandbox: {codex_copy})", lines[codex_banner + 1])
        claude_header = lines.index("engine: claude  (claude.sh install)")
        claude_banner = lines.index("== claude.sh install ==  (dry-run)")
        self.assertEqual(f"(sandbox: {claude_copy})", lines[claude_banner + 1])
        self.assertLess(codex_header, codex_banner)
        self.assertLess(codex_banner, claude_header)
        self.assertLess(claude_header, claude_banner)
        self.assertNotIn("antigravity", result.out)

    def test_t_inst_52_unknown_engine(self) -> None:
        result = self.installer.install_sh("install", "--engine", "nope")
        self.assertEqual(2, result.code)
        self.assertEqual(f"install.sh: no engine script at {ENGINES_DIR}/nope.sh\n", result.err)

    def test_t_inst_52_bad_arguments(self) -> None:
        usage = "usage: install.sh {install|uninstall|status} [--dry-run] [--engine NAME]..."
        result = self.installer.install_sh("install", "--frob")
        self.assertEqual(2, result.code)
        self.assertEqual("install.sh: unknown argument: --frob", result.err.splitlines()[0])
        self.assertIn(usage, result.err)
        result = self.installer.install_sh()
        self.assertEqual(2, result.code)
        self.assertIn(usage, result.err)
        self.assertEqual("", result.out)
