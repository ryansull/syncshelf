"""The fixture scrubber and checker (SPEC §2.9)."""

import json
from pathlib import Path

import pytest

from scrub import (
    PLACEHOLDER_DATE,
    PLACEHOLDER_DATETIME,
    PLACEHOLDER_TEXT,
    PLACEHOLDER_URL,
    check_fixture,
    check_tree,
    scrub,
    write_fixture,
)

# Shaped like a Hardcover user_book with its book, edition and contributors.
RECORDED = {
    "data": {
        "me": [{"id": 4242, "username": "realperson"}],
        "user_books": [
            {
                "id": 987654,
                "status_id": 3,
                "rating": 4.5,
                "review": "Loved it",
                "private_notes": "",
                "date_added": "2025-03-04",
                "updated_at": "2025-03-04T18:22:01.123+00:00",
                "has_review": True,
                "first_read_date": None,
                "book": {
                    "id": 312460,
                    "title": "Dune",
                    "subtitle": None,
                    "slug": "dune",
                    "users_count": 51000,
                    "image": {"url": "https://assets.hardcover.app/x/y.jpg"},
                    "contributions": [{"author": {"id": 204214, "name": "Frank Herbert"}}],
                    "book_series": [{"series": {"name": "Dune"}, "position": 1}],
                },
                "edition": {
                    "id": 30395880,
                    "isbn_10": "0441013597",
                    "isbn_13": "9780441013593",
                    "asin": "B00B7NPRY8",
                },
            }
        ],
    }
}


@pytest.fixture
def scrubbed() -> dict:
    return scrub(RECORDED)


def user_book(doc: dict) -> dict:
    return doc["data"]["user_books"][0]


class TestKeepsPublicCatalogFields:
    def test_titles(self, scrubbed: dict) -> None:
        assert user_book(scrubbed)["book"]["title"] == "Dune"

    def test_authors(self, scrubbed: dict) -> None:
        author = user_book(scrubbed)["book"]["contributions"][0]["author"]
        assert author == {"id": 204214, "name": "Frank Herbert"}

    def test_series(self, scrubbed: dict) -> None:
        assert user_book(scrubbed)["book"]["book_series"][0]["series"]["name"] == "Dune"

    def test_isbn_and_asin(self, scrubbed: dict) -> None:
        edition = user_book(scrubbed)["edition"]
        assert edition["isbn_10"] == "0441013597"
        assert edition["isbn_13"] == "9780441013593"
        assert edition["asin"] == "B00B7NPRY8"

    def test_hardcover_book_and_edition_ids(self, scrubbed: dict) -> None:
        assert user_book(scrubbed)["book"]["id"] == 312460
        assert user_book(scrubbed)["edition"]["id"] == 30395880


class TestReplacesEverythingPersonal:
    def test_user_ids_and_names(self, scrubbed: dict) -> None:
        assert scrubbed["data"]["me"] == [{"id": 0, "username": PLACEHOLDER_TEXT}]
        assert user_book(scrubbed)["id"] == 0

    def test_ratings_and_statuses(self, scrubbed: dict) -> None:
        assert user_book(scrubbed)["rating"] == 0.0
        assert user_book(scrubbed)["status_id"] == 0
        assert user_book(scrubbed)["has_review"] is False

    def test_every_date(self, scrubbed: dict) -> None:
        assert user_book(scrubbed)["date_added"] == PLACEHOLDER_DATE
        assert user_book(scrubbed)["updated_at"] == PLACEHOLDER_DATETIME

    def test_reviews_and_notes(self, scrubbed: dict) -> None:
        assert user_book(scrubbed)["review"] == PLACEHOLDER_TEXT

    def test_urls_and_hosts(self, scrubbed: dict) -> None:
        assert user_book(scrubbed)["book"]["image"]["url"] == PLACEHOLDER_URL

    def test_non_catalog_fields_by_default(self, scrubbed: dict) -> None:
        # An allowlist: a field nobody classified is treated as personal.
        assert user_book(scrubbed)["book"]["slug"] == PLACEHOLDER_TEXT
        assert user_book(scrubbed)["book"]["users_count"] == 0


class TestPreservesShape:
    """Contract tests compare keys, types and nullability, so scrubbing must keep all three."""

    def test_nulls_and_empty_strings_survive(self, scrubbed: dict) -> None:
        assert user_book(scrubbed)["first_read_date"] is None
        assert user_book(scrubbed)["book"]["subtitle"] is None
        assert user_book(scrubbed)["private_notes"] == ""

    def test_keys_and_leaf_types_match(self, scrubbed: dict) -> None:
        def shape(value: object) -> object:
            if isinstance(value, dict):
                return {k: shape(v) for k, v in value.items()}
            if isinstance(value, list):
                return [shape(v) for v in value]
            return type(value)

        assert shape(scrubbed) == shape(RECORDED)

    def test_is_idempotent(self, scrubbed: dict) -> None:
        assert scrub(scrubbed) == scrubbed

    def test_does_not_mutate_its_input(self) -> None:
        before = json.dumps(RECORDED, sort_keys=True)
        scrub(RECORDED)
        assert json.dumps(RECORDED, sort_keys=True) == before


class TestChecker:
    def fixture(self, tmp_path: Path, body: object, **meta: object) -> Path:
        path = tmp_path / "hardcover" / "user_books.json"
        write_fixture(path, body, server_version="2026.10", recorded_at="2026-10-17", **meta)
        return path

    def test_write_fixture_scrubs_and_passes(self, tmp_path: Path) -> None:
        path = self.fixture(tmp_path, RECORDED)
        assert check_fixture(path) == []
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert stored["meta"] == {
            "recorded_at": "2026-10-17",
            "server_version": "2026.10",
            "provenance": "live",
        }
        assert stored["body"] == scrub(RECORDED)

    def test_source_only_provenance_is_recorded(self, tmp_path: Path) -> None:
        path = self.fixture(tmp_path, {}, provenance="source-only")
        assert check_fixture(path) == []
        assert json.loads(path.read_text(encoding="utf-8"))["meta"]["provenance"] == "source-only"

    def test_rejects_an_unscrubbed_body_naming_the_paths(self, tmp_path: Path) -> None:
        path = tmp_path / "raw.json"
        meta = {"recorded_at": "2026-10-17", "server_version": "1", "provenance": "live"}
        path.write_text(json.dumps({"meta": meta, "body": RECORDED}), encoding="utf-8")
        problems = check_fixture(path)
        assert any("$.data.user_books[0].review" in p for p in problems)
        assert not any("title" in p for p in problems)

    @pytest.mark.parametrize("missing", ["recorded_at", "server_version", "provenance"])
    def test_requires_metadata(self, tmp_path: Path, missing: str) -> None:
        meta = {"recorded_at": "2026-10-17", "server_version": "1", "provenance": "live"}
        del meta[missing]
        path = tmp_path / "f.json"
        path.write_text(json.dumps({"meta": meta, "body": {}}), encoding="utf-8")
        assert any(missing in p for p in check_fixture(path))

    def test_rejects_unknown_provenance(self, tmp_path: Path) -> None:
        meta = {"recorded_at": "2026-10-17", "server_version": "1", "provenance": "guess"}
        path = tmp_path / "f.json"
        path.write_text(json.dumps({"meta": meta, "body": {}}), encoding="utf-8")
        assert any("provenance" in p for p in check_fixture(path))

    def test_rejects_real_looking_emails_and_ips_even_in_kept_fields(self, tmp_path: Path) -> None:
        body = {"title": "Mail me at someone@gmail.test or 10.1.2.3"}
        path = self.fixture(tmp_path, body)
        problems = check_fixture(path)
        assert any("email" in p for p in problems)
        assert any("IP" in p for p in problems)

    def test_allows_placeholder_emails_and_ips(self, tmp_path: Path) -> None:
        path = self.fixture(tmp_path, {"title": "alice@example.com at 192.0.2.10"})
        assert check_fixture(path) == []

    def test_rejects_invalid_json(self, tmp_path: Path) -> None:
        path = tmp_path / "f.json"
        path.write_text("{not json", encoding="utf-8")
        assert check_fixture(path) != []

    def test_rejects_non_json_fixture_files(self, tmp_path: Path) -> None:
        # Native XML is accepted only under goodreads/ (#44).
        path = tmp_path / "abs" / "shelf.xml"
        path.parent.mkdir()
        path.write_text("<rss/>", encoding="utf-8")
        assert check_tree(tmp_path) == {
            path: [f"{path.name}: only .json fixtures are supported here"]
        }

    def test_check_tree_reports_only_failures_and_ignores_gitkeep(self, tmp_path: Path) -> None:
        self.fixture(tmp_path, RECORDED)
        (tmp_path / ".gitkeep").touch()
        assert check_tree(tmp_path) == {}
