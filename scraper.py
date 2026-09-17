"""
books.toscrape.com catalog scraper — portfolio piece.

Design goal: never hide a failure. Every URL that could not be fetched or
parsed ends up in failures.csv and report.md with the reason and the number
of attempts, instead of being silently dropped.

books.toscrape.com is a sandbox site built specifically for scraping practice,
so it is safe to run this against it.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import random
import re
import sys
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Tuple
from urllib import robotparser
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from store import Diff, Store, diff_markdown

BASE_URL = "https://books.toscrape.com/"
USER_AGENT = "EtherPortfolioScraper/1.0 (+https://github.com/chapse57)"
RETRY_STATUS = {429, 500, 502, 503, 504}
RATING_WORDS = {"One": 1, "Two": 2, "Three": 3, "Four": 4, "Five": 5}

log = logging.getLogger("scraper")


# --------------------------------------------------------------------------- #
# Data models
# --------------------------------------------------------------------------- #
@dataclass
class Book:
    upc: str
    title: str
    category: str
    price_incl_tax: float
    price_excl_tax: float
    tax: float
    stock_count: int
    rating: int
    num_reviews: int
    url: str


@dataclass
class Failure:
    url: str
    stage: str      # "fetch" | "parse" | "robots"
    reason: str
    attempts: int


@dataclass
class RunResult:
    books: List[Book] = field(default_factory=list)
    failures: List[Failure] = field(default_factory=list)
    listing_pages: int = 0
    duplicates_skipped: int = 0
    crawl_complete: bool = True   # False if a listing page failed ("gone" detection is unsafe)


class FetchError(Exception):
    def __init__(self, reason: str, attempts: int, stage: str = "fetch"):
        super().__init__(reason)
        self.reason = reason
        self.attempts = attempts
        self.stage = stage


class ParseError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Politeness: rate limit + robots.txt
# --------------------------------------------------------------------------- #
class RateLimiter:
    """Guarantees at least `min_interval` seconds between requests."""

    def __init__(self, min_interval: float,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._last: Optional[float] = None

    def wait(self) -> None:
        now = self._clock()
        if self._last is not None:
            remaining = self.min_interval - (now - self._last)
            if remaining > 0:
                self._sleep(remaining)
                now = self._clock()
        self._last = now


def load_robots(session: requests.Session, base_url: str,
                timeout: float = 15) -> Tuple[robotparser.RobotFileParser, str]:
    """
    Returns (parser, status_note).
    - 200      -> obey the file
    - 4xx      -> no rules published, everything allowed (standard behaviour)
    - 5xx/down -> treat as "disallow all" (conservative choice)
    """
    robots_url = urljoin(base_url, "/robots.txt")
    rp = robotparser.RobotFileParser()
    try:
        resp = session.get(robots_url, timeout=timeout)
    except requests.RequestException as exc:
        rp.parse(["User-agent: *", "Disallow: /"])
        return rp, f"robots.txt unreachable ({type(exc).__name__}) -> disallow all"

    if resp.status_code == 200:
        rp.parse(resp.content.decode("utf-8", "replace").splitlines())
        return rp, "robots.txt found and obeyed"
    if 400 <= resp.status_code < 500:
        rp.parse([])
        return rp, f"robots.txt returned {resp.status_code} -> no rules, all allowed"
    rp.parse(["User-agent: *", "Disallow: /"])
    return rp, f"robots.txt returned {resp.status_code} -> disallow all"


# --------------------------------------------------------------------------- #
# Fetching with retries
# --------------------------------------------------------------------------- #
class Fetcher:
    def __init__(self, session, limiter: RateLimiter, robots,
                 user_agent: str = USER_AGENT, max_retries: int = 3,
                 backoff_base: float = 1.0, timeout: float = 15,
                 jitter: bool = True,
                 sleep: Callable[[float], None] = time.sleep):
        self.session = session
        self.limiter = limiter
        self.robots = robots
        self.user_agent = user_agent
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.timeout = timeout
        self.jitter = jitter
        self._sleep = sleep
        self.requests_made = 0
        self.retries = 0

    def _backoff(self, attempt: int, retry_after: Optional[str]) -> float:
        if retry_after and retry_after.strip().isdigit():
            return float(retry_after.strip())
        delay = self.backoff_base * (2 ** (attempt - 1))
        if self.jitter:
            delay += random.uniform(0, self.backoff_base * 0.25)
        return delay

    def get(self, url: str) -> bytes:
        if not self.robots.can_fetch(self.user_agent, url):
            raise FetchError("blocked by robots.txt", attempts=0, stage="robots")

        last_reason = "unknown"
        total_attempts = self.max_retries + 1
        for attempt in range(1, total_attempts + 1):
            self.limiter.wait()
            self.requests_made += 1
            retry_after = None
            try:
                resp = self.session.get(url, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_reason = f"{type(exc).__name__}"
            else:
                if resp.status_code == 200:
                    return resp.content
                last_reason = f"HTTP {resp.status_code}"
                if resp.status_code not in RETRY_STATUS:
                    # 404, 403 ... retrying will not help
                    raise FetchError(last_reason, attempts=attempt)
                retry_after = resp.headers.get("Retry-After")

            if attempt < total_attempts:
                delay = self._backoff(attempt, retry_after)
                self.retries += 1
                log.warning("retry %d/%d in %.1fs (%s) %s",
                            attempt, self.max_retries, delay, last_reason, url)
                self._sleep(delay)

        raise FetchError(f"gave up: {last_reason}", attempts=total_attempts)


# --------------------------------------------------------------------------- #
# Parsing (pure functions — unit tested against saved HTML)
# --------------------------------------------------------------------------- #
def _soup(html: bytes) -> BeautifulSoup:
    # If the server omits a charset, requests may guess ISO-8859-1 and
    # turn "£" into "Â£". Decode the raw bytes as UTF-8 ourselves.
    text = html.decode("utf-8", "replace") if isinstance(html, bytes) else html
    return BeautifulSoup(text, "html.parser")


def _money(text: str) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)", text)
    if not m:
        raise ParseError(f"not a price: {text!r}")
    return float(m.group(1))


def parse_listing(html: bytes, page_url: str) -> Tuple[List[str], Optional[str]]:
    soup = _soup(html)
    links = [urljoin(page_url, a["href"])
             for a in soup.select("article.product_pod h3 a[href]")]
    nxt = soup.select_one("li.next a[href]")
    next_url = urljoin(page_url, nxt["href"]) if nxt else None
    return links, next_url


def parse_detail(html: bytes, url: str) -> Book:
    soup = _soup(html)
    title_el = soup.select_one("div.product_main h1")
    if not title_el:
        raise ParseError("title not found")

    table = {}
    for row in soup.select("table.table-striped tr"):
        th, td = row.find("th"), row.find("td")
        if th and td:
            table[th.get_text(strip=True)] = td.get_text(strip=True)

    required = ["UPC", "Price (excl. tax)", "Price (incl. tax)", "Tax",
                "Availability", "Number of reviews"]
    missing = [k for k in required if k not in table]
    if missing:
        raise ParseError(f"missing fields: {', '.join(missing)}")

    crumbs = soup.select("ul.breadcrumb li a")
    category = crumbs[2].get_text(strip=True) if len(crumbs) >= 3 else ""

    rating_el = soup.select_one("div.product_main p.star-rating")
    rating = 0
    if rating_el:
        for cls in rating_el.get("class", []):
            rating = RATING_WORDS.get(cls, rating)

    stock = re.search(r"(\d+)\s+available", table["Availability"])

    return Book(
        upc=table["UPC"],
        title=title_el.get_text(strip=True),
        category=category,
        price_incl_tax=_money(table["Price (incl. tax)"]),
        price_excl_tax=_money(table["Price (excl. tax)"]),
        tax=_money(table["Tax"]),
        stock_count=int(stock.group(1)) if stock else 0,
        rating=rating,
        num_reviews=int(table["Number of reviews"]),
        url=url,
    )


# --------------------------------------------------------------------------- #
# Crawl
# --------------------------------------------------------------------------- #
def scrape(fetcher, start_url: str, max_pages: int = 3,
           max_books: Optional[int] = None,
           extra_detail_urls: Iterable[str] = ()) -> RunResult:
    result = RunResult()
    detail_urls: List[str] = []
    seen_urls = set()

    page_url: Optional[str] = start_url
    while page_url and result.listing_pages < max_pages:
        try:
            html = fetcher.get(page_url)
            links, page_url_next = parse_listing(html, page_url)
        except FetchError as exc:
            result.failures.append(Failure(page_url, exc.stage, exc.reason, exc.attempts))
            result.crawl_complete = False
            break  # cannot discover the next page without this one
        result.listing_pages += 1
        for link in links:
            if link not in seen_urls:
                seen_urls.add(link)
                detail_urls.append(link)
        log.info("listing page %d: %d links", result.listing_pages, len(links))
        page_url = page_url_next

    if max_books is not None:
        detail_urls = detail_urls[:max_books]

    for extra in extra_detail_urls:  # added after the cap so they always run
        if extra not in seen_urls:
            seen_urls.add(extra)
            detail_urls.append(extra)

    seen_upc = set()
    for i, url in enumerate(detail_urls, 1):
        try:
            book = parse_detail(fetcher.get(url), url)
        except FetchError as exc:
            result.failures.append(Failure(url, exc.stage, exc.reason, exc.attempts))
            log.error("[%d/%d] FAIL %s (%s)", i, len(detail_urls), url, exc.reason)
            continue
        except ParseError as exc:
            result.failures.append(Failure(url, "parse", str(exc), 1))
            log.error("[%d/%d] PARSE FAIL %s (%s)", i, len(detail_urls), url, exc)
            continue

        if book.upc in seen_upc:
            result.duplicates_skipped += 1
            log.info("[%d/%d] duplicate UPC %s skipped", i, len(detail_urls), book.upc)
            continue
        seen_upc.add(book.upc)
        result.books.append(book)
        log.info("[%d/%d] ok %s", i, len(detail_urls), book.title[:50])

    return result


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def write_outputs(result: RunResult, out_dir: Path, *, started: datetime,
                  finished: datetime, fetcher: Fetcher, robots_note: str,
                  injected: List[str], diff: Optional[Diff] = None) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    with open(out_dir / "books.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=[fl.name for fl in fields(Book)])
        w.writeheader()
        for b in result.books:
            w.writerow(asdict(b))

    with open(out_dir / "books.json", "w", encoding="utf-8") as f:
        json.dump([asdict(b) for b in result.books], f, ensure_ascii=False, indent=2)

    with open(out_dir / "failures.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=[fl.name for fl in fields(Failure)])
        w.writeheader()
        for fl in result.failures:
            w.writerow(asdict(fl))

    attempted = len(result.books) + len(result.failures) + result.duplicates_skipped
    rate = (len(result.books) + result.duplicates_skipped) / attempted * 100 if attempted else 0
    lines = [
        "# Scrape run report",
        "",
        f"- Started: {started:%Y-%m-%d %H:%M:%S}",
        f"- Duration: {(finished - started).total_seconds():.1f}s",
        f"- robots.txt: {robots_note}",
        f"- Listing pages crawled: {result.listing_pages}",
        f"- HTTP requests: {fetcher.requests_made} (retries: {fetcher.retries})",
        f"- Books saved: **{len(result.books)}**",
        f"- Duplicates skipped (same UPC): {result.duplicates_skipped}",
        f"- Failures: **{len(result.failures)}**",
        f"- Success rate (fetched & parsed): {rate:.1f}%",
        "",
    ]
    if injected:
        lines += ["> Note: the following URLs were injected on purpose (--inject-failures)",
                  "> to demonstrate failure reporting. They are not site errors.", ""]
        lines += [f"> - {u}" for u in injected] + [""]
    lines += ["## Failures", ""]
    if result.failures:
        lines += ["| Stage | Reason | Attempts | URL |", "|---|---|---|---|"]
        lines += [f"| {fl.stage} | {fl.reason} | {fl.attempts} | {fl.url} |"
                  for fl in result.failures]
    else:
        lines.append("None.")
    if diff is not None:
        lines += [""] + diff_markdown(diff, "books")
    (out_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
INJECTED_URLS = [
    urljoin(BASE_URL, "catalogue/this-book-does-not-exist_99999/index.html"),  # -> 404
    urljoin(BASE_URL, "index.html"),  # a listing page, not a book -> parse failure
]


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="books.toscrape.com scraper with honest failure reporting")
    ap.add_argument("--pages", type=int, default=3, help="listing pages to crawl (20 books/page, max 50)")
    ap.add_argument("--max-books", type=int, default=None, help="cap detail pages fetched")
    ap.add_argument("--delay", type=float, default=1.0, help="min seconds between requests")
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--out", default="output")
    ap.add_argument("--db", default="data/scrape.db", help="SQLite store for change detection ('' to disable)")
    ap.add_argument("--inject-failures", action="store_true",
                    help="add known-bad URLs to demonstrate failure handling")
    args = ap.parse_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(out_dir / "run.log", encoding="utf-8")],
    )

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    robots, robots_note = load_robots(session, BASE_URL)
    log.info(robots_note)

    fetcher = Fetcher(session, RateLimiter(args.delay), robots, max_retries=args.retries)
    injected = INJECTED_URLS if args.inject_failures else []

    started = datetime.now()
    result = scrape(fetcher, BASE_URL, max_pages=args.pages,
                    max_books=args.max_books, extra_detail_urls=injected)
    finished = datetime.now()

    diff = None
    if args.db:
        store = Store(args.db)
        diff = store.record_run("books", ((b.upc, asdict(b)) for b in result.books),
                                n_failures=len(result.failures), started=started,
                                crawl_complete=result.crawl_complete)
        store.close()
        log.info("change detection: %s", diff.summary)

    write_outputs(result, out_dir, started=started, finished=finished,
                  fetcher=fetcher, robots_note=robots_note, injected=injected, diff=diff)
    log.info("done: %d books, %d failures, %d duplicates -> %s",
             len(result.books), len(result.failures), result.duplicates_skipped, out_dir.resolve())
    return 0 if result.books else 1


if __name__ == "__main__":
    sys.exit(main())
