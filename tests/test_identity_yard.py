from contextlib import closing

from ear_tag_transfer import identity, ledger, report, yard
from auction_support import AuctionCase, LATE, SHIFT, close, event, review, scan, seal


class IdentityYardTests(AuctionCase):
    def test_identity_retag_with_late_historical_scan(self):
        self.prefix("retag-1")
        historical = {**scan("late-old", 1, "RF-101", "OUT", "P-STAGE"), "at": "2026-11-24T09:10:15Z"}
        self.apply(historical, desk="yard")
        self.apply(review("historical-review", 2, "late-old", mark="CHALK-1"), desk="identity")
        self.assertEqual(identity.latest_review(self.db, "late-old")["animal"], "A01")
        self.assertEqual(identity.candidate(self.db, "RF-201", "2026-11-24T09:20:00Z"), "A01")
        self.assertIsNone(identity.candidate(self.db, "RF-101", "2026-11-24T09:20:00Z"))


    def test_identity_rejects_recycled_and_out_of_window_tags(self):
        self.prefix("retag-1")
        self.assert_refused("tag_already_issued", event("register-animal", "another", 0,
                            animal="A04", tag="RF-101", mark="CHALK-4", owner="Cedar"), desk="identity")
        self.apply(scan("retired-echo", 1, "RF-101", "P-STAGE", "P-NORTH"))
        self.assert_refused("tag_outside_issue_window", review("bad-old-tag", 2, "retired-echo", mark="CHALK-1"))


    def test_handheld_confirms_unknown_and_rejects_conflicting_or_stale_review(self):
        self.prefix("retag-1")
        self.apply(scan("unregistered-reader-tag", 0, "UNREAD", "P-STAGE", "P-NORTH"),
                   scan("opposing-reader-tag", 1, "RF-102", "P-STAGE", "P-NORTH"))
        self.apply(review("manual", 2, "unregistered-reader-tag", mark="CHALK-1"))
        self.assertEqual(identity.latest_review(self.db, "unregistered-reader-tag")["animal"], "A01")
        self.assert_refused("tag_mark_disagree", review("conflict", 3, "opposing-reader-tag", mark="CHALK-1"))
        self.assert_refused("stale_review", review("old-exclude", 3, "unregistered-reader-tag"))
        self.apply(review("exclude", 4, "unregistered-reader-tag", expected="manual"))
        self.assertEqual(identity.latest_review(self.db, "unregistered-reader-tag")["outcome"], "exclude")


    def test_pen_keeper_contract_changes_lot_allocation(self):
        self.prefix("tally-17")
        contract = dict(pen="P-NORTH", owner="Maple", keeper="Juniper Yard", action="revoke", expected="grant-P-NORTH")
        self.assert_refused("wrong_keeper", event("pen-contract", "bad-keeper", 0, **{**contract, "keeper": "Maple"}))
        self.apply(event("pen-contract", "north-revoke", 0, **contract), desk="yard")
        rows = {row["animal"]: row for row in self.lot()["allocations"]}
        self.assertEqual(rows["A01"]["reason"], "no_pen_contract")
        self.assertTrue(rows["A02"]["eligible"])
        self.assert_refused("no_pen_contract", close("blocked", 1))
        self.apply(event("pen-contract", "north-regrant", 2, **{**contract, "action": "grant", "expected": "north-revoke"}))
        self.assert_refused("tally_evidence_changed", close("old-version", 3))
        self.apply(seal("after-grant", 4), close("allowed", 5, "after-grant"))
        self.assertEqual(self.lot()["state"], "closed")


    def test_directional_move_requires_route_and_consumes_scan_once(self):
        self.prefix("r-in-1")
        move = SHIFT[11]
        self.assertEqual(move["id"], "m-in-1")
        self.assert_refused("route_mismatch", {**move, "to": "P-WAIT"}, desk="yard")
        self.apply(move, desk="yard")
        self.assertEqual(yard.position(self.db, "A01")["pen"], "P-STAGE")
        self.assert_refused("scan_consumed", {**move, "id": "reuse", "at": "2026-11-24T10:01:00Z"})
        self.assertEqual(self.apply(move)["duplicates"], ["m-in-1"])


    def test_drain_blocks_admission_but_allows_departure(self):
        self.prefix("retag-1")
        self.apply(event("drain-pen", "drain-wait", 0, pen="P-WAIT", keeper="Nora"),
                   scan("arrival", 1, "RF-201", "P-STAGE", "P-WAIT"), review("arrival-review", 2, "arrival", mark="CHALK-1"))
        self.assert_refused("pen_not_admitting", event("move", "arrival-move", 3, animal="A01", scan="arrival", owner="Cedar",
                            **{"from": "P-STAGE", "to": "P-WAIT"}))
        self.apply(scan("leave-wait", 3, "RF-103", "P-WAIT", "OUT"), review("leave-review", 4, "leave-wait", mark="CHALK-3"),
                   event("move", "leave", 5, animal="A03", scan="leave-wait", owner="Cedar", **{"from": "P-WAIT", "to": "OUT"}))
        self.assertEqual(self.pen("P-WAIT")["occupants"], [])
        self.apply(review("cancel-denied-arrival", 6, "arrival", expected="arrival-review"),
                   event("clear-pen", "walk-wait", 7, pen="P-WAIT", keeper="Nora", walked_by="Ira"))
        self.assertEqual(self.pen("P-WAIT")["state"], "cleared")
        self.assertEqual(report.snapshot(self.db)["lots"], [])


    def test_clear_requires_empty_pen_and_reconciled_gate(self):
        self.prefix("drain-stage")
        clear = event("clear-pen", "walk", 2, pen="P-STAGE", keeper="Nora", walked_by="Ira")
        self.assert_refused("pen_occupied", clear)
        self.prefix("close-17")
        self.apply(LATE[0])
        self.assert_refused("independent_walk_required", {**clear, "walked_by": "Nora"})
        self.assert_refused("unresolved_gate_scans.*late-stage-echo", clear)
        self.apply(review("ignore-reader-echo", 1, "late-stage-echo"), clear, desk=None)
        self.assertEqual((self.pen()["state"], self.pen()["clear_event"]), ("cleared", "walk"))


    def test_two_valid_dispositions_of_same_late_scan_change_clearance(self):
        self.prefix()
        late_read = {**LATE[0], "id": "late-unmapped", "tag": "UNMAPPED"}
        self.apply(late_read)
        states = {}
        for mark in (None, "CHALK-1"):
            with closing(ledger.connect(":memory:")) as branch:
                self.db.backup(branch)
                ledger.apply(branch, [review("review-ambiguous-read", 0, "late-unmapped", mark=mark)])
                state = report.snapshot(branch)
                pen = next(row for row in state["pens"] if row["id"] == "P-STAGE")
                self.assertEqual(pen["clear_event"], "clear-stage")
                self.assertEqual(state["lots"][0]["state"], "closed")
                states[mark] = (pen["state"], pen["unreconciled_scans"])
        self.assertEqual(states, {None: ("cleared", []), "CHALK-1": ("clear_disputed", ["late-unmapped"])})

