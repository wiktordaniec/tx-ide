"""OPT-IN live fork smoke for the Antigravity engine — NOT part of the unit suite.

Runs the real `agy` against the real `~/.gemini/antigravity-cli`: creates a tiny conversation
holding a codeword, forks it via the db surgery, and verifies the fork both RECALLS the codeword
(history carried) and DIVERGES (new turns land on the fork only, the source db is untouched).
Costs three flash-low print turns and leaves two throwaway conversations behind.

The surgery is version-fragile by nature (an unsupported on-disk format), so this aborts loudly
unless the installed `agy --version` matches the adapter's verified pin:

    PYTHONPATH=lib python3.14 tests/live_smoke_antigravity.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from tx.engines.antigravity import (
    AGY_BIN,
    AGY_VERIFIED_VERSION,
    _fork_conversation_db,
    conversation_db,
)

CODEWORD = f"TX-FORK-{uuid.uuid4().hex[:8].upper()}"


def run_agy(*arguments: str, cwd: str) -> dict:
    """One print-mode turn, JSON output — returns the result envelope."""
    completed = subprocess.run(
        [AGY_BIN, "--output-format", "json", "--add-dir", cwd, "--log-file",
         f"{cwd}/agy.log", "--model", "gemini-3.7-flash-low",
         "--dangerously-skip-permissions", *arguments],
        cwd=cwd, capture_output=True, text=True, timeout=300,
    )
    if completed.returncode != 0:
        sys.exit(f"agy failed: {completed.stderr.strip() or completed.stdout.strip()}")
    return json.loads(completed.stdout.strip().splitlines()[-1])


def main() -> None:
    version = subprocess.run(
        [AGY_BIN, "--version"], capture_output=True, text=True
    ).stdout.strip()
    if version != AGY_VERIFIED_VERSION:
        sys.exit(
            f"ABORT: installed agy is {version}, fork surgery verified against "
            f"{AGY_VERIFIED_VERSION} — re-verify the db layout before trusting this smoke "
            "(then bump AGY_VERIFIED_VERSION)"
        )

    with tempfile.TemporaryDirectory(prefix="tx-agy-smoke-") as directory:
        print(f"1/3 seeding a conversation with codeword {CODEWORD}")
        seeded = run_agy(
            "-p", f"Remember this codeword: {CODEWORD}. Reply with exactly: stored",
            cwd=directory,
        )
        source_id = seeded["conversation_id"]
        print(f"    source conversation {source_id}")

        print("2/3 forking via db surgery")
        source_before = conversation_db(source_id).read_bytes()
        fork_id = _fork_conversation_db(source_id)
        print(f"    fork conversation {fork_id}")

        print("3/3 resuming the fork, checking recall + divergence")
        recalled = run_agy(
            "--conversation", fork_id, "-p",
            "What was the codeword? Reply with the codeword only.",
            cwd=directory,
        )
        response = recalled["response"]
        assert CODEWORD in response, f"fork did not recall the codeword: {response!r}"
        assert recalled["conversation_id"] == fork_id, "turn landed on the wrong conversation"
        assert conversation_db(source_id).read_bytes() == source_before, (
            "source db changed — fork was not isolated"
        )
        assert conversation_db(fork_id).is_file()

    print(f"PASS: fork recalled {CODEWORD}, diverged under {fork_id}, source untouched")
    print(f"      throwaway conversations left behind: {source_id}, {fork_id}")


if __name__ == "__main__":
    main()
