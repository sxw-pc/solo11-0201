from contextlib import closing

from ear_tag_transfer import coverage, identity, ledger, report, yard
from auction_support import AuctionCase, LATE, SHIFT, close, event, review, scan, seal

DAY = "2026-11-25T"


def stamp(second):
    return f"{DAY}08:{second // 60:02d}:{second % 60:02d}Z"


def device_event(kind, key, second, **fields):
    return {"type": kind, "id": key, "at": stamp(second), **fields}


def reader_session(key, reader, session_id, second=0):
    return device_event("reader-session", key, second, reader=reader, session=session_id)


def reader_lane(key, reader, session_id, start, end, origin, destination):
    return {"type": "reader-lane", "id": key, "at": stamp(start), "reader": reader, "session": session_id,
            "from": origin, "to": destination, "starts_at": stamp(start), "ends_at": stamp(end)}


def clock_head(key, reader, session_id, second, seq, earliest, latest):
    return {"type": "reader-clock-head", "id": key, "at": stamp(second), "reader": reader,
            "session": session_id, "seq": seq, "earliest_at": stamp(earliest), "latest_at": stamp(latest)}


def device_scan(key, second, tag, reader, session_id, seq, origin, destination):
    return {"type": "gate-scan", "id": key, "at": stamp(second), "tag": tag, "reader": reader,
            "session": session_id, "seq": seq, "from": origin, "to": destination}


def authorization(key, second, pen_id, origin, destination, source, backup):
    return device_event("coverage-authorization", key, second, pen=pen_id,
                        **{"from": origin, "to": destination},
                        source_reader=source, backup_reader=backup)


def device_review(key, second, scan_id, *, mark=None, expected=None):
    fields = {"scan": scan_id, "expected": expected, "outcome": "confirm" if mark else "exclude",
              "operator": "Ira", "reason": "Handheld adjudication at the crossbar"}
    if mark:
        fields["mark"] = mark
    return device_event("review-scan", key, second, **fields)


def device_move(key, second, animal_id, scan_id, origin, destination):
    return device_event("move", key, second, animal=animal_id, scan=scan_id, owner="Cedar",
                        **{"from": origin, "to": destination})


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


class DeviceCoverageTests(AuctionCase):
    """Synthetic reader sessions: sequence gaps, lane switches and redundant proof."""

    WAIT, NORTH, OTHER = "P-WAIT", "P-NORTH", "P-OTHER"
    READER, BACKUP = "Reader-1", "Reader-2"
    SESSION, BACKUP_SESSION = "shift-A", "shift-B"

    def _yard(self):
        # Pens, contracts and animals all open before the 08:00 device day.
        self.apply(event("open-pen", "open-wait", 0, pen=self.WAIT, keeper="Nora"),
                   event("pen-contract", "grant-wait", 1, pen=self.WAIT, owner="Cedar", keeper="Nora",
                         action="grant", expected=None),
                   event("open-pen", "open-north", 2, pen=self.NORTH, keeper="Juniper Yard"),
                   event("pen-contract", "grant-north", 3, pen=self.NORTH, owner="Maple",
                         keeper="Juniper Yard", action="grant", expected=None),
                   event("pen-contract", "grant-north-cedar", 4, pen=self.NORTH, owner="Cedar",
                         keeper="Juniper Yard", action="grant", expected=None),
                   event("open-pen", "open-other", 5, pen=self.OTHER, keeper="Nora"),
                   event("register-animal", "reg-a01", 6, animal="A01", tag="RF-101",
                         mark="CHALK-1", owner="Cedar"),
                   event("register-animal", "reg-a02", 7, animal="A02", tag="RF-102",
                         mark="CHALK-2", owner="Cedar"), desk=None)

    def _stage_animals(self, animals=("A01", "A02")):
        # Legacy (non-session) intake scans move the animals into the waiting pen,
        # using the original capture protocol that needs no device coverage. The
        # moves must precede the session's second-10 departure scans.
        tags = {"A01": ("intake-a01", "RF-101", "CHALK-1"), "A02": ("intake-a02", "RF-102", "CHALK-2")}
        second, events = 2, []
        for animal_id in animals:
            scan_id, tag, mark = tags[animal_id]
            events.append({"type": "gate-scan", "id": scan_id, "at": stamp(second), "tag": tag,
                           "reader": "Intake gate", "from": "OUT", "to": self.WAIT})
            second += 1
            events.append(device_review(f"rv-{scan_id}", second, scan_id, mark=mark))
            second += 1
            events.append(device_move(f"mv-{scan_id}", second, animal_id, scan_id, "OUT", self.WAIT))
            second += 1
        self.apply(*events)

    def _scenario_a_fragments(self):
        # Reader-1 anchors seq 10 at second 10 and seq 13 at second 13; gaps 11 and 12
        # can only occupy seconds 11 and 12. At second 12 the device leaves the
        # WAIT -> NORTH lane for another directed lane (half-open segments).
        return [
            reader_session("r1-open", self.READER, self.SESSION),
            reader_lane("r1-lane-wait", self.READER, self.SESSION, 0, 12, self.WAIT, self.NORTH),
            reader_lane("r1-lane-other", self.READER, self.SESSION, 12, 20, self.OTHER, self.NORTH),
            device_scan("r1-seq10", 10, "RF-101", self.READER, self.SESSION, 10, self.WAIT, self.NORTH),
            device_scan("r1-seq13", 13, "RF-103", self.READER, self.SESSION, 13, self.OTHER, self.NORTH),
        ]

    def _backup_fragments(self):
        # An independent reader with a complete, gap-free original range on the
        # same directed lane, continuously covering second 11.
        return [
            reader_session("r2-open", self.BACKUP, self.BACKUP_SESSION),
            reader_lane("r2-lane", self.BACKUP, self.BACKUP_SESSION, 0, 20, self.WAIT, self.NORTH),
            device_scan("r2-seq10", 10, "RF-101", self.BACKUP, self.BACKUP_SESSION, 10, self.WAIT, self.NORTH),
            device_scan("r2-seq11", 11, "RF-102", self.BACKUP, self.BACKUP_SESSION, 11, self.WAIT, self.NORTH),
            device_scan("r2-seq12", 12, "RF-104", self.BACKUP, self.BACKUP_SESSION, 12, self.WAIT, self.NORTH),
        ]

    def _reconcile_and_drain(self, backup=False):
        # Resolve every real gate scan, move the staged animals out, then drain.
        # All adjudication happens after the last device read so control events stay ordered.
        second = 24
        events = [device_review("rv-r1-10", second, "r1-seq10", mark="CHALK-1")]
        second += 1
        events.append(device_review("rv-r1-13", second, "r1-seq13"))
        second += 1
        if backup:
            for scan_id, mark in (("r2-seq10", None), ("r2-seq11", "CHALK-2"), ("r2-seq12", None)):
                events.append(device_review(f"rv-{scan_id}", second, scan_id, mark=mark))
                second += 1
        events.append(device_move("mv-out-a01", second, "A01", "r1-seq10", self.WAIT, self.NORTH))
        second += 1
        if backup:
            events.append(device_move("mv-out-a02", second, "A02", "r2-seq11", self.WAIT, self.NORTH))
            second += 1
        events.append(device_event("drain-pen", "drain-wait", second, pen=self.WAIT, keeper="Nora"))
        self.apply(*events)

    def _clear(self, key="clear-wait", second=40):
        return device_event("clear-pen", key, second, pen=self.WAIT, keeper="Nora", walked_by="Ira")

    def _coverage(self, through):
        return coverage.pen_coverage(self.db, yard.pen(self.db, self.WAIT), stamp(through))

    def _gap(self, conclusion, seq):
        return next(row for row in conclusion["blocking_gaps"] + conclusion["redundant_gaps"]
                    if row["seq"] == seq and row["reader"] == self.READER)

    def test_source_protocol_holds_clearance_for_wait_lane_gap_only(self):
        self._yard()
        self._stage_animals(("A01",))
        self.apply(*self._scenario_a_fragments())
        self._reconcile_and_drain()
        # Signed cutoff at second 12, exactly the acceptance cutoff in the brief.
        conclusion = self._coverage(12)
        self.assertEqual(conclusion["evidence_conflicts"], [])
        self.assertEqual([row["seq"] for row in conclusion["blocking_gaps"]], [11])
        gap11 = self._gap(conclusion, 11)
        self.assertEqual(gap11["blocking_at"], [stamp(11)])
        self.assertEqual(gap11["blocking_lanes"], [(self.WAIT, self.NORTH)])
        # Gap 12's only feasible second is 12, already on the unrelated OTHER lane.
        self.assertEqual(conclusion["sessions"][0]["feasible_at"],
                         {11: [stamp(11)], 12: [stamp(12)]})
        self.assertEqual(conclusion["redundant_gaps"], [])
        self.assert_refused("unverified_device_gap.*#11", self._clear())
        self.assertIsNone(yard.pen(self.db, self.WAIT)["clear_event"])

    def test_redundant_protocol_signs_when_independent_reader_covers_gap(self):
        self._yard()
        self._stage_animals()
        self.apply(*self._scenario_a_fragments(), *self._backup_fragments())
        self._reconcile_and_drain(backup=True)
        # Same materials still follow the source-machine protocol before authorization.
        self.assert_refused("unverified_device_gap.*#11", self._clear())
        self.apply(authorization("cover-11", 35, self.WAIT, self.WAIT, self.NORTH, self.READER, self.BACKUP))
        conclusion = self._coverage(40)
        self.assertEqual(conclusion["blocking_gaps"], [])
        gap11 = self._gap(conclusion, 11)
        self.assertTrue(gap11["redundant"])
        self.assertEqual(gap11["backup_reader"], self.BACKUP)
        self.apply(self._clear())
        pen = self.pen(self.WAIT)
        self.assertEqual(pen["state"], "cleared")
        self.assertEqual(pen["clear_event"], "clear-wait")
        # The visa freezes the basis and the range actually used.
        self.assertEqual(pen["gate_coverage"]["clearance_evidence"],
                         [{"clear_event": "clear-wait", "reader": self.READER, "session": self.SESSION,
                           "origin": self.WAIT, "destination": self.NORTH, "covered_seqs": [11],
                           "basis": "redundant_reader_coverage"}])
        self.assertEqual(ledger.replay(self.db)["differences"], [])

    def test_redundant_protocol_rejects_backup_with_its_own_gap(self):
        self._yard()
        self._stage_animals(("A01",))
        self.apply(
            *self._scenario_a_fragments(),
            reader_session("r2-open", self.BACKUP, self.BACKUP_SESSION),
            reader_lane("r2-lane", self.BACKUP, self.BACKUP_SESSION, 0, 20, self.WAIT, self.NORTH),
            # Anchors seq 10 and seq 12 with seq 11 missing: the backup itself is not gap-free.
            device_scan("r2-seq10", 10, "RF-101", self.BACKUP, self.BACKUP_SESSION, 10, self.WAIT, self.NORTH),
            device_scan("r2-seq12", 12, "RF-104", self.BACKUP, self.BACKUP_SESSION, 12, self.WAIT, self.NORTH))
        # Reconcile only the source reader: its OTHER-lane read is excluded, the one
        # real WAIT crossing moves A01 out. The backup's scans are excluded as
        # adjudication copies, but its own missing seq 11 still disqualifies its range.
        self.apply(device_review("rv-r1-10", 24, "r1-seq10", mark="CHALK-1"),
                   device_review("rv-r1-13", 25, "r1-seq13"),
                   device_review("rv-r2-10", 26, "r2-seq10"),
                   device_review("rv-r2-12", 27, "r2-seq12"),
                   device_move("mv-out-a01", 28, "A01", "r1-seq10", self.WAIT, self.NORTH),
                   device_event("drain-pen", "drain-wait", 29, pen=self.WAIT, keeper="Nora"))
        self.apply(authorization("cover-11", 35, self.WAIT, self.WAIT, self.NORTH, self.READER, self.BACKUP))
        # The backup range has its own unexplained gap, so it cannot neutralize gap 11.
        self.assert_refused("unverified_device_gap.*#11", self._clear())

    def test_joint_session_order_removes_false_risk_at_last_lane_second(self):
        # Tail anchor moved from second 13 to second 16 (same sequences 10 and 13).
        # The device serves the WAIT lane only on seconds 14 and 15 (half-open to
        # second 16); seconds 10-13 belong to another directed lane. Clock heads
        # bound gap 11 to seconds 11-14 and gap 12 to second 13 alone.
        self._yard()
        self._stage_animals(("A01",))
        self.apply(
            reader_session("r1-open", self.READER, self.SESSION),
            reader_lane("r1-lane-other", self.READER, self.SESSION, 0, 14, self.OTHER, self.NORTH),
            reader_lane("r1-lane-wait", self.READER, self.SESSION, 14, 16, self.WAIT, self.NORTH),
            reader_lane("r1-lane-tail", self.READER, self.SESSION, 16, 20, self.OTHER, self.NORTH),
            device_scan("r1-seq10", 10, "RF-101", self.READER, self.SESSION, 10, self.OTHER, self.NORTH),
            device_scan("r1-seq13", 16, "RF-103", self.READER, self.SESSION, 13, self.OTHER, self.NORTH),
            clock_head("head-11", self.READER, self.SESSION, 9, 11, 11, 14),
            clock_head("head-12", self.READER, self.SESSION, 9, 12, 13, 13),
            # A01's real WAIT departure rides the legacy protocol, independent of the session.
            {"type": "gate-scan", "id": "wait-exit-a01", "at": stamp(8), "tag": "RF-101",
             "reader": "Crossbar WAIT", "from": self.WAIT, "to": self.NORTH})
        conclusion = self._coverage(40)
        self.assertEqual(conclusion["evidence_conflicts"], [])
        # The clock head alone still permits second 14 for gap 11 (the naive risk).
        head11 = next(row for row in self.db.execute(
            "SELECT * FROM reader_clock_head WHERE seq = 11"))
        self.assertEqual(head11["earliest_at"], stamp(11))
        self.assertEqual(head11["latest_at"], stamp(14))
        # Joint same-session order forces gap 12 onto second 13, which confines
        # gap 11 to seconds 11 and 12; the WAIT lane's seconds 14-15 never risk it.
        self.assertEqual(conclusion["sessions"][0]["feasible_at"],
                         {11: [stamp(11), stamp(12)], 12: [stamp(13)]})
        self.assertNotIn(stamp(14), conclusion["sessions"][0]["feasible_at"][11])
        self.assertEqual(conclusion["blocking_gaps"], [])
        self.apply(device_review("rv-wait-exit", 24, "wait-exit-a01", mark="CHALK-1"),
                   device_review("rv-r1-10", 25, "r1-seq10"),
                   device_review("rv-r1-13", 26, "r1-seq13"),
                   device_move("mv-out-a01", 27, "A01", "wait-exit-a01", self.WAIT, self.NORTH),
                   device_event("drain-pen", "drain-wait", 28, pen=self.WAIT, keeper="Nora"))
        self.apply(self._clear())
        self.assertEqual(self.pen(self.WAIT)["state"], "cleared")
        self.assertEqual(ledger.replay(self.db)["differences"], [])

    def test_infeasible_placements_report_device_evidence_conflict(self):
        self._yard()
        self._stage_animals(("A01",))
        self.apply(
            reader_session("r1-open", self.READER, self.SESSION),
            reader_lane("r1-lane-wait", self.READER, self.SESSION, 0, 20, self.WAIT, self.NORTH),
            device_scan("r1-seq10", 10, "RF-101", self.READER, self.SESSION, 10, self.WAIT, self.NORTH),
            device_scan("r1-seq13", 13, "RF-103", self.READER, self.SESSION, 13, self.WAIT, self.NORTH),
            # Both missing sequences are forced onto second 12: no distinct-second,
            # strictly ordered placement can satisfy both clock heads.
            clock_head("head-11", self.READER, self.SESSION, 9, 11, 12, 12),
            clock_head("head-12", self.READER, self.SESSION, 9, 12, 12, 12))
        conclusion = self._coverage(40)
        self.assertEqual(conclusion["blocking_gaps"], [])
        self.assertEqual(conclusion["evidence_conflicts"],
                         [{"reader": self.READER, "session": self.SESSION,
                           "reason": "device_evidence_conflict"}])
        self._reconcile_and_drain()
        self.assert_refused("device_evidence_conflict", self._clear())

    def test_gap_after_sealing_second_and_other_lane_gaps_do_not_block(self):
        self._yard()
        self._stage_animals()
        self.apply(*self._scenario_a_fragments())
        # At a cutoff of second 10 nothing missing could have occurred yet.
        self.assertEqual(self._coverage(10)["blocking_gaps"], [])
        # The OTHER pen is only an endpoint of its own lane: gap 12 at second 12 is
        # its risk, and a cutoff before second 12 removes it.
        other = yard.pen(self.db, self.OTHER)
        self.assertEqual(
            [row["seq"] for row in coverage.pen_coverage(self.db, other, stamp(11))["blocking_gaps"]], [])
        self.assertEqual(
            [row["seq"] for row in coverage.pen_coverage(self.db, other, stamp(12))["blocking_gaps"]], [12])

    def test_legacy_gate_scans_without_device_evidence_keep_old_protocol(self):
        self.prefix("drain-stage")
        # The whole fixture shift has no reader sessions; clearance behaves exactly as before.
        self.prefix("close-17")
        self.apply(LATE[0])
        self.assert_refused("unresolved_gate_scans.*late-stage-echo",
                            event("clear-pen", "walk-again", 40, pen="P-STAGE", keeper="Nora", walked_by="Ira"))
        self.apply(review("ignore-reader-echo", 1, "late-stage-echo"),
                   event("clear-pen", "walk-again", 40, pen="P-STAGE", keeper="Nora", walked_by="Ira"), desk=None)
        pen = self.pen("P-STAGE")
        self.assertEqual(pen["state"], "cleared")
        self.assertEqual(pen["gate_coverage"]["sessions"], [])
        self.assertEqual(pen["gate_coverage"]["blocking_gaps"], [])
        self.assertEqual(pen["gate_coverage"]["clearance_evidence"], [])

    def test_frozen_visa_survives_later_device_upload(self):
        self._yard()
        self._stage_animals()
        self.apply(*self._scenario_a_fragments(), *self._backup_fragments())
        self._reconcile_and_drain(backup=True)
        self.apply(authorization("cover-11", 35, self.WAIT, self.WAIT, self.NORTH, self.READER, self.BACKUP),
                  self._clear())
        frozen = self.pen(self.WAIT)["gate_coverage"]["clearance_evidence"]
        # A later clock-head upload cannot rewrite the signed visa or its range.
        self.apply(clock_head("late-head", self.READER, self.SESSION, 41, 11, 11, 11))
        again = self.pen(self.WAIT)
        self.assertEqual(again["clear_event"], "clear-wait")
        self.assertEqual(again["gate_coverage"]["clearance_evidence"], frozen)

    def test_capture_validates_session_sequence_and_lane_segment(self):
        self._yard()
        self.apply(reader_session("r1-open", self.READER, self.SESSION),
                   reader_lane("r1-lane-wait", self.READER, self.SESSION, 0, 12, self.WAIT, self.NORTH))
        good = device_scan("r1-seq10", 10, "RF-101", self.READER, self.SESSION, 10, self.WAIT, self.NORTH)
        self.assert_refused("scan_lane_mismatch",
                            {**good, "id": "wrong-route", "from": self.OTHER, "to": self.NORTH})
        self.assert_refused("scan_outside_lane_segment", {**good, "id": "outside", "at": stamp(13)})
        self.apply(good)
        self.assert_refused("duplicate_session_sequence", {**good, "id": "dup"})
        # Adjacent sequences must stay at least one second apart and stay ordered.
        self.assert_refused("session_sequence_order",
                            device_scan("r1-seq11", 10, "RF-102", self.READER, self.SESSION, 11,
                                        self.WAIT, self.NORTH))
