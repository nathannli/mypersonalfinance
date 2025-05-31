CREATE TABLE substring_auto_match (
    id serial NOT NULL,
    substring text NOT NULL,
    merchant_category text NOT NULL,
    merchant_subcategory text NOT NULL,
    CONSTRAINT substring_auto_match_pkey PRIMARY KEY (id),
    CONSTRAINT substring_auto_match_substring_merchant_category_key UNIQUE (substring, merchant_category, merchant_subcategory)
    -- Table for matching merchant names by substring patterns
    -- Used as fallback when exact merchant name match fails in merchant_name_auto_match
    -- entries should be added manually based on finding sure patterns from the merchant_name_auto_match table
);


INSERT INTO substring_auto_match (substring, merchant_category, merchant_subcategory) VALUES ('uber', 'Commuting', 'Rides');
INSERT INTO substring_auto_match (substring, merchant_category, merchant_subcategory) VALUES ('presto', 'Full Reimburse', 'Full Reimburse');
INSERT INTO substring_auto_match (substring, merchant_category, merchant_subcategory) VALUES ('rumble boxing', 'Entertainment', 'Hobbies');
INSERT INTO substring_auto_match (substring, merchant_category, merchant_subcategory) VALUES ('golf', 'Entertainment', 'Hobbies');
