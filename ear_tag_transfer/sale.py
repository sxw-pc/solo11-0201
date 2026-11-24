"""Lot reservation and atomic transfer of title and boarding custody."""

from . import weighing, yard
from .identity import animal
from .rules import RuleError, expected, identifiers, integer, known, one, records, require, text


def lot(db, lot_id):
    return known(db, "SELECT * FROM auction_lot WHERE id = ?", (lot_id,), "unknown_lot")


def active_lot(db, lot_id):
    sale = lot(db, lot_id)
    require(not sale["cancelled_by"] and not sale["closed_by"], "lot_final", lot_id)
    return sale


def nominations(db, lot_id):
    return db.execute("SELECT * FROM nomination WHERE lot = ? ORDER BY animal", (lot_id,)).fetchall()


def eligibility(db, sale, animal_id, destination, version=None):
    subject = animal(db, animal_id)
    if one(db, "SELECT 1 FROM handover WHERE animal = ?", (animal_id,)):
        return "already_sold"
    if subject["owner"] != sale["seller"] or (version and subject["owner_version"] != version):
        return "seller_title_mismatch"
    reservation = one(db, "SELECT lot FROM reservation WHERE animal = ?", (animal_id,))
    if reservation and reservation["lot"] != sale["id"]:
        return "reserved_elsewhere"
    current = yard.position(db, animal_id)
    if not current or current["pen"] == yard.OUT or current["pen"] == destination:
        return "not_staged_for_destination"
    if yard.movement_disputed(db, current["scan"]):
        return "staging_movement_disputed"
    return yard.admission_problem(db, yard.pen(db, destination), sale["buyer"])


def offer(db, event):
    lot_id, seller, buyer = (text(event, key) for key in ("lot", "seller", "buyer"))
    require(seller != buyer, "buyer_is_seller", buyer)
    require(not one(db, "SELECT 1 FROM auction_lot WHERE id = ?", (lot_id,)), "lot_id_in_use", lot_id)
    count = integer(event, "expected_count", 1)
    members = [(text(row, "animal"), text(row, "to")) for row in records(event, "members")]
    require(len({animal_id for animal_id, _ in members}) == len(members), "duplicate_nomination", lot_id)
    require(len(members) == count, "declared_count_mismatch", lot_id)
    sale = {"id": lot_id, "seller": seller, "buyer": buyer}
    for animal_id, destination in members:
        issue = eligibility(db, sale, animal_id, destination)
        require(not issue, issue, animal_id)
    db.execute("INSERT INTO auction_lot VALUES (?,?,?,?,NULL,NULL)", (lot_id, seller, buyer, count))
    for animal_id, destination in members:
        db.execute("INSERT INTO nomination VALUES (?,?,?,?)", (lot_id, animal_id, destination, animal(db, animal_id)["owner_version"]))
        db.execute("INSERT INTO reservation VALUES (?,?)", (animal_id, lot_id))


def cancel(db, event):
    lot_id = text(event, "lot")
    sale = active_lot(db, lot_id)
    require(text(event, "seller") == sale["seller"], "seller_title_mismatch", lot_id)
    text(event, "reason")
    db.execute("UPDATE auction_lot SET cancelled_by = ? WHERE id = ?", (event["id"], lot_id))
    db.execute("DELETE FROM reservation WHERE lot = ?", (lot_id,))


def latest_tally(db, lot_id):
    return one(db, """SELECT t.* FROM tally t WHERE t.lot = ?
                      AND NOT EXISTS (SELECT 1 FROM tally n WHERE n.prior = t.id)""", (lot_id,))


def tally_members(db, tally_id):
    return [dict(row) for row in db.execute("SELECT * FROM tally_member WHERE tally = ? ORDER BY animal", (tally_id,))]


def tally_weights(db, tally_id):
    return [dict(row) for row in db.execute("SELECT * FROM tally_weight WHERE tally = ? ORDER BY load", (tally_id,))]


def assemble(db, sale, gate_ids, load_ids, at):
    roster = {row["animal"]: row for row in nominations(db, sale["id"])}
    require(len(gate_ids) == sale["expected_count"], "gate_count_mismatch", sale["id"])
    counted = {}
    for scan_id in gate_ids:
        scan = known(db, "SELECT * FROM gate_scan WHERE id = ?", (scan_id,), "unknown_scan")
        review = yard.latest_review(db, scan_id)
        require(review and review["outcome"] == "confirm", "gate_not_countable", scan_id)
        animal_id = review["animal"]
        require(animal_id in roster and animal_id not in counted, "gate_membership_mismatch", animal_id)
        nomination = roster[animal_id]
        destination = nomination["destination"]
        issue = eligibility(db, sale, animal_id, destination, nomination["owner_version"])
        require(not issue, issue, animal_id)
        current = yard.position(db, animal_id)
        route = yard.proof(db, scan_id, animal_id, current["pen"], destination, sale["buyer"], at)
        counted[animal_id] = {"animal": animal_id, "scan": scan_id, "origin": current["pen"],
                              "destination": destination, **route}
    seen = set()
    weights = []
    for load_id in sorted(load_ids):
        weighing.load(db, load_id)
        group = set(weighing.members(db, load_id))
        require(group <= counted.keys() and not group & seen, "scale_membership_mismatch", load_id)
        destinations = {counted[animal_id]["destination"] for animal_id in group}
        require(len(destinations) == 1, "scale_crosses_destinations", load_id)
        selected = weighing.current(db, load_id)
        require(selected and not weighing.pending(db, load_id), "load_weight_unresolved", load_id)
        for animal_id in sorted(group):
            staged = yard.position(db, animal_id)["at"]
            crossing = one(db, "SELECT at FROM gate_scan WHERE id = ?", (counted[animal_id]["scan"],))["at"]
            require(staged <= selected["at"] <= crossing, "weight_outside_transfer_window", animal_id)
        seen.update(group)
        weights.append({"load": load_id, "choice": selected["choice"], "ticket": selected["ticket"],
                        "destination": destinations.pop(), "net_g": selected["net_g"]})
    require(seen == counted.keys(), "scale_roster_incomplete", sale["id"])
    return [counted[animal_id] for animal_id in sorted(counted)], weights


def seal(db, event):
    lot_id = text(event, "lot")
    sale = active_lot(db, lot_id)
    previous = latest_tally(db, lot_id)
    prior = previous["id"] if previous else None
    require(expected(event, "expected") == prior, "stale_tally", lot_id)
    gates, loads = identifiers(event, "scans"), identifiers(event, "loads")
    members, weights = assemble(db, sale, gates, loads, event["at"])
    db.execute("INSERT INTO tally VALUES (?,?,?,?,?)",
               (event["id"], lot_id, prior, event["at"], sum(row["net_g"] for row in weights)))
    for row in members:
        db.execute("""INSERT INTO tally_member(tally, animal, scan, review, origin, destination, contract, origin_move)
                      VALUES (:tally,:animal,:scan,:review,:origin,:destination,:contract,:origin_move)""",
                   {"tally": event["id"], **row})
    for row in weights:
        db.execute("""INSERT INTO tally_weight(tally, load, choice, ticket, destination, net_g)
                      VALUES (:tally,:load,:choice,:ticket,:destination,:net_g)""", {"tally": event["id"], **row})


def verified_tally(db, sale, tally, at):
    require(tally, "unsealed_lot", sale["id"])
    frozen_members = tally_members(db, tally["id"])
    frozen_weights = tally_weights(db, tally["id"])
    live_members, live_weights = assemble(db, sale, [row["scan"] for row in frozen_members],
                                          [row["load"] for row in frozen_weights], at)
    require(frozen_members == [{"tally": tally["id"], **row} for row in live_members] and
            frozen_weights == [{"tally": tally["id"], **row} for row in live_weights],
            "tally_evidence_changed", tally["id"])
    return live_members


def close(db, event):
    lot_id = text(event, "lot")
    sale = active_lot(db, lot_id)
    require(text(event, "seller") == sale["seller"] and text(event, "buyer") == sale["buyer"], "wrong_sale_parties", lot_id)
    tally = latest_tally(db, lot_id)
    require(tally and text(event, "tally") == tally["id"], "stale_or_missing_tally", lot_id)
    members = verified_tally(db, sale, tally, event["at"])
    for row in members:
        animal_id = row["animal"]
        before = animal(db, animal_id)
        keeper = yard.pen(db, row["destination"])["keeper"]
        yard.record_move(db, event, animal_id, row["scan"], row["origin"], row["destination"], row["review"], lot_id)
        db.execute("INSERT INTO handover VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (animal_id, lot_id, tally["id"], event["at"], sale["seller"], sale["buyer"], keeper,
                    row["destination"], row["scan"], before["owner_version"]))
        db.execute("UPDATE animal SET owner = ?, owner_version = ? WHERE id = ?", (sale["buyer"], event["id"], animal_id))
    db.execute("DELETE FROM reservation WHERE lot = ?", (lot_id,))
    db.execute("UPDATE auction_lot SET closed_by = ? WHERE id = ?", (event["id"], lot_id))


def pending_plan(db, sale):
    rows = []
    for member in nominations(db, sale["id"]):
        animal_id, destination = member["animal"], member["destination"]
        current = yard.position(db, animal_id)
        issue = eligibility(db, sale, animal_id, destination, member["owner_version"])
        rows.append({"animal": animal_id, "from": current["pen"] if current else yard.OUT,
                     "to": destination, "eligible": issue is None, "reason": issue})
    return rows


def readiness(db, sale, tally, at):
    try:
        verified_tally(db, sale, tally, at)
    except RuleError as exc:
        return {"ready": False, "reason": str(exc)}
    return {"ready": True, "reason": None}


def handover_exceptions(db, tally):
    issues = []
    for row in tally_members(db, tally["id"]):
        if yard.movement_disputed(db, row["scan"]):
            issues.append({"code": "handover_identity_disputed", "scan": row["scan"], "animal": row["animal"]})
    for row in tally_weights(db, tally["id"]):
        live = weighing.current(db, row["load"])
        pending = weighing.pending(db, row["load"])
        if pending or live["ticket"] != row["ticket"]:
            issues.append({"code": "handover_weight_disputed", "load": row["load"], "frozen_ticket": row["ticket"],
                           "current_ticket": live["ticket"], "pending_tickets": pending})
    return issues


COMMANDS = {"offer-lot": offer, "cancel-lot": cancel, "seal-lot": seal, "close-lot": close}
