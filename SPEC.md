# Amex statement download automation

## §G

G1|One local command opens Amex Canada in a headed browser, lets the user complete login and MFA, downloads statement transaction data for the selected Amex card, validates the artifact, and returns a path ready for the existing transaction loader.

## §C

C1|Scope is the Canadian consumer Amex Online Services flow for the account's sole Amex card.
C2|Browser runs locally and visibly. User enters credentials, completes MFA, and handles CAPTCHA when Amex requires it.
C3|No bank password, MFA secret, session cookie, browser profile, or downloaded statement enters git.
C4|Use a dedicated persistent Chromium profile outside the repository so an approved Amex session can be reused locally.
C5|Do not bypass Amex security controls or attempt fully unattended authentication.
C6|Downloader does not write to PostgreSQL. Loading remains an explicit existing command after download.
C7|Reuse `sources/csv/amex.py` and the current loader contract. Add only the smallest format adaptation proven necessary by the authenticated export.
C8|Do not create a general bank-automation framework in this change.
C9|No real Amex account access in automated tests. Use local fixtures and mocked browser behavior.
C10|Use local Playwright only. No Browserbase, remote browser, or hosted browser-automation service.

## §I

I1|CLI|`uv run python scripts/download_amex_transactions.py --output-dir <directory> --database <finance|parents_finance>`; `--database` is required and selected by the user.
I2|Auth|Launch local headed Chromium at `https://www.americanexpress.com/en-ca/account/login/` with a dedicated user-data directory outside the repo. Wait for the user to finish login/MFA and reach an authenticated account landing state; automation then navigates to statements.
I3|Amex flow|Follow the official Online Services path: `Statement` -> `Export Statement Data` -> select the transaction data type -> download. Prefer role, label, and visible-text locators over brittle CSS selectors.
I4|Card|Use the account's sole Amex card. Exit with an actionable error if the account later exposes zero or multiple cards.
I5|Download|Capture the browser download event, sanitize the server-suggested filename, save into `--output-dir`, refuse silent overwrite, and remove partial output after failure.
I6|Format discovery|During the first authenticated smoke test, record which consumer export formats Amex actually offers. Prefer the format already accepted by `AmexStatement`; if unavailable, make the narrowest parser change needed for the exported file.
I7|Validation|Before reporting success, parse the saved artifact through the Amex source and confirm the standardized columns are exactly `date`, `merchant`, `cost`, and `cc_category`.
I8|Handoff|Print the saved path and `uv run python load-transactions.py --type amex --filepath <saved-path> --database <user-selected-database>`. Do not run that command automatically.
I9|Dependency|Keep Playwright in an optional browser-automation dependency group and document the one-time Chromium installation command.
I10|Reference|Amex Canada login is `https://www.americanexpress.com/en-ca/account/login/`; Amex documents the Online Services export path at `https://www.americanexpress.com/en-ca/customer-service/payments-and-billings/faq.card-statements.html`.

## §V

V1|Automation never accepts a credential or MFA secret as a CLI argument, config value, or environment variable.
V2|Automation never stores Playwright auth state or the persistent browser profile inside the repository.
V3|The browser remains headed through authentication and download; security challenges always require user action.
V4|A run succeeds only after one complete downloaded artifact exists and the existing Amex source can produce the standardized transaction DataFrame.
V5|A timeout, changed Amex page, absent download, unsupported format, or parse failure exits nonzero with an actionable message and leaves no partial file.
V6|Existing `amex`, `amex_annual`, registry, CLI, and database-loading behavior remains unchanged unless authenticated format discovery proves a focused compatibility edit is required.
V7|Downloaded financial data, browser profiles, screenshots, traces, and auth artifacts are excluded from git and test fixtures.
V8|Automated tests make no network request to Amex and require no user account.
V9|Repeated runs never silently replace an existing statement file.
V10|Browser process, profile, authenticated session, and downloaded files remain on the user's machine; no remote automation service receives them.
V11|`AmexStatement` maps `Date`, `Description`, and `Amount` by header name for authenticated CSV and Excel exports; physical column positions do not change standardized output.

## §T

id|status|goal|cites
T1|x|Run one user-supervised authenticated discovery session; confirm sole-card statement navigation, offered export formats, and downloaded schema without capturing secrets|C1,C2,C3,I3,I4,I6
T2|x|Add optional local Playwright dependency, dedicated-profile defaults outside the repo, and ignore rules for any local browser/download artifacts|C3,C4,C10,I2,I9,V1,V2,V7,V10
T3|x|Implement the local headed Amex downloader, manual-auth wait, resilient statement navigation, download capture, safe naming, collision handling, timeout, and cleanup|C2,C5,C10,I2,I3,I4,I5,V3,V5,V9,V10
T4|x|Connect downloaded-file validation to `AmexStatement`; make a focused parser compatibility edit only if T1 proves it necessary; print the existing loader handoff command|C6,C7,I6,I7,I8,V4,V6,V11
T5|x|Add focused tests with local HTML/download fixtures and mocked browser behavior for auth wait, navigation, successful download, collision, timeout, cleanup, and parser validation|C9,V5,V8,V9
T6|.|Document setup, Chromium install, first login, profile location, normal use, security boundaries, troubleshooting, and the manual authenticated acceptance test|C2,C3,C4,C5,I9,V1,V2,V3,V7

## §B

id|date|cause|fix
B1|2026-08-03|Authenticated Excel export moved `Description` and `Amount`, but parser used fixed column positions|V11
