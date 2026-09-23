## GROUP

No case where the spec's Then disagreed with `bin/tx`. All 12 cases assert the spec text verbatim
(`own:      <g|—>\nresolved: <g>\n`, `Grouped …`, `Cleared …`, argparse errors, log `type`/`msg`/`actor`).

Observations (not disagreements):
- T-GROUP-06 "alive" is the RECORD state (`Session.is_alive()` = non-terminal), not tmux liveness —
  `tx group NAME` never reconciles, so an `idle` record with no tmux session still ranks as alive.
- T-GROUP-12 log `actor` is `$TX_SESSION_ID` when set (tests run with `TX_SESSION_ID=s1`); with it
  unset and no `$TMUX` the actor is the `user` sentinel (cli.py `ArtifactCommand._actor`).
