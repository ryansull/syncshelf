"""Matcher calibration (SPEC §3.4a.13): run the real matcher over a Goodreads library export,
read-only, against live Hardcover search through the limiter, and list every pair in the
**near band** for a person to label right or wrong.

    uv run python tests/live/calibrate_matcher.py --token-var HARDCOVER_TOKEN_ALICE

Each book is searched as the fuzzy step searches it (the title filtered to its author, then
unfiltered) and each candidate scored. A pair is near when its title scores 80–98, its author
80–95, or, for the top candidate, its margin over the next is under 15 (measured as the
auto-accept rule measures it): the band around the thresholds (60 / 95 / 90 / 10) where a
label tells most. The exact ids (the Goodreads
mapping, ISBN-13, ISBN-10) are looked up too; where they name a book, ``id_agrees`` says
whether the candidate is that book, a strong hint for the label.

The pairs go to ``--out`` (default ``local/matcher_calibration.yaml``, git-ignored), each
with ``label: null`` for a person to set to ``right`` or ``wrong``. They hold public data only
(titles, authors, Hardcover and Goodreads book ids), but they list a real library, so they
stay in ``local/`` until labelled pairs are copied into ``tests/golden/matcher.yaml``.
Nothing is written to Hardcover.
"""

import argparse
import asyncio
import io
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml
from record import HARDCOVER_URL, ROOT, read_env

from syncshelf.clients.goodreads_csv import CsvRow, parse_export
from syncshelf.clients.hardcover import HardcoverClient
from syncshelf.sync.limiter import HardcoverLimiter
from syncshelf.sync.matcher import (
    Hit,
    MatchInput,
    Scored,
    auto_accepts,
    is_likely,
    match,
    search_candidates,
)

NEAR_TITLE = (80.0, 98.0)
NEAR_AUTHOR = (80.0, 95.0)
NEAR_MARGIN = 15.0


def near_pairs(row: CsvRow, hits: Sequence[Hit], id_book: int | None) -> list[dict[str, Any]]:
    """The pairs of ``row`` and its search ``hits`` (best first) in the near band, each as it
    is written for labelling."""
    pairs: list[dict[str, Any]] = []
    for rank, hit in enumerate(hits):
        scored = hit.scored
        top = rank == 0
        others = [h.scored for h in hits if h is not hit]
        margin = _margin(scored, others) if top and others else None
        near = (
            NEAR_TITLE[0] <= scored.title <= NEAR_TITLE[1]
            or NEAR_AUTHOR[0] <= scored.author <= NEAR_AUTHOR[1]
            or (margin is not None and margin < NEAR_MARGIN)
        )
        if not near:
            continue
        ranked = [scored, *(h.scored for h in hits if h is not hit)]
        pairs.append(
            {
                "gr_book_id": row.book_id,
                "source_title": row.title,
                "source_author": row.author,
                "rank": rank + 1,
                "book_id": hit.book_id,
                "title": hit.document.get("title"),
                "authors": list(hit.document.get("author_names") or []),
                "title_score": round(scored.title, 1),
                "full_title_score": round(scored.full_title, 1),
                "author_score": round(scored.author, 1),
                "margin": None if margin is None else round(margin, 1),
                "auto_accepts": auto_accepts(ranked) if top else None,
                "likely": is_likely(ranked) if top else None,
                "id_book": id_book,
                "id_agrees": None if id_book is None else id_book == hit.book_id,
                "label": None,
            }
        )
    return pairs


def _margin(top: Scored, others: Sequence[Scored]) -> float:
    """The top candidate's lead as the auto-accept rule measures it: over full titles when
    its own is exact, so an adaptation sharing the part before ':' isn't a rival (#138)."""
    if top.full_title == 100:
        return top.full_title - max(o.full_title for o in others)
    return top.title - max(o.title for o in others)


async def calibrate(client: HardcoverClient, rows: Sequence[CsvRow]) -> list[dict[str, Any]]:
    """Every near-band pair over ``rows``: the exact ids first (batched), then one search per
    book. Only reads."""
    inputs = [MatchInput(r.book_id, r.title, r.author, r.isbn13, r.isbn10) for r in rows]
    exact = await match(client, inputs, accept_fuzzy=False, exact_only=True)
    pairs: list[dict[str, Any]] = []
    for row in rows:
        hit = exact[row.book_id].hit
        hits = await search_candidates(client, row.title, row.author)
        pairs.extend(near_pairs(row, hits, hit.book_id if hit is not None else None))
    return pairs


def _line(pair: dict[str, Any]) -> str:
    agrees = {True: "id= ", False: "id!=", None: "    "}[pair["id_agrees"]]
    margin = "" if pair["margin"] is None else f" m{pair['margin']:>5}"
    return (
        f"{agrees} t{pair['title_score']:>5} a{pair['author_score']:>5}{margin}  "
        f"{pair['source_title']!r} -> #{pair['rank']} {pair['title']!r} ({pair['book_id']})"
    )


async def run(token: str, export: Path, out: Path) -> None:
    rows = parse_export(export.read_bytes())
    # Gated like production, with no reserve: a few searches per book, all reads.
    client = HardcoverClient(HARDCOVER_URL, token, gate=HardcoverLimiter("calibrate", reserve=0))
    try:
        pairs = await calibrate(client, rows)
    finally:
        await client.aclose()
    # A Windows console may not take every character a title has.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(errors="replace")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        yaml.safe_dump({"pairs": pairs}, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    for pair in pairs:
        print(_line(pair))
    print(f"{len(pairs)} near-band pairs from {len(rows)} books, written to {out}")


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--token-var", required=True, help="the .env variable holding the PAT")
    parser.add_argument("--file", type=Path, default=ROOT / "local/goodreads_library_export.csv")
    parser.add_argument("--out", type=Path, default=ROOT / "local/matcher_calibration.yaml")
    parser.add_argument("--env", type=Path, default=ROOT / ".env")
    args = parser.parse_args()
    token = read_env(args.env).get(args.token_var)
    if not token:
        sys.exit(f"{args.token_var} isn't set in {args.env}")
    asyncio.run(run(token, args.file, args.out))


if __name__ == "__main__":
    main()
