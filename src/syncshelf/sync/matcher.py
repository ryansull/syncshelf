"""The matcher's normalisation and scoring helpers (SPEC §3.4a.5).

Phase 3 brings these and the fuzzy step's search (:func:`search_candidates`), which
``check_books`` uses. The rest of the matcher chain (Goodreads mapping, ISBNs) and the
unmatched queue build on them in phase 4a.

The thresholds are starting values, not measured ones (§3.4a.5). They're pinned by
``tests/golden/matcher.yaml`` and calibrated in 4a, so they live here as named constants.
"""

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from rapidfuzz.fuzz import token_sort_ratio

from syncshelf.clients.hardcover import (
    SEARCH,
    HardcoverClient,
    document_id,
    search_documents,
    search_filter,
)

__all__ = [
    "AUTO_ACCEPT_AUTHOR",
    "AUTO_ACCEPT_TITLE",
    "CANDIDATE_HITS",
    "LIKELY_AUTHOR",
    "LIKELY_TITLE",
    "MARGIN",
    "TITLE_FLOOR",
    "Hit",
    "Scored",
    "author_score",
    "auto_accepts",
    "full_title_score",
    "is_likely",
    "normalise",
    "search_candidates",
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

CANDIDATE_HITS = 5
"""Search hits scored per source title in the fuzzy step."""

_SERIES = re.compile(r"\s*\([^()]*#\s*\d+(?:\.\d+)?\s*\)\s*$")
_LEADING_ARTICLE = re.compile(r"^(?:the|a|an)\s+")


@dataclass(frozen=True)
class Scored:
    """One candidate's scores against a source, each 0–100."""

    title: float
    author: float
    full: float | None = None
    """:func:`full_title_score`, when it differs from ``title``; ``None`` means the same."""

    @property
    def full_title(self) -> float:
        return self.title if self.full is None else self.full


@dataclass(frozen=True)
class Hit:
    """One search hit for a source title, with its scores and its Typesense document."""

    book_id: int
    scored: Scored
    document: dict[str, Any]


async def search_candidates(client: HardcoverClient, title: str, author: str | None) -> list[Hit]:
    """The fuzzy step's search (§3.4a.5): search by the normalised title, filtered to the
    primary author, and once more without the filter if that finds nothing; then score each
    hit, best first. Up to :data:`CANDIDATE_HITS` hits, costing at most 2 searches.

    With no ``author`` the author score is set aside (scored 100), so only a caller that
    requires one can treat a hit as certain (#142). Hits aren't hydrated here: callers batch
    one ``books`` lookup over every hit they keep.
    """
    variables: dict[str, Any] = {
        "q": normalise(title) or title,
        "filter": search_filter(author or None, None),
        "sort": None,
        "per": CANDIDATE_HITS,
        "page": 1,
    }
    _, docs = search_documents(await client.execute(SEARCH, variables))
    if not docs and variables["filter"]:
        variables["filter"] = None
        _, docs = search_documents(await client.execute(SEARCH, variables))
    hits = [
        Hit(
            book_id,
            Scored(
                title_score(title, d.get("title") or ""),
                author_score(author, d.get("author_names") or []) if author else 100.0,
                full_title_score(title, d.get("title") or ""),
            ),
            d,
        )
        for d in docs
        if (book_id := document_id(d)) is not None
    ]
    # Ties on the title break on the full title (#138), then the author.
    hits.sort(key=lambda h: (h.scored.title, h.scored.full_title, h.scored.author), reverse=True)
    return hits


def normalise(text: str, *, author: bool = False) -> str:
    """The one normalisation applied to both sides before scoring (§3.4a.5, #105).

    The trailing Goodreads series parenthetical and, for ``author`` names only, the
    ``Last, First`` swap are applied before punctuation is dropped, since they need the
    brackets and the comma. On a title the swap would turn any single-comma title around.
    """
    # NFKD, then strip diacritics (combining marks); casefold; & → and.
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    text = text.casefold().replace("&", " and ")
    # A trailing "(Series Name, #N)".
    text = _SERIES.sub("", text)
    # Author names only: "Last, First" → "First Last", for exactly one comma.
    if author:
        parts = text.split(",")
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            text = f"{parts[1].strip()} {parts[0].strip()}"
    # Every punctuation mark becomes a space, then whitespace collapses.
    text = "".join(c if c.isalnum() or c.isspace() else " " for c in text)
    text = " ".join(text.split())
    # A leading article.
    return _LEADING_ARTICLE.sub("", text)


def full_title_score(source: str, candidate: str) -> float:
    """``token_sort_ratio`` of the whole normalised titles, subtitles included: what tells a
    book from its adaptation when both share the part before ':' (#138)."""
    return _ratio(normalise(source), normalise(candidate))


def title_score(source: str, candidate: str) -> float:
    """``max(token_sort_ratio(full), token_sort_ratio(title before ':'))`` on normalised text,
    so a subtitle on either side doesn't sink a match. A side with nothing to compare scores
    0, never the 100 two empty strings would."""
    full = full_title_score(source, candidate)
    if ":" not in source and ":" not in candidate:
        return full
    main = _ratio(normalise(source.split(":", 1)[0]), normalise(candidate.split(":", 1)[0]))
    return max(full, main)


def author_score(source: str, candidates: Iterable[str]) -> float:
    """The best ``token_sort_ratio`` of the source's primary author against each of the
    candidate's authors; 0 when either side has none."""
    want = normalise(source, author=True)
    return max((_ratio(want, normalise(name, author=True)) for name in candidates), default=0.0)


def auto_accepts(ranked: Sequence[Scored]) -> bool:
    """Whether the top of ``ranked`` passes the fuzzy auto-accept rule: title ≥ 95, author
    ≥ 90, and every other candidate's title at least 10 points lower (full titles, when the
    top one's is exact)."""
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
    if top.full_title == 100:
        # An exact full title: others that only reach it through the part before ':' (an
        # adaptation, "…: A Graphic Novel") don't make it ambiguous (#138).
        return all(other.full_title <= top.full_title - MARGIN for other in ranked[1:])
    return all(other.title <= top.title - MARGIN for other in ranked[1:])


def _ratio(a: str, b: str) -> float:
    return token_sort_ratio(a, b) if a and b else 0.0
