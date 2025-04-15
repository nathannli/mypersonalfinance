manual_cc_merchant_category_ref: dict[str, tuple[str, str]] = {
    "Travel": ("Travel", "Travel"),
}

rogers_cc_merchant_category_ref: dict[str, tuple[str, str]] = {
    "Eating Places and Restaurants": ("Food", "Eating Out"),
    "Grocery Stores and Supermarkets": ("Food", "Grocery"),
    "Miscellaneous Food Stores-Convenience Stores and Specialty Markets": ("Food", "Grocery"),
    "Local and Suburban Commuter Passenger Transportation, including Ferries": ("Commuting", "Transit"),
}

amex_cc_merchant_name_to_category_ref: dict[str, tuple[str, str]] = {
    "UBER": ("Commuting", "Rides"),
    "PRESTO": ("Commuting", "Transit"),
    "RUMBLE BOXING": ("Entertainment", "Hobbies"),
    "GOLF": ("Entertainment", "Hobbies"),
}
