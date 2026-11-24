"""Pen tenancy, directional gate use, and physical clearance."""

from .identity import animal, latest_review
from .rules import expected, known, one, require, text

OUT = "OUT"


def pen(db, pen_id):
    return known(db, "SELECT * FROM pen WHERE id = ?", (pen_id,), "unknown_pen")


def position(db, animal_id):
    return one(db, """SELECT p.*, m.at, m.event FROM position p JOIN movement m ON m.scan = p.scan
                      WHERE p.animal = ?""", (animal_id,))


def contract(db, pen_id, owner):
    return one(db, """SELECT c.* FROM pen_contract c WHERE c.pen = ? AND c.owner = ?
                      AND NOT EXISTS (SELECT 1 FROM pen_contract n WHERE n.prior = c.event)""", (pen_id, owner))


def admission_problem(db, target, owner):
    if target["drain_at"]:
        return "pen_not_admitting"
    agreement = contract(db, target["id"], owner)
    if not agreement or not agreement["active"]:
        return "no_pen_contract"
    return None


def open_pen(db, event):
    pen_id, keeper = text(event, "pen"), text(event, "keeper")
    require(pen_id != OUT and not one(db, "SELECT 1 FROM pen WHERE id = ?", (pen_id,)), "pen_id_in_use", pen_id)
    db.execute("INSERT INTO pen(id, keeper, opened_at) VALUES (?,?,?)", (pen_id, keeper, event["at"]))


def contract_pen(db, event):
    pen_id, owner, action = (text(event, key) for key in ("pen", "owner", "action"))
    target = pen(db, pen_id)
    require(text(event, "keeper") == target["keeper"], "wrong_keeper", pen_id)
    require(action in {"grant", "revoke"}, "contract_action", action)
    current = contract(db, pen_id, owner)
    prior = current["event"] if current else None
    require(expected(event, "expected") == prior, "stale_pen_contract", pen_id)
    active = action == "grant"
    require(active != bool(current and current["active"]), "contract_no_change", pen_id)
    require(not active or not target["drain_at"], "pen_not_admitting", pen_id)
    db.execute("INSERT INTO pen_contract VALUES (?,?,?,?,?)", (event["id"], pen_id, owner, active, prior))


def capture(db, event):
    origin, destination, tag, reader = (text(event, key) for key in ("from", "to", "tag", "reader"))
    require(origin != destination, "same_pen", origin)
    for endpoint in (origin, destination):
        if endpoint != OUT:
            require(event["at"] >= pen(db, endpoint)["opened_at"], "scan_before_pen_open", endpoint)
    db.execute("INSERT INTO gate_scan VALUES (?,?,?,?,?,?)", (event["id"], event["at"], tag, reader, origin, destination))


def proof(db, scan_id, animal_id, origin, destination, owner, at):
    gate = known(db, "SELECT * FROM gate_scan WHERE id = ?", (scan_id,), "unknown_scan")
    require(gate["origin"] == origin and gate["destination"] == destination, "route_mismatch", scan_id)
    require(not one(db, "SELECT 1 FROM movement WHERE scan = ?", (scan_id,)), "scan_consumed", scan_id)
    review = latest_review(db, scan_id)
    require(review and review["outcome"] == "confirm" and review["animal"] == animal_id,
            "scan_not_confirmed_for_animal", scan_id)
    require(gate["at"] <= at, "future_gate_scan", scan_id)
    current = position(db, animal_id)
    actual = current["pen"] if current else OUT
    require(actual == origin, "origin_mismatch", f"{animal_id} is at {actual}")
    require(not current or current["at"] <= gate["at"], "scan_before_current_position", scan_id)
    require(not current or not movement_disputed(db, current["scan"]), "current_position_disputed", animal_id)
    if origin != OUT:
        require(not pen(db, origin)["clear_event"], "origin_cleared", origin)
    agreement = None
    if destination != OUT:
        issue = admission_problem(db, pen(db, destination), owner)
        require(not issue, issue, f"{owner} / {destination}")
        agreement = contract(db, destination, owner)["event"]
    return {"review": review["id"], "contract": agreement, "origin_move": current["event"] if current else None}


def record_move(db, event, animal_id, scan_id, origin, destination, review_id, lot=None):
    db.execute("INSERT INTO movement VALUES (?,?,?,?,?,?,?,?)",
               (scan_id, event["id"], review_id, animal_id, origin, destination, event["at"], lot))
    db.execute("""INSERT INTO position VALUES (?,?,?) ON CONFLICT(animal)
                  DO UPDATE SET pen = excluded.pen, scan = excluded.scan""", (animal_id, destination, scan_id))


def move(db, event):
    animal_id, scan_id, origin, destination = (text(event, key) for key in ("animal", "scan", "from", "to"))
    subject = animal(db, animal_id)
    require(text(event, "owner") == subject["owner"], "wrong_owner", animal_id)
    accepted = proof(db, scan_id, animal_id, origin, destination, subject["owner"], event["at"])
    record_move(db, event, animal_id, scan_id, origin, destination, accepted["review"])


def drain(db, event):
    pen_id = text(event, "pen")
    target = pen(db, pen_id)
    require(text(event, "keeper") == target["keeper"], "wrong_keeper", pen_id)
    require(not target["drain_at"], "pen_already_draining", pen_id)
    db.execute("UPDATE pen SET drain_at = ?, drain_event = ? WHERE id = ?", (event["at"], event["id"], pen_id))


def unresolved(db, target, through=None):
    scans = db.execute("""SELECT s.id FROM gate_scan s WHERE (s.origin = ? OR s.destination = ?)
                         AND s.at >= ? AND (? IS NULL OR s.at <= ?)
                         AND NOT EXISTS (SELECT 1 FROM movement m WHERE m.scan = s.id)
                         ORDER BY s.at, s.id""",
                       (target["id"], target["id"], target["opened_at"], through, through))
    pending = []
    for scan in scans:
        review = latest_review(db, scan["id"])
        if not review or review["outcome"] != "exclude":
            pending.append(scan["id"])
    return pending


def movement_disputed(db, scan_id):
    movement = known(db, "SELECT animal FROM movement WHERE scan = ?", (scan_id,), "unused_movement")
    review = latest_review(db, scan_id)
    return review["outcome"] != "confirm" or review["animal"] != movement["animal"]


def movement_disputes(db, pen_id=None):
    rows = db.execute("""SELECT m.* FROM movement m WHERE (? IS NULL OR m.origin = ? OR m.destination = ?)
                         ORDER BY m.at, m.scan""", (pen_id, pen_id, pen_id))
    return [dict(row) for row in rows if movement_disputed(db, row["scan"])]


def occupants(db, pen_id):
    return [row["animal"] for row in db.execute("SELECT animal FROM position WHERE pen = ? ORDER BY animal", (pen_id,))]


def clear(db, event):
    pen_id = text(event, "pen")
    target = pen(db, pen_id)
    require(text(event, "keeper") == target["keeper"], "wrong_keeper", pen_id)
    require(text(event, "walked_by") != target["keeper"], "independent_walk_required", pen_id)
    require(target["drain_at"] and not target["clear_event"], "pen_not_draining", pen_id)
    require(not occupants(db, pen_id), "pen_occupied", pen_id)
    blockers = unresolved(db, target, event["at"])
    require(not blockers, "unresolved_gate_scans", ", ".join(blockers))
    require(not movement_disputes(db, pen_id), "disputed_pen_movement", pen_id)
    db.execute("UPDATE pen SET cleared_at = ?, clear_event = ? WHERE id = ?", (event["at"], event["id"], pen_id))


COMMANDS = {"open-pen": open_pen, "pen-contract": contract_pen, "gate-scan": capture,
            "move": move, "drain-pen": drain, "clear-pen": clear}

