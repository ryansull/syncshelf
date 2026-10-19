"""The Goodreads RSS fixture scrubber and checker (SPEC §2.9, #44): native XML, per-source keep
rules, and placeholders that still parse as the feed."""

import json
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path

import pytest

from scrub import (
    PLACEHOLDER_SHELF,
    PLACEHOLDER_TEXT,
    PLACEHOLDER_URL,
    check_fixture,
    check_tree,
    scrub_rss,
    write_native_fixture,
)

# Shaped like a real #ALL# page; every personal value is made up but realistic.
RECORDED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" >
  <channel>
    <xhtml:meta xmlns:xhtml="http://www.w3.org/1999/xhtml" name="robots" content="noindex" />
    <title>Realperson's bookshelf: all</title>
    <copyright><![CDATA[Copyright (C) 2026 Goodreads Inc. All rights reserved.]]></copyright>
    <link><![CDATA[https://www.goodreads.com/review/list_rss/31415926?key=SEKRITKEY&shelf=%23ALL%23]]></link>
    <atom:link href="https://www.goodreads.com/review/list_rss/31415926?key=SEKRITKEY"
               rel="self" type="application/rss+xml"/>
    <description><![CDATA[Realperson's bookshelf: all]]></description>
    <language>en-US</language>
    <lastBuildDate>Sat, 17 Oct 2026 10:11:12 -0700</lastBuildDate>
    <ttl>60</ttl>
    <image>
      <title>Realperson's bookshelf: all</title>
      <link><![CDATA[https://www.goodreads.com/review/list_rss/31415926]]></link>
      <width>144</width>
      <height>41</height>
      <url>https://www.goodreads.com/images/layout/goodreads_logo_144.jpg</url>
    </image>
    <item>
      <guid><![CDATA[https://www.goodreads.com/review/show/7777777777?utm_medium=api]]></guid>
      <pubDate><![CDATA[Thu, 15 Oct 2026 08:09:10 -0700]]></pubDate>
      <title>Example Book (Example Series, #2)</title>
      <link><![CDATA[https://www.goodreads.com/review/show/7777777777?utm_medium=api]]></link>
      <book_id>12345678</book_id>
      <book_image_url><![CDATA[https://i.gr-assets.com/images/S/x.jpg]]></book_image_url>
      <book_description><![CDATA[<b>Blurb</b> about the book.]]></book_description>
      <book id="12345678">
        <num_pages>412</num_pages>
      </book>
      <author_name>Ann Author</author_name>
      <isbn>080441234X</isbn>
      <user_name>Realperson</user_name>
      <user_rating>4</user_rating>
      <user_read_at><![CDATA[Fri, 2 Oct 2026 00:00:00 +0000]]></user_read_at>
      <user_date_added><![CDATA[Thu, 15 Oct 2026 08:09:10 -0700]]></user_date_added>
      <user_date_created><![CDATA[Mon, 21 Sep 2026 19:20:21 -0700]]></user_date_created>
      <user_shelves>favourites, currently-reading</user_shelves>
      <user_review><![CDATA[Loved it.<br/>Would reread.]]></user_review>
      <average_rating>4.21</average_rating>
      <book_published>2019</book_published>
      <description><![CDATA[author: Ann Author<br/>name: Realperson<br/>rating: 4]]></description>
    </item>
    <item>
      <guid><![CDATA[https://www.goodreads.com/review/show/7777777778?utm_medium=api]]></guid>
      <pubDate><![CDATA[Wed, 14 Oct 2026 08:09:10 -0700]]></pubDate>
      <title>Second Book</title>
      <book_id>87654321</book_id>
      <book id="87654321">
        <num_pages></num_pages>
      </book>
      <author_name>Bo Writer</author_name>
      <isbn></isbn>
      <user_name>Realperson</user_name>
      <user_rating>0</user_rating>
      <user_read_at></user_read_at>
      <user_date_added><![CDATA[Wed, 14 Oct 2026 08:09:10 -0700]]></user_date_added>
      <user_date_created><![CDATA[Wed, 14 Oct 2026 08:09:10 -0700]]></user_date_created>
      <user_shelves></user_shelves>
      <user_review></user_review>
      <average_rating>3.90</average_rating>
      <book_published></book_published>
      <description></description>
    </item>
  </channel>
</rss>
"""

PERSONAL = ("Realperson", "31415926", "SEKRITKEY", "7777777777", "Loved it", "favourites")


def items(text: str) -> list[ET.Element]:
    channel = ET.fromstring(text).find("channel")
    assert channel is not None
    return channel.findall("item")


class TestRss:
    def test_no_personal_value_survives(self) -> None:
        scrubbed = scrub_rss(RECORDED)
        for value in PERSONAL:
            assert value not in scrubbed, value

    def test_catalog_values_are_kept(self) -> None:
        first = items(scrub_rss(RECORDED))[0]
        assert first.findtext("title") == "Example Book (Example Series, #2)"
        assert first.findtext("book_id") == "12345678"
        assert first.findtext("author_name") == "Ann Author"
        assert first.findtext("isbn") == "080441234X"
        assert first.findtext("book/num_pages") == "412"
        assert first.findtext("book_published") == "2019"

    def test_the_channel_still_names_the_all_shelf(self) -> None:
        channel = ET.fromstring(scrub_rss(RECORDED)).find("channel")
        assert channel is not None
        assert channel.findtext("title") == f"{PLACEHOLDER_TEXT}'s bookshelf: all"

    def test_dates_stay_rfc_822_with_their_offsets(self) -> None:
        first, second = items(scrub_rss(RECORDED))
        read_at = parsedate_to_datetime(first.findtext("user_read_at") or "")
        added = parsedate_to_datetime(first.findtext("user_date_added") or "")
        assert read_at.utcoffset() is not None and read_at.utcoffset().total_seconds() == 0  # type: ignore[union-attr]
        assert added.utcoffset() is not None and added.utcoffset().total_seconds() == -7 * 3600  # type: ignore[union-attr]
        assert read_at.year == 2000
        # An empty date stays empty: "not read" is a shape the parser must see.
        assert second.findtext("user_read_at") == ""

    def test_ratings_shelves_and_reviews_get_placeholders(self) -> None:
        first, second = items(scrub_rss(RECORDED))
        assert first.findtext("user_rating") == "0"
        assert first.findtext("user_shelves") == PLACEHOLDER_SHELF
        assert first.findtext("user_review") == ""
        assert first.findtext("user_name") == PLACEHOLDER_TEXT
        assert second.findtext("user_shelves") == ""

    def test_urls_are_replaced(self) -> None:
        scrubbed = scrub_rss(RECORDED)
        first = items(scrubbed)[0]
        assert first.findtext("guid") == PLACEHOLDER_URL
        assert first.findtext("book_image_url") == PLACEHOLDER_URL
        assert 'href="https://example.com/"' in scrubbed

    def test_scrubbing_is_a_fixed_point(self) -> None:
        once = scrub_rss(RECORDED)
        assert scrub_rss(once) == once


class TestNativeFixtures:
    def write(self, root: Path, text: str = RECORDED) -> Path:
        path = root / "goodreads" / "rss_page.xml"
        write_native_fixture(path, text, recorded_at="2026-10-18", server_version="www")
        return path

    def test_a_written_fixture_passes_the_checker(self, tmp_path: Path) -> None:
        path = self.write(tmp_path)
        assert check_fixture(path) == []
        assert check_tree(tmp_path) == {}
        meta = json.loads(path.with_name("rss_page.xml.meta.json").read_text(encoding="utf-8"))
        assert meta == {
            "meta": {"recorded_at": "2026-10-18", "server_version": "www", "provenance": "live"}
        }

    def test_an_unscrubbed_feed_is_refused(self, tmp_path: Path) -> None:
        path = self.write(tmp_path)
        path.write_text(RECORDED, encoding="utf-8")
        assert any("not scrubbed" in p for p in check_fixture(path))

    def test_a_feed_without_its_metadata_is_refused(self, tmp_path: Path) -> None:
        path = self.write(tmp_path)
        path.with_name("rss_page.xml.meta.json").unlink()
        assert any("meta" in p for p in check_fixture(path))

    def test_xml_outside_the_goodreads_directory_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "hardcover" / "x.xml"
        path.parent.mkdir()
        path.write_text(scrub_rss(RECORDED), encoding="utf-8")
        assert check_fixture(path) == ["x.xml: only .json fixtures are supported here"]

    @pytest.mark.parametrize("bad", ["not xml", "<rss><channel>"])
    def test_a_fixture_that_does_not_parse_is_refused(self, tmp_path: Path, bad: str) -> None:
        path = self.write(tmp_path)
        path.write_text(bad, encoding="utf-8")
        assert any("not valid XML" in p for p in check_fixture(path))
