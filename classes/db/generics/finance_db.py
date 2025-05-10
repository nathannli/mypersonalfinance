from abc import abstractmethod
from datetime import date
from classes.db.generics.database import PostgresDB
from typing import Any

class FinanceDB(PostgresDB):

    def __init__(self, database_name: str, debug: bool = False):
        super().__init__(database_name=database_name, debug=debug)

    @abstractmethod
    def insert_expense(self, date: date, merchant: str, cost: float) -> None:
        """
        Insert an expense into the database.
        Ask the user to select a category and subcategory for the expense.
        """
        return NotImplementedError

    @abstractmethod
    def get_auto_match_category(self, merchant: str):
        """
        Get the category and subcategory for the merchant.
        """
        return NotImplementedError

    @abstractmethod
    def insert_into_auto_match(self, merchant: str, category: str, subcategory: str) -> None:
        """
        Insert a new merchant into the auto_match table.
        """
        return NotImplementedError

    def _check_exists(self, table: str, filters: dict[str, Any]) -> bool:
        """
        Generic method to check if a row exists in the database table.
        """
        conditions = " AND ".join(f"{key} = %s" for key in filters)
        query = f"SELECT id FROM {table} WHERE {conditions}"
        params = tuple(filters.values())
        return len(self.select(query, params)) > 0

    def check_if_expense_exists(self, date: date, merchant: str, cost: float) -> bool:
        return self._check_exists("expenses", {"date": date, "merchant": merchant, "cost": cost})

    def get_expense_id(self, date: date, merchant: str, cost: float) -> int:
        """
        Get the id of an expense in the database.
        """
        query = "select id from expenses where date = %s and merchant = %s and cost = %s"
        result = self.select(query, (date, merchant, cost))
        return result[0][0]

    def delete_expense(self, expense_id: int) -> None:
        """
        Delete an expense from the database.
        """
        query = "delete from expenses where id = %s"
        self.insert(query, (expense_id,))
