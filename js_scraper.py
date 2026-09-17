"""
JavaScript-rendered site scraper — quotes.toscrape.com/js (sandbox site).

Two strategies for the same data, run side by side and cross-checked:

  A) "api"      — look for the JSON endpoint the page itself calls
                  (quotes.toscrape.com/api/quotes?page=N) and use it directly.
                  Fast, cheap, and the least load on the server.
  B) "rendered" — drive a real browser with Playwright, wait for the JS to
                  render, read the DOM. Slow, but works when there is no API.

Rule I follow on real jobs: try A first, fall back to B, and never fake B by
"parsing" the raw HTML of a JS page (it contains no data).

Same philosophy as scraper.py: every failure is reported, nothing is dropped.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional, Tuple
from urllib.parse import urljoin

import requests

from scraper import Failure, Fetcher, FetchError, RateLimiter, load_robots
from store import Diff, Store, diff_markdown

BASE_URL = "https://quotes.toscrape.com/"
JS_URL = urljoin(BASE_URL, "js/")
API_URL = urljoin(BASE_URL, "api/quotes")
USER_AGENT = "EtherPortfolioScraper/1.0 (+https://github.com/chapse57)"

log = logging.getLogger("js_scraper")


@dataclass
class Quote:
    text: str
    author: str
    tags: str          # "a|b|c" so it stays one CSV column
    source: str        # "api" | "rendered"
    page: int

    def key(self) -> Tuple[str, str]:
        return (self.text, self.author)


@dataclass
class StrategyResult:
    name: str
    quotes: List[Quote] = field(default_factory=list)
    failures: List[Failure] = field(default_factory=list)
    pages: int = 0
    seconds: float = 0.0
    requests_made: int = 0


# --------------------------------------------------------------------------- #
# Strategy A: JSON API
# --------------------------------------------------------------------------- #
def parse_api_page(raw: bytes, page: int) -> Tuple[List[Quote], bool]:
    data = json.loads(raw.decode("utf-8"))
    quotes = [Quote(text=q["text"], author=q["author"]["name"],
                    tags="|".join(q.get("tags", [])), source="api", page=page)
              for q in data["quotes"]]
    return quotes, bool(data.get("has_next"))


def scrape_api(fetcher: Fetcher, api_url: str, max_pages: int) -> StrategyResult:
    r = StrategyResult("api")
    t0 = time.monotonic()
    page, has_next = 1, True
    while has_next and page <= max_pages:
        url = f"{api_url}?page={page}"
        try:
            quotes, has_next = parse_api_page(fetcher.get(url), page)
        except FetchError as exc:
            r.failures.append(Failure(url, exc.stage, exc.reason, exc.attempts))
            break
        except (ValueError, KeyError) as exc:
            r.failures.append(Failure(url, "parse", f"bad JSON: {exc}", 1))
            break
        r.quotes.extend(quotes)
        r.pages += 1
        log.info("[api] page %d: %d quotes", page, len(quotes))
        page += 1
    r.seconds = time.monotonic() - t0
    r.requests_made = fetcher.requests_made
    return r


# --------------------------------------------------------------------------- #
# Strategy B: headless browser
# --------------------------------------------------------------------------- #
EXTRACT_JS = """
() => Array.from(document.querySelectorAll('div.quote')).map(q => ({
    text:   q.querySelector('span.text')?.textContent ?? '',
    author: q.querySelector('small.author')?.textContent ?? '',
    tags:   Array.from(q.querySelectorAll('a.tag')).map(t => t.textContent),
}))
"""


def scrape_rendered(start_url: str, max_pages: int, *, delay: float = 1.0,
                    timeout_ms: int = 10_000, max_retries: int = 2,
                    headless: bool = True,
                    sleep: Callable[[float], None] = time.sleep) -> StrategyResult:
    from playwright.sync_api import Error as PWError, sync_playwright

    r = StrategyResult("rendered")
    t0 = time.monotonic()
    limiter = RateLimiter(delay, sleep=sleep)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(user_agent=USER_AGENT)
        # Images/fonts are not data. Skipping them is faster and lighter on the server.
        context.route("**/*", lambda route: route.abort()
                      if route.request.resource_type in {"image", "font", "media"}
                      else route.continue_())
        page = context.new_page()

        url: Optional[str] = start_url
        page_no = 1
        while url and page_no <= max_pages:
            ok = False
            reason = ""
            attempts = 0
            for attempts in range(1, max_retries + 2):
                limiter.wait()
                r.requests_made += 1
                try:
                    page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                    # The raw HTML has no quotes; they appear only after the JS runs.
                    page.wait_for_selector("div.quote", timeout=timeout_ms)
                    ok = True
                    break
                except PWError as exc:
                    reason = exc.__class__.__name__ + ": " + str(exc).splitlines()[0][:80]
                    if attempts <= max_retries:
                        log.warning("[rendered] retry %d/%d %s (%s)", attempts, max_retries, url, reason)
                        sleep(delay * attempts)
            if not ok:
                r.failures.append(Failure(url, "render", reason, attempts))
                log.error("[rendered] FAIL %s (%s)", url, reason)
                break  # cannot find the next link without this page

            items = page.evaluate(EXTRACT_JS)
            r.quotes.extend(Quote(text=i["text"], author=i["author"], tags="|".join(i["tags"]),
                                  source="rendered", page=page_no) for i in items)
            r.pages += 1
            log.info("[rendered] page %d: %d quotes", page_no, len(items))

            nxt = page.query_selector("li.next a")
            url = urljoin(url, nxt.get_attribute("href")) if nxt else None
            page_no += 1

        browser.close()

    r.seconds = time.monotonic() - t0
    return r


# --------------------------------------------------------------------------- #
# Cross-check + output
# --------------------------------------------------------------------------- #
def cross_check(a: StrategyResult, b: StrategyResult) -> dict:
    ka = {q.key() for q in a.quotes}
    kb = {q.key() for q in b.quotes}
    return {"only_in_" + a.name: sorted(ka - kb), "only_in_" + b.name: sorted(kb - ka),
            "common": len(ka & kb)}


def write_outputs(results: List[StrategyResult], out_dir: Path, *, started: datetime,
                  robots_note: str, check: Optional[dict], diff: Optional[Diff] = None) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for r in results:
        with open(out_dir / f"quotes_{r.name}.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=[fl.name for fl in fields(Quote)])
            w.writeheader()
            for q in r.quotes:
                w.writerow(asdict(q))

    lines = ["# JS scrape report (quotes.toscrape.com/js)", "",
             f"- Started: {started:%Y-%m-%d %H:%M:%S}",
             f"- robots.txt: {robots_note}", "",
             "| Strategy | Pages | Quotes | Failures | Requests | Time |",
             "|---|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r.name} | {r.pages} | {len(r.quotes)} | {len(r.failures)} | "
                     f"{r.requests_made} | {r.seconds:.1f}s |")
    if check is not None:
        lines += ["", "## Cross-check (api vs rendered)", "",
                  f"- Quotes found by both: **{check['common']}**"]
        for k, v in check.items():
            if k.startswith("only_in_"):
                lines.append(f"- {k}: **{len(v)}**" + (f" — e.g. {v[0][1]}: {v[0][0][:60]}…" if v else ""))
        verdict = "MATCH — the two strategies agree" if not any(
            v for k, v in check.items() if k.startswith("only_in_")) else "MISMATCH — investigate before delivering"
        lines.append(f"- Verdict: **{verdict}**")
    lines += ["", "## Failures", ""]
    fails = [(r.name, fl) for r in results for fl in r.failures]
    if fails:
        lines += ["| Strategy | Stage | Reason | Attempts | URL |", "|---|---|---|---|---|"]
        lines += [f"| {n} | {fl.stage} | {fl.reason} | {fl.attempts} | {fl.url} |" for n, fl in fails]
    else:
        lines.append("None.")
    if diff is not None:
        lines += [""] + diff_markdown(diff, "quotes", label_field="author")
    (out_dir / "report_js.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="JS-rendered site scraper: API strategy vs headless browser strategy")
    ap.add_argument("--pages", type=int, default=3)
    ap.add_argument("--strategy", choices=["api", "rendered", "both"], default="both")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--out", default="output")
    ap.add_argument("--db", default="data/scrape.db", help="SQLite store for change detection ('' to disable)")
    ap.add_argument("--headed", action="store_true", help="show the browser window")
    args = ap.parse_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(out_dir / "run_js.log", encoding="utf-8")])

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    robots, robots_note = load_robots(session, BASE_URL)
    log.info(robots_note)
    if not robots.can_fetch(USER_AGENT, JS_URL):
        log.error("robots.txt disallows %s — stopping", JS_URL)
        return 2

    started = datetime.now()
    results: List[StrategyResult] = []
    if args.strategy in ("api", "both"):
        fetcher = Fetcher(session, RateLimiter(args.delay), robots)
        results.append(scrape_api(fetcher, API_URL, args.pages))
    if args.strategy in ("rendered", "both"):
        results.append(scrape_rendered(JS_URL, args.pages, delay=args.delay,
                                       headless=not args.headed))

    check = cross_check(results[0], results[1]) if len(results) == 2 else None

    diff = None
    if args.db:
        # The API result is the canonical one (rendered is the fallback / verifier).
        canon = results[0]
        store = Store(args.db)
        diff = store.record_run("quotes", ((f"{q.author}::{q.text}", asdict(q)) for q in canon.quotes),
                                n_failures=sum(len(r.failures) for r in results), started=started,
                                crawl_complete=not canon.failures)
        store.close()
        log.info("change detection: %s", diff.summary)

    write_outputs(results, out_dir, started=started, robots_note=robots_note, check=check, diff=diff)
    for r in results:
        log.info("%s: %d quotes from %d pages, %d failures, %.1fs",
                 r.name, len(r.quotes), r.pages, len(r.failures), r.seconds)
    if check:
        log.info("cross-check: common=%d, only api=%d, only rendered=%d", check["common"],
                 len(check["only_in_api"]), len(check["only_in_rendered"]))
    return 0 if all(r.quotes for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
