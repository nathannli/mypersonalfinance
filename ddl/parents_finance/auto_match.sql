CREATE TABLE auto_match (
    id serial NOT NULL,
    merchant_name text NOT NULL,
    merchant_category text NOT NULL,
    CONSTRAINT auto_match_pkey PRIMARY KEY (id),
    CONSTRAINT auto_match_merchant_name_merchant_category_key UNIQUE (merchant_name, merchant_category)
);

INSERT INTO auto_match (merchant_name, merchant_category) VALUES ('COSTCO', 'Grocery');
