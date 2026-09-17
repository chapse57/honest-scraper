"""
Alert layer: summarize the latest run of each source from the store and,
if WEBHOOK_URL is set, POST it as {"text": ...} (Slack/Discord-compatible).
Without a webhook it just prints — the GitHub Actions job summary picks it up.
"""
import argparse
import json
import os
import sqlite3
import sys
import urllib.request


def latest_runs(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn.execute("""
        SELECT r.* FROM runs r
        JOIN (SELECT source, MAX(id) AS id FROM runs WHERE finished IS NOT NULL GROUP BY source) m
          ON m.id = r.id ORDER BY r.source""").fetchall()


def oneline(rows):
    return " | ".join(f"{r['source']}: +{r['n_new']} ~{r['n_changed']} -{r['n_gone']}"
                      + (f" ({r['n_failures']} failures)" if r['n_failures'] else "") for r in rows)


def markdown(rows):
    lines = ["### Scrape run summary", "", "| Source | New | Changed | Gone | Unchanged | Failures | Started |",
             "|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['source']} | {r['n_new']} | {r['n_changed']} | {r['n_gone']} | "
                     f"{r['n_unchanged']} | {r['n_failures']} | {r['started']} |")
    attention = [r for r in rows if r["n_new"] or r["n_changed"] or r["n_gone"] or r["n_failures"]]
    lines += ["", ("**Attention needed**" if attention else "No changes, no failures.")]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/scrape.db")
    ap.add_argument("--oneline", action="store_true")
    args = ap.parse_args(argv)
    rows = latest_runs(args.db)
    if args.oneline:
        print(oneline(rows))
        return 0
    text = markdown(rows)
    print(text)
    url = os.environ.get("WEBHOOK_URL")
    if url:
        body = json.dumps({"text": oneline(rows) + "\n" + text, "content": oneline(rows)}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"webhook: HTTP {resp.status}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
