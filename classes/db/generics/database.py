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
    pl.Config.set_tbl_rows(900)

    database_name: str
    debug: bool
    uri: str

    def __init__(self, database_name: str, debug: bool = False):
        """
        Initialize the PostgresDB instance.
        """
        # Get the current working directory and find .env file
        current_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.abspath(os.path.join(current_dir, "../../.."))
        dotenv_path = os.path.join(project_root, ".env")
        print(f"{dotenv_path=}")
        load_dotenv(dotenv_path=dotenv_path)
        self.database_name = database_name
        self.uri = f"{os.getenv("POSTGRES_CONNECTION_STRING")}/{self.database_name}"
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

    def select(self, query: str, args: tuple | None = None) -> list[tuple]:
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
