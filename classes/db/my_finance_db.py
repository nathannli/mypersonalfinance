from datetime import date
import polars as pl
from classes.db.generics.finance_db import FinanceDB
from classes.cc.rogers import RogersStatement
from classes.cc.amex import AmexStatement

class MyFinanceDB(FinanceDB):

    def __init__(self, debug: bool = False):
        super().__init__(database_name="finance", debug=debug)

    def get_subcategory_id_from_name(self, subcategory_name: str) -> int:
        """
        Get the subcategory id from the subcategory name.
        """
        query = "select id from subcategories where name = %s"
        return self.select(query, (subcategory_name,))[0][0]

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
        query = "select subcategories.id as subcategory_id, subcategories.name as subcategory, categories.name as category from subcategories join categories on subcategories.category_id = categories.id"
        schema = {
            "subcategory_id": pl.Int64,
            "subcategory": pl.Utf8,
            "category": pl.Utf8
        }
        return pl.DataFrame(self.select(query), schema=schema, orient="row")

    def insert_expense(self, date: date, merchant: str, cost: float, card_type: str, cc_category: str | None = None) -> None:
        print(f"Transaction on {date} at {merchant} for {cost}")
        # try auto match
        if card_type == "rogers" and cc_category is not None:
            ref_category_tuple = RogersStatement.auto_match_category(cc_category)
        elif card_type == "amex":
            ref_category_tuple = AmexStatement.auto_match_category(merchant)
        else:
            raise ValueError(f"Invalid card type: {card_type}")
        if ref_category_tuple is not None:
            category, subcategory = ref_category_tuple
            print(f"Ref Category: {category}, Ref Subcategory: {subcategory}")
            subcategory_id = self.get_subcategory_id_from_name(subcategory)
            category_id = self.get_category_id_from_subcategory_id(subcategory_id)
            query = "insert into expenses (date, merchant, cost, category_id, subcategory_id) values (%s, %s, %s, %s, %s)"
            self.insert(query, (date, merchant, cost, category_id, subcategory_id))
            return
        # else user input
        df = self.get_subcategory_and_category()
        print("\n\n")
        print(df)
        print("\n\n")
        subcategory_id = input("Enter the subcategory id: ")
        category_id = self.get_category_id_from_subcategory_id(subcategory_id)
        query = "insert into expenses (date, merchant, cost, category_id, subcategory_id) values (%s, %s, %s, %s, %s)"
        self.insert(query, (date, merchant, cost, category_id, subcategory_id))
