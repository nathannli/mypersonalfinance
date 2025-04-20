from datetime import date
from classes.db.generics.finance_db import FinanceDB
import polars as pl

class ParentsFinanceDB(FinanceDB):

    def __init__(self, debug: bool = False):
        super().__init__(database_name="parents_finance", debug=debug)

    def get_category(self) -> pl.DataFrame:
        """
        Get all categories.
        """
        query = "select id, name as category from categories"
        return pl.DataFrame(self.select(query), schema={"id": pl.Int64, "category": pl.Utf8}, orient="row")

    def get_category_name_from_id(self, category_id: int) -> str:
        """
        Get the category name from the category id.
        """
        query = "select name as category from categories where id = %s"
        return self.select(query, (category_id,))[0][0]

    def get_category_id_from_name(self, category_name: str) -> int:
        """
        Get the category id from the category name.
        """
        query = "select id from categories where lower(name) = lower(%s)"
        result = self.select(query, (category_name,))
        if len(result) == 1:
            return result[0][0]
        elif len(result) > 1:
            print(f"Multiple categories found for {category_name}. Something is wrong.")
            raise ValueError(f"Multiple categories found for {category_name}. Something is wrong.")
        else:
            return None

    def insert_expense(self, date: date, merchant: str, cost: float, cc_category: str | None = None) -> None:
        """
        Insert an expense into the database.
        Ask the user to select a category for the expense.
        """
        print(f"Transaction on {date} at {merchant} for {cost}")

        # Try to get category_id from cc_category or merchant, or ask user if both fail
        found_match = False
        category_id = None
        if cc_category is not None:
            category_id = self.get_category_id_from_name(cc_category)
            if category_id is not None:
                found_match = True

        if category_id is None:
            category = self.get_auto_match_category(merchant)
            if category:
                category_id = self.get_category_id_from_name(category)
                found_match = True

        if category_id is None:
            # Ask user to select category
            df = self.get_category()
            print(df)
            while True:
                category_id = input("Enter the category id: ")
                try:
                    category_id = int(category_id)
                    if category_id in df['id'].to_list():
                        break
                    else:
                        print(f"Category ID {category_id} not found. Please enter a valid category ID.")
                except ValueError:
                    print("Please enter a valid integer for category ID.")

        # insert the expense
        query = "insert into expenses (date, merchant, cost, category_id) values (%s, %s, %s, %s)"
        self.insert(query, (date, merchant, cost, category_id))

        # ask the user if they want to add the merchant to the auto_match table
        if not found_match:
            while True:
                add_to_auto_match = input("Add to auto_match table? (y/n): ")
                if add_to_auto_match == "y":
                    # get merchant name substring from user input
                    merchant_name_substring = input("Enter the merchant name substring: ")
                    self.insert_into_auto_match(merchant_name_substring, self.get_category_name_from_id(category_id))
                    break
                elif add_to_auto_match == "n":
                    break
                else:
                    print("Please enter a valid response (y/n).")

    def get_auto_match_category(self, merchant_name: str) -> str | None:
        """
        Get the category for the merchant.
        """
        query = "select merchant_name, merchant_category from auto_match"
        result_dict = {row[0]: row[1] for row in self.select(query)}
        match_results = list()
        for k,v in result_dict.items():
            if k in merchant_name.lower():
                match_results.append(v)
        if len(match_results) > 1:
            raise ValueError(f"Multiple categories found for {merchant_name}. Something is wrong.")
        elif len(match_results) == 1:
            return match_results[0]
        else:
            return None

    def insert_into_auto_match(self, merchant_name: str, merchant_category: str) -> None:
        """
        Insert a new merchant into the auto_match table.
        """
        query = "insert into auto_match (merchant_name, merchant_category) values (%s, %s)"
        self.insert(query, (merchant_name.lower(), merchant_category))
