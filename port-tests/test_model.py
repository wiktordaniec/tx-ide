"""T-MODEL — the record model (lib/tx/session.py) as seen through `tx show` / `tx ls` /
`tx history` / `tx chat ls` and the store-scan stderr. DROPPED: 04, 19, 20, 22, 24.

Spawned names here avoid single hex letters (`s`, `e`, …): name resolution asks tmux for `@tx_id`
of the session `-t <name>`, and tmux prefix-matches that against the uuid-named sessions, so `e`
can resolve to whichever record's uuid happens to start with `e` (see NOTES-01)."""

from __future__ import annotations

import json
import time

from txkit import TxCase, expected_failure_on_python

SKIP = "tx: skipping unreadable record"
UNSUPPORTED = (
    "record schema_version={version} is unsupported (expected 6); tx-ide does not back-migrate "
    "older records on load (§9) — run `tx migrate` to upgrade older records"
)
NO_RECORD = "tx show: no record for 'nosuch'\n"

LLM_KEYS = [
    "schema_version", "id", "name", "role", "state", "cwd", "cmd", "tags", "group", "env",
    "parent", "pid", "attached_to", "created_at", "ended_at", "engine", "last_activity", "chats",
    "turn_started_at",
]
OTHER_KEYS = LLM_KEYS[:15] + ["artifact_id"]
CHAT_KEYS = [
    "id", "role", "cwd", "transcript_path", "origin", "bundle_path", "started_at", "ended_at",
    "summary", "engine",
]
LOCATION_KEYS = ["host", "window_index", "window_name", "pane_id", "pane_index"]
LOCATION = {"host": "Views", "window_index": "1", "window_name": "work", "pane_id": "%41", "pane_index": "1"}
MODEL_08_CHAT = {
    "id": None, "role": "fork", "cwd": "/r", "transcript_path": "/t",
    "origin": {"how": "fork", "session_id": "s1", "chat_id": "c0"},
    "bundle_path": None, "started_at": 1.0, "ended_at": None, "summary": "", "engine": "codex",
}
MODEL_09_ID = "11111111-aaaa-4bbb-8ccc-000000000001"
MODEL_09_RECORD = {
    "schema_version": 6, "id": MODEL_09_ID, "name": "worker-1", "role": "llm", "state": "waiting",
    "cwd": "/repo", "cmd": "", "tags": ["docs"], "group": None, "env": {}, "parent": None,
    "pid": None, "attached_to": [], "created_at": 900.0, "ended_at": None, "engine": "claude",
    "last_activity": 970.0, "chats": [], "turn_started_at": None,
}
MODEL_10_ID = "22222222-aaaa-4bbb-8ccc-000000000002"
MODEL_10_RECORD = {
    "schema_version": 6, "id": MODEL_10_ID, "name": "ed", "role": "nvim", "state": "exited",
    "cwd": "/repo", "cmd": "", "tags": [], "group": None, "env": {}, "parent": None, "pid": None,
    "attached_to": [dict(LOCATION)], "created_at": 950.0, "ended_at": 960.0, "artifact_id": None,
}


class TestModel(TxCase):
    # ----- helpers ---------------------------------------------------------------------------

    def write_raw(self, name: str, data) -> None:
        """A record file written verbatim (indent 2, no trailing newline) — for defective shapes
        `Records` cannot express (missing keys, wrong versions)."""
        self.home.sessions_dir.mkdir(parents=True, exist_ok=True)
        (self.home.sessions_dir / f"{name}.json").write_text(json.dumps(data, indent=2))

    def model_09_file(self) -> None:
        reversed_record = dict(reversed(list(MODEL_09_RECORD.items())))
        del reversed_record["turn_started_at"]
        reversed_record["zzz_extra"] = 1
        self.write_raw(MODEL_09_ID, reversed_record)

    def model_10_file(self) -> None:
        record = {**MODEL_10_RECORD, "engine": "claude", "chats": [], "last_activity": 1.0, "kind": "process"}
        del record["group"]
        self.write_raw(MODEL_10_ID, record)

    def show(self, target: str) -> dict:
        result = self.tx(["show", target])
        self.assertEqual(result.code, 0, result.err)
        return json.loads(result.out)

    def states_of(self, listing: str) -> dict[str, str]:
        return {line.split()[0]: line.split()[1] for line in listing.splitlines()[1:]}

    # ----- T-MODEL-01 ------------------------------------------------------------------------

    def test_t_model_01_role_values(self):
        ids = {
            "llm": self.records.llm(name="l"),
            "nvim": self.records.other(name="n", role="nvim"),
            "shell": self.records.other(name="s", role="shell"),
            "other": self.records.other(name="o", role="other", artifact_id="art"),
        }
        self.records.other(id="g", name="g", role="view")
        for role, session_id in ids.items():
            result = self.tx(["show", session_id])
            self.assertEqual(result.code, 0, result.err)
            self.assertIn(f'"role": "{role}"', result.out)
        result = self.tx(["show", "nosuch"])
        self.assertEqual(result.code, 1)
        self.assertEqual(result.out, "")
        self.assertEqual(result.err, f"{SKIP} g.json: 'view' is not a valid Role\n{NO_RECORD}")

    def test_t_model_01_spawn_infers_role(self):
        for name, argv in (
            ("sh", ["spawn", "sh", "--tag", "t", "--cmd", "bash"]),
            ("ot", ["spawn", "ot", "--tag", "t", "--cmd", "sleep 1000"]),
            ("nv", ["spawn-nvim", "nv", "--tag", "t"]),
            ("wk", ["spawn", "wk", "--tag", "t", "--cwd", str(self.git.path), "--cmd", "claude"]),
        ):
            result = self.tx(argv)
            self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self.show("sh")["role"], "shell")
        self.assertEqual(self.show("ot")["role"], "other")
        self.assertEqual(self.show("nv")["role"], "nvim")
        self.assertEqual(self.show("wk")["role"], "llm")

    # ----- T-MODEL-02 ------------------------------------------------------------------------

    def test_t_model_02_state_values_and_is_terminal(self):
        for state in ("alive", "working", "waiting", "idle", "exited", "archived"):
            session_id = self.records.llm(id=state, name=state, state=state)
            if state in ("alive", "working", "waiting", "idle"):
                self.live(session_id)
        self.records.llm(id="s", name="s", state="running")
        listing = self.tx(["ls"])
        self.assertEqual(listing.code, 0)
        self.assertEqual(self.states_of(listing.out), {s: s for s in ("alive", "working", "waiting", "idle")})
        history = self.tx(["history"])
        self.assertEqual(history.code, 0)
        self.assertEqual(self.states_of(history.out), {"exited": "exited", "archived": "archived"})
        self.assertFalse(set(self.states_of(listing.out)) & set(self.states_of(history.out)))
        result = self.tx(["show", "nosuch"])
        self.assertEqual(result.code, 1)
        self.assertEqual(result.err, f"{SKIP} s.json: 'running' is not a valid State\n{NO_RECORD}")

    # ----- T-MODEL-03 ------------------------------------------------------------------------

    def test_t_model_03_state_initial_for(self):
        for argv in (
            ["spawn", "sh", "--tag", "t", "--cmd", "bash"],
            ["spawn", "ot", "--tag", "t", "--cmd", "sleep 1000"],
            ["spawn-nvim", "nv", "--tag", "t"],
            ["spawn", "wk", "--tag", "t", "--cwd", str(self.git.path), "--engine", "claude", "--prompt", "hi"],
        ):
            result = self.tx(argv)
            self.assertEqual(result.code, 0, result.err)
        for name in ("sh", "ot", "nv"):
            self.assertEqual(self.show(name)["state"], "alive")
        self.assertEqual(self.show("wk")["state"], "idle")

    # ----- T-MODEL-05 ------------------------------------------------------------------------

    def test_t_model_05_engine_values(self):
        claude_id = self.records.llm(name="c", engine="claude")
        codex_id = self.records.llm(name="x", engine="codex")
        self.records.llm(id="h", name="h", engine="gemini")
        result = self.tx(["spawn", "w", "--tag", "t", "--engine", "gemini"])
        self.assertEqual(result.code, 2)
        self.assertTrue(result.err.endswith(
            "tx spawn: error: argument --engine: invalid choice: 'gemini' "
            "(choose from 'claude', 'codex', 'antigravity')\n"
        ), result.err)
        self.assertIn('"engine": "claude"', self.tx(["show", claude_id]).out)
        self.assertIn('"engine": "codex"', self.tx(["show", codex_id]).out)
        result = self.tx(["show", "nosuch"])
        self.assertEqual(result.code, 1)
        self.assertEqual(result.err, f"{SKIP} h.json: 'gemini' is not a valid Engine\n{NO_RECORD}")

    # ----- T-MODEL-06 ------------------------------------------------------------------------

    def test_t_model_06_location_round_trip_no_remote(self):
        session_id = self.records.other(
            name="ed", role="nvim", state="exited", ended_at=960.0,
            attached_to=({"pane_id": "%41", "remote": "h", "window_name": "work", "host": "Views",
                          "pane_index": "1", "window_index": "1"},),
        )
        self.records.other(id="l", name="l", role="nvim", attached_to=({"host": "x"},))
        record = self.show(session_id)
        self.assertEqual(len(record["attached_to"]), 1)
        self.assertEqual(list(record["attached_to"][0].keys()), LOCATION_KEYS)
        self.assertEqual(record["attached_to"][0], LOCATION)
        self.assertTrue(all(isinstance(value, str) for value in record["attached_to"][0].values()))
        result = self.tx(["show", "nosuch"])
        self.assertEqual(result.code, 1)
        self.assertEqual(result.err, f"{SKIP} l.json: 'window_index'\n{NO_RECORD}")

    def test_t_model_06_live_record_shows_live_join(self):
        session_id = self.records.other(name="ed", role="nvim", state="alive", attached_to=(dict(LOCATION),))
        self.assertEqual(self.show(session_id)["attached_to"], [])

    # ----- T-MODEL-07 ------------------------------------------------------------------------

    def test_t_model_07_origin_round_trip(self):
        now = time.time()
        session_id = "orig"
        chats = [
            self.records.chat_ref(session_id="s1", id="abcdef0123456789", role="fork", cwd="/r",
                                  how="fork", chat_id="c0c0c0c0-0000-4000-8000-000000000000",
                                  started_at=now - 900),
            self.records.chat_ref(session_id="z", id="fedcba9876543210", role="original", cwd="/r",
                                  how="spawn", chat_id=None, started_at=now - 900),
        ]
        self.records.llm(id=session_id, name="w", chats=chats)
        bad = self.records.chat_ref(session_id="m")
        del bad["origin"]["chat_id"]
        self.records.llm(id="m", name="m", chats=[bad])
        record = self.show(session_id)
        origins = [chat["origin"] for chat in record["chats"]]
        self.assertEqual([list(origin.keys()) for origin in origins], [["how", "session_id", "chat_id"]] * 2)
        self.assertEqual(origins[0], {"how": "fork", "session_id": "s1", "chat_id": "c0c0c0c0-0000-4000-8000-000000000000"})
        self.assertEqual(origins[1], {"how": "spawn", "session_id": "z", "chat_id": None})
        listing = self.tx(["chat", "ls", session_id])
        self.assertEqual(listing.code, 0, listing.err)
        # Only the origin cells are this case's Then (the row layout and ages are T-RENDER-10).
        self.assertEqual(listing.lines[0], "w — 2 chat(s)")
        rows = [line.split() for line in listing.lines[1:]]
        self.assertEqual([row[0] for row in rows], ["abcdef01", "fedcba98"])
        self.assertEqual([row[2] for row in rows], ["fork←c0c0c0c0", "spawn"])
        result = self.tx(["show", "nosuch"])
        self.assertEqual(result.code, 1)
        self.assertEqual(result.err, f"{SKIP} m.json: 'chat_id'\n{NO_RECORD}")

    # ----- T-MODEL-08 ------------------------------------------------------------------------

    def test_t_model_08_chatref_round_trip_engine_null(self):
        session_id = self.records.llm(name="w", chats=[dict(MODEL_08_CHAT), {**MODEL_08_CHAT, "engine": None}])
        self.records.llm(id="n", name="n", chats=[{**MODEL_08_CHAT, "engine": "x"}])
        no_summary = dict(MODEL_08_CHAT)
        del no_summary["summary"]
        self.records.llm(id="n2", name="n2", chats=[no_summary])
        record = self.show(session_id)
        self.assertEqual([list(chat.keys()) for chat in record["chats"]], [CHAT_KEYS] * 2)
        self.assertEqual(record["chats"][0], MODEL_08_CHAT)
        self.assertEqual(record["chats"][1]["engine"], None)
        result = self.tx(["show", "nosuch"])
        self.assertEqual(result.code, 1)
        self.assertEqual(result.err, (
            f"{SKIP} n.json: 'x' is not a valid Engine\n"
            f"{SKIP} n2.json: 'summary'\n{NO_RECORD}"
        ))

    # ----- T-MODEL-09 / 10 (golden) ----------------------------------------------------------

    def test_t_model_09_llm_record_exact_shape(self):
        self.model_09_file()
        result = self.tx(["show", MODEL_09_ID])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, json.dumps(MODEL_09_RECORD, indent=2) + "\n")
        self.assertEqual(list(json.loads(result.out).keys()), LLM_KEYS)
        for absent in ("artifact_id", "kind", "zzz_extra"):
            self.assertNotIn(absent, result.out)
        self.assert_golden("model/09", result.out)

    def test_t_model_10_other_record_exact_shape(self):
        self.model_10_file()
        result = self.tx(["show", MODEL_10_ID])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, json.dumps(MODEL_10_RECORD, indent=2) + "\n")
        self.assertEqual(list(json.loads(result.out).keys()), OTHER_KEYS)
        for absent in ("engine", "chats", "last_activity", "turn_started_at", "kind"):
            self.assertNotIn(f'"{absent}"', result.out)
        self.assert_golden("model/10", result.out)

    # ----- T-MODEL-11 / 12 -------------------------------------------------------------------

    def test_t_model_11_llm_records_load_as_llm_session(self):
        first = {**MODEL_09_RECORD, "chats": [dict(MODEL_08_CHAT)], "turn_started_at": 5.0,
                 "cmd": "claude --x", "env": {"A": "1"}}
        self.write_raw(MODEL_09_ID, first)
        for key in ("engine", "chats", "last_activity"):
            record = {**MODEL_09_RECORD, "id": f"miss_{key}"}
            del record[key]
            self.write_raw(f"miss_{key}", record)
        last = {**MODEL_09_RECORD, "id": "noturn"}
        del last["turn_started_at"]
        self.write_raw("noturn", last)

        shown = self.show(MODEL_09_ID)
        self.assertEqual(shown["role"], "llm")
        self.assertEqual(shown["engine"], "claude")
        self.assertEqual(shown["turn_started_at"], 5.0)
        self.assertEqual(shown["chats"][0]["origin"]["chat_id"], "c0")
        self.assertEqual(shown["cmd"], "claude --x")
        self.assertEqual(shown["env"], {"A": "1"})
        self.assertIn('"turn_started_at": null', self.tx(["show", "noturn"]).out)
        result = self.tx(["show", "nosuch"])
        self.assertEqual(result.code, 1)
        self.assertEqual(result.err, (
            f"{SKIP} miss_chats.json: 'chats'\n"
            f"{SKIP} miss_engine.json: 'engine'\n"
            f"{SKIP} miss_last_activity.json: 'last_activity'\n{NO_RECORD}"
        ))

    def test_t_model_12_non_llm_records_load_as_other_session(self):
        self.model_10_file()
        shell = {**MODEL_10_RECORD, "id": "sh", "name": "sh", "role": "shell", "engine": "claude",
                 "chats": [], "last_activity": 970.0}
        del shell["artifact_id"]
        del shell["group"]
        self.write_raw("sh", shell)
        self.write_raw("bogus", {**MODEL_10_RECORD, "id": "bogus", "role": "bogus"})

        nvim = self.show(MODEL_10_ID)
        self.assertEqual(nvim["role"], "nvim")
        self.assertEqual(nvim["artifact_id"], None)
        self.assertEqual(nvim["attached_to"][0]["pane_id"], "%41")
        shown = self.tx(["show", "sh"])
        self.assertEqual(shown.code, 0, shown.err)
        for absent in ("engine", "chats", "last_activity", "turn_started_at"):
            self.assertNotIn(f'"{absent}"', shown.out)
        self.assertIn('"artifact_id": null', shown.out)
        self.assertIn('"group": null', shown.out)
        chats = self.tx(["chat", "ls", "sh"])
        self.assertEqual(chats.code, 0, chats.err)
        self.assertEqual(chats.out, "sh — 0 chat(s)\n  (none)\n")
        result = self.tx(["show", "nosuch"])
        self.assertEqual(result.code, 1)
        self.assertEqual(result.err, f"{SKIP} bogus.json: 'bogus' is not a valid Role\n{NO_RECORD}")

    # ----- T-MODEL-13 ------------------------------------------------------------------------

    def model_13_files(self) -> None:
        self.records.llm(id="c", name="c")
        self.records.patch("c", schema_version=5)
        self.records.llm(id="i", name="i")
        self.records.patch("i", schema_version=...)
        self.records.llm(id="j", name="j")
        self.records.patch("j", schema_version="6")
        self.write_raw("k", {})

    MODEL_13_STDERR = (
        f"{SKIP} c.json: {UNSUPPORTED.format(version='5')}\n"
        f"{SKIP} i.json: {UNSUPPORTED.format(version='None')}\n"
        f"{SKIP} j.json: {UNSUPPORTED.format(version=repr('6'))}\n"
        f"{SKIP} k.json: {UNSUPPORTED.format(version='None')}\n"
        f"{NO_RECORD}"
    )

    def test_t_model_13_unsupported_schema_refused(self):
        self.model_13_files()
        result = self.tx(["show", "nosuch"])
        self.assertEqual(result.code, 1)
        self.assertEqual(result.out, "")
        self.assertEqual(result.err, self.MODEL_13_STDERR)
        self.assert_golden("model/13", result.err)
        by_id = self.tx(["show", "c"])
        self.assertEqual(by_id.code, 1)
        self.assertEqual(by_id.out, "")
        self.assertIn(UNSUPPORTED.format(version="5"), by_id.err)

    @expected_failure_on_python
    def test_t_model_13_fixed_ls_history_warn_once(self):
        self.model_13_files()
        for verb in ("ls", "history"):
            result = self.tx([verb])
            self.assertEqual(result.code, 0)
            skip_lines = [line for line in result.err.splitlines() if line.startswith(SKIP)]
            self.assertEqual(skip_lines, self.MODEL_13_STDERR.splitlines()[:4], verb)

    # ----- T-MODEL-14 ------------------------------------------------------------------------

    def test_t_model_14_strictness_on_current_records(self):
        keys = ["id", "name", "state", "cwd", "cmd", "tags", "env", "parent", "pid", "attached_to",
                "created_at", "ended_at", "role"]
        for key in keys:
            record = {**MODEL_09_RECORD, "id": f"miss_{key}"}
            del record[key]
            self.write_raw(f"miss_{key}", record)
        self.write_raw("s", {**MODEL_09_RECORD, "id": "s", "state": "running"})
        self.write_raw("l", {**MODEL_09_RECORD, "id": "l", "attached_to": [{"host": "x"}]})
        expected = sorted(
            [(f"miss_{key}.json", f"'{key}'") for key in keys]
            + [("l.json", "'window_index'"), ("s.json", "'running' is not a valid State")]
        )
        result = self.tx(["show", "nosuch"])
        self.assertEqual(result.code, 1)
        self.assertEqual(result.out, "")
        self.assertEqual(
            result.err,
            "".join(f"{SKIP} {name}: {reason}\n" for name, reason in expected) + NO_RECORD,
        )

    # ----- T-MODEL-15 ------------------------------------------------------------------------

    def test_t_model_15_load_save_identity(self):
        llm = {
            "schema_version": 6, "id": "ident-llm", "name": "worker", "role": "llm", "state": "exited",
            "cwd": "/repo", "cmd": "claude --x", "tags": ["a", "b"], "group": "g",
            "env": {"TX_READ_ONLY": "1"}, "parent": "p", "pid": 123,
            "attached_to": [dict(LOCATION), {**LOCATION, "window_index": "2", "pane_id": "%42"}],
            "created_at": 900.0, "ended_at": 1000.0, "engine": "claude", "last_activity": 970.0,
            "chats": [dict(MODEL_08_CHAT), {**MODEL_08_CHAT, "id": "c1", "engine": None}],
            "turn_started_at": 950.0,
        }
        nvim = {**MODEL_10_RECORD, "id": "ident-nvim", "artifact_id": "art"}
        for record in (llm, nvim):
            self.write_raw(record["id"], record)
            path = self.records.path(record["id"])
            written = path.read_bytes()
            result = self.tx(["kill", record["id"]])
            self.assertEqual(result.code, 0, result.err)
            self.assertEqual(result.out, f"Killed '{record['name']}'\n")
            self.assertEqual(path.read_bytes(), written)
            shown = self.tx(["show", record["id"]])
            self.assertEqual(shown.out, written.decode() + "\n")

    def test_t_model_15_tag_resave_clears_locations(self):
        self.write_raw("ident-nvim", {**MODEL_10_RECORD, "id": "ident-nvim"})
        result = self.tx(["tag", "ident-nvim", "x"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self.records.load("ident-nvim")["attached_to"], [])

    # ----- T-MODEL-16 ------------------------------------------------------------------------

    def test_t_model_16_tmux_name_is_id(self):
        result = self.tx(["spawn", "worker", "--tag", "t", "--cmd", "bash"])
        self.assertEqual(result.code, 0, result.err)
        record_id = self.show("worker")["id"]
        rows = self.tmux.run("list-sessions", "-F", "#{session_name} #{@tx_id}").stdout.splitlines()
        self.assertEqual(rows, [f"{record_id} {record_id}"])
        self.assertNotEqual(record_id, "worker")
        self.assertEqual(self.tx(["_tmux-name", "worker"]).out, f"{record_id}\n")
        renamed = self.tx(["rename", "worker", "worker2"])
        self.assertEqual(renamed.code, 0, renamed.err)
        self.assertEqual(self.tmux.sessions(), [record_id])
        self.assertEqual(self.tx(["_tmux-name", "worker2"]).out, f"{record_id}\n")

    # ----- T-MODEL-17 ------------------------------------------------------------------------

    def test_t_model_17_activity_at_fallback_chain(self):
        self.records.llm(id="a", name="a", state="exited", created_at=5.0, last_activity=None)
        self.records.llm(id="b", name="b", state="exited", last_activity=None)
        self.records.patch("b", created_at=None)
        self.records.llm(id="c", name="c", state="exited", created_at=5.0, last_activity=9.0)
        self.records.other(id="d", name="d", role="shell", state="exited", created_at=3.0)
        self.records.llm(id="e", name="e", state="exited", created_at=5.0, last_activity=0.0)
        result = self.tx(["history"])
        self.assertEqual(result.code, 0, result.err)
        rows = result.out.splitlines()[1:]
        self.assertEqual([row.split()[0] for row in rows], ["c", "a", "e", "d", "b"])
        self.assertEqual(rows[-1].split()[2], "-")

    # ----- T-MODEL-18 ------------------------------------------------------------------------

    def test_t_model_18_is_alive(self):
        for state in ("alive", "working", "waiting", "idle", "exited", "archived"):
            session_id = self.records.llm(id=state, name=state, state=state)
            if state in ("alive", "working", "waiting", "idle"):
                self.live(session_id)
        self.assertEqual(set(self.states_of(self.tx(["ls"]).out)), {"alive", "working", "waiting", "idle"})
        self.assertEqual(set(self.states_of(self.tx(["history"]).out)), {"exited", "archived"})
        reuse = self.tx(["spawn", "exited", "--tag", "t", "--cmd", "bash"])
        self.assertEqual(reuse.code, 0, reuse.err)
        self.assertTrue(reuse.out.startswith("Spawned 'exited' ("))
        clash = self.tx(["spawn", "idle", "--tag", "t", "--cmd", "bash"])
        self.assertEqual(clash.code, 1)
        self.assertEqual(clash.err, "tx spawn: session 'idle' already exists\n")

    # ----- T-MODEL-21 ------------------------------------------------------------------------

    def test_t_model_21_terminal_states_absorbing(self):
        self.records.llm(id="x1", name="x1", state="exited", ended_at=1000.0, chats=[])
        self.records.llm(id="x2", name="x2", state="archived", ended_at=1000.0, chats=[])
        result = self.tx(["archive", "x1"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, "Archived 'x1'\n")
        self.assertEqual(self.tx(["kill", "x1"]).out, "Killed 'x1'\n")
        x1 = self.records.load("x1")
        self.assertEqual((x1["state"], x1["ended_at"]), ("exited", 1000.0))
        self.assertEqual(self.tx(["kill", "x2"]).out, "Killed 'x2'\n")
        self.assertEqual(self.records.load("x2")["state"], "archived")

        self.assertEqual(self.tx(["spawn", "live", "--tag", "t", "--cmd", "bash"]).code, 0)
        self.assertEqual(self.show("live")["state"], "alive")
        before = time.time()
        self.assertEqual(self.tx(["kill", "live"]).out, "Killed 'live'\n")
        live = self.show("live")
        self.assertEqual(live["state"], "exited")
        self.assertGreaterEqual(live["ended_at"], before)
        self.assertEqual(self.tx(["archive", "live"]).out, "Archived 'live'\n")
        self.assertEqual(self.show("live")["state"], "exited")

        log = self.log_lines()
        self.assertEqual([(line["type"], line["msg"]) for line in log if line["type"] != "spawn"], [
            ("archive", "x1"), ("kill", "x1"), ("kill", "x2"), ("kill", "live"), ("archive", "live"),
        ])
        self.assertEqual([line["type"] for line in log][3], "spawn")

    # ----- T-MODEL-23 ------------------------------------------------------------------------

    def test_t_model_23_non_llm_records_have_no_chats(self):
        self.records.other(id="ed", name="ed", role="nvim", state="exited", ended_at=960.0)
        stray = {**MODEL_10_RECORD, "id": "stray", "name": "stray", "chats": [dict(MODEL_08_CHAT)]}
        self.write_raw("stray", stray)
        result = self.tx(["chat", "ls", "ed"])
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, "ed — 0 chat(s)\n  (none)\n")
        history = self.tx(["history"])
        rows = history.out.splitlines()[1:]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(" 0c " in row for row in rows), history.out)
        stray_show = self.tx(["show", "stray"])
        self.assertEqual(stray_show.code, 0, stray_show.err)
        self.assertNotIn('"chats"', stray_show.out)

    # ----- T-MODEL-25 ------------------------------------------------------------------------

    def test_t_model_25_schema_version_constant(self):
        self.assertEqual(self.tx(["spawn", "s", "--tag", "t", "--cmd", "bash"]).code, 0)
        self.assertEqual(self.records.load(self.show("s")["id"])["schema_version"], 6)
        selfcheck = self.tx(["selfcheck"])
        self.assertEqual(selfcheck.code, 0, selfcheck.err)
        self.assertIn("S1a self-check PASSED ✓\n", selfcheck.out)
        self.assertTrue(any("schema v6" in line for line in selfcheck.lines), selfcheck.out)
        migrate = self.tx(["migrate"])
        self.assertEqual(migrate.code, 0, migrate.err)
        self.assertTrue(any("to v6" in line and line.startswith("migrated") for line in migrate.lines), migrate.out)
