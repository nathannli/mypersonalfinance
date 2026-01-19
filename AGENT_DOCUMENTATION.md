# Personal Finance Agent Documentation

## Overview

This is a comprehensive personal finance management system that processes credit card transactions from various financial institutions, categorizes them, and stores them in a PostgreSQL database. The system supports both file-based CSV imports and online API integrations.

## Architecture

The system follows a modular architecture with clear separation of concerns:

```
┌───────────────────────────────────────────────────────────────┐
│                        CLI Interface                            │
└───────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌───────────────────────────────────────────────────────────────┐
│                     Transaction Processor                       │
└───────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌───────────────────────────────────────────────────────────────┐
│                      Transaction Loader                         │
└───────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌───────────────────────────────────────────────────────────────┐
│                    Card Statement Classes                       │
└───────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌───────────────────────────────────────────────────────────────┐
│                         Database Layer                           │
└───────────────────────────────────────────────────────────────┘
```

## Main Components

### 1. Entry Points

#### `load-cc-transactions.py`
- **Purpose**: Main entry point for loading credit card transactions
- **Usage**: Handles both file-based and online transaction loading
- **Key Features**:
  - Supports single files, multiple files in folders, and online sources
  - Validates arguments and orchestrates the processing workflow
  - Provides detailed error handling and user feedback

#### `load-excel-transactions.py`
- **Purpose**: Legacy entry point for processing pre-existing Excel files
- **Usage**: Specifically designed for parents' finance Excel files
- **Key Features**:
  - Handles Excel file parsing with Polars
  - Includes special logic for transfer detection and skipping
  - Supports FTP downloads for remote files
  - Provides Discord notifications for cron jobs

### 2. CLI Module

#### `cli/transaction_loader_cli.py`
- **TransactionLoaderCLI**: Main CLI handler class
- **Responsibilities**:
  - Argument parsing and validation
  - Database instance management
  - Service orchestration
  - Error handling and user feedback

**Supported Arguments**:
- `--type`: Credit card type (required)
- `--filepath`: Path to single CSV file (optional)
- `--folder`: Path to folder with multiple CSV files (optional)
- `--database`: Database name ('finance' or 'parents_finance') (required)

### 3. Card Registry

#### `classes/cc/card_registry.py`
- **Purpose**: Central registry for all supported credit card types
- **Key Functions**:
  - `get_card_type_names()`: Returns all supported card types
  - `get_file_based_card_types()`: Returns card types requiring file input
  - `get_online_card_types()`: Returns card types using online sources
  - `get_card_class(card_type)`: Returns the appropriate statement class
  - `requires_file(card_type)`: Checks if card type needs file input

**Supported Card Types**:
- **File-based**: amex, amex_annual, rogers, simplii_visa, simplii_debit, bmo, canadian_tire, cibc_mc, rbc_cc, td_debit, td_visa
- **Online**: ws_debit, ws_credit

### 4. Transaction Processing Services

#### `services/transaction_processor.py`
- **TransactionProcessor**: Orchestrates the transaction processing workflow
- **Key Methods**:
  - `process_files(card_type, files)`: Main processing method
  - `_process_single_file(card_type, file_path, file_name)`: Processes individual files
  - `_insert_transactions(df, card_type)`: Inserts transactions into database

#### `services/transaction_loader.py`
- **TransactionLoader**: Loads transaction data from various sources
- **Key Methods**:
  - `load(card_type, file_path)`: Main loading method
  - `load_from_file(card_type, file_path)`: Convenience method for file-based cards
  - `load_from_online(card_type)`: Convenience method for online cards

### 5. Card Statement Classes

#### Base Classes
- **FileBasedCardStatement**: Abstract base class for file-based card statements
- **OnlineCardStatement**: Abstract base class for online card statements

#### Concrete Implementations
Each card type has its own implementation that extends one of the base classes:

**File-based implementations**:
- `CibcMcStatement`: CIBC Mastercard CSV processing
- `AmexStatement`: American Express CSV processing
- `RogersStatement`: Rogers Mastercard CSV processing
- `SimpliiVisaStatement`: Simplii Visa CSV processing
- `BMOStatement`: BMO CSV processing
- `CanadianTireStatement`: Canadian Tire CSV processing
- `RbcCcStatement`: RBC Credit Card CSV processing
- `TdDebitStatement`: TD Debit CSV processing
- `TdVisaStatement`: TD Visa CSV processing

**Online implementations**:
- `WealthsimpleDebitStatement`: Wealthsimple Debit API integration
- `WealthsimpleCreditStatement`: Wealthsimple Credit API integration

**Common Processing Pattern**:
1. Read raw data (CSV file or API response)
2. Parse and clean data
3. Filter out irrelevant transactions (payments, transfers)
4. Standardize column names to: `date`, `merchant`, `cost`, `cc_category`
5. Return processed DataFrame

### 6. Database Layer

#### Base Classes
- **PostgresDB**: Abstract base class for PostgreSQL database operations
- **FinanceDB**: Abstract base class for finance-specific database operations

#### Concrete Implementations
- **MyFinanceDB**: Main personal finance database implementation
- **ParentsFinanceDB**: Parents' finance database implementation

**Key Database Methods**:
- `insert_expense(date, merchant, cost, card_type, cc_category)`: Main expense insertion
- `check_if_expense_exists(date, merchant, cost)`: Duplicate detection
- `get_auto_match_category(merchant)`: Automatic categorization
- `insert_into_auto_match(merchant, category, subcategory)`: Learning from user input

**Database Schema**:
- `expenses`: Main transaction table
- `categories`: Transaction categories
- `subcategories`: Transaction subcategories
- `merchant_name_auto_match`: Merchant-to-category mapping
- `substring_auto_match`: Substring-based categorization rules

### 7. Configuration

#### `classes/config.py`
- **Config**: Central configuration management
- **Responsibilities**:
  - Loads environment variables from `.env` file
  - Provides database connection strings
  - Manages Wealthsimple API links
  - Handles debug mode configuration

**Environment Variables**:
- `POSTGRES_CONNECTION_STRING`: PostgreSQL connection URI
- `WS_DEBIT_LINK`: Wealthsimple Debit account activity URL
- `WS_CREDIT_LINK`: Wealthsimple Credit account activity URL

### 8. Utilities

#### `utils/processing_results.py`
- **ProcessingResults**: Tracks and reports processing results
- **Features**:
  - Tracks successful and failed file processing
  - Provides summary statistics
  - Generates formatted output for user feedback

## Data Flow

### File-based Processing Flow

```
1. User runs: python load-cc-transactions.py --type cibc_mc --filepath statement.csv --database finance
2. CLI validates arguments and initializes services
3. TransactionLoader.load() is called with card type and file path
4. CardRegistry.get_card_class() returns CibcMcStatement class
5. CibcMcStatement processes the CSV file into a standardized DataFrame
6. TransactionProcessor._insert_transactions() iterates through DataFrame rows
7. For each transaction:
   - Check for duplicates using database.check_if_expense_exists()
   - If new transaction:
     - Attempt auto-categorization using database.get_auto_match_category()
     - If no auto-match found, prompt user for manual categorization
     - Insert transaction with category/subcategory into database
     - Optionally add merchant to auto-match table for future automation
8. ProcessingResults tracks statistics and provides summary
```

### Online Processing Flow

```
1. User runs: python load-cc-transactions.py --type ws_debit --database finance
2. CLI validates arguments and initializes services
3. TransactionLoader.load() is called with card type (no file path)
4. CardRegistry.get_card_class() returns WealthsimpleDebitStatement class
5. WealthsimpleDebitStatement uses wealthsimpleton API to fetch transactions
6. Transactions are processed and filtered according to business rules
7. Standardized DataFrame is returned
8. TransactionProcessor inserts transactions into database (same as file-based)
```

## Key Features

### 1. Automatic Categorization
- **Merchant Name Matching**: Exact merchant name matching from `merchant_name_auto_match` table
- **Substring Matching**: Partial string matching from `substring_auto_match` table
- **Card-specific Rules**: Some cards (like Rogers) have built-in categorization logic
- **Learning System**: Users can add new merchant-to-category mappings during processing

### 2. Duplicate Detection
- **Primary Key**: Date + Merchant + Cost combination
- **Reimbursement Handling**: Special logic for reimbursement transactions
- **Transfer Detection**: Automatic detection and skipping of internal transfers

### 3. Data Standardization
- **Column Names**: All card types standardize to: `date`, `merchant`, `cost`, `cc_category`
- **Data Types**: Consistent date formats, numeric values, and string handling
- **Filtering**: Removal of payments, transfers, and other non-expense transactions

### 4. Error Handling
- **File Validation**: Checks for file existence and readability
- **Data Validation**: Validates CSV structure and content
- **Database Validation**: Checks for duplicate transactions
- **User Feedback**: Clear error messages and progress reporting

### 5. Extensibility
- **Card Type Registry**: Easy to add new card types by extending base classes
- **Modular Design**: Clear separation between loading, processing, and storage
- **Configuration Management**: Centralized configuration system

## Technical Stack

### Core Technologies
- **Language**: Python 3.x
- **Database**: PostgreSQL
- **Data Processing**: Polars (high-performance DataFrame library)
- **ORM**: psycopg (PostgreSQL adapter)
- **Configuration**: python-dotenv
- **API Integration**: requests, wealthsimpleton
- **Excel Processing**: fastexcel

### Development Tools
- **Code Quality**: pre-commit, ruff
- **Testing**: (not explicitly shown in codebase)
- **CI/CD**: GitHub Actions (based on `.github/workflows/ci.yml`)

## Business Logic

### Transaction Filtering Rules

**Common Filters**:
- Remove payment transactions (contain "PAYMENT" in merchant name)
- Remove transfer transactions (contain "Tfr=" or "TFR-TO")
- Remove credit/refund transactions (null or negative cost values)

**Wealthsimple-specific Filters**:
- Remove internal transfers between Wealthsimple accounts
- Remove bill payments to other credit cards
- Remove specific e-transfer patterns
- Remove credit card payments

### Categorization Logic

**Priority Order**:
1. Card-specific categorization (e.g., Rogers CC categories)
2. Exact merchant name matching
3. Substring matching
4. Manual user input

**Special Cases**:
- Reimbursement transactions require additional duplicate checking
- Interac e-Transfer transactions are handled specially
- Some merchants are automatically skipped

## Usage Examples

### Single File Processing
```bash
python load-cc-transactions.py --type cibc_mc --filepath statement.csv --database finance
```

### Multiple Files Processing
```bash
python load-cc-transactions.py --type cibc_mc --folder statements/ --database finance
```

### Online Source Processing
```bash
python load-cc-transactions.py --type ws_debit --database finance
```

### Parents Finance Excel Processing
```bash
python load-excel-transactions.py --filepath parents_statement.xlsx --cron
```

## Error Handling and Debugging

### Common Issues

1. **File Not Found**: Ensure file paths are correct and files exist
2. **Invalid Card Type**: Check supported card types with `--help`
3. **Database Connection**: Verify PostgreSQL connection string in `.env`
4. **Duplicate Transactions**: System automatically detects and skips duplicates
5. **Categorization Failures**: User will be prompted for manual input

### Debug Mode

Set `debug=True` in configuration or use debug flags to get detailed logging:
- Database queries and parameters
- Configuration values
- Data processing steps
- Error stack traces

## Extending the System

### Adding New Card Types

1. **Create new statement class**: Extend `FileBasedCardStatement` or `OnlineCardStatement`
2. **Implement load_data()**: Parse raw data into standardized DataFrame format
3. **Register card type**: Add entry to `CARD_TYPES` dictionary in card_registry.py
4. **Add categorization logic**: (Optional) Add auto-match rules or card-specific logic

### Example: Adding New File-based Card

```python
# classes/cc/new_card.py
from classes.cc.generics.file_based_card_statement import FileBasedCardStatement
import polars as pl

class NewCardStatement(FileBasedCardStatement):
    def __init__(self, file_path: str):
        super().__init__(type="new_card", file_path=file_path)

    def load_data(self) -> None:
        # Read CSV file
        df = pl.read_csv(source=self.file_path, has_header=True)
        
        # Process data according to card format
        df_processed = df.rename({
            "transaction_date": "date",
            "description": "merchant", 
            "amount": "cost"
        })
        
        # Add cc_category column
        df_processed = df_processed.with_columns(pl.lit(None).alias("cc_category"))
        
        # Standardize date format
        df_processed = df_processed.with_columns(
            pl.col("date").str.to_date(format="%Y-%m-%d")
        )
        
        self.df = df_processed

# Register in card_registry.py
CARD_TYPES["new_card"] = {
    "class": NewCardStatement,
    "requires_file": True,
    "description": "New Card Type",
}
```

## Database Schema

### Main Tables

**expenses**:
- `id`: Primary key
- `date`: Transaction date
- `merchant`: Merchant name
- `cost`: Transaction amount
- `category_id`: Foreign key to categories
- `subcategory_id`: Foreign key to subcategories

**categories**:
- `id`: Primary key
- `name`: Category name (e.g., "Food", "Transportation")

**subcategories**:
- `id`: Primary key
- `name`: Subcategory name (e.g., "Groceries", "Restaurants")
- `category_id`: Foreign key to categories

**merchant_name_auto_match**:
- `merchant_name`: Merchant name
- `merchant_category`: Category name
- `merchant_subcategory`: Subcategory name

**substring_auto_match**:
- `substring`: Partial string to match
- `merchant_category`: Category name
- `merchant_subcategory`: Subcategory name

## Performance Considerations

### Data Processing
- **Polars**: Used for high-performance DataFrame operations
- **Batch Processing**: Files are processed in batches for efficiency
- **Memory Management**: Large files are processed incrementally

### Database Operations
- **Connection Pooling**: psycopg handles connection management
- **Batch Inserts**: Transactions are inserted individually for data integrity
- **Indexing**: Database tables should be properly indexed for performance

## Security Considerations

### Sensitive Data
- **Environment Variables**: Database credentials stored in `.env` file
- **Configuration**: `.env` file should be excluded from version control
- **API Keys**: Wealthsimple API links contain sensitive information

### Data Protection
- **File Handling**: Temporary files are cleaned up after processing
- **Error Reporting**: Sensitive information is obscured in error messages
- **Access Control**: Database permissions should be properly configured

## Future Enhancements

### Potential Improvements

1. **Batch Processing**: Add support for batch database inserts
2. **Parallel Processing**: Process multiple files concurrently
3. **Enhanced Categorization**: Machine learning for automatic categorization
4. **Web Interface**: Replace CLI with web-based interface
5. **Mobile App**: Mobile companion app for transaction entry
6. **Budgeting Features**: Add budget tracking and alerts
7. **Reporting**: Enhanced reporting and visualization
8. **Multi-currency Support**: Handle foreign currency transactions
9. **Recurring Transaction Detection**: Identify and categorize recurring expenses
10. **API Integration**: REST API for third-party integrations

## Troubleshooting Guide

### Common Error Messages

**"File not found"**: Check file path and permissions
**"Invalid card type"**: Verify card type is supported (use `--help`)
**"Database connection failed"**: Check PostgreSQL connection string and server status
**"Duplicate transaction"**: System automatically skips duplicates
**"Manual categorization required"**: User input needed for new merchant

### Debugging Steps

1. **Enable debug mode**: Set debug flags to get detailed logging
2. **Check file formats**: Verify CSV files match expected format
3. **Validate database**: Ensure database tables and schema are correct
4. **Review logs**: Check console output for error details
5. **Test with small files**: Start with small test files to isolate issues

## Best Practices

### Development
- **Modular Design**: Keep components loosely coupled
- **Type Hints**: Use Python type hints for better code clarity
- **Documentation**: Maintain comprehensive docstrings
- **Testing**: Add unit tests for new functionality
- **Code Quality**: Use pre-commit hooks and linters

### Usage
- **Backup Data**: Regularly backup database before major operations
- **Test New Cards**: Test new card types with small datasets first
- **Monitor Performance**: Watch for performance issues with large files
- **Update Regularly**: Keep dependencies up to date
- **Document Changes**: Maintain changelog for significant modifications

## Conclusion

This personal finance system provides a robust, extensible framework for managing credit card transactions from multiple financial institutions. Its modular design, comprehensive error handling, and automatic categorization features make it a powerful tool for personal financial management. The system can be easily extended to support additional card types and enhanced with new features as needed.