"""Run the official standalone installer outside Codex's interactive session.

The installer verifies releases and switches `current`; tx only schedules it and keeps its log.
The log's lock spans the detached process, and its mtime limits all attempts to four-hour intervals.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path


INSTALLER_URL = "https://chatgpt.com/codex/install.sh"
CHECK_INTERVAL_SECONDS = 4 * 60 * 60
UPDATE_CHECK_CONFIGURATION = "check_for_update_on_startup=false"


def standalone_executable(
    binary: str, environment: Mapping[str, str], codex_home: Path
) -> Path | None:
    """Only manage the installer's visible symlink through `current`, never pinned releases."""
    executable = shutil.which(binary, path=environment.get("PATH", os.environ.get("PATH")))
    if executable is None:
        return None
    executable = Path(executable).absolute()
    current = codex_home.expanduser().absolute() / "packages" / "standalone" / "current"
    if not executable.is_symlink() or not current.is_symlink():
        return None
    target = Path(os.path.abspath(executable.parent / os.readlink(executable)))
    if target not in (current / "bin/codex", current / "codex"):
        return None
    if not executable.resolve().is_relative_to((current.parent / "releases").resolve()):
        return None
    return executable


def disable_startup_update_check(tokens: list[str]) -> list[str]:
    """Make centrally-managed update behavior authoritative without duplicate config overrides."""
    filtered_tokens = [tokens[0]]
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in ("-c", "--config") and index + 1 < len(tokens):
            configuration_value = tokens[index + 1]
            if configuration_value.partition("=")[0] == "check_for_update_on_startup":
                index += 2
                continue
        if token.startswith("--config="):
            configuration_value = token.removeprefix("--config=")
            if configuration_value.partition("=")[0] == "check_for_update_on_startup":
                index += 1
                continue
        filtered_tokens.append(token)
        index += 1
    filtered_tokens[1:1] = ["-c", UPDATE_CHECK_CONFIGURATION]
    return filtered_tokens


def schedule_update(
    executable: Path,
    codex_home: Path,
    environment: Mapping[str, str],
    state_directory: Path,
) -> None:
    """Claim a due check without waiting; the child inherits the locked log via stdout."""
    state_directory.mkdir(parents=True, exist_ok=True)
    with (state_directory / "update.log").open("a+") as log_handle:
        try:
            fcntl.flock(log_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        last_check = os.fstat(log_handle.fileno())
        if last_check.st_size and time.time() - last_check.st_mtime < CHECK_INTERVAL_SECONDS:
            return
        log_handle.truncate(0)
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        log_handle.write(f"[{timestamp}] Checking for Codex updates\n")
        log_handle.flush()
        try:
            subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve())],
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                env={
                    **os.environ,
                    **environment,
                    "CODEX_HOME": str(codex_home.expanduser().absolute()),
                    "CODEX_INSTALL_DIR": str(executable.parent),
                    "CODEX_NON_INTERACTIVE": "1",
                },
                start_new_session=True,
            )
        except OSError as error:
            log_handle.write(f"Update failed: {error}\n")
            raise


def run_update() -> int:
    try:
        with tempfile.TemporaryDirectory(prefix="tx-codex-update-") as temporary_directory:
            installer_path = Path(temporary_directory) / "install.sh"
            # Download separately: a curl | sh pipeline can hide download failures.
            subprocess.run(
                [
                    "curl", "-fsSL", "--connect-timeout", "10", "--max-time", "45",
                    INSTALLER_URL, "-o", str(installer_path),
                ],
                check=True,
                timeout=60,
            )
            subprocess.run(["sh", str(installer_path)], check=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as error:
        print(f"Update failed: {error}", flush=True)
        return 1
    print("Update succeeded", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(run_update())
