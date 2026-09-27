CREATE TABLE IF NOT EXISTS main_category (
    id serial NOT NULL,
    name character varying NOT NULL,
    CONSTRAINT main_category_pk PRIMARY KEY (id),
    CONSTRAINT main_category_name_key UNIQUE (name)
);

-- The id is the whole point of this table. parents_finance inverts
-- finance.expense_type: here 1 is Fixed and 2 is Variable. Copying a row
-- across the two databases silently mis-buckets every expense.
INSERT INTO main_category (id, name) VALUES (1, 'Fixed') ON CONFLICT DO NOTHING;
INSERT INTO main_category (id, name) VALUES (2, 'Variable') ON CONFLICT DO NOTHING;

-- The INSERTs above pin explicit ids, so the sequence is still at 1 and the
-- first runtime insert after a rebuild would collide with a seeded id.
SELECT setval(pg_get_serial_sequence('main_category', 'id'), (SELECT max(id) FROM main_category));
