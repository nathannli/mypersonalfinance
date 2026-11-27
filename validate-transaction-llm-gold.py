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
from collections import defaultdict
from decimal import Decimal

from config import Config
from db.my_finance import MyFinanceDB
from db.parents_finance import ParentsFinanceDB
from services.deterministic_categorization import (
    DeterministicOutcome,
    DeterministicResolution,
    resolve_deterministic_choice,
)
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
from services.transaction_categorization import (
    amount_to_minor_units,
    normalize_context_text,
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
    parser.add_argument(
        "--workers",
        type=int,
        choices=(1, 2, 3),
        help="Concurrent requests per enriched pass (default: 1)",
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
    database: MyFinanceDB,
    workers: int = 1,
) -> int:
    model = config.enriched_transaction_llm_model
    try:
        cases = load_enriched_gold_cases(
            database_name, choices, packet_resolver=resolve_approved_packet
        )
        # Gold stores normalized descriptors. Recover the observed spelling for
        # exact-match rules and leave ambiguous spellings on the model route.
        descriptors = defaultdict(set)
        amounts = sorted(
            {Decimal(case.amount_minor_units).scaleb(-2) for case in cases}
        )
        for merchant, cost in database.select(
            "SELECT DISTINCT merchant, cost FROM expenses WHERE cost = ANY(%s)",
            (amounts,),
        ):
            descriptors[
                normalize_context_text(merchant), amount_to_minor_units(cost)
            ].add(merchant)

        def resolve_rule(case):
            matches = descriptors.get((case.merchant, case.amount_minor_units), ())
            if case.statement_category is not None or len(matches) != 1:
                return DeterministicResolution(DeterministicOutcome.NO_MATCH)
            (merchant,) = matches
            return resolve_deterministic_choice(
                card_type="amex",
                cc_category=None,
                choices=choices,
                merchant=merchant,
                amount=Decimal(case.amount_minor_units).scaleb(-2),
                auto_match=lambda: database.get_auto_match_category(merchant),
            )

        completed = [0, 0, 0]

        def report(pass_number, case_result):
            completed[pass_number - 1] += 1
            mark = "ok" if case_result.matched else "MISMATCH"
            print(
                f"Pass {pass_number} [{completed[pass_number - 1]}/{len(cases)}]: "
                f"{case_result.fingerprint[:12]} {case_result.detail} {mark}",
                flush=True,
            )

        validation = run_enriched_validation(
            cases,
            choices,
            production_categorizer_factory(config, model=model),
            packet_resolver=resolve_approved_packet,
            workers=workers,
            on_case=report,
            resolve_rule=resolve_rule,
        )
    except Exception as e:
        print(f"Validation failed: {e}")
        return 1

    for pass_number, single in enumerate(validation.passes, start=1):
        if single.failure:
            print(f"Pass {pass_number} failed: {single.failure}", flush=True)
    if not validation.approved:
        print("Validation failed: approval requires three fully matching passes")
        return 1

    record_path = write_enriched_approval_record(
        database=database_name,
        base_url=config.llm_base_url,
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
        base_url=config.llm_base_url,
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
    if args.workers is not None and not args.enriched:
        print("Validation failed: --workers requires --enriched")
        return 1

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
        return _run_enriched(
            database_name, config, choices, database, workers=args.workers or 1
        )
    return _run_legacy(database_name, config, choices)


if __name__ == "__main__":
    raise SystemExit(main())
