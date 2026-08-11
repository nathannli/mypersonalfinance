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

Default route requires Browse CLI and `BROWSERBASE_API_KEY`. BrowserOS route
requires BrowserOS neo running locally with its MCP endpoint at
`BROWSEROS_MCP_URL` (default: `http://127.0.0.1:9010/mcp`). Both routes use
local `.env` values for `AMEX_USER` and `AMEX_PASSWORD`; grant Full Disk Access
to terminal app so `macos-messages` can read incoming Amex SMS code.

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

Set this local `.env` value to use BrowserOS neo instead of Browserbase:

```sh
AMEX_BROWSER_BACKEND=browseros
```

Then run:

```bash
uv run python -m scripts.download_amex_transactions \
  --output-dir ~/Downloads/amex \
  --database finance \
  --months latest 2026-07 2026-06 2026-05
```

Browserbase remains default. `AMEX_BROWSER_BACKEND=browseros` opens a BrowserOS neo task-owned tab,
reuses its authenticated profile when available, and otherwise fills
`AMEX_USER`/`AMEX_PASSWORD` from `.env`, selects SMS delivery, retrieves recent
Amex SMS code through `macos-messages`, and submits it. CAPTCHA and unexpected
security challenges stop with an actionable error.

After authentication, automation navigates through `Statement` ->
`Export Statement Data` -> `Go to Statement Activity`, dismisses the first-run
welcome dialog when present, opens `Download`, selects CSV, retrieves the
Browserbase session download archive, moves the single CSV into `--output-dir`,
validates it through `AmexStatement`, and prints the exact
`load-transactions.py` command. Run that printed command separately when ready
to load the transactions.

Security boundaries:

- Amex credentials are read from existing `.env` values and entered only into the
  live Amex login form. Recent Amex MFA codes are used only in memory for the
  Browserbase form; credentials and codes are never printed or passed as CLI
  arguments. `BROWSERBASE_API_KEY` is required only to start the remote session.
- Browserbase and BrowserOS session state plus statement data are used only for
  this run; none of these artifacts may be committed.
- Keep `--output-dir` outside the repository. Existing files are never silently
  overwritten.
- Authentication and security challenges always require user action; the tool
  does not bypass Amex controls or use Playwright.

Troubleshooting:

- Missing `BROWSERBASE_API_KEY`: export it before running the command, for example
  by sourcing the shell secrets file that defines it.
- BrowserOS endpoint unavailable: start BrowserOS neo and confirm
  `BROWSEROS_MCP_URL` points to its local MCP endpoint. The BrowserOS route
  starts `BrowserOS neo` automatically and waits up to 30 seconds for endpoint.
- `macos-messages` cannot read SMS: grant Full Disk Access to the terminal app in
  macOS System Settings, then rerun.
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
