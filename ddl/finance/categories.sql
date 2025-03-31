CREATE TABLE categories (
    id serial NOT NULL,
    name text NOT NULL,
    CONSTRAINT categories_pkey PRIMARY KEY (id)
);

INSERT INTO categories (name) VALUES ('Commuting');
INSERT INTO categories (name) VALUES ('Debt');
INSERT INTO categories (name) VALUES ('Entertainment');
INSERT INTO categories (name) VALUES ('Food');
INSERT INTO categories (name) VALUES ('Full Reimburse');
INSERT INTO categories (name) VALUES ('Housing');
INSERT INTO categories (name) VALUES ('Insurance');
INSERT INTO categories (name) VALUES ('Misc');
INSERT INTO categories (name) VALUES ('Personal Care');
INSERT INTO categories (name) VALUES ('Shopping');
INSERT INTO categories (name) VALUES ('Travel');
INSERT INTO categories (name) VALUES ('Utilities');
