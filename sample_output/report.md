# Scrape run report

- Started: 2026-09-17 16:10:14
- Duration: 64.6s
- robots.txt: robots.txt returned 404 -> no rules, all allowed
- Listing pages crawled: 3
- HTTP requests: 65 (retries: 0)
- Books saved: **60**
- Duplicates skipped (same UPC): 0
- Failures: **2**
- Success rate (fetched & parsed): 96.8%

> Note: the following URLs were injected on purpose (--inject-failures)
> to demonstrate failure reporting. They are not site errors.

> - https://books.toscrape.com/catalogue/this-book-does-not-exist_99999/index.html
> - https://books.toscrape.com/index.html

## Failures

| Stage | Reason | Attempts | URL |
|---|---|---|---|
| fetch | HTTP 404 | 1 | https://books.toscrape.com/catalogue/this-book-does-not-exist_99999/index.html |
| parse | title not found | 1 | https://books.toscrape.com/index.html |

## Changes since last run (books)

- first run: 60 items stored (no baseline to compare against)

