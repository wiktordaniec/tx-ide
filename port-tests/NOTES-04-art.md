# NOTES-04 — ART (spec vs `bin/tx`)

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
