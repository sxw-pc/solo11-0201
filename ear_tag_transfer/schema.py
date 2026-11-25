"""SQLite schema. Domain projections are reproducible from the journal."""

JOURNAL = """
CREATE TABLE IF NOT EXISTS journal (
    seq INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    body TEXT NOT NULL,
    previous TEXT NOT NULL,
    digest TEXT NOT NULL
);
"""

DOMAIN = """
CREATE TABLE animal (
    id TEXT PRIMARY KEY, mark TEXT NOT NULL UNIQUE, owner TEXT NOT NULL,
    owner_version TEXT NOT NULL, registered_at TEXT NOT NULL
);
CREATE TABLE tag (
    tag TEXT PRIMARY KEY, animal TEXT NOT NULL REFERENCES animal(id),
    starts TEXT NOT NULL, ends TEXT, event TEXT NOT NULL, retired_by TEXT
);
CREATE UNIQUE INDEX one_active_tag ON tag(animal) WHERE ends IS NULL;
CREATE TABLE pen (
    id TEXT PRIMARY KEY, keeper TEXT NOT NULL, opened_at TEXT NOT NULL,
    drain_at TEXT, drain_event TEXT, cleared_at TEXT, clear_event TEXT
);
CREATE TABLE pen_contract (
    event TEXT PRIMARY KEY, pen TEXT NOT NULL REFERENCES pen(id), owner TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0,1)), prior TEXT REFERENCES pen_contract(event)
);
CREATE TABLE gate_scan (
    id TEXT PRIMARY KEY, at TEXT NOT NULL, tag TEXT NOT NULL, reader TEXT NOT NULL,
    origin TEXT NOT NULL, destination TEXT NOT NULL,
    session TEXT, seq INTEGER
);
CREATE TABLE reader_session (
    reader TEXT NOT NULL, session TEXT NOT NULL, opened_at TEXT NOT NULL, event TEXT NOT NULL,
    PRIMARY KEY(reader, session)
);
CREATE TABLE reader_lane (
    event TEXT PRIMARY KEY, reader TEXT NOT NULL, session TEXT NOT NULL,
    origin TEXT NOT NULL, destination TEXT NOT NULL, starts_at TEXT NOT NULL, ends_at TEXT NOT NULL
);
CREATE TABLE reader_clock_head (
    event TEXT PRIMARY KEY, reader TEXT NOT NULL, session TEXT NOT NULL,
    seq INTEGER NOT NULL, earliest_at TEXT NOT NULL, latest_at TEXT NOT NULL
);
CREATE UNIQUE INDEX one_clock_head ON reader_clock_head(reader, session, seq);
CREATE TABLE coverage_authorization (
    event TEXT PRIMARY KEY, pen TEXT NOT NULL REFERENCES pen(id),
    origin TEXT NOT NULL, destination TEXT NOT NULL,
    source_reader TEXT NOT NULL, backup_reader TEXT NOT NULL
);
CREATE TABLE clearance_evidence (
    clear_event TEXT NOT NULL, reader TEXT NOT NULL, session TEXT NOT NULL,
    origin TEXT NOT NULL, destination TEXT NOT NULL,
    covered_seqs TEXT NOT NULL, basis TEXT NOT NULL,
    PRIMARY KEY(clear_event, reader, session, origin, destination)
);
CREATE TABLE gate_review (
    id TEXT PRIMARY KEY, scan TEXT NOT NULL REFERENCES gate_scan(id), at TEXT NOT NULL,
    prior TEXT REFERENCES gate_review(id), outcome TEXT NOT NULL CHECK(outcome IN ('confirm','exclude')),
    animal TEXT REFERENCES animal(id), mark TEXT, operator TEXT NOT NULL, reason TEXT NOT NULL
);
CREATE UNIQUE INDEX one_review_root ON gate_review(scan) WHERE prior IS NULL;
CREATE UNIQUE INDEX one_review_successor ON gate_review(prior) WHERE prior IS NOT NULL;
CREATE TABLE scale_load (id TEXT PRIMARY KEY, opened_at TEXT NOT NULL, deck TEXT NOT NULL);
CREATE TABLE load_member (
    load TEXT NOT NULL REFERENCES scale_load(id), animal TEXT NOT NULL REFERENCES animal(id),
    PRIMARY KEY(load, animal)
);
CREATE TABLE scale_ticket (
    id TEXT PRIMARY KEY, load TEXT NOT NULL REFERENCES scale_load(id), deck TEXT NOT NULL, receipt TEXT NOT NULL,
    at TEXT NOT NULL, gross_g INTEGER NOT NULL, tare_g INTEGER NOT NULL CHECK(tare_g < gross_g),
    UNIQUE(deck, receipt)
);
CREATE TABLE scale_choice (
    id TEXT PRIMARY KEY, load TEXT NOT NULL REFERENCES scale_load(id),
    ticket TEXT NOT NULL REFERENCES scale_ticket(id), prior TEXT REFERENCES scale_choice(id),
    reason TEXT NOT NULL
);
CREATE TABLE resolved_ticket (
    ticket TEXT PRIMARY KEY REFERENCES scale_ticket(id), choice TEXT NOT NULL REFERENCES scale_choice(id)
);
CREATE TABLE auction_lot (
    id TEXT PRIMARY KEY, seller TEXT NOT NULL, buyer TEXT NOT NULL, expected_count INTEGER NOT NULL,
    cancelled_by TEXT, closed_by TEXT
);
CREATE TABLE nomination (
    lot TEXT NOT NULL REFERENCES auction_lot(id), animal TEXT NOT NULL REFERENCES animal(id),
    destination TEXT NOT NULL REFERENCES pen(id), owner_version TEXT NOT NULL,
    PRIMARY KEY(lot, animal)
);
CREATE TABLE reservation (
    animal TEXT PRIMARY KEY REFERENCES animal(id), lot TEXT NOT NULL REFERENCES auction_lot(id)
);
CREATE TABLE tally (
    id TEXT PRIMARY KEY, lot TEXT NOT NULL REFERENCES auction_lot(id), prior TEXT REFERENCES tally(id),
    counted_at TEXT NOT NULL, net_g INTEGER NOT NULL
);
CREATE UNIQUE INDEX one_tally_root ON tally(lot) WHERE prior IS NULL;
CREATE UNIQUE INDEX one_tally_successor ON tally(prior) WHERE prior IS NOT NULL;
CREATE TABLE tally_member (
    tally TEXT NOT NULL REFERENCES tally(id), animal TEXT NOT NULL REFERENCES animal(id),
    scan TEXT NOT NULL REFERENCES gate_scan(id), review TEXT NOT NULL REFERENCES gate_review(id),
    origin TEXT NOT NULL, destination TEXT NOT NULL REFERENCES pen(id),
    contract TEXT NOT NULL REFERENCES pen_contract(event), origin_move TEXT NOT NULL,
    PRIMARY KEY(tally, animal), UNIQUE(tally, scan)
);
CREATE TABLE tally_weight (
    tally TEXT NOT NULL REFERENCES tally(id), load TEXT NOT NULL REFERENCES scale_load(id),
    choice TEXT NOT NULL REFERENCES scale_choice(id), ticket TEXT NOT NULL REFERENCES scale_ticket(id),
    destination TEXT NOT NULL REFERENCES pen(id), net_g INTEGER NOT NULL,
    PRIMARY KEY(tally, load)
);
CREATE TABLE movement (
    scan TEXT PRIMARY KEY REFERENCES gate_scan(id), event TEXT NOT NULL,
    review TEXT NOT NULL REFERENCES gate_review(id), animal TEXT NOT NULL REFERENCES animal(id),
    origin TEXT NOT NULL, destination TEXT NOT NULL, at TEXT NOT NULL,
    lot TEXT REFERENCES auction_lot(id)
);
CREATE TABLE position (
    animal TEXT PRIMARY KEY REFERENCES animal(id), pen TEXT NOT NULL,
    scan TEXT NOT NULL REFERENCES movement(scan)
);
CREATE TABLE handover (
    animal TEXT PRIMARY KEY REFERENCES animal(id), lot TEXT NOT NULL REFERENCES auction_lot(id),
    tally TEXT NOT NULL REFERENCES tally(id), at TEXT NOT NULL, seller TEXT NOT NULL,
    buyer TEXT NOT NULL, keeper TEXT NOT NULL, destination TEXT NOT NULL REFERENCES pen(id),
    scan TEXT NOT NULL REFERENCES movement(scan), previous_title TEXT NOT NULL
);
"""

TABLES = tuple(line.split()[2] for line in DOMAIN.splitlines() if line.startswith("CREATE TABLE "))
