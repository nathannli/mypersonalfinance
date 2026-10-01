"""
Load Credit Card Transactions - Main entry point.

This script loads credit card transaction data from various sources
into a PostgreSQL database with automatic categorization.

Usage:
    python load-transactions.py --type <card_type> [--filepath <file> | --folder <dir>] [--database <db>]

Examples:
    # Single file
    python load-transactions.py --type cibc_mc --filepath statement.csv

    # Multiple files
    python load-transactions.py --type cibc_mc --folder statements/

    # Wealthsimple activity downloaded by scripts/download_wealthsimple_transactions.py
    python load-transactions.py --type ws_debit --filepath ws-debit.csv

For more information, run with --help
"""

if __name__ == "__main__":
    from cli.transaction_loader_cli import TransactionLoaderCLI

    # Initialize CLI handler
    cli = TransactionLoaderCLI()

    # Parse arguments
    args = cli.parser.parse_args()

    # Run the transaction loading process
    cli.run(args)
