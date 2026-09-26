"""Deterministic merchant query derivation and evidence relevance prefilter.

Raw statement descriptors are unusable as search queries: they return
confident wrong-topic results rather than no results, so the query is derived
deterministically and the evidence is junk-filtered (V44, V45, V53, V56).

Relevance matching is only a prefilter. It never proves merchant identity;
only explicit human approval of an exact packet hash establishes eligibility.

Changing the derivation order, the prefix/location/stopword sets, or the token
rules requires bumping ``RESEARCH_QUERY_VERSION`` in ``services.research_packets``.
"""

from __future__ import annotations

import re
import unicodedata

from services.tinyfish_research import ResearchIrrelevantError

# Longest first, so a longer prefix can never be shadowed by a shorter one.
PROCESSOR_PREFIXES: tuple[str, ...] = tuple(
    sorted(
        (
            "paypal *",
            "sp ",
            "sq *",
            "tst-",
            "stripe-",
            "link.com*",
            "airwalxsg*",
            "msbill.info",
        ),
        key=len,
        reverse=True,
    )
)

LOCATION_FRAGMENTS: frozenset[str] = frozenset(
    {
        # provinces and common abbreviations
        "on",
        "ont",
        "ontario",
        "bc",
        "ab",
        "alta",
        "qc",
        "que",
        "ns",
        "nb",
        "mb",
        "sk",
        "nl",
        "pe",
        # cities and the truncations seen in real descriptors
        "toronto",
        "toront",
        "mississauga",
        "mississaug",
        "brampton",
        "markham",
        "scarborough",
        "scarboro",
        "etobicoke",
        "guelph",
        "ottawa",
        "vaughan",
        "richmond",
        "calgary",
        "edmonton",
        "winnipeg",
        "vancouver",
        "victoria",
        "montreal",
        "montr",
        "quebec",
        "halifax",
        # countries, country-code TLDs, and regions
        "canada",
        "ca",
        "usa",
        "us",
        "uk",
        "gb",
        "sg",
        "hk",
        "cn",
        "au",
        "nz",
        "ie",
        "de",
        "fr",
    }
)

STOPWORDS: frozenset[str] = frozenset(
    {
        "and",
        "the",
        "for",
        "with",
        "from",
        "your",
        "you",
        "our",
        "this",
        "that",
        "inc",
        "ltd",
        "llc",
        "corp",
        "co",
        "company",
        "group",
        "holdings",
        "services",
        "service",
        "store",
        "stores",
        "shop",
        "online",
        "web",
        "www",
        "com",
        "net",
        "org",
        "payment",
        "payments",
        "purchase",
        "order",
        "orders",
        "invoice",
        "ref",
        "reference",
        "pos",
        "debit",
        "credit",
        "card",
        "visa",
        "mastercard",
        "amex",
        "transaction",
        "dated",
        "date",
        "time",
        "city",
        "street",
        "ave",
        "avenue",
        "road",
        "blvd",
        "suite",
        "unit",
        "building",
        "plaza",
        "mall",
        "centre",
        "center",
        "north",
        "south",
        "east",
        "west",
        "new",
        "via",
    }
)

MIN_SIGNIFICANT_TOKEN_LENGTH = 4

# Alphanumeric runs: an isolated match is therefore bounded by a non-alphanumeric
# character or a string edge on both sides (V56).
_TOKEN_PATTERN = re.compile(r"[^\W_]+", re.UNICODE)


def normalize_for_matching(value: str) -> str:
    """The one normalization shared by query tokens and page text (V56)."""

    return unicodedata.normalize("NFKC", value).replace("\xa0", " ").casefold()


def query_tokens(derived_query: str) -> tuple[str, ...]:
    return tuple(_TOKEN_PATTERN.findall(normalize_for_matching(derived_query)))


def significant_tokens(derived_query: str) -> tuple[str, ...]:
    """Versioned junk filter: length and stopword rules, deterministically sorted."""

    tokens = {
        token
        for token in query_tokens(derived_query)
        if len(token) >= MIN_SIGNIFICANT_TOKEN_LENGTH and token not in STOPWORDS
    }
    return tuple(sorted(tokens))


def matched_relevance_tokens(
    tokens: tuple[str, ...], texts: tuple[str, ...]
) -> tuple[str, ...]:
    """Whole-token matches of ``tokens`` across ``texts``, in token order."""

    present: set[str] = set()
    for text in texts:
        if text:
            present.update(_TOKEN_PATTERN.findall(normalize_for_matching(text)))
    return tuple(token for token in tokens if token in present)


def _strip_processor_prefix(value: str) -> str:
    for prefix in PROCESSOR_PREFIXES:
        if value.startswith(prefix):
            return value[len(prefix) :].lstrip()
    return value


def derive_query(raw_descriptor: str) -> str:
    """Fixed-order derivation of the search term from a raw descriptor (V44).

    Order matters: processor prefixes are removed before separator collapsing
    because prefixes are matched literally (``paypal *``), and trailing
    store/order/phone digit runs and location fragments are removed together so
    interleaved forms such as ``acme 1234 toronto`` are fully reduced.

    An empty or digits-only result is a typed failure and issues no request.
    """

    if not isinstance(raw_descriptor, str):
        raise ResearchIrrelevantError("merchant descriptor must be a string")

    normalized = normalize_for_matching(raw_descriptor)

    while True:
        stripped = _strip_processor_prefix(normalized)
        if stripped == normalized:
            break
        normalized = stripped

    tokens = list(_TOKEN_PATTERN.findall(normalized))

    # Never reduce a descriptor to nothing: a single remaining token stands.
    while len(tokens) > 1 and (
        tokens[-1].isdigit() or tokens[-1] in LOCATION_FRAGMENTS
    ):
        tokens.pop()

    derived = " ".join(tokens)
    if not derived or derived.replace(" ", "").isdigit():
        raise ResearchIrrelevantError(
            "derived search term must contain a non-numeric merchant token"
        )
    return derived
