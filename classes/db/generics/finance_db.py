from abc import abstractmethod
from datetime import date
from classes.db.generics.database import PostgresDB

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

    def check_if_expense_exists(self, date: date, merchant: str, cost: float) -> bool:
        """
        Check if an expense exists in the database.
        True if it exists, False otherwise.
        """
        query = "select id from expenses where date = %s and merchant = %s and cost = %s"
        result = self.select(query, (date, merchant, cost))
        return len(result) > 0
