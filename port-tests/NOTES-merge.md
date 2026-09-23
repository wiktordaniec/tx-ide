# NOTES-merge — Phase 3b: integrating `feat/port-tests-01..05` into `feat/port-tests`

Kit reconciliations (every section was green on its own; together they disagreed) and every test
edit made during integration, with the case id and the reason. No assertion was weakened below
what its spec case says.

## Kit reconciliations

- **Private server boot order** (04's 68639d0 vs the kit's 2b19ca4 and 02's d5a9502). 04 booted
  the server inside `TmuxServer.__init__`, before `TxCase.setUp` assigns the hermetic env, so the
  private server's global environment was the test process's own (real `HOME`, operator `PATH`).
  A pane spawned by `tx` then resolved `tmux` and `tx` to the operator's binaries, and
  `nest_attach`'s `TMUX= tmux attach` went to the live server — the EDITOR/TMUX "nested in %1"
  timeouts, and the same path that reconciled the real store to exited on 2026-09-23. Resolution:
  `TmuxServer.start()` (02's shape) called from `TxCase.setUp` after `tmux.env` is set; 04's
  semantics kept (`-f /dev/null` on every call, `exit-empty off`).
- **`TmuxServer.run(env=)`** (03's 6615899 vs the kit's per-call `env=self.env`): an explicit
  client env wins, else the server's hermetic env. `new_session(client_env=)` and
  `TxCase.tx_env()` kept (`tx_env` is the same env as `TxCase.env`).
- **`_check_safe` rejected legitimate runs**: T-HOME-01 (`TX_IDE_HOME=None` → the `$HOME/.tx-ide`
  default, `HOME` being the temp user home) and T-STATUS-01..10 (`bash statusline.sh` under
  `scrubbed_env(home)` with no `TmuxServer`). The guard now checks the RESOLVED home —
  `TX_IDE_HOME`, a leading `~/` expanded against the env's own `HOME`, else `$HOME/.tx-ide` —
  against the kit temp root (`resolved_home(env)`), and without a server it still puts the
  wrapper first on PATH aimed at a fresh `txkit-dead-<hex>` socket nothing ever started. A real
  home (`HOME` unset, the process owner's `HOME`, a `~user` form) is still refused.

- **`TmuxServer.run` signature** (02's `run(*args, check=)` vs 03's `run(*args, check=, env=)`):
  03's kept. 02's `TmuxServer.start()` and its `TxCase.setUp` call merged identically because the
  04 resolution had already adopted that shape (d5a9502).

## Test edits

| test | edit | reason |
|---|---|---|
| `test_smoke.py::TestSmokeSafety::test_run_tx_refuses_real_home_or_missing_socket` | split into `test_run_tx_refuses_a_home_that_resolves_outside_the_kit_root` and `test_no_server_fixture_means_a_dead_private_socket` | encoded the kit accident (`TX_IDE_HOME=None` and `tmux=None` refused outright). Now pins the reconciled guard: real / unset `HOME` and `~user` refused, the temp-home default and `~/x` allowed, and a no-server env reaches a socket with no server (tmux 3.4 says `error connecting to …/<socket>`) |
| `test_life.py::TestLife::test_t_life_12_revive_exited_record_whose_tx_id_session_is_alive` + `..._12_revive_unknown_session` | added | spec rev 4 added T-LIFE-12 (`tx revive`, Q32 FIX, D14). Both legs carry `@expected_failure_on_python` (the reference has no `revive` verb — argparse `invalid choice`); forced with `TX_IMPL=rust` they fail on the reference at the first assertion, as intended. Refusal messages are asserted by the `tx revive: ` prefix + quoted name only, as the case's Edge line asks (wording proposed); the not-found message is the resolver's established text (T-LIFE-11) |
| `test_chat.py::TestChat::test_t_chat_08_finish_idempotent` | waits for the spawned worker's own fake-claude dump (`wait_dump("claude", worker_id)`) before asserting exactly one dump | the pane's fake starts asynchronously after `_chat-op-finish` exits; the immediate count raced it (failed ≈ 1 in 6 alone, every fast run). The assertion is unchanged (exactly one worker, exactly one fake run); only a bounded wait precedes it |
| `test_spawn.py::TestSpawn::test_t_spawn_19_bare_spawn_is_shell_and_cmd_stamps_engine` | session names `c` → `cw`, `e1` → `ex1`, `e2` → `ex2` | with `s` (and later `z`, `X`) live, `tx show c` prefix-matched another session's uuid on the reference (Q27 shape, ≈ 6 % per pair; seen once in the full run as `KeyError: 'engine'`). Same remedy as 02's 645773f; no assertion changed (the spec's `Spawned 's' …` line is untouched, the worker's record and dump are asserted by id) |
| `test_attach.py::TestAttach::test_t_attach_07_nest_attach_into_view_pane` | waits until `#{window_name}` of the nested pane is `tmux`, then asserts `tx ls` location `tmux[<pane>]` and `attached_to.window_name == "tmux"` | stock config (`-f /dev/null`) leaves `automatic-rename` on, and tmux applies the rename a beat after `pane_current_command` changes; the test read the name once and `tx ls` read it again later, so the two could straddle the `bash` → `tmux` flip (seen once as `bash[0]` vs `tmux[0]`). The case's rev-4 Then names the settled value (`tmux` once nested), so the assertion is now exact rather than "whatever tmux said a moment ago". Scan: every other window-name/location assertion after a nested attach uses an explicitly named window (`-n main`, `rename-window`), which turns automatic-rename off for that window |

## Phase 5a — kit-level fixes from the five reviews (artifact e0f490c7)

Kit changes only (section fixers follow):

- **K1 entry points.** `TX_HELPERS_DIR`, `TX_INSTALLER`, `TX_UNINSTALLER`, `TX_ENGINE_SETUP`,
  `TX_STATUSLINE` (defaults = the reference's files). Helpers are COPIED into
  `<root>/helpers/bin/` beside a `tx → TX_BIN` link, with `shared/` and `tmux/` copied beside them
  (`fakes.helpers_root`) and a `helpers/lib` link for the Python-only `tmux-session-relabel`
  under the reference layout — a symlinked helper `readlink -f`'d back into the repo and ran the
  reference `tx`. `fakes.helper(name)`, `run_script` / `run_helper`, `TxCase.script` /
  `TxCase.helper`.
- **K2 marker.** `@python_reference_only` (parity legs of FIX quirks); pairing rule in README.
- **K3 hermeticity.** `TMUX_TMPDIR=<root>/tmux-tmp`; `XDG_*` / `NVIM*` / `GIT_*` / `TMUX*` /
  `VIMINIT` / `MYVIMRC` scrubbed; `GitFixture` with `GIT_CONFIG_GLOBAL=/dev/null` +
  `GIT_CONFIG_NOSYSTEM=1`; `split_window` / `new_window` run `bash --noprofile --norc`
  (`PANE_SHELL`); `TmuxServer.socket_path`; `kill_home_children` from `TxCase.tearDown` (before
  every `addCleanup`); `TxCase.tx_popen`.
- **K4 flakes.** `TmuxServer.wait_for_window_name`, `TxCase.non_hex_name`, `new_session` stamps
  `@tx_id` with `-t =name`.
- **K5 assertions.** `assert_golden_raw` / `TxCase.assert_golden_raw`.

Test edits made for the kit changes (case ids, reasons):

| test | edit | reason |
|---|---|---|
| `test_smoke.py` | wrapper passthrough probe carries the kit `TMUX_TMPDIR`; new `TestSmokeHelpers` / `TestSmokeHermeticity` | the private socket moved under the root; K1/K3 need a green proof |
| `test_nvim.py::TestNvim.attach_client` (T-NVIM-15) | local `script(1)` attach with `dict(os.environ)` removed; the kit's `attach_client` (scrubbed pty client) applies | review 05 §4, D15 breach; K3 |
| `test_nvim.py` crafted `$TMUX` value | `self.tmux.socket_path` instead of a runner-env-derived path | the socket now lives under `<root>/tmux-tmp` |
| `test_attach.py` T-ATTACH-07 | `self.tmux.wait_for_window_name(pane, "tmux")` replaces the inline wait | K4 helper |

## Phase 5b — integrating the section fixers (`feat/port-tests-fix-01..05`)

All five branch from `c7056fa`, touch disjoint test files and no kit file; the merges were clean.

- **Entry points aligned to spec rev 5 (H9).** The K1 kit exposed `TX_ENGINE_SETUP` as the
  `install.sh` FILE and every scalar entry point as one resolved path; H9 makes `TX_ENGINE_SETUP` a
  DIRECTORY (`{install,claude,codex,antigravity}.sh` by file name) and each scalar an argv PREFIX
  that may carry arguments (`TX_INSTALLER="tx install"`). fix-05's NOTES flagged both. The kit now
  matches H9: `TX_ENGINE_SETUP: Path` (dir), `TX_INSTALLER` / `TX_UNINSTALLER` / `TX_STATUSLINE:
  list[str]` (shell-split; a first word with `/` resolved). Test edits: `test_inst.py` module
  constants (`INSTALL = TX_INSTALLER`, `INSTALL_SH = [str(TX_ENGINE_SETUP / "install.sh")]`, …),
  `Installer.run(command: list[str], *args)`, the uninstaller-source read via `Path(UNINSTALL[0])`;
  `test_status.py` runs `[*TX_STATUSLINE]` (the reference script has a shebang and is executable; a
  port's `tx statusline` is an argv prefix). No assertion changed.
