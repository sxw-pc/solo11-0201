"""Receipt-ordered journal, transactional command execution and projection recovery."""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from . import identity, sale, weighing, yard
from .rules import canonical, invalid_constant, one, require, strict_object, text, timestamp
from .schema import DOMAIN, JOURNAL, TABLES

VERSION = 1
GENESIS = "0" * 64
RAW = {"gate-scan", "scale-ticket"}
DESKS = {"identity": identity.COMMANDS, "yard": yard.COMMANDS, "weigh": weighing.COMMANDS, "sale": sale.COMMANDS}
COMMANDS = {command: handler for desk in DESKS.values() for command, handler in desk.items()}


def connect(path, *, create=False):
    if path == ":memory:":
        db = sqlite3.connect(path, isolation_level=None)
    else:
        uri = Path(path).resolve().as_uri() + ("?mode=rwc" if create else "?mode=rw")
        db = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


@contextmanager
def transaction(db, *, write=False):
    db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise


def schema(db, script):
    for statement in script.split(";"):
        if statement.strip():
            db.execute(statement)


def version(db):
    return one(db, "PRAGMA user_version")[0]


def check_version(db):
    require(version(db) == VERSION, "database_version", "run init or open a v1 ledger")


def initialize(db):
    with transaction(db, write=True):
        current = version(db)
        require(current in {0, VERSION}, "database_version", str(current))
        if current == 0:
            require(not one(db, "SELECT 1 FROM sqlite_master WHERE type = 'table'"), "database_not_empty", "init")
            schema(db, JOURNAL)
            schema(db, DOMAIN)
            db.execute(f"PRAGMA user_version = {VERSION}")


def parse_batch(path):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"), object_pairs_hook=strict_object,
                      parse_constant=invalid_constant)
    require(isinstance(data, list) and data and all(isinstance(row, dict) for row in data), "input_batch", str(path))
    return data


def event_type(event, desk=None):
    text(event, "id")
    timestamp(event)
    kind = text(event, "type")
    require(kind in COMMANDS, "unknown_event_type", kind)
    require(desk is None or kind in DESKS[desk], "wrong_desk", kind)
    return kind


def digest(previous, body):
    return hashlib.sha256((previous + "\n" + body).encode("utf-8")).hexdigest()


def business_clock(db):
    for row in db.execute("SELECT body FROM journal ORDER BY seq DESC"):
        event = json.loads(row["body"])
        if event["type"] not in RAW:
            return event["at"]
    return ""


def execute(db, event, kind, clock):
    if kind not in RAW:
        require(event["at"] > clock, "business_clock", f"{event['id']} must follow {clock}")
    COMMANDS[kind](db, event)
    return clock if kind in RAW else event["at"]


def apply(db, events, desk=None):
    result = {"applied": [], "duplicates": []}
    with transaction(db, write=True):
        check_version(db)
        head = one(db, "SELECT digest FROM journal ORDER BY seq DESC LIMIT 1")
        previous = head[0] if head else GENESIS
        clock = business_clock(db)
        for event in events:
            kind = event_type(event, desk)
            event_id, body = event["id"], canonical(event)
            existing = one(db, "SELECT body FROM journal WHERE event_id = ?", (event_id,))
            if existing:
                require(existing["body"] == body, "event_id_conflict", event_id)
                result["duplicates"].append(event_id)
                continue
            clock = execute(db, event, kind, clock)
            checksum = digest(previous, body)
            db.execute("INSERT INTO journal(event_id, body, previous, digest) VALUES (?,?,?,?)",
                       (event_id, body, previous, checksum))
            previous = checksum
            result["applied"].append(event_id)
    return result


def rebuild(db, target):
    schema(target, DOMAIN)
    previous, clock, count = GENESIS, "", 0
    for row in db.execute("SELECT * FROM journal ORDER BY seq"):
        count += 1
        body = row["body"]
        require(row["seq"] == count and row["previous"] == previous and row["digest"] == digest(previous, body),
                "journal_integrity", f"receipt {count}")
        event = json.loads(body)
        require(canonical(event) == body and event["id"] == row["event_id"], "journal_identity", str(count))
        clock = execute(target, event, event_type(event), clock)
        previous = row["digest"]
    return count, previous


def rows(db, table):
    return db.execute(f"SELECT * FROM {table}").fetchall()


def signatures(db, table):
    if not one(db, "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)):
        return None
    return sorted(canonical(dict(row)) for row in rows(db, table))


def replace_domain(db, target):
    for table in reversed(TABLES):
        db.execute(f"DROP TABLE IF EXISTS {table}")
    schema(db, DOMAIN)
    for table in TABLES:
        data = target.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        if data:
            placeholders = ",".join("?" for field in data[0].keys())
            db.executemany(f"INSERT INTO {table} VALUES ({placeholders})", data)


def replay(db, *, repair=False):
    target = connect(":memory:")
    try:
        with transaction(db, write=repair):
            check_version(db)
            count, head = rebuild(db, target)
            changed = [table for table in TABLES if signatures(db, table) != signatures(target, table)]
            if repair and changed:
                replace_domain(db, target)
            return {"events": count, "journal_head": head, "differences": changed,
                    "repaired": bool(repair and changed)}
    finally:
        target.close()
