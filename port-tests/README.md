# port-tests — the black-box acceptance suite for the Rust `tx`

Spec: artifact `15681067-0027-4677-9ffd-c618378aa890` (*tx-ide Rust port — test-suite specification*).
The suite is stdlib `unittest`, never imports `lib/tx`, and drives whatever binary `TX_BIN` names
through the observable surfaces of Appendix B3.

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

Every test runs on a private tmux server behind a `tmux` wrapper first on PATH, so the operator's
live server is never touched. Each test gets a fresh temp root; teardown kills the server and
removes the tree.

## Layout

```
port-tests/
  txkit.py            fixtures, runner, goldens, markers
  check_coverage.py   spec heading → test method audit
  test_smoke.py       one green test per kit layer (not spec cases)
  test_<area>.py      Phase 3: one file per spec area (model, store, home, …, inst, status, nvim)
  golden/<area>/<nn>.txt   captured Python output for `golden:` cases (see below)
```

## Naming rule

`test_<area>.py :: Test<Area> :: test_t_<area>_<nn>_<slug>` for spec case `T-<AREA>-<nn>`, e.g.
`T-TMUXCONF-03` → `test_tmuxconf.py::TestTmuxconf::test_t_tmuxconf_03_<slug>`. `check_coverage.py`
matches on the `test_t_<area>_<nn>` prefix, so a case may be split across several methods (rev 3:
put the `expected_failure_on_python` marker on the fixed-behaviour leg only, in its own method).

## Fixtures (`txkit.py`)

Subclass `TxCase`; `setUp` gives you:

| attribute | fixture | what it is |
|---|---|---|
| `self.root` | `Path` | the temp tree everything below lives in |
| `self.home` | `TxHome` | `$TX_IDE_HOME` with the `ensure_home` dirs, `agents → <repo>/agents`, hook shims for claude + codex, plus `HOME` / `CLAUDE_CONFIG_DIR` / `CODEX_HOME` under the same root |
| `self.tmux` | `TmuxServer` | private `tmux -L <random>`; `sessions()`, `has_session()`, `option(target, name, scope)`, `display(target, fmt)`, `environment(target)`, `capture(target)`, `new_session(name, cmd, tx_id=None)`, `kill_session()` |
| `self.fakes` | `FakeBins` | PATH dir of recorders for `claude codex agy nvim fzf bwrap brew zsh`; `configure(name, **knobs)`, `dumps(name)`, `wait_dump(name, key)`, `remove(name)` |
| `self.records` | `Records` | `llm(...)` / `other(...)` write schema-6 records, `artifact(...)` writes a v2 record + `revs/` + `current.<ext>`; `load(id)`, `path(id)`, `chat_ref(...)` |
| `self.git` | `GitFixture` (lazy) | repo with one commit on `main`; `with_origin_main()`, `as_linked_worktree()`, `git(...)`, `worktrees()`, `head()` |

Change how the home is built per class with `home_options`, e.g. `{"hooks": ()}` (no shims,
T-SPAWN-13), `{"skeleton": False}` (empty home, T-HOME-04), `{"link_agents": False}` (Fixture R
creates a plain `agents/` dir).

`self.tx(argv, *, env=None, stdin=None, cwd=None) -> Result(code, out, err, raw_out, raw_err)`
runs `TX_BIN` under a scrubbed environment: inherited env minus `TX_*`, `TMUX`, `TMUX_PANE`,
`NAMEW`, `FZF_*`, `TERM_PROGRAM`, `PYTHONPATH`; plus the home's vars, `FAKE_OUT`, and PATH with the tmux
wrapper then the fakes first. `env` overrides (a `None` value unsets). `out`/`err` are ANSI-stripped;
`raw_*` keep the escapes. Default `cwd` is the temp root.

Also: `self.log_lines()`, `self.log_tail(n)`, `self.wait_until(predicate, timeout)`,
`self.assert_golden(name, actual)`. The module-level `run_tx(...)`, `log_lines(home)`, etc. exist for
tests that compose fixtures by hand.

### Fake binaries

Each run writes `$FAKE_OUT/<basename>-<TX_SESSION_ID or pid>.json` with `argv`, `env`, `cwd`,
`pid`, `stdin` (read only for `fzf` unless `read_stdin=True`). Knobs via `self.fakes.configure(name, ...)`
before the run:

- `sleep=<s>` — how long to stay alive (default 600 for claude/codex/agy/nvim/zsh, 0 otherwise)
- `exit_code=<n>`
- `transcript="<path>"` (+ `transcript_text=`) — write a fake transcript before sleeping
- `prompt_glyph=True` — echo `❯ ` to stdout (visible in `capture-pane`)
- `stdout="..."` — extra stdout (e.g. an fzf selection)
- `passthrough=False` — `bwrap` only: do not exec the command after `--` (default: exec it, D13)

The `bwrap` fake is the default on every host (user namespaces are blocked on CI-class hosts); the
real sandbox is a hand-run smoke check.

### Records and ages (D8)

There is no clock injection. RECON/RENDER ages come from explicit past timestamps on crafted
records: `self.records.llm(created_at=now-300, last_activity=now-300, turn_started_at=now-700, ...)`
plus `self.home.write_config({"stuck_working_threshold_seconds": ...})`. A crafted record is "live"
when the private server has a session named by its id with `@tx_id` set:
`self.tmux.new_session(record_id, "sleep 1000", tx_id=record_id)`.

## Markers

- `@expected_failure_on_python` — D9: the case asserts the FIXED behaviour of an Appendix-B quirk;
  skipped when `TX_BIN` is the Python reference. `TX_IMPL=python|rust` says so explicitly and always
  wins; without it the shim is recognised by the `python3.14 -m tx` line in `bin/tx`. Set `TX_IMPL=rust`
  when the port is wrapped in a shell script.
- `@requires_tmux(min="3.6")` — skip below the floor (this dev host runs 3.4; D13 sets 3.6 for CI).
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

## Adding a fixture

Add a class to `txkit.py` that takes `root: Path` (and whatever fixtures it composes), creates its
files under `root`, and exposes a `close()` only if it owns a process. Wire it into `TxCase.setUp`
with `self.addCleanup(...)` when every test needs it, or expose it lazily like `git`. Keep it
stdlib-only and black-box: no `lib/tx` imports, no writes outside the temp root.
