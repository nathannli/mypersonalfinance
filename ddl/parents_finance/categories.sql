CREATE TABLE IF NOT EXISTS categories (
    id serial NOT NULL,
    name text NOT NULL,
    main_category_id integer NOT NULL,
    CONSTRAINT categories_pkey PRIMARY KEY (id),
    CONSTRAINT categories_uk UNIQUE (main_category_id, name),
    CONSTRAINT categories_main_category_id_fk FOREIGN KEY (main_category_id) REFERENCES main_category (id)
);

-- Live ids are authoritative, gaps included. 6 and 16 were never issued and
-- no expense references either. A dense 1..22 re-seed shifts every id from 7
-- upward and mis-resolves the 4124 expenses that point at them.
INSERT INTO categories (id, name, main_category_id) VALUES (1, 'Clothing', 2) ON CONFLICT DO NOTHING;
INSERT INTO categories (id, name, main_category_id) VALUES (2, 'Donation', 2) ON CONFLICT DO NOTHING;
INSERT INTO categories (id, name, main_category_id) VALUES (3, 'Entertainment', 2) ON CONFLICT DO NOTHING;
INSERT INTO categories (id, name, main_category_id) VALUES (4, 'Food', 2) ON CONFLICT DO NOTHING;
INSERT INTO categories (id, name, main_category_id) VALUES (5, 'Fund Tfr', 2) ON CONFLICT DO NOTHING;
INSERT INTO categories (id, name, main_category_id) VALUES (7, 'Household', 2) ON CONFLICT DO NOTHING;
INSERT INTO categories (id, name, main_category_id) VALUES (8, 'Housekeeping', 2) ON CONFLICT DO NOTHING;
INSERT INTO categories (id, name, main_category_id) VALUES (9, 'Insurance', 1) ON CONFLICT DO NOTHING;
INSERT INTO categories (id, name, main_category_id) VALUES (10, 'Loans', 1) ON CONFLICT DO NOTHING;
INSERT INTO categories (id, name, main_category_id) VALUES (11, 'Medical/Health', 1) ON CONFLICT DO NOTHING;
INSERT INTO categories (id, name, main_category_id) VALUES (12, 'NewHome', 2) ON CONFLICT DO NOTHING;
INSERT INTO categories (id, name, main_category_id) VALUES (13, 'Transportation', 1) ON CONFLICT DO NOTHING;
INSERT INTO categories (id, name, main_category_id) VALUES (14, 'Tuitions', 2) ON CONFLICT DO NOTHING;
INSERT INTO categories (id, name, main_category_id) VALUES (15, 'ApartmentRental', 1) ON CONFLICT DO NOTHING;
INSERT INTO categories (id, name, main_category_id) VALUES (17, 'MISCexpense', 2) ON CONFLICT DO NOTHING;
INSERT INTO categories (id, name, main_category_id) VALUES (18, 'Taxes/Legal', 1) ON CONFLICT DO NOTHING;
INSERT INTO categories (id, name, main_category_id) VALUES (19, 'Fees & Interest', 1) ON CONFLICT DO NOTHING;
INSERT INTO categories (id, name, main_category_id) VALUES (20, 'Kitchen', 2) ON CONFLICT DO NOTHING;
INSERT INTO categories (id, name, main_category_id) VALUES (21, 'Hygiene', 2) ON CONFLICT DO NOTHING;
INSERT INTO categories (id, name, main_category_id) VALUES (22, 'Utilities', 1) ON CONFLICT DO NOTHING;
INSERT INTO categories (id, name, main_category_id) VALUES (23, 'Misc - Cash Payment', 2) ON CONFLICT DO NOTHING;
INSERT INTO categories (id, name, main_category_id) VALUES (24, 'Rent Mortgage', 1) ON CONFLICT DO NOTHING;

-- The Gifts/Donations row the previous version of this file seeded is gone.
-- It is not in the live table, and a rebuild must match live rather than
-- resurrect it.

-- The INSERTs above pin explicit ids, so the sequence is still at 1 and the
-- first runtime insert after a rebuild would collide with a seeded id.
SELECT setval(pg_get_serial_sequence('categories', 'id'), (SELECT max(id) FROM categories));
