"""The matcher's normalisation and scoring helpers (SPEC §3.4a.5).

Phase 3 brings these and the fuzzy step's search (:func:`search_candidates`), which
``check_books`` uses. Phase 4a adds the chain (:func:`match`): the Goodreads mapping, then
ISBN-13, then ISBN-10, then fuzzy, the first accepted hit winning.

The thresholds are starting values, not measured ones (§3.4a.5). They're pinned by
``tests/golden/matcher.yaml`` and calibrated in 4a, so they live here as named constants.
"""

import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from rapidfuzz.fuzz import token_sort_ratio

from syncshelf.clients.hardcover import (
    BOOKS_BY_ID,
    BY_GOODREADS,
    BY_ISBN10,
    BY_ISBN13,
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
    "ID_BATCH",
    "LIKELY_AUTHOR",
    "LIKELY_TITLE",
    "MARGIN",
    "TITLE_FLOOR",
    "Candidate",
    "Hit",
    "IdHit",
    "MatchInput",
    "Method",
    "Outcome",
    "QueueReason",
    "Scored",
    "author_score",
    "auto_accepts",
    "full_title_score",
    "goodreads_mappings",
    "is_likely",
    "match",
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

ID_BATCH = 200
"""Ids per batched lookup: well under Hardcover's silent 1,000-row cap on a list field, even
when an id maps to several rows."""

Method = Literal["human", "gr_mapping", "isbn13", "isbn10", "asin", "fuzzy"]
"""A link's ``method``: the chain step (or person) that made it."""

METHOD_RANK: dict[str, int] = {
    "fuzzy": 0,
    "isbn13": 1,
    "isbn10": 1,
    "asin": 1,
    "gr_mapping": 2,
    "human": 3,
}
"""Authority (§3.4a.4 rule 5): a re-match replaces a link only with a strictly stronger
method, and nothing replaces ``human``."""

QueueReason = Literal[
    "no_hit",
    "id_title_mismatch",
    "fuzzy_low",
    "fuzzy_ambiguous",
    "not_in_library",
    "profile_conflict",
    "stale",
    "ambiguous_mapping",
]

MAX_CANDIDATES = 5
"""Candidates kept on a queue entry."""
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


# --- the chain's id steps -------------------------------------------------------------------


@dataclass(frozen=True)
class IdHit:
    """An exact id match (the Goodreads mapping, an ISBN, an ASIN), already following
    ``canonical_id``: ``book_id`` is the book that counts (§3.4a.5)."""

    book_id: int
    edition_id: int | None
    """The edition the id names: informational, and the RSS-only read's edition (§3.3.5)."""
    title: str
    """The title Hardcover gave with the hit, for the title floor."""
    method: Method
    reading_format_id: int | None = None
    """The edition's format: 1 physical, 2 audio, 3 both, 4 ebook, or unknown."""


async def goodreads_mappings(client: HardcoverClient, ids: Iterable[str]) -> dict[str, list[IdHit]]:
    """The chain's first step (§3.4a.5): each Goodreads edition id's ``book_mappings``
    (platform 1), as the distinct books it names after following ``canonical_id``, oldest
    mapping first. Batched, at one request per :data:`ID_BATCH` ids. An id with no mapping is
    left out; one naming several books is ambiguous (#185)."""
    wanted = sorted(set(ids))
    hits: dict[str, list[IdHit]] = {}
    for start in range(0, len(wanted), ID_BATCH):
        result = await client.execute(BY_GOODREADS, {"ids": wanted[start : start + ID_BATCH]})
        for row in result.data.get("book_mappings") or []:
            external_id, book_id = row.get("external_id"), row.get("book_id")
            if external_id is None or book_id is None:
                continue
            book = row.get("book") or {}
            edition = row.get("edition") or {}
            hit = IdHit(
                # A merged book counts as the book it was merged into.
                book_id=book.get("canonical_id") or book_id,
                edition_id=row.get("edition_id"),
                title=book.get("title") or "",
                method="gr_mapping",
                reading_format_id=edition.get("reading_format_id"),
            )
            _add_distinct(hits.setdefault(str(external_id), []), hit)
    return hits


async def isbn_editions(
    client: HardcoverClient, method: Literal["isbn13", "isbn10"], isbns: Iterable[str]
) -> dict[str, list[IdHit]]:
    """The ISBN steps (§3.4a.5): each ISBN's editions, as the distinct books they belong to
    after ``canonical_id``, oldest edition first. Batched like the mapping step."""
    query, key = (BY_ISBN13, "isbn_13") if method == "isbn13" else (BY_ISBN10, "isbn_10")
    wanted = sorted(set(isbns))
    hits: dict[str, list[IdHit]] = {}
    for start in range(0, len(wanted), ID_BATCH):
        result = await client.execute(query, {"v": wanted[start : start + ID_BATCH]})
        for row in result.data.get("editions") or []:
            isbn, book_id = row.get(key), row.get("book_id")
            if isbn is None or book_id is None:
                continue
            book = row.get("book") or {}
            hit = IdHit(
                book_id=book.get("canonical_id") or book_id,
                edition_id=row.get("id"),
                title=book.get("title") or "",
                method=method,
                reading_format_id=row.get("reading_format_id"),
            )
            _add_distinct(hits.setdefault(str(isbn), []), hit)
    return hits


def _add_distinct(hits: list[IdHit], hit: IdHit) -> None:
    if all(h.book_id != hit.book_id for h in hits):
        hits.append(hit)


# --- the chain -----------------------------------------------------------------------------


@dataclass(frozen=True)
class MatchInput:
    """A source row as the matcher sees it."""

    source_id: str
    """The Goodreads ``Book Id``, for the mapping step."""
    title: str
    author: str | None
    isbn13: str | None = None
    isbn10: str | None = None


@dataclass(frozen=True)
class Candidate:
    """One book a queued row might be, as ``match_candidates`` keeps it."""

    book_id: int
    edition_id: int | None
    method: Method
    title_score: float | None
    author_score: float | None = None


@dataclass(frozen=True)
class Outcome:
    """What the chain found for one row: an accepted hit to link, or why it's queued."""

    hit: IdHit | None
    reason: QueueReason | None = None
    candidates: tuple[Candidate, ...] = ()


async def match(
    client: HardcoverClient, rows: Sequence[MatchInput], *, accept_fuzzy: bool
) -> dict[str, Outcome]:
    """Run the chain over ``rows`` (§3.4a.5): the Goodreads mapping, then ISBN-13, then
    ISBN-10, then fuzzy. The first step with a hit decides:

    - **an id hit** (mapping, ISBN) is accepted if its title scores at least
      :data:`TITLE_FLOOR`; below that the row is queued ``id_title_mismatch``, with the hit as
      its top candidate. An id naming several books is queued ``ambiguous_mapping``, each
      book a candidate (#185);
    - **fuzzy** search links only with ``accept_fuzzy`` (never in the backfill) and when the
      top hit passes the auto-accept rule; otherwise the row is queued ``fuzzy_ambiguous``
      (it would pass but for the margin) or ``fuzzy_low``, keeping up to 5 hits scoring at
      least the floor, or ``no_hit`` when none does.

    The id steps are batched across rows; fuzzy costs at most 2 searches and 1 hydrate per
    row that reaches it.
    """
    outcomes: dict[str, Outcome] = {}
    rest = list(rows)
    rest = _decide(rest, await goodreads_mappings(client, [r.source_id for r in rest]), outcomes)
    steps: tuple[tuple[Literal["isbn13", "isbn10"], Callable[[MatchInput], str | None]], ...] = (
        ("isbn13", lambda r: r.isbn13),
        ("isbn10", lambda r: r.isbn10),
    )
    for method, isbn_of in steps:
        keys = {r.source_id: key for r in rest if (key := _isbn(isbn_of(r)))}
        if not keys:
            continue
        found = await isbn_editions(client, method, keys.values())
        by_row = {sid: found[key] for sid, key in keys.items() if key in found}
        rest = _decide(rest, by_row, outcomes)
    for row in rest:
        outcomes[row.source_id] = await _fuzzy(client, row, accept_fuzzy)
    return outcomes


def _decide(
    rows: list[MatchInput], found: Mapping[str, list[IdHit]], outcomes: dict[str, Outcome]
) -> list[MatchInput]:
    """Decide each row an id step ``found`` (by source id), returning the rows it didn't."""
    rest: list[MatchInput] = []
    for row in rows:
        hits = found.get(row.source_id)
        if not hits:
            rest.append(row)
            continue
        scored = [(hit, title_score(row.title, hit.title)) for hit in hits]
        candidates = tuple(
            Candidate(h.book_id, h.edition_id, h.method, score) for h, score in scored
        )[:MAX_CANDIDATES]
        if len(hits) > 1:
            outcomes[row.source_id] = Outcome(None, "ambiguous_mapping", candidates)
        elif scored[0][1] < TITLE_FLOOR:
            outcomes[row.source_id] = Outcome(None, "id_title_mismatch", candidates)
        else:
            outcomes[row.source_id] = Outcome(hits[0])
    return rest


async def _fuzzy(client: HardcoverClient, row: MatchInput, accept: bool) -> Outcome:
    hits = await search_candidates(client, row.title, row.author)
    kept = [h for h in hits if h.scored.title >= TITLE_FLOOR][:MAX_CANDIDATES]
    if not kept:
        return Outcome(None, "no_hit")
    # Every hit follows canonical_id before it counts: one hydrate of the ones kept.
    result = await client.execute(BOOKS_BY_ID, {"ids": [h.book_id for h in kept]})
    canonical = {b["id"]: b.get("canonical_id") for b in result.data.get("books") or []}
    candidates = tuple(
        Candidate(
            canonical.get(h.book_id) or h.book_id,
            None,
            "fuzzy",
            h.scored.title,
            h.scored.author if row.author else None,
        )
        for h in kept
    )
    ranked = [h.scored for h in hits]
    if accept and row.author and auto_accepts(ranked):
        top = candidates[0]
        title = str(kept[0].document.get("title") or "")
        return Outcome(IdHit(top.book_id, None, title, "fuzzy"))
    # Ambiguous: it would be accepted but for the runner-up's margin. Anything else, the
    # backfill's refusal to auto-link included, is low.
    top = ranked[0]
    clears = top.title >= AUTO_ACCEPT_TITLE and top.author >= AUTO_ACCEPT_AUTHOR
    ambiguous = bool(row.author) and clears and not auto_accepts(ranked)
    return Outcome(None, "fuzzy_ambiguous" if ambiguous else "fuzzy_low", candidates)


def _isbn(value: str | None) -> str | None:
    """An ISBN as Hardcover stores it: digits, with a final ``X`` kept upper case."""
    if not value:
        return None
    cleaned = "".join(c for c in value if c.isalnum()).upper()
    return cleaned or None
