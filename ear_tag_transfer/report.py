"""Explain live decisions and keep frozen handovers visible alongside disputes."""

import json

from . import identity, sale, weighing, yard
from .ledger import business_clock, check_version, transaction


def pen_report(db, target):
    pending = yard.unresolved(db, target)
    disputes = yard.movement_disputes(db, target["id"])
    occupants = yard.occupants(db, target["id"])
    if target["clear_event"]:
        state = "clear_disputed" if pending or disputes or occupants else "cleared"
    elif target["drain_at"]:
        state = "draining"
    else:
        state = "open"
    contracts = db.execute("""SELECT c.* FROM pen_contract c WHERE c.pen = ?
                              AND NOT EXISTS (SELECT 1 FROM pen_contract n WHERE n.prior = c.event) ORDER BY c.owner""",
                           (target["id"],))
    return {**dict(target), "state": state, "occupants": occupants, "unreconciled_scans": pending,
            "disputed_moves": [row["scan"] for row in disputes], "contracts": [dict(row) for row in contracts]}


def lot_report(db, contract, clock):
    key = contract["id"]
    tallies = [dict(row) for row in db.execute("SELECT * FROM tally WHERE lot = ? ORDER BY counted_at", (key,))]
    latest = tallies[-1] if tallies else None
    if latest:
        latest = {**latest, "count": len(sale.tally_members(db, latest["id"])),
                  "members": sale.tally_members(db, latest["id"]), "weights": sale.tally_weights(db, latest["id"])}
        totals = {}
        for weight in latest["weights"]:
            pen_id = weight["destination"]
            totals[pen_id] = totals.get(pen_id, 0) + weight["net_g"]
        latest["net_g_by_pen"] = totals
    result = {**dict(contract), "tally": latest, "tally_history": tallies}
    if contract["closed_by"]:
        handovers = db.execute("SELECT * FROM handover WHERE lot = ? ORDER BY animal", (key,))
        return {**result, "state": "closed", "handover": [dict(row) for row in handovers],
                "exceptions": sale.handover_exceptions(db, latest)}
    if contract["cancelled_by"]:
        return {**result, "state": "cancelled"}
    return {**result, "state": "pending", "allocations": sale.pending_plan(db, contract),
            "release": sale.readiness(db, contract, latest, clock)}


def load_report(db, load):
    load_id = load["id"]
    chosen = weighing.current(db, load_id)
    pending = weighing.pending(db, load_id)
    return {**dict(load), "animals": weighing.members(db, load_id), "chosen": dict(chosen) if chosen else None,
            "pending": pending, "usable": bool(chosen and not pending)}


def scan_report(db, scan):
    review = identity.latest_review(db, scan["id"])
    return {**dict(scan), "rfid_candidate": identity.candidate(db, scan["tag"], scan["at"]),
            "review": dict(review) if review else None}


def snapshot(db, *, include_evidence=False):
    with transaction(db):
        check_version(db)
        clock = business_clock(db)
        report = {"business_clock": clock or None,
                  "animals": [dict(row) for row in db.execute("SELECT * FROM animal ORDER BY id")],
                  "pens": [pen_report(db, row) for row in db.execute("SELECT * FROM pen ORDER BY id")],
                  "loads": [load_report(db, row) for row in db.execute("SELECT * FROM scale_load ORDER BY id")],
                  "lots": [lot_report(db, row, clock) for row in db.execute("SELECT * FROM auction_lot ORDER BY id")],
                  "scans": [scan_report(db, row) for row in db.execute("SELECT * FROM gate_scan ORDER BY at, id")],
                  "movement_disputes": yard.movement_disputes(db)}
        if include_evidence:
            report["journal"] = [{"seq": row["seq"], "event": json.loads(row["body"]), "digest": row["digest"]}
                                 for row in db.execute("SELECT * FROM journal ORDER BY seq")]
        return report
