"""Statement e-transfers that need a transaction-specific answer from the user."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from services.transaction_categorization import (
    amount_to_minor_units,
    normalize_context_text,
)

MANUAL_CONTEXT_PREFIXES: tuple[str, ...] = ("interac e-transfer:",)


def needs_manual_context(normalized_merchant: str) -> bool:
    return normalized_merchant.startswith(MANUAL_CONTEXT_PREFIXES)


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
) -> tuple[ManualTransfer, ...]:
    """Keep separate transactions, deduplicating overlapping statement exports."""
    transfers: dict[str, ManualTransfer] = {}
    for row in rows:
        merchant = row.get("merchant")
        if not isinstance(merchant, str) or not needs_manual_context(
            normalize_context_text(merchant)
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
        sorted(transfers.values(), key=lambda item: (item.date, item.merchant, item.cost))
    )
