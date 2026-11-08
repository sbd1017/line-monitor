-- The ledger's stream CHECK predates the 'path' stream and SQLite cannot alter a constraint, so rebuild the table.
-- Rows are copied, not recomputed: chain_index, prev_hash and chain_hash keep their exact values, so the existing
-- chain and every earlier head stay valid.

DROP TRIGGER IF EXISTS ledger_append_only_u;
DROP TRIGGER IF EXISTS ledger_append_only_d;

CREATE TABLE ledger_new (
    chain_index  INTEGER PRIMARY KEY,
    stream       TEXT NOT NULL CHECK (stream IN ('measurement', 'diagnostic', 'probe', 'event', 'path')),
    uuid         TEXT NOT NULL,
    record_hash  TEXT NOT NULL,
    prev_hash    TEXT NOT NULL,
    chain_hash   TEXT NOT NULL,
    hash_version INTEGER NOT NULL,
    recorded_at  TEXT NOT NULL,
    UNIQUE (stream, uuid)
);

INSERT INTO ledger_new (chain_index, stream, uuid, record_hash, prev_hash, chain_hash, hash_version, recorded_at)
    SELECT chain_index, stream, uuid, record_hash, prev_hash, chain_hash, hash_version, recorded_at FROM ledger;

DROP TABLE ledger;
ALTER TABLE ledger_new RENAME TO ledger;

CREATE TRIGGER ledger_append_only_u BEFORE UPDATE ON ledger BEGIN SELECT RAISE(ABORT, 'evidence is append-only'); END;
CREATE TRIGGER ledger_append_only_d BEFORE DELETE ON ledger BEGIN SELECT RAISE(ABORT, 'evidence is append-only'); END;
