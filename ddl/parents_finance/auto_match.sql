CREATE TABLE IF NOT EXISTS auto_match (
    id serial NOT NULL,
    merchant_name text NOT NULL,
    merchant_category text NOT NULL,
    CONSTRAINT auto_match_pkey PRIMARY KEY (id),
    CONSTRAINT auto_match_merchant_name_merchant_category_key UNIQUE (merchant_name, merchant_category)
);

-- No seed here. These are real bank descriptors and this repository is
-- public. The rows live in the gitignored ddl/seed/parents_finance.auto_match.sql,
-- regenerated from live by scripts/export_ddl_seed.py --database parents_finance.
