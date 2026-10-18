"""The fixture scrubber and checker (SPEC §2.9).

Recorded responses are committed to a public repo, so the scrubber is an **allowlist**: a field
survives verbatim only if it is public catalog data (titles, authors, series, ISBN/ASIN and
Hardcover book, edition and author ids). Every other leaf becomes a placeholder of the same type,
so contract tests still see the real keys, types and nullability.

A fixture is a JSON file ``{"meta": {...}, "body": ...}``. The checker accepts it only if its
body is already a fixed point of :func:`scrub` and its metadata says when and against what it
was recorded. ``tests/live/record.py`` writes fixtures through :func:`write_fixture`.
"""

import json
import re
from collections.abc import Iterable
from pathlib import Path

PLACEHOLDER_TEXT = "scrubbed"
PLACEHOLDER_DATE = "2000-01-01"
PLACEHOLDER_DATETIME = "2000-01-01T00:00:00Z"
PLACEHOLDER_URL = "https://example.com/"

PROVENANCES = frozenset({"live", "source-only"})
"""``source-only`` marks a fixture built from a service's source rather than a live instance."""

META_KEYS = ("recorded_at", "server_version", "provenance")

CATALOG: frozenset[str] = frozenset(
    {
        # Titles
        "title",
        "subtitle",
        # Identifiers
        "isbn",
        "isbn_10",
        "isbn_13",
        "isbns",
        "asin",
        "book_id",
        "edition_id",
        "author_id",
        "book.id",
        "books.id",
        "edition.id",
        "editions.id",
        "author.id",
        "authors.id",
        # Authors and series
        "author_names",
        "author.name",
        "authors.name",
        "series_names",
        "series.name",
    }
)
"""Keep rules. ``key`` keeps that key anywhere; ``parent.key`` keeps it only directly under a
dict reached through ``parent`` (list indexes are skipped). A kept value is kept whole."""

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
_URL = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_EMAIL = re.compile(r"[\w.+-]+@((?:[\w-]+\.)+[\w-]+)")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_PLACEHOLDER_EMAIL_DOMAINS = frozenset({"example.com", "example.net", "example.org"})
_PLACEHOLDER_IP_PREFIX = "192.0.2."


def scrub(value: object, keep: frozenset[str] = CATALOG) -> object:
    """Return a scrubbed copy of a JSON-shaped ``value``; the input is not modified."""
    return _scrub(value, parent=None, keep=keep)


def _kept(key: str, parent: str | None, keep: frozenset[str]) -> bool:
    return key in keep or (parent is not None and f"{parent}.{key}" in keep)


def _scrub(value: object, parent: str | None, keep: frozenset[str]) -> object:
    if isinstance(value, dict):
        return {
            k: v if _kept(k, parent, keep) else _scrub(v, parent=k, keep=keep)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_scrub(v, parent=parent, keep=keep) for v in value]
    return _placeholder(value)


def _placeholder(leaf: object) -> object:
    # bool before int: bool is an int subclass.
    if leaf is None:
        return None
    if isinstance(leaf, bool):
        return False
    if isinstance(leaf, int):
        return 0
    if isinstance(leaf, float):
        return 0.0
    if isinstance(leaf, str):
        if leaf == "":
            return ""
        if _DATE.match(leaf):
            return PLACEHOLDER_DATE
        if _DATETIME.match(leaf):
            return PLACEHOLDER_DATETIME
        if _URL.match(leaf):
            return PLACEHOLDER_URL
        return PLACEHOLDER_TEXT
    raise TypeError(f"not a JSON value: {type(leaf).__name__}")


def write_fixture(
    path: Path,
    body: object,
    *,
    recorded_at: str,
    server_version: str,
    provenance: str = "live",
) -> None:
    """Scrub ``body`` and write it to ``path`` with its metadata, creating parent directories."""
    if provenance not in PROVENANCES:
        raise ValueError(f"provenance must be one of {sorted(PROVENANCES)}")
    doc = {
        "meta": {
            "recorded_at": recorded_at,
            "server_version": server_version,
            "provenance": provenance,
        },
        "body": scrub(body),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")


def check_fixture(path: Path) -> list[str]:
    """Return every problem with one fixture file; an empty list means it may be committed."""
    name = path.name
    if path.suffix != ".json":
        return [f"{name}: only .json fixtures are supported"]
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [f"{name}: not valid JSON ({exc})"]
    if not isinstance(doc, dict) or set(doc) != {"meta", "body"}:
        return [f"{name}: must be an object with exactly 'meta' and 'body'"]

    problems: list[str] = []
    meta = doc["meta"]
    if not isinstance(meta, dict):
        problems.append(f"{name}: 'meta' must be an object")
        meta = {}
    for key in META_KEYS:
        if not isinstance(meta.get(key), str) or not meta[key]:
            problems.append(f"{name}: meta.{key} is missing or empty")
    if "provenance" in meta and meta["provenance"] not in PROVENANCES:
        problems.append(f"{name}: meta.provenance must be one of {sorted(PROVENANCES)}")

    body = doc["body"]
    for where in _differences(body, scrub(body), "$"):
        problems.append(f"{name}: {where} is not scrubbed")
    for text in _strings(body):
        for match in _EMAIL.finditer(text):
            if match.group(1).lower() not in _PLACEHOLDER_EMAIL_DOMAINS:
                problems.append(f"{name}: a non-placeholder email address remains")
        for match in _IPV4.finditer(text):
            if not match.group(0).startswith(_PLACEHOLDER_IP_PREFIX):
                problems.append(f"{name}: a non-placeholder IP address remains")
    return problems


def check_tree(root: Path) -> dict[Path, list[str]]:
    """Check every fixture under ``root``, returning only the files that have problems."""
    results: dict[Path, list[str]] = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file() and p.name != ".gitkeep"):
        problems = check_fixture(path)
        if problems:
            results[path] = problems
    return results


def _differences(actual: object, expected: object, where: str) -> Iterable[str]:
    if isinstance(actual, dict) and isinstance(expected, dict):
        for key in actual:
            yield from _differences(actual[key], expected[key], f"{where}.{key}")
    elif isinstance(actual, list) and isinstance(expected, list):
        for i, (a, e) in enumerate(zip(actual, expected, strict=True)):
            yield from _differences(a, e, f"{where}[{i}]")
    elif actual != expected or type(actual) is not type(expected):
        yield where


def _strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)
