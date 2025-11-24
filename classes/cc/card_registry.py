"""
Card Type Registry - Central configuration for all supported credit card types.

This module provides a single source of truth for card type definitions,
eliminating the need to update multiple locations when adding new card types.
"""

from classes.cc.amex import AmexAnnualStatement, AmexStatement
from classes.cc.bmo import BMOStatement
from classes.cc.cibc_mc import CibcMcStatement
from classes.cc.rbc_cc import RbcCcStatement
from classes.cc.rogers import RogersStatement
from classes.cc.simplii_debit import SimpliiDebitStatement
from classes.cc.simplii_visa import SimpliiVisaStatement
from classes.cc.td_visa import TdVisaStatement
from classes.cc.wealthsimple_credit import WealthsimpleCreditStatement
from classes.cc.wealthsimple_debit import WealthsimpleDebitStatement

# Card type registry with metadata
CARD_TYPES = {
    "amex": {
        "class": AmexStatement,
        "requires_file": True,
        "description": "American Express",
    },
    "amex_annual": {
        "class": AmexAnnualStatement,
        "requires_file": True,
        "description": "American Express Annual",
    },
    "rogers": {
        "class": RogersStatement,
        "requires_file": True,
        "description": "Rogers Mastercard",
    },
    "simplii_visa": {
        "class": SimpliiVisaStatement,
        "requires_file": True,
        "description": "Simplii Visa",
    },
    "simplii_debit": {
        "class": SimpliiDebitStatement,
        "requires_file": True,
        "description": "Simplii Debit",
    },
    "bmo": {
        "class": BMOStatement,
        "requires_file": True,
        "description": "BMO",
    },
    "cibc_mc": {
        "class": CibcMcStatement,
        "requires_file": True,
        "description": "CIBC Mastercard",
    },
    "rbc_cc": {
        "class": RbcCcStatement,
        "requires_file": True,
        "description": "RBC Credit Card",
    },
    "td_visa": {
        "class": TdVisaStatement,
        "requires_file": True,
        "description": "TD Visa",
    },
    "ws_debit": {
        "class": WealthsimpleDebitStatement,
        "requires_file": False,
        "description": "Wealthsimple Debit",
    },
    "ws_credit": {
        "class": WealthsimpleCreditStatement,
        "requires_file": False,
        "description": "Wealthsimple Credit",
    },
}


def get_card_type_names() -> list[str]:
    """Get list of all supported card type names."""
    return list(CARD_TYPES.keys())


def get_file_based_card_types() -> set[str]:
    """Get set of card types that require file input."""
    return {name for name, config in CARD_TYPES.items() if config["requires_file"]}


def get_online_card_types() -> set[str]:
    """Get set of card types that don't require file input (online sources)."""
    return {name for name, config in CARD_TYPES.items() if not config["requires_file"]}


def get_card_class(card_type: str):
    """
    Get the statement class for a given card type.

    Args:
        card_type: The card type identifier

    Returns:
        The statement class for the card type

    Raises:
        ValueError: If card type is not found in registry
    """
    if card_type not in CARD_TYPES:
        valid_types = ", ".join(get_card_type_names())
        raise ValueError(
            f"Invalid card type: {card_type}. Valid types are: {valid_types}"
        )
    return CARD_TYPES[card_type]["class"]


def requires_file(card_type: str) -> bool:
    """
    Check if a card type requires file input.

    Args:
        card_type: The card type identifier

    Returns:
        True if the card type requires a file, False otherwise

    Raises:
        ValueError: If card type is not found in registry
    """
    if card_type not in CARD_TYPES:
        raise ValueError(f"Invalid card type: {card_type}")
    return CARD_TYPES[card_type]["requires_file"]
