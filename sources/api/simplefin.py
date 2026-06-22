import base64
import datetime
import os
import stat

import polars as pl
import requests

from sources.base import OnlineCardStatement

# Path to persist the one-time-claimed Access URL.
# The Access URL embeds Basic Auth credentials, so the file is chmod 600.
ACCESS_URL_FILE = os.path.expanduser("~/.simplefin_access_url")

BRIDGE_CREATE_URL = "https://beta-bridge.simplefin.org/simplefin/create"


class SimplefinStatement(OnlineCardStatement):
    def __init__(self):
        super().__init__(type="simplefin")

    def load_data(self) -> None:
        setup_token = self.config.simplefin_setup_token
        if not setup_token:
            raise ValueError(
                "SIMPLEFIN_SETUP_TOKEN not set. Get one at "
                f"{BRIDGE_CREATE_URL} and add it to .env"
            )

        access_url = self._get_access_url(setup_token)
        base_url, username, password = self._parse_access_url(access_url)
        data = self._fetch_accounts(base_url, username, password)

        rows = []
        for account in data.get("accounts", []):
            for txn in account.get("transactions", []):
                posted = txn.get("posted", 0)
                if not posted:
                    # Pending or unposted transaction; skip — it'll settle and
                    # appear as posted in a later fetch.
                    continue
                rows.append(
                    {
                        "date": datetime.datetime.fromtimestamp(posted).date(),
                        "merchant": txn.get("description", ""),
                        # Protocol: positive amount = deposit. Negate for the
                        # project's expense convention (positive cost = expense).
                        "cost": -float(txn.get("amount", "0")),
                        "cc_category": None,
                    }
                )

        self.df = pl.DataFrame(
            rows,
            schema={
                "date": pl.Date,
                "merchant": pl.Utf8,
                "cost": pl.Float64,
                "cc_category": pl.Utf8,
            },
        )

    def _get_access_url(self, setup_token: str) -> str:
        if os.path.exists(ACCESS_URL_FILE):
            with open(ACCESS_URL_FILE) as f:
                access_url = f.read().strip()
            if access_url:
                return access_url

        access_url = self._claim_access_url(setup_token)
        with open(ACCESS_URL_FILE, "w") as f:
            f.write(access_url)
        os.chmod(ACCESS_URL_FILE, stat.S_IRUSR | stat.S_IWUSR)
        return access_url

    def _claim_access_url(self, setup_token: str) -> str:
        claim_url = base64.b64decode(setup_token).decode()
        response = requests.post(claim_url)
        if response.status_code == 403:
            raise ValueError(
                "SimpleFIN setup token already claimed or invalid. "
                f"Get a new one at {BRIDGE_CREATE_URL}"
            )
        response.raise_for_status()
        return response.text.strip()

    def _parse_access_url(self, access_url: str) -> tuple[str, str, str]:
        # Access URL form: https://user:pass@host/path
        scheme, rest = access_url.split("//", 1)
        auth, rest = rest.split("@", 1)
        username, password = auth.split(":", 1)
        base_url = f"{scheme}//{rest}"
        return base_url, username, password

    def _fetch_accounts(self, base_url: str, username: str, password: str) -> dict:
        response = requests.get(
            f"{base_url}/accounts",
            auth=(username, password),
            params={"version": "2"},
        )
        if response.status_code == 402:
            raise ValueError("SimpleFIN: payment required")
        if response.status_code == 403:
            raise ValueError(
                f"SimpleFIN access revoked. Get a new token at {BRIDGE_CREATE_URL}"
            )
        response.raise_for_status()
        data = response.json()

        # Protocol requires showing errors to the end user.
        for err in data.get("errlist", []):
            print(f"SimpleFIN error: {str(err.get('msg', '')).strip()}")
        # Deprecated in v2 but still emitted by older servers.
        for msg in data.get("errors", []):
            print(f"SimpleFIN error: {str(msg).strip()}")

        return data
