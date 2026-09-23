# NOTES-02 — section 02 (TMUX SPAWN LIFE WT RO ATTACH TMUXCONF EDITOR)

Every case where the spec's Then disagreed with the real `bin/tx` (Python reference, tmux 3.4,
Linux). Each entry: what the spec said, what the code does (verified in `lib/tx/` / `bin/` /
`tmux/`), what the test asserts and why. Harness-level notes come last.

## Cross-cutting

- **`#{pane_start_command}` quoting** (T-TMUX-01/18/20, T-SPAWN-03/05/10/16, T-RO-02, T-ATTACH-07).
  Spec writes the command bare (`= sleep 30`). tmux 3.4 renders a command containing spaces
  args-escaped: `"sleep 30"`, `"/bin/sh <home>/launch/<id>.sh"`, `\$` for `$`. Tests unwrap one
  quote layer / accept an optional leading `"` before comparing exactly; the content is the spec's.
- **`TX_SKILLS` on engine-built worker env** (T-SPAWN-02/12/16, T-WT-01, T-RO-02). Spec:
  `env={"TX_REQUIRE_WORKTREE":"1"}` / `{"TX_READ_ONLY":"1"}`. Code: `cli._resolve_command` runs
  `resolve_role_skills([])` on every `--engine` spawn, so with the SHIPPED `agents/COMMON.md`
  (frontmatter `skills: [tx-sessions, tx-artifacts]`) the record env also carries
  `TX_SKILLS=tx-sessions,tx-artifacts`. SPAWN asserts the real env with the shipped agents dir
  (the kit's default `agents → <repo>/agents` link); WT/RO build the home with
  `link_agents=False` and a grant-less `agents/COMMON.md` so the env matches the spec text
  exactly. A `--cmd` worker never carries `TX_SKILLS` (matches the spec). A port must reproduce
  the grant from the role file, not the literal env in the spec.

- **Short hex session names mis-resolve (reference quirk, not in Appendix B).** `SessionService._resolve`
  calls `Tmux.get_tx_id(name)` = `show-options -vqt <name> @tx_id` with a bare target, and tmux
  PREFIX-matches a bare session target when no exact name exists. A process is tmux-named by its
  uuid, so a display name that is a hex string (`a`, `b`, `d1`, `abc`) resolves to whichever single
  live uuid session starts with it — `tx show b` / `tx kill b` / `_tmux-name b` then act on the
  wrong record whenever exactly one OTHER uuid session begins with `b` (≈6 % per pair of live
  sessions for a one-letter name). Reproduced as a load-independent flake in T-SPAWN-10 / T-TMUX-20
  (`s`/`b`) and T-SPAWN-11 (`a`/`s`). `has_session` uses `=name` (T-TMUX-02) but `show_option`
  does not. Tests avoid hex-only names wherever two uuid sessions coexist (`small`/`big`,
  `alpha`/`beta`, `agent`); the spec's own names are kept where a single uuid session is live
  (a name can then only prefix-match its own session). Port decision needed: PARITY (reproduce
  the prefix match) or FIX (`=name` in `show_option`, as `has_session` already does).

## TMUX

- **T-TMUX-15 `@remote-session` edge — not in Appendix B, needs a decision.** Spec: pane option
  `@remote-session=host1` on `%1` → `inner-remote='1' inner-session-name='host1'`, no join. Code:
  `Tmux.focus_attrs` reads `show_option(pane_id, "@remote-session")` = `show-options -vqt <pane>`
  WITHOUT `-p`, i.e. the host SESSION's option; the pane-scoped stamp that `tx attach --host`
  writes with `-p` is never seen and the envelope still shows the nested join. Asserted a parity
  leg (session-scope option → the remote shape; pane-scope → plain join) and a fixed leg with the
  spec's pane-option Then under `@expected_failure_on_python`.
- **T-TMUX-01 / T-TMUX-06 `/bin/true` edge.** Spec: stderr `… failed: no such session: <id>`.
  Code surfaces tmux's stderr verbatim; when the dead pane was the server's ONLY session the
  server exits and tmux says `no server running on <socket>`. Asserted the spec text with a live
  sibling session in the Given (what the spec's fixture implies).
- **T-TMUX-08 gone pane.** As the spec allows: 3.4 prints an envelope with empty attrs, no server
  prints nothing; asserted only exit 0, no `inner-session-name`/`session-kind`, no trailing newline.
- **T-TMUX-19 `tx start` edge.** `tx start` runs `bin/tx-assistant --warm`, which would create a
  worktree of the REAL checkout; the test pre-seeds a live crafted `tx-assistant` record so
  `tx start` prints `tx-assistant already running.`, `Views session created.`, then fails on
  `switch-client` (`no current client`, matched by prefix).
- **T-TMUX-11.** Window names are read after `rename-window` (automatic-rename would otherwise
  flip them to `tmux` once nested).

## SPAWN

- **T-SPAWN-10 UTF-8 edge.** Spec: `"sleep 30 #" + "é"*4091 = 8193 bytes → script`. It is 8192
  bytes (10 + 2·4091) → inline. Asserted the true boundary: 4091 `é` inline, 4092 `é` (8194 bytes,
  4102 chars) → launch script — still proves byte counting.
- **T-SPAWN-12.** No `--env FAKE_OUT` needed: the private server's global env (seeded by the
  kit's first `tx` call) already carries it; documented in the method docstring.
- **T-SPAWN-01.** `agy` leg skipped per D3 (docstring).

## LIFE

- **T-LIFE-10.** Spec: `tx _tmux-name U3` → "empty line, exit 0". Code prints nothing when the
  record is not live → stdout `""` (no newline). Asserted `""` (matches T-TMUXCONF-10's wording).
- **T-LIFE-06.** Spec abbreviates the argparse message to `a group cannot be empty`; real text is
  `tx group: error: a group cannot be empty — use --clear to drop the override`. Asserted the
  `usage: tx group` prefix + the full error line, exit 2.
- **T-LIFE-07.** Spec: log tail `spawn` → `bind-artifact` → `artifact-open`. Code's
  `artifact-open` line is `msg="<A> → user"` with `actor="user"` (outside tx the actor is
  `USER_ACTOR`). Asserted that exact triple. `tx artifact create` prints the basename
  (`(plan.md)`), not the given path.
- **T-LIFE-05.** `window_name` is volatile under automatic-rename; the test renames `Views:0`
  to `w0` and asserts the full Location dict.

## WT / RO

- **T-WT-01.** `PWD=<WT>` in the engine dump asserted exactly (the pane's `sh -c` sets `PWD`
  to tmux's `-c` cwd).
- **T-RO-02 / T-RO-03.** See the `#{pane_start_command}` note; the rest of the bwrap argv is
  asserted exactly from the fake-bwrap dump (`--bind / / --ro-bind <r> <r> --ro-bind <key> <key>
  --chdir <wt> -- claude …`).
- **T-RO-03** is written against a hand-rolled fake `sandbox-exec` and `@platform_only("darwin")`;
  it skips on Linux.

## TMUXCONF

- **T-TMUXCONF-01.** `pane-focus-in`, `pane-exited`, `window-pane-changed` are window hooks on
  tmux 3.4: they appear in `show-hooks -gw`, not the plain `show-hooks -g` listing (per-name
  `show-hooks -g <name>` answers for all 8). Asserted the union of `-g`/`-gw` plus per-name
  queries. `focus-events` is a server option (`show-options -s`); `show-options -gv focus-events`
  also answers `on` — asserted that.
- **T-TMUXCONF-02/06/07.** Spec writes single-quoted `if-shell -F '…'`; tmux's normalized
  `list-keys` rendering uses double quotes (`if-shell -F "…"`). Asserted tmux's rendering,
  whitespace-collapsed, same content.
- **T-TMUXCONF-03.** Hook renders as
  `after-new-window[0] if-shell -F "#{@tx_view}" "setw pane-border-status top"`. "Window in `p`
  stays off" asserted via the `#{pane-border-status}` expansion = `off` (inherited default).
- **T-TMUXCONF-08.** `show-options -s user-keys[2]` prints `user-keys[2] \033W3` unquoted on 3.4
  (spec shows `"\033W3"`). Asserted the unquoted form.
- **T-TMUXCONF-16.** State file placed under the temp root (`TMUX=<root>/fake-sock,123,0` →
  `<root>/fake-sock.system-resources`), never `/tmp/tmux-1000/default`. CPU values asserted
  exactly from the state file the script writes (aggregate, delta, stale fallback); MEM by regex.
- **T-TMUXCONF-17/18.** `bin/tx-assistant` spawns with `--cwd "$repo"` (its own checkout); the
  test COPIES it into a temp git repo with `bin/tx → TX_BIN` so the worktree belongs to the temp
  repo, not this checkout. Spec "exit 0 within ~2 s" measured 1.4 s — asserted < 5 s; the
  refused-spawn edge measured 11 s — asserted ≥ 9 s.
- **T-TMUXCONF-18 `TMUX_PANE` unset.** The server's "current" pane (no client) resolves to the
  assistant's own pane; asserted `pane-id='<display-message -p #{pane_id}>'` in the typed line
  rather than a fixed pane.
- **T-TMUXCONF-14.** curses paints with cursor moves, so the pty stream can read
  `a,bComma-separated`; asserted label→value adjacency (`Tags:\s+a,b`), the border cell count
  (80 / 44 / 80), the title, and no writes.

## ATTACH

- **T-ATTACH-07 LOCATION.** Spec: `Views:<win>.<pane>`; `location_text` renders
  `<window_name>[<pane_index>]` (and the window auto-renames to `tmux` once nested). Asserted the
  rendered form with the window name read from tmux.
- **T-ATTACH-07 respawn command.** Spec: `bash -c '…; exec \${SHELL:-zsh}'`; tmux 3.4 renders
  `"bash -c '…; exec \\${SHELL:-zsh}'"` (outer double quotes, `$` and `"` backslash-escaped).
  The test undoes tmux's rendering and compares the raw command exactly.
- **T-ATTACH-07 untracked `a b`.** Spec: "contains `tmux attach -t 'a b'`"; the wrapper is itself
  shlex-quoted for `bash -c`, so the text is `tmux attach -t '"'"'a b'"'"'`. Asserted
  `bash -c ` + `shlex.quote(wrapper)` exactly.
- **T-ATTACH-07 "TMUX unset" precondition.** Spec says it falls through to switch-client;
  `_switch_or_attach` takes the FOREGROUND attach when `TMUX` is unset. Asserted a new client on
  `U2` on the popup's own pty, `%3` untouched, the Views client unchanged, popup closes on detach.
- **T-ATTACH-07 "%3 running nvim/claude".** Uses the fake `nvim` (its `pane_current_command` reads
  `python3.14`); the predicate under test is "not in SHELL_COMMANDS".
- **T-ATTACH-09 current session in a popup (reference behaviour).** A popup has no `TMUX_PANE`, so
  `current_session_name()` resolves `#S` through tmux's most-recently-active client; with nested
  clients newer than the launching client, `#S` was the nested one's session (== target) and
  `--jump` returned "already there". The fixture attaches the launching client last (what a real
  prefix+t keypress guarantees); the "popup on the nested client itself" leg first sends a keystroke
  into it. A port resolving the current session the same way inherits this.
- **T-ATTACH-09 "switch fails" edge.** The spec's `setsid` + `TMUX=<sock>,1,0` recipe never fails
  when the server's best session is `U2` itself (`#S` → `U2`). Asserted the failure by driving the
  picker in a pane of a detached view (no client): stderr `tx: could not jump to or switch to
  session U2`, a second fzf run, exit 0, pane untouched. (T-ATTACH-08's setsid edge behaves as spec'd.)
- **T-ATTACH-01/04 feed.** fzf's stdin has no trailing newline (`subprocess.run(input=…)`) while
  `tx _list` prints one; compared as line lists with STARTED/IDLE masked.
  `reload-sync(<bin>/tx _list)` is `_repo_root()/bin/tx`, asserted against the resolved `TX_BIN`.
- **T-ATTACH-02 "arm file exists during the run".** The kit fake dumps env only; existence during
  the run is asserted in T-ATTACH-05 with a hand-written `fzf`.
- **T-ATTACH-03 `ctrl-t`.** The middle bakes `PYTHONPATH=<repo>/lib <python> -m tx`;
  prefix / suffix / tail asserted as the spec instructs.

## EDITOR

- No spec/code disagreement. Harness note: with a nested client on the server,
  `new-session -d -x 20 -y 5` is sized from that client (`window-size latest`) and
  `window-size manual` makes tmux 3.4 abort the server for a second small session; the test
  creates `Ed` at 20×5 BEFORE nesting and gates the form start on a trigger file, then asserts
  `#{pane_width}x#{pane_height}` = `20x5`.

## Harness notes (section 02)

- **Private server boot.** `TxCase.setUp` boots the private server config-free from the scrubbed
  env (`TmuxServer.start`, `exit-empty off`) after `tmux.env` is set. The order matters: a boot run
  from the test process's own environment seeds the server's global environment with the
  operator's PATH/HOME; an in-pane `TMUX= tmux attach` or a popup's `tx` then reaches the
  operator's tmux binary and home. That is how one throwaway probe on 2026-09-23 reconciled the
  live store's records to exited (restored the same afternoon from the log's last `state` lines);
  the kit guards (`TXKIT_TMUX_SOCKET`-gated wrapper, `KitSafetyError`) plus the env-carrying
  direct calls close it.
- **Prefix-match decision: FIX.** `test_t_tmux_07_fixed_short_hex_name_never_prefix_matches`
  pins it (`@expected_failure_on_python`; forced with `TX_IMPL=rust` it fails on the reference).
- **`display-popup -E` blocks** the invoking `tmux` until the popup closes; a foreground attach
  inside it would hang `self.tmux.run`. ATTACH runs popups through `subprocess.Popen` with cleanup.
- **Login shells drop the kit PATH.** A command-less `split-window`/`new-window` starts a login
  shell whose `/etc/profile` resets PATH; use `TmuxServer.split_window` / `new_window` (explicit
  non-login `/bin/bash`).
- **Runtime.** Whole section 162 s sequential on this host (202 tests, one private server per test).

## Review pass (Phase 5b, review `bfd226f5…`, spec rev 5)

Every non-OK row of the section-02 review, applied on `feat/port-tests-fix-02`. Where the spec was
corrected separately (rev 5) the test asserts the code's behaviour; the entries below say what.

### WRONG

- **T-TMUX-15.** `…_15_parity_remote_session_read_at_session_scope` now carries
  `@python_reference_only` (D17 pair with `…_15_fixed_…`): a Q30-fixed port reads pane scope only
  and the session-scope shape is reference-only.

### WEAK → strengthened

- **T-TMUX-02 / T-TMUX-09.** The "no server" legs `kill-server` first (the kit's server is always
  up, `exit-empty off`) and assert `list-sessions` fails before the verb runs; `tx spawn-view work`
  is asserted to start the server (exact stdout, `spawn-view` log line).
- **T-TMUX-16.** Both envelopes assert exit 0 and empty stderr.
- **T-SPAWN-18.** Every argparse line is asserted exactly (`err.endswith("\ntx spawn: error: <line>\n")`).
  Two lines differ from the spec's abbreviation: `--env` / `--group` are `type=` converters, so
  argparse prefixes them — `tx spawn: error: argument --env: --env expects KEY=VALUE, got 'NOEQ'`
  and `tx spawn: error: argument --group: a group cannot be empty`. `--effort 6` keeps the
  `argument --effort: invalid choice: '6' (choose from …)` prefix (the choice list is the
  registry's).
- **T-SPAWN-19.** The `mytool` leg compares `git worktree list` before/after (no worktree added)
  and reads the fake's argv; name `mx` (non-hex). "NO tmux server" → the rev-5 "no live sessions"
  (`self.tmux.sessions() == []` before the bare spawn).
- **T-LIFE-01/03/05/08/09.** `live_shell` / `live_llm` seed a STALE `attached_to`
  (`STALE_LOCATION`) by default, so every "cleared / re-snapshotted to `[]`" assertion now
  observes a write. T-LIFE-01: the by-id leg asserts the launch script, `ended_at`, `err == ""` and
  exactly one new `kill` line from `before`; the idempotent leg asserts the record was rewritten
  (mtime). T-LIFE-03: the exited leg asserts `err == ""`. (Kit candidate: seed the stale Location in
  `Records.llm/other` or a `live_*` helper.)
- **T-LIFE-06.** The full `tx group: error: a group cannot be empty — use --clear to drop the
  override` line is asserted (`endswith`), as NOTES-02 claimed.
- **T-LIFE-12.** New leg `…_12_revive_non_llm_record_to_alive` (`@expected_failure_on_python`): an
  exited shell record with a live `@tx_id` session revives to `alive`, `ended_at` null,
  `attached_to` re-snapshotted from a stale value, one `revive` line, `tx ls` lists it and does not
  re-exit it.
- **T-ATTACH-07.** New leg `…_07_nested_tmux_pane_falls_through_to_switch_client` (the view pane
  already hosts a nested `tmux attach` of an untracked session): no respawn (start command and
  `pane_pid` unchanged), the Views client switches to `U2`, the pane's own nested client stays on
  `plain`, `attached_to` stays `[]`. "within ~1 s" is waited with a 3 s bound (headroom for a loaded
  host).
- **T-ATTACH-09.** The nest-attach fallback leg repeats T-ATTACH-07's checks: the pane's `pane_pid`
  changed, a client on the pane tty views `U2`, `tx ls` LOCATION `tmux[0] +1` and `attached_to` =
  `[Alt:0.0, Views:0.0]` (both windows waited to `tmux` under automatic-rename).
- **T-TMUXCONF-03.** Borders-off snapshots all six border window options before the fragment runs
  and asserts them unchanged after (the config-free server's stock values; `pane-border-indicators`
  is `colour` on tmux 3.4, not `off`).
- **T-TMUXCONF-08.** Pane-keys-off compares prefix `M-1..M-7` with the STOCK state read before the
  fragment ran — bound line or `unknown key` per key (3.4 binds `M-1..M-5` only); `M-1` is
  additionally pinned to `select-layout even-horizontal`.
- **T-TMUXCONF-11.** New leg `…_11_non_ascii_tag_colour_agrees_with_list` (Q13 PARITY): the helper's
  `colour<n>` for `é` equals the `\e[38;5;<n>m` number of the same chip in `tx _list`. Verified: the
  bash palette hashes per CHARACTER only under a UTF-8 locale (`é` → colour 73, matching Python's
  code points) and per BYTE under `C` (colour 198), so every helper run pins
  `LANG=LC_ALL=C.UTF-8` (`UTF8_LOCALE`); no marker — both implementations agree under the pinned
  locale. Also asserts no trailing newline and `err == ""` on the main leg.
- **T-TMUXCONF-12.** The attach-cycle leg runs the helper behind a recording `tmux` (ahead of the kit
  wrapper on PATH) and asserts exactly 8 `display-message -p -t <pane> #{pane_at_left}` edge checks,
  no `select-pane`, and the view's active pane unchanged.
- **T-TMUXCONF-16.** New "negative idle delta" stale leg (falls back to the aggregate); the state
  file is asserted `^\d+ \d+\n$` on every read (also the `$XDG_RUNTIME_DIR` file); the second-run
  check asserts `elapsed > 0` and `elapsed_idle >= 0` instead of branching on them.
- **T-TMUXCONF-18.** New leg `…_18_blank_or_missing_buffer_never_spawns` with NO live assistant:
  exit 0, buffer consumed, `sessions/` empty, no tmux session, no fake-claude dump, no log line.
- **T-EDITOR-03.** New leg `…_03_home_then_end_moves_the_cursor_to_the_end`: `Home`, insert, `BSpace`,
  `End`, `X`, Enter → name `abcX`, log `rename abc → abcX` (a broken `End` would yield `Xabc`).

### FRAGILE → deterministic

- **T-TMUX-01 (Q21).** The bad-cwd leg is version-gated as the spec says: on tmux < 3.5 (verified
  3.4) the fallback is asserted exactly (exit 0, exact stdout, record `cwd=/nope`, pane path
  `$HOME`, one `spawn` line). On a newer tmux the tolerance is UNVERIFIED (no such tmux on this host;
  the tmux changelog is silent), so the leg accepts either documented outcome, each in full —
  fallback as above, or `tx spawn: tmux new-session … failed: …` with nothing persisted. First run
  on a ≥ 3.5 host should settle it and drop the branch.
- **T-TMUX-11/12/13/16/19, T-ATTACH-09 `jump_fixture`, T-LIFE-05.** Every command-less
  `new-window` / `split-window` (login shell — a profile can reorder PATH ahead of the wrapper, D15)
  is now `self.tmux.new_window` / `split_window`; `test_life.py`'s private `split_window` is gone.
- **T-TMUX-17.** The recording `tmux` is a separate dir PREPENDED to PATH for the picker's pty run;
  the kit wrapper is no longer overwritten in place.
- **T-TMUX-18.** The popup runs through `subprocess.Popen` with a 15 s bound (a picker that hangs
  fails the case, not the suite).
- **T-TMUX-19 `tx start` leg.** An inert `tx-assistant` stub is first on PATH in the pane, beside
  the pre-seeded live record. The reference resolves the helper REPO-relative (`_repo_root()/bin/
  tx-assistant`), so only the record protects it; the stub covers a port that resolves it through
  PATH. `@tx_view`, the two record files and the log tail (`spawn`, `spawn-view`) are asserted.
- **T-SPAWN-01.** `e1`/`e2` → `ex1`/`ex2` (Q27, ~10 uuid sessions live).
- **T-SPAWN-16 / T-RO-02 / T-RO-03.** `path_without_binary(case, binary)` (in `test_ro.py`, imported by
  `test_spawn.py`; kit candidate): the kit dirs first, then ONE symlink dir holding every executable
  of the inherited PATH except the binary (first hit wins) — `bwrap` is gone even from an apt
  host's `/usr/bin` while bash / git / python stay reachable. The refusal legs also assert no tmux
  session and no log line.
- **T-ATTACH-07.** `bash` before the popup is waited for, not read once.
- **T-ATTACH-08.** The crafted `$TMUX` uses `self.tmux.socket_path` (under the root's `tmux-tmp`).
- **T-ATTACH-09 "moves nothing".** The Space keystroke into the nested client is repeated until that
  client's `client_activity` (1 s resolution) is strictly past the outer client's, so the nested
  client is provably the most recently active one (Q43) before the popup opens; the client set is
  compared by `(client_tty, client_session)`, not the volatile activity stamp; Alt's active pane is
  asserted unchanged too.
- **T-TMUXCONF-14.** The pty client and the server's global env pin `LANG`/`LC_ALL=C.UTF-8`
  (`utf8_client`), so the rounded border, `›` and `—` render regardless of the host locale.
- **T-TMUXCONF-17.** `--warm` bound relaxed from < 5 s to < 10 s (measured 1.4 s; the refused edge
  is ≈ 10 s and asserted ≥ 9 s).

### SPEC (rev 5 amended; the tests assert the code)

- **T-SPAWN-17.** Order as the rev-5 Given: live `w` and `w-2` first, then the refused `codex` spawn;
  the listing keeps main + `r--w` + `r--w-2` and has no `r--c` (only the refused spawn's own
  worktree is removed); no new record, no new log line.
- **T-SPAWN-19.** "NO tmux server" is unreachable under D15; asserted "no live sessions".
- **T-WT-02 (Q25 FIX leg).** `--engine claude` on all three names (`""`, `.`, `..`) so the worker
  path (`next_name`) is reached; each asserts exit 1, a single `tx spawn: …` stderr line naming
  `'<name>'`, empty stdout, no record, no worktree entry, nothing under `worktrees/<key>/`, no tmux
  session, no log line. The exact wording is left to the port (the spec says "names the rejected
  name").
- **T-TMUXCONF-05.** tmux renders the focus hooks `run-shell -b <dir>/tmux/../bin/tx-graph-focus-poke`
  — un-normalised and unquoted — asserted as such (now against the kit's copied fragment).
- **T-TMUXCONF-13.** The plain-session `y` FAILS VISIBLY: the client shows
  `tx kill: session 'p' not found (no live @tx_id, no store record)` then tmux's `returned 1`
  (asserted both); new leg `…_13_prefix_x_binding_drives_the_helper` sources the fragment, presses
  `C-b X` on the pty client, answers `y`, and asserts the kill through the record and the log.

### Entry points (D16)

- **TMUXCONF** runs the kit's COPIES throughout: the fragment is `<root>/helpers/tmux/tx-ide.tmux`
  (its `dirname`-relative binds name `<root>/helpers/tmux/../bin/<helper>` — the `helper_prefix`
  the T-TMUXCONF-02/04/05/07 renderings are compared against), helpers go through
  `self.helper(name, …)` / `helper_popen(name, …)` (`self.fakes.helper(name)`), and `tx-assistant`
  is the kit copy re-copied into the temp git repo beside a `bin/tx → TX_BIN` link (it spawns with
  `--cwd <two dirs above its real path>` and runs `<that>/bin/tx`, so the copy must sit in a git
  repo). Nothing under `<repo>/bin` or `<repo>/tmux` is referenced any more. **EDITOR** only ever
  ran `TX_BIN` (`tx _edit-session`), unchanged.
