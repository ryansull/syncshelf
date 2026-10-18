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
"""The title score a fuzzy top candidate needs to auto-link (with the author and margin)."""

AUTO_ACCEPT_AUTHOR = 90.0
"""The author score a fuzzy top candidate needs to auto-link (with the title and margin)."""

LIKELY_TITLE = 85.0
"""The title score for ``check_books``'s ``likely`` [assembly choice; calibrated in 4a]."""

LIKELY_AUTHOR = 80.0
"""The author score for ``check_books``'s ``likely`` [assembly choice; calibrated in 4a]."""

MARGIN = 10.0
"""Every other candidate's title score must be at least this many points below the top one."""

_SERIES = re.compile(r"\s*\([^()]*#\s*\d+(?:\.\d+)?\s*\)\s*$")
_LEADING_ARTICLE = re.compile(r"^(?:the|a|an)\s+")


@dataclass(frozen=True)
class Scored:
    """One candidate's scores against a source, each 0–100."""

    title: float
    author: float


def normalise(text: str) -> str:
    """The one normalisation applied to both sides before scoring, in the order §3.4a.5
    states.

    Step 4 drops the punctuation that steps 6 (the trailing ``(… #N)``) and 7 (``Last,
    First``) look for, so as stated they can't fire. #105 asks which order is meant; they're
    built as written until it's decided.
    """
    # 1. NFKD, then strip diacritics (combining marks).
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    # 2. casefold; 3. & → and.
    text = text.casefold().replace("&", " and ")
    # 4. every punctuation mark becomes a space, then whitespace collapses.
    text = "".join(c if c.isalnum() or c.isspace() else " " for c in text)
    text = " ".join(text.split())
    # 5. a leading article.
    text = _LEADING_ARTICLE.sub("", text)
    # 6. a trailing "(Series Name, #N)".
    text = _SERIES.sub("", text)
    # 7. "Last, First" → "First Last", only for exactly one comma.
    parts = text.split(",")
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        text = f"{parts[1].strip()} {parts[0].strip()}"
    return text


def title_score(source: str, candidate: str) -> float:
    """``max(token_sort_ratio(full), token_sort_ratio(title before ':'))`` on normalised text,
    so a subtitle on either side doesn't sink a match. A side with nothing to compare scores
    0, never the 100 two empty strings would."""
    full = _ratio(normalise(source), normalise(candidate))
    if ":" not in source and ":" not in candidate:
        return full
    main = _ratio(normalise(source.split(":", 1)[0]), normalise(candidate.split(":", 1)[0]))
    return max(full, main)


def author_score(source: str, candidates: Iterable[str]) -> float:
    """The best ``token_sort_ratio`` of the source's primary author against each of the
    candidate's authors; 0 when either side has none."""
    want = normalise(source)
    return max((_ratio(want, normalise(name)) for name in candidates), default=0.0)


def auto_accepts(ranked: Sequence[Scored]) -> bool:
    """Whether the top of ``ranked`` passes the fuzzy auto-accept rule: title ≥ 95, author
    ≥ 90, and every other candidate's title at least 10 points lower."""
    return _passes(ranked, AUTO_ACCEPT_TITLE, AUTO_ACCEPT_AUTHOR)


def is_likely(ranked: Sequence[Scored]) -> bool:
    """Whether the top of ``ranked`` is in ``check_books``'s ``likely`` band: title ≥ 85,
    author ≥ 80, with the same margin."""
    return _passes(ranked, LIKELY_TITLE, LIKELY_AUTHOR)


def _passes(ranked: Sequence[Scored], title: float, author: float) -> bool:
    # ranked[0] is the candidate under test; the margin holds against all the others, so the
    # rest needn't be sorted by title.
    if not ranked:
        return False
    top = ranked[0]
    if top.title < title or top.author < author:
        return False
    return all(other.title <= top.title - MARGIN for other in ranked[1:])


def _ratio(a: str, b: str) -> float:
    return token_sort_ratio(a, b) if a and b else 0.0
