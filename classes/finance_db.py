from datetime import date
from classes.database import PostgresDB
import polars as pl

class FinanceDB(PostgresDB):
    pl.Config.set_fmt_str_lengths(900)
    pl.Config.set_tbl_width_chars(900)

    def __init__(self, debug: bool = False):
        super().__init__(schema_name="finance", debug=debug)

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
        """
        Insert an expense into the database.
        Ask the user to select a category and subcategory for the expense.
        """
        print(f"Transaction on {date} at {merchant} for {cost}")
        df = self.get_subcategory_and_category()
        print(df)
        subcategory_id = input("Enter the subcategory id: ")
        category_id = self.get_category_id_from_subcategory_id(subcategory_id)
        query = "insert into expenses (date, merchant, cost, category_id, subcategory_id) values (%s, %s, %s, %s, %s)"
        self.insert(query, (date, merchant, cost, category_id, subcategory_id))

    def check_if_expense_exists(self, date: date, merchant: str, cost: float) -> bool:
        """
        Check if an expense exists in the database.
        True if it exists, False otherwise.
        """
        query = "select id from expenses where date = %s and merchant = %s and cost = %s"
        result = self.select(query, (date, merchant, cost))
        print(f"{result=}")
        return len(result) > 0
