# honest-scraper — nothing is silently dropped

Two small Python scrapers built around one rule: **every URL that fails is reported with the reason and the
number of attempts.** A scraper that returns 950 rows from 1,000 pages and doesn't tell you where the other 50
went is a liability, not a deliverable.

Both targets are sandbox sites made for scraping practice (toscrape.com), so running this is allowed.

| | `scraper.py` | `js_scraper.py` |
|---|---|---|
| Target | books.toscrape.com (static HTML) | quotes.toscrape.com/js (JavaScript-rendered) |
| Method | requests + BeautifulSoup | **A)** the site's own JSON API · **B)** Playwright headless Chromium |
| Extra | de-dup by UPC, injected failures for the demo | the two methods are run side by side and **cross-checked** |

Both feed one pipeline layer: `store.py` (SQLite, change detection) → `notify.py` (summary / webhook) →
`.github/workflows/scrape.yml` (runs daily on GitHub Actions and commits the results). A one-off scrape answers
"what is on the site?"; the pipeline answers "what changed since yesterday?" — which is the question that is
actually worth paying for.

## Actual run (2026-09-17, Windows PC)

`scraper.py --pages 3 --inject-failures`: 3 listing pages, 65 requests, 0 retries needed, **60 books saved,
2 failures** — both the injected ones (a 404 and a non-book page), each listed in `failures.csv` with its reason.
Success rate 96.8%, 64.7s at 1 request/second.

`js_scraper.py --pages 3 --strategy both`: API strategy 3 requests / 2.2s, browser strategy 3 page loads / 6.8s,
30 quotes each, **cross-check MATCH** (0 only-in-api, 0 only-in-rendered).

## What is handled, and how

| Concern | Handling |
|---|---|
| Temporary errors (429, 5xx, timeout, connection drop) | Retry with exponential backoff (1s, 2s, 4s + jitter). `Retry-After` header honored. |
| Permanent errors (404, 403) | No retry — recorded as a failure immediately. |
| Rate limiting | Minimum interval between requests (`--delay`, default 1s). Same limiter for the browser strategy. |
| robots.txt | Checked before every request. Unreachable robots.txt (5xx / network error) = *disallow all*, stop. |
| Duplicates | De-duplicated by the item's own ID (UPC), not by URL. Skipped count goes in the report. |
| Unexpected page shape | Parser raises a clear error (`missing fields: UPC`, `title not found`) → failure list. |
| Encoding | Raw bytes decoded as UTF-8, so `£51.77` doesn't become `Â£51.77`. CSV written with BOM for Excel. |
| JavaScript-rendered pages | Playwright waits for the rendered selector, not for "page load". Timeouts are retried, then reported. Images/fonts are blocked to reduce load. |
| JS site with a hidden API | Try the JSON endpoint first (`/api/quotes?page=N`) — faster and lighter for the server. The browser path is the fallback. |
| Trusting the result | The API result and the rendered result are compared; the report says **MATCH** or **MISMATCH**. |
| What changed since last run | Every item is keyed by its own stable ID and hashed into SQLite. Each run reports **new / changed (field by field) / gone / unchanged**. |
| "Gone" vs "couldn't fetch" | If a listing page failed, the crawl is marked incomplete and *gone* detection is suppressed — an item we failed to reach is not an item that disappeared. |
| Running it every day | GitHub Actions cron (free): tests → both scrapers → change summary in the job page → results committed to `data/`. Optional `WEBHOOK_URL` secret posts the summary to Slack/Discord. |

## Output

```
output/
  books.csv / books.json      static-site data
  failures.csv                url, stage (fetch/parse/robots/render), reason, attempts
  report.md                   run summary + failure table
  quotes_api.csv              JS site, strategy A
  quotes_rendered.csv         JS site, strategy B
  report_js.md                pages / quotes / failures / requests / time per strategy + cross-check verdict
  run.log, run_js.log
data/
  scrape.db                   SQLite: runs, items, changes (the baseline for the next run)
  latest/                     the reports from the last scheduled run (written by GitHub Actions)
```

`report.md` / `report_js.md` end with a *Changes since last run* section, e.g.
`+0 new, ~1 changed, -0 gone, 59 unchanged` and a field-level table for the changed items.

## Run

Windows: double-click `run.bat` — creates a venv, installs Chromium for Playwright, runs the tests, then both scrapers.

Manual:

```bash
pip install -r requirements.txt
python -m playwright install chromium
python -m pytest -v
python scraper.py --pages 3 --inject-failures
python js_scraper.py --pages 3 --strategy both      # or --strategy api / rendered, --headed to watch
```

`--inject-failures` adds two known-bad URLs (a 404 and a non-book page) so the failure report can be seen
working. They are labeled as injected in the report — they are not site errors.

## Tests (30, all offline)

- Parsing against saved HTML fixtures; relative-URL resolution; last-page detection; missing-field errors.
- Retry timing (1s → 2s), no retry on 404, give-up after N retries, `Retry-After`, robots.txt blocking, rate limiter.
- De-dup by UPC; a failed listing page is reported, not swallowed.
- **Playwright is tested for real** against a local HTTP server whose page renders its data 200ms after load
  via JavaScript — the raw HTML contains no quotes, so passing proves the scraper waited for the JS.
  A page that never renders must produce a timeout failure with the attempt count.
- End-to-end: API strategy and browser strategy on the same local site must agree.
- Store: new / changed / gone / unchanged, item that disappears and comes back, incomplete crawl never reports "gone",
  sources isolated. Full CLI run twice with a price change in between → the report shows the field-level diff.

## Honest limits

- No login, CAPTCHA, or anti-bot evasion, and I don't build those for sites whose terms forbid scraping.
  For Google Maps, TikTok, Instagram and similar the right answer is usually an official or paid API
  (Outscraper, SerpApi, the platform's own API) — I'll say so up front rather than sell a scraper that breaks in a week.
- No resume/checkpoint inside a run: a crashed run starts over (the store keeps the previous baseline, so nothing is lost).
  For 1,000+ pages, per-page checkpointing is the first thing I'd add.
- Change detection is per item, not per page: a site that reorders items is fine; a site that changes its item IDs between visits would look like everything is new+gone. The key choice is per site and documented in code.
- Single-threaded by design (politeness > speed for sites this size). Concurrency is easy to add once the target's rate limits are known.
- Playwright's `page.evaluate` extraction is written per site; there is no generic "scrape anything" mode.

`sample_output/` holds the files from the run above, unedited.
