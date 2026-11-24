import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ear_tag_transfer import ledger, report
from ear_tag_transfer.rules import RuleError

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
SHIFT = ledger.parse_batch(FIXTURES / "auction_shift.json")
LATE = ledger.parse_batch(FIXTURES / "late_bridge.json")
RESOLUTIONS = ledger.parse_batch(FIXTURES / "late_resolutions.json")


def event(kind, key, minute, **fields):
    return {"type": kind, "id": key, "at": f"2026-11-24T10:{minute:02d}:00Z", **fields}


def scan(key, minute, tag, origin, destination):
    return event("gate-scan", key, minute, tag=tag, reader="Crossbar fixture", **{"from": origin, "to": destination})


def review(key, minute, scan_id, *, mark=None, expected=None):
    fields = {"scan": scan_id, "expected": expected, "outcome": "confirm" if mark else "exclude",
              "operator": "Ira", "reason": "Second observation at the crossbar"}
    if mark:
        fields["mark"] = mark
    return event("review-scan", key, minute, **fields)


def seal(key, minute, *, expected="tally-17", scans=("out-1", "out-2"), loads=("DECK-1", "DECK-2")):
    return event("seal-lot", key, minute, lot="LOT-17", expected=expected, scans=list(scans), loads=list(loads))


def close(key, minute, tally="tally-17"):
    return event("close-lot", key, minute, lot="LOT-17", tally=tally, seller="Cedar", buyer="Maple")


def offer(key, minute, animal="A01", seller="Cedar", buyer="Maple"):
    return event("offer-lot", key, minute, lot=key, seller=seller, buyer=buyer, expected_count=1,
                 members=[{"animal": animal, "to": "P-NORTH"}])


def choice(key, minute, ticket, considered, expected="choose-1"):
    return event("choose-ticket", key, minute, load="DECK-1", ticket=ticket, considered=considered,
                 expected=expected, reason="Checked each paper ticket against the deck log")


class AuctionCase(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "shift.sqlite"
        self.db = ledger.connect(self.path, create=True)
        self.addCleanup(self.db.close)
        ledger.initialize(self.db)


    def prefix(self, last="clear-stage"):
        index = next(i for i, row in enumerate(SHIFT) if row["id"] == last)
        return ledger.apply(self.db, SHIFT[:index + 1])


    def apply(self, *events, desk=None):
        return ledger.apply(self.db, events, desk)


    def assert_refused(self, code, *events, desk=None):
        before = report.snapshot(self.db, include_evidence=True)
        with self.assertRaisesRegex(RuleError, code):
            self.apply(*events, desk=desk)
        self.assertEqual(report.snapshot(self.db, include_evidence=True), before)


    def lot(self):
        return report.snapshot(self.db)["lots"][0]


    def pen(self, pen_id="P-STAGE"):
        return next(row for row in report.snapshot(self.db)["pens"] if row["id"] == pen_id)


    def load(self, load_id="DECK-1"):
        return next(row for row in report.snapshot(self.db)["loads"] if row["id"] == load_id)


    def cli(self, command, *args):
        return subprocess.run([sys.executable, "-B", "-m", "ear_tag_transfer", command,
                               "--db", str(self.path), *map(str, args)], capture_output=True, text=True,
                              cwd=FIXTURES.parent, timeout=10, check=False)

