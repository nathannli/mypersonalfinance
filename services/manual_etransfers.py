"""Statement rows that need a transaction-specific answer from the user."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from services.transaction_categorization import (
    amount_to_minor_units,
    normalize_context_text,
)

MANUAL_CONTEXT_PREFIXES: tuple[str, ...] = ("interac e-transfer:",)


# Transfers, cheques, drafts and cash in parents bank descriptors. `chq` and
# `w/d` must start a token, so an ordinary merchant name containing them passes.
PARENTS_MANUAL_CONTEXT = re.compile(
    r"e-tfr|e-transfer|tfr-to|tfr-fr|tfr to |cash with|cad draft|draft #"
    r"|(?<![a-z0-9])(?:chq|w/d)"
)


def needs_manual_context(normalized_merchant: str, database: str = "finance") -> bool:
    if database == "finance":
        return normalized_merchant.startswith(MANUAL_CONTEXT_PREFIXES)
    if database == "parents_finance":
        return (
            normalized_merchant.startswith(MANUAL_CONTEXT_PREFIXES)
            or PARENTS_MANUAL_CONTEXT.search(normalized_merchant) is not None
        )
    raise ValueError(f"Unsupported database: {database}")


@dataclass(frozen=True)
class ManualTransfer:
    date: date
    merchant: str
    amount_minor_units: int

    @property
    def cost(self) -> Decimal:
        return Decimal(self.amount_minor_units) / 100

    @property
    def transaction_id(self) -> str:
        identity = json.dumps(
            [self.date.isoformat(), self.merchant, self.amount_minor_units],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, str]:
        return {
            "transaction_id": self.transaction_id,
            "date": self.date.isoformat(),
            "merchant": self.merchant,
            "cost": f"{self.cost:.2f}",
        }


def statement_transfers(
    rows: Iterable[Mapping[str, object]],
    database: str = "finance",
) -> tuple[ManualTransfer, ...]:
    """Keep separate transactions, deduplicating overlapping statement exports."""
    transfers: dict[str, ManualTransfer] = {}
    for row in rows:
        merchant = row.get("merchant")
        if not isinstance(merchant, str) or not needs_manual_context(
            normalize_context_text(merchant), database
        ):
            continue
        transaction_date = row.get("date")
        if not isinstance(transaction_date, date):
            raise ValueError(f"Invalid e-transfer date: {transaction_date!r}")
        transfer = ManualTransfer(
            transaction_date, merchant, amount_to_minor_units(row.get("cost"))
        )
        transfers.setdefault(transfer.transaction_id, transfer)
    return tuple(
        sorted(
            transfers.values(), key=lambda item: (item.date, item.merchant, item.cost)
        )
    )
