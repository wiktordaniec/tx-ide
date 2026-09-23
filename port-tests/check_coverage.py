#!/usr/bin/env python3
"""Spec → test coverage check.

Parses the spec working copy, collects every `### T-<AREA>-<nn>` case not marked `DROPPED` /
`DEFERRED`, and reports which have no `test_t_<area>_<nn>` method under `port-tests/`. Exit 1 when
any case is missing.

    python3.14 port-tests/check_coverage.py [--spec PATH] [--tests DIR] [--verbose]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DEFAULT_SPEC = Path("~/.tx-ide/artifacts/15681067-0027-4677-9ffd-c618378aa890/current.md").expanduser()
TESTS_DIR = Path(__file__).resolve().parent

CASE_HEADING = re.compile(r"^### (T-([A-Z]+)-(\d+))\b")
EXCLUDED_MARKER = re.compile(r"^- (DROPPED|DEFERRED)\b")
TEST_METHOD = re.compile(r"^\s*def (test_t_([a-z]+)_(\d+))(?:_\w*)?\s*\(", re.MULTILINE)


def spec_cases(spec_text: str) -> tuple[list[str], list[str]]:
    """(required case ids, excluded case ids) in spec order. A case is excluded when a bullet
    starting `- DROPPED` / `- DEFERRED` appears in its block (up to the next heading)."""
    required: list[str] = []
    excluded: list[str] = []
    current: str | None = None
    current_excluded = False

    def flush() -> None:
        if current is None:
            return
        (excluded if current_excluded else required).append(current)

    for line in spec_text.splitlines():
        heading = CASE_HEADING.match(line)
        if heading:
            flush()
            current = heading.group(1)
            current_excluded = False
            continue
        if line.startswith("#"):
            flush()
            current = None
            continue
        if current is not None and EXCLUDED_MARKER.match(line):
            current_excluded = True
    flush()
    return required, excluded


def method_key(case_id: str) -> str:
    """`T-TMUXCONF-03` → `test_t_tmuxconf_03`."""
    _, area, number = case_id.split("-")
    return f"test_t_{area.lower()}_{number}"


def implemented_keys(tests_dir: Path) -> set[str]:
    keys: set[str] = set()
    for path in sorted(tests_dir.rglob("test_*.py")):
        for match in TEST_METHOD.finditer(path.read_text()):
            keys.add(f"test_t_{match.group(2)}_{match.group(3)}")
    return keys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    parser.add_argument("--tests", type=Path, default=TESTS_DIR)
    parser.add_argument("--verbose", action="store_true", help="also list covered cases")
    arguments = parser.parse_args(argv)

    required, excluded = spec_cases(arguments.spec.read_text())
    implemented = implemented_keys(arguments.tests)
    missing = [case for case in required if method_key(case) not in implemented]
    covered = [case for case in required if method_key(case) in implemented]

    per_area: dict[str, list[int]] = {}
    for case in required:
        area = case.split("-")[1]
        counts = per_area.setdefault(area, [0, 0])
        counts[1] += 1
        if case in covered:
            counts[0] += 1
    for area, (done, total) in per_area.items():
        print(f"{area:<9} {done:>3}/{total:<3}")
    print(f"total     {len(covered):>3}/{len(required):<3}   excluded (DROPPED/DEFERRED): {len(excluded)}")

    if arguments.verbose:
        for case in covered:
            print(f"covered  {case}")
    for case in missing:
        print(f"MISSING  {case}  ({method_key(case)})")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
