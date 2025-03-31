from datetime import date
import polars as pl
from classes.db.generics.finance_db import FinanceDB

class MyFinanceDB(FinanceDB):

    def __init__(self, debug: bool = False):
        super().__init__(database_name="finance", debug=debug)

    def get_category_id_from_subcategory_id(self, subcategory_id: int) -> int:
        """
        Get the category id from the subcategory id.
        """
        query = "select category_id from subcategories where id = %s"
        return self.select(query, (subcategory_id,))[0][0]


    def get_subcategory_and_category(self) -> pl.DataFrame:
        """
        Get all subcategories and categories.
        """
        query = "select id, name as subcategory, categories.name as category from subcategories join categories on subcategories.category_id = categories.id"
        return pl.DataFrame(self.select(query))

    def insert_expense(self, date: date, merchant: str, cost: float) -> None:

        print(f"Transaction on {date} at {merchant} for {cost}")
        df = self.get_subcategory_and_category()
        print(df)
        subcategory_id = input("Enter the subcategory id: ")
        category_id = self.get_category_id_from_subcategory_id(subcategory_id)
        query = "insert into expenses (date, merchant, cost, category_id, subcategory_id) values (%s, %s, %s, %s, %s)"
        self.insert(query, (date, merchant, cost, category_id, subcategory_id))
