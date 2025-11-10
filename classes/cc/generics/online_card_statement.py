from abc import ABC, abstractmethod

import polars as pl

from classes.config import Config


class OnlineCardStatement(ABC):
    type: str
    df: pl.DataFrame
    config: Config

    def __init__(self, type: str):
        self.type = type
        self.config = Config(debug=True)
        self.load_data()

    @abstractmethod
    def load_data(self) -> None:
        pass

    def get_df(self) -> pl.DataFrame:
        return self.df
