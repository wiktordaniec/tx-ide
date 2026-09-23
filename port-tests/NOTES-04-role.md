# NOTES-04 — ROLE

Spec vs `bin/tx` disagreements found while writing `test_role.py`: **none**. Every Then/Edge line of
T-ROLE-01..14 matched `lib/tx/roles.py` / `lib/tx/skills.py` / `SpawnCommand` behaviour as run.

Legs deliberately not tested (per the spec itself):

- T-ROLE-12 antigravity leg (`.agents/skills`, `antigravity.py`) — deferred engine (D3).
- T-ROLE-14 "non-git cwd" — unreachable through a verb (`spawn_worker` refuses with
  `could not create worktree` before linking); the linked-worktree `--cwd` variant is tested.

Q26 FIX legs (T-ROLE-09 / -11 / -13) are split: the parity method asserts exit 1, worktree removed,
no record, message present (true on the reference, which leaks a traceback); the
`@expected_failure_on_python` `_fixed_` method adds `no "Traceback" on stderr`.
