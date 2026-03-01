"""
Card Type Registry - Central configuration for all supported credit card types.

This module provides a single source of truth for card type definitions,
eliminating the need to update multiple locations when adding new card types.
"""

from importlib import import_module

# Card type registry with metadata
CARD_TYPES = {
    "amex": {
        "module": "classes.cc.amex",
        "class_name": "AmexStatement",
        "requires_file": True,
        "description": "American Express",
    },
    "amex_annual": {
        "module": "classes.cc.amex",
        "class_name": "AmexAnnualStatement",
        "requires_file": True,
        "description": "American Express Annual",
    },
    "rogers": {
        "module": "classes.cc.rogers",
        "class_name": "RogersStatement",
        "requires_file": True,
        "description": "Rogers Mastercard",
    },
    "simplii_visa": {
        "module": "classes.cc.simplii_visa",
        "class_name": "SimpliiVisaStatement",
        "requires_file": True,
        "description": "Simplii Visa",
    },
    "simplii_debit": {
        "module": "classes.cc.simplii_debit",
        "class_name": "SimpliiDebitStatement",
        "requires_file": True,
        "description": "Simplii Debit",
    },
    "bmo": {
        "module": "classes.cc.bmo",
        "class_name": "BMOStatement",
        "requires_file": True,
        "description": "BMO",
    },
    "canadian_tire": {
        "module": "classes.cc.canadian_tire",
        "class_name": "CanadianTireStatement",
        "requires_file": True,
        "description": "Canadian Tire",
    },
    "cibc_mc": {
        "module": "classes.cc.cibc_mc",
        "class_name": "CibcMcStatement",
        "requires_file": True,
        "description": "CIBC Mastercard",
    },
    "rbc_cc": {
        "module": "classes.cc.rbc_cc",
        "class_name": "RbcCcStatement",
        "requires_file": True,
        "description": "RBC Credit Card",
    },
    "td_debit": {
        "module": "classes.cc.td_debit",
        "class_name": "TdDebitStatement",
        "requires_file": True,
        "description": "TD Debit",
    },
    "td_visa": {
        "module": "classes.cc.td_visa",
        "class_name": "TdVisaStatement",
        "requires_file": True,
        "description": "TD Visa",
    },
    "ws_debit": {
        "module": "classes.cc.wealthsimple_debit",
        "class_name": "WealthsimpleDebitStatement",
        "requires_file": False,
        "description": "Wealthsimple Debit",
    },
    "ws_credit": {
        "module": "classes.cc.wealthsimple_credit",
        "class_name": "WealthsimpleCreditStatement",
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
    config = CARD_TYPES[card_type]
    module = import_module(config["module"])
    return getattr(module, config["class_name"])


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
