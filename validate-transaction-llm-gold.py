"""Validate the private transaction-LLM gold set for one database.

Runs three fresh passes of the production categorizer over the selected
database's private gold subset and, only when every case matches in all
three passes, writes that database's write-approval record.

With ``--enriched`` it validates the packet-bound enriched gold subset for
``finance`` instead: each case must still resolve an approved research packet
with its exact bound hash and schema/query versions, the request goes through
the exact production enriched path, and approval is written only under the
enriched key so the unenriched records are left untouched.

Never prints gold content: merchants, amounts, and expected choices stay
off the console; per-case output uses the fingerprint prefix and the
model's returned action/choice only.
"""

import argparse

from config import Config
from db.my_finance import MyFinanceDB
from db.parents_finance import ParentsFinanceDB
from services.enriched_categorization import resolve_approved_packet
from services.gold_validator import (
    production_categorizer_factory,
    run_enriched_validation,
    run_validation,
    write_approval_record,
    write_enriched_approval_record,
)
from services.transaction_llm_approval import (
    ENRICHED_GOLD_DATABASE,
    load_enriched_gold_cases,
    load_gold_cases,
)

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
    parser.add_argument(
        "--enriched",
        action="store_true",
        help=(
            "Validate the packet-bound enriched gold subset and write the "
            f"enriched approval record ({ENRICHED_GOLD_DATABASE} only)"
        ),
    )
    return parser


def _report(validation) -> None:
    """Print per-case progress. Never prints gold content, only model output."""
    for index, single in enumerate(validation.passes, start=1):
        if single.failure:
            print(f"Pass {index} failed: {single.failure}")
        for case_result in single.results:
            mark = "ok" if case_result.matched else "MISMATCH"
            print(
                f"Pass {index}: {case_result.fingerprint[:12]} "
                f"{case_result.detail} {mark}"
            )


def _run_enriched(
    database_name: str,
    config: Config,
    choices: list[dict[str, object]],
) -> int:
    model = config.enriched_transaction_llm_model
    try:
        cases = load_enriched_gold_cases(
            database_name, choices, packet_resolver=resolve_approved_packet
        )
        validation = run_enriched_validation(
            cases,
            choices,
            production_categorizer_factory(config, model=model),
            packet_resolver=resolve_approved_packet,
        )
    except Exception as e:
        print(f"Validation failed: {e}")
        return 1

    _report(validation)
    if not validation.approved:
        print("Validation failed: approval requires three fully matching passes")
        return 1

    record_path = write_enriched_approval_record(
        database=database_name,
        base_url=config.opencodex_base_url,
        model=model,
        choices=choices,
        cases=cases,
        validation=validation,
    )
    print(
        f"Approved: enriched write mode for '{database_name}' recorded in "
        f"{record_path.name}"
    )
    return 0


def _run_legacy(
    database_name: str,
    config: Config,
    choices: list[dict[str, object]],
) -> int:
    model = config.transaction_llm_model
    try:
        cases = load_gold_cases(database_name)
        validation = run_validation(
            cases,
            choices,
            production_categorizer_factory(config, model=model),
        )
    except Exception as e:
        print(f"Validation failed: {e}")
        return 1

    _report(validation)
    if not validation.approved:
        print("Validation failed: approval requires three fully matching passes")
        return 1

    record_path = write_approval_record(
        database=database_name,
        base_url=config.opencodex_base_url,
        model=model,
        choices=choices,
        cases=cases,
        validation=validation,
    )
    print(f"Approved: write mode for '{database_name}' recorded in {record_path.name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    database_name = args.database

    if args.enriched and database_name != ENRICHED_GOLD_DATABASE:
        print(
            f"Validation failed: --enriched is only supported for "
            f"{ENRICHED_GOLD_DATABASE}"
        )
        return 1

    try:
        config = Config()
        database = DATABASE_CLASSES[database_name](debug=False)
        choices = database.get_categorization_choices()
    except Exception as e:
        print(f"Validation failed: {e}")
        return 1

    if args.enriched:
        return _run_enriched(database_name, config, choices)
    return _run_legacy(database_name, config, choices)


if __name__ == "__main__":
    raise SystemExit(main())
