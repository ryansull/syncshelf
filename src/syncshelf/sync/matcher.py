"""The matcher's normalisation and scoring helpers (SPEC §3.4a.5).

Phase 3 brings only these: ``check_books`` scores search hits with them. The matcher chain
(Goodreads mapping, ISBNs, then fuzzy) and the unmatched queue build on them in phase 4a.

The thresholds are starting values, not measured ones (§3.4a.5). They're pinned by
``tests/golden/matcher.yaml`` and calibrated in 4a, so they live here as named constants.
"""

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from rapidfuzz.fuzz import token_sort_ratio

__all__ = [
    "AUTO_ACCEPT_AUTHOR",
    "AUTO_ACCEPT_TITLE",
    "LIKELY_AUTHOR",
    "LIKELY_TITLE",
    "MARGIN",
    "TITLE_FLOOR",
    "Scored",
    "author_score",
    "auto_accepts",
    "is_likely",
    "normalise",
    "title_score",
]

TITLE_FLOOR = 60.0
"""An id hit (Goodreads mapping, ISBN, ASIN) whose title scores below this is queued as
``id_title_mismatch`` (4a). Fuzzy candidates below it aren't kept."""

AUTO_ACCEPT_TITLE = 95.0
AUTO_ACCEPT_AUTHOR = 90.0
"""A fuzzy top candidate auto-links only at or above both (and with :data:`MARGIN`)."""

LIKELY_TITLE = 85.0
LIKELY_AUTHOR = 80.0
"""``check_books``'s ``likely`` band [assembly choice; calibrated with the matcher in 4a]."""

MARGIN = 10.0
"""The runner-up's title score must be at least this many points below the top one."""

_SERIES = re.compile(r"\s*\([^()]*#\s*\d+(?:\.\d+)?\s*\)\s*$")
_LEADING_ARTICLE = re.compile(r"^(?:the|a|an)\s+")


@dataclass(frozen=True)
class Scored:
    """One candidate's scores against a source, each 0–100."""

    title: float
    author: float


def normalise(text: str) -> str:
    """The one normalisation applied to both sides before scoring (§3.4a.5).

    Steps 6 (the trailing Goodreads series parenthetical) and 7 (``Last, First``) run before
    punctuation is dropped, since step 4 removes what they look for (#105, provisional).
    """
    # 1. NFKD, then strip diacritics (combining marks).
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    # 2. casefold; 3. & → and.
    text = text.casefold().replace("&", " and ")
    # 6. a trailing "(Series Name, #N)".
    text = _SERIES.sub("", text)
    # 7. "Last, First" → "First Last", only for exactly one comma.
    parts = text.split(",")
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        text = f"{parts[1].strip()} {parts[0].strip()}"
    # 4. every punctuation mark becomes a space, then whitespace collapses.
    text = "".join(c if c.isalnum() or c.isspace() else " " for c in text)
    text = " ".join(text.split())
    # 5. a leading article.
    return _LEADING_ARTICLE.sub("", text)


def title_score(source: str, candidate: str) -> float:
    """``max(token_sort_ratio(full), token_sort_ratio(title before ':'))`` on normalised text,
    so a subtitle on either side doesn't sink a match."""
    full = token_sort_ratio(normalise(source), normalise(candidate))
    main = token_sort_ratio(
        normalise(source.split(":", 1)[0]), normalise(candidate.split(":", 1)[0])
    )
    return max(full, main)


def author_score(source: str, candidates: Iterable[str]) -> float:
    """The best ``token_sort_ratio`` of the source's primary author against each of the
    candidate's authors; 0 when the candidate has none."""
    want = normalise(source)
    return max((token_sort_ratio(want, normalise(name)) for name in candidates), default=0.0)


def auto_accepts(ranked: Sequence[Scored]) -> bool:
    """Whether the top of ``ranked`` (best first) passes the fuzzy auto-accept rule: title
    ≥ 95, author ≥ 90, and the runner-up's title at least 10 points lower."""
    return _passes(ranked, AUTO_ACCEPT_TITLE, AUTO_ACCEPT_AUTHOR)


def is_likely(ranked: Sequence[Scored]) -> bool:
    """Whether the top of ``ranked`` is in ``check_books``'s ``likely`` band: title ≥ 85,
    author ≥ 80, with the same margin."""
    return _passes(ranked, LIKELY_TITLE, LIKELY_AUTHOR)


def _passes(ranked: Sequence[Scored], title: float, author: float) -> bool:
    if not ranked:
        return False
    top = ranked[0]
    if top.title < title or top.author < author:
        return False
    return len(ranked) < 2 or ranked[1].title <= top.title - MARGIN
