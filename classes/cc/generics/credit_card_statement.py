from abc import ABC, abstractmethod
import os
import polars as pl


class CreditCardStatement(ABC):
    type: str
    file_path: str
    df: pl.DataFrame

    def __init__(self, type: str, file_path: str):
        self.type = type
        self.file_path = file_path
        self.check_file_exists()

    def check_file_exists(self) -> bool:
        if not os.path.exists(self.file_path):
            raise FileNotFoundError(f"File {self.file_path} not found.")
        return True

    @abstractmethod
    def load_data(self) -> None:
        pass

    def get_df(self) -> pl.DataFrame:
        return self.df
