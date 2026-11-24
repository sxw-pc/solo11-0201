import json
import sqlite3
from contextlib import closing

from ear_tag_transfer import identity, ledger, report, yard
from ear_tag_transfer.rules import RuleError
from auction_support import AuctionCase, FIXTURES, LATE, RESOLUTIONS, SHIFT, choice, close, event, offer, review, scan, seal


class SaleReplayTests(AuctionCase):
    def test_scale_ticket_rejects_nonpositive_duplicate_deck_receipt_and_future_ticket(self):
        self.prefix("load-2")
        self.assert_refused("nonpositive_net", event("scale-ticket", "bad-net", 0, load="DECK-2", receipt="new", gross_g=1, tare_g=1))
        self.assert_refused("receipt_already_captured", event("scale-ticket", "used-receipt", 0, load="DECK-2",
                            receipt="B2-801", gross_g=500000, tare_g=80000))
        self.apply(event("scale-ticket", "future-print", 3, load="DECK-2", receipt="new", gross_g=500000, tare_g=80000), desk="weigh")
        select = event("choose-ticket", "future-select", 2, load="DECK-2", ticket="future-print", considered=["future-print"],
                       expected=None, reason="Check date")
        self.assert_refused("future_scale_ticket", select)
        self.apply({**select, "at": "2026-11-24T10:04:00Z"}, desk="weigh")
        self.assertEqual(self.load("DECK-2")["chosen"]["net_g"], 420000)


    def test_scale_choice_requires_complete_current_alternatives(self):
        self.prefix("ticket-1-retare")
        self.apply(LATE[1], desk="weigh")
        self.assertFalse(self.load()["usable"])
        self.assert_refused("unacknowledged_tickets", choice("partial", 0, "ticket-1-retare", ["ticket-1-retare"]))
        self.assert_refused("stale_scale_choice", choice("stale", 0, "ticket-1-retare", ["ticket-1-retare", "late-deck-print"], None))
        self.apply(choice("explicit-choice", 0, "ticket-1-retare", ["ticket-1-retare", "late-deck-print"]))
        self.assertTrue(self.load()["usable"])
        self.assertEqual(self.load()["chosen"]["ticket"], "ticket-1-retare")


    def test_lot_reservation_is_exclusive_and_cancel_releases_it(self):
        self.prefix("offer-17")
        self.assert_refused("reserved_elsewhere", offer("second-lot", 0))
        self.assert_refused("seller_title_mismatch", event("cancel-lot", "wrong-cancel", 0, lot="LOT-17", seller="Maple", reason="Remove"))
        self.apply(event("cancel-lot", "void-17", 0, lot="LOT-17", seller="Cedar", reason="Consignor withdrew before close"), desk="sale")
        self.apply(offer("second-lot", 1), desk="sale")
        state = {row["id"]: row["state"] for row in report.snapshot(self.db)["lots"]}
        self.assertEqual(state, {"LOT-17": "cancelled", "second-lot": "pending"})
        self.assert_refused("lot_final", close("cannot-close-withdrawn", 2))


    def test_exact_membership_rejects_same_head_count_and_wrong_pen(self):
        self.prefix("r-out-2")
        self.apply(scan("out-substitute", 0, "RF-103", "P-WAIT", "P-SOUTH"),
                   review("r-out-substitute", 1, "out-substitute", mark="CHALK-3"),
                   scan("out-wrong-pen", 2, "RF-102", "P-STAGE", "P-NORTH"),
                   review("r-out-wrong-pen", 3, "out-wrong-pen", mark="CHALK-2"))
        self.assert_refused("gate_count_mismatch", seal("missing", 4, expected=None, scans=("out-1",)))
        self.assert_refused("gate_membership_mismatch.*A03", seal("wrong-animal", 4, expected=None, scans=("out-1", "out-substitute")))
        self.assert_refused("route_mismatch", seal("wrong-route", 4, expected=None, scans=("out-1", "out-wrong-pen")))
        self.apply(seal("good", 4, expected=None))
        self.assertEqual(self.lot()["tally"]["count"], 2)


    def test_exact_scale_partition_rejects_cross_pen_and_overlap(self):
        self.prefix("r-out-2")
        self.apply(event("open-load", "mix", 0, load="MIX", deck="Bridge 3", animals=["A01", "A02"]),
                   event("open-load", "repeat-a1", 1, load="REPEAT", deck="Bridge 3", animals=["A01"]))
        self.assert_refused("scale_crosses_destinations", seal("mixed", 2, expected=None, loads=("MIX",)))
        self.assert_refused("scale_membership_mismatch", seal("doubled", 2, expected=None, loads=("DECK-1", "DECK-2", "REPEAT")))
        self.assert_refused("scale_roster_incomplete", seal("short-scale", 2, expected=None, loads=("DECK-1",)))
        self.apply(seal("exact", 2, expected=None))
        self.assertEqual(self.lot()["tally"]["net_g_by_pen"], {"P-NORTH": 405250, "P-SOUTH": 358750})


    def test_weight_after_crossbar_needs_new_crossbar_evidence(self):
        self.prefix("tally-17")
        self.apply(event("scale-ticket", "new-print", 0, load="DECK-1", receipt="B2-new", gross_g=474000, tare_g=68000),
                   choice("new-print-choice", 1, "new-print", ["new-print"]))
        self.assert_refused("weight_outside_transfer_window", seal("bad-window", 2))
        self.apply(scan("new-crossing", 2, "RF-201", "P-STAGE", "P-NORTH"),
                   review("new-crossing-review", 3, "new-crossing", mark="CHALK-1"),
                   review("discard-old-crossing", 4, "out-1", expected="r-out-1"),
                   seal("window-restored", 5, scans=("new-crossing", "out-2")))
        self.assertTrue(self.lot()["release"]["ready"])


    def test_late_weight_holds_then_reseal_changes_handover_mass(self):
        self.prefix("tally-17")
        self.assertEqual(self.lot()["release"], {"ready": True, "reason": None})
        self.assertEqual(self.lot()["tally"]["net_g"], 764000)
        self.prefix("ticket-1-retare")
        self.assertIn("load_weight_unresolved", self.lot()["release"]["reason"])
        self.assert_refused("load_weight_unresolved", close("wait-for-choice", 0))
        self.prefix("choose-1-retare")
        self.assert_refused("tally_evidence_changed", close("must-reseal", 0))
        self.prefix("clear-stage")
        self.assertEqual((self.lot()["state"], self.lot()["tally"]["net_g"]), ("closed", 762750))
        self.assertEqual(ledger.replay(self.db)["differences"], [])


    def test_two_valid_ticket_choices_on_same_receipts_change_delivered_weight(self):
        self.prefix("ticket-1-retare")
        weighed = {}
        for use in ("ticket-1", "ticket-1-retare"):
            with closing(ledger.connect(":memory:")) as branch:
                self.db.backup(branch)
                ledger.apply(branch, [choice("adjudication", 0, use, ["ticket-1-retare"]),
                                      seal("resealed", 1), close("finished", 2, "resealed")])
                lot = report.snapshot(branch)["lots"][0]
                self.assertEqual([row["keeper"] for row in lot["handover"]], ["Juniper Yard", "Willow Yard"])
                weighed[use] = lot["tally"]["net_g"]
        self.assertEqual(weighed, {"ticket-1": 764000, "ticket-1-retare": 762750})


    def test_same_input_supports_original_crossbar_or_reweigh_and_recross(self):
        self.prefix("tally-17")
        self.apply(event("scale-ticket", "after-gate-print", 0, load="DECK-1", receipt="B2-new",
                         gross_g=474000, tare_g=68000),
                   scan("recross", 1, "RF-201", "P-STAGE", "P-NORTH"),
                   review("r-recross", 2, "recross", mark="CHALK-1"))
        results = []
        for ticket, used_gate, ignored_gate, ignored_review in (
            ("ticket-1", "out-1", "recross", "r-recross"),
            ("after-gate-print", "recross", "out-1", "r-out-1"),
        ):
            with closing(ledger.connect(":memory:")) as branch:
                self.db.backup(branch)
                ledger.apply(branch, [choice("adjudicate", 3, ticket, ["after-gate-print"])])
                if used_gate == "recross":
                    with self.assertRaisesRegex(RuleError, "weight_outside_transfer_window"):
                        ledger.apply(branch, [seal("naive-retally", 4)])
                ledger.apply(branch, [review("reconcile-unused-crossing", 4, ignored_gate, expected=ignored_review),
                                      seal("new-tally", 5, scans=(used_gate, "out-2")),
                                      event("drain-pen", "drain", 6, pen="P-STAGE", keeper="Nora"),
                                      close("closed", 7, "new-tally"),
                                      event("clear-pen", "clear", 8, pen="P-STAGE", keeper="Nora", walked_by="Ira")])
                state = report.snapshot(branch)
                lot = state["lots"][0]
                stage = next(pen for pen in state["pens"] if pen["id"] == "P-STAGE")
                self.assertEqual(stage["state"], "cleared")
                self.assertEqual(ledger.replay(branch)["differences"], [])
                results.append((lot["tally"]["net_g"], lot["handover"][0]["scan"]))
        self.assertEqual(results, [(764000, "out-1"), (764750, "recross")])

    def test_handover_changes_title_and_custody_once(self):
        self.prefix("close-17")
        lot = self.lot()
        self.assertEqual([(row["animal"], row["buyer"], row["keeper"], row["destination"]) for row in lot["handover"]],
                         [("A01", "Maple", "Juniper Yard", "P-NORTH"), ("A02", "Maple", "Willow Yard", "P-SOUTH")])
        self.assertEqual(identity.animal(self.db, "A01")["owner_version"], "close-17")
        self.assertEqual(identity.animal(self.db, "A03")["owner"], "Cedar")
        self.assert_refused("already_sold", offer("resale", 0, seller="Maple", buyer="Cedar"))
        self.assert_refused("lot_final", close("double-close", 1, "tally-17-retare"))
        self.assertEqual(self.prefix("close-17")["applied"], [])


    def test_identity_reversal_blocks_seal_and_retally_uses_new_review(self):
        self.prefix("tally-17")
        self.apply(review("remove-south-proof", 0, "out-2", expected="r-out-2"), desk="identity")
        self.assert_refused("gate_not_countable", close("cannot-close", 1))
        self.apply(review("restore-south-proof", 2, "out-2", expected="remove-south-proof", mark="CHALK-2"), desk="identity")
        self.assert_refused("tally_evidence_changed", close("old-proof", 3))
        self.apply(seal("updated-review", 4), close("same-title-new-proof", 5, "updated-review"))
        self.assertEqual(self.lot()["tally"]["members"][1]["review"], "restore-south-proof")


    def test_late_identity_disputes_closed_handover_and_clearance_without_rewriting_them(self):
        self.prefix()
        self.apply(review("post-sale-exclusion", 0, "out-1", expected="r-out-1"))
        self.assertEqual(self.pen()["state"], "clear_disputed")
        self.assertEqual(self.lot()["exceptions"][0]["code"], "handover_identity_disputed")
        self.assertEqual(identity.animal(self.db, "A01")["owner"], "Maple")
        self.assertEqual(yard.position(self.db, "A01")["pen"], "P-NORTH")
        self.apply(review("post-sale-confirm", 1, "out-1", expected="post-sale-exclusion", mark="CHALK-1"))
        self.assertEqual(self.pen()["state"], "cleared")
        self.assertEqual(self.lot()["exceptions"], [])


    def test_post_close_raw_receipts_dispute_then_reconcile_frozen_outputs(self):
        self.prefix()
        ledger.apply(self.db, LATE)
        state = report.snapshot(self.db, include_evidence=True)
        self.assertEqual(self.pen()["state"], "clear_disputed")
        self.assertEqual(self.pen()["clear_event"], "clear-stage")
        self.assertEqual(self.lot()["exceptions"][0]["code"], "handover_weight_disputed")
        self.assertEqual(self.lot()["tally"]["net_g"], 762750)
        self.assertEqual(state["journal"][-2]["event"]["id"], "late-stage-echo")
        self.assertEqual(ledger.replay(self.db)["differences"], [])
        ledger.apply(self.db, RESOLUTIONS)
        self.assertEqual(self.pen()["state"], "cleared")
        self.assertEqual(self.lot()["exceptions"], [])
        self.assertEqual(self.lot()["tally"]["net_g"], 762750)


    def test_atomic_failure_keeps_lot_and_original_titles_across_retry(self):
        self.prefix("tally-17")
        # Inject a write failure after A01's custody, title and handover changes were staged.
        self.db.execute("""CREATE TEMP TRIGGER break_second BEFORE INSERT ON handover WHEN NEW.animal='A02'
                           BEGIN SELECT RAISE(ABORT, 'fixture second handover failure'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "second handover failure"):
            self.apply(close("atomic-close", 0))
        self.assertEqual(self.lot()["state"], "pending")
        self.assertEqual(self.pen()["occupants"], ["A01", "A02"])
        self.assertEqual(identity.animal(self.db, "A01")["owner"], "Cedar")
        self.assertEqual(ledger.replay(self.db)["events"], 33)
        self.db.execute("DROP TRIGGER break_second")
        self.apply(close("atomic-close", 0))
        self.assertEqual(len(self.lot()["handover"]), 2)
        self.assertEqual(ledger.replay(self.db)["differences"], [])


    def test_bad_batch_and_id_conflict_leave_journal_unchanged(self):
        self.prefix("retag-1")
        self.assert_refused("event_id_conflict", scan("temporary", 0, "RF-201", "P-STAGE", "P-NORTH"),
                            {**SHIFT[8], "owner": "Different"})
        self.assert_refused("wrong_desk", SHIFT[21], desk="yard")
        self.assert_refused("business_clock", {**offer("backdated", 1), "at": "2026-11-24T09:00:00Z"})


    def test_repair_rebuilds_all_domain_rows_without_changing_raw(self):
        self.prefix()
        ledger.apply(self.db, LATE)
        frozen = report.snapshot(self.db, include_evidence=True)
        self.db.execute("UPDATE tally SET net_g = net_g + 1 WHERE id = 'tally-17-retare'")
        self.db.execute("UPDATE animal SET owner = 'projection-error' WHERE id='A01'")
        self.assertEqual(ledger.replay(self.db)["differences"], ["animal", "tally"])
        self.assertTrue(ledger.replay(self.db, repair=True)["repaired"])
        self.assertEqual(report.snapshot(self.db, include_evidence=True), frozen)
        self.assertEqual(ledger.replay(self.db)["differences"], [])


    def test_cli_routes_each_desk_and_rejects_real_fixture_errors(self):
        cases = [
            ("identity", "reg-1", "r-out-2", "tag_mark_disagree"),
            ("sale", "offer-17", "r-out-2", "gate_membership_mismatch"),
            ("weigh", "load-1", "r-out-2", "receipt_already_captured"),
            ("yard", "open-P-STAGE", "drain-stage", "pen_occupied"),
        ]
        self.assertEqual(json.loads(self.cli("init").stdout), {"schema": 1})
        # Route each desk's first real operation through its own CLI on an isolated prefix.
        for desk, first_id, last_id, code in cases:
            with self.subTest(desk=desk):
                index = next(i for i, row in enumerate(SHIFT) if row["id"] == first_id)
                if index:
                    ledger.apply(self.db, SHIFT[:index])
                single = self.path.with_name(f"{desk}-normal.json")
                single.write_text(json.dumps([SHIFT[index]]), encoding="utf-8")
                accepted = self.cli(desk, "--input", single)
                self.assertEqual(accepted.returncode, 0, accepted.stderr)
                self.assertIn(first_id, json.loads(accepted.stdout)["applied"])
                self.prefix(last_id)
                refused = self.cli(desk, "--input", FIXTURES / f"reject_{desk}.json")
                self.assertEqual(refused.returncode, 2, refused.stdout)
                self.assertIn(code, refused.stderr)
                # Reset the isolated prefix before testing the adjacent desk.
                with closing(ledger.connect(":memory:")) as empty:
                    ledger.initialize(empty)
                    empty.backup(self.db)


    def test_cli_replay_exit_status_and_repair(self):
        self.prefix()
        self.db.execute("UPDATE animal SET owner = 'stale' WHERE id='A01'")
        mismatch = self.cli("replay")
        self.assertEqual(mismatch.returncode, 1)
        self.assertEqual(json.loads(mismatch.stdout)["differences"], ["animal"])
        repaired = self.cli("replay", "--repair")
        self.assertEqual(repaired.returncode, 0, repaired.stderr)
        self.assertTrue(json.loads(repaired.stdout)["repaired"])
        self.assertEqual(self.cli("report", "--evidence").returncode, 0)


    def test_corrupt_raw_chain_refuses_repair(self):
        self.prefix()
        self.db.execute("UPDATE journal SET digest = ? WHERE seq = 8", ("0" * 64,))
        with self.assertRaisesRegex(RuleError, "journal_integrity: receipt 8"):
            ledger.replay(self.db, repair=True)
        self.assertEqual(self.lot()["state"], "closed")
        self.assertEqual(ledger.one(self.db, "SELECT digest FROM journal WHERE seq = 8")[0], "0" * 64)

