import base64
import datetime
import os
import stat
from urllib.parse import unquote, urlsplit, urlunsplit

import polars as pl
import requests

from sources.base import OnlineCardStatement

# Path to persist the one-time-claimed Access URL.
# The Access URL embeds Basic Auth credentials, so the file is chmod 600.
ACCESS_URL_FILE = os.path.expanduser("~/.simplefin_access_url")

BRIDGE_CREATE_URL = "https://beta-bridge.simplefin.org/simplefin/create"

# (connect, read) timeouts in seconds for outbound SimpleFIN Bridge calls.
REQUEST_TIMEOUT = (5, 30)


def _is_https_url(url: str) -> bool:
    """Return whether a URL has an HTTPS scheme and hostname."""
    try:
        parsed = urlsplit(url)
        return parsed.scheme.lower() == "https" and parsed.hostname is not None
    except ValueError:
        return False


class SimplefinStatement(OnlineCardStatement):
    """Fetch and normalize transactions from the SimpleFIN Bridge (v2 protocol)."""

    def __init__(self):
        super().__init__(type="simplefin")

    def load_data(self) -> None:
        """Claim (or reuse) the access URL, fetch accounts, and build the txn frame."""
        setup_token = self.config.simplefin_setup_token
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
                        "date": datetime.datetime.fromtimestamp(
                            posted, tz=datetime.timezone.utc
                        ).date(),
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
        """Return a cached access URL if present, else claim one from the setup token."""
        if os.path.exists(ACCESS_URL_FILE):
            with open(ACCESS_URL_FILE) as f:
                access_url = f.read().strip()
            if access_url:
                return access_url

        if not setup_token:
            raise ValueError(
                "No cached SimpleFIN access URL found and SIMPLEFIN_SETUP_TOKEN is "
                "not set. Get one at "
                f"{BRIDGE_CREATE_URL} and add it to .env"
            )

        access_url = self._claim_access_url(setup_token)
        with open(ACCESS_URL_FILE, "w") as f:
            f.write(access_url)
        os.chmod(ACCESS_URL_FILE, stat.S_IRUSR | stat.S_IWUSR)
        return access_url

    def _claim_access_url(self, setup_token: str) -> str:
        """Decode the one-time setup token and POST to claim the access URL."""
        claim_url = base64.b64decode(setup_token).decode()
        if not _is_https_url(claim_url):
            raise ValueError("Invalid SimpleFIN setup token: claim URL must use HTTPS")
        response = requests.post(claim_url, timeout=REQUEST_TIMEOUT)
        if response.status_code == 403:
            raise ValueError(
                "SimpleFIN setup token already claimed or invalid. "
                f"Get a new one at {BRIDGE_CREATE_URL}"
            )
        response.raise_for_status()
        return response.text.strip()

    def _parse_access_url(self, access_url: str) -> tuple[str, str, str]:
        """Split an access URL into (base_url, username, password)."""
        # Access URL form: https://user:pass@host/path
        error_msg = (
            "Invalid SimpleFIN access URL: expected format https://user:pass@host/path"
        )
        try:
            parsed = urlsplit(access_url)
        except ValueError as err:
            raise ValueError(error_msg) from err
        if (
            not _is_https_url(access_url)
            or parsed.username is None
            or parsed.password is None
        ):
            raise ValueError(error_msg)
        base_url = urlunsplit(
            (
                parsed.scheme,
                parsed.netloc.rsplit("@", 1)[-1],
                parsed.path,
                parsed.query,
                "",
            )
        )
        return base_url, unquote(parsed.username), unquote(parsed.password)

    def _fetch_accounts(self, base_url: str, username: str, password: str) -> dict:
        """GET /accounts with Basic Auth and map 402/403 to actionable errors."""
        response = requests.get(
            f"{base_url}/accounts",
            auth=(username, password),
            params={"version": "2"},
            timeout=REQUEST_TIMEOUT,
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
