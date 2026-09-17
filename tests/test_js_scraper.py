"""
Tests for js_scraper.py — run against a local HTTP server that mimics
quotes.toscrape.com/js: the HTML contains NO quote markup; a script renders
it 200ms later. This proves Playwright really waits for the JavaScript.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import js_scraper
from js_scraper import Quote, StrategyResult, cross_check, parse_api_page, scrape_api, scrape_rendered
from tests.test_scraper import FakeResp, FakeSession, make_fetcher

QUOTES = {
    1: [{"text": "“Be yourself.”", "author": {"name": "Oscar Wilde"}, "tags": ["life", "be-yourself"]},
        {"text": "“Simplicity.”", "author": {"name": "Leonardo"}, "tags": []}],
    2: [{"text": "“Third one.”", "author": {"name": "Anon"}, "tags": ["x"]}],
}

JS_PAGE = """<!DOCTYPE html><html><body>
<div class="quotes"></div>
<nav><ul class="pager">%(next)s</ul></nav>
<script>
var data = %(data)s;
setTimeout(function () {
  var box = document.querySelector('.quotes');
  for (var i = 0; i < data.length; i++) {
    var q = data[i];
    var tags = q.tags.map(function (t) { return '<a class="tag">' + t + '</a>'; }).join('');
    box.innerHTML += '<div class="quote"><span class="text">' + q.text + '</span>'
      + '<small class="author">' + q.author.name + '</small><div class="tags">' + tags + '</div></div>';
  }
}, 200);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def do_GET(self):
        path = self.path
        if path.startswith("/api/quotes"):
            page = int(path.split("page=")[1]) if "page=" in path else 1
            if page == 3:
                return self._send(500, "boom")
            body = {"page": page, "has_next": page < 3, "quotes": QUOTES.get(page, [])}
            return self._send(200, json.dumps(body), "application/json")
        if path in ("/js/", "/js/page/1/"):
            return self._send(200, JS_PAGE % {"data": json.dumps(QUOTES[1]),
                                              "next": '<li class="next"><a href="/js/page/2/">Next</a></li>'})
        if path == "/js/page/2/":
            return self._send(200, JS_PAGE % {"data": json.dumps(QUOTES[2]), "next": ""})
        if path == "/js/never/":
            # JS never renders anything -> wait_for_selector must time out
            return self._send(200, "<html><body><div class='quotes'></div></body></html>")
        return self._send(404, "nope")


@pytest.fixture(scope="module")
def server():
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


# ------------------------------------------------------------------ api --- #
def test_parse_api_page_flattens_author_and_tags():
    raw = json.dumps({"has_next": True, "quotes": QUOTES[1]}).encode()
    quotes, has_next = parse_api_page(raw, page=1)
    assert has_next is True
    assert quotes[0] == Quote("“Be yourself.”", "Oscar Wilde", "life|be-yourself", "api", 1)
    assert quotes[1].tags == ""


def test_scrape_api_stops_at_has_next_false_and_reports_5xx():
    ok = json.dumps({"has_next": True, "quotes": QUOTES[1]}).encode()
    s = FakeSession({"a?page=1": [FakeResp(200, ok)], "a?page=2": [FakeResp(500)] * 2})
    f, _ = make_fetcher(s, retries=1)
    r = scrape_api(f, "a", max_pages=5)
    assert len(r.quotes) == 2 and r.pages == 1
    assert r.failures[0].url == "a?page=2" and "gave up" in r.failures[0].reason


def test_scrape_api_bad_json_is_a_parse_failure():
    s = FakeSession({"a?page=1": [FakeResp(200, b"<html>not json</html>")]})
    f, _ = make_fetcher(s)
    r = scrape_api(f, "a", max_pages=1)
    assert r.quotes == [] and r.failures[0].stage == "parse"


# ------------------------------------------------------------- rendered --- #
def test_rendered_waits_for_js_and_follows_next(server):
    r = scrape_rendered(server + "/js/", max_pages=5, delay=0, sleep=lambda s: None)
    assert r.pages == 2 and r.failures == []
    assert [q.key() for q in r.quotes] == [
        ("“Be yourself.”", "Oscar Wilde"), ("“Simplicity.”", "Leonardo"), ("“Third one.”", "Anon")]
    assert r.quotes[0].tags == "life|be-yourself" and r.quotes[0].source == "rendered"


def test_rendered_timeout_is_reported_with_attempt_count(server):
    r = scrape_rendered(server + "/js/never/", max_pages=1, delay=0, timeout_ms=500,
                        max_retries=1, sleep=lambda s: None)
    assert r.quotes == []
    assert len(r.failures) == 1
    assert r.failures[0].stage == "render" and r.failures[0].attempts == 2
    assert "Timeout" in r.failures[0].reason


# ---------------------------------------------------------- cross-check --- #
def test_cross_check_reports_differences():
    a = StrategyResult("api", quotes=[Quote("x", "A", "", "api", 1), Quote("y", "B", "", "api", 1)])
    b = StrategyResult("rendered", quotes=[Quote("x", "A", "", "rendered", 1)])
    c = cross_check(a, b)
    assert c["common"] == 1 and c["only_in_api"] == [("y", "B")] and c["only_in_rendered"] == []


def test_end_to_end_against_local_server(server, tmp_path, monkeypatch):
    """api + rendered on the same local site must agree."""
    import requests
    from scraper import Fetcher, RateLimiter
    from tests.test_scraper import AllowAll
    from datetime import datetime

    fetcher = Fetcher(requests.Session(), RateLimiter(0), AllowAll())
    a = scrape_api(fetcher, server + "/api/quotes", max_pages=2)
    b = scrape_rendered(server + "/js/", max_pages=2, delay=0, sleep=lambda s: None)
    check = cross_check(a, b)
    assert check["common"] == 3 and not check["only_in_api"] and not check["only_in_rendered"]
    js_scraper.write_outputs([a, b], tmp_path, started=datetime.now(), robots_note="test", check=check)
    report = (tmp_path / "report_js.md").read_text(encoding="utf-8")
    assert "MATCH" in report and (tmp_path / "quotes_rendered.csv").exists()
