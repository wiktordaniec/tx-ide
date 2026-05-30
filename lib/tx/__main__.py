"""Package entry — `python3.14 -m tx` (what `bin/tx` execs).

S1a replaces S0's temporary foundation entry with the real CLI: this module just routes to the
`cli.py` command registry. All argv parsing, dispatch, and rendering live there; all business
logic lives in `SessionService`.
"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
