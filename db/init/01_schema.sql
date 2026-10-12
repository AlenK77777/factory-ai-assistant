-- Schema for the Factory AI Assistant (synthetic data only).
-- Runs automatically on the first start of the db container.

-- pgvector: needed later for document embeddings (RAG).
CREATE EXTENSION IF NOT EXISTS vector;

-- One row per production order.
CREATE TABLE IF NOT EXISTS orders (
    order_id        TEXT PRIMARY KEY,
    customer_sector TEXT    NOT NULL,
    product_type    TEXT    NOT NULL,
    layers          INTEGER NOT NULL CHECK (layers BETWEEN 2 AND 16),
    quantity        INTEGER NOT NULL CHECK (quantity > 0),
    status          TEXT    NOT NULL,
    production_line TEXT    NOT NULL,
    created_date    DATE    NOT NULL,
    due_date        DATE    NOT NULL,
    CHECK (due_date >= created_date)
);

-- One row per defect finding; every defect belongs to an existing order.
CREATE TABLE IF NOT EXISTS defects (
    defect_id     TEXT PRIMARY KEY,
    order_id      TEXT    NOT NULL REFERENCES orders (order_id),
    detected_date DATE    NOT NULL,
    station       TEXT    NOT NULL,
    defect_type   TEXT    NOT NULL,
    quantity      INTEGER NOT NULL CHECK (quantity > 0)
);

CREATE INDEX IF NOT EXISTS defects_order_id_idx ON defects (order_id);
CREATE INDEX IF NOT EXISTS defects_detected_date_idx ON defects (detected_date);

-- One row per file per loader run (an audit log, so it grows on every run).
CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id        INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    file_name     TEXT        NOT NULL,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    rows_loaded   INTEGER     NOT NULL,
    rows_rejected INTEGER     NOT NULL
);
