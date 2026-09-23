# NOTES — SYNC (section 04)

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
