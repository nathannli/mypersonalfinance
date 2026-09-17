import os

from dotenv import load_dotenv


class Config:
    postgres_connection_string: str | None
    ws_debt_link: str | None
    ws_credit_link: str | None
    opencodex_base_url: str
    opencodex_api_key: str
    transaction_llm_model: str
    enriched_transaction_llm_model: str
    transaction_llm_timeout_seconds: float
    transaction_llm_mode: str
    debug: bool

    def __init__(self, debug: bool = False):
        # .env lives next to config.py at project root
        project_root = os.path.dirname(os.path.abspath(__file__))
        dotenv_path = os.path.join(project_root, ".env")
        load_dotenv(dotenv_path=dotenv_path)
        self.postgres_connection_string = os.getenv("POSTGRES_CONNECTION_STRING")
        self.ws_debt_link = os.getenv("WS_DEBIT_LINK")
        self.ws_credit_link = os.getenv("WS_CREDIT_LINK")
        self.opencodex_base_url = os.getenv(
            "OPENCODEX_BASE_URL", "http://localhost:10100"
        )
        self.opencodex_api_key = os.getenv("OPENCODEX_API_KEY", "")
        self.transaction_llm_model = os.getenv(
            "TRANSACTION_LLM_MODEL",
            "SingularityApiDev/deepseek-v4-flash-0731",
        )
        # Enriched finance reads its own model so pinning the enriched default
        # never changes TRANSACTION_LLM_MODEL or the parents_finance path (V51).
        self.enriched_transaction_llm_model = os.getenv(
            "ENRICHED_TRANSACTION_LLM_MODEL",
            "anthropic/claude-haiku-4-5",
        )
        timeout_value = os.getenv("TRANSACTION_LLM_TIMEOUT_SECONDS", "120")
        try:
            self.transaction_llm_timeout_seconds = float(timeout_value)
        except ValueError as exc:
            raise ValueError(
                "TRANSACTION_LLM_TIMEOUT_SECONDS must be a number"
            ) from exc
        if self.transaction_llm_timeout_seconds <= 0:
            raise ValueError("TRANSACTION_LLM_TIMEOUT_SECONDS must be positive")
        self.transaction_llm_mode = os.getenv("TRANSACTION_LLM_MODE", "shadow")
        if self.transaction_llm_mode not in {"shadow", "write"}:
            raise ValueError("TRANSACTION_LLM_MODE must be shadow or write")
        self.debug = debug
        if self.debug:
            print("postgres_connection_string=<redacted>")
            print(f"{self.ws_debt_link=}")
            print(f"{self.ws_credit_link=}")
