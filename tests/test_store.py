"""Change detection: new / changed / gone / unchanged, and the incomplete-crawl guard."""
from store import Store, diff_markdown


def items(**kv):
    return [(k, {"title": k, "price": v}) for k, v in kv.items()]


def test_first_run_stores_everything_and_has_no_baseline(tmp_path):
    s = Store(tmp_path / "t.db")
    d = s.record_run("books", items(a=1, b=2))
    assert d.is_first_run and d.new == ["a", "b"] and d.unchanged == 0
    assert "first run" in d.summary
    assert len(s.current_items("books")) == 2


def test_second_run_detects_new_changed_gone_unchanged(tmp_path):
    s = Store(tmp_path / "t.db")
    s.record_run("books", items(a=1, b=2, c=3))
    d = s.record_run("books", items(a=1, b=5, d=4))          # b changed, c gone, d new
    assert not d.is_first_run
    assert d.new == ["d"]
    assert [(k, b["price"], a["price"]) for k, b, a in d.changed] == [("b", 2, 5)]
    assert [k for k, _ in d.gone] == ["c"]
    assert d.unchanged == 1
    assert d.summary == "+1 new, ~1 changed, -1 gone, 1 unchanged"
    # the store now reflects the latest state
    assert {i["title"]: i["price"] for i in s.current_items("books")} == {"a": 1, "b": 5, "d": 4}


def test_incomplete_crawl_never_reports_gone(tmp_path):
    """If a listing page failed, missing items are unknown, not gone."""
    s = Store(tmp_path / "t.db")
    s.record_run("books", items(a=1, b=2))
    d = s.record_run("books", items(a=1), crawl_complete=False)
    assert d.gone == [] and d.unchanged == 1


def test_item_that_was_gone_and_returns_is_reported_as_new(tmp_path):
    s = Store(tmp_path / "t.db")
    s.record_run("books", items(a=1, b=2))
    s.record_run("books", items(a=1))                        # b gone
    d = s.record_run("books", items(a=1, b=2))               # b back
    assert d.new == ["b"] and d.gone == [] and d.unchanged == 1


def test_sources_are_isolated_and_history_is_recorded(tmp_path):
    s = Store(tmp_path / "t.db")
    s.record_run("books", items(a=1))
    d = s.record_run("quotes", items(a=1))
    assert d.is_first_run                                     # different source, no baseline
    hist = s.history("books")
    assert len(hist) == 1 and hist[0]["n_new"] == 1 and hist[0]["finished"]


def test_duplicate_keys_within_one_run_are_counted_once(tmp_path):
    s = Store(tmp_path / "t.db")
    d = s.record_run("books", items(a=1) + items(a=1))
    assert d.new == ["a"]


def test_diff_markdown_lists_field_level_changes(tmp_path):
    s = Store(tmp_path / "t.db")
    s.record_run("books", items(a=1, b=2))
    d = s.record_run("books", items(a=9, c=3))
    md = "\n".join(diff_markdown(d, "books"))
    assert "| a | price | 1 | 9 |" in md
    assert "- c" in md and "- b (b)" in md
