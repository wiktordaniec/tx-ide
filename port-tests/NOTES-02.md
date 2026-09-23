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

## EDITOR

- No spec/code disagreement. Harness note: with a nested client on the server,
  `new-session -d -x 20 -y 5` is sized from that client (`window-size latest`) and
  `window-size manual` makes tmux 3.4 abort the server for a second small session; the test
  creates `Ed` at 20×5 BEFORE nesting and gates the form start on a trigger file, then asserts
  `#{pane_width}x#{pane_height}` = `20x5`.
