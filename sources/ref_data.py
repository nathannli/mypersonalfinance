manual_cc_merchant_category_ref: dict[str, tuple[str, str]] = {
    "Travel": ("Travel", "Travel"),
}

rogers_cc_merchant_category_ref: dict[str, tuple[str, str]] = {
    "Eating Places and Restaurants": ("Food", "Eating Out"),
    "Grocery Stores and Supermarkets": ("Food", "Grocery"),
    "Miscellaneous Food Stores-Convenience Stores and Specialty Markets": (
        "Food",
        "Grocery",
    ),
    "Local and Suburban Commuter Passenger Transportation, including Ferries": (
        "Commuting",
        "Transit",
    ),
}

reimbursement_merchant_ref: list[str] = [
    "NEXUS MASSAGE AND REHAB TORONTO",
    # PS5s bought for a friend, fully reimbursed (user direction 2026-09-17)
    "STAPLES.CA/48517198128  MISSISSAUGA",
    "STAPLES.CA/48620392128  MISSISSAUGA",
]

simplii_visa_cc_merchant_name_to_category_ref: dict[str, tuple[str, str]] = {
    "Restaurants": ("Food", "Eating Out"),
}
