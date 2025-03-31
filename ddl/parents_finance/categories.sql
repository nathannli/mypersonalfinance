CREATE TABLE categories (
    id serial NOT NULL,
    name text NOT NULL,
    CONSTRAINT categories_pkey PRIMARY KEY (id)
);

INSERT INTO categories (name) VALUES ('Clothing');
INSERT INTO categories (name) VALUES ('Donation');
INSERT INTO categories (name) VALUES ('Entertainment');
INSERT INTO categories (name) VALUES ('Food');
INSERT INTO categories (name) VALUES ('Fund Tfr');
INSERT INTO categories (name) VALUES ('Gifts/Donations');
INSERT INTO categories (name) VALUES ('Household');
INSERT INTO categories (name) VALUES ('Housekeeping');
INSERT INTO categories (name) VALUES ('Insurance');
INSERT INTO categories (name) VALUES ('Loans');
INSERT INTO categories (name) VALUES ('Medical/Health');
INSERT INTO categories (name) VALUES ('NewHome');
INSERT INTO categories (name) VALUES ('Transportation');
INSERT INTO categories (name) VALUES ('Tuitions');
