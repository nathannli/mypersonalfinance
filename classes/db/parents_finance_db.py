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
        return pl.DataFrame(self.select(query))

    def insert_expense(self, date: date, merchant: str, cost: float) -> None:
        """
        Insert an expense into the database.
        Ask the user to select a category for the expense.
        """
        print(f"Transaction on {date} at {merchant} for {cost}")
        df = self.get_category()
        print(df)
        category_id = input("Enter the category id: ")
        query = "insert into expenses (date, merchant, cost, category_id) values (%s, %s, %s, %s)"
        self.insert(query, (date, merchant, cost, category_id))
