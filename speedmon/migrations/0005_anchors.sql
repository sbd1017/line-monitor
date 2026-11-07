-- External timestamps over the ledger head (see speedmon/anchors.py).
--
-- An anchor is deliberately NOT part of the evidence chain: it points at a chain position, so chaining it back
-- into the ledger would be circular. It sits beside the chain and is verified against it.
--
-- What each row proves: a third party signed "at time T the chain head was H at index N". Anyone holding the
-- receipt can check that the head hash of the export they were given still resolves to N, so records inserted
-- or rewritten after T break it. This is the part the chain alone cannot do, because a chain is only strong
-- against someone who could rewrite the whole database.
--
-- Receipts are opaque DER from an RFC 3161 authority. Verification never trusts the row: it re-reads the
-- statement, re-checks its hash and the chain position, and re-verifies the signature.

CREATE TABLE anchors (
    id                INTEGER PRIMARY KEY,
    head_index        INTEGER NOT NULL,          -- ledger position the statement names
    head_hash         TEXT NOT NULL,             -- ... and the head value in it
    provider          TEXT NOT NULL,             -- TSA name from anchors.PROVIDERS
    statement         TEXT NOT NULL,             -- exact bytes the authority signed
    statement_sha256  TEXT NOT NULL,
    token             BLOB,                      -- DER TimeStampToken, null when status = 'error'
    status            TEXT NOT NULL,             -- 'ok' | 'error'
    error             TEXT,
    created_at        TEXT NOT NULL
);

CREATE INDEX anchors_by_provider ON anchors (provider, id);
CREATE INDEX anchors_by_head ON anchors (head_index);

CREATE TABLE anchor_checks (
    id         INTEGER PRIMARY KEY,
    anchor_id  INTEGER NOT NULL REFERENCES anchors(id),
    checked_at TEXT NOT NULL,
    ok         INTEGER NOT NULL,
    problem    TEXT
);

CREATE INDEX anchor_checks_recent ON anchor_checks (checked_at);

CREATE TRIGGER anchors_append_only_u BEFORE UPDATE ON anchors BEGIN SELECT RAISE(ABORT, 'evidence is append-only'); END;
CREATE TRIGGER anchors_append_only_d BEFORE DELETE ON anchors BEGIN SELECT RAISE(ABORT, 'evidence is append-only'); END;
