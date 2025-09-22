import os

from dotenv import load_dotenv


class Config:
    postgres_connection_string: str
    ws_debt_link: str
    ws_credit_link: str
    debug: bool

    def __init__(self, debug: bool = False):
        # Get the current working directory and find .env file
        current_dir = os.path.dirname(os.path.abspath(__file__))
        print(f"{current_dir=}")
        current_dir_list = current_dir.split("/")
        current_dir_list.pop(-1)
        current_dir_fixed = "/".join(current_dir_list)
        print(f"{current_dir_fixed=}")
        dotenv_path = os.path.join(current_dir_fixed, ".env")
        print(f"{dotenv_path=}")
        print(f"exists: {os.path.exists(dotenv_path)=}")
        load_dotenv(dotenv_path=dotenv_path)
        self.postgres_connection_string = os.getenv("POSTGRES_CONNECTION_STRING")
        self.ws_debt_link = os.getenv("WS_DEBT_LINK")
        self.ws_credit_link = os.getenv("WS_CREDIT_LINK")
        self.debug = debug
        if self.debug:
            print(f"{self.postgres_connection_string=}")
            print(f"{self.ws_debt_link=}")
            print(f"{self.ws_credit_link=}")
