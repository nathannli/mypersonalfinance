CREATE TABLE merchant_name_auto_match (
    id serial NOT NULL,
    merchant_name text NOT NULL,
    merchant_category text NOT NULL,
    merchant_subcategory text NOT NULL,
    CONSTRAINT merchant_name_auto_match_pkey PRIMARY KEY (id),
    CONSTRAINT merchant_name_auto_match_merchant_name_merchant_category_key UNIQUE (merchant_name, merchant_category, merchant_subcategory)
);
