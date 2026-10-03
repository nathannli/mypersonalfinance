"""Ask the user to categorize pending statement e-transfers, one transaction at a time."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

from db.my_finance import MyFinanceDB
from services.manual_etransfers import ManualTransfer, statement_transfers
from services.transaction_loader import TransactionLoader, statement_files
from sources.registry import get_file_based_card_types


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--type", choices=sorted(get_file_based_card_types()), default="ws_debit")
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--filepath")
    inputs.add_argument("--folder")
    parser.add_argument("--database", choices=["finance"], default="finance")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--list", action="store_true", help="Read-only JSON queue for an agent"
    )
    mode.add_argument("--transaction", help="Transaction ID from --list to save")
    parser.add_argument("--subcategory", type=int, help="User-selected subcategory ID")
    return parser


def load_transfers(args) -> tuple[ManualTransfer, ...]:
    files = statement_files(args.folder) if args.folder else [args.filepath]
    loader = TransactionLoader()
    rows = []
    for filename in files:
        if not Path(filename).is_file():
            raise ValueError(f"Statement does not exist: {filename}")
        rows.extend(loader.load(args.type, filename).iter_rows(named=True))
    return statement_transfers(rows)


def interactive(transfers, choices, database) -> None:
    for choice in sorted(
        choices, key=lambda row: (row["category_name"], row["subcategory_name"])
    ):
        print(
            f"  {choice['subcategory_id']}: "
            f"{choice['category_name']} / {choice['subcategory_name']}"
        )
    valid_ids = {row["subcategory_id"] for row in choices}
    for index, transfer in enumerate(transfers, 1):
        print(
            f"\n[{index}/{len(transfers)}] {transfer.date} | "
            f"{transfer.merchant} | ${transfer.cost:.2f}"
        )
        while True:
            try:
                answer = input("Subcategory ID to save [s=skip, q=quit]: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nStopped. Earlier choices saved; remaining transfers stay pending.")
                return
            if answer.lower() == "q":
                print("Stopped. Remaining transfers stay pending.")
                return
            if answer.lower() == "s":
                print("Skipped.")
                break
            try:
                choice_id = int(answer)
            except ValueError:
                print("Enter a listed subcategory ID, s, or q.")
                continue
            if choice_id not in valid_ids:
                print("Unknown subcategory ID. Choose from the list above.")
                continue
            outcome = database.insert_manual_etransfer(
                transfer.date, transfer.merchant, transfer.cost, choice_id
            )
            print(outcome.status.value)
            break


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if bool(args.transaction) != (args.subcategory is not None):
        parser.error("--transaction and --subcategory must be supplied together")
    if not args.list and not args.transaction and not sys.stdin.isatty():
        parser.error(
            "Interactive mode needs a terminal; "
            "agents use --list then --transaction/--subcategory"
        )
    try:
        # Statement loaders print diagnostics; keep machine-readable stdout clean.
        with contextlib.redirect_stdout(sys.stderr):
            transfers = load_transfers(args)
            database = MyFinanceDB(debug=False)
            choices = database.get_categorization_choices()
        if args.transaction:
            transfer = next(
                (item for item in transfers if item.transaction_id == args.transaction),
                None,
            )
            if transfer is None:
                raise ValueError("Transaction ID not found in the supplied statements")
            outcome = database.insert_manual_etransfer(
                transfer.date, transfer.merchant, transfer.cost, args.subcategory
            )
            print(json.dumps({
                "transaction_id": transfer.transaction_id,
                "status": outcome.status.value,
            }))
            return 0
        pending = tuple(
            item for item in transfers
            if not database.check_if_expense_exists(item.date, item.merchant, item.cost)
        )
        if args.list:
            print(json.dumps({
                "transactions": [item.as_dict() for item in pending],
                "choices": choices,
            }, ensure_ascii=False))
        elif not pending:
            print("No pending e-transfers.")
        else:
            interactive(pending, choices, database)
        return 0
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
