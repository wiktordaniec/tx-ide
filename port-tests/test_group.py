"""GROUP — grouping.py + `tx group` / `tx artifact group` (spec section 04, T-GROUP-01..12).

Effort groups are derived on read: session = own override → first override up the parent chain
(stopping at hubs / cycles) → bound artifact's group → tags[0] → name; artifact = own → creator's
resolved group → newest surviving toucher → "ungrouped". Everything is observed through `tx group
NAME` / `tx artifact group ID` stdout on crafted records; no tmux session is needed.
"""

from __future__ import annotations

import json

from txkit import TxCase

def read_output(own: str | None, resolved: str) -> str:
    return f"own:      {own if own is not None else '—'}\nresolved: {resolved}\n"


def artifact_record(case: TxCase, artifact_id: str) -> dict:
    return json.loads(case.records.artifact_record_path(artifact_id).read_text())


def touch(session_id: str, rev: int, at: float = 1000.0) -> dict:
    return {"session_id": session_id, "at": at, "rev": rev, "changes": None}


class TestGroup(TxCase):
    def fixture_g(self, **overrides) -> None:
        """Fixture G: A(a1 assist) ← B(b1 worker, tags feat-x,extra) ← C(c1 child); hub H(h1)."""
        fields = {
            "a1": dict(name="assist", tags=(), group=None, parent=None, created_at=100.0),
            "b1": dict(name="worker", tags=("feat-x", "extra"), group=None, parent="a1", created_at=200.0),
            "c1": dict(name="child", tags=(), group=None, parent="b1", created_at=300.0),
            "h1": dict(name="tx-assistant", tags=(), group="hubgrp", parent=None, created_at=50.0),
        }
        for session_id, values in overrides.items():
            fields[session_id].update(values)
        for session_id, values in fields.items():
            self.records.llm(id=session_id, state="idle", chats=[], **values)

    def assert_group(self, argv: list[str], expected: str, env: dict | None = None):
        result = self.tx(argv, env=env)
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out, expected)
        self.assertEqual(result.err, "")
        return result

    # ----- T-GROUP-01 ---------------------------------------------------------------------

    def test_t_group_01_own_override_wins(self):
        self.fixture_g(c1={"group": "mine"})
        self.assert_group(["group", "child"], read_output("mine", "mine"))

    # ----- T-GROUP-02 ---------------------------------------------------------------------

    def test_t_group_02_tags_then_name_floor(self):
        self.fixture_g()
        self.assert_group(["group", "worker"], read_output(None, "feat-x"))
        self.assert_group(["group", "assist"], read_output(None, "assist"))

    # ----- T-GROUP-03 ---------------------------------------------------------------------

    def test_t_group_03_parent_chain_override_inherits_tags_do_not(self):
        self.fixture_g(a1={"group": "agrp"})
        self.assert_group(["group", "child"], read_output(None, "agrp"))

    def test_t_group_03_edge_no_override_up_the_chain_falls_to_own_name(self):
        self.fixture_g()
        self.assert_group(["group", "child"], read_output(None, "child"))

    # ----- T-GROUP-04 ---------------------------------------------------------------------

    def hub_topology(self, hub_group: str | None) -> None:
        self.fixture_g(a1={"parent": "h1"}, h1={"group": hub_group, "parent": "x1"})
        self.records.llm(id="x1", name="up", tags=(), group="upgrp", parent=None, created_at=10.0, chats=[])

    def test_t_group_04_hub_stops_the_walk(self):
        self.hub_topology("hubgrp")
        self.assert_group(["group", "child"], read_output(None, "hubgrp"))

    def test_t_group_04_hub_without_override_never_reaches_beyond(self):
        self.hub_topology(None)
        self.assert_group(["group", "child"], read_output(None, "child"))

    def test_t_group_04_edge_hub_itself_does_not_walk_its_parent(self):
        self.hub_topology("hubgrp")
        self.assert_group(["group", "tx-assistant"], read_output("hubgrp", "hubgrp"))
        self.hub_topology(None)
        self.assert_group(["group", "tx-assistant"], read_output(None, "tx-assistant"))

    # ----- T-GROUP-05 ---------------------------------------------------------------------

    def test_t_group_05_parent_cycle_terminates(self):
        self.records.llm(id="p1", name="P", tags=(), group=None, parent="q1", chats=[])
        self.records.llm(id="q1", name="Q", tags=(), group=None, parent="p1", chats=[])
        self.assert_group(["group", "P"], read_output(None, "P"))

    # ----- T-GROUP-06 ---------------------------------------------------------------------

    def dup_parents(self) -> None:
        self.records.llm(id="n1", name="dup", tags=(), group="old", created_at=100.0, state="exited", ended_at=150.0, chats=[])
        self.records.llm(id="n2", name="dup", tags=(), group="new", created_at=500.0, state="idle", chats=[])

    def test_t_group_06_parent_by_era_scoped_name(self):
        self.dup_parents()
        self.records.llm(id="k1", name="K", tags=(), group=None, parent="dup", created_at=300.0, chats=[])
        self.assert_group(["group", "K"], read_output(None, "old"))

    def test_t_group_06_edge_later_referrer_ranks_alive_then_newest(self):
        self.dup_parents()
        self.records.llm(id="k2", name="K2", tags=(), group=None, parent="dup", created_at=600.0, chats=[])
        self.assert_group(["group", "K2"], read_output(None, "new"))

    def test_t_group_06_edge_unresolvable_parent_ends_the_chain(self):
        self.dup_parents()
        self.records.llm(id="k3", name="K3", tags=(), group=None, parent="nothing", created_at=600.0, chats=[])
        self.assert_group(["group", "K3"], read_output(None, "K3"))

    # ----- T-GROUP-07 ---------------------------------------------------------------------

    def nvim_view(self, artifact_group: str | None) -> None:
        self.fixture_g()
        self.records.other(id="v1", name="view", role="nvim", tags=("artifact",), group=None, parent=None, artifact_id="art1")
        self.records.artifact(id="art1", group=artifact_group, history=[touch("b1", 0)])

    def test_t_group_07_bound_artifact_group_for_an_nvim_view(self):
        self.nvim_view("artgrp")
        self.assert_group(["group", "view"], read_output(None, "artgrp"))

    def test_t_group_07_edge_creator_full_resolution_and_llm_ignores_artifacts(self):
        self.nvim_view(None)
        self.assert_group(["group", "view"], read_output(None, "feat-x"))
        self.assert_group(["group", "worker"], read_output(None, "feat-x"))

    # ----- T-GROUP-08 ---------------------------------------------------------------------

    def test_t_group_08_artifact_group_cascade(self):
        self.fixture_g()
        self.records.artifact(id="art2", group=None, history=[touch("user", 0), touch("c1", 1), touch("b1", 2)])
        self.assert_group(["artifact", "group", "art2"], read_output(None, "feat-x"))

    def test_t_group_08_edge_no_surviving_toucher_is_ungrouped(self):
        self.fixture_g()
        self.records.artifact(id="art2", group=None, history=[touch("user", 0), touch("gone", 1), touch("user", 2)])
        self.assert_group(["artifact", "group", "art2"], read_output(None, "ungrouped"))

    def test_t_group_08_edge_creator_wins_over_later_toucher(self):
        self.fixture_g()
        self.records.artifact(id="art2", group=None, history=[touch("c1", 0), touch("b1", 1)])
        self.assert_group(["artifact", "group", "art2"], read_output(None, "child"))

    def test_t_group_08_edge_own_group(self):
        self.fixture_g()
        self.records.artifact(id="art2", group="g", history=[touch("user", 0), touch("b1", 1)])
        self.assert_group(["artifact", "group", "art2"], read_output("g", "g"))

    # ----- T-GROUP-09 ---------------------------------------------------------------------

    def test_t_group_09_session_artifact_recursion_guard(self):
        self.records.other(id="v1", name="view", role="nvim", tags=(), group=None, parent=None, artifact_id="art3")
        self.records.artifact(id="art3", group=None, history=[touch("v1", 0)])
        self.assert_group(["group", "view"], read_output(None, "view"))
        self.assert_group(["artifact", "group", "art3"], read_output(None, "view"))

    # ----- T-GROUP-10 ---------------------------------------------------------------------

    def test_t_group_10_read_output(self):
        self.fixture_g()
        self.assert_group(["group", "worker"], read_output(None, "feat-x"))
        self.fixture_g(b1={"group": "g"})
        self.assert_group(["group", "worker"], read_output("g", "g"))

    def test_t_group_10_edge_unknown_name(self):
        self.fixture_g()
        result = self.tx(["group", "zzz"])
        self.assertEqual((result.code, result.out, result.err), (1, "", "tx group: session 'zzz' not found\n"))

    # ----- T-GROUP-11 ---------------------------------------------------------------------

    def test_t_group_11_set_and_clear(self):
        self.fixture_g()
        result = self.tx(["group", "worker", "g2"])
        self.assertEqual((result.code, result.out, result.err), (0, "Grouped 'worker' (group=g2)\n", ""))
        self.assertEqual(self.records.load("b1")["group"], "g2")
        tail = self.log_tail()[0]
        self.assertEqual((tail["type"], tail["msg"]), ("group", "worker g2"))
        self.assert_group(["group", "worker"], read_output("g2", "g2"))

        result = self.tx(["group", "worker", "--clear"])
        self.assertEqual(
            (result.code, result.out, result.err),
            (0, "Cleared group override on 'worker' (back to derived)\n", ""),
        )
        self.assertIsNone(self.records.load("b1")["group"])
        tail = self.log_tail()[0]
        self.assertEqual((tail["type"], tail["msg"]), ("group", "worker (cleared)"))
        self.assertEqual(len(self.log_lines()), 2)
        self.assert_group(["group", "worker"], read_output(None, "feat-x"))

    def test_t_group_11_edge_validation(self):
        self.fixture_g()
        result = self.tx(["group", "worker", "g", "--clear"])
        self.assertEqual(result.code, 2)
        self.assertTrue(result.err.startswith("usage: tx group"), result.err)
        self.assertTrue(result.err.endswith("tx group: error: give a group or --clear, not both\n"), result.err)

        result = self.tx(["group", "worker", ""])
        self.assertEqual(result.code, 2)
        self.assertTrue(
            result.err.endswith("tx group: error: a group cannot be empty — use --clear to drop the override\n"),
            result.err,
        )

        result = self.tx(["group", "zzz", "g"])
        self.assertEqual(
            (result.code, result.out, result.err),
            (1, "", "tx group: session 'zzz' not found (no live @tx_id, no store record)\n"),
        )
        self.assertIsNone(self.records.load("b1")["group"])
        self.assertEqual(self.log_lines(), [])

    # ----- T-GROUP-12 ---------------------------------------------------------------------

    def test_t_group_12_artifact_group_read_set_clear(self):
        self.fixture_g()
        history = [touch("user", 0), touch("c1", 1), touch("b1", 2)]
        artifact_id = "aaaaaaaa-0000-4000-8000-000000000002"
        self.records.artifact(id=artifact_id, group=None, history=history)
        actor = {"TX_SESSION_ID": "s1"}

        self.assert_group(["artifact", "group", artifact_id], read_output(None, "feat-x"), env=actor)

        result = self.tx(["artifact", "group", artifact_id, "g9"], env=actor)
        self.assertEqual((result.code, result.out, result.err), (0, f"Grouped artifact {artifact_id} (group=g9)\n", ""))
        tail = self.log_tail()[0]
        self.assertEqual((tail["type"], tail["msg"], tail["actor"]), ("artifact-group", f"{artifact_id} g9", "s1"))
        record = artifact_record(self, artifact_id)
        self.assertEqual(record["group"], "g9")
        self.assertEqual(record["history"], history)
        self.assert_group(["artifact", "group", artifact_id], read_output("g9", "g9"), env=actor)

        result = self.tx(["artifact", "group", artifact_id, "--clear"], env=actor)
        self.assertEqual(
            (result.code, result.out, result.err),
            (0, f"Cleared group override on artifact {artifact_id} (back to derived)\n", ""),
        )
        tail = self.log_tail()[0]
        self.assertEqual((tail["type"], tail["msg"], tail["actor"]), ("artifact-group", f"{artifact_id} (cleared)", "s1"))
        record = artifact_record(self, artifact_id)
        self.assertIsNone(record["group"])
        self.assertEqual(record["history"], history)
        self.assertEqual(len(self.log_lines()), 2)

    def test_t_group_12_edge_prefix_and_argparse(self):
        self.fixture_g()
        artifact_id = "aaaaaaaa-0000-4000-8000-000000000002"
        self.records.artifact(id=artifact_id, group=None, history=[touch("b1", 0)])
        result = self.tx(["artifact", "group", "aaaa", "g9"])
        self.assertEqual((result.code, result.out), (0, f"Grouped artifact {artifact_id} (group=g9)\n"))
        self.assert_group(["artifact", "group", "aaaa"], read_output("g9", "g9"))

        result = self.tx(["artifact", "group", "aaaa", ""])
        self.assertEqual(result.code, 2)
        self.assertTrue(result.err.startswith("usage: tx artifact group"), result.err)
        self.assertTrue(
            result.err.endswith("tx artifact group: error: a group cannot be empty — use --clear to drop the override\n"),
            result.err,
        )
        result = self.tx(["artifact", "group", "aaaa", "g", "--clear"])
        self.assertEqual(result.code, 2)
        self.assertTrue(result.err.endswith("tx artifact group: error: give a group or --clear, not both\n"), result.err)
