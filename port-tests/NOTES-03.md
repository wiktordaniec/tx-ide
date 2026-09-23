# NOTES-03 — section 03 (ENG HOOK HIST CHAT): spec Then vs real bin/tx

One section per area. Each table lists the cases where the spec's Then disagreed with the
reference (`lib/tx/` verified first), what the tests assert instead, and why. Harness notes record
Given-level choices that are not disagreements. Branch `feat/port-tests-03`.

## ENG (T-ENG-01 … T-ENG-44; antigravity cases deferred, D3)

Spec Then vs real `bin/tx` (verified against `lib/tx/engines/claude.py`, `codex.py`,
`codex_update.py`, `service.py`, `chat.py`, `cli.py`).

| case | spec said | bin/tx does | asserted | why |
|---|---|---|---|---|
| T-ENG-19 | `tx resume Sᵣₒ` (Sᵣₒ = RO shape + persona) → `["claude","--resume","c1","--model","opus","--effort","high","--no-chrome", <RO block>]` | the whole persona is inherited, `--append-system-prompt P` included: `[…,"--no-chrome","--append-system-prompt","P", <RO block>]` (`_strip_identity` keeps every value-flag; only the access flags are stripped and re-applied) | the list WITH `--append-system-prompt P`, exactly one `--permission-mode`, no `--dangerously-skip-permissions`, `env.TX_READ_ONLY == "1"` | the spec's list dropped the priming flag its own Given put into Sᵣₒ; the code is the reference |
| T-ENG-20 | reject leg: stderr `tx resume: claude command does not enforce the requested read-only mode` | that line, preceded by the transcript warning when the chat's transcript is not on disk | exact stderr with the transcript on disk (Given amended so only the error line prints) | the warning is T-CHAT-18's edge, not this case's |
| T-ENG-23 | "the copy exists before the fake launches" (a launch-time stat by the fake) | `prepare_chat_for_cwd` runs in `before_spawn`, ahead of `tmux new-session` | copied transcript `st_ctime <= dump["at"]` (the fake's start time) | the shared fake cannot stat at launch; ctime ordering is the black-box equivalent |
| T-ENG-41 | "`tx spawn` returned before the child finished (exit 0 immediately)" | the update child is a detached `Popen` | stub `curl` blocks on a gate file; `tx spawn` returns with the log lacking `Update succeeded`, then the gate is released and the log completes | deterministic proof of detachment without a wall-clock bound |

Harness notes (no deviation):

- F-ENG uses `home_options={"link_agents": False}` and writes a PLAIN `agents/` dir (`COMMON.md` body
  `C` without frontmatter, `roleP.md`, skill `foo`), so an engine spawn carries no `TX_SKILLS` and the
  record `env` is exactly `{"TX_REQUIRE_WORKTREE":"1"}` as T-ENG-10 states. With the installer's
  `agents → <repo>/agents` link the COMMON grant would add `TX_SKILLS=tx-sessions,tx-artifacts`.
- `argv(N)` is the fake's dump with `argv[0]` reduced to its basename (the fake lives at a temp path);
  T-ENG-14's "binary preserved" leg compares the full path. The T-ENG-16 golden stores the normalised
  argv, one JSON list per line.
- T-ENG-03 `--engine claude --cmd 'codex -m x'`: the record is stamped `claude` but the pane runs the
  codex fake, so the dump read is `codex-<id>.json`.
- Distiller-path handovers (T-ENG-13, T-ENG-35) leave a detached `_chat-op-watch` (600 s timeout);
  the tests `pkill -f "_chat-op-watch <op_id>"` in cleanup.
- T-ENG-13/19/35 seed texts are built from chat.py's f-strings with `TX_BIN` (txkit's resolved
  binary) as the tx program; for the Python reference that equals chat.py's own `<repo>/bin/tx`.
- T-ENG-39–42 build the standalone layout under the temp root: `<root>/<label>/cx` as
  `--env CODEX_HOME`, `<root>/<label>/bin` (symlink `codex` + stub `curl`) prepended to the run PATH
  via `self.tx(env={"PATH": …})`. The stub `curl` records argv+env to `<root>/curl-out/`, honours
  `-o` (writes `echo ok`), and takes `CURL_STUB_GATE` / `CURL_STUB_FAIL` knobs.
- T-ENG-30 variant 4 ("no `CODEX_HOME` anywhere") unsets the kit's `CODEX_HOME` with
  `env={"CODEX_HOME": None}`.
- T-ENG-01/02 argparse texts match the spec verbatim on Python 3.14 (`invalid choice: '0' (choose
  from '1', '2', '3', '4', '5')`).

## HOOK (T-HOOK-01 … T-HOOK-21)

Spec Then vs real `bin/tx` (verified against `lib/tx/hooks.py`, `service.py::record_state`,
`reconcile.py`, `history.py`). Nothing in HOOK contradicts the reference; two Then lines needed a
reading choice, recorded here.

| case | spec said | bin/tx does | asserted | why |
|---|---|---|---|---|
| T-HOOK-01 | "garbage stdin" is one of the paths where the store stays untouched | garbage stdin on a capture event with a VALID `TX_SESSION_ID` still flips the record to WORKING (`_capture_chat_ref` raise is swallowed, the state arm runs — T-HOOK-20) | garbage stdin on the id-less / unknown-id / unknown-event paths (store + log untouched); the valid-id leg is T-HOOK-20 | the "store untouched" Then only holds when dispatch no-ops before the state arm |
| T-HOOK-10 | record EXITED, `ended_at` set, exit 0 | additionally appends a `reconcile` line `<name> → exited (vanished)` to `log.jsonl` (`Reconciler._mark_exited`) | the spec's Then plus that log tail | provenance line is part of the observable contract (H8) |

Harness notes:
- ">1 s gap / mtime unchanged" edges backdate the record file with `os.utime` instead of sleeping.
- "does NOT appear within a bounded wait" edges poll for the spec's 2 s and then assert absence.
- The 50 MB latency edge (T-HOOK-07) asserts `tx hook stop` returns in < 1.0 s as the spec states; on this
  4-core host it returns in well under that, but it is the one wall-clock bound in the area.

## HIST (T-HIST-01 … T-HIST-10)

Spec Then vs real `bin/tx` (verified in `lib/tx/history.py`, `render.py`, `cli.py`, `service.py`).

| case | spec said | bin/tx does | asserted | why |
|---|---|---|---|---|
| T-HIST-10 | pending row `  pending   fork      fork←d2d2d2d2         3d ago   —` (9 spaces before `3d`) | `"  {:<8}  {:<9} {:<18} {:>4} ago   {}"` pads the 13-char origin `fork←d2d2d2d2` to 18 (5 spaces) + 1 separator + `{:>4}` → `  3d`: 8 spaces before `3d` | the 8-space row (literal + golden `golden/hist/10.txt`) | the spec's own format string produces 8; the block's extra space is a transcription slip, the code is the reference |
| T-HIST-09 | unknown → exit 1 `tx archive: ...` | stderr `tx archive: session 'nope' not found (no live @tx_id, no store record)` | exit 1, stderr starts `tx archive: ` and names `'nope'`, empty stdout, no log line | the spec leaves the tail as `...`; asserting the prefix + name keeps the port free to word it |

Harness notes (no deviation, but the spec's Given is under-specified):

- T-HIST-07 "yet record `bundle_path` stamped (dir returned)": after the Given's first ingest the stamp is already there, so the test clears `bundle_path` to null before holding the lock — the coalescing `tx hook ingest` must re-stamp it without touching the bundle bytes.
- T-HIST-08 rename edge: `ArchiveCommand` calls `service.archive()` (saves ARCHIVED + logs) BEFORE the blocking `ingest_session(wait=True)`, so the test renames the record only once the file shows `state == "archived"` and asserts the rename happened while the lock was still held; the fresh reload in `_stamp_bundle_paths` then preserves `name == "renamed"` and stamps `bundle_path`.
- T-HIST-09 "live record R": `tx archive` never reconciles or consults tmux, so liveness is not required for the verb; the test still gives R a live `@tx_id` tmux session (kit `new_session`) to match the Given, and holds the first chat's `.ingest.lock` for 1 s so the forced (blocking) mirror is exercised.
- T-HIST-02 Q7 FIX leg (`test_t_hist_02_fixed_codex_fallback_is_per_engine`) is skipped against the Python reference (D9); the three cwd variants + single/no-match edges run as parity methods.
- "mtime unchanged over a >1 s gap" (T-HIST-04 edge, T-HIST-08) is asserted by backdating the file with `os.utime` and checking the mtime did not move — no sleeps.

## CHAT (T-CHAT-01 … T-CHAT-21)

Where the spec's Then disagreed with real `bin/tx` (verified in `lib/tx/`), what is asserted and why.

| case | spec said | bin/tx does | asserted | why |
|---|---|---|---|---|
| T-CHAT-05 (1) | bundle exists "already when the fake distiller starts (the fake stats it at launch)" | ingest is blocking (`ChatOps.handover` → `ingest_session(wait=True)` before `_spawn_distiller`) | bundle byte-equal to the source when the verb returns | the shared kit fake records argv/env only; a launch-time stat is not reachable black-box. Ordering follows from the blocking call. |
| T-CHAT-05/11 seed | `<q(tx program)>` = "the binary under test's resolved path" | `chat.py::_tx_program()` bakes `<repo>/bin/tx` from its own file location | `shlex.quote(txkit.TX_BIN)` | equal for the reference (`TX_BIN` defaults to `<repo>/bin/tx`, resolved). A port run via `TX_BIN` elsewhere must bake its own resolved path. |
| T-CHAT-09 edge | a spec missing "any other key (e.g. no `worker_name`)" → non-zero exit; "the aborted attempt leaves `claim/` behind" | `ChatOpSpec` gives `worker_name` (and `pane`, `distiller_name`, `self_catch_up`) a default, so a spec without `worker_name` LOADS and spawns a worker named `-2` (Q25 territory), exit 0. A truly required key (`op_id`, `kind`, `source_txid`, `source_chat`, `cwd`, `artifact_path`) missing → `TypeError` traceback, exit 1 — raised by `ChatOpSpec.load` BEFORE the `claim/` mkdir, so no `claim/` is left | fixed leg omits `cwd`: exit 1, `tx _chat-op-finish: …` naming `cwd`, no `Traceback`, no record, no fake launch, no `done`, retry also exit 1. `claim/` presence not asserted | the spec's `claim/`-left-behind sub-assertion contradicts the load-then-claim order; the port keeping that order leaves nothing behind |
| T-CHAT-11 | "S `chats` unchanged" | the blocking ingest stamps `bundle_path` on `c1` (`history._stamp_bundle_paths`) | one chat, id `c1`, `ended_at` null, `bundle_path == <home>/history/<S.id>/c1` | stamping is the documented ingest side effect (F7); nothing else about the chats moves |
| T-CHAT-13 >8192 edge | `#{pane_start_command} == /bin/sh '<home>/launch/<S.id>.sh'` | `transportable_command` uses `shlex.quote`, which leaves a plain path unquoted → `/bin/sh <home>/launch/<S.id>.sh` | `f"/bin/sh {shlex.quote(str(script))}"` | shlex quoting, not literal quotes |
| T-CHAT-13/16/19 `#{pane_start_command}` | the command string itself | tmux 3.4 renders a single-argument command double-quoted (`args_escape`): `"env K=V … 'seed'"` | `shlex.split(display)` must yield exactly one argument, compared to the expected command | tmux formatting, not tx behaviour; the un-escaped argument is what tx passed |
| T-CHAT-16 | capture-pane shows the stdout line | the kit pane is 80 columns; the line wraps | `capture-pane -J` (joined) | terminal width, not tx |
| T-CHAT-17 stdout | `chat c1c1c1c1` | `chat.id[:8]` | `chat c1` for the fixture id `c1` | the spec's id is a stand-in |
| T-CHAT-18 live-record leg | the `--as` hint (Q6 FIX) | `ResumeCommand` checks `has_session(name)` only; the live record is refused later by `_require_name_free` → `tx resume: session 'w1' already exists`, exit 1, no record/worktree | parity method: exit 1, `tx resume:` prefix, no record / `…--lv-2` worktree / tmux session, then `--as lv2` succeeds; fixed method (skipped on Python) asserts the hint text | D9 split |
| T-CHAT-21 missing spec | Q9 FIX: one `tx …:` line, no traceback | `FileNotFoundError` traceback, exit 1 | parity: exit 1 and no side effects (no `chat-ops/`, no record, no fake launch); fixed leg asserts the message shape | D9 split |
| T-CHAT-12/16 "during the window" | spec observable only in a race window | the detached finish blocks in `ingest_session(wait=True)` while the test holds `history/<S.id>/c1/.ingest.lock` with `flock` | spec read deterministically while the lock is held, then released | makes the edge deterministic without touching tx |
| T-CHAT-15 (all legs), T-CHAT-16 handover edge | `TX_CHAT_OP_POLL_S/GRACE_S/TIMEOUT_S` | module constants 2 s / 20 s / 600 s (Q12) | written against the env contract, `@expected_failure_on_python` | D9; not runnable against the reference |

Harness notes (no spec disagreement):

- Crafted sources without a live tmux session are `state: exited`: reconcile-on-read would otherwise stamp a non-terminal record EXITED and add a `state` log line under the asserted tails.
- The fallback transcript glob (`projects/*/<chat>.jsonl`) is cross-project, so a source whose transcript must be absent (T-CHAT-18 warning edge) carries a chat id no sibling source uses.
- Every distiller path leaves a detached `_chat-op-watch` (600 s); tests `pkill -f "_chat-op-watch <op_id>"` at cleanup, before the kit tears the server down.

