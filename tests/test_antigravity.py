"""Tests for the Antigravity (`agy`) engine adapter — the repo's test-suite start.

Pytest-style plain-assert functions, runnable without pytest too:

    PYTHONPATH=lib python3.14 tests/test_antigravity.py

The fork surgery is the one deliberately version-fragile piece (an unsupported on-disk format), so
it gets the dedicated fixture coverage the parity checklist mandates: a synthetic conversation db
mirroring agy's real schema (text ids + protobuf-ish blobs, including non-UTF-8 bytes), rewritten
and then verified for full replacement, unchanged row counts, and zero value-length drift.
`tests/live_smoke_antigravity.py` is the opt-in live half (real agy, real db, real fork).
"""

from __future__ import annotations

import shlex
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from tx.engines import antigravity
from tx.engines.antigravity import AntigravityEngine, _rewrite_embedded_id, _strip_identity
from tx.engines.engine_adapter import EngineError

SOURCE_ID = "c462751c-1111-2222-3333-444455556666"
OTHER_ID = "0a0a0a0a-bbbb-cccc-dddd-eeeeffff0000"


# ----- fork surgery fixture ------------------------------------------------------------------


def _build_fixture_db(path: Path) -> dict[str, int]:
    """A miniature of agy's conversation schema with the source uuid embedded the ways the real db
    embeds it: as bare TEXT ids, inside larger text, and inside binary blobs beside non-UTF-8
    bytes (the protobuf case — same-length replace must leave surrounding bytes untouched)."""
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE trajectory_meta (trajectory_id text, cascade_id text, "
        "trajectory_type integer, PRIMARY KEY (trajectory_id))"
    )
    connection.execute(
        "CREATE TABLE steps (idx integer, metadata blob, step_payload blob, PRIMARY KEY (idx))"
    )
    connection.execute("CREATE TABLE gen_metadata (idx integer, data blob, PRIMARY KEY (idx))")
    connection.execute(
        "INSERT INTO trajectory_meta VALUES (?, ?, 1)", (SOURCE_ID, SOURCE_ID)
    )
    binary_with_id = b"\x08\x96\x01\x12$" + SOURCE_ID.encode() + b"\xff\xfe\x00tail"
    connection.execute(
        "INSERT INTO steps VALUES (0, ?, ?)",
        (binary_with_id, f"cascade_id: {SOURCE_ID} more".encode()),
    )
    connection.execute(
        "INSERT INTO steps VALUES (1, ?, NULL)", (b"no id here \xde\xad\xbe\xef",)
    )
    connection.execute(
        "INSERT INTO gen_metadata VALUES (0, ?)",
        (SOURCE_ID.encode() + b"|" + OTHER_ID.encode(),),
    )
    connection.commit()
    counts = {
        table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("trajectory_meta", "steps", "gen_metadata")
    }
    connection.close()
    return counts


def test_rewrite_replaces_every_embedded_occurrence() -> None:
    new_id = str(uuid.uuid4())
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "conversation.db"
        counts_before = _build_fixture_db(db)
        _rewrite_embedded_id(db, SOURCE_ID, new_id)

        connection = sqlite3.connect(db)
        # Full replacement: the source uuid survives nowhere, in text or blob form.
        raw = db.read_bytes()
        assert SOURCE_ID.encode() not in raw
        assert raw.count(new_id.encode()) >= 4  # 2 text ids + 2 blob embeddings

        trajectory_id, cascade_id = connection.execute(
            "SELECT trajectory_id, cascade_id FROM trajectory_meta"
        ).fetchone()
        assert trajectory_id == new_id and cascade_id == new_id

        # Same-length replace: surrounding protobuf-ish bytes are untouched, lengths identical.
        metadata, payload = connection.execute(
            "SELECT metadata, step_payload FROM steps WHERE idx = 0"
        ).fetchone()
        assert metadata == b"\x08\x96\x01\x12$" + new_id.encode() + b"\xff\xfe\x00tail"
        assert payload == f"cascade_id: {new_id} more".encode()

        # A row without the id, and a DIFFERENT embedded uuid, are untouched.
        untouched = connection.execute(
            "SELECT metadata FROM steps WHERE idx = 1"
        ).fetchone()[0]
        assert untouched == b"no id here \xde\xad\xbe\xef"
        data = connection.execute("SELECT data FROM gen_metadata").fetchone()[0]
        assert data == new_id.encode() + b"|" + OTHER_ID.encode()

        counts_after = {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in counts_before
        }
        assert counts_after == counts_before
        connection.close()


# ----- flag grammar --------------------------------------------------------------------------


def test_strip_identity_drops_identity_prompt_and_workspace_binding() -> None:
    binary, inherited = _strip_identity(
        "agy --conversation abc --add-dir /old/wt --log-file /old.log "
        "--model gemini-3.7-flash-high --dangerously-skip-permissions -i 'baked prompt'"
    )
    assert binary == "agy"
    assert inherited == ["--model", "gemini-3.7-flash-high", "--dangerously-skip-permissions"]


def test_strip_identity_go_flag_grammar() -> None:
    # Single-dash / double-dash equivalence, `=` values, bare -c identity, unknown value flags.
    binary, inherited = _strip_identity(
        "agy -conversation=abc -c -model=slug --unknown-flag value --sandbox 'stray positional'"
    )
    assert binary == "agy"
    assert inherited == ["-model=slug", "--unknown-flag", "value", "--sandbox"]


def test_strip_identity_truncates_shell_wrapping() -> None:
    binary, inherited = _strip_identity("agy --model slug && rm -rf /")
    assert binary == "agy"
    assert inherited == ["--model", "slug"]


# ----- launch / access shapes ----------------------------------------------------------------


def test_effort_renders_into_slug_and_rejects_missing_tiers() -> None:
    adapter = AntigravityEngine()
    assert adapter.build_launch_command(effort=1, env={})[:3] == [
        "agy", "--model", "gemini-3.7-flash-low",
    ]
    assert "--effort" not in adapter.build_launch_command(effort=3, env={})
    for unsupported in (4, 5):
        try:
            adapter.build_launch_command(effort=unsupported, env={})
        except EngineError:
            continue
        raise AssertionError(f"effort {unsupported} should raise")
    # An explicit model wins verbatim — no suffixing, no --effort, no tier check.
    explicit = adapter.build_launch_command(model="claude-opus-4-6-thinking", effort=5, env={})
    assert explicit[:3] == ["agy", "--model", "claude-opus-4-6-thinking"]
    assert "--effort" not in explicit


def test_prompt_rides_dash_i_never_positional() -> None:
    adapter = AntigravityEngine()
    command = adapter.build_launch_command(effort=2, initial_prompt="do it", env={})
    assert command[-2:] == ["-i", "do it"]
    seeded = adapter.seed_command("agy --model slug --dangerously-skip-permissions", "the seed")
    assert seeded[-2:] == ["-i", "the seed"]


def test_read_only_shape_and_predicate() -> None:
    adapter = AntigravityEngine()
    read_only = shlex.join(adapter.build_launch_command(effort=1, read_only=True, env={}))
    writable = shlex.join(adapter.build_launch_command(effort=1, env={}))
    assert adapter.is_read_only_command(read_only)
    assert not adapter.is_read_only_command(writable)
    # Every disqualifier: skip-permissions present, --sandbox present, --mode plan absent.
    assert not adapter.is_read_only_command("agy --mode plan --dangerously-skip-permissions")
    assert not adapter.is_read_only_command("agy --mode plan --sandbox")
    assert not adapter.is_read_only_command("agy --model slug")
    assert adapter.is_read_only_command("agy --mode=plan")


def test_resume_and_fork_carry_persona_without_source_identity() -> None:
    adapter = AntigravityEngine()
    source = "agy --conversation old-id --model slug --dangerously-skip-permissions -i seed"
    resume = adapter.resume_command("the-chat", source_cmd=source)
    assert resume[:3] == ["agy", "--conversation", "the-chat"]
    assert "--model" in resume and "-i" not in resume and "old-id" not in resume
    assert resume.count("--dangerously-skip-permissions") == 1


if __name__ == "__main__":
    failed = 0
    for name, function in sorted(globals().items()):
        if name.startswith("test_") and callable(function):
            try:
                function()
                print(f"ok   {name}")
            except AssertionError as error:
                failed += 1
                print(f"FAIL {name}: {error}")
    sys.exit(1 if failed else 0)
