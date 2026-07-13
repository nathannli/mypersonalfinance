CREATE TABLE expenses (
    id serial NOT NULL,
    date date NOT NULL,
    merchant text NOT NULL,
    category_id integer NOT NULL,
    subcategory_id integer NOT NULL,
    cost numeric(10,2) NOT NULL,
    source text,
    source_account_id text,
    source_transaction_id text,
    CONSTRAINT expenses_pkey PRIMARY KEY (id),
    CONSTRAINT expenses_source_identity_check CHECK (
        (source IS NULL AND source_account_id IS NULL AND source_transaction_id IS NULL)
        OR (source IS NOT NULL AND source_account_id IS NOT NULL AND source_transaction_id IS NOT NULL)
    ),
    CONSTRAINT expenses_category_id_fkey FOREIGN KEY (category_id) REFERENCES categories (id),
    CONSTRAINT expenses_subcategory_id_fkey FOREIGN KEY (subcategory_id) REFERENCES subcategories (id)
);

ALTER TABLE expenses ADD COLUMN comments TEXT;

CREATE UNIQUE INDEX expenses_legacy_date_merchant_cost_key
    ON expenses (date, merchant, cost)
    WHERE source IS NULL;

CREATE UNIQUE INDEX expenses_source_account_transaction_key
    ON expenses (source, source_account_id, source_transaction_id)
    WHERE source IS NOT NULL;
