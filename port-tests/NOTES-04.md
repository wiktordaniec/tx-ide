# NOTES-04 — spec vs `bin/tx` for section 04 (ROLE GROUP MSG ART SYNC CLI)

Every case where the spec's Then disagreed with the reference, or where the test had to depart from
the literal Given to make the Then reachable — verified against `lib/tx/` before deciding (the spec
is downstream of the code). Cases not listed matched the spec verbatim. FIX legs (Appendix B
quirks) are separate `..._fixed_...` methods under `@expected_failure_on_python`.

Section-wide harness findings (fixed in the kit on this branch, see the two kit commits):

- `run_tx_inside` / `TxCase.tx_inside` — the "type the command into session s1" recipe: tmux
  `run-shell -t <session>` is synchronous and hands the job `$TMUX`; `TMUX_PANE` is pinned to the
  target's active pane explicitly because on tmux 3.4 the job otherwise inherits the server's stale
  global value and `#S` resolves to whichever session tmux deems current.
- `TmuxServer` boots the private server with `-f /dev/null` (throwaway session, `exit-empty off`):
  the operator's `~/.tmux.conf` (which on this host sets `base-index 1` / `pane-base-index 1` and
  sources the live tx-ide config with its hooks) otherwise leaks into every topology assertion.

## ROLE

Spec vs `bin/tx` disagreements found while writing `test_role.py`: **none**. Every Then/Edge line of
T-ROLE-01..14 matched `lib/tx/roles.py` / `lib/tx/skills.py` / `SpawnCommand` behaviour as run.

Legs deliberately not tested (per the spec itself):

- T-ROLE-12 antigravity leg (`.agents/skills`, `antigravity.py`) — deferred engine (D3).
- T-ROLE-14 "non-git cwd" — unreachable through a verb (`spawn_worker` refuses with
  `could not create worktree` before linking); the linked-worktree `--cwd` variant is tested.

Q26 FIX legs (T-ROLE-09 / -11 / -13) are split: the parity method asserts exit 1, worktree removed,
no record, message present (true on the reference, which leaks a traceback); the
`@expected_failure_on_python` `_fixed_` method adds `no "Traceback" on stderr`.

## GROUP

No case where the spec's Then disagreed with `bin/tx`. All 12 cases assert the spec text verbatim
(`own:      <g|—>\nresolved: <g>\n`, `Grouped …`, `Cleared …`, argparse errors, log `type`/`msg`/`actor`).

Observations (not disagreements):
- T-GROUP-06 "alive" is the RECORD state (`Session.is_alive()` = non-terminal), not tmux liveness —
  `tx group NAME` never reconciles, so an `idle` record with no tmux session still ranks as alive.
- T-GROUP-12 log `actor` is `$TX_SESSION_ID` when set (tests run with `TX_SESSION_ID=s1`); with it
  unset and no `$TMUX` the actor is the `user` sentinel (cli.py `ArtifactCommand._actor`).

## MSG

No case where the spec's Then disagreed with `bin/tx`. T-MSG-05..14 are DROPPED (no tests).

Observations (not disagreements):
- Delivery is observed through `capture-pane` on the `cat` target: the envelope shows twice (typed
  echo + `cat`'s echo after Enter), exactly as T-MSG-02 states. The literal `send-keys -t t1 --` argv
  of T-MSG-04 is not an observable surface; the pane capture pins the same outcome.
- T-MSG-02 `Enter`-body leg is `expected_failure_on_python` per the spec marker; it passes against
  the reference too (`TX_IMPL=rust` run verified) — the marker only skips, as the spec says.
- The "harness types the command into s1" recipe is realised with the kit's `tx_inside` (tmux
  `run-shell -t s1`): `$TMUX` set, `#S` == `s1`, synchronous.

## ART

Cases where the spec's Then and the reference implementation disagree, or where the test had to
depart from the literal Given to make the Then reachable. Verified against `lib/tx/artifact*.py`,
`cli.py::ArtifactCommand`, `render.py`.

- **T-ART-06 (edge)** — spec: `tx artifact doctor` on the scan-tolerance store reports ONLY the
  `x: content directory …` line. Code: `doctor` also cross-checks every touch against `log.jsonl`,
  so a hand-crafted `good.json` (no `artifact-create` line) would add a `rev 0 touch has no
  matching EventLog mutation line` problem. Asserted the spec's single line by giving the crafted
  record the create log line a real `tx artifact create` leaves (`{"type":"artifact-create",
  "msg":"good plan.md"}`) — the store scan behaviour under test is unchanged.
- **T-ART-22 (parity leg)** — spec marks the whole case `expected_failure_on_python` for the
  `resolve_id("")` fix. On Python the empty token prefix-matches every id: with three artifacts it
  already yields the ambiguous error (exit 1, stdout empty), so only the single-artifact store
  distinguishes the fix. Split per B3: `test_t_art_22_id_resolution` (exact / prefix / ambiguous /
  unknown + `modify`/`group`/`diff`/`open` by prefix) runs against the reference;
  `test_t_art_22_fixed_empty_token_never_resolves` carries the marker and asserts `""` → exit 1 on
  both the three- and the one-artifact store.
- **T-ART-24 (edge, 40-char title)** — spec: "first 27 chars + `…`" — confirmed (`_trunc` keeps
  `width-1` chars); note the `{:<28}` column then has NO padding before the author chips, so the row
  is `…` immediately followed by ` [s1]` (one space, from `_chips`), not the two-space gap the
  fixed-width example rows show.
- **T-ART-25 / T-ART-02 / T-ART-22 FIX legs** — asserted as the spec states (exit 1, empty stdout,
  a `tx artifact: …` stderr line, no `Traceback`); on Python they are skipped by the marker. The
  reference leaks `FileNotFoundError` (missing `current.md`), `UnsupportedArtifactError` (named v1
  record) and resolves `""` on a one-artifact store.
- **T-ART-28 `--repair` re-run report** — spec: "`A: removed orphan rev file 5` lines first, then the
  (re-run) doctor report". Confirmed; the re-run report is in uuid order with A's block gone, so the
  expectation is the T-ART-17 list minus A's line (the spec's letter labels are not id order).

Harness note: `tmux run-shell -t <session>` on tmux 3.4 does not set `TMUX_PANE` for the job — it
inherits the server's stale global value (the outer pane the test process ran in), so `#S` inside
`tx` resolved to whichever session tmux deemed current once a second session existed. Found while
writing T-ART-21; `txkit.run_tx_inside` now pins `TMUX_PANE` to the target's active pane.

## SYNC

Spec Then vs real `bin/tx` behaviour, verified against `lib/tx/sync.py`, `lib/tx/storage.py`,
`lib/tx/cli.py::SyncCommand`.

- **T-SYNC-10** — spec: `Given: —`, `tx sync push --s3 mybucket/pre` → the deferred-S3 error, exit 1.
  Code: `_copy_direction` only touches the remote per local corpus key, so with an EMPTY local corpus
  (the bare kit home: no records, no history, no `log.jsonl`, no `config.json`) the push completes
  vacuously — stdout `sync push (local → s3://mybucket/pre): 0 added, 0 updated, 0 unchanged`, exit 0.
  Asserted: the error leg with one local record seeded (`test_t_sync_10_s3_deferred_error_text`), and
  the vacuous exit-0 behaviour explicitly as its own method (`test_t_sync_10_s3_empty_local_corpus`)
  alongside the spec's own edge (status with an empty corpus still hits `remote.list()` on the pull
  count). The port must reproduce both.
- **T-SYNC-07 (ftp backend)** — spec Then is the Q9/Q26 FIX (`tx sync: unknown sync backend 'ftp' …`,
  exit 1, no traceback). Python leaks a `ValueError` traceback from `remote_from_spec` for both `status`
  and `push` (verified with `TX_IMPL=rust`: the assertion fails on the reference). Asserted the fixed
  behaviour in `test_t_sync_07_fixed_unknown_backend` under `@expected_failure_on_python`; the other
  config variants are parity methods.
- **T-SYNC-08 golden** — the spec's `/tmp/arch` and `<home>` are illustrative; the remote lives under
  the test root, so the captured text normalises the remote path to `<arch>` and the home to `<home>`
  before comparison (`port-tests/golden/sync/08.txt`). Not a behaviour difference.
- **T-SYNC-09 precedence** — the spec says `--s3` beats `--remote`, both beat config, without naming the
  observable. Pinned through `tx sync status` (push with `--s3` would need a local key to reach the
  stub — see T-SYNC-10): `--remote X --s3 b` → the `s3://b` deferred line; `--remote X` with an s3
  config → the `X` count line; config alone → the `s3://cfg` deferred line.

## CLI

Cases where the spec's Then disagreed with the reference; the tests assert the code's behaviour.

- **T-CLI-19** — spec: rows `sh1` then `w1` ("newest-activity first"). Code: `_by_recent_activity`
  sorts by `activity_at` (llm `last_activity`, else `created_at`) desc; with `w1.last_activity =
  now−90` and `sh1.created_at = now−3h` the feed is `w1` then `sh1`. Asserted the code order (the
  spec's own timestamps contradict its row order).
- **T-CLI-21 (pane gone)** — spec: empty stdout. Code: `focus_attrs` only returns None when the
  `display-message` expansion is empty; tmux (3.4) expands a missing `-t %999` target to empty
  fields with exit 0, so the envelope is printed with every value empty except `pane-id`. Asserted
  that shape, exit 0.
- **T-CLI-21 (`@remote-session` pane)** — spec: "pane with `@remote-session host`". Code:
  `Tmux.show_option` reads `show-options -vqt <pane>` WITHOUT `-p`, i.e. the pane's SESSION scope;
  a pane-scoped option (which is what `tx attach --host` writes with `set-option -p`) is invisible
  to `focus-envelope`. The test sets the option at session scope on `Views` (the only scope the
  reference observes) and asserts `inner-remote='1' inner-session-name='host'` + no inner-kind join.
  Port should decide which scope is right (probably `-p`); flagged, not pinned.
- **T-CLI-21 (window/pane indices)** and **T-CLI-09/10/19 (`main[1]`)** — hold only when the private
  server runs without the operator's `~/.tmux.conf` (this host sets `base-index 1` /
  `pane-base-index 1`, which leaked into `window-index` / `[pane]`). The kit's `TmuxServer` now
  boots config-free (`-f /dev/null`), so every area's topology assertions are hermetic.
- **T-CLI-26** — on the reference `tx resume <exited e1 id>` (no `--as`) does not report the live
  same-name clash (`has_session('e1')` is false — tx sessions are named by id) and falls through to
  the cwd error. Parity leg asserts the `--as e2` / `--cwd` cwd errors; the clash message is the
  FIX leg (`@expected_failure_on_python`).
- **T-CLI-03 (`--cmd zsh --prompt ""`)** — reference spawns a shell (Q16 quirk); only the FIX leg
  is written (`@expected_failure_on_python`).
- **T-CLI-02 (`--cwd /nonexistent`)** — tmux 3.4 tolerates the missing dir (Q21 PARITY); asserted
  the spawn succeeds with `cwd=/nonexistent`.
- **T-CLI-06** — `#{pane_start_command}` comes back double-quoted by tmux; the test strips the
  quotes before splitting. On this host the wrapped command fits under `MAX_COMMAND_BYTES`, so no
  `launch/<id>.sh` is written (the test handles both surfaces).
- **T-CLI-07** — spec spawns `ed` three times; live names must be unique, so the test uses `ed`,
  `ed2`, `ed3`.

### Kit notes

- The config-free server boot and the `TMUX_PANE` pinning this file relied on now live in
  `txkit.py` (see the section-wide findings above); `test_cli.py` uses the kit's `tx_inside`.
- No `ssh` recorder among the fakes; `test_t_cli_27_attach_host_runs_ssh` writes its own.
