## MSG

No case where the spec's Then disagreed with `bin/tx`. T-MSG-05..14 are DROPPED (no tests).

Observations (not disagreements):
- Delivery is observed through `capture-pane` on the `cat` target: the envelope shows twice (typed
  echo + `cat`'s echo after Enter), exactly as T-MSG-02 states. The literal `send-keys -t t1 --` argv
  of T-MSG-04 is not an observable surface; the pane capture pins the same outcome.
- T-MSG-02 `Enter`-body leg is `expected_failure_on_python` per the spec marker; it passes against
  the reference too (`TX_IMPL=rust` run verified) — the marker only skips, as the spec says.
- The "harness types the command into s1" recipe is realised with the kit's `tx_inside` (tmux
  `run-shell -t s1`): `$TMUX` set, `#S` == `s1`, synchronous.
