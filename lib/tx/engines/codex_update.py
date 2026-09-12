"""Non-blocking, rollback-safe updates for tx-managed standalone Codex launches.

Codex's interactive startup prompt owns the wrong lifecycle for tx: accepting an update exits the
TUI that is the session's foreground process.  Instead, tx suppresses that prompt only when it can
identify the official standalone layout, then schedules this updater beside (never inside) the new
session.  The official installer owns package verification and its atomic ``current`` switch; this
module adds a fetch boundary with a real exit status, post-install validation, and rollback.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


INSTALLER_URL = "https://chatgpt.com/codex/install.sh"
SUCCESS_INTERVAL_SECONDS = 24 * 60 * 60
FAILURE_INTERVAL_SECONDS = 60 * 60
DOWNLOAD_TIMEOUT_SECONDS = 45
INSTALL_TIMEOUT_SECONDS = 10 * 60
UPDATE_CHECK_CONFIGURATION = "check_for_update_on_startup=false"


@dataclass(frozen=True)
class StandaloneInstallation:
    visible_executable: Path
    resolved_executable: Path
    codex_home: Path
    current_link: Path
    release_directory: Path


def standalone_installation(
    binary: str, environment: Mapping[str, str], codex_home: Path
) -> StandaloneInstallation | None:
    """Describe an official standalone install, or None for npm/brew/fixed release binaries.

    The executable is an external boundary.  A managed command must resolve through a symlink onto
    the current release under the same ``CODEX_HOME``; a fixed path to an old release is
    deliberately left alone because updating ``current`` would not update what that command
    launches.
    """
    process_environment = {**os.environ, **environment}
    found_executable = shutil.which(binary, path=process_environment.get("PATH"))
    if found_executable is None:
        return None
    visible_executable = Path(found_executable).absolute()
    try:
        resolved_executable = visible_executable.resolve(strict=True)
        current_link = codex_home.expanduser().absolute() / "packages" / "standalone" / "current"
        release_directory = current_link.resolve(strict=True)
    except OSError:
        return None
    if visible_executable == resolved_executable:
        return None
    if resolved_executable.parent.name == "bin":
        executable_release = resolved_executable.parent.parent
    else:
        executable_release = resolved_executable.parent
    releases_directory = current_link.parent / "releases"
    if not executable_release.is_relative_to(releases_directory):
        return None
    if executable_release != release_directory or resolved_executable.name != "codex":
        return None
    return StandaloneInstallation(
        visible_executable=visible_executable,
        resolved_executable=resolved_executable,
        codex_home=codex_home.expanduser().absolute(),
        current_link=current_link,
        release_directory=release_directory,
    )


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
    installation: StandaloneInstallation,
    environment: Mapping[str, str],
    state_directory: Path,
) -> bool:
    """Start a detached updater.  It lock-checks and rate-limits itself; launch never waits."""
    process_environment = {**os.environ, **environment}
    process_environment["CODEX_HOME"] = str(installation.codex_home)
    process_environment["CODEX_INSTALL_DIR"] = str(installation.visible_executable.parent)
    try:
        state_directory.mkdir(parents=True, exist_ok=True)
        state = _read_state(state_directory / "status.json")
        current_time = time.time()
        if state.get("status") == "checking":
            started_at = state.get("started_at")
            if isinstance(started_at, (int, float)) and (
                current_time - started_at < INSTALL_TIMEOUT_SECONDS + DOWNLOAD_TIMEOUT_SECONDS
            ):
                return True
        elif not _attempt_due(state, current_time):
            return True
        subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--visible-executable",
                str(installation.visible_executable),
                "--resolved-executable",
                str(installation.resolved_executable),
                "--codex-home",
                str(installation.codex_home),
                "--state-directory",
                str(state_directory),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=process_environment,
            start_new_session=True,
            close_fds=True,
        )
    except OSError:
        return False
    return True


def _read_state(path: Path) -> dict:
    """Read updater-owned state tolerantly: it is an on-disk process boundary."""
    try:
        with path.open() as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_state(path: Path, value: dict) -> None:
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=".status.", suffix=".tmp"
    )
    try:
        with os.fdopen(file_descriptor, "w") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary_name, path)
    except BaseException:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
        raise


def _version(executable: Path) -> str | None:
    try:
        version_result = subprocess.run(
            [str(executable), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if version_result.returncode != 0:
        return None
    version_words = version_result.stdout.strip().split()
    if not version_words:
        return None
    version = version_words[-1]
    pattern = r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?"
    return version if re.fullmatch(pattern, version) else None


def _current_executable(current_link: Path) -> Path | None:
    for relative_path in (Path("bin/codex"), Path("codex")):
        candidate = current_link / relative_path
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    return None


def _restore_current(current_link: Path, release_directory: Path) -> None:
    temporary_link = current_link.parent / f".current.tx-rollback.{os.getpid()}"
    temporary_link.unlink(missing_ok=True)
    temporary_link.symlink_to(release_directory, target_is_directory=True)
    os.replace(temporary_link, current_link)


def _attempt_due(state: dict, current_time: float) -> bool:
    completed_at = state.get("completed_at")
    if not isinstance(completed_at, (int, float)):
        return True
    interval = (
        SUCCESS_INTERVAL_SECONDS
        if state.get("status") in ("current", "updated")
        else FAILURE_INTERVAL_SECONDS
    )
    return current_time - completed_at >= interval


def _record_failure(
    *,
    state_path: Path,
    started_at: float,
    previous_version: str | None,
    active_version: str | None,
    message: str,
    rollback: str,
) -> None:
    _write_state(
        state_path,
        {
            "status": "failed",
            "started_at": started_at,
            "completed_at": time.time(),
            "previous_version": previous_version,
            "active_version": active_version,
            "message": message,
            "rollback": rollback,
        },
    )


def run_update(
    installation: StandaloneInstallation, state_directory: Path
) -> None:
    state_path = state_directory / "status.json"
    lock_path = state_directory / "update.lock"
    log_path = state_directory / "update.log"
    state_directory.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_handle:
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        current_time = time.time()
        previous_state = _read_state(state_path)
        if not _attempt_due(previous_state, current_time):
            return

        previous_release = installation.release_directory
        previous_version = _version(installation.resolved_executable)
        _write_state(
            state_path,
            {
                "status": "checking",
                "started_at": current_time,
                "previous_version": previous_version,
            },
        )

        with log_path.open("a") as log_handle:
            timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(current_time))
            log_handle.write(f"\n[{timestamp}] update check\n")
            with tempfile.TemporaryDirectory(prefix="tx-codex-update-") as temporary_directory:
                installer_path = Path(temporary_directory) / "install.sh"
                try:
                    download_result = subprocess.run(
                        [
                            "curl",
                            "-fsSL",
                            "--connect-timeout",
                            "10",
                            "--max-time",
                            str(DOWNLOAD_TIMEOUT_SECONDS),
                            INSTALLER_URL,
                            "-o",
                            str(installer_path),
                        ],
                        stdin=subprocess.DEVNULL,
                        stdout=log_handle,
                        stderr=subprocess.STDOUT,
                        check=False,
                    )
                    download_message = f"installer download exited {download_result.returncode}"
                    download_succeeded = download_result.returncode == 0
                except OSError as error:
                    download_message = f"could not run curl: {error}"
                    download_succeeded = False
                if not download_succeeded:
                    _record_failure(
                        state_path=state_path,
                        started_at=current_time,
                        previous_version=previous_version,
                        active_version=_version(installation.resolved_executable),
                        message=download_message,
                        rollback="not needed; current release was never changed",
                    )
                    return

                installer_environment = dict(os.environ)
                installer_environment["CODEX_NON_INTERACTIVE"] = "1"
                try:
                    installation_result = subprocess.run(
                        ["sh", str(installer_path)],
                        stdin=subprocess.DEVNULL,
                        stdout=log_handle,
                        stderr=subprocess.STDOUT,
                        env=installer_environment,
                        timeout=INSTALL_TIMEOUT_SECONDS,
                        check=False,
                    )
                    installation_message = f"installer exited {installation_result.returncode}"
                    installation_succeeded = installation_result.returncode == 0
                except subprocess.TimeoutExpired:
                    installation_message = f"installer exceeded {INSTALL_TIMEOUT_SECONDS} seconds"
                    installation_succeeded = False
                except OSError as error:
                    installation_message = f"could not run installer: {error}"
                    installation_succeeded = False

        active_executable = _current_executable(installation.current_link)
        active_version = _version(active_executable) if active_executable is not None else None
        if installation_succeeded and active_version is not None:
            update_status = "updated" if active_version != previous_version else "current"
            _write_state(
                state_path,
                {
                    "status": update_status,
                    "started_at": current_time,
                    "completed_at": time.time(),
                    "previous_version": previous_version,
                    "active_version": active_version,
                    "message": installation_message,
                    "rollback": "not needed; previous release remains installed",
                },
            )
            return

        rollback = "not needed; current release stayed valid"
        if active_version is None:
            _restore_current(installation.current_link, previous_release)
            active_version = _version(installation.resolved_executable)
            rollback = f"restored {previous_release}"
        _record_failure(
            state_path=state_path,
            started_at=current_time,
            previous_version=previous_version,
            active_version=active_version,
            message=installation_message,
            rollback=rollback,
        )


def main(arguments_list: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--visible-executable", required=True, type=Path)
    parser.add_argument("--resolved-executable", required=True, type=Path)
    parser.add_argument("--codex-home", required=True, type=Path)
    parser.add_argument("--state-directory", required=True, type=Path)
    arguments = parser.parse_args(arguments_list)
    current_link = arguments.codex_home / "packages" / "standalone" / "current"
    release_directory = (
        arguments.resolved_executable.parent.parent
        if arguments.resolved_executable.parent.name == "bin"
        else arguments.resolved_executable.parent
    )
    run_update(
        StandaloneInstallation(
            visible_executable=arguments.visible_executable,
            resolved_executable=arguments.resolved_executable,
            codex_home=arguments.codex_home,
            current_link=current_link,
            release_directory=release_directory,
        ),
        arguments.state_directory,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
