CREATE TABLE substring_auto_match (
    id serial NOT NULL,
    substring text NOT NULL,
    merchant_category text NOT NULL,
    CONSTRAINT substring_auto_match_pkey PRIMARY KEY (id),
    CONSTRAINT substring_auto_match_substring_merchant_category_key UNIQUE (substring, merchant_category)
    -- Table for matching merchant names by substring patterns
    -- Used as fallback when exact merchant name match fails in merchant_name_auto_match
    -- entries should be added manually based on finding sure patterns from the merchant_name_auto_match table
);
