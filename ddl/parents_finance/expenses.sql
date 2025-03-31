CREATE TABLE expenses (
    id serial NOT NULL,
    date date NOT NULL,
    merchant text NOT NULL,
    category_id integer NOT NULL,
    cost numeric(10,2) NOT NULL,
    CONSTRAINT expenses_pkey PRIMARY KEY (id),
    CONSTRAINT expenses_date_merchant_cost_key UNIQUE (date, merchant, cost),
    CONSTRAINT expenses_category_id_fkey FOREIGN KEY (category_id) REFERENCES categories (id)
);
