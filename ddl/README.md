# ddl

Schema and seed for the two live databases, `finance` and `parents_finance`.

Everything here is safe to apply more than once. Every `CREATE TABLE` and
`ADD COLUMN` is `IF NOT EXISTS`, every seeded `INSERT` carries its explicit id
and `ON CONFLICT DO NOTHING`, and every file that seeds rows ends with a
`setval` guard. Applying the whole tree twice inserts 0 rows and raises 0
errors.

## Schema vs seed

| | |
| --- | --- |
| `ddl/common/` | database creation only |
| `ddl/finance/`, `ddl/parents_finance/` | tracked schema plus taxonomy rows |
| `ddl/seed/` | **gitignored** merchant rows, generated from live |

The split is not cosmetic. This repository is public, and a merchant row is a
behavioural profile: the 164 exact-match and 100 substring rows in `finance`
alone name insurers, dentists, a gym, a student-loan servicer, TCM and rehab
providers, and specific neighbourhood grocers. So the tracked files carry
`CREATE TABLE`, constraints, and the taxonomy, and nothing else. The taxonomy
— categories, subcategories, `expense_type`, `main_category` — is generic, names
no merchant, and stays committed.

`ddl/seed/` is gitignored for the same reason `tests/test_private_*.py` already
was. It is not a data-loss risk because it is regenerable: see below.

## Apply order

Order matters in two places, because of foreign keys.

```
ddl/common/databases.sql

ddl/finance/            categories → expense_type → subcategories → expenses
                        → merchant_name_auto_match → substring_auto_match
ddl/seed/finance.*.sql

ddl/parents_finance/    main_category → categories → expenses
                        → auto_match → substring_auto_match
ddl/seed/parents_finance.*.sql
```

- `expense_type` before `subcategories`, which carries a foreign key to it.
- `categories` before `subcategories` and `expenses`, same reason.
- `main_category` before `parents_finance.categories`, which carries a foreign
  key to it.
- schema before seed. The seed files insert into tables the schema files
  create.

## Rebuilding a database from scratch

The commands below use the standard `psql` client, which is **not** bundled
with this repository and is not on every developer's `PATH`. There is no Python
driver for them on purpose: `psql` is the one tool a rebuild is expected to use
regardless of which virtualenv is active.

```sh
createdb finance          # or: psql -f ddl/common/databases.sql
uv run python scripts/export_ddl_seed.py --database finance   # writes ddl/seed/
psql -d finance -f ddl/finance/categories.sql
psql -d finance -f ddl/finance/expense_type.sql
psql -d finance -f ddl/finance/subcategories.sql
psql -d finance -f ddl/finance/expenses.sql
psql -d finance -f ddl/finance/merchant_name_auto_match.sql
psql -d finance -f ddl/finance/substring_auto_match.sql
psql -d finance -f ddl/seed/finance.merchant_name_auto_match.sql
psql -d finance -f ddl/seed/finance.substring_auto_match.sql
```

Swap `finance` for `parents_finance` and the file list for that directory. The
result is schema and taxonomy identical to live, with 0 `expenses` rows. Expense
data is personal and is never in this directory; restore it separately.

## Regenerating the seed

`ddl/seed/` is written from a live database, never by hand:

```sh
uv run --frozen python scripts/export_ddl_seed.py --database finance
uv run --frozen python scripts/export_ddl_seed.py --database parents_finance
```

`--check` is the read-only mode. It renders in memory, writes nothing, and
exits 1 if any file on disk differs from live:

```sh
uv run --frozen python scripts/export_ddl_seed.py --database parents_finance --check
```

The script only reads. It never writes to a database.

## Why seeded ids are explicit

Every runtime insert in this repo (`db/my_finance.py`, `db/parents_finance.py`)
omits the id and relies on `nextval`. A seed that assigns its own ids would
therefore leave the sequence at 1, and the first insert after a rebuild would
collide with a seeded primary key. Every seeded file ends with:

```sql
SELECT setval(pg_get_serial_sequence('<table>', 'id'), (SELECT max(id) FROM <table>));
```

`parents_finance.categories` also has live id gaps at 6 and 16. They are kept.
4124 live expenses reference 21 distinct `category_id` values, and a densified
1..22 re-seed would shift every id from 7 upward — `Household` (7) would become
6, and every one of those expenses would report the wrong category with no
constraint violated.

## The two databases are not the same shape

`parents_finance` keeps its own naming on purpose: `main_category` → `categories`,
two levels, no subcategory column. Its `auto_match` and `substring_auto_match`
have no subcategory column, so their 2-column `UNIQUE` is correct there —
`finance`'s are 3-column, also correct. Converging the two is a regression, not
an improvement.

Their id conventions are **inverted**:

| | id 1 | id 2 |
| --- | --- | --- |
| `finance.expense_type` | `variable` | `fixed` |
| `parents_finance.main_category` | `Fixed` | `Variable` |

A row copied between the two silently mis-buckets every expense.

## Verifying

`scripts/verify_spec_claims.py` checks every claim this directory makes against
the live databases — column and constraint parity, sequence positions, row ids,
and which rows are absent from the tracked files:

```sh
uv run --frozen python scripts/verify_spec_claims.py
```

It needs a live connection and `gh`, so it is not part of the unit suite and
never runs in CI. Run it before and after editing anything in `ddl/`.

`scripts/rebuild_ddl_scratch.py` proves the rebuild recipe above still works.
It builds both databases into scratch databases with distinct names, applies
the tracked tree plus the generated seed, and asserts parity with live:
column-for-column across all 11 tables, id sequences including the 6 and 16
gaps, `expenses` empty, `parents_finance.expenses.comments` present, every
seeded sequence past its max id, the whole tree re-applied with 0 rows inserted
and 0 errors, and every live `parents_finance` `category_id` replayed into the
scratch copy resolving to the same category name. It then drops both scratch
databases and never writes to live:

```sh
uv run --frozen python scripts/rebuild_ddl_scratch.py
```

It needs a live connection and an exported `ddl/seed/`, so like
`scripts/verify_spec_claims.py` it is not part of the unit suite and never runs
in CI. Run it after changing anything in the tree itself: a schema or seed edit
can pass every claim check and still break the rebuild.
