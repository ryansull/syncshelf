"""The fixture scrubber and checker (SPEC §2.9).

Recorded responses are committed to a public repo, so the scrubber is an **allowlist**: a field
survives verbatim only if it is public catalog data (titles, authors, series, ISBN/ASIN and
Hardcover book, edition and author ids). Every other leaf becomes a placeholder of the same type,
so contract tests still see the real keys, types and nullability.

A fixture is a JSON file ``{"meta": {...}, "body": ...}``. The checker accepts it only if its
body is already a fixed point of :func:`scrub` and its metadata says when and against what it
was recorded. ``tests/live/record.py`` writes fixtures through :func:`write_fixture`.

Goodreads fixtures are the exception (#44): the shelf RSS feed is kept as **native XML** under
``goodreads/``, so the parser is tested against the bytes Goodreads sends. Its metadata sits in
a ``<name>.meta.json`` sidecar, and :func:`scrub_rss` holds its per-source rules: the catalog
elements are kept, and everything personal becomes a placeholder that still parses.
"""

import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from pathlib import Path

PLACEHOLDER_TEXT = "scrubbed"
PLACEHOLDER_DATE = "2000-01-01"
PLACEHOLDER_DATETIME = "2000-01-01T00:00:00Z"
PLACEHOLDER_URL = "https://example.com/"

PLACEHOLDER_SHELF = "scrubbed"
PLACEHOLDER_RATING = "0"
"""Goodreads' "unrated", so a scrubbed rating still parses."""

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
    if path.suffix in NATIVE_SUFFIXES and path.parent.name == NATIVE_DIR:
        return _check_native(path)
    if path.suffix != ".json":
        return [f"{name}: only .json fixtures are supported here"]
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [f"{name}: not valid JSON ({exc})"]
    if not isinstance(doc, dict) or set(doc) != {"meta", "body"}:
        return [f"{name}: must be an object with exactly 'meta' and 'body'"]

    problems = _check_meta(name, doc["meta"])
    body = doc["body"]
    for where in _differences(body, scrub(body), "$"):
        problems.append(f"{name}: {where} is not scrubbed")
    problems.extend(_personal_values(name, _strings(body)))
    return problems


def _check_meta(name: str, meta: object) -> list[str]:
    problems: list[str] = []
    if not isinstance(meta, dict):
        problems.append(f"{name}: 'meta' must be an object")
        meta = {}
    for key in META_KEYS:
        if not isinstance(meta.get(key), str) or not meta[key]:
            problems.append(f"{name}: meta.{key} is missing or empty")
    if "provenance" in meta and meta["provenance"] not in PROVENANCES:
        problems.append(f"{name}: meta.provenance must be one of {sorted(PROVENANCES)}")
    return problems


def _personal_values(name: str, texts: Iterable[str]) -> list[str]:
    problems: list[str] = []
    for text in texts:
        for match in _EMAIL.finditer(text):
            if match.group(1).lower() not in _PLACEHOLDER_EMAIL_DOMAINS:
                problems.append(f"{name}: a non-placeholder email address remains")
        for match in _IPV4.finditer(text):
            if not match.group(0).startswith(_PLACEHOLDER_IP_PREFIX):
                problems.append(f"{name}: a non-placeholder IP address remains")
    return problems


def check_tree(root: Path) -> dict[Path, list[str]]:
    """Check every fixture under ``root``, returning only the files that have problems.
    A native fixture's ``.meta.json`` sidecar is checked with it, not on its own."""
    results: dict[Path, list[str]] = {}
    files = (p for p in root.rglob("*") if p.is_file() and p.name != ".gitkeep")
    for path in sorted(p for p in files if not p.name.endswith(META_SUFFIX)):
        problems = check_fixture(path)
        if problems:
            results[path] = problems
    return results


# --- native Goodreads fixtures (#44) ------------------------------------------------------

NATIVE_DIR = "goodreads"
NATIVE_SUFFIXES = frozenset({".xml"})
"""The CSV scrubber arrives with the CSV parser (#167); until then a ``.csv`` is refused."""
META_SUFFIX = ".meta.json"

RSS_CATALOG: frozenset[str] = frozenset(
    {
        "item/title",
        "item/book_id",
        "item/author_name",
        "item/isbn",
        "book/num_pages",
        "item/book_published",
        "channel/language",
        "channel/ttl",
        "image/width",
        "image/height",
    }
)
"""Elements kept verbatim, as ``parent/tag`` (the immediate parent is enough for the feed)."""

RSS_DATES = frozenset(
    {"pubDate", "lastBuildDate", "user_read_at", "user_date_added", "user_date_created"}
)
_SHELF_TITLE = re.compile(r"^.*'s bookshelf: (.*)$", re.DOTALL)
_RFC822_OFFSET = re.compile(r"([+-]\d{4}|GMT|UT|[A-Z]{3})$")
for _prefix, _uri in {
    "atom": "http://www.w3.org/2005/Atom",
    "xhtml": "http://www.w3.org/1999/xhtml",
}.items():
    ET.register_namespace(_prefix, _uri)


def scrub_rss(text: str) -> str:
    """Return a Goodreads shelf RSS document with every personal value replaced.

    Catalog elements (:data:`RSS_CATALOG`) are kept. Dates become 2000-01-01 at midnight in
    RFC 822 with their original offset, so ``user_read_at`` stays ``+0000`` and the parser
    sees the real date forms. The channel title keeps its ``…'s bookshelf: <shelf>`` form,
    which the usable-page check reads. Ratings become ``0`` (unrated), shelves a placeholder,
    reviews empty, URLs :data:`PLACEHOLDER_URL`, and other text :data:`PLACEHOLDER_TEXT`.
    Empty elements stay empty, since "no value" is a shape the parser must handle.
    """
    root = ET.fromstring(text)
    _scrub_element(root, parent="")
    body = ET.tostring(root, encoding="unicode")
    return f'<?xml version="1.0" encoding="UTF-8"?>\n{body}\n'


def _scrub_element(element: ET.Element, parent: str) -> None:
    tag = _local(element.tag)
    for key, value in element.attrib.items():
        if _URL.match(value):
            element.attrib[key] = PLACEHOLDER_URL
    if element.text is not None and element.text.strip():
        element.text = _rss_placeholder(tag, parent, element.text.strip())
    for child in element:
        _scrub_element(child, parent=tag)


def _rss_placeholder(tag: str, parent: str, text: str) -> str:
    if f"{parent}/{tag}" in RSS_CATALOG:
        return text
    if tag == "title" and (match := _SHELF_TITLE.match(text)):
        return f"{PLACEHOLDER_TEXT}'s bookshelf: {match.group(1)}"
    if tag in RSS_DATES:
        offset = _RFC822_OFFSET.search(text)
        return f"Sat, 01 Jan 2000 00:00:00 {offset.group(1) if offset else '+0000'}"
    if tag == "user_rating":
        return PLACEHOLDER_RATING
    if tag == "user_shelves":
        return PLACEHOLDER_SHELF
    if tag == "user_review":
        return ""
    if _URL.match(text):
        return PLACEHOLDER_URL
    return PLACEHOLDER_TEXT


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def write_native_fixture(
    path: Path,
    text: str,
    *,
    recorded_at: str,
    server_version: str,
    provenance: str = "live",
) -> None:
    """Scrub a Goodreads RSS document and write it to ``path`` (in a ``goodreads/``
    directory), with its metadata in the ``.meta.json`` sidecar beside it."""
    if provenance not in PROVENANCES:
        raise ValueError(f"provenance must be one of {sorted(PROVENANCES)}")
    if path.parent.name != NATIVE_DIR or path.suffix not in NATIVE_SUFFIXES:
        raise ValueError(f"a native fixture is a {sorted(NATIVE_SUFFIXES)} file in {NATIVE_DIR}/")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(scrub_rss(text), encoding="utf-8", newline="\n")
    meta = {"recorded_at": recorded_at, "server_version": server_version, "provenance": provenance}
    sidecar = path.with_name(path.name + META_SUFFIX)
    sidecar.write_text(json.dumps({"meta": meta}, indent=2) + "\n", encoding="utf-8", newline="\n")


def _check_native(path: Path) -> list[str]:
    name = path.name
    sidecar = path.with_name(name + META_SUFFIX)
    problems: list[str] = []
    try:
        doc = json.loads(sidecar.read_text(encoding="utf-8"))
    except FileNotFoundError:
        problems.append(f"{name}: its meta file {sidecar.name} is missing")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        problems.append(f"{sidecar.name}: not valid JSON ({exc})")
    else:
        if not isinstance(doc, dict) or set(doc) != {"meta"}:
            problems.append(f"{sidecar.name}: must be an object with exactly 'meta'")
        else:
            problems.extend(_check_meta(name, doc["meta"]))
    try:
        text = path.read_text(encoding="utf-8")
        scrubbed = scrub_rss(text)
    except (UnicodeDecodeError, ET.ParseError) as exc:
        return [*problems, f"{name}: not valid XML ({exc})"]
    if scrubbed != text:
        problems.append(f"{name}: the feed is not scrubbed")
    problems.extend(_personal_values(name, [text]))
    return problems


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
