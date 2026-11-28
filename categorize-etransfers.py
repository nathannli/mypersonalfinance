"""Ask the user to categorize pending statement rows held for manual context.

`finance` holds e-transfers and saves a subcategory. `parents_finance` holds
transfers, cheques, drafts and cash, and saves a category. One at a time.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

from db.my_finance import MyFinanceDB
from db.parents_finance import ParentsFinanceDB
from services.manual_etransfers import ManualTransfer, statement_transfers
from services.transaction_loader import TransactionLoader, statement_files
from sources.registry import get_file_based_card_types


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--type", choices=sorted(get_file_based_card_types()), default="ws_debit"
    )
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--filepath")
    inputs.add_argument("--folder")
    parser.add_argument(
        "--database", choices=["finance", "parents_finance"], default="finance"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--list", action="store_true", help="Read-only JSON queue for an agent"
    )
    mode.add_argument("--transaction", help="Transaction ID from --list to save")
    parser.add_argument(
        "--subcategory", type=int, help="User-selected subcategory ID (finance)"
    )
    parser.add_argument(
        "--category", type=int, help="User-selected category ID (parents_finance)"
    )
    return parser


def load_transfers(args) -> tuple[ManualTransfer, ...]:
    files = statement_files(args.folder) if args.folder else [args.filepath]
    loader = TransactionLoader()
    rows = []
    for filename in files:
        if not Path(filename).is_file():
            raise ValueError(f"Statement does not exist: {filename}")
        rows.extend(loader.load(args.type, filename).iter_rows(named=True))
    return statement_transfers(rows, args.database)


def interactive(transfers, choices, save, level="subcategory") -> None:
    """``level`` is the taxonomy level the user picks: its ID is what gets saved."""
    names = ["category_name", "subcategory_name"][: 2 if level == "subcategory" else 1]
    for choice in sorted(choices, key=lambda row: [row[name] for name in names]):
        label = " / ".join(choice[name] for name in names)
        print(f"  {choice[f'{level}_id']}: {label}")
    valid_ids = {row[f"{level}_id"] for row in choices}
    for index, transfer in enumerate(transfers, 1):
        print(
            f"\n[{index}/{len(transfers)}] {transfer.date} | "
            f"{transfer.merchant} | ${transfer.cost:.2f}"
        )
        while True:
            try:
                answer = input(
                    f"{level.capitalize()} ID to save [s=skip, q=quit]: "
                ).strip()
            except (EOFError, KeyboardInterrupt):
                print(
                    "\nStopped. Earlier choices saved; remaining transfers stay pending."
                )
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
                print(f"Enter a listed {level} ID, s, or q.")
                continue
            if choice_id not in valid_ids:
                print(f"Unknown {level} ID. Choose from the list above.")
                continue
            outcome = save(transfer.date, transfer.merchant, transfer.cost, choice_id)
            print(outcome.status.value)
            break


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    parents = args.database == "parents_finance"
    level, other = (
        ("category", "subcategory") if parents else ("subcategory", "category")
    )
    if getattr(args, other) is not None:
        parser.error(f"--{other} is not valid with --database {args.database}")
    selected = getattr(args, level)
    if bool(args.transaction) != (selected is not None):
        parser.error(f"--transaction and --{level} must be supplied together")
    if not args.list and not args.transaction and not sys.stdin.isatty():
        parser.error(
            "Interactive mode needs a terminal; "
            f"agents use --list then --transaction/--{level}"
        )
    try:
        # Statement loaders print diagnostics; keep machine-readable stdout clean.
        with contextlib.redirect_stdout(sys.stderr):
            transfers = load_transfers(args)
            database = (ParentsFinanceDB if parents else MyFinanceDB)(debug=False)
            choices = database.get_categorization_choices()
        save = (
            database.insert_manual_transfer
            if parents
            else database.insert_manual_etransfer
        )
        if args.transaction:
            transfer = next(
                (item for item in transfers if item.transaction_id == args.transaction),
                None,
            )
            if transfer is None:
                raise ValueError("Transaction ID not found in the supplied statements")
            outcome = save(transfer.date, transfer.merchant, transfer.cost, selected)
            print(
                json.dumps(
                    {
                        "transaction_id": transfer.transaction_id,
                        "status": outcome.status.value,
                    }
                )
            )
            return 0
        pending = tuple(
            item
            for item in transfers
            if not database.check_if_expense_exists(item.date, item.merchant, item.cost)
        )
        if args.list:
            print(
                json.dumps(
                    {
                        "transactions": [item.as_dict() for item in pending],
                        "choices": choices,
                    },
                    ensure_ascii=False,
                )
            )
        elif not pending:
            print("No pending held rows." if parents else "No pending e-transfers.")
        else:
            interactive(pending, choices, save, level)
        return 0
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
