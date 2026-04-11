# personal finance
database: postgres <br>
backend logic: python <br>
frontend: metabase <br>

## setup

```bash
uv sync --group dev
uv run pre-commit install
```

## common commands

load card or bank transactions from files:
```bash
uv run python load-transactions.py --type <card_type> --filepath <path_to_csv> --database finance
```

load online wealthsimple transactions:
```bash
uv sync --extra wealthsimple
uv run python load-transactions.py --type ws_debit --database finance
uv run python load-transactions.py --type ws_credit --database finance
```

load pre-categorized excel transactions:
```bash
uv run python load-excel-transactions.py --filepath <path_to_excel>
```

# custom packages:
- custom version of https://github.com/ImranR98/Wealthsimpleton that has been modified to be a pip-installable package
- `uv` resolves `wealthsimpleton` from the local `../Wealthsimpleton` checkout
