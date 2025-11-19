# WARP.md

This file provides guidance to WARP (warp.dev) when working with code in this repository.

## Project Overview

A personal finance tracking system that loads credit card and bank transactions from various sources into a PostgreSQL database, with automatic categorization and a Metabase frontend for visualization. The system runs on k3s and includes automated processing via cron jobs.

## Technology Stack

- **Database**: PostgreSQL
- **Backend**: Python with Polars for data processing, SQLAlchemy for database connections
- **Frontend**: Metabase
- **Platform**: k3s
- **Data Processing**: Polars DataFrames (preferred over pandas for performance)

## Environment Setup

1. Copy `env-sample` to `.env` and populate with:
   - `POSTGRES_CONNECTION_STRING`: PostgreSQL connection URI
   - `WS_DEBIT_LINK`: Wealthsimple debit transaction export link
   - `WS_CREDIT_LINK`: Wealthsimple credit transaction export link

2. Install dependencies:
   ```fish
   pip install -r requirements.txt
   ```

3. Install pre-commit hooks (uses ruff for linting and formatting):
   ```fish
   pre-commit install
   ```

## Common Commands

### Loading Credit Card Transactions

Load from CSV files (Amex, Rogers, Simplii, BMO, RBC):
```fish
python load-cc-transactions.py --type <card_type> --filepath <path_to_csv> --database finance
```

Load from online sources (Wealthsimple - no file needed):
```fish
python load-cc-transactions.py --type ws_debit --database finance
python load-cc-transactions.py --type ws_credit --database finance
```

Card types: `amex`, `rogers`, `simplii_visa`, `simplii_debit`, `bmo`, `rbc_cc`, `ws_debit`, `ws_credit`

If `--type` is omitted, it will be inferred from the filename.

### Loading Excel Transactions

For pre-categorized Excel files (typically for parents' database):
```fish
python load-excel-transactions.py --filepath <path_to_excel>
```

For FTP sources (used by cron):
```fish
python load-excel-transactions.py --filepath ftp://user:pass@host/file.xlsx --cron true
```

### Code Quality

Run linter and formatter:
```fish
pre-commit run --all-files
```

Or manually:
```fish
ruff check --fix .
ruff format .
```

## Architecture

### Database Schemas

The system uses two PostgreSQL schemas:
- `finance`: Personal expenses
- `parents_finance`: Parents' expenses

Core tables:
- `expenses`: Transaction records (date, merchant, cost, category_id, subcategory_id)
- `categories`: Top-level expense categories (e.g., Food, Housing, Travel)
- `subcategories`: Detailed categorization within categories
- `merchant_name_auto_match`: Exact merchant name to category mappings
- `substring_auto_match`: Substring-based merchant categorization

### Class Hierarchy

**Database Classes** (`classes/db/`):
- `PostgresDB` (abstract): Base database connection and query methods
- `FinanceDB` (abstract): Finance-specific operations extending PostgresDB
- `MyFinanceDB`: Personal finance database implementation
- `ParentsFinanceDB`: Parents' finance database implementation

**Credit Card Statement Classes** (`classes/cc/`):
- `FileBasedCardStatement` (abstract): For CSV-based statements
- `OnlineCardStatement` (abstract): For API/link-based statements
- Card-specific implementations: `AmexStatement`, `RogersStatement`, `SimpliiVisaStatement`, `SimpliiDebitStatement`, `BMOStatement`, `RbcCcStatement`, `WealthsimpleDebitStatement`, `WealthsimpleCreditStatement`

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

## Database Configuration

- Uses `Config` class to load environment variables from `.env`
- Connection string format: `{POSTGRES_CONNECTION_STRING}/{database_name}`
- Debug mode available via constructor parameter for verbose logging

## Cron Automation

A weekly cron job processes parents' transactions from FTP sources. The script:
- Downloads files from FTP to temporary location
- Processes transactions with `--cron` flag for automated categorization
- Sends Discord notifications on completion or errors
- Cleans up temporary files

## Development Notes

- Use Polars DataFrames throughout; polars config set to display 900 rows/chars for debugging
- All database operations use context managers for connection safety
- Interactive categorization allows "skip" command to skip a transaction
- Card type can be inferred from filename if it contains the card type keyword
- Wealthsimple uses custom package (modified fork of Wealthsimpleton) for API access
