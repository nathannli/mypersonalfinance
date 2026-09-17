"""Validate the private transaction-LLM gold set for one database.

Runs three fresh passes of the production categorizer over the selected
database's private gold subset and, only when every case matches in all
three passes, writes that database's write-approval record.

Never prints gold content: merchants, amounts, and expected choices stay
off the console; per-case output uses the fingerprint prefix and the
model's returned action/choice only.
"""

import argparse

from config import Config
from db.my_finance import MyFinanceDB
from db.parents_finance import ParentsFinanceDB
from services.gold_validator import (
    production_categorizer_factory,
    run_validation,
    write_approval_record,
)
from services.transaction_llm_approval import load_gold_cases

DATABASE_CLASSES = {
    "finance": MyFinanceDB,
    "parents_finance": ParentsFinanceDB,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate the private gold set for one database and write its "
            "write-approval record after three fully passing fresh runs"
        )
    )
    parser.add_argument(
        "--database",
        required=True,
        choices=sorted(DATABASE_CLASSES),
        help="Database whose gold subset is validated",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    database_name = args.database

    try:
        config = Config()
        database = DATABASE_CLASSES[database_name](debug=False)
        choices = database.get_categorization_choices()
        cases = load_gold_cases(database_name)
        categorizer_factory = production_categorizer_factory(config)
        validation = run_validation(cases, choices, categorizer_factory)
    except Exception as e:
        print(f"Validation failed: {e}")
        return 1

    for index, single in enumerate(validation.passes, start=1):
        if single.failure:
            print(f"Pass {index} failed: {single.failure}")
        for case_result in single.results:
            mark = "ok" if case_result.matched else "MISMATCH"
            print(
                f"Pass {index}: {case_result.fingerprint[:12]} "
                f"{case_result.detail} {mark}"
            )

    if not validation.approved:
        print("Validation failed: approval requires three fully matching passes")
        return 1

    record_path = write_approval_record(
        database=database_name,
        base_url=config.opencodex_base_url,
        model=config.transaction_llm_model,
        choices=choices,
        cases=cases,
        validation=validation,
    )
    print(f"Approved: write mode for '{database_name}' recorded in {record_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
