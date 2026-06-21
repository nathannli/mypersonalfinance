# AGENTS.md

This file gives repository-specific guidance for coding agents working in this project.

## Scope

- Applies to the entire repository rooted at `/Users/nathan/data/personal/mypersonalfinance`.
- Prefer these instructions when making changes in this repo.

## Project Overview

- This repo loads and categorizes financial transactions into PostgreSQL databases.
- The repo now uses `uv` with `pyproject.toml` for dependency management.
- Primary entry point for card/file ingestion: `load-transactions.py` (CLI/service flow).
- Primary Excel ingestion entry point: `load-excel-transactions.py`.
- Two databases are used:
  - `finance`
  - `parents_finance`

## Setup

- Install dependencies with `uv sync --group dev`.
- Install hooks with `uv run pre-commit install`.
- Wealthsimple commands require `uv sync --extra wealthsimple`, which resolves `wealthsimpleton` from the local `../Wealthsimpleton` checkout.

## Transaction Loading Flow (Current)

- CLI -> `services/transaction_loader.py` -> source class in `sources/` -> standardized Polars DataFrame (`date`, `merchant`, `cost`, `cc_category`) -> `services/transaction_processor.py` -> DB `insert_expense(...)`.
- Duplicate detection is done before insertion via `check_if_expense_exists(date, merchant, cost)`.

## Important Code Paths

- Rogers CSV parser: `/Users/nathan/data/personal/mypersonalfinance/sources/rogers.py`
- Parents DB insert logic: `/Users/nathan/data/personal/mypersonalfinance/db/parents_finance.py`
- Personal DB insert logic: `/Users/nathan/data/personal/mypersonalfinance/db/my_finance.py`
- Processing loop/counters: `/Users/nathan/data/personal/mypersonalfinance/services/transaction_processor.py`

## Known Pitfalls (Do Not Reintroduce)

- Rogers CSV schema inference can fail on oversized `Reference Number` values.
  - Keep `Reference Number` as `Utf8` using `schema_overrides` in `pl.read_csv(...)`.
- Rogers CSVs can include non-breaking spaces (`\\xa0`) in text fields.
  - Normalize `merchant` and `cc_category` (`replace \\xa0`, then trim) before matching or DB lookups.
- `parents_finance` category IDs are not stable across DBs/environments.
  - Never hardcode an "ignore" category ID (e.g. `22`).
  - If skipping by category, compare normalized category name (`"Ignore"`), not numeric ID.

## Editing Guidance

- Preserve the standardized transaction DataFrame contract:
  - columns: `date`, `merchant`, `cost`, `cc_category`
- When changing parsing logic, avoid broad schema inference reliance for CSVs with mixed/dirty exports.
- Prefer normalization at ingestion boundaries (CSV parsing) and again at lookup boundaries (DB lookup helpers) for resilience.

## Debugging Guidance

- When a transaction logs `New transaction found` but is not inserted:
  - inspect `insert_expense(...)` skip paths (ignore category, cron/manual intervention, exceptions)
  - inspect category lookup inputs/normalization (especially trailing whitespace or NBSP)
  - verify database-specific category names/IDs rather than assuming seed order
- If per-file inserted counts look wrong, inspect `services/transaction_processor.py`:
  - `_insert_transactions()` currently increments after calling `insert_expense(...)` and may not reflect skipped/manual rows unless it uses the return code consistently.

## Validation

- Quick syntax check (sandbox-safe):
  - `PYTHONPYCACHEPREFIX=/tmp/pycache python3 -m py_compile <file.py>`
- Run repo tooling with `uv run ...`, e.g. `uv run pre-commit run --all-files`.

## database reference
- you have select access only to the database via my custom function `aipg`. it defaults to the finance database
