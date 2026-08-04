import os

from dotenv import load_dotenv


class Config:
    postgres_connection_string: str
    ws_debt_link: str
    ws_credit_link: str
    debug: bool

    def __init__(self, debug: bool = False):
        # .env lives next to config.py at project root
        project_root = os.path.dirname(os.path.abspath(__file__))
        dotenv_path = os.path.join(project_root, ".env")
        load_dotenv(dotenv_path=dotenv_path)
        self.postgres_connection_string = os.getenv("POSTGRES_CONNECTION_STRING")
        self.ws_debt_link = os.getenv("WS_DEBIT_LINK")
        self.ws_credit_link = os.getenv("WS_CREDIT_LINK")
        self.debug = debug
        if self.debug:
            print(f"{self.postgres_connection_string=}")
            print(f"{self.ws_debt_link=}")
            print(f"{self.ws_credit_link=}")
