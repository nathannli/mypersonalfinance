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

## download Amex Canada transactions

Requirements: the Browse CLI and a `BROWSERBASE_API_KEY` exported in the shell.
The command creates a named Browserbase remote session and prints its live-view
URL so you can complete login, MFA, and any CAPTCHA/security challenge.

For Fish users, load the existing secrets file before running the command:

```fish
source ~/.config/fish/secrets.fish
```

Download the latest statement activity without loading it into PostgreSQL:

```bash
uv run python -m scripts.download_amex_transactions \
  --output-dir ~/Downloads/amex \
  --database finance
```

Use `--database parents_finance` when that is the intended loader target. The
database argument is required only to produce the correct handoff command; the
downloader never connects to PostgreSQL or runs the loader.

The command opens Amex in Browserbase. On the first run, complete Amex login,
MFA, and any CAPTCHA manually in the printed live session.

After authentication, automation navigates through `Statement` ->
`Export Statement Data` -> `Go to Statement Activity`, dismisses the first-run
welcome dialog when present, opens `Download`, selects CSV, retrieves the
Browserbase session download archive, moves the single CSV into `--output-dir`,
validates it through `AmexStatement`, and prints the exact
`load-transactions.py` command. Run that printed command separately when ready
to load the transactions.

Security boundaries:

- Never pass Amex credentials or MFA secrets through CLI arguments or repository
  configuration. `BROWSERBASE_API_KEY` is required only to start the remote session.
- Browserbase session state and statement data are used only for this supervised
  run; none of these artifacts may be committed.
- Keep `--output-dir` outside the repository. Existing files are never silently
  overwritten.
- Authentication and security challenges always require user action; the tool
  does not bypass Amex controls or use Playwright.

Troubleshooting:

- Missing `BROWSERBASE_API_KEY`: export it before running the command, for example
  by sourcing the shell secrets file that defines it.
- Missing download after CSV selection: confirm the Browserbase session download
  archive contains exactly one CSV, then retry with a fresh session.
- Authentication timeout: rerun the command and finish login/MFA within five
  minutes.
- Missing `Statement`, `Export Statement Data`, CSV, or `Download`: Amex likely
  changed the page; stop and update the locators before retrying.
- Zero or multiple cards: this workflow supports an account with exactly one
  Amex card.
- Existing or stale partial file: choose another output directory or move the
  named file before retrying. The downloader will not overwrite it.
- Parser validation failure: retain the completed export locally and update
  `AmexStatement` for the observed schema; do not load the file first.

Manual authenticated acceptance test:

1. Run the downloader with an empty output directory and the intended database.
2. Complete login/MFA in the Browserbase live session.
3. Confirm automation opens statement export, selects CSV, and downloads one
   complete file.
4. Confirm the output file parses to exactly `date`, `merchant`, `cost`, and
   `cc_category`.
5. Confirm the printed loader command contains the saved path and selected
   database but is not executed automatically.
6. Run the downloader again with the same statement filename and confirm it
   refuses to overwrite the first file.

# custom packages:
- custom version of https://github.com/ImranR98/Wealthsimpleton that has been modified to be a pip-installable package
- `uv` resolves `wealthsimpleton` from the local `../Wealthsimpleton` checkout
