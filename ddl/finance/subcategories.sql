CREATE TABLE IF NOT EXISTS subcategories (
    id serial NOT NULL,
    name text NOT NULL,
    category_id integer NOT NULL,
    expense_type_id integer NOT NULL,
    CONSTRAINT subcategories_pkey PRIMARY KEY (id),
    CONSTRAINT subcategories_category_id_fkey FOREIGN KEY (category_id) REFERENCES categories (id),
    CONSTRAINT subcategories_expense_type_id_fkey FOREIGN KEY (expense_type_id) REFERENCES expense_type (id)
);

INSERT INTO subcategories (id, name, category_id, expense_type_id) VALUES (1, 'Car Maintenance', 1, 2) ON CONFLICT DO NOTHING;
INSERT INTO subcategories (id, name, category_id, expense_type_id) VALUES (2, 'Gas', 1, 1) ON CONFLICT DO NOTHING;
INSERT INTO subcategories (id, name, category_id, expense_type_id) VALUES (3, 'Parking', 1, 1) ON CONFLICT DO NOTHING;
INSERT INTO subcategories (id, name, category_id, expense_type_id) VALUES (4, 'Rides', 1, 1) ON CONFLICT DO NOTHING;
INSERT INTO subcategories (id, name, category_id, expense_type_id) VALUES (5, 'Transit', 1, 2) ON CONFLICT DO NOTHING;
INSERT INTO subcategories (id, name, category_id, expense_type_id) VALUES (6, 'OSAP', 2, 2) ON CONFLICT DO NOTHING;
INSERT INTO subcategories (id, name, category_id, expense_type_id) VALUES (7, 'Hobbies', 3, 1) ON CONFLICT DO NOTHING;
INSERT INTO subcategories (id, name, category_id, expense_type_id) VALUES (8, 'Media', 3, 1) ON CONFLICT DO NOTHING;
INSERT INTO subcategories (id, name, category_id, expense_type_id) VALUES (9, 'Other', 3, 1) ON CONFLICT DO NOTHING;
INSERT INTO subcategories (id, name, category_id, expense_type_id) VALUES (10, 'Substances', 3, 1) ON CONFLICT DO NOTHING;
INSERT INTO subcategories (id, name, category_id, expense_type_id) VALUES (11, 'Eating Out', 4, 1) ON CONFLICT DO NOTHING;
INSERT INTO subcategories (id, name, category_id, expense_type_id) VALUES (12, 'Food Delivery', 4, 1) ON CONFLICT DO NOTHING;
INSERT INTO subcategories (id, name, category_id, expense_type_id) VALUES (13, 'Grocery', 4, 1) ON CONFLICT DO NOTHING;
INSERT INTO subcategories (id, name, category_id, expense_type_id) VALUES (14, 'Full Reimburse', 5, 1) ON CONFLICT DO NOTHING;
INSERT INTO subcategories (id, name, category_id, expense_type_id) VALUES (15, 'Rent', 6, 2) ON CONFLICT DO NOTHING;
INSERT INTO subcategories (id, name, category_id, expense_type_id) VALUES (16, 'Insurance', 7, 2) ON CONFLICT DO NOTHING;
INSERT INTO subcategories (id, name, category_id, expense_type_id) VALUES (17, 'Charity', 8, 1) ON CONFLICT DO NOTHING;
INSERT INTO subcategories (id, name, category_id, expense_type_id) VALUES (18, 'Fees', 8, 1) ON CONFLICT DO NOTHING;
INSERT INTO subcategories (id, name, category_id, expense_type_id) VALUES (19, 'Misc', 8, 1) ON CONFLICT DO NOTHING;
INSERT INTO subcategories (id, name, category_id, expense_type_id) VALUES (20, 'Subscriptions', 8, 1) ON CONFLICT DO NOTHING;
INSERT INTO subcategories (id, name, category_id, expense_type_id) VALUES (21, 'Fitness', 9, 2) ON CONFLICT DO NOTHING;
INSERT INTO subcategories (id, name, category_id, expense_type_id) VALUES (22, 'Health', 9, 2) ON CONFLICT DO NOTHING;
INSERT INTO subcategories (id, name, category_id, expense_type_id) VALUES (23, 'Hygiene', 9, 2) ON CONFLICT DO NOTHING;
INSERT INTO subcategories (id, name, category_id, expense_type_id) VALUES (24, 'Learning', 9, 2) ON CONFLICT DO NOTHING;
INSERT INTO subcategories (id, name, category_id, expense_type_id) VALUES (25, 'Clothes', 10, 1) ON CONFLICT DO NOTHING;
INSERT INTO subcategories (id, name, category_id, expense_type_id) VALUES (26, 'Electronics', 10, 1) ON CONFLICT DO NOTHING;
INSERT INTO subcategories (id, name, category_id, expense_type_id) VALUES (27, 'Household', 10, 1) ON CONFLICT DO NOTHING;
INSERT INTO subcategories (id, name, category_id, expense_type_id) VALUES (28, 'Misc', 10, 1) ON CONFLICT DO NOTHING;
INSERT INTO subcategories (id, name, category_id, expense_type_id) VALUES (29, 'Office', 10, 1) ON CONFLICT DO NOTHING;
INSERT INTO subcategories (id, name, category_id, expense_type_id) VALUES (30, 'Travel', 11, 1) ON CONFLICT DO NOTHING;
INSERT INTO subcategories (id, name, category_id, expense_type_id) VALUES (31, 'Hydro', 12, 2) ON CONFLICT DO NOTHING;
INSERT INTO subcategories (id, name, category_id, expense_type_id) VALUES (32, 'Internet', 12, 2) ON CONFLICT DO NOTHING;
INSERT INTO subcategories (id, name, category_id, expense_type_id) VALUES (33, 'Mobile', 12, 2) ON CONFLICT DO NOTHING;
INSERT INTO subcategories (id, name, category_id, expense_type_id) VALUES (34, 'China', 11, 1) ON CONFLICT DO NOTHING;
INSERT INTO subcategories (id, name, category_id, expense_type_id) VALUES (35, 'Paris', 11, 1) ON CONFLICT DO NOTHING;
INSERT INTO subcategories (id, name, category_id, expense_type_id) VALUES (36, 'AI', 13, 1) ON CONFLICT DO NOTHING;

SELECT setval(pg_get_serial_sequence('subcategories', 'id'), (SELECT max(id) FROM subcategories));
