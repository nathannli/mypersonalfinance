"""
Transaction Loader Service - Load credit card transaction data.

This module provides a service class for loading transaction data from
various credit card statement sources.
"""

import polars as pl

from classes.cc.card_registry import get_card_class, requires_file


class TransactionLoader:
    """
    Service for loading credit card transaction data.

    Uses the card registry to dynamically load the appropriate
    statement class for each card type.
    """

    def load(self, card_type: str, file_path: str = None) -> pl.DataFrame:
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
