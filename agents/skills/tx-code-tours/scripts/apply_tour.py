#!/usr/bin/env python3

import argparse
import json
import subprocess
from pathlib import Path


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply code-tour diagnostics to a live nvim session."
    )
    parser.add_argument("socket", help="Path to the nvim RPC socket")
    parser.add_argument("stops", type=Path, help="Lua file returning the ordered stops")
    return parser.parse_args()


def main() -> None:
    arguments = parse_arguments()
    runtime_path = Path(__file__).with_name("apply_tour.lua").resolve()
    lua_expression = "(dofile(_A.runtime))(_A.stops)"
    lua_arguments = {
        "runtime": str(runtime_path),
        "stops": str(arguments.stops.resolve()),
    }
    remote_expression = (
        f"luaeval({json.dumps(lua_expression)}, {json.dumps(lua_arguments)})"
    )
    completed_process = subprocess.run(
        [
            "nvim",
            "--server",
            arguments.socket,
            "--remote-expr",
            remote_expression,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    print(completed_process.stdout.strip())


if __name__ == "__main__":
    main()
