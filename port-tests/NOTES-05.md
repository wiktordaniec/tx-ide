# NOTES-05 — spec vs. reference for INST / STATUS / NVIM

Rule applied throughout: the spec is downstream of the code, so where a case's Then disagreed
with what `install` / `uninstall` / `setup/engines/*.sh` / `claude/statusline.sh` / `bin/tx`
actually do, the test asserts the reference behaviour and the disagreement is recorded here.
FIX legs (Q9, Q10, Q22, Q11, D11 `nvim_socket`) are separate `_fixed_` methods under
`@expected_failure_on_python`; parity legs stay green against the reference.

Harness for INST (`test_inst.py::Installer`): the bash scripts run as subprocesses with a
RESTRICTED PATH (tmux wrapper, fakes, `$HOME/.local/bin`, the test interpreter's dir, system
dirs) so the operator's own engine CLIs are invisible; `agy` is always off PATH (D3) and `codex`
only on when a case asks; a silent stub `tx` covers the install banner's unescaped `$(tx)` /
`$(tx start)` command substitutions (`install` lines 131–132 — a script bug: the banner runs
`tx start` before the operator confirms anything).

Host used for the reference runs: tmux 3.4, NVIM v0.12.3, lsof 4.95, python3.14.

## NOTES — INST cases T-INST-01..22 (`install` / `uninstall`)

Spec vs script disagreements found while writing `port-tests/test_inst.py`. Rule applied: the
script wins; the test asserts what the script does and the disagreement is recorded here.

### Disagreements

#### T-INST-05 (fish edge) — the warn line prints the expanded path, not `~/.local/bin`
- Spec: stdout `warn … add ~/.local/bin to PATH yourself`.
- Script (`install` line 230): `warn "shell rc" "SHELL='${SHELL:-unknown}' — add $LOCAL_BIN to PATH yourself"`
  where `LOCAL_BIN="$HOME_DIR/.local/bin"` (line 18) — the expanded absolute path.
- Asserted: `  → shell rc <pad> SHELL='/bin/fish' — add <HOME>/.local/bin to PATH yourself`.

#### T-INST-13 — "no prompt consumed" is not visible on stdout
- Spec: `no prompt consumed`.
- Script: `confirm` uses `read -r -p` (line 45); bash prints the `-p` prompt only when stdin is a
  terminal, so under the harness (piped stdin) neither the replace prompt nor its absence shows up
  in the output. The only observable is what the NEXT reader receives.
- Asserted: stdin `y\ny\n`; `~/.tmux.conf` unchanged + no `.bak` proves the replace prompt did not
  take the second `y`; that `y` therefore reaches the nvim `[y/N]` (`install` line 407) and
  `setup/nvim.sh install` links `$XDG_CONFIG_HOME/nvim → <REPO>/nvim`. `XDG_CONFIG_HOME` is pinned
  to `<HOME>/.config` inside the temp tree (it would otherwise be inherited from the operator's
  env) and the symlink is asserted with `os.readlink`.

#### T-INST-14 — `.bak` stamps: the C10 backup uses install's STAMP, claude.sh uses its own
- Spec: "the newest `settings.json.bak.*` has `statusLine.command == "old.sh"`".
- Script: `install` takes `STAMP` once at start (line 25) and the C10 edit writes
  `<real>.bak.$STAMP` (line 383); `claude.sh` takes its own `date` stamp and writes its own
  `.bak` before that. Both snapshots predate the statusLine edit, so both hold `old.sh`; when the two
  stamps fall in the same second the C10 write overwrites claude.sh's file (one `.bak` left).
- Asserted: newest by mtime (not by name) has `statusLine.command == "old.sh"`, and the path on
  the `backup:` line matches `<settings>.bak.<14 digits>` and exists.

#### T-INST-15 — "settings.json.bak.<STAMP> is written again"
- Same stamp mechanics as above: a re-run within the same second would overwrite rather than add a
  `.bak`, so the count of `.bak` files is not a safe signal.
- Asserted: the newest `settings.json.bak.*` mtime is >= the re-run's start time; `.zshrc.bak.*` /
  `~/.tmux.conf.bak.*` inventories are asserted via a full `snapshot($HOME)` equality (no writes
  happen there at all, so collisions are moot).

#### T-INST-16 (`--bogus`) — `nothing touched`
- Asserted as: `$HOME` snapshot unchanged, `$TX_IDE_HOME` still absent, `$CLAUDE_CONFIG_DIR` empty,
  stdout empty. The argument loop (`uninstall` lines 26–33) runs before the banner, so stdout is
  exactly `""`.

#### T-INST-17 — "hooks/profile restored per T-INST-33/34"
- Asserted only at the level this case owns: `hooks`, `statusLine` and the four top-level profile
  keys absent from settings.json, `permissions.deny` absent, `theme` preserved. The reference
  leaves `"permissions": {}` behind (claude.sh strips the leaf, not the emptied parent) — not
  asserted either way here; that is T-INST-34's surface.

#### T-INST-18 — "settings.json unchanged, no `.bak`"
- Spec: no `.bak`.
- Script: the statusLine step writes nothing (correct), but `uninstall` then runs
  `setup/engines/install.sh uninstall` → `claude.sh` → `run_settings_py uninstall`, and
  `do_uninstall` returns an (empty) plan whenever ANY marker exists (`claude.sh` lines 416–441), so
  `write(data)` runs: settings.json is rewritten (byte-identical, since the fixture is
  `indent=2` + newline) and a `settings.json.bak.<stamp>` is dropped (`claude.sh` line 267).
- Asserted: settings.json bytes unchanged; the `Claude statusLine (C10 reverse)` section contains
  `statusLine not ours / absent — left alone` and no `backup:` line; the `.bak` files on disk are
  exactly the set announced by `backup:` lines anywhere in the output (so a port that also stops
  claude.sh's needless rewrite still passes with 0 == 0).

#### T-INST-18 (legacy edge) — the `.bak` written by the statusLine step is clobbered
- The statusLine step writes `<real>.bak.$STAMP` (uninstall's STAMP); claude.sh then writes its own
  `.bak.<its stamp>` — same second → same name → overwritten with claude.sh's post-removal snapshot.
- Asserted: the `statusLine removed  (<cmd>)` + `backup: <settings>.bak.` lines, `statusLine` gone
  from settings.json, `theme` preserved, at least one `.bak` present. The `.bak` CONTENT is not
  asserted (it depends on sub-second timing in the reference).

#### T-INST-21 — Q10 (FIX) legs
- Parity leg (`test_t_inst_21_parity_…`): `uninstall` lines 173–178 `rm -f` `tmux.conf`,
  `config.json` AND `log.jsonl` unconditionally — asserted `log.jsonl` gone with a `removed` line.
- Fixed leg (`test_t_inst_21_fixed_…`, `@expected_failure_on_python`): `log.jsonl` byte-identical,
  no `→ <HOME>/log.jsonl` line.
- `--purge` and the "(a) then (b)" edge behave the same under both decisions → plain methods.

#### T-INST-09 / T-INST-15 — Q22 (FIX) legs
- Parity: `install` line 270 compares `$(cat "$TX_IDE_CONF")` (trailing newline stripped) with
  `$desired_content` (ends in `\n`) → never equal → `written` + a byte-identical `.bak` on every run.
  T-INST-09 parity asserts `written` + one `.bak` == desired bytes; T-INST-15 parity asserts 17
  `already current` lines (12 events + 5 profile keys), the shim line `written`, one shim `.bak`.
- Fixed legs (`@expected_failure_on_python`): `already current`, no shim `.bak`; T-INST-15 fixed
  expects 18 `already current` lines (the shim's own line included).

### Harness notes (not spec disagreements)

- The kit's fake `fzf` drains stdin by default (`read_stdin` defaults to true for `fzf` in
  `FAKE_TEMPLATE`). `install`'s dependency check runs `fzf --version` (line 170) BEFORE the
  `~/.tmux.conf` prompt, so the fake swallowed every answer after the initial `y` — T-INST-12
  (`y\ny\n` → replace) could never see its second `y`. `Installer.__init__` now calls
  `case.fakes.configure("fzf", read_stdin=False)`; anything else driving `install` through the
  kit's fakes needs the same. (Kit gap; worked around in `test_inst.py`, `txkit.py` untouched.)
- `uninstall` drives `install.sh uninstall` for every registered engine regardless of CLI presence
  (`driving: antigravity claude codex`); the antigravity/codex `already absent` chatter is harmless
  and not asserted.
- Under the private tmux wrapper the claude.sh global-hook step reports `no tmux server — will
  apply on next start` (no session was ever started); T-INST-17 still passes `TX_TMUX_SOCKET` per
  the spec so the path is pinned to the private socket.

### Not implemented

None — every case T-INST-01..22 and every listed Edge has at least one method.

## NOTES-05-inst-b — T-INST-23..47, 50..52 (`port-tests/test_inst_engines.py`)

Where the spec's Then and the script disagreed, the script was asserted (the spec is downstream of
the code) and the disagreement is recorded here. T-INST-48/49 are DEFERRED (D3): no tests written.
Result against the bash/Python reference: 54 pass, 5 skip (1 D9 `_fixed_` leg, 4 tmux-version gated).

### Spec vs script

| Case | Spec says | Script does | Asserted |
|---|---|---|---|
| T-INST-23 | `--settings` without PATH → exit ≠0 | bash `${2:?--settings needs a PATH}` (claude.sh:80) exits **1** with `claude.sh: line 80: 2: --settings needs a PATH` | exit ≠0 + the message (as the spec words it; the port may choose 2) |
| T-INST-25 | Given "settings copy `{}`", Then "no `.bak` (file did not exist)" | `write()` (claude.sh:266-268) writes a `.bak` whenever the file exists — even for `{}`; `load()` (259-260) treats an absent file as `{}` | the copy is left ABSENT so both halves hold (no `.bak`); the existing-file `.bak` is pinned by T-INST-26 |
| T-INST-26 | `<copy>.bak.<STAMP>` == original | the `.bak` is `json.dump(data_before, indent=2)+"\n"` (claude.sh:268), not the original bytes | byte equality — true only because the fixture writes the original in the same `indent=2 + "\n"` shape; a hand-formatted original would NOT round-trip |
| T-INST-27 | plan `repoint  /old/… → <new>/…` | `f"repoint  {target}  →  {new_command}"` (claude.sh:372): two spaces each side of the arrow | the code's spacing |
| T-INST-31 | plan `context profile     skipped (--no-context-profile)` (5 spaces) | `f"  → {event:<18} {action}"` (claude.sh:486) pads to 18 → 3 spaces | the code's padding via `engines_plan_line` |
| T-INST-31 / 33 | uninstall restores "verbatim"; T-INST-33 "hooks dropped when empty" | `path_del` (claude.sh:325-330) pops only the leaf, so the `permissions` parent that install's `path_set` created stays behind as `"permissions": {}` | `{"permissions": {}}` after uninstall of a fresh `{}` install (33 hooks-dropped edge, 31 carry edge). Candidate quirk for Appendix B: uninstall is not byte-verbatim when `permissions` did not exist before tx |
| T-INST-33 | `.bak.<STAMP>` written | STAMP has 1 s resolution (claude.sh:51); an install and uninstall in the same second share the name and the second overwrites the first | membership of the pre-uninstall bytes in the `.bak` inventory, not a count |
| T-INST-34 Q9 parity | (FIX: exit 1 + one-line message) | `path_del` raises `KeyError: 'permissions'` → Python traceback on stderr, heredoc exits 1, `set -e` stops the script before `write()` and before `remove_shim` | parity leg: exit 1, `Traceback` + `KeyError: 'permissions'` in stderr, copy bytes unchanged, `.bak` inventory unchanged (the install's `.bak` remains; none added), sidecar present, all 6 shims still present. Fixed leg `@expected_failure_on_python` as specified |
| T-INST-35 / 36 | hook set / restored / unset on the `TX_TMUX_SOCKET` server | the liveness probe is `tmux_cmd info` (claude.sh:523). On tmux 3.4 `show-messages` has flags `CMD_AFTERHOOK\|CMD_CLIENT_TFLAG` (no `CMD_CLIENT_CANFAIL`), so `info` from an unattached client always fails with `no current client` and the step degrades to `no tmux server — will apply on next start`. tmux 3.6 adds `CMD_CLIENT_CANFAIL` + `tc != NULL` guards (verified in `cmd-show-messages.c` at tags 3.4 and 3.6) | the four hook-applied methods are gated `@requires_tmux(min="3.6")` (D13 floor, Q21 precedent). Their bodies were validated green against a scratch copy of claude.sh whose probe is `list-sessions`. Candidate quirk for Appendix B: on tmux 3.4 the reference NEVER installs the global hook from a non-tmux shell; the port should probe with `list-sessions`/`has-session` (or its own tmux adapter), not `info` |
| T-INST-35 | `show-hooks -g session-closed` == `run-shell -b …` | tmux 3.4 prints `session-closed[0] <value>`; an unset hook prints a bare `session-closed` | the `session-closed[N] ` prefix is stripped with the script's own regex (claude.sh:530); "unset" == `""` |
| T-INST-37 | `TMUX_TMPDIR=<tmp>` makes the default socket private | in this harness the `tmux` wrapper pins EVERY call to the private server, so the default socket IS `self.tmux` and `TMUX_TMPDIR` is irrelevant | `TX_TMUX_SOCKET` unset, foreign hook on `self.tmux` untouched, no `.session-closed.prev`, sleeper alive, both `live-only` lines, sidecar beside the copy, `<HOME>/claude-managed.json` absent |
| T-INST-40 | hook step applied (Given `TX_TMUX_SOCKET`) | see 35/36 — on 3.4 the hook step warns; the case's Then only concerns mx-speaker | mx-speaker facts only (`wait()` == -15 proves SIGTERM; `kill -0` raises `ProcessLookupError` after reaping) |
| T-INST-42 symlink edge | "realpath rewritten atomically" | `backup()` (codex.sh:168-170) writes `.bak` beside the REALPATH, never beside the symlink | target rewritten, symlink intact, `.bak` beside the target only, no `.tx-codex.*.tmp` left |
| T-INST-44 absent/empty edge | `no .bak` | additionally the stdout line has no `(backup …)` tail (codex.sh:235) | line ends right after the path |
| T-INST-45 | "first match only" | `BLOCK_RE.sub("", content, count=1)` (codex.sh:249) | extra two-block variant: only the first block is stripped |
| T-INST-52 `--engine nope` | stderr `no engine script at <dir>/nope.sh`, exit 2 | `driving: nope` is printed to stdout first (install.sh:103 runs before `run_engine`) | stderr exactly + exit 2 (stdout not constrained) |

No other disagreement: T-INST-24, 28, 29, 30, 32, 38, 39, 41, 43, 46, 47, 50, 51 assert the Then
literally (exact shim bytes, exact marker text `indent=2 + "\n"`, exact status lines, snapshot
equality for dry-run, exact stderr for the codex sandbox guard and the install.sh errors).

### Not implemented

None. T-INST-48/49 are deferred by instruction (no antigravity tests).

### Kit gaps (worked around locally)

- `FakeBins.remove()` is one-way and `Installer.__init__` always removes `agy`, so a second
  `Installer(case, codex=True)` inside a test raises `FileNotFoundError`. T-INST-50 puts `codex` back
  as a silent `#!/bin/sh` stub in `fakes.bin_dir` (install.sh only asks `command -v codex`). A
  `FakeBins.add(name)` / `restore(name)` would remove the workaround.
- `Installer` exposes neither the mx-speaker pid file (`$CLAUDE_CONFIG_DIR/mailbox/mx-speaker.pid`)
  nor the stash file (`$TX_IDE_HOME/hooks/.session-closed.prev`) nor the codex `hooks.json` /
  `config.toml` paths; defined as `engines_*` helpers — candidates to fold into `Installer` on merge.
- `Installer.claude_shim_text` / `tmux_session_closed` exist for claude; the codex shim body and
  the tx-owned `hooks.json` text have no kit builder — `engines_codex_shim_text` /
  `engines_codex_hooks_json` here.
- This host's tmux 3.4 cannot exercise claude.sh's hook step at all (see T-INST-35/36 above); the
  four gated tests need a ≥ 3.6 host (CI) to run.

## NOTES-05 — STATUS (`claude/statusline.sh`, T-STATUS-01..10)

Every Then in the STATUS section was checked against the script before being asserted. No Then
contradicts the script; the entries below are the places where the spec is looser, more concrete,
or less precise than the script, and what `test_status.py` asserts as a result.

| Case | Spec says | Script does (line) | Asserted |
|---|---|---|---|
| T-STATUS-01 | repo at `/tmp/r/myrepo` | only `basename "$(git rev-parse --show-toplevel)"` matters (95–98) | `GitFixture(self.root, "myrepo")` under the temp root; the full concrete byte string from the spec, no trailing newline, exit 0, empty stderr |
| T-STATUS-01 | tokens 1000+200000+1049000 → `1.2M` | awk `%.1f` of 1.25 rounds half-to-even → `1.2` (35) | `1.2M` as the spec says (agrees; noting it because 1.25 is a rounding boundary the port must reproduce with C `printf` semantics) |
| T-STATUS-02 | "only these paths influence output" | jq extractions at 3–19; `.total_input_tokens` and any sibling of `.context_window.current_usage.{input,cache_creation,cache_read}` are never read | payload with decoys at every level (`total_input_tokens`, `context_window.total_input_tokens`, `current_usage.output_tokens`, `model.id`, `effort.label`, `rate_limits.one_hour`, `seven_day.remaining_percentage`, a top-level unknown object containing look-alike keys); exact stdout computed from the real fields only |
| T-STATUS-03 (b) | cwd `/tmp/plain/dir` | `basename "$cwd"` when `git rev-parse` fails (100) | `<root>/plain/dir`; the test first proves with `git -C … rev-parse --show-toplevel` that it is not inside a repo |
| T-STATUS-03 (c) | "only line 2 printed with no leading `\n`" | `printf "%b" "$line2"` branch (145–146) | stdout == line 2 exactly |
| T-STATUS-05 | Source cites `lib/tx/engines/engine_adapter.py::EFFORT_LEVELS` | the mapping lives in the script's `case` (22–28); the Python table is not observable black-box (D1) | the script's table only: low/medium/high/xhigh/max → 1..5, `turbo` passes through, absent → no segment |
| T-STATUS-06 | 999950 → `1000.0K` | awk `%.1fK` of 999.95 → `1000.0K` (37) — verified, not `999.9K` | `1000.0K` |
| T-STATUS-07 edge | `used_percentage` absent → no 7d segment "at all" | `format_rate_limit` returns before looking at `resets_at` (46) | payload with `resets_at` present but no `used_percentage` → no 7d segment even though a countdown could have been computed |
| T-STATUS-08 | `5%` | `printf '%.0f' 5` → `5` (47) | `used_percentage: 5` (integer) |
| T-STATUS-09 edge | "jq errors on stderr" | eight separate `jq -r` invocations each print `jq: parse error: …` (3–19); exit 0 because the last statement is a backgrounded job (151) | stderr non-empty and every line starts with `jq: `; the count (8) is an implementation detail and is not asserted |
| T-STATUS-10 | "within 0.3 s+" | the POST is fired after the printf from a backgrounded subshell (151); in practice the request reaches the listener ~10–30 ms *before* `subprocess.run` returns | polled with a 3 s deadline / 20 ms interval |
| T-STATUS-10 | "stdout pipe is closed before the POST completes" | `push_anthropic_usage >/dev/null 2>&1 &` (151) | handler blocks on an Event; after `subprocess.run` returns the handler is still blocked **and** `returned_at − received_at < 0.15 s` (= curl timeout / 2). The second bound is what actually discriminates: a mutant that keeps stdout open (`push_anthropic_usage 2>/dev/null &`) still returns — after curl's 0.3 s timeout — so the "still blocked" check alone passes for it; measured gap is ≈ +0.303 s for the mutant vs ≈ −0.02 s for the real script |
| T-STATUS-10 edge | listener hangs → "returns promptly (curl `-m 0.3`)" | same backgrounding; the script itself exits in ~0.1 s regardless of curl | `subprocess.run(timeout=5)` returns with exit 0 in < 2 s while the handler is still blocked |
| T-STATUS-10 edge | "missing → `null`" | `--argjson … null` defaults (77–78) | extra method: seven_day only → `{"five_hour":{"used_percentage":null,"resets_at":null},"seven_day":{…}}` |
| T-STATUS-10 edges | port file absent / empty / no `rate_limits` → "no connection attempted" | early returns at 71, 73, 74 | listener up, no request recorded after a fixed 0.5 s wait (the only fixed sleep in the file) |
| T-STATUS-10 edge | `TX_IDE_HOME` unset → `$HOME/.tx-ide/sessions-graph.port` | `${TX_IDE_HOME:-$HOME/.tx-ide}` (70) | env without `TX_IDE_HOME`, port file under `self.home.user_home/.tx-ide/` |

### Kit gaps worked around locally

- `run_tx` only runs `TX_BIN`; there is no runner for an arbitrary script under `scrubbed_env`.
  `TestStatus.run_statusline` calls `subprocess.run(["bash", statusline.sh], …, encoding="utf-8")`
  with `scrubbed_env(self.home, extra=…)`.
- No HTTP-listener fixture. `UsageListener` (in `test_status.py`) is an `http.server.HTTPServer`
  on `127.0.0.1:0` in a daemon thread recording method/path/content-type/body/arrival time, with an
  optional hold-until-released mode; `close()` releases before `shutdown()` so a held handler cannot
  deadlock teardown.
- `TxCase.wait_until` was reusable as-is for the request poll.

## NOTES-05 — NVIM (T-NVIM-01..20)

Every Then was probed against `bin/tx` (Python) on a private tmux server before being asserted.
No Then contradicts `lib/tx`; the rows below are where the spec's Given/edge does not hold as
written, and what `test_nvim.py` asserts instead.

| Case | Spec says | Code does | Asserted |
|---|---|---|---|
| T-NVIM-04 | file `/tmp/p/my plan (v2).md` | `spawn.py::for_nvim` only `shlex.quote`s the string; the fake nvim never opens it | the same basename under the temp root (`<root>/p/my plan (v2).md`) so nothing is written outside the test tree; `cmd`, fake argv and stdout carry that path |
| T-NVIM-05 edge | `--cwd` omitted → calling pane's path, "else the `tx` process's own cwd" | `Command._default_cwd` = `tmux display-message -p '#{pane_current_path}'` **without** a `$TMUX` gate (Q20): outside tmux with a live server it takes the server's current pane, not the caller's cwd | the pane leg (inside W via `TMUX_PANE`) and the plain-terminal leg with **no live sessions** (server absent → own cwd); the live-server plain-terminal leg is Q20's FIX and is pinned by T-SPAWN-19 / T-TMUX-09 in section 02, not duplicated here |
| T-NVIM-06 edge | `--cmd 'nvim-qt'` → role `other`, "the binary need not exist for the record assertion" | `service._spawn` writes the record only after `tmux set-option @tx_id` succeeds; a command that exits at once takes the session (and, when it was the last one, the server) with it, so `tx spawn` exits 1 with `tmux set-option … failed` and no record | a long-running `nvim-qt` stand-in (a copy of the kit's `zsh` fake) on PATH; role `other` / `shell` and the log lines as the spec says |
| T-NVIM-09 | `tmux list-sessions -F '#{@tx_id}'` + `tx ls --json` (Q11 FIX, marker) | `tx ls` takes no arguments on Python (`--json` → argparse exit 2) | split: `_parity_tx_id_lines` (green) asserts the list-sessions lines; `_fixed_ls_json` + `_fixed_ls_json_empty` carry `@expected_failure_on_python` and also assert that a record planted under `$HOME/.tx-ide/sessions/` is not listed |
| T-NVIM-10 | fake claude "reads its pty stdin raw and appends each chunk with a timestamp" | no such fake existed in the kit | kit commit `stdin_log=True` knob (canonical mode + ICRNL off, echo kept, one JSON line per `os.read` chunk); concatenated chunks == envelope + `\r`, and the `\r` chunk's read time is ≥ 0.3 s after the last envelope chunk's (`service._deliver` sleeps 0.3 s between the two `send-keys`) |
| T-NVIM-10 edge | "record exists but tmux session gone" | a crafted llm record without `engine` is an unreadable record (warning on stderr) | the dead namesake is a crafted `shell` record so stderr is exactly the one error line |
| T-NVIM-16 | `sock=$(lsof -U -a -p $nvim_pid \| awk '{print $NF}' \| grep nvim \| head -1)` | lsof 4.95 (this host) prints `… /run/user/1000/nvim.<pid>.0 type=STREAM (LISTEN)`, so `$NF` is `(LISTEN)` and the skill's recipe finds nothing; also the real nvim stalls on a hit-enter prompt (E185 `tokyonight-moon` missing) which blocks its RPC server | the socket is taken from any lsof field that is an absolute path containing `nvim`; a stub `~/.config/nvim/colors/tokyonight-moon.vim` under the temp HOME keeps the companion responsive. `agents/skills/tx-code-tours/SKILL.md` should drop `$NF` (skill fix, out of this suite's scope) |
| T-NVIM-17/18/19 | files under `/tmp/ct/` | paths only flow through `stops.lua` and the quickfix entries | `<root>/ct/f.txt` + `<root>/ct/stops.lua`; `json_encode` prints `, ` separators, so the rows are compared as parsed JSON rather than the spec's unspaced literal |
| T-NVIM-20 | new behaviour (`nvim_socket`) | not implemented in Python | two `_fixed_` methods (fake nvim argv/record/kill; real nvim `--remote-expr`), both `@expected_failure_on_python` |

Skips on this host: none beyond the four `expected_failure_on_python` legs (09 ×2, 20 ×2).
`nvim` (v0.12.3) and `lsof` are present, so 16–19 run for real.


## Review pass (2026-09-23) — review artifact 90c3c7d1-6c52-4520-af2b-35e81de797b8, spec rev 5

Branch `feat/port-tests-fix-05`. Every non-OK row of the review was applied; the section is green on
the reference (tmux 3.4 host) and `check_coverage.py` reports 0 MISSING for INST / STATUS / NVIM.
Corrections to the earlier notes above: the rev-4 FIX legs for Q31 (T-INST-01), Q33 (T-INST-18),
Q29 (T-INST-31/33) and Q28 (T-INST-35/36) did NOT exist before this pass ("Not implemented: None"
was wrong on that count), and `test_inst_engines.py` was merged into `test_inst.py` at integration.

### Entry points (H9 / D16)

`test_inst.py` runs `TX_INSTALLER`, `TX_UNINSTALLER` and `<TX_ENGINE_SETUP dir>/{install,claude,codex}.sh`
(the kit's `TX_ENGINE_SETUP` names `install.sh`; `claude.sh` / `codex.sh` sit beside it);
`test_status.py` runs `bash $TX_STATUSLINE`; `test_nvim.py` drives `tmux-nav` through the kit's
`TX_HELPERS_DIR` copy (`self.helper("tmux-nav", …)`). `REPO` survives only in expected strings
(`readlink == <REPO>/bin/<tool>`, the `PYTHONPATH=<REPO>/lib` exec line the reference bakes into
shims / the tmux hook, `tmux/tmux.conf`, the code-tour skill script).

### Marker pairs (H9b / D17)

Parity legs of FIX quirks now carry `@python_reference_only`: T-INST-09/15 (Q22), 21 (Q10), 34 (Q9),
plus the new pairs 01 (Q31), 18 (Q33), 31 and 33 (Q29), 35 and 36 (Q28). Shared assertions were
moved out of the marked legs: T-INST-31's main leg and T-INST-33's restore/strip leg compare the
settings subtree EXCLUDING `permissions` (and assert `permissions.deny` gone); the plain
`_33_hooks_dropped_when_empty` asserts only `hooks` absent and nothing but `permissions` left.

### How the fixed legs were validated

Not against a port (none exists yet): against scratch copies of the reference scripts patched to
the Appendix-B decisions, placed inside the worktree so `REPO_ROOT` / `LIB_DIR` resolve unchanged
and selected with `TX_IMPL=rust TX_INSTALLER=… TX_UNINSTALLER=… TX_ENGINE_SETUP=…`:
`install` with `\$(tx)` / `\$(tx start)` escaped (Q31); `claude.sh` with `tmux_cmd list-sessions`
as the liveness probe (Q28), `path_del` dropping a two-level parent it emptied (Q29), and no
`write()` on an empty uninstall plan (Q33); `uninstall` pointed at that `claude.sh`. All eight
fixed legs pass there and all fail against the unpatched reference. The scratch copies are not
committed. T-STATUS-10's main leg was checked the same way against a mutant statusline whose
background push keeps stdout open (`push_anthropic_usage 2>/dev/null &`): it fails as intended.

### SPEC rows — code behaviour asserted, spec corrected in rev 5

| Case | Review | Asserted |
|---|---|---|
| T-INST-31 | SPEC: Appendix B listed 31 under Q29 but the case body had no Split/Marker line; the Q29 assertion sat unmarked in the main leg | pair `_31_parity_uninstall_leaves_emptied_permissions` (`{"disableWorkflows": false, "permissions": {}}`) / `_31_fixed_uninstall_drops_emptied_permissions` (`{"disableWorkflows": false}`); main leg excludes `permissions` |
| T-INST-35 | SPEC: (1) the Q28 fixed leg was gated `tmux ≥ 3.6` although a `list-sessions` probe works on any version; (2) the "no tmux server" edge aimed at the kit's own (live) socket and passed only because of the Q28 bug on 3.4 | fixed legs `_35_fixed_*_on_any_tmux` / `_36_fixed_*_on_any_tmux` are ungated; the shared hook-applied legs stay gated to the 3.6 floor; the Q28 PARITY legs (`_35_parity_…`, `_36_parity_…`: live server on the socket, yet `no tmux server — will apply on next start`, hook and stash file untouched) are gated BELOW 3.6 — the only place the quirk is observable; the no-server edge aims at `txkit-dead-<hex>` (claude.sh's `-L $TX_TMUX_SOCKET` follows the PATH wrapper's `-L <kit>` and wins) and asserts the kit server's hook unchanged and no server started on the dead name |

### Other rows, by class

- WEAK — T-INST-01 (stub `tx` now logs argv to `<root>/tx-stub.log`; `Installer.stub_calls()`),
  T-INST-18 (fixed leg: no `.bak`, no `backup:` line, inode + `mtime_ns` unchanged; parity twin
  pins the one needless `.bak` == the file's bytes), T-INST-36 (ungated fixed copies), T-INST-38
  (both dry runs now run with `TX_TMUX_SOCKET` set and a hook planted on the kit server; the hook
  and the stash file must be untouched — the `live-only (skipped here)` lines are still printed
  because DRY_RUN short-circuits before the socket check), T-STATUS-03 (linked-worktree edge uses
  `<linked>/src`, where `basename $cwd` and the main checkout's toplevel both give the wrong answer),
  T-NVIM-01 (`--tag TAG` must stand outside `[…]`; new `_01_env_is_repeatable`: `--env A=1 --env B=2`
  → record `env` and the fake's env carry both), T-NVIM-09 (the `$HOME/.tx-ide` ghost now has a
  live session carrying its `@tx_id`; `tx ls` names == `tx ls --json` names both ways), T-NVIM-14
  (top-level edge starts on L via `select-pane`, so a wrapping nav would show), T-NVIM-20 (socket
  file planted before `tx kill`, `<HOME>/nvim/` asserted a dir, real-nvim leg gains the kill).
- WRONG — T-INST-31, T-INST-33 (see the pairs above), T-INST-35 (dead socket).
- FRAGILE — T-NVIM-03/05/06/20 renamed to `non_hex_name()` names (`dx1`, `edx1`, `ex1`, …);
  T-NVIM-06 edge legs assert `(type, msg)`; T-NVIM-10's `delivered()` waits until the joined
  chunks == envelope + `\r` (a count of 2 returned early when the envelope split across reads) and
  the Enter gap is `≥ 0.25 s`; the unset-`TX_SESSION_ID` leg runs inside a spawned view's pane
  (`#S` == `view1`) and asserts the view name never reaches the target; T-NVIM-16 spawns with
  `--env XDG_RUNTIME_DIR=<root>/xdg-runtime` (the socket lands there, never in the operator's
  `/run/user/<uid>`), picks the pane child whose comm is `nvim` rather than `pgrep -P | head -1`,
  and kills it at cleanup (`nvim_pid_of`, shared with T-NVIM-20's real leg).
- T-STATUS-10 (FRAGILE + WEAK) — replaces the rev-4 row above. Main leg: `Popen` + `communicate`;
  at stdout EOF either no request has been recorded yet or a `curl … 127.0.0.1:<port>/api/anthropic-usage`
  process is still alive (`/proc` cmdline scan) — a script that held stdout until curl gave up hands
  over EOF only after the request is recorded AND curl is gone; `accepted == 1` (the listener now
  counts every accepted connection in `verify_request`, so a GET or a bare connect is no longer
  invisible); after `release()` the curl disappears (positive control on the /proc probe). The
  no-connection edges use a fresh listener per subtest, assert `accepted == 0` and no live curl
  after 0.5 s, and end with a positive-control subtest that DOES see the connection. The hang edge
  waits for the curl to time out on its own.
- Minor rows counted OK in the review but cheap: T-INST-15 `.bak` mtime check gets 1 s of slack;
  T-STATUS-05 also covers the `effort` key absent entirely; T-NVIM-08's error legs assert no log
  line was added; H8 spawn log lines added to T-NVIM-02/03/04/13/20 and to T-NVIM-10's unknown /
  dead-target legs (no line added).

### Left as is (needs a spec decision, not a test edit)

- Cross-cutting 7: shims and the tmux hook are byte-compared against the reference's
  `PYTHONPATH="<LIB>" "python3.14" -m tx hook …` line. The INST preamble lets a port substitute its
  own binary, but nothing says what the port's line IS; until it does, the exact-text assertions
  stand (they are what the reference writes) and INST implicitly needs `python3.14`.
- The H9 note that a scalar entry point "may carry arguments" (`TX_INSTALLER="tx install"`): the kit
  resolves each variable as ONE path (`_entry_point`), so a port wraps such a command in a script,
  as the README says.
