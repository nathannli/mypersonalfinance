# personal finance
database: postgres <br>
backend logic: python <br>
frontend: metabase <br>

## common commands

load card or bank transactions from files:
```bash
python load-transactions.py --type <card_type> --filepath <path_to_csv> --database finance
```

load online wealthsimple transactions:
```bash
python load-transactions.py --type ws_debit --database finance
python load-transactions.py --type ws_credit --database finance
```

load pre-categorized excel transactions:
```bash
python load-excel-transactions.py --filepath <path_to_excel>
```

# custom packages:
- custom version of https://github.com/ImranR98/Wealthsimpleton that has been modified to be a pip-installable package
