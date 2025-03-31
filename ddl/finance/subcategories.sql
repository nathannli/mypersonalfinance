CREATE TABLE subcategories (
    id serial NOT NULL,
    name text NOT NULL,
    category_id integer NOT NULL,
    CONSTRAINT subcategories_pkey PRIMARY KEY (id),
    CONSTRAINT subcategories_category_id_fkey FOREIGN KEY (category_id) REFERENCES categories (id)
);

INSERT INTO subcategories (name, category_id) VALUES ('Car Maintenance', (SELECT id FROM categories WHERE name = 'Commuting'));
INSERT INTO subcategories (name, category_id) VALUES ('Gas', (SELECT id FROM categories WHERE name = 'Commuting'));
INSERT INTO subcategories (name, category_id) VALUES ('Parking', (SELECT id FROM categories WHERE name = 'Commuting'));
INSERT INTO subcategories (name, category_id) VALUES ('Rides', (SELECT id FROM categories WHERE name = 'Commuting'));
INSERT INTO subcategories (name, category_id) VALUES ('Transit', (SELECT id FROM categories WHERE name = 'Commuting'));
INSERT INTO subcategories (name, category_id) VALUES ('OSAP', (SELECT id FROM categories WHERE name = 'Debt'));
INSERT INTO subcategories (name, category_id) VALUES ('Hobbies', (SELECT id FROM categories WHERE name = 'Entertainment'));
INSERT INTO subcategories (name, category_id) VALUES ('Media', (SELECT id FROM categories WHERE name = 'Entertainment'));
INSERT INTO subcategories (name, category_id) VALUES ('Other', (SELECT id FROM categories WHERE name = 'Entertainment'));
INSERT INTO subcategories (name, category_id) VALUES ('Substances', (SELECT id FROM categories WHERE name = 'Entertainment'));
INSERT INTO subcategories (name, category_id) VALUES ('Eating Out', (SELECT id FROM categories WHERE name = 'Food'));
INSERT INTO subcategories (name, category_id) VALUES ('Food Delivery', (SELECT id FROM categories WHERE name = 'Food'));
INSERT INTO subcategories (name, category_id) VALUES ('Grocery', (SELECT id FROM categories WHERE name = 'Food'));
INSERT INTO subcategories (name, category_id) VALUES ('Full Reimburse', (SELECT id FROM categories WHERE name = 'Full Reimburse'));
INSERT INTO subcategories (name, category_id) VALUES ('Rent', (SELECT id FROM categories WHERE name = 'Housing'));
INSERT INTO subcategories (name, category_id) VALUES ('Insurance', (SELECT id FROM categories WHERE name = 'Insurance'));
INSERT INTO subcategories (name, category_id) VALUES ('Charity', (SELECT id FROM categories WHERE name = 'Misc'));
INSERT INTO subcategories (name, category_id) VALUES ('Fees', (SELECT id FROM categories WHERE name = 'Misc'));
INSERT INTO subcategories (name, category_id) VALUES ('Misc', (SELECT id FROM categories WHERE name = 'Misc'));
INSERT INTO subcategories (name, category_id) VALUES ('Subscriptions', (SELECT id FROM categories WHERE name = 'Misc'));
INSERT INTO subcategories (name, category_id) VALUES ('Fitness', (SELECT id FROM categories WHERE name = 'Personal Care'));
INSERT INTO subcategories (name, category_id) VALUES ('Health', (SELECT id FROM categories WHERE name = 'Personal Care'));
INSERT INTO subcategories (name, category_id) VALUES ('Hygiene', (SELECT id FROM categories WHERE name = 'Personal Care'));
INSERT INTO subcategories (name, category_id) VALUES ('Learning', (SELECT id FROM categories WHERE name = 'Personal Care'));
INSERT INTO subcategories (name, category_id) VALUES ('Clothes', (SELECT id FROM categories WHERE name = 'Shopping'));
INSERT INTO subcategories (name, category_id) VALUES ('Electronics', (SELECT id FROM categories WHERE name = 'Shopping'));
INSERT INTO subcategories (name, category_id) VALUES ('Household', (SELECT id FROM categories WHERE name = 'Shopping'));
INSERT INTO subcategories (name, category_id) VALUES ('Misc', (SELECT id FROM categories WHERE name = 'Shopping'));
INSERT INTO subcategories (name, category_id) VALUES ('Office', (SELECT id FROM categories WHERE name = 'Shopping'));
INSERT INTO subcategories (name, category_id) VALUES ('Travel', (SELECT id FROM categories WHERE name = 'Travel'));
INSERT INTO subcategories (name, category_id) VALUES ('Hydro', (SELECT id FROM categories WHERE name = 'Utilities'));
INSERT INTO subcategories (name, category_id) VALUES ('Internet', (SELECT id FROM categories WHERE name = 'Utilities'));
INSERT INTO subcategories (name, category_id) VALUES ('Mobile', (SELECT id FROM categories WHERE name = 'Utilities'));
