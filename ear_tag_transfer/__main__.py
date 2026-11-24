"""Command-line entry point for the local auction shift."""

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from . import ledger, report
from .rules import RuleError

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def parser():
    main = argparse.ArgumentParser(description=__doc__)
    commands = main.add_subparsers(dest="command", required=True)
    for key, help_text in (
        ("init", "create an empty v1 shift"), ("identity", "register, replace tags or review scans"),
        ("yard", "open pens, record boarding contracts and reconcile crossings"),
        ("weigh", "record a deck load, its tickets or the scale clerk's choice"),
        ("sale", "reserve, count, cancel or close a lot"), ("ingest", "apply a mixed-desk event file"),
        ("report", "show live allocations, frozen weights and handover exceptions"),
        ("replay", "compare or restore projections from the receipt journal"), ("demo", "run the synthetic fixture"),
    ):
        command = commands.add_parser(key, help=help_text)
        command.add_argument("--db", type=Path, required=key != "demo")
        if key in ledger.DESKS or key == "ingest":
            command.add_argument("--input", type=Path, required=True)
        if key == "report":
            command.add_argument("--evidence", action="store_true")
        if key == "replay":
            command.add_argument("--repair", action="store_true")
    return main


def demo(path):
    with closing(ledger.connect(path, create=True)) as db:
        ledger.initialize(db)
        imported = ledger.apply(db, ledger.parse_batch(FIXTURES / "auction_shift.json"))
        state = report.snapshot(db)
        return {"applied": len(imported["applied"]), "duplicates": len(imported["duplicates"]),
                "pens": {row["id"]: {"state": row["state"], "occupants": row["occupants"]} for row in state["pens"]},
                "lot": state["lots"][0], "replay": ledger.replay(db)}


def run(args):
    if args.command == "demo":
        if args.db is not None:
            return demo(args.db), 0
        with TemporaryDirectory(prefix="ear-tag-transfer-") as directory:
            return demo(Path(directory) / "shift.sqlite"), 0
    with closing(ledger.connect(args.db, create=args.command == "init")) as db:
        if args.command == "init":
            ledger.initialize(db)
            return {"schema": ledger.VERSION}, 0
        if args.command == "report":
            return report.snapshot(db, include_evidence=args.evidence), 0
        if args.command == "replay":
            result = ledger.replay(db, repair=args.repair)
            return result, 1 if result["differences"] and not result["repaired"] else 0
        desk = None if args.command == "ingest" else args.command
        return ledger.apply(db, ledger.parse_batch(args.input), desk), 0


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result, code = run(args)
    except (RuleError, sqlite3.Error, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
