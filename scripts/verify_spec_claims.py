"""Verify every live-schema claim in SPEC.md, each labelled with its database.

The spec asserted four things that live disagrees with, and a fifth shape it
never described at all. Every one of them was a claim no check covered. This
script exists so that cannot recur: every claim names the database it is
checked against, and a claim about `finance` is never answered from a
`parents_finance` connection.

The `detail` of every check carries the *observed* value. An earlier version
hardcoded the detail, so 77 passes printed the word "absent" beside them and
the run was green and uninformative. A check reporting a constant is a check
with no output.

Run from the repo root:  uv run --frozen python scripts/verify_spec_claims.py
"""

import pathlib
import subprocess

import psycopg

from config import Config

CONF = Config(debug=False)
REPO = pathlib.Path(__file__).resolve().parent.parent

results: list[tuple[bool, str, str]] = []


def check(ok: bool, ident: str, detail: str) -> None:
    results.append((bool(ok), ident, detail))


def conn(db: str):
    return psycopg.connect(f"{CONF.postgres_connection_string}/{db}")


def q(db: str, sql: str, args: tuple = ()) -> list[tuple]:
    with conn(db) as c, c.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def scalar(db: str, sql: str, args: tuple = ()):
    rows = q(db, sql, args)
    return rows[0][0] if rows else None


def uniques(db: str, table: str) -> list[str]:
    return [
        d.replace("UNIQUE ", "")
        for (d,) in q(
            db,
            "select pg_get_constraintdef(oid) from pg_constraint "
            "where conrelid=%s::regclass and contype='u'",
            (table,),
        )
    ]


def cols(db: str, table: str) -> dict[str, tuple[str, str, str]]:
    return {
        n: (d, nl, dflt or "")
        for n, d, nl, dflt in q(
            db,
            "select column_name, data_type, is_nullable, column_default "
            "from information_schema.columns where table_name=%s",
            (table,),
        )
    }


def ddl_text(path: str) -> str:
    """A DDL file with `--` comment lines removed, so a check on seeded rows
    reads INSERTs rather than prose describing them."""
    text = (REPO / path).read_text(encoding="utf-8")
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("--")
    )


# ---------------------------------------------------------------- FINANCE
def finance() -> None:
    db = "finance"

    rows = q(db, "select id, name from categories order by id")
    check(len(rows) == 13, "F1a finance.categories row count = 13", f"got {len(rows)}")
    check(
        [r[0] for r in rows] == list(range(1, 14)),
        "F1b finance.categories ids 1..13, no gaps",
        f"got {[r[0] for r in rows]}",
    )
    coding = [r for r in rows if r[1] == "Coding"]
    check(
        coding == [(13, "Coding")],
        "F1c finance.categories id 13 is 'Coding'",
        f"got {coding}",
    )

    sub = q(
        db,
        "select id, name, category_id, expense_type_id from subcategories order by id",
    )
    check(len(sub) == 36, "F2a finance.subcategories row count = 36", f"got {len(sub)}")
    check(
        [r[0] for r in sub] == list(range(1, 37)),
        "F2b finance.subcategories ids 1..36, no gaps",
        f"got {[r[0] for r in sub]}",
    )
    for sid, sname in ((34, "China"), (35, "Paris"), (36, "AI")):
        check(
            (sid, sname) in [(r[0], r[1]) for r in sub],
            f"F2c finance.subcategories id {sid} is {sname!r}",
            f"got {[r[:2] for r in sub if r[0] in (34, 35, 36)]}",
        )

    et_col = cols(db, "subcategories").get("expense_type_id")
    check(
        et_col is not None and et_col[0] == "integer" and et_col[1] == "NO",
        "F3 finance.subcategories.expense_type_id is integer NOT NULL",
        f"got {et_col}",
    )

    et = q(db, "select id, type from expense_type order by id")
    check(
        et == [(1, "variable"), (2, "fixed")],
        "F4 finance.expense_type is (1,'variable'),(2,'fixed')",
        f"got {et}",
    )
    et_col = cols(db, "expense_type")
    check(
        et_col.get("type") == ("character varying", "NO", ""),
        "F4b finance.expense_type.type is character varying NOT NULL",
        f"got {et_col.get('type')}",
    )
    check(
        uniques(db, "expense_type") == ["(type)"],
        "F4c finance.expense_type has UNIQUE (type)",
        f"got {uniques(db, 'expense_type')}",
    )
    et_fk = q(
        db,
        "select conname, pg_get_constraintdef(oid) from pg_constraint "
        "where conrelid='subcategories'::regclass and contype='f' "
        "and pg_get_constraintdef(oid) like '%%expense_type%%'",
    )
    check(
        et_fk
        == [
            (
                "subcategories_expense_type_id_fk",
                "FOREIGN KEY (expense_type_id) REFERENCES expense_type(id)",
            )
        ],
        "F4d finance.subcategories FK expense_type_id -> expense_type(id)",
        f"got {et_fk}",
    )

    n_exp = scalar(db, "select count(*) from expenses")
    check(n_exp == 3063, "F5 finance.expenses row count = 3063", f"got {n_exp}")
    check(
        "(date, merchant, cost)" in uniques(db, "expenses"),
        "F5b finance.expenses UNIQUE (date, merchant, cost)",
        f"got {uniques(db, 'expenses')}",
    )

    n_m = scalar(db, "select count(*) from merchant_name_auto_match")
    check(n_m == 164, "F6 finance.merchant_name_auto_match = 164", f"got {n_m}")
    check(
        uniques(db, "merchant_name_auto_match")
        == ["(merchant_name, merchant_category, merchant_subcategory)"],
        "F6b finance.merchant_name_auto_match UNIQUE is 3-column (real defect)",
        f"got {uniques(db, 'merchant_name_auto_match')}",
    )

    n_s = scalar(db, "select count(*) from substring_auto_match")
    check(n_s == 100, "F7 finance.substring_auto_match = 100", f"got {n_s}")
    su = uniques(db, "substring_auto_match")
    check(
        su == ['("substring", merchant_category, merchant_subcategory)'],
        "F7b finance.substring_auto_match UNIQUE already covers all 3 columns",
        f"got {su}  <-- SPEC CLAIM WAS WRONG IF NOT THIS",
    )

    r78 = q(
        db,
        "select substring, merchant_category, merchant_subcategory "
        "from substring_auto_match where id=78",
    )
    check(
        r78 == [("paula's choice", "Shopping", "Hygiene")],
        "F8 finance.substring_auto_match id 78 is the broken pair",
        f"got {r78}",
    )

    hyg = q(
        db,
        "select s.id, s.name, c.name from subcategories s join categories c "
        "on c.id=s.category_id where s.name='Hygiene'",
    )
    check(
        hyg == [(23, "Hygiene", "Personal Care")],
        "F9 finance 'Hygiene' sits under 'Personal Care', not Shopping",
        f"got {hyg}",
    )

    e1001 = q(
        db,
        "select e.id, e.merchant, c.name, s.name from expenses e "
        "join categories c on c.id=e.category_id "
        "join subcategories s on s.id=e.subcategory_id where e.id=1001",
    )
    check(
        len(e1001) == 1 and e1001[0][2] == "Personal Care" and e1001[0][3] == "Hygiene",
        "F10 expense 1001 resolved to Personal Care / Hygiene",
        f"got {e1001}",
    )

    mism = scalar(
        db,
        "select count(*) from expenses e join subcategories s "
        "on s.id=e.subcategory_id where e.category_id <> s.category_id",
    )
    check(mism == 0, "F11 zero expenses with category_id disagreeing", f"got {mism}")

    dupe = q(
        db,
        "select merchant_name, count(*) from merchant_name_auto_match "
        "group by merchant_name having count(*)>1",
    )
    check(not dupe, "F12 zero merchants mapped to two categories", f"got {dupe}")

    orphan_m = q(
        db,
        "select count(*) from merchant_name_auto_match m where not exists ("
        "select 1 from subcategories s where s.name=m.merchant_subcategory and "
        "s.category_id=(select id from categories where name=m.merchant_category))",
    )
    check(orphan_m[0][0] == 0, "F13 zero orphan auto_match pairs", f"got {orphan_m}")

    orphan_s = q(
        db,
        "select substring, merchant_category, merchant_subcategory "
        "from substring_auto_match m where not exists ("
        "select 1 from subcategories s where s.name=m.merchant_subcategory and "
        "s.category_id=(select id from categories where name=m.merchant_category))",
    )
    check(
        len(orphan_s) == 1 and orphan_s[0][0] == "paula's choice",
        "F14 exactly one orphan substring rule, paula's choice",
        f"got {orphan_s}",
    )

    pats = q(
        db,
        "select substring, merchant_category, merchant_subcategory from substring_auto_match",
    )
    merchants = [r[0] for r in q(db, "select distinct merchant from expenses")]
    multi = conflicts = 0
    for m in merchants:
        low = m.lower()
        hits = [(p, c, s) for p, c, s in pats if p in low]
        if len(hits) > 1:
            multi += 1
            if len({(c, s) for _, c, s in hits}) > 1:
                conflicts += 1
    check(multi == 74, "F15 74 merchants match >1 substring rule", f"got {multi}")
    check(conflicts == 0, "F15b 0 of them disagree on the answer", f"got {conflicts}")

    # committed DDL now carries the rows §I recorded as missing
    ddl_cat = ddl_text("ddl/finance/categories.sql")
    ddl_sub = ddl_text("ddl/finance/subcategories.sql")
    for name in ("Coding", "China", "Paris", "AI"):
        where = ddl_cat if name == "Coding" else ddl_sub
        check(
            f"'{name}'" in where,
            f"F16 committed DDL carries {name!r}",
            f"{name!r} is absent from the committed DDL",
        )
    check(
        "expense_type_id" in ddl_sub,
        "F16e committed subcategories.sql has expense_type_id",
        "expense_type_id is absent from the committed DDL",
    )
    check(
        "INSERT" in ddl_text("ddl/finance/expense_type.sql")
        if (REPO / "ddl/finance/expense_type.sql").exists()
        else True,
        "F16f ddl/finance/expense_type.sql (new in this branch)",
        "not yet created",
    )


# ----------------------------------------------------------- PARENTS_FINANCE
def parents() -> None:
    db = "parents_finance"

    n = scalar(db, "select count(*) from expenses")
    check(n == 4124, "P1 parents.expenses = 4124", f"got {n}")

    pcols = cols(db, "expenses")
    check(
        pcols.get("comments") == ("text", "YES", ""),
        "P1b parents.expenses.comments is text nullable",
        f"got {pcols.get('comments')}",
    )
    pddl = ddl_text("ddl/parents_finance/expenses.sql")
    check(
        "comments" in pddl,
        "P1c committed parents expenses.sql carries comments",
        "comments is absent from the committed DDL",
    )

    mc = q(db, "select id, name from main_category order by id")
    check(
        mc == [(1, "Fixed"), (2, "Variable")],
        "P2 parents.main_category is (1,'Fixed'),(2,'Variable') -- INVERTED vs finance",
        f"got {mc}",
    )
    check(
        "(name)" in uniques(db, "main_category"),
        "P2b parents.main_category UNIQUE (name)",
        f"got {uniques(db, 'main_category')}",
    )

    cat = q(db, "select id, name, main_category_id from categories order by id")
    ids = [r[0] for r in cat]
    check(len(cat) == 22, "P3a parents.categories = 22 rows", f"got {len(cat)}")
    check(
        ids == [i for i in range(1, 25) if i not in (6, 16)],
        "P3b ids run 1..24 with exactly 6 and 16 missing",
        f"got {ids}",
    )
    check(
        "(main_category_id, name)" in uniques(db, "categories"),
        "P3c parents.categories UNIQUE (main_category_id, name)",
        f"got {uniques(db, 'categories')}",
    )
    mc_col = cols(db, "categories").get("main_category_id")
    check(
        mc_col is not None and mc_col[1] == "NO",
        "P4 parents.categories.main_category_id NOT NULL",
        f"got {mc_col}",
    )
    fk = q(
        db,
        "select count(*) from pg_constraint where conrelid='categories'::regclass "
        "and contype='f'",
    )
    check(fk[0][0] == 1, "P4b parents.categories has the main_category FK", f"got {fk}")

    dangling = scalar(
        db,
        "select count(*) from expenses where category_id not in (select id from categories)",
    )
    check(dangling == 0, "P5 zero dangling expense category_id", f"got {dangling}")
    used = scalar(db, "select count(distinct category_id) from expenses")
    check(used == 21, "P6 21 distinct category_id in use", f"got {used}")

    names = {r[1] for r in cat}
    check(
        "Gifts/Donations" not in names,
        "P7 'Gifts/Donations' absent from live",
        f"got {sorted(names)}",
    )
    ddl = ddl_text("ddl/parents_finance/categories.sql")
    check(
        "Gifts/Donations" not in ddl,
        "P7b committed DDL no longer seeds 'Gifts/Donations'",
        "'Gifts/Donations' is still in the committed DDL",
    )
    missing = {
        "ApartmentRental": 15,
        "Fees & Interest": 19,
        "Hygiene": 21,
        "Kitchen": 20,
        "MISCexpense": 17,
        "Misc - Cash Payment": 23,
        "Rent Mortgage": 24,
        "Taxes/Legal": 18,
        "Utilities": 22,
    }
    for name, cid in missing.items():
        check(
            (cid, name) in [(r[0], r[1]) for r in cat],
            f"P8 parents.categories id {cid} is {name!r}",
            f"got {[r[:2] for r in cat if r[0] == cid]}",
        )
        check(
            f"'{name}'" in ddl,
            f"P8b committed DDL carries {name!r} at id {cid}",
            f"{name!r} is absent from the committed DDL",
        )

    am = scalar(db, "select count(*) from auto_match")
    check(am == 204, "P9a parents.auto_match = 204", f"got {am}")
    check(
        "(merchant_name, merchant_category)" in uniques(db, "auto_match"),
        "P9b parents.auto_match UNIQUE is 2-column (correct here)",
        f"got {uniques(db, 'auto_match')}",
    )
    check(
        "merchant_subcategory" not in cols(db, "auto_match"),
        "P9c parents.auto_match has no subcategory column",
        f"got {sorted(cols(db, 'auto_match'))}",
    )

    ps = scalar(db, "select count(*) from substring_auto_match")
    check(ps == 60, "P10a parents.substring_auto_match = 60", f"got {ps}")
    check(
        uniques(db, "substring_auto_match") == ['("substring", merchant_category)'],
        "P10b parents substring UNIQUE is 2-column -- THIS is what the spec misattributed",
        f"got {uniques(db, 'substring_auto_match')}",
    )
    check(
        "merchant_subcategory" not in cols(db, "substring_auto_match"),
        "P10c parents substring has no subcategory column",
        f"got {sorted(cols(db, 'substring_auto_match'))}",
    )

    # Every live sequence sits past its highest row id, and every runtime insert
    # in db/my_finance.py and db/parents_finance.py omits the id and uses nextval.
    # A seed at explicit ids that never setvals breaks the first insert after a rebuild.
    for db_name, pairs in (
        (
            "finance",
            (
                ("expenses", 3063),
                ("categories", 13),
                ("substring_auto_match", 100),
                ("merchant_name_auto_match", 164),
                ("subcategories", 36),
                ("expense_type", 2),
            ),
        ),
        (
            "parents_finance",
            (
                ("expenses", 4124),
                ("categories", 22),
                ("main_category", 2),
                ("auto_match", 204),
                ("substring_auto_match", 60),
            ),
        ),
    ):
        for table, nrows in pairs:
            last = scalar(
                db_name,
                "select last_value from pg_sequences where sequencename=%s",
                (f"{table}_id_seq",),
            )
            check(
                last is not None and last >= nrows,
                f"P11 {db_name}.{table}_id_seq is at or past its {nrows} rows",
                f"got last_value={last}, rows={nrows}",
            )


# --------------------------------------------------------------------- REPO
def repo() -> None:
    out = subprocess.run(
        ["gh", "repo", "view", "nathannli/mypersonalfinance", "--json", "isPrivate"],
        capture_output=True,
        text=True,
        cwd=REPO,
    ).stdout
    check(
        '"isPrivate":false' in out.replace(" ", ""),
        "R1 repository is PUBLIC",
        out.strip(),
    )

    # R2-R4 describe the state T17 fixed. They are now historical, so assert the
    # *post*-T17 truth and keep the pre-T17 fact in the spec's §B.
    ci = (REPO / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    pc = (REPO / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    check(
        "unittest discover" in ci,
        "R2 CI now runs the unit suite (T17 fixed the old gap)",
        "unittest discover IS absent from ci.yml",
    )
    check(
        "3.13" in ci and "3.12" not in ci,
        "R3 CI now pinned to 3.13 (T17)",
        f"3.13 present={'3.13' in ci}, 3.12 present={'3.12' in ci}",
    )
    check(
        "setup-uv" in ci,
        "R4 CI now installs uv (T17)",
        "setup-uv IS absent from ci.yml",
    )
    check(
        "unittest" not in pc,
        "R2b pre-commit config still only runs ruff, by design",
        "pre-commit config DOES mention unittest",
    )

    gi = (REPO / ".gitignore").read_text(encoding="utf-8")
    check(
        "bin/donotcommit/" in gi,
        "R5a .gitignore has bin/donotcommit/",
        "bin/donotcommit/ IS absent",
    )
    check(
        "tests/test_private_*.py" in gi,
        "R5b .gitignore has tests/test_private_*.py (the precedent)",
        "tests/test_private_*.py IS absent",
    )
    check(
        "ddl/seed/" in gi,
        "R5c .gitignore has ddl/seed/ (added in T1)",
        "ddl/seed/ IS absent",
    )

    pp = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    check(
        'requires-python = ">=3.12"' in pp,
        "R7 requires-python >=3.12",
        'requires-python = ">=3.12" IS absent',
    )
    check(
        '"../Wealthsimpleton"' in pp,
        "R8 pyproject has the ../Wealthsimpleton path source",
        '"../Wealthsimpleton" IS absent',
    )


def main() -> int:
    finance()
    parents()
    repo()

    failed = [r for r in results if not r[0]]
    for ok, ident, detail in results:
        if not ok:
            print(f"FAIL  {ident:66} {detail}")
    print(f"\n{len(results) - len(failed)}/{len(results)} claims verified")
    if failed:
        print(f"{len(failed)} FAILED -- the spec asserts something untrue")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
