"""V3: no tracked file carries a real bank descriptor.

The repository is public. A merchant row in a committed ``.sql`` file is a
permanent behavioural profile in public git history: the 164 exact-match and
100 substring rows in ``finance`` name insurers, dentists, a gym, a
student-loan servicer, TCM and rehab providers, and specific neighbourhood
grocers. So the tracked files under ``ddl/`` carry schema, constraints, and the
taxonomy, and no merchant rows.

Every test here is offline. ``verify_spec_claims.py`` is the check that reads
live, and it is not part of the unit suite. The tests that need the real
descriptors read the gitignored ``ddl/seed/`` and skip when it is absent, which
is the case on CI, so the suite stays runnable on a fresh clone with no
database.

``TAXONOMY_NAMES`` is a reviewed constant rather than a set derived from the
tracked taxonomy files. An earlier version read the allowlist out of the same
working tree it was auditing, so a descriptor appended to a taxonomy file
allowlisted itself and the test passed on a leak. An allowlist that comes from
the thing under test cannot detect that thing. ``test_taxonomy_constant_matches_
the_ddl`` is what keeps the constant honest, and it fails loudly when the
taxonomy changes, so the list is updated deliberately.
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

from utils.repo_paths import repo_root

REPO_ROOT = repo_root()
DDL_DIR = REPO_ROOT / "ddl"
SEED_DIR = DDL_DIR / "seed"

# Tables whose rows are merchant identity. Taxonomy tables are exempt: a
# category name names no merchant.
MERCHANT_TABLES = (
    "merchant_name_auto_match",
    "substring_auto_match",
    "auto_match",
)

TAXONOMY_TABLES = (
    "categories",
    "subcategories",
    "expense_type",
    "main_category",
)

TEXT_SUFFIXES = frozenset({".py", ".sql", ".json", ".md", ".sh", ".yml", ".yaml"})

# Every name the live taxonomy seeds, across both databases. A seed row's
# `merchant_category` legitimately holds one of these, so they must not read as
# a leak.
TAXONOMY_NAMES = frozenset(
    {
        "AI",
        "ApartmentRental",
        "Car Maintenance",
        "Charity",
        "China",
        "Clothes",
        "Clothing",
        "Coding",
        "Commuting",
        "Debt",
        "Donation",
        "Eating Out",
        "Electronics",
        "Entertainment",
        "Fees",
        "Fees & Interest",
        "Fitness",
        "Fixed",
        "Food",
        "Food Delivery",
        "Full Reimburse",
        "Fund Tfr",
        "Gas",
        "Grocery",
        "Health",
        "Hobbies",
        "Household",
        "Housekeeping",
        "Housing",
        "Hydro",
        "Hygiene",
        "Insurance",
        "Internet",
        "Kitchen",
        "Learning",
        "Loans",
        "MISCexpense",
        "Media",
        "Medical/Health",
        "Misc",
        "Misc - Cash Payment",
        "Mobile",
        "NewHome",
        "OSAP",
        "Office",
        "Other",
        "Paris",
        "Parking",
        "Personal Care",
        "Rent",
        "Rent Mortgage",
        "Rides",
        "Shopping",
        "Subscriptions",
        "Substances",
        "Taxes/Legal",
        "Transit",
        "Transportation",
        "Travel",
        "Tuitions",
        "Utilities",
        "Variable",
        "fixed",
        "variable",
    }
)

SQL_LITERAL_RE = re.compile(r"'([^']*)'")
INSERT_RE = re.compile(
    r"insert\s+into\s+(?:\"(?P<quoted>[^\"]+)\"|(?P<bare>\w+))",
    re.IGNORECASE,
)
CREATE_TABLE_RE = re.compile(
    r"create\s+table\s+(?:if\s+not\s+exists\s+)?"
    r"(?:\"(?P<quoted>[^\"]+)\"|(?P<bare>\w+))",
    re.IGNORECASE,
)
INSERT_LINE_RE = re.compile(r"^\s*insert\b", re.IGNORECASE)


def git_tracked(paths: list[str]) -> list[str]:
    """Files git tracks under `paths`, or [] when git is unavailable."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "--", *paths],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    return [line for line in out.stdout.splitlines() if line.strip()]


def tracked_ddl_sql() -> list[Path]:
    """Every tracked `.sql` under `ddl/` outside the gitignored `ddl/seed/`."""
    return [
        REPO_ROOT / name
        for name in git_tracked(["ddl"])
        if name.endswith(".sql") and not name.startswith("ddl/seed/")
    ]


def seed_literals() -> set[str]:
    """Every quoted row value in the gitignored seed files.

    INSERT lines only. The trailing ``pg_get_serial_sequence('<table>', 'id')``
    guard also has quoted text in it, and `'id'` is a schema identifier, not a
    merchant descriptor.
    """
    literals: set[str] = set()
    if not SEED_DIR.is_dir():
        return literals
    for path in sorted(SEED_DIR.glob("*.sql")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not INSERT_LINE_RE.match(line):
                continue
            for value in SQL_LITERAL_RE.findall(line):
                if value.strip():
                    literals.add(value)
    return literals


def seeded_taxonomy_names() -> set[str]:
    """Literals the tracked taxonomy files actually seed, INSERT lines only."""
    names: set[str] = set()
    for database in ("finance", "parents_finance"):
        for table in TAXONOMY_TABLES:
            path = DDL_DIR / database / f"{table}.sql"
            if not path.is_file():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                if INSERT_LINE_RE.match(line):
                    names.update(SQL_LITERAL_RE.findall(line))
    return {name for name in names if name.strip()}


def literals_in(path: Path) -> set[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return set()
    return set(SQL_LITERAL_RE.findall(text))


class TrackedDdlHasNoMerchantRows(unittest.TestCase):
    """The structural rule. Needs no database and no seed files."""

    def setUp(self) -> None:
        self.files = tracked_ddl_sql()

    def test_git_reports_tracked_ddl_sql(self) -> None:
        # Guard the vacuous pass. An empty file list would make every
        # assertion below pass over nothing, which is how the first
        # verify_spec_claims.py ended up green and uninformative.
        self.assertTrue(
            self.files,
            "git ls-files returned no tracked .sql under ddl/; "
            "the assertions below would be vacuous",
        )

    def test_seed_dir_is_gitignored(self) -> None:
        # Precedent: `.gitignore` already excludes `tests/test_private_*.py`
        # for this same reason. The generated seed needs an explicit rule, not
        # merely an absence.
        ignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(
            "ddl/seed/",
            ignore,
            "ddl/seed/ must stay in .gitignore; it holds real bank descriptors",
        )

    def test_no_seed_file_is_tracked(self) -> None:
        tracked = git_tracked(["ddl/seed"])
        self.assertEqual(
            [],
            tracked,
            f"ddl/seed/ is gitignored but git tracks {tracked}",
        )

    def test_no_tracked_ddl_file_inserts_into_a_merchant_table(self) -> None:
        for path in self.files:
            text = path.read_text(encoding="utf-8")
            for match in INSERT_RE.finditer(text):
                table = (match.group("quoted") or match.group("bare")).lower()
                self.assertNotIn(
                    table,
                    MERCHANT_TABLES,
                    f"{path.relative_to(REPO_ROOT)} inserts into merchant table "
                    f"{table!r}; merchant rows belong in gitignored ddl/seed/",
                )

    def test_every_tracked_ddl_table_is_classified(self) -> None:
        # A new tracked table has to be classified. An unclassified one would
        # slip past the rule above, which only knows the four merchant tables
        # by name.
        known = set(MERCHANT_TABLES) | set(TAXONOMY_TABLES) | {"expenses"}
        for path in self.files:
            text = path.read_text(encoding="utf-8")
            for match in CREATE_TABLE_RE.finditer(text):
                table = (match.group("quoted") or match.group("bare")).lower()
                self.assertIn(
                    table,
                    known,
                    f"{path.relative_to(REPO_ROOT)} creates unclassified table "
                    f"{table!r}; classify it as taxonomy or merchant here",
                )

    def test_taxonomy_constant_matches_the_ddl(self) -> None:
        # Keeps TAXONOMY_NAMES from rotting. A taxonomy row added without
        # updating the constant fails here rather than silently reading as a
        # descriptor leak later.
        seeded = seeded_taxonomy_names()
        self.assertTrue(seeded, "expected the taxonomy DDL to seed rows")
        self.assertEqual(
            seeded,
            set(TAXONOMY_NAMES),
            "TAXONOMY_NAMES drifted from the tracked taxonomy. Add the name to "
            "the constant deliberately, after confirming it names no merchant.",
        )


@unittest.skipUnless(
    seed_literals(),
    "ddl/seed/ is absent (a fresh clone or CI); the private descriptor check "
    "needs the generated seed",
)
class NoLiveDescriptorReachesATrackedFile(unittest.TestCase):
    """The direct assertion: no descriptor from the live seed is tracked.

    Skipped when `ddl/seed/` does not exist, so the suite still runs on CI. On
    a private checkout this is the check that actually names the leak.
    """

    def setUp(self) -> None:
        self.descriptors = seed_literals() - TAXONOMY_NAMES
        self.assertTrue(
            self.descriptors,
            "expected merchant descriptors in ddl/seed/ once the taxonomy "
            "names are subtracted",
        )

    def test_no_descriptor_reaches_a_tracked_ddl_file(self) -> None:
        files = tracked_ddl_sql()
        self.assertTrue(files, "no tracked .sql under ddl/ to check")

        leaks = [
            f"{path.relative_to(REPO_ROOT)}: {value!r}"
            for path in files
            for value in sorted(literals_in(path) & self.descriptors)
        ]
        self.assertEqual(
            [],
            leaks,
            "real bank descriptor(s) reached a tracked .sql file. Move the row "
            "to ddl/seed/ and regenerate with scripts/export_ddl_seed.py. "
            f"Leaks: {leaks}",
        )

    def test_no_descriptor_reaches_any_tracked_file_at_all(self) -> None:
        # ddl/ is the expected leak site because that is where the seeds live,
        # but a descriptor in a Python source, a fixture, or a doc is the same
        # permanent public record.
        tracked = git_tracked(["."])
        self.assertTrue(tracked, "git ls-files returned nothing")

        leaks: list[str] = []
        for name in tracked:
            path = REPO_ROOT / name
            if path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            for value in sorted(literals_in(path) & self.descriptors):
                leaks.append(f"{name}: {value!r}")
        self.assertEqual(
            [],
            leaks,
            f"real bank descriptor(s) reached a tracked file: {leaks}",
        )


if __name__ == "__main__":
    unittest.main()
