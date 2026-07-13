ALTER TABLE expenses ADD COLUMN IF NOT EXISTS source text;
ALTER TABLE expenses ADD COLUMN IF NOT EXISTS source_account_id text;
ALTER TABLE expenses ADD COLUMN IF NOT EXISTS source_transaction_id text;

CREATE UNIQUE INDEX IF NOT EXISTS expenses_legacy_date_merchant_cost_key
    ON expenses (date, merchant, cost)
    WHERE source IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS expenses_source_account_transaction_key
    ON expenses (source, source_account_id, source_transaction_id)
    WHERE source IS NOT NULL;

ALTER TABLE expenses DROP CONSTRAINT IF EXISTS expenses_date_merchant_cost_key;

ALTER TABLE expenses ADD CONSTRAINT expenses_source_identity_check CHECK (
    (source IS NULL AND source_account_id IS NULL AND source_transaction_id IS NULL)
    OR (source IS NOT NULL AND source_account_id IS NOT NULL AND source_transaction_id IS NOT NULL)
);
