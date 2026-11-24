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
