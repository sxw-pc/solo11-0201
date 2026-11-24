"""Physical deck loads, integer-gram tickets and explicit reticket decisions."""

from .identity import animal
from .rules import expected, identifiers, integer, known, one, require, text


def load(db, load_id):
    return known(db, "SELECT * FROM scale_load WHERE id = ?", (load_id,), "unknown_load")


def members(db, load_id):
    return [row["animal"] for row in db.execute("SELECT animal FROM load_member WHERE load = ? ORDER BY animal", (load_id,))]


def open_load(db, event):
    load_id, deck = text(event, "load"), text(event, "deck")
    group = identifiers(event, "animals")
    require(not one(db, "SELECT 1 FROM scale_load WHERE id = ?", (load_id,)), "load_id_in_use", load_id)
    for animal_id in group:
        require(animal(db, animal_id)["registered_at"] <= event["at"], "load_before_registration", animal_id)
    db.execute("INSERT INTO scale_load VALUES (?,?,?)", (load_id, event["at"], deck))
    db.executemany("INSERT INTO load_member VALUES (?,?)", [(load_id, animal_id) for animal_id in group])


def capture(db, event):
    load_id, receipt = text(event, "load"), text(event, "receipt")
    opened = load(db, load_id)
    require(opened["opened_at"] <= event["at"], "ticket_before_load", load_id)
    gross, tare = integer(event, "gross_g", 1), integer(event, "tare_g")
    require(gross > tare, "nonpositive_net", receipt)
    require(not one(db, "SELECT 1 FROM scale_ticket WHERE deck = ? AND receipt = ?", (opened["deck"], receipt)),
            "receipt_already_captured", receipt)
    db.execute("INSERT INTO scale_ticket VALUES (?,?,?,?,?,?,?)",
               (event["id"], load_id, opened["deck"], receipt, event["at"], gross, tare))


def current(db, load_id):
    return one(db, """SELECT c.id AS choice, t.id AS ticket, t.gross_g, t.tare_g, t.at,
                             t.gross_g - t.tare_g AS net_g
                      FROM scale_choice c JOIN scale_ticket t ON t.id = c.ticket WHERE c.load = ?
                      AND NOT EXISTS (SELECT 1 FROM scale_choice n WHERE n.prior = c.id)""", (load_id,))


def pending(db, load_id):
    return [row["id"] for row in db.execute("""SELECT t.id FROM scale_ticket t WHERE t.load = ?
            AND NOT EXISTS (SELECT 1 FROM resolved_ticket r WHERE r.ticket = t.id) ORDER BY t.id""", (load_id,))]


def choose(db, event):
    load_id, ticket_id, reason = (text(event, key) for key in ("load", "ticket", "reason"))
    load(db, load_id)
    before = current(db, load_id)
    prior = before["choice"] if before else None
    require(expected(event, "expected") == prior, "stale_scale_choice", load_id)
    seen = identifiers(event, "considered")
    require(set(seen) == set(pending(db, load_id)), "unacknowledged_tickets", load_id)
    eligible = set(seen)
    if before:
        eligible.add(before["ticket"])
    require(ticket_id in eligible, "ticket_not_eligible", ticket_id)
    for candidate in eligible:
        ticket = known(db, "SELECT at FROM scale_ticket WHERE id = ? AND load = ?", (candidate, load_id), "unknown_ticket")
        require(ticket["at"] <= event["at"], "future_scale_ticket", candidate)
    db.execute("INSERT INTO scale_choice VALUES (?,?,?,?,?)", (event["id"], load_id, ticket_id, prior, reason))
    db.executemany("INSERT INTO resolved_ticket VALUES (?,?)", [(candidate, event["id"]) for candidate in seen])


COMMANDS = {"open-load": open_load, "scale-ticket": capture, "choose-ticket": choose}
