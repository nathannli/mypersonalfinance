CREATE TABLE IF NOT EXISTS categories (
    id serial NOT NULL,
    name text NOT NULL,
    CONSTRAINT categories_pkey PRIMARY KEY (id)
);

INSERT INTO categories (id, name) VALUES (1, 'Commuting') ON CONFLICT DO NOTHING;
INSERT INTO categories (id, name) VALUES (2, 'Debt') ON CONFLICT DO NOTHING;
INSERT INTO categories (id, name) VALUES (3, 'Entertainment') ON CONFLICT DO NOTHING;
INSERT INTO categories (id, name) VALUES (4, 'Food') ON CONFLICT DO NOTHING;
INSERT INTO categories (id, name) VALUES (5, 'Full Reimburse') ON CONFLICT DO NOTHING;
INSERT INTO categories (id, name) VALUES (6, 'Housing') ON CONFLICT DO NOTHING;
INSERT INTO categories (id, name) VALUES (7, 'Insurance') ON CONFLICT DO NOTHING;
INSERT INTO categories (id, name) VALUES (8, 'Misc') ON CONFLICT DO NOTHING;
INSERT INTO categories (id, name) VALUES (9, 'Personal Care') ON CONFLICT DO NOTHING;
INSERT INTO categories (id, name) VALUES (10, 'Shopping') ON CONFLICT DO NOTHING;
INSERT INTO categories (id, name) VALUES (11, 'Travel') ON CONFLICT DO NOTHING;
INSERT INTO categories (id, name) VALUES (12, 'Utilities') ON CONFLICT DO NOTHING;
INSERT INTO categories (id, name) VALUES (13, 'Coding') ON CONFLICT DO NOTHING;

SELECT setval(pg_get_serial_sequence('categories', 'id'), (SELECT max(id) FROM categories));
