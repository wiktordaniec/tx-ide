# NOTES-01 — spec vs `bin/tx` disagreements (MODEL STORE HOME EVENTS RECON RENDER MIGR)

Every entry: what the spec's Then said, what the reference code does (verified against `lib/tx/`
and a real run), and which behaviour the test asserts. The spec is downstream of the code.

## Cross-cutting

### Q19 / Q26 legs (MODEL-13, STORE-03, STORE-04, HOME-05, HOME-06, RECON-10)
Spec: the FIX edges (skip lines printed once by `tx ls`/`tx history`; no traceback on a named bad
record, malformed `config.json`, unknown/incomplete sync backend).
Code: the reference prints each skip line twice (reconcile scan + listing scan) and leaks
`UnsupportedRecordError` / `JSONDecodeError` / `ValueError` / `KeyError` tracebacks (exit 1,
stdout empty, message on the last traceback line).
Asserted: parity leg asserts exit code, empty stdout and the message text (present in the
traceback); the fixed leg (`@expected_failure_on_python`) asserts "no `Traceback`" / "exactly once".

## MODEL

### T-MODEL-01 / 03 — single-letter spawn names resolve through tmux prefix matching
Spec: `tx spawn s|o|e|w …` then `tx show s|o|e|w`.
Code: `SessionService._resolve` calls `tmux show-options -t <token> @tx_id` before the name lookup; tmux resolves `-t e` by prefix against the uuid-named sessions, so `tx show e` returns whichever live record's uuid starts with `e` (flaky in a real run — observed once in two).
Asserted: the same spawns under names `sh` / `ot` / `nv` / `wk` (a non-hex second character cannot prefix a uuid). The role / state assertions are unchanged.

## STORE

### T-STORE-03 — load
Spec: `tx show bad` (malformed JSON) exits 1 with stderr naming `bad.json`; `tx show v4` stderr contains the unsupported-version message — both without a traceback (Q26 FIX).
Code: `SessionStore.load` propagates `UnsupportedRecordError` / `json.JSONDecodeError`; `cli.main` catches only `ServiceError`/`TmuxError`/`EngineError`, so both leak a Python traceback (exit 1, stdout empty). The `JSONDecodeError` traceback does not mention `bad.json` at all.
Asserted: parity leg — exit 1, empty stdout, and (v4 only) the message in stderr; fixed leg (`@expected_failure_on_python`) — additionally `bad.json` named and no `Traceback`.

### T-STORE-03 — `tx show zzz` stderr
Spec: stderr `tx show: no record for 'zzz'`.
Code: the name fallback (`SessionService._resolve_name`) scans the whole store first, so with `v4.json`/`bad.json` present the skip warnings precede the verdict line.
Asserted: stdout empty, exit 1, and the LAST stderr line is `tx show: no record for 'zzz'`.

### T-STORE-08 — query predicate
Spec: "crafted records in states idle, exited, archived" — the idle record is absent from `tx history`.
Code: `tx history` reconciles first; a non-live IDLE record is driven to EXITED and would then be listed.
Asserted: the idle record is backed by a live `@tx_id` tmux session, so it stays idle and is absent; its file still reads `"state": "idle"` after the run.

## HOME

### T-HOME-02 — `--role NAME` refusal
Spec: the `--role` spawn is refused with `unknown role 'COMMON' (no .md file under /h/user-agents or /h/agents)`.
Code: `SpawnCommand` maps `RoleError` to `parser.error(...)`, so it is an argparse error — exit 2, stderr ends `tx spawn: error: unknown role 'COMMON' (…)` after the usage block (not the exit-1 `ServiceError` path).
Asserted: exit 2 and stderr ending with that exact line.

### T-HOME-05 — malformed `config.json` (e)
Spec: `tx ls` exits 1, empty stdout, stderr names `config.json`, no traceback, no record changed (Q26 FIX).
Code: `Reconciler._stuck_threshold` calls `json.loads` unguarded before the record loop → a `JSONDecodeError` traceback (exit 1, stdout empty); the traceback never names `config.json`. Records are untouched because the threshold is read before any record is visited.
Asserted: parity leg — exit 1, empty stdout, record bytes unchanged, no `log.jsonl`; fixed leg (`@expected_failure_on_python`) — `config.json` in stderr and no `Traceback`.

### T-HOME-06 — bad backend / missing `path`
Spec: `gcs` → exit 1, stderr contains `unknown sync backend 'gcs' (expected 's3' or 'local')`, no traceback; `local` without `path` → exit 1, stderr mentions `path`, no traceback (Q26 FIX).
Code: `sync.remote_from_spec` raises `ValueError` / `KeyError: 'path'` uncaught → tracebacks whose last line carries the message.
Asserted: parity leg — exit 1, empty stdout, message present; fixed leg (`@expected_failure_on_python`) — additionally no `Traceback`. The spec's single-case marker is split per rev 3 (B3) so the a–e/g legs still run against the reference.

## EVENTS

### T-EVENTS-06 — `tx artifact show` does not log `artifact-read`
- Spec: `show`/read → `artifact-read`.
- Code: `ArtifactCommand._show` renders without calling `ArtifactService.content` (the only method that appends `artifact-read`); no CLI verb reaches `content()`.
- Asserted: `tx artifact show` appends nothing; `artifact-read` stays in the allowed catalogue set but is not produced by any exercised verb.

### T-EVENTS-06 / T-EVENTS-02 edge — `artifact-open` actor is the invoker, not the new view
- Spec: `open` → `artifact-open` (`<artifact_id> → <session_id>`, `actor` = that session id — the new view's id per the T-EVENTS-02 edge).
- Code: `ArtifactCommand._open` calls `self.artifacts.opened(artifact_id, self._actor())`; `_actor()` is `$TX_SESSION_ID`, else the current tmux session's `@tx_id`, else `user` — never the spawned view's id. `open` appends three lines: `spawn` (`art-<id8> [nvim] <content dir>`), `bind-artifact` (`art-<id8> → <artifact_id>`), `artifact-open` (`<artifact_id> → <invoker>` with `actor` = invoker).
- Asserted: with `TX_SESSION_ID=sess-x`, the three lines above with actor `sess-x` on all of them (the first two default to the env actor).

### T-EVENTS-06 — verbs not exercised here
`fork`, `rollover`/`rollover-finish`, `handover`/`handover-finish`, `send-message` (needs a tmux client context), `capture-skip` are pinned by sections 03 (CHAT/HOOK) and 04 (MSG). The catalogue subset assertion still covers every line written during the test.

### T-EVENTS-03 — file mode is pinned on a tx-created log
The seed line is written by the test, which would set the mode itself; the test lets `tx rm` create `log.jsonl` first, then rewrites its content with the seed, so the mode assertion (`0o644 & ~umask`) still reflects tx's `os.open` mode. T-EVENTS-01 asserts the same on a log created from absent.

## RECON

### T-RECON-08 — `launch/` removed is recreated by the skeleton
- Spec: `launch/` removed (`rmdir` after `_init-home`) → `tx ls` exits 0 and does not recreate it.
- Code: `cli.main()` calls `ensure_home()` before dispatching any verb, and `ensure_home` creates `launch/`; the reconciler's `if not directory.is_dir(): return` guard is therefore unreachable through the CLI.
- Asserted: exit 0, stdout `PROCESSES`, and `launch/` exists again (empty).

### T-RECON-10 — skip warning count on the reference
- Spec (Q19 FIX): the `bad.json` skip line prints exactly once.
- Code: `tx ls` scans the store twice (`reconcile()` then the listing), so the reference prints it twice.
- Asserted: parity leg ≥ 1 occurrence + no traceback + `abc` exited + `bad.json` untouched; fixed leg (`@expected_failure_on_python`) exactly once and nothing else on stderr.

## RENDER

### T-RENDER-01 — reltime (`-` rows)
- Spec: the literal expected rows for `n` and `z` read `exited      -   0c   /r` (six spaces before `-`).
- Code: `render_history` formats `"  {:<24} {:<8} {:>5}  …"` — `exited` padded to 8, a space, then `-` right-aligned in 5 → seven spaces before `-` (`exited       -`). The `0s`/`1m` rows in the spec are right (two-char ages give six spaces).
- Asserted: the code's spacing (seven spaces); golden `render/01.txt` captured from the reference.

No other RENDER disagreement: every other literal in T-RENDER-02..17 matched `bin/tx` byte for byte.

## MIGR

No case in T-MIGR-01..10 disagreed with `bin/tx`: every Then (stdout lines, re-saved key order, `@tx_view` stamping, skip reasons, idempotence) matched the reference verbatim. T-MIGR-11 is DROPPED (no test).
