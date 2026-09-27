"""
Transaction Loader Service - Load credit card transaction data.

This module provides a service class for loading transaction data from
various credit card statement sources.
"""

import os

import polars as pl

from sources.registry import get_card_class, requires_file

# Statement files only. A folder handed to the loader is very often also where
# someone redirected a debug run's output, and an unfiltered listing fed that
# log to polars: the research entry point aborted before making any TinyFish
# call, and the load entry point processed the real statements and still exited
# 1. ``amex`` additionally falls back to Excel for a non-``.csv`` extension, so
# the spreadsheet formats have to stay in the allowlist.
STATEMENT_EXTENSIONS = frozenset({".csv", ".xlsx", ".xls"})


def statement_files(folder_path: str) -> list[str]:
    """List the statement files in a folder, sorted, skipping everything else.

    Shared by the load and research entry points so both agree on what counts
    as a statement, and so a stray log is reported rather than parsed. Lives
    here rather than in the loader CLI because the research review CLI must
    stay importable without ``Config`` or ``db`` (V3/V35).

    Args:
        folder_path: Directory to scan

    Returns:
        Sorted paths of the files that look like statements

    Raises:
        ValueError: If the folder is missing, holds no files, or holds no
            statement files
    """

    if not os.path.isdir(folder_path):
        raise ValueError(f"Folder does not exist: {folder_path}")

    regular_files = sorted(
        name
        for name in os.listdir(folder_path)
        if os.path.isfile(os.path.join(folder_path, name))
    )
    if not regular_files:
        raise ValueError(f"Folder is empty: {folder_path}")

    selected = [
        os.path.join(folder_path, name)
        for name in regular_files
        if os.path.splitext(name)[1].lower() in STATEMENT_EXTENSIONS
    ]
    if not selected:
        skipped = ", ".join(regular_files)
        raise ValueError(
            f"No statement files in {folder_path}: expected one of "
            f"{', '.join(sorted(STATEMENT_EXTENSIONS))}, found {skipped}"
        )

    skipped = [
        name
        for name in regular_files
        if name not in {os.path.basename(p) for p in selected}
    ]
    if skipped:
        print(
            f"Skipped {len(skipped)} non-statement file(s) in {folder_path}: {', '.join(skipped)}"
        )
    return selected


class TransactionLoader:
    """
    Service for loading credit card transaction data.

    Uses the card registry to dynamically load the appropriate
    statement class for each card type.
    """

    def load(self, card_type: str, file_path: str | None = None) -> pl.DataFrame:
        """
        Load credit card statement data based on card type.

        Args:
            card_type: Type of credit card
            file_path: Path to the transaction data file (not needed for online card types)

        Returns:
            pl.DataFrame: Loaded transaction data with standardized columns

        Raises:
            ValueError: If invalid card type or missing file path
        """
        # Get the appropriate statement class from the registry
        statement_class = get_card_class(card_type)

        # Instantiate and load data based on whether file is required
        if requires_file(card_type):
            if file_path is None:
                raise ValueError(f"Card type {card_type} requires a file input")
            return statement_class(file_path=file_path).get_df()
        else:
            return statement_class().get_df()

    def load_from_file(self, card_type: str, file_path: str) -> pl.DataFrame:
        """
        Load transaction data from a specific file.

        Convenience method for file-based card types.

        Args:
            card_type: Type of credit card
            file_path: Path to the transaction data file

        Returns:
            pl.DataFrame: Loaded transaction data

        Raises:
            ValueError: If card type doesn't support file input
        """
        if not requires_file(card_type):
            raise ValueError(
                f"Card type {card_type} does not support file input (uses online source)"
            )
        return self.load(card_type, file_path)

    def load_from_online(self, card_type: str) -> pl.DataFrame:
        """
        Load transaction data from online source.

        Convenience method for online card types.

        Args:
            card_type: Type of credit card

        Returns:
            pl.DataFrame: Loaded transaction data

        Raises:
            ValueError: If card type doesn't support online source
        """
        if requires_file(card_type):
            raise ValueError(
                f"Card type {card_type} requires a file input (not online)"
            )
        return self.load(card_type)
