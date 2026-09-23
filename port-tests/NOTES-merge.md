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

## Test edits

| test | edit | reason |
|---|---|---|
| `test_smoke.py::TestSmokeSafety::test_run_tx_refuses_real_home_or_missing_socket` | split into `test_run_tx_refuses_a_home_that_resolves_outside_the_kit_root` and `test_no_server_fixture_means_a_dead_private_socket` | encoded the kit accident (`TX_IDE_HOME=None` and `tmux=None` refused outright). Now pins the reconciled guard: real / unset `HOME` and `~user` refused, the temp-home default and `~/x` allowed, and a no-server env reaches a socket with no server (tmux 3.4 says `error connecting to …/<socket>`) |
