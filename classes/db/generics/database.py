from abc import ABC

import polars as pl
import psycopg

from classes.config import Config


class PostgresDB(ABC):
    """
    Abstract base class for Postgres databases.
    """

    pl.Config.set_fmt_str_lengths(900)
    pl.Config.set_tbl_width_chars(900)
    pl.Config.set_tbl_rows(900)

    database_name: str
    uri: str

    def __init__(self, database_name: str, debug: bool = False):
        """
        Initialize the PostgresDB instance.
        """
        self.config = Config(debug=debug)
        self.database_name = database_name
        self.uri = f"{self.config.postgres_connection_string}/{self.database_name}"
        if self.config.debug:
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
        if self.config.debug:
            print(f"{query=}")
            print(f"{args=}")
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, args)
                conn.commit()

    def select(self, query: str, args: tuple | None = None) -> list[tuple]:
        """
        Select data from the database.
        """
        if self.config.debug:
            print(f"{query=}")
            print(f"{args=}")
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, args)
                return cur.fetchall()
