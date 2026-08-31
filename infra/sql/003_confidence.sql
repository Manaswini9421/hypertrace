-- Migration 003: record how confident the detector was, and how mature the
-- baseline it judged against.
--
-- Dossier §24.3 makes authority a function of confidence and only of
-- confidence: >=0.85 may act autonomously, >=0.60 must ask a human, below
-- that it may only alert. Storing the score is what makes that decision
-- auditable after the fact rather than a claim about what the code did.
BEGIN;

ALTER TABLE anomalies
    ADD COLUMN IF NOT EXISTS confidence        REAL
        CHECK (confidence IS NULL OR (confidence BETWEEN 0 AND 1)),
    ADD COLUMN IF NOT EXISTS baseline_mature   BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS cost_delta_usd_hr DOUBLE PRECISION;

COMMIT;
