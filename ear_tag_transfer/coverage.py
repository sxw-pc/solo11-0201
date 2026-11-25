"""Reader session evidence, sequence-gap feasibility and lane coverage.

Device fragments arrive as journaled receipts.  A reader serves one directed
lane (origin -> destination) during each registered half-open time segment and
produces nothing outside its segments.  Reads inside one session are numbered
with the device's own monotonic sequence; trusted anchors (the first and last
known sequence) delimit the gaps.  A separately stored clock head may bound the
second range in which one missing sequence could have occurred.

Every feasible placement of the missing sequences must simultaneously satisfy
sequence order, the adjacent known occurrence times, the clock heads, the lane
segments and a one-second spacing rule: two missing sequences can never reuse
the same occurrence second.
"""

import json

from .rules import integer, known, one, require, text


def to_seconds(value):
    year, rest = value.split("-", 1)
    month, rest = rest.split("-", 1)
    day, rest = rest.split("T", 1)
    hour, rest = rest.split(":", 1)
    minute, second = rest.rstrip("Z").split(":", 1)
    days = _days_before(int(year), int(month), int(day))
    return (((days * 24 + int(hour)) * 60 + int(minute)) * 60 + int(second))


def from_seconds(value):
    second = value % 60
    value //= 60
    minute = value % 60
    value //= 60
    hour = value % 24
    days = value // 24
    year, month, day = _date_from_days(days)
    return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}Z"


def _is_leap(year):
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _days_before(year, month, day):
    lengths = [31, 29 if _is_leap(year) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return (year - 1) * 365 + (year - 1) // 4 - (year - 1) // 100 + (year - 1) // 400 \
        + sum(lengths[:month - 1]) + day - 1


def _date_from_days(days):
    year = 1
    while True:
        span = 366 if _is_leap(year) else 365
        if days < span:
            break
        days -= span
        year += 1
    lengths = [31, 29 if _is_leap(year) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    month = 1
    for length in lengths:
        if days < length:
            break
        days -= length
        month += 1
    return year, month, days + 1


def _stamp(event, key):
    value = text(event, key)
    require(from_seconds(to_seconds(value)) == value, "time", f"expected canonical UTC seconds: {key}")
    return value


def open_session(db, event):
    reader, session_id = text(event, "reader"), text(event, "session")
    at = _stamp(event, "at")
    require(not one(db, "SELECT 1 FROM reader_session WHERE reader = ? AND session = ?", (reader, session_id)),
            "reader_session_in_use", f"{reader} / {session_id}")
    db.execute("INSERT INTO reader_session VALUES (?,?,?,?)", (reader, session_id, at, event["id"]))


def register_lane(db, event):
    reader, session_id = (text(event, key) for key in ("reader", "session"))
    origin, destination = text(event, "from"), text(event, "to")
    starts_at, ends_at = _stamp(event, "starts_at"), _stamp(event, "ends_at")
    known(db, "SELECT 1 FROM reader_session WHERE reader = ? AND session = ?", (reader, session_id),
          f"unknown_reader_session: {reader} / {session_id}")
    require(origin != destination, "same_pen", origin)
    require(to_seconds(starts_at) < to_seconds(ends_at), "lane_segment_order", event["id"])
    overlap = one(db, """SELECT 1 FROM reader_lane WHERE reader = ? AND session = ?
                         AND starts_at < ? AND ? < ends_at""",
                  (reader, session_id, ends_at, starts_at))
    require(not overlap, "lane_segment_overlap", event["id"])
    db.execute("INSERT INTO reader_lane VALUES (?,?,?,?,?,?,?)",
               (event["id"], reader, session_id, origin, destination, starts_at, ends_at))


def register_clock_head(db, event):
    reader, session_id = (text(event, key) for key in ("reader", "session"))
    seq = integer(event, "seq", 0)
    earliest_at, latest_at = _stamp(event, "earliest_at"), _stamp(event, "latest_at")
    known(db, "SELECT 1 FROM reader_session WHERE reader = ? AND session = ?", (reader, session_id),
          f"unknown_reader_session: {reader} / {session_id}")
    require(to_seconds(earliest_at) <= to_seconds(latest_at), "clock_head_order", event["id"])
    require(not one(db, "SELECT 1 FROM reader_clock_head WHERE reader = ? AND session = ? AND seq = ?",
                    (reader, session_id, seq)), "clock_head_in_use", str(seq))
    db.execute("INSERT INTO reader_clock_head VALUES (?,?,?,?,?,?)",
               (event["id"], reader, session_id, seq, earliest_at, latest_at))


def lane_at(db, reader, session_id, second):
    return one(db, """SELECT * FROM reader_lane WHERE reader = ? AND session = ?
                      AND starts_at <= ? AND ? < ends_at""",
               (reader, session_id, from_seconds(second), from_seconds(second)))


def validate_session_scan(db, reader, session_id, seq, at):
    prior = one(db, "SELECT at FROM gate_scan WHERE reader = ? AND session = ? AND seq = ?",
                (reader, session_id, seq - 1))
    following = one(db, """SELECT at FROM gate_scan WHERE reader = ? AND session = ? AND seq = ?""",
                    (reader, session_id, seq + 1))
    second = to_seconds(at)
    if prior:
        require(second > to_seconds(prior["at"]), "session_sequence_order", f"{session_id} / {seq}")
    if following:
        require(second < to_seconds(following["at"]), "session_sequence_order", f"{session_id} / {seq}")
    lane = lane_at(db, reader, session_id, second)
    require(lane is not None, "scan_outside_lane_segment", f"{session_id} / {seq}")
    return lane


def analyze_session(db, reader, session_id):
    """Return known reads, missing sequences and every joint feasible placement."""
    rows = db.execute("SELECT * FROM gate_scan WHERE reader = ? AND session = ? ORDER BY seq",
                      (reader, session_id)).fetchall()
    result = {"reader": reader, "session": session_id, "reads": [dict(row) for row in rows],
              "missing": [], "placements": [], "conflict": None}
    if not rows:
        return result
    seqs = [row["seq"] for row in rows]
    require(seqs == sorted(set(seqs)), "duplicate_session_sequence", session_id)
    known_at = {row["seq"]: to_seconds(row["at"]) for row in rows}
    heads = {row["seq"]: (to_seconds(row["earliest_at"]), to_seconds(row["latest_at"]))
             for row in db.execute("SELECT * FROM reader_clock_head WHERE reader = ? AND session = ?",
                                   (reader, session_id))}
    first, last = seqs[0], seqs[-1]
    missing = [seq for seq in range(first, last + 1) if seq not in known_at]
    result["missing"] = missing

    def options(seq):
        prev_seq = max(candidate for candidate in known_at if candidate < seq)
        next_seq = min(candidate for candidate in known_at if candidate > seq)
        lower, upper = known_at[prev_seq] + 1, known_at[next_seq] - 1
        if seq in heads:
            lower = max(lower, heads[seq][0])
            upper = min(upper, heads[seq][1])
        choices = []
        for second in range(lower, upper + 1):
            lane = lane_at(db, reader, session_id, second)
            if lane:
                choices.append((second, lane["origin"], lane["destination"]))
        return choices

    runs, current = [], []
    for seq in missing:
        if current and seq == current[-1] + 1:
            current.append(seq)
        else:
            if current:
                runs.append(current)
            current = [seq]
    if current:
        runs.append(current)

    placements = [{}]
    for run in runs:
        extended = []
        run_options = {seq: options(seq) for seq in run}
        if any(not run_options[seq] for seq in run):
            result["conflict"] = "device_evidence_conflict"
            result["placements"] = []
            return result

        def extend(index, assigned, used):
            if index == len(run):
                for base in placements:
                    extended.append({**base, **assigned})
                return
            seq = run[index]
            previous_second = assigned[run[index - 1]][0] if index else None
            for second, origin, destination in run_options[seq]:
                if second not in used and (previous_second is None or second > previous_second):
                    assigned[seq] = (second, origin, destination)
                    used.add(second)
                    extend(index + 1, assigned, used)
                    used.remove(second)
                    del assigned[seq]

        extend(0, {}, set())
        placements = extended
        if not placements:
            result["conflict"] = "device_evidence_conflict"
            result["placements"] = []
            return result
    result["placements"] = placements
    return result


def _sessions_for_pen(db, pen_id):
    rows = db.execute("""SELECT DISTINCT reader, session FROM reader_lane
                         WHERE origin = ? OR destination = ? ORDER BY reader, session""", (pen_id, pen_id))
    return [(row["reader"], row["session"]) for row in rows]


def _backup_covers(db, reader, session_id, origin, destination, second):
    """A complete, gap-free original range covering the second on this lane."""
    reads = db.execute("SELECT seq, at FROM gate_scan WHERE reader = ? AND session = ? ORDER BY seq",
                       (reader, session_id)).fetchall()
    if not reads:
        return False
    seqs = [row["seq"] for row in reads]
    if seqs != list(range(seqs[0], seqs[-1] + 1)):
        return False
    seconds = [to_seconds(row["at"]) for row in reads]
    if any(seconds[index] <= seconds[index - 1] for index in range(1, len(seconds))):
        return False
    if not (seconds[0] <= second <= seconds[-1]):
        return False
    lane = lane_at(db, reader, session_id, second)
    return bool(lane and lane["origin"] == origin and lane["destination"] == destination)


def backup_reader_covers(db, backup_reader, origin, destination, seconds):
    """Every second is covered by one complete same-lane range of the backup."""
    sessions = [row["session"] for row in db.execute(
        "SELECT DISTINCT session FROM reader_session WHERE reader = ? ORDER BY session", (backup_reader,))]
    return all(any(_backup_covers(db, backup_reader, session_id, origin, destination, second)
                   for session_id in sessions) for second in seconds)


def pen_coverage(db, target, through=None):
    """Shared gate-scan coverage conclusion used by clear() and pen_report()."""
    pen_id = target["id"]
    cutoff = None if through is None else to_seconds(through)
    sessions, blockers, conflicts, redundant = [], [], [], []
    for reader, session_id in _sessions_for_pen(db, pen_id):
        analysis = analyze_session(db, reader, session_id)
        if analysis["conflict"]:
            conflicts.append({"reader": reader, "session": session_id, "reason": analysis["conflict"]})
            continue
        sessions.append({"reader": reader, "session": session_id,
                         "missing_seqs": analysis["missing"],
                         "feasible_seconds": {
                             seq: sorted({placement[seq][0] for placement in analysis["placements"]})
                             for seq in analysis["missing"]},
                         "feasible_at": {
                             seq: [from_seconds(second) for second in sorted(
                                 {placement[seq][0] for placement in analysis["placements"]})]
                             for seq in analysis["missing"]}})
        for seq in analysis["missing"]:
            blocking = []
            for placement in analysis["placements"]:
                second, origin, destination = placement[seq]
                if pen_id in (origin, destination) and (cutoff is None or second <= cutoff):
                    blocking.append({"second": second, "at": from_seconds(second),
                                     "origin": origin, "destination": destination})
            if not blocking:
                continue
            # A gap may be feasible on more than one lane touching this pen. A
            # redundant grant only neutralizes it when every blocking second is
            # covered on that second's own directed lane.
            covered_seconds, covering_reader = set(), None
            grants = db.execute("""SELECT * FROM coverage_authorization
                                   WHERE pen = ? AND source_reader = ? ORDER BY event""",
                                (pen_id, reader)).fetchall()
            for grant in grants:
                seconds_on_lane = sorted({item["second"] for item in blocking
                                          if item["origin"] == grant["origin"]
                                          and item["destination"] == grant["destination"]})
                if seconds_on_lane and backup_reader_covers(
                        db, grant["backup_reader"], grant["origin"], grant["destination"], seconds_on_lane):
                    covered_seconds.update(seconds_on_lane)
                    covering_reader = grant["backup_reader"]
            blocking_seconds = sorted({item["second"] for item in blocking})
            fully_covered = covered_seconds == set(blocking_seconds)
            gap = {"reader": reader, "session": session_id, "seq": seq,
                   "blocking_seconds": blocking_seconds,
                   "blocking_at": [from_seconds(second) for second in blocking_seconds],
                   "blocking_lanes": sorted({(item["origin"], item["destination"]) for item in blocking}),
                   "redundant": fully_covered,
                   "backup_reader": covering_reader if fully_covered else None}
            (redundant if fully_covered else blockers).append(gap)
    authorizations = [dict(row) for row in db.execute(
        "SELECT * FROM coverage_authorization WHERE pen = ? ORDER BY event", (pen_id,))]
    frozen = []
    if target["clear_event"]:
        frozen = [dict(row, covered_seqs=json.loads(row["covered_seqs"])) for row in db.execute(
            "SELECT * FROM clearance_evidence WHERE clear_event = ? ORDER BY reader, session",
            (target["clear_event"],))]
    return {"through": through, "sessions": sessions, "blocking_gaps": blockers,
            "evidence_conflicts": conflicts, "redundant_gaps": redundant,
            "authorizations": authorizations, "clearance_evidence": frozen}


def authorize(db, event):
    pen_id = text(event, "pen")
    origin, destination = text(event, "from"), text(event, "to")
    source_reader, backup_reader = text(event, "source_reader"), text(event, "backup_reader")
    target = known(db, "SELECT * FROM pen WHERE id = ?", (pen_id,), f"unknown_pen: {pen_id}")
    require(origin != destination and pen_id in (origin, destination), "authorization_route", pen_id)
    require(source_reader != backup_reader, "backup_reader_independent", backup_reader)
    require(one(db, "SELECT 1 FROM reader_session WHERE reader = ?", (source_reader,)),
            "unknown_reader_session", source_reader)
    require(one(db, "SELECT 1 FROM reader_session WHERE reader = ?", (backup_reader,)),
            "unknown_reader_session", backup_reader)
    require(not one(db, """SELECT 1 FROM coverage_authorization
                           WHERE pen = ? AND origin = ? AND destination = ?
                           AND source_reader = ? AND backup_reader = ?""",
                    (pen_id, origin, destination, source_reader, backup_reader)),
            "authorization_in_use", event["id"])
    db.execute("INSERT INTO coverage_authorization VALUES (?,?,?,?,?,?)",
               (event["id"], pen_id, origin, destination, source_reader, backup_reader))


def freeze_evidence(db, clear_event, target, through, conclusion):
    grouped = {}
    for gap in conclusion["redundant_gaps"]:
        for origin, destination in gap["blocking_lanes"]:
            bucket = grouped.setdefault((gap["reader"], gap["session"], origin, destination), set())
            bucket.add(gap["seq"])
    for (reader, session_id, origin, destination), seqs in sorted(grouped.items()):
        db.execute("INSERT INTO clearance_evidence VALUES (?,?,?,?,?,?,?)",
                   (clear_event, reader, session_id, origin, destination,
                    json.dumps(sorted(seqs), separators=(",", ":")), "redundant_reader_coverage"))
