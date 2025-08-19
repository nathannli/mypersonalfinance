from abc import ABC, abstractmethod

import polars as pl


class OnlineCardStatement(ABC):
    type: str
    df: pl.DataFrame

    def __init__(self, type: str):
        self.type = type
        self.load_data()

    @abstractmethod
    def load_data(self) -> None:
        pass

    def get_df(self) -> pl.DataFrame:
        return self.df
