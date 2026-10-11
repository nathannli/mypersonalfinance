#!/usr/bin/env python3
"""Source-agnostic helpers shared by the account-activity downloaders.

Amex, Wealthsimple and Simplii all follow one shape: download an account-activity
CSV, validate it through the registry parser, and print the loader handoff. This
module holds the parts that do not depend on which bank produced the file.
"""

import logging
import re
import shlex
import shutil
import time
from datetime import datetime
from pathlib import Path

import messages

from sources.registry import get_card_class

logger = logging.getLogger("browser_common")

STANDARDIZED_COLUMNS = ["date", "merchant", "cost", "cc_category"]
MFA_CODE_PATTERN = re.compile(r"\b(\d{6})\b")
CARD_TYPE_LABELS = {
    "amex": "Amex",
    "ws_debit": "Wealthsimple",
    "ws_credit": "Wealthsimple",
    "simplii_visa": "Simplii",
    "simplii_debit": "Simplii",
}


def sanitize_filename(suggested_filename: str) -> str:
    filename = Path(suggested_filename).name
    filename = re.sub(r"[^A-Za-z0-9._-]+", "_", filename).strip("._")
    if not filename:
        raise ValueError("The bank supplied an unusable download filename")
    return filename


def move_download(
    download: Path,
    output_dir: Path,
    destination_name: str | None = None,
    *,
    delete_source: bool = True,
) -> Path:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / sanitize_filename(destination_name or download.name)
    try:
        with destination.open("xb") as reserved, download.open("rb") as source:
            shutil.copyfileobj(source, reserved)
    except FileExistsError:
        raise FileExistsError(f"Refusing to overwrite existing file: {destination}")
    except OSError:
        destination.unlink(missing_ok=True)
        raise
    if delete_source:
        download.unlink()
    return destination


def validate_download(path: Path, card_type: str = "amex") -> None:
    columns = get_card_class(card_type)(str(path)).get_df().columns
    if columns != STANDARDIZED_COLUMNS:
        label = CARD_TYPE_LABELS.get(card_type, card_type)
        raise RuntimeError(
            f"Downloaded {label} file has unsupported standardized columns: {columns}"
        )


def loader_command(path: Path, database: str, card_type: str) -> str:
    return shlex.join(
        [
            "uv",
            "run",
            "--frozen",
            "python",
            "load-transactions.py",
            "--type",
            card_type,
            "--filepath",
            str(path),
            "--database",
            database,
        ]
    )


def wait_for_sms_code(
    markers: tuple[str, ...],
    requested_at: datetime,
    *,
    source: str,
    initial_delay_seconds: float,
    timeout_seconds: float,
    pattern: re.Pattern[str] = MFA_CODE_PATTERN,
) -> str:
    try:
        database = messages.get_db()
    except PermissionError as exc:
        raise RuntimeError(
            "macos-messages needs Full Disk Access for this terminal application"
        ) from exc

    sms_chat_ids = [chat.id for chat in database.chats(service="SMS", limit=100)]
    if not sms_chat_ids:
        raise RuntimeError("macos-messages found no SMS conversations")

    logger.info(
        "Waiting %s seconds before checking Messages for MFA code...",
        initial_delay_seconds,
    )
    time.sleep(initial_delay_seconds)
    logger.info("Waiting for recent %s MFA code in Messages...", source)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        for message in reversed(
            list(
                database.messages(
                    chat_ids=sms_chat_ids,
                    after=requested_at,
                    limit=25,
                    include_unsent=False,
                    include_unknown_senders=True,
                )
            )
        ):
            text = message.text or ""
            if message.is_from_me or not any(
                marker in text.lower() for marker in markers
            ):
                continue
            if code := pattern.search(text):
                logger.info("Received recent %s MFA code.", source)
                return code.group(1)
        time.sleep(0.5)
    raise RuntimeError(f"Timed out waiting for a recent {source} SMS verification code")
