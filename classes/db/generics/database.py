from abc import ABC
from dotenv import load_dotenv
import os
import polars as pl
import psycopg


class PostgresDB(ABC):
    """
    Abstract base class for Postgres databases.
    """
    pl.Config.set_fmt_str_lengths(900)
    pl.Config.set_tbl_width_chars(900)
    database_name: str
    debug: bool

    def __init__(self, database_name: str, debug: bool = False):
        """
        Initialize the PostgresDB instance.
        """
        load_dotenv()
        self.uri = os.getenv("POSTGRES_CONNECTION_STRING")
        self.database_name = database_name
        self.debug = debug
        if self.debug:
            print(f"{self.uri=}")
            print(f"{self.database_name=}")

    def connect(self) -> psycopg.Connection:
        """
        Connect to the database.
        """
        return psycopg.connect(self.uri)

    def close(self) -> None:
        """
        Close the database connection.
        """
        self.conn.close()

    def insert(self, query: str, args: tuple) -> None:
        """
        Insert data into the database.
        """
        if self.debug:
            print(f"{query=}")
            print(f"{args=}")
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, args)
                conn.commit()

    def select(self, query: str, args: tuple = None) -> list[tuple]:
        """
        Select data from the database.
        """
        if self.debug:
            print(f"{query=}")
            print(f"{args=}")
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, args)
                return cur.fetchall()
