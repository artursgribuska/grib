"""News timestamps with a fake Claude client and fake GDELT: storage, bundle, rating rule."""
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from ingest import news
from tests.test_features import T, build_db


class FakeClaude:
    """Answers the query prompt, then marks headlines containing 'named' as reports_outcome."""
    def __init__(self):
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))
        self.calls = []

    def _create(self, **kw):
        self.calls.append(kw)
        user = kw["messages"][0]["content"]
        if kw["system"] == news.QUERY_PROMPT:
            out = {"query": '"X" resign', "what_news_would_reveal_it": "says X resigned"}
        else:
            lines = [l for l in user.splitlines() if l[:1].isdigit()]
            out = {"relevant": [{"i": int(l.split(".")[0]), "label": "reports_outcome", "why": "says so"}
                                for l in lines if "resigns" in l]}
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(out))])


class FakeHttp:
    def __init__(self, articles):
        self.articles, self.calls = articles, 0

    def get_json(self, url, params):
        self.calls += 1
        return {"articles": self.articles}


def art(hours_before_T, title):
    return {"url": "https://n.example/" + title, "title": title, "domain": "n.example", "language": "English",
            "seendate": (T - timedelta(hours=hours_before_T)).strftime("%Y%m%dT%H%M%SZ")}


@pytest.fixture()
def con():
    from scoring.run import run
    c = build_db()
    run(c)
    return c


def test_news_stored_with_relevance_and_earliest_time(con):
    http = FakeHttp([art(20, "X under pressure"), art(4, "X resigns, sources say"), art(2, "Markets wrap")])
    res = news.run(con, FakeClaude(), http)
    assert res["processed"] >= 1
    q = con.execute("SELECT n_articles, n_relevant, earliest_relevant_at FROM news_queries WHERE market_id='slow'").fetchone()
    assert q[0] == 3 and q[1] == 1 and q[2] == T - timedelta(hours=4)
    assert news.run(con, FakeClaude(), http)["processed"] == 0           # cached: done markets are skipped


def test_bundle_news_before_first_buy_lowers_starting_rating(con):
    from cases.bundle import build_bundle
    # insider's first buy is 6 h before T; relevant news 8 h before T is earlier
    news.run(con, FakeClaude(), FakeHttp([art(8, "X resigns"), art(1, "X resigns officially")]))
    pid = con.execute("SELECT position_id FROM position_scores WHERE account_id='insider'").fetchone()[0]
    b = build_bundle(con, pid)
    n = b["news"]
    assert n["relevant_before_first_buy"] == 1 and n["earliest_relevant_before_first_buy"][0]["hours_before_first_buy"] == 2.0
    assert "news was published before the first buy" in b["score"]["starting_risk_rating_rule"] \
        or b["score"]["starting_risk_rating"] == "Low"
    assert not any(d.startswith("No news") for d in b["data_limitations"])


def test_news_after_first_buy_does_not_lower_rating(con):
    from cases.bundle import build_bundle
    news.run(con, FakeClaude(), FakeHttp([art(1, "X resigns")]))
    pid = con.execute("SELECT position_id FROM position_scores WHERE account_id='insider'").fetchone()[0]
    b = build_bundle(con, pid)
    assert b["news"]["relevant_before_first_buy"] == 0
    assert "news was published" not in b["score"]["starting_risk_rating_rule"]


def test_risk_band_news_rule():
    from scoring.score import risk_band
    strong3 = {"pre_resolution_timing": 0.9, "low_probability_conviction": 0.8, "size_anomaly": 0.75}
    assert risk_band(70, strong3, 0.0, news_before_first_buy=True)[0] == "Medium"
    assert risk_band(45, strong3, 0.0, news_before_first_buy=True)[0] == "Low"


def test_bundle_without_news_search_says_so(con):
    from cases.bundle import build_bundle
    pid = con.execute("SELECT position_id FROM position_scores LIMIT 1").fetchone()[0]
    b = build_bundle(con, pid)
    assert b["news"] == {"status": "not searched"}
    assert any(d.startswith("No news") for d in b["data_limitations"])


def test_dashboard_flags_news_before_first_buy(con, tmp_path):
    from app.build import build
    news.run(con, FakeClaude(), FakeHttp([art(8, "X resigns")]))
    page = build(con, top=10, out=tmp_path / "i.html").read_text()
    assert "Relevant news before the first buy" in page and "X resigns" in page


def test_unreachable_gdelt_is_not_reported_as_no_news(con):
    from ingest.http import ApiError
    from cases.bundle import build_bundle

    class DownHttp:
        def get_json(self, url, params):
            raise ApiError("connection reset")

        def get_text(self, url, params):
            raise ApiError("connection reset")
    news.run(con, FakeClaude(), DownHttp())
    assert con.execute("SELECT status FROM news_queries WHERE market_id='slow'").fetchone()[0] == "news source unreachable"
    pid = con.execute("SELECT position_id FROM position_scores WHERE account_id='insider'").fetchone()[0]
    b = build_bundle(con, pid)
    assert b["news"]["status"] == "news source unreachable"
    assert any(d.startswith("No news") for d in b["data_limitations"])     # limitation kept
    # retried next run
    res = news.run(con, FakeClaude(), FakeHttp([art(8, "X resigns")]))
    assert res["processed"] >= 1


def _rss(items):
    body = "".join(f"<item><title>{t}</title><link>https://g.example/{i}</link><pubDate>{d}</pubDate>"
                   f"<source url='https://src.example'>Src</source></item>" for i, (t, d) in enumerate(items))
    return f"<?xml version='1.0'?><rss><channel>{body}</channel></rss>"


class GoogleOnlyHttp:
    """GDELT down; Google News answers with one minute-precise and one day-precise item."""
    def __init__(self, items):
        self.items, self.gdelt_calls, self.google_calls = items, 0, 0

    def get_json(self, url, params):
        self.gdelt_calls += 1
        from ingest.http import ApiError
        raise ApiError("HTTP 429")

    def get_text(self, url, params):
        self.google_calls += 1
        assert "after:" in params["q"] and "before:" in params["q"]
        return _rss(self.items)


def test_google_news_fallback_and_day_precision(con):
    from email.utils import format_datetime
    from datetime import timezone
    precise = (T - timedelta(hours=30)).replace(minute=17, tzinfo=timezone.utc)
    day_only = (T - timedelta(hours=3)).replace(hour=7, minute=0, second=0, tzinfo=timezone.utc)
    http = GoogleOnlyHttp([("X resigns, sources say", format_datetime(precise)),
                           ("X resigns: official", format_datetime(day_only))])
    news.run(con, FakeClaude(), http)
    assert http.gdelt_calls >= 1 and http.google_calls >= 1
    q = con.execute("SELECT status, source, n_relevant FROM news_queries WHERE market_id='slow'").fetchone()
    assert q == ("ok", "google_news", 2)
    a = con.execute("SELECT seen_at, time_precision FROM news_articles WHERE market_id='slow' ORDER BY seen_at").fetchall()
    assert a[0][1] == "minute" and a[1][1] == "day"
    assert (a[1][0].date() - day_only.date()).days == 1 and (a[1][0].hour, a[1][0].minute) == (23, 59)
    assert a[1][0] > T                                          # so it can never read as preceding the buys
    out = news.news_for_position(con, "slow", T - timedelta(hours=2))
    assert out["news_source"] == "google_news"
    assert any("date only" in x.get("time_precision", "") for x in out["earliest_relevant_overall"])


def test_windows_older_than_gdelt_go_straight_to_google_news():
    from datetime import datetime
    import pandas as pd
    m = SimpleNamespace(market_id="x", question="q", winner="Yes", category="personnel",
                        first_flagged_buy=pd.Timestamp(datetime(2025, 1, 1)), certainty_at=pd.Timestamp(datetime(2025, 1, 2)))
    http = GoogleOnlyHttp([])
    row, arts = news.process_market(FakeClaude(), http, m)
    assert http.gdelt_calls == 0 and http.google_calls == 1
    assert row["status"] == "ok" and row["source"] == "google_news" and arts == []


def test_gdelt_failure_trips_breaker_for_the_rest_of_the_run():
    from datetime import datetime
    import pandas as pd
    now = datetime.now()
    m = SimpleNamespace(market_id="x", question="q", winner="Yes", category="personnel",
                        first_flagged_buy=pd.Timestamp(now - timedelta(days=3)), certainty_at=pd.Timestamp(now - timedelta(days=2)))
    http, ok = GoogleOnlyHttp([]), [True]
    news.process_market(FakeClaude(), http, m, ok)
    news.process_market(FakeClaude(), http, m, ok)
    assert ok == [False] and http.gdelt_calls == 1 and http.google_calls == 2
