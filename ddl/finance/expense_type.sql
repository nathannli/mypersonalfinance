CREATE TABLE IF NOT EXISTS expense_type (
    id serial NOT NULL,
    type character varying NOT NULL,
    CONSTRAINT expense_type_pkey PRIMARY KEY (id),
    CONSTRAINT expense_type_type_key UNIQUE (type)
);

INSERT INTO expense_type (id, type) VALUES (1, 'variable') ON CONFLICT DO NOTHING;
INSERT INTO expense_type (id, type) VALUES (2, 'fixed') ON CONFLICT DO NOTHING;

SELECT setval(pg_get_serial_sequence('expense_type', 'id'), (SELECT max(id) FROM expense_type));
