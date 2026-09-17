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

The unattended Amex workflow requires macOS and Python 3.12+. Default
Browserbase route requires Browse CLI and `BROWSERBASE_API_KEY`. BrowserOS route
starts BrowserOS neo when needed, then connects to `BROWSEROS_MCP_URL` (default:
`http://127.0.0.1:9010/mcp`). Both routes use local `.env` values for
`AMEX_USER` and `AMEX_PASSWORD`; grant Full Disk Access to terminal app so
`macos-messages` can read incoming Amex SMS code.

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
- BrowserOS endpoint unavailable: confirm `BROWSEROS_MCP_URL` points to its
  local MCP endpoint. The route starts `BrowserOS neo` automatically and waits
  up to 30 seconds for endpoint.
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

## LLM transaction categorization

Unknown transactions are categorized by an LLM instead of an interactive
prompt, so `load-transactions.py` and `load-excel-transactions.py` both finish
without user input. Existing deterministic matching (card category rules,
exact merchant auto-match, substring auto-match) always runs first and always
wins; a matched transaction makes no provider request.

### Environment

```sh
OPENCODEX_BASE_URL=http://localhost:10100
OPENCODEX_API_KEY=<key>
TRANSACTION_LLM_MODEL=SingularityApiDev/deepseek-v4-flash-0731
ENRICHED_TRANSACTION_LLM_MODEL=anthropic/claude-haiku-4-5
TRANSACTION_LLM_TIMEOUT_SECONDS=120
TRANSACTION_LLM_MODE=shadow
```

`TRANSACTION_LLM_MODEL` governs `parents_finance` and unenriched validation.
`ENRICHED_TRANSACTION_LLM_MODEL` governs enriched `finance` only; it defaults to
`anthropic/claude-haiku-4-5` and never changes the parents path. Only the
configured base URL and the exact configured model are ever called; there is no
provider or model fallback. A missing or empty `OPENCODEX_API_KEY` becomes a
`provider_error` on the first LLM call, so deterministic-only runs do not need a
key.

### Research-first workflow

Enriched `finance` categorization answers unknown merchants from frozen,
human-approved research packets. The load path never calls the web.

1. Research: `research-transaction-merchants.py --database finance` discovers
   current deterministic unknowns, queries TinyFish once per merchant, and
   writes a pending packet per merchant. Add `--refresh` to re-research a
   merchant whose evidence changed.
2. Review: `review-transaction-research.py` lists pending packets and records
   `--approve <packet_id>` or `--reject <packet_id> --reason <text>` offline.
   Approval binds the exact `packet_sha256`; a successful refresh returns the
   packet to pending.
3. Load: `load-transactions.py --type amex --database finance` consumes only
   approved packets. A `suggest_new` result is recorded for review, never
   inserted.
4. Review suggestions: inspect the private suggestion artifact and decide
   whether to add the proposed category or subcategory yourself.

Run the research and review steps on a schedule you control. A new or changed
packet reaches the load path only after a human approves its exact hash.

### TinyFish research limits and privacy

`research-transaction-merchants.py` is the only entry point that reads
`TINYFISH_API_KEY`; it loads the repository `.env` at startup. Transaction load,
Excel load, cron, and gold validation never read that key and make zero TinyFish
requests, so they run unchanged without it.

- Search timeout 30 seconds; each fetched URL gets its own 150-second budget.
- Requests are paced to stay within 30 requests per minute, and a request is
  retried at most 3 times behind `Retry-After` or bounded backoff.
- A packet is immutable until an explicit `--refresh` succeeds; a failed
  refresh changes nothing. There is no TTL or background refresh.
- TinyFish receives only the derived merchant search term, the fixed research
  purpose, `CA`, and `en`. Amounts, dates, card and account data, the taxonomy,
  and other transactions never leave.
- Packets, evidence, approvals, and suggestions live in git-ignored private
  files under the repository root: `.transaction-web-research/`,
  `.transaction-web-research-approvals.json`, and
  `.transaction-category-suggestions.json`. Search and fetch content, real
  packets, suggestions, amounts, and keys never enter git, cron output, or
  persistent logs.

### Category suggestions

`suggest_new` is recommendation-only. It records a cited proposal for a
category the live taxonomy does not contain and makes the run `partial`; it
never creates a category or subcategory row, writes an auto-match entry, or
inserts the expense. Applying a suggestion is a manual decision.

### Cloud and proxy trust

`anthropic/claude-haiku-4-5` and `SingularityApiDev/deepseek-v4-flash-0731` are
cloud models. Merchant names, amounts, and the live category list for the
selected database leave the local network when an unknown transaction is
categorized, and the derived merchant term leaves it during research. The
configured OpenCodex proxy is the trusted routing boundary: approval binds the
normalized proxy URL and the exact requested model ID, but it cannot attest
which upstream route the proxy chooses. Point `OPENCODEX_BASE_URL` at a proxy
you control.

### Shadow mode and write mode

`TRANSACTION_LLM_MODE=shadow` is the default and performs zero LLM-driven
database writes. A validated LLM choice is reported as a `shadow` outcome with
its suggested choice ID; deterministic inserts and deletes behave exactly as
before.

`TRANSACTION_LLM_MODE=write` applies LLM-selected categories, but only after a
per-database approval record exists. An unapproved or stale approval aborts the
run before any database mutation; it is never silently downgraded to shadow.
Even when approved, a write happens only for a context that appears in that
database's approved gold subset **and** whose runtime choice equals the approved
choice. Unseen or mismatched contexts stay `shadow`.

Approval is per database. A `finance` approval never authorizes
`parents_finance` writes, or the reverse.

### Run status and exit codes

Every processed row produces exactly one outcome: `inserted`, `duplicate`,
`ignored`, `deleted`, `shadow`, `suggested`, or `unresolved`. Totals reconcile
against the number of processed rows, and the summary prints each unresolved
merchant, date, and reason.

- `complete`: no `shadow`, `suggested`, or `unresolved` rows; exit code 0.
- `partial`: at least one `shadow`, `suggested`, or `unresolved` row; exit code 0.
- `failed`: a file failed or write approval aborted; exit code 1.

Restricted cron output reports status counts and suggestion IDs only; normalized
merchant names, rationale, citations, and packet bodies stay out of
Discord/persistent logs.

The Excel cron run reports the same status and every outcome count through its
Discord notification.

### Circuit breaker

A provider connection error, authentication failure, unavailable model, or any
non-2xx response opens the run-level circuit immediately. Two consecutive
timeouts or protocol/schema validation failures also open it. Once open, no
further provider calls are made for that run, and remaining unknown rows become
`unresolved` instead of stalling the load. Only a fully validated `select` or
`abstain` resets the failure streak.

Within a run, a validated `select` is cached by full canonical context
(database, normalized merchant, signed amount in minor units, normalized
statement category, live choice list) plus model, prompt version, schema
version, and mode. Abstentions and failures are never cached.

### Gold validation before write mode

Write mode requires a user-approved gold set and three consecutive fully
passing validation runs:

- `.transaction-llm-gold.json` — private, git-ignored, real transaction
  contexts with user-approved expected results.
- `.transaction-llm-approval.json` — private, git-ignored approval record with
  separate per-database entries and a separate `finance:enriched` entry holding
  only identity hashes, pass count, and timestamp.
- `tests/fixtures/transaction_llm_gold_synthetic.json` and
  `tests/fixtures/transaction_llm_gold_enriched_synthetic.json` — tracked,
  synthetic data only, used by the automated tests.

Validate the protocol you are approving:

```sh
uv run python validate-transaction-llm-gold.py --database finance --enriched
uv run python validate-transaction-llm-gold.py --database parents_finance
```

All private files resolve from the repository root, never the process working
directory, so cron and direct runs agree. Each validation pass builds a fresh
categorizer with an empty cache and reset circuit, and each gold case must cost
exactly one real provider request; a cache replay, an opened circuit, or any
mismatch fails the pass. Validation makes no TinyFish requests.

Enriched approval additionally binds each case's approved research packet hash
plus that packet's schema and query versions, so a successful refresh, a
tampered packet, or an unapproved packet can never satisfy it. Approval is
invalidated whenever the database, base URL, model, prompt bytes,
response-schema bytes, live taxonomy, gold subset, packet hash, or packet
versions change. Unenriched and enriched approvals never authorize each other.

The automated test suite makes no network requests; it drives fake
categorizers only.

# custom packages:
- custom version of https://github.com/ImranR98/Wealthsimpleton that has been modified to be a pip-installable package
- `uv` resolves `wealthsimpleton` from the local `../Wealthsimpleton` checkout
