# port-tests — the black-box acceptance suite for the Rust `tx`

Spec: artifact `15681067-0027-4677-9ffd-c618378aa890` (*tx-ide Rust port — test-suite specification*),
rev 4: 432 cases across 27 areas (Appendix A). The suite is stdlib `unittest` (D7), never imports `lib/tx`
(D1), and drives whatever binary `TX_BIN` names through the observable surfaces of Appendix B3:
stdout/stderr/exit code, files under `$TX_IDE_HOME`, tmux state on a private server, fake-binary
argv/env dumps, and the git fixture.

## Run

```sh
# against the Python reference (default TX_BIN = <repo>/bin/tx)
python3.14 -m unittest discover port-tests

# against a port
TX_BIN=/path/to/rust/tx python3.14 -m unittest discover port-tests

# one area, one case, verbose
python3.14 -m unittest discover port-tests -v -p 'test_art.py'
python3.14 -m unittest port-tests.test_art.TestArt.test_t_art_01_record_round_trip   # from repo root, with port-tests on sys.path

# spec → test coverage (exit 1 while any non-DROPPED/DEFERRED case lacks a method)
python3.14 port-tests/check_coverage.py [--spec PATH] [--tests DIR] [--verbose]
```

## Entry points

Everything the suite executes is selected by environment variables (spec H9), resolved once at
import (`txkit.TX_BIN`, `TX_HELPERS_DIR`, …). The defaults are the Python reference's files; a port
sets every one of them, or the suite silently tests the checkout instead of the port. Two are
directories holding one entry point per file name; the scalars are argv prefixes split like a
shell would (a first word with a `/` is resolved, a bare word such as `tx` is found on the run's
PATH, where `tx` is the link to `TX_BIN`).

| variable | default | what it names |
|---|---|---|
| `TX_BIN` | `<repo>/bin/tx` | the `tx` binary under test |
| `TX_IMPL` | detected | `python` or `rust`, for the D9 / parity markers (see Markers). Without it the Python shim is recognised by the `-m tx` line in `bin/tx`; set `TX_IMPL=rust` when the port is wrapped in a shell script |
| `TX_HELPERS_DIR` | `<repo>/bin` | the dir holding `tmux-pane-session-name`, `tmux-nav`, `tmux-kill-tx-session`, `tmux-edit-tx-session`, `tmux-session-relabel`, `tmux-system-resources`, `tx-assistant`, `tx-graph-focus-poke`. Every test gets COPIES of them in `<root>/helpers/bin/` beside a `tx → TX_BIN` link, because the helpers find `tx` relative to their own resolved path (`$(dirname $(readlink -f $0))/tx`, `<dir>/../bin/tx`) — a symlink would resolve back into the repo and run the reference. Use `self.helper(name, …)` / `self.fakes.helper(name)`, never `<repo>/bin/<name>` |
| `TX_INSTALLER` | `<repo>/install` | the installer — an argv PREFIX, may carry arguments (`TX_INSTALLER="tx install"`); the kit exposes it as a list, run it as `self.script([*TX_INSTALLER, …])` |
| `TX_UNINSTALLER` | `<repo>/uninstall` | the uninstaller (argv prefix) |
| `TX_ENGINE_SETUP` | `<repo>/setup/engines` | the DIRECTORY of engine-wiring entry points, used as `TX_ENGINE_SETUP / "{install,claude,codex,antigravity}.sh"` |
| `TX_STATUSLINE` | `<repo>/claude/statusline.sh` | the statusline (argv prefix; `tx statusline` in a port), fed the hook payload on stdin |
| `TX_UPDATE_GOLDEN=1` | — | capture goldens from the Python reference instead of comparing (see Goldens) |

The copy is a small tree, `self.fakes.helpers_root` = `<root>/helpers/{bin,shared,tmux}`: the
helpers source `../shared/palette.sh`, and `tmux/tx-ide.tmux` binds `../bin/*`, so sourcing
`<helpers_root>/tmux/tx-ide.tmux` binds the copies. `tmux-session-relabel` is a Python helper that
imports `lib/tx`; under the reference layout the kit links `<root>/helpers/lib → <TX_HELPERS_DIR>/../lib`
so the copy still imports. A port ships its own relabel in `TX_HELPERS_DIR`.

The whole suite takes ≈ 10 minutes sequentially on a laptop-class host (one private tmux server per
test; section 02 — TMUX/SPAWN/LIFE/WT/RO/ATTACH/TMUXCONF/EDITOR — is ≈ 4 minutes of it). The spec's
tmux floor is 3.6 (D13); on a 3.4 host the version-gated cases skip.

## Layout

```
port-tests/
  txkit.py            fixtures, runner, safety guards, goldens, markers
  check_coverage.py   spec heading → test method audit
  test_smoke.py       one green test per kit layer (not spec cases)
  test_<area>.py      one file per spec area: model store home events recon render migr ·
                      tmux spawn life wt ro attach tmuxconf editor · eng hook hist chat ·
                      role group msg art sync cli · inst status nvim
  golden/<area>/<nn>.txt   captured Python output for `golden:` cases
  NOTES-<nn>.md       per section: every case where the spec's Then and the reference disagreed
  NOTES-merge.md      kit reconciliations and test edits made while integrating the sections
```

## Naming rule

`test_<area>.py :: Test<Area> :: test_t_<area>_<nn>_<slug>` for spec case `T-<AREA>-<nn>`, e.g.
`T-TMUXCONF-03` → `test_tmuxconf.py::TestTmuxconf::test_t_tmuxconf_03_<slug>`. `check_coverage.py`
matches on the `test_t_<area>_<nn>` prefix, so a case may be split across several methods; a case
with a FIX leg and a parity leg is two methods (`..._<nn>_parity_...`, `..._<nn>_fixed_...`), the
fixed one carrying the D9 marker. Cases marked `DROPPED` / `DEFERRED` in the spec have no test.

## Markers

- `@expected_failure_on_python` — D9: the case asserts the FIXED behaviour of an Appendix-B quirk;
  skipped when `TX_BIN` is the Python reference (`TX_IMPL` decides, see Entry points).
- `@python_reference_only` — its mirror: a PARITY leg that pins the reference's UNFIXED behaviour
  of a quirk whose decision is FIX; skipped when `TX_BIN` is a port.

  **Pairing rule.** For every Appendix-B row with Decision FIX, the case has two methods: the fixed
  leg (`..._<nn>_fixed_...`, `@expected_failure_on_python`) and, only if the parity behaviour is
  worth pinning on the reference, a parity twin (`..._<nn>_parity_...`, `@python_reference_only`).
  Never leave a parity leg of a FIX quirk unmarked — a fixed port cannot pass it. Rows with
  Decision PARITY are plain, unmarked tests (both implementations must agree).
- `@requires_tmux(min="3.6")` — skip below the floor.
- `@requires_bin("nvim")` — skip when the binary is absent.
- `@platform_only("linux")` / `@platform_only("darwin")`.

## Goldens (D10)

`golden:` cases compare against embedded fixtures in `port-tests/golden/<area>/<nn>.txt`, captured
from the Python reference ONCE and committed — the port's agents may not have `python3.14`.

```sh
TX_UPDATE_GOLDEN=1 python3.14 -m unittest discover port-tests -p 'test_art.py'   # capture (Python tx)
python3.14 -m unittest discover port-tests -p 'test_art.py'                      # compare
```

`TX_UPDATE_GOLDEN=1` is the `--update-golden` switch (unittest's discover accepts no custom flags).
A missing golden skips the case rather than failing (H6). Normalise volatile fields (uuids, `ts`)
before calling `assert_golden`, as each case's spec text describes.

`Result.out` is ANSI-stripped, so `self.assert_golden(name, result.out)` is blind to colour a port
adds or drops. Cases whose Then says "stdout exactly" or pins colour (RENDER, MIGR, the picker
chips) compare the raw text: `self.assert_golden_raw(name, result.raw_out)` — same golden file,
same capture flag; re-capture the golden when switching a case from stripped to raw.

## Safety

Every `tx` the suite runs is aimed at a temp home on a private tmux server. Four guards enforce it
(added after a leaked kit PATH let a reconcile against an empty private server exit every live
record of the operator's real store, twice):

1. **The PATH `tmux` wrapper injects `-L` (and `-f /dev/null`) only when `TXKIT_TMUX_SOCKET` is
   set.** `scrubbed_env` sets it to the test's private socket; in any other shell the wrapper execs
   the real tmux untouched, so a leaked PATH is harmless.
2. **`scrubbed_env` (hence `run_tx` / `self.tx` / every helper) raises `KitSafetyError`** unless the
   home `tx` would RESOLVE — `TX_IDE_HOME`, a leading `~/` expanded against the env's own `HOME`,
   else `$HOME/.tx-ide` — lies under the kit's temp root (`resolved_home(env)`), and unless
   `TXKIT_TMUX_SOCKET` is set. `TX_IDE_HOME=None` is fine (it exercises the default while `HOME`
   is the temp user home); the process owner's `HOME`, an unset `HOME` or a `~user` path are refused.
3. **The private server is booted from the scrubbed environment**, config-free, by
   `TxCase.setUp` → `TmuxServer.start()` AFTER `tmux.env` is set, and every direct
   `TmuxServer.run` carries that env. The server's global environment — inherited by every pane,
   popup and hook it later launches — is therefore the temp home's, never the test process's: a
   pane's `tmux` is the wrapper and its `tx` the helper link, so an in-pane `TMUX= tmux attach` or a
   popup's `tx` can never reach the operator's tmux binary or home. (Booting inside
   `TmuxServer.__init__`, before the env exists, is exactly the incident.)
4. **A run without a `TmuxServer`** (`scrubbed_env(home)` for a script such as `statusline.sh`) still
   gets the wrapper first on PATH, aimed at a fresh `txkit-dead-<hex>` socket nothing ever started,
   so a stray `tmux` call meets "no server" instead of the live one.

5. **Nothing of the operator's leaks in, nothing of the test's leaks out (D15).** `scrubbed_env`
   drops `TX_*`, `TXKIT_*`, `FZF_*`, `XDG_*`, `NVIM*`, `GIT_*`, `TMUX*`, `VIMINIT` / `MYVIMRC` from
   the inherited env (unset, they default under the temp `HOME`), and sets
   `TMUX_TMPDIR=<root>/tmux-tmp`: every socket — the private server's (`self.tmux.socket_path`), the
   dead one, anything a bare real `tmux` would try after the wrapper dir is gone — lives under the
   temp root, never in the operator's `/tmp/tmux-<uid>/`. `GitFixture` runs git with
   `GIT_CONFIG_GLOBAL=/dev/null` and `GIT_CONFIG_NOSYSTEM=1`. Kit-made panes run
   `bash --noprofile --norc` (`self.tmux.split_window` / `new_window`, `PANE_SHELL`): a login shell's
   profile can reorder PATH ahead of the wrapper.
6. **Teardown reaps first.** `TxCase.tearDown` SIGKILLs every process whose environment carries this
   test's `TX_IDE_HOME` — detached chat-op finishers and watchers, `hook ingest` children, update
   curls, fake engines, pane shells; not the private server — and `tearDown` runs BEFORE any
   `addCleanup` (lock releases, `tmux.close`, rmtree), so a straggler can never outlive the lock it
   waits on, the PATH wrapper or the socket dir (`kill_home_children`). Start background verbs with
   `self.tx_popen(argv)` (scrubbed env, stdin closed, killed at cleanup) rather than a raw `Popen`.

Rules for humans and agents: never call `$TX_BIN` yourself with the kit's PATH but a real home — go
through `self.tx(...)`; never export a kit `fake-bin/` / `tmux-bin/` dir into an interactive shell's
PATH; before any hand-run probe, `which tmux` must be the system tmux and `$TX_IDE_HOME` empty (or an
explicit temp dir).

## Fixtures and helper index (`txkit.py`)

Subclass `TxCase`; `setUp` gives you:

| attribute | fixture | what it is |
|---|---|---|
| `self.root` | `Path` | the temp tree everything below lives in (`/tmp/txkit-*`, removed at teardown) |
| `self.home` | `TxHome` | `$TX_IDE_HOME` (`<root>/home`) with the `ensure_home` dirs, `agents → <repo>/agents`, hook shims for claude + codex; `HOME` (`<root>/user-home`), `CLAUDE_CONFIG_DIR`, `CODEX_HOME` under the same root |
| `self.tmux` | `TmuxServer` | private `tmux -L <random> -f /dev/null`, booted from the scrubbed env, `exit-empty off`; killed at teardown |
| `self.fakes` | `FakeBins` | PATH dir of argv+env recorders for `claude codex agy nvim fzf bwrap brew zsh`, plus `helpers_dir` (`<root>/helpers/bin`, after the fakes): `tx → TX_BIN` and a copy of every `TX_HELPERS_DIR` helper |
| `self.records` | `Records` | crafted schema-6 session records and artifact-v2 records with explicit timestamps (D8) |
| `self.git` | `GitFixture` (lazy) | repo with one commit on `main` |

Change how the home is built per class with `home_options`, e.g. `{"hooks": ()}` (no shims,
T-SPAWN-13), `{"skeleton": False}` (empty home, T-HOME-04), `{"link_agents": False}` (Fixture R
creates a plain `agents/` dir).

### Running `tx`

- `self.tx(argv, *, env=None, stdin=None, cwd=None) -> Result(code, out, err, raw_out, raw_err)` —
  `TX_BIN` under the scrubbed environment: inherited env minus `TX_*`, `TXKIT_*`, `TMUX`,
  `TMUX_PANE`, `NAMEW`, `FZF_*`, `TERM_PROGRAM`, `PYTHONPATH`; plus the home's vars, `FAKE_OUT`, and
  PATH with the tmux wrapper, the fakes, then the helper links first. `env` overrides (a `None`
  value unsets). `out`/`err` are ANSI-stripped, `raw_*` keep the escapes; `stdin=None` is EOF.
  Default `cwd` is the temp root.
- `self.script(argv, *, env=, stdin=, cwd=, timeout=)` — any executable under that same environment:
  an entry point (`[TX_INSTALLER]`, `["bash", TX_STATUSLINE]`) or a hand-written script.
- `self.helper(name, *args, env=, stdin=, cwd=)` — the kit's copy of helper `name`
  (`tmux-pane-session-name`, `tmux-nav`, …); `self.fakes.helper(name)` is its path.
- `self.env(extra=None)` / `self.tx_env(extra=None)` — that environment itself, for pty clients,
  hand-run scripts and crafted live sessions (`client_env=`).
- `self.tx_inside(session, argv, env=, cwd=)` — the same run INSIDE a private-server session via
  `run-shell -t` (`$TMUX` set, `#S` == session, `TMUX_PANE` pinned to its active pane).
- `self.run_in_pane(pane, "tx whoami")` — types the command into a shell pane (output redirected
  under the temp root) and waits for its exit code; the pane must run an interactive shell.
- `self.tx_popen(argv, env=, cwd=)` — `TX_BIN` in the background (scrubbed env, stdin closed,
  stdout/stderr piped; killed at cleanup) for verbs observed while they run.
- `self.tx_detached(argv, env=)` — `setsid -w`, no controlling tty (`tx attach` then sizes from
  `$COLUMNS`); skips where `setsid` is absent (macOS).
- `self.tx_pty(argv, env=, cwd=, rows=, cols=)` — `TX_BIN` on its own pty (picker outside tmux,
  curses); closed at teardown.
- `self.spawn_process(name, cmd=, tag=, cwd=, extra=)` → record / `self.spawn_view(name, cwd=, cmd=)`
  — spawn, assert exit 0.
- `self.live(id, cmd="sleep 1000")` — make a crafted record live (a session named by the id with
  `@tx_id` set, the RECON/RENDER recipe).
- `self.non_hex_name(prefix="s")` — `sx1`, `sx2`, …: a name that can never prefix-match a uuid
  session (see Flake rules).
- `self.attach_client(session, rows=, cols=)` — a real outer `tmux attach` client on a pty (the
  `-c <client_tty>` target for `display-popup` cases); closed at teardown.
- `self.log_lines()`, `self.log_tail(n)` — parsed `log.jsonl`; `self.assert_golden(name, actual)` /
  `self.assert_golden_raw(name, actual_raw)`; `self.wait_until(predicate, timeout=10, interval=0.05)`.
- Module level, for tests composing fixtures by hand: `run_script`, `run_tx`, `run_helper`,
  `run_tx_detached`, `run_tx_inside`, `scrubbed_env`, `resolved_home`, `log_lines`, `log_tail`,
  `wait_until`, `strip_ansi`, `munge`, `assert_golden`, `assert_golden_raw`, `golden`, `golden_path`, `tmux_version`,
  `is_python_reference`, `kill_home_children`, `REAL_TMUX`, `TX_BIN`, `TX_HELPERS_DIR`,
  `TX_INSTALLER`, `TX_UNINSTALLER`, `TX_ENGINE_SETUP`, `TX_STATUSLINE`, `HELPER_BINS`, `PANE_SHELL`,
  `REPO`, `HOME_DIRS`, `SESSION_RECORD_CMD`, `TMUX_SOCKET_ENV`, `KitSafetyError`.

### `TxHome`

`path`, `user_home`, `claude_config_dir`, `codex_home`, `root`; `sessions_dir`, `artifacts_dir`,
`history_dir`, `worktrees_dir`, `user_agents_dir`, `launch_dir`, `chat_ops_dir`, `hooks_dir`, `agents`,
`log_path`, `config_path`; `entries()` (`ls -A`), `write_config(dict | str)`,
`write_hook_shims(engine)`, `claude_transcript_path(cwd, chat_id)`, `env()`.

### `TmuxServer`

`socket`, `socket_path` (under `<root>/tmux-tmp`), `bin_dir`, `env`; `start()`;
`run(*args, check=False, env=None)` (a direct call on the private server; `env` is the issuing
client's environment, default the hermetic one);
`sessions()`, `has_session(name)`, `option(target, name, scope="session"|"window"|"pane"|"global"|"global-window")`,
`display(target, fmt)`, `environment(target)`, `capture(target)`, `clients()`, `panes(target=None)`,
`pane_id(target)`, `pane_tty(pane)`, `pane_commands()` (`@tx_id → #{pane_current_command}`),
`wait_for_window_name(target, name)`;
`new_session(name, cmd, tx_id=None, *, cwd=, env=, client_env=)`, `kill_session(name)`,
`split_window(target, *flags)` / `new_window(target, *flags)` (an explicit `bash --noprofile --norc`,
`PANE_SHELL` — a command-less split starts a login shell whose profile drops the kit PATH; return the
new pane id);
`send_keys(target, *keys, literal=)`, `type_line(target, line)`; `attach_client(session, env=)`,
`nest_attach(pane, session)` (nest a session the way the picker does: `TMUX= tmux attach -t …` typed
into the pane, then wait for the client whose `client_tty` is that pane's tty); `close()`.

### `PtyProcess(argv, *, env, cwd=None, rows=24, cols=80)`

A child on its own pty: `write(text)`, `read(timeout)`, `expect(text, timeout)`, `wait(timeout)`,
`close()`, `tty` (the slave path — what tmux reports as `client_tty`), `pid`.

### `FakeBins`

Each run writes `$FAKE_OUT/<basename>-<TX_SESSION_ID or pid>.json` with `argv`, `env`, `cwd`, `pid`,
`stdin` (read only for `fzf` unless `read_stdin=True`). `configure(name, **knobs)` (replaces earlier
knobs; a `sequence` restarts at its first entry), `dumps(name)`, `wait_dump(name, key=None, timeout=)`,
`dump_path(name, key)`, `stdin_log(name, key)`, `remove(name)`, `add(name)` (the standard recorder
under a new basename, e.g. `ssh`, or a removed one back), `write_script(name, body)` (a hand-written
executable), `helper(name)` (the copied helper's path), `bin_dir`, `helpers_dir`, `out_dir`. Knobs:

- `sleep=<s>` — how long to stay alive (default 600 for claude/codex/agy/nvim/zsh, 0 otherwise)
- `exit_code=<n>`
- `transcript="<path>"` (+ `transcript_text=`) — write a fake transcript before sleeping
- `prompt_glyph=True` — echo `❯ ` to stdout (visible in `capture-pane`)
- `stdout="..."` — extra stdout (e.g. an fzf selection)
- `read_stdin=True|False` — drain stdin (default only for `fzf`)
- `passthrough=False` — `bwrap` only: do not exec the command after `--` (default: exec it, D13)
- `sequence=[{...}, {...}]` — per-invocation knobs: the n-th run merges `sequence[n]` (the last entry
  repeats), e.g. `fzf` printing a row once then exiting 130 on every later run
- `stdin_log=True` — a tty-attached fake logs every raw stdin chunk with a timestamp to
  `<name>-<key>.stdin.jsonl` (`stdin_log(name, key)`); the Enter key arrives as a literal `\r`, and
  the pane still echoes the text

The `bwrap` fake is the default on every host (user namespaces are blocked on CI-class hosts); the
real sandbox is a hand-run smoke check.

### `Records`

`llm(...)` / `other(...)` write schema-6 records (every field a keyword; explicit `created_at`,
`last_activity`, `turn_started_at`, `ended_at`), `artifact(...)` writes a v2 record + `revs/<n>.<ext>`
+ `current.<ext>`; `chat_ref(...)`; `load(id)`, `path(id)`, `write(record)`,
`patch(id, key=value, gone=...)` (rewrite a stored record; `...` deletes a key);
`artifact_record_path(id)`, `artifact_dir(id)`.

There is no clock injection (D8). RECON/RENDER ages come from explicit past timestamps on crafted
records: `self.records.llm(created_at=now-300, last_activity=now-300, turn_started_at=now-700, ...)`
plus `self.home.write_config({"stuck_working_threshold_seconds": ...})`; `self.live(id)` makes such
a record live.

### `GitFixture(root, name="repo")`

`path`, `git(*args, cwd=None)`, `head()`, `worktrees()`, `with_origin_main()` (a bare `origin` with
`main` pushed), `as_linked_worktree(branch="linked")` (a linked worktree of the fixture repo).

## Flake rules

Two tmux behaviours turn a correct assertion into a coin toss; the kit has a helper for each.

- **No bare hex names.** The reference resolves a name through `show-options -t <name>`, which tmux
  PREFIX-matches, so a hex-only display name (`a`, `c`, `e1`, `abc`, `ed`) that coexists with two or
  more uuid-named sessions can resolve to ANOTHER session's record (Q27, ≈ 6 % per pair for one
  letter). Name sessions with a non-hex character — `self.non_hex_name("w")` → `wx1` — whenever
  more than one uuid session is live and the name is looked up (`tx show/kill/tag <name>`).
- **Window names settle late.** Stock `automatic-rename` follows `pane_current_command`, applied a
  beat after the command changes. Before asserting a window name (a `tx ls` LOCATION cell,
  `attached_to.window_name`) after a pane's command changed, call
  `self.tmux.wait_for_window_name(pane, "tmux")`; or create the window with `-n` / `rename-window`,
  which pins it.

## Known kit limitations

Collected from the five section NOTES files and the integration; each is worked around locally in
the test named, not in the kit.

- **Fakes record argv/env only.** A launch-time observation ("the bundle exists when the fake
  distiller starts", T-CHAT-05; "the arm file exists during the run", T-ATTACH-02) is not reachable
  black-box; those legs assert the ordering that follows from the blocking call instead.
- **The fake `fzf` drains stdin by default** (`read_stdin` defaults to true for `fzf`). Anything
  driving `install` through the fakes must `configure("fzf", read_stdin=False)`, or the dependency
  check's `fzf --version` eats the answers meant for the later `[y/N]` prompts (T-INST-12).
- **`FakeBins.remove()` is one-way**; `add(name)` re-installs the standard recorder. `test_inst.py`'s
  `Installer` predates `add` and still puts `codex` back as a silent stub (T-INST-50).
- **No runner for an arbitrary script under the scrubbed env.** STATUS calls
  `subprocess.run(["bash", statusline.sh], env=scrubbed_env(self.home, extra=…))` itself; INST has its
  own `Installer` (restricted PATH so the operator's engine CLIs stay invisible; the pre-flight
  banner's unescaped `$(tx)` / `$(tx start)` are absorbed by a silent `tx` stub on the fakes PATH).
- **No HTTP-listener fixture.** `test_status.py::UsageListener` (an `http.server` on `127.0.0.1:0` in
  a daemon thread, with a hold-until-released mode) is local to STATUS.
- **No `ssh` recorder among the fakes**; T-CLI-27 hand-writes its own script into `fakes.bin_dir`
  (`add("ssh")` / `write_script` are available when the standard recorder suffices).
- **Codex shim / `hooks.json` builders** live in `test_inst.py` (`engines_codex_shim_text`,
  `engines_codex_hooks_json`), not in `txkit`; the claude ones are `Installer.claude_shim_text` /
  `tmux_session_closed`.
- **tmux 3.4 on the dev host** (floor 3.6, D13): `claude.sh`'s hook step probes liveness with
  `tmux info`, which fails from an unattached client on 3.4 — the four hook-applied INST cases are
  gated `@requires_tmux(min="3.6")` and need CI to run; `run-shell -t` hands its job a stale
  `TMUX_PANE` (pinned explicitly by `run_tx_inside`); `list-clients`-based waits replace `tmux info`.
- **tmux prefix-matches a bare `show-options -t <name>` target** (Q27, FIX leg xfail): see Flake
  rules — `self.non_hex_name()`.
- **`display-popup -E` blocks the invoking `tmux`** until the popup closes; ATTACH runs popups
  through `subprocess.Popen` with cleanup rather than `TmuxServer.run`.
- **Command-less `split-window` / `new-window` start a login shell** whose `/etc/profile` resets
  PATH and drops the wrapper / helper dirs; use `split_window` / `new_window` (explicit `/bin/bash`).
- **A pane command that exits at once takes its session (and, when it was the last, the server)
  with it**, so `tx spawn --cmd <missing-binary>` fails before writing a record (T-NVIM-06 uses a
  long-running stand-in; `exit-empty off` keeps the kit's server alive, not a spawned session).
- **Distiller paths leave a detached `_chat-op-watch` (600 s)**; ENG/CHAT tests `pkill -f
  "_chat-op-watch <op_id>"` at cleanup, before the kit tears the server down.
- **Reconcile-on-read stamps non-terminal crafted records without a live session EXITED** and adds a
  `state` log line; crafted sources that must stay untouched are written `state: exited`, or made
  live with `self.live(id)`.
- **Kit panes are 80 columns**; long stdout lines wrap in `capture-pane` — use `capture-pane -J`.
- **`tx_detached` needs `setsid`** (Linux); it skips on macOS.
- **`nvim` code tours / companions**: the `tx-code-tours` skill's `lsof '$NF'` recipe fails on lsof
  4.95 and a real companion blocks RPC on a missing colorscheme, which is why the nvim socket is
  recorded on the record at spawn (D11) rather than discovered.

## Adding a fixture

Add a class to `txkit.py` that takes `root: Path` (and whatever fixtures it composes), creates its
files under `root`, and exposes a `close()` only if it owns a process. Wire it into `TxCase.setUp`
with `self.addCleanup(...)` when every test needs it, or expose it lazily like `git`. Keep it
stdlib-only and black-box: no `lib/tx` imports, no writes outside the temp root, and every `tmux`
call through `TmuxServer.run` (so it carries the hermetic env).
