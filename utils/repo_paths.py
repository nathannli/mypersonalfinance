"""Repository-root resolution for private transaction-LLM artifacts.

Paths resolve from this module's own ``__file__`` so direct CLI runs and
cron runs (which start from different working directories) agree on where
the private gold set and approval record live.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

PRIVATE_GOLD_FILENAME = ".transaction-llm-gold.json"
PRIVATE_APPROVAL_FILENAME = ".transaction-llm-approval.json"

# Private transaction-web-enrichment artifacts (V11). Ignore rules for these
# must exist before any of them is created.
PRIVATE_RESEARCH_DIRNAME = ".transaction-web-research"
RESEARCH_REVIEW_FILENAME = ".transaction-web-research-approvals.json"
RESEARCH_LOCK_FILENAME = ".transaction-web-research.lock"
SUGGESTION_FILENAME = ".transaction-category-suggestions.json"


def repo_root() -> Path:
    """Return the repository root, independent of the process CWD."""
    return REPO_ROOT


def private_gold_path() -> Path:
    """Path of the ignored, user-approved private gold set."""
    return REPO_ROOT / PRIVATE_GOLD_FILENAME


def private_approval_path() -> Path:
    """Path of the ignored, per-database write-approval record."""
    return REPO_ROOT / PRIVATE_APPROVAL_FILENAME
