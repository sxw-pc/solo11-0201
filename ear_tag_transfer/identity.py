"""Individual registration, ear-tag history, and handheld gate adjudication."""

from .rules import expected, known, one, require, text


def animal(db, animal_id):
    return known(db, "SELECT * FROM animal WHERE id = ?", (animal_id,), "unknown_animal")


def register(db, event):
    animal_id, tag, mark, owner = (text(event, key) for key in ("animal", "tag", "mark", "owner"))
    require(not one(db, "SELECT 1 FROM animal WHERE id = ? OR mark = ?", (animal_id, mark)),
            "duplicate_animal", animal_id)
    require(not one(db, "SELECT 1 FROM tag WHERE tag = ?", (tag,)), "tag_already_issued", tag)
    db.execute("INSERT INTO animal VALUES (?,?,?,?,?)", (animal_id, mark, owner, event["id"], event["at"]))
    db.execute("INSERT INTO tag VALUES (?,?,?,NULL,?,NULL)", (tag, animal_id, event["at"], event["id"]))


def retag(db, event):
    animal_id, prior, replacement = (text(event, key) for key in ("animal", "prior_tag", "new_tag"))
    text(event, "reason")
    active = known(db, "SELECT * FROM tag WHERE animal = ? AND ends IS NULL", (animal_id,), "unknown_animal")
    require(active["tag"] == prior and active["starts"] < event["at"], "stale_tag", prior)
    require(not one(db, "SELECT 1 FROM tag WHERE tag = ?", (replacement,)), "tag_already_issued", replacement)
    db.execute("UPDATE tag SET ends = ?, retired_by = ? WHERE tag = ?", (event["at"], event["id"], prior))
    db.execute("INSERT INTO tag VALUES (?,?,?,NULL,?,NULL)", (replacement, animal_id, event["at"], event["id"]))


def candidate(db, tag, observed_at):
    # Use the device time so a delayed upload cannot resolve to a later ear tag.
    row = one(db, "SELECT animal FROM tag WHERE tag = ? AND starts <= ? AND (ends IS NULL OR ? < ends)",
              (tag, observed_at, observed_at))
    return row["animal"] if row else None


def latest_review(db, scan_id):
    return one(db, """SELECT r.* FROM gate_review r WHERE r.scan = ?
                     AND NOT EXISTS (SELECT 1 FROM gate_review n WHERE n.prior = r.id)""", (scan_id,))


def review(db, event):
    scan_id, outcome, operator, reason = (text(event, key) for key in ("scan", "outcome", "operator", "reason"))
    scan = known(db, "SELECT * FROM gate_scan WHERE id = ?", (scan_id,), "unknown_scan")
    previous = latest_review(db, scan_id)
    prior = previous["id"] if previous else None
    require(expected(event, "expected") == prior, "stale_review", scan_id)
    require(event["at"] >= scan["at"], "review_before_scan", scan_id)
    require(outcome in {"confirm", "exclude"}, "review_outcome", outcome)
    subject = mark = None
    if outcome == "confirm":
        mark = text(event, "mark")
        match = known(db, "SELECT * FROM animal WHERE mark = ? AND registered_at <= ?",
                      (mark, scan["at"]), "unknown_handheld_mark")
        subject = match["id"]
        observed = candidate(db, scan["tag"], scan["at"])
        require(observed is None or observed == subject, "tag_mark_disagree", scan_id)
        issued = one(db, "SELECT 1 FROM tag WHERE tag = ?", (scan["tag"],))
        require(not issued or observed is not None, "tag_outside_issue_window", scan_id)
    db.execute("INSERT INTO gate_review VALUES (?,?,?,?,?,?,?,?,?)",
               (event["id"], scan_id, event["at"], prior, outcome, subject, mark, operator, reason))


COMMANDS = {"register-animal": register, "retag": retag, "review-scan": review}
