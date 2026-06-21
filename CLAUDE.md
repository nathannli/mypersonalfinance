# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A personal finance tracking system that loads credit card and bank transactions from various sources into a PostgreSQL database, with automatic categorization and a Metabase frontend for visualization. The system runs on k3s and includes automated processing via cron jobs.

## Technology Stack

- **Database**: PostgreSQL
- **Backend**: Python with Polars for data processing, SQLAlchemy for database connections
- **Frontend**: Metabase
- **Platform**: k3s
- **Data Processing**: Polars DataFrames (preferred over pandas for performance)

## Setup and Common Commands

### Initial Setup

1. Copy `env-sample` to `.env` and populate with:
   - `POSTGRES_CONNECTION_STRING`: PostgreSQL connection URI
   - `WS_DEBIT_LINK`: Wealthsimple debit transaction export link
   - `WS_CREDIT_LINK`: Wealthsimple credit transaction export link

2. Install dependencies:
   ```
   uv sync --group dev
   uv run pre-commit install
   ```

### Loading Credit Card Transactions

Load from CSV files:
```bash
uv run python load-transactions.py --type <card_type> --filepath <path_to_csv> --database finance
```

Card types: `amex`, `amex_annual`, `rogers`, `simplii_visa`, `simplii_debit`, `bmo`, `rbc_cc`, `ws_debit`, `ws_credit`

Load from online sources (no file needed):
```bash
uv sync --extra wealthsimple
uv run python load-transactions.py --type ws_debit --database finance
uv run python load-transactions.py --type ws_credit --database finance
```

If `--type` is omitted, it will be inferred from the filename.

### Loading Excel Transactions

For pre-categorized Excel files:
```bash
uv run python load-excel-transactions.py --filepath <path_to_excel>
```

For FTP sources (used by cron):
```bash
uv run python load-excel-transactions.py --filepath ftp://user:pass@host/file.xlsx --cron true
```

### Code Quality

Using ruff (via pre-commit):
```bash
uv run pre-commit run --all-files
```

Or manually:
```bash
uv run ruff check --fix .
uv run ruff format .
```

## Architecture

### Database Schemas

Two PostgreSQL schemas:
- `finance`: Personal expenses
- `parents_finance`: Parents' expenses

Core tables:
- `expenses`: Transaction records (date, merchant, cost, category_id, subcategory_id)
- `categories`: Top-level expense categories
- `subcategories`: Detailed categorization within categories
- `merchant_name_auto_match`: Exact merchant name to category mappings
- `substring_auto_match`: Substring-based merchant categorization

### Class Hierarchy

**Database Classes** (`db/`):
- `PostgresDB` (abstract): Base database connection and query methods in `db/base.py`
- `FinanceDB` (abstract): Finance-specific operations extending PostgresDB in `db/finance_base.py`
- `MyFinanceDB`: Personal finance database implementation in `db/my_finance.py`
- `ParentsFinanceDB`: Parents' finance database implementation in `db/parents_finance.py`

**Transaction Source Classes** (`sources/`):
- `FileBasedCardStatement` (abstract): For CSV-based statements in `sources/base.py`
- `OnlineCardStatement` (abstract): For API/link-based statements in `sources/base.py`
- Card-specific implementations: `AmexStatement`, `AmexAnnualStatement`, `RogersStatement`, `SimpliiVisaStatement`, `SimpliiDebitStatement`, `BMOStatement`, `RbcCcStatement`, `WealthsimpleDebitStatement`, `WealthsimpleCreditStatement`

All card statement classes must implement `load_data()` and return a Polars DataFrame with columns: `date`, `merchant`, `cost`, `cc_category`

### Transaction Loading Flow

1. Statement class loads and normalizes transaction data into standard format
2. Script checks if transaction exists (by date, merchant, cost)
3. For new transactions:
   - Attempts auto-categorization via merchant name match or substring match
   - If no match, prompts user interactively to select category/subcategory
   - Inserts transaction into expenses table
   - Optionally adds merchant to auto_match tables for future use

### Auto-Categorization

The system uses a two-tier matching strategy:
1. **Exact match**: `merchant_name_auto_match` table for exact merchant names
2. **Substring match**: `substring_auto_match` table for partial matches (e.g., "shoppers" matches "Shoppers Drug Mart")
3. **Card-specific**: Rogers statements use `cc_category` from statement for additional matching

Special handling for reimbursements: Checks for duplicate entries by (date, merchant) only, without cost.

## Development Notes

- Use Polars DataFrames throughout; Polars config is set to display 900 rows/chars for debugging in database classes
- All database operations use context managers for connection safety
- Interactive categorization allows "skip" command to skip a transaction
- Card type can be inferred from filename if it contains the card type keyword
- Wealthsimple uses a custom local package (modified fork of Wealthsimpleton) for API access; `uv` resolves it from `../Wealthsimpleton` when you run `uv sync --extra wealthsimple`
- Cron automation: Weekly cron job processes parents' transactions from FTP sources, with Discord notification integration
