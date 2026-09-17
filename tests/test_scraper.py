"""Offline tests — no network. HTML fixtures mirror books.toscrape.com markup."""
from pathlib import Path

import pytest
import requests

import scraper
from scraper import (Fetcher, FetchError, ParseError, RateLimiter, parse_detail,
                     parse_listing, scrape)

FIX = Path(__file__).parent / "fixtures"
BASE = "https://books.toscrape.com/"


def fixture(name: str) -> bytes:
    return (FIX / name).read_bytes()


# ---------------------------------------------------------------- fakes --- #
class FakeResp:
    def __init__(self, status=200, content=b"", headers=None):
        self.status_code = status
        self.content = content
        self.headers = headers or {}


class FakeSession:
    """Returns queued responses per URL; an Exception instance is raised."""

    def __init__(self, script):
        self.script = {u: list(r) for u, r in script.items()}
        self.calls = []

    def get(self, url, timeout=None):
        self.calls.append(url)
        item = self.script[url].pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class AllowAll:
    def can_fetch(self, ua, url):
        return True


class DenyAll:
    def can_fetch(self, ua, url):
        return False


def make_fetcher(session, robots=None, retries=3):
    sleeps = []
    f = Fetcher(session, RateLimiter(0, sleep=lambda s: None), robots or AllowAll(),
                max_retries=retries, backoff_base=1.0, jitter=False,
                sleep=sleeps.append)
    return f, sleeps


# -------------------------------------------------------------- parsing --- #
def test_parse_listing_resolves_relative_links_and_next():
    url = BASE + "catalogue/page-1.html"
    links, nxt = parse_listing(fixture("listing.html"), url)
    assert links == [
        BASE + "catalogue/a-light-in-the-attic_1000/index.html",
        BASE + "catalogue/tipping-the-velvet_999/index.html",
    ]
    assert nxt == BASE + "catalogue/page-2.html"


def test_parse_listing_last_page_has_no_next():
    _, nxt = parse_listing(fixture("listing_last.html"), BASE + "catalogue/page-50.html")
    assert nxt is None


def test_parse_detail_extracts_all_fields_and_decodes_pound_sign():
    url = BASE + "catalogue/a-light-in-the-attic_1000/index.html"
    b = parse_detail(fixture("detail.html"), url)
    assert b.upc == "a897fe39b1053632"
    assert b.title == "A Light in the Attic"
    assert b.category == "Poetry"
    assert b.price_incl_tax == 51.77
    assert b.price_excl_tax == 51.77
    assert b.tax == 0.0
    assert b.stock_count == 22
    assert b.rating == 3
    assert b.num_reviews == 0


def test_parse_detail_missing_upc_raises():
    html = fixture("detail.html").replace(b"<th>UPC</th>", b"<th>XXX</th>")
    with pytest.raises(ParseError, match="UPC"):
        parse_detail(html, "u")


def test_parse_detail_on_non_book_page_raises():
    with pytest.raises(ParseError):
        parse_detail(fixture("listing.html"), "u")


# ------------------------------------------------------------- fetching --- #
def test_retries_on_503_then_succeeds_with_exponential_backoff():
    s = FakeSession({"u": [FakeResp(503), FakeResp(503), FakeResp(200, b"ok")]})
    f, sleeps = make_fetcher(s)
    assert f.get("u") == b"ok"
    assert sleeps == [1.0, 2.0]
    assert f.retries == 2 and f.requests_made == 3


def test_retries_on_connection_error():
    s = FakeSession({"u": [requests.ConnectionError(), FakeResp(200, b"ok")]})
    f, _ = make_fetcher(s)
    assert f.get("u") == b"ok"


def test_no_retry_on_404():
    s = FakeSession({"u": [FakeResp(404)]})
    f, sleeps = make_fetcher(s)
    with pytest.raises(FetchError) as e:
        f.get("u")
    assert e.value.reason == "HTTP 404" and e.value.attempts == 1
    assert sleeps == []


def test_gives_up_after_max_retries():
    s = FakeSession({"u": [FakeResp(500)] * 3})
    f, sleeps = make_fetcher(s, retries=2)
    with pytest.raises(FetchError) as e:
        f.get("u")
    assert e.value.attempts == 3
    assert "gave up" in e.value.reason
    assert len(sleeps) == 2


def test_honors_retry_after_header_on_429():
    s = FakeSession({"u": [FakeResp(429, headers={"Retry-After": "7"}), FakeResp(200, b"ok")]})
    f, sleeps = make_fetcher(s)
    f.get("u")
    assert sleeps == [7.0]


def test_robots_disallow_blocks_without_sending_request():
    s = FakeSession({})
    f, _ = make_fetcher(s, robots=DenyAll())
    with pytest.raises(FetchError) as e:
        f.get("u")
    assert e.value.stage == "robots"
    assert s.calls == []


def test_load_robots_404_allows_all_and_5xx_disallows_all():
    rp, note = scraper.load_robots(FakeSession({BASE + "robots.txt": [FakeResp(404)]}), BASE)
    assert rp.can_fetch("x", BASE + "anything") and "all allowed" in note
    rp, note = scraper.load_robots(FakeSession({BASE + "robots.txt": [FakeResp(503)]}), BASE)
    assert not rp.can_fetch("x", BASE + "anything") and "disallow all" in note


def test_rate_limiter_sleeps_remaining_interval():
    t = [100.0]
    slept = []
    rl = RateLimiter(1.0, clock=lambda: t[0], sleep=slept.append)
    rl.wait()
    t[0] = 100.3
    rl.wait()
    assert slept == [pytest.approx(0.7)]


# ---------------------------------------------------------------- crawl --- #
def test_scrape_dedupes_by_upc_and_records_failures():
    p1 = BASE + "catalogue/page-1.html"
    book1 = BASE + "catalogue/a-light-in-the-attic_1000/index.html"
    book2 = BASE + "catalogue/tipping-the-velvet_999/index.html"
    bad = BASE + "catalogue/missing/index.html"
    detail = fixture("detail.html")
    s = FakeSession({
        p1: [FakeResp(200, fixture("listing_last.html").replace(
            b"</ol>", b'<li><article class="product_pod"><h3><a href="tipping-the-velvet_999/index.html">x</a></h3></article></li></ol>'))],
        book1: [FakeResp(200, detail)],
        book2: [FakeResp(200, detail)],   # same UPC on a different URL -> duplicate
        bad: [FakeResp(404)],
    })
    f, _ = make_fetcher(s)
    r = scrape(f, p1, max_pages=5, extra_detail_urls=[bad])
    assert r.listing_pages == 1
    assert len(r.books) == 1
    assert r.duplicates_skipped == 1
    assert [(x.url, x.reason) for x in r.failures] == [(bad, "HTTP 404")]


def test_scrape_listing_failure_is_reported_not_swallowed():
    p1 = BASE + "catalogue/page-1.html"
    f, _ = make_fetcher(FakeSession({p1: [FakeResp(500)] * 2}), retries=1)
    r = scrape(f, p1)
    assert r.books == [] and r.failures[0].url == p1


# ------------------------------------------------------------------ cli --- #
def test_cli_twice_reports_changes_between_runs(tmp_path, monkeypatch):
    """Full CLI path: scrape -> store -> report, run twice, price changes in between."""
    listing = fixture("listing_last.html")
    detail = fixture("detail.html")
    state = {"detail": detail}

    class Sess:
        headers = {}
        def get(self, url, timeout=None):
            if url.endswith("robots.txt"):
                return FakeResp(404)
            if url == BASE:
                return FakeResp(200, listing)
            return FakeResp(200, state["detail"])

    monkeypatch.setattr(scraper.requests, "Session", Sess)
    monkeypatch.setattr(scraper.time, "sleep", lambda s: None)
    out, db = tmp_path / "out", tmp_path / "d.db"

    assert scraper.main(["--pages", "1", "--out", str(out), "--db", str(db), "--delay", "0"]) == 0
    r1 = (out / "report.md").read_text(encoding="utf-8")
    assert "first run: 1 items stored" in r1

    state["detail"] = detail.replace(b"<td>\xc2\xa351.77</td>", b"<td>\xc2\xa349.00</td>")
    assert scraper.main(["--pages", "1", "--out", str(out), "--db", str(db), "--delay", "0"]) == 0
    r2 = (out / "report.md").read_text(encoding="utf-8")
    assert "+0 new, ~1 changed, -0 gone, 0 unchanged" in r2
    assert "| a897fe39b1053632 | price_excl_tax | 51.77 | 49.0 |" in r2
    assert "| a897fe39b1053632 | price_incl_tax | 51.77 | 49.0 |" in r2
