"""News page: the market's reaction to each story (from the last whole minute before it, next to NIFTY) and the
breaking-news burst alert (several outlets naming one stock within minutes)."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace

import httpx
import pytest

from app.api import market
from app.api import news as news_api
from app.db.models import NewsItem
from app.services import news_tags as nt
from app.services import news_wire as nw
from app.services.event_bus import event_bus
from app.services.instrument_master import InstrumentMaster

REL, NIFTY = "NSE:RELIANCE-EQ", "NSE:NIFTY50-INDEX"
B = 1_800_000_000                                     # a whole minute


@pytest.fixture(autouse=True)
def seed(tmp_path, monkeypatch):
    m = InstrumentMaster(data_dir=tmp_path / "none")  # the built-in seed, whatever data/scrip_master holds
    monkeypatch.setattr(nt, "get_master", lambda: m)
    news_api._series.clear()
    yield
    news_api._series.clear()


def test_move_since_starts_from_the_last_whole_minute_before_the_story():
    c = [[B, 100, 101, 99, 100, 9], [B + 60, 100, 102, 100, 101, 9], [B + 120, 101, 105, 100, 104, 9],
         [B + 180, 104, 104, 98, 99, 9]]
    m = news_api.move_since(c, B + 130)                # the story lands inside the B+120 minute
    assert m == {"t0": B + 60, "px0": 101.0, "px": 99.0, "pct": -1.98, "traded": True, "hi_pct": 3.96, "lo_pct": -2.97}
    assert news_api.move_since(c, B + 130, ltp=103)["pct"] == 1.98                 # the live quote, when there is one
    assert news_api.move_since(c, B + 30) is None                                  # nothing traded before it
    late = news_api.move_since(c, B + 400)
    assert late["px0"] == 99.0 and late["traded"] is False and "hi_pct" not in late   # no trade since: no range


class _Backend:
    def __init__(self, series: dict, ltp: dict):
        self.series, self.ltp, self.calls = series, ltp, []

    async def get_history_range(self, sym, *, resolution, from_ts, to_ts, oi=False):
        self.calls.append((sym, resolution))
        return self.series.get(sym, [])

    async def get_quote(self, syms):
        return [SimpleNamespace(symbol=s, last_price=self.ltp[s]) for s in syms if s in self.ltp]


def _minutes(t0: int, n: int, before: float, after: float, at: int) -> list:
    return [[t, before, before, before, before, 1] if t + 60 <= at else [t, after, after, before, after, 1]
            for t in range(t0, t0 + n * 60, 60)]


def test_reaction_api_measures_the_stock_against_nifty_and_reuses_the_candles(client, isolated_db, db_session,
                                                                              monkeypatch):
    now = datetime.now(timezone.utc).replace(tzinfo=None, second=0, microsecond=0)
    story = now - timedelta(minutes=30)
    at = int(story.replace(tzinfo=timezone.utc).timestamp())
    for k, (title, when) in enumerate([("Reliance Q2 profit rises 10%", story), ("Sensex ends higher", story),
                                       ("Reliance shares slip", now - timedelta(days=5))]):
        db_session.add(NewsItem(url=f"https://x.example/{k}", source="Mint", feed="f", title=title,
                                published_at=when, seen_at=when + timedelta(seconds=40)))
    db_session.commit()
    ids = ",".join(str(i.id) for i in db_session.query(NewsItem).order_by(NewsItem.id))

    assert client.get(f"/api/news/reaction?ids={ids}").json()["ok"] is False    # no Fyers account: nothing to say
    be = _Backend({REL: _minutes(at - 600, 40, 100.0, 102.0, at), NIFTY: _minutes(at - 600, 40, 25000.0, 25050.0, at)},
                  {REL: 102.0, NIFTY: 25050.0})
    monkeypatch.setattr(market, "_fyers_backend", lambda: be)
    r = client.get(f"/api/news/reaction?ids={ids}").json()
    assert r["ok"] is True and list(r["reactions"]) == [ids.split(",")[0]]         # the stock story, not the old one
    rx = r["reactions"][ids.split(",")[0]]
    assert rx["sym"] == REL and rx["px0"] == 100.0 and rx["pct"] == 2.0 and rx["traded"] is True
    assert rx["nifty_pct"] == 0.2 and rx["excess_pct"] == 1.8                       # its own move, beyond the market's
    assert {c[1] for c in be.calls} == {"1"} and len(be.calls) == 2                 # one 1-minute series per symbol
    client.get(f"/api/news/reaction?ids={ids}")
    assert len(be.calls) == 2                                                       # reused within the TTL
    assert client.get("/api/news/reaction?ids=x").status_code == 422


def _rss(*items: tuple[str, str]) -> bytes:
    now = format_datetime(datetime.now(timezone.utc))
    body = "".join(f"<item><title>{t}</title><link>{u}</link><pubDate>{now}</pubDate></item>" for t, u in items)
    return f"<rss><channel>{body}</channel></rss>".encode()


def test_burst_alert_when_three_outlets_name_one_stock(isolated_db, monkeypatch):
    feeds = [{"source": s, "url": f"https://{s.lower()}.example/rss"} for s in ("Alpha", "Beta", "Gamma")]
    body = {f["url"]: _rss((f"{f['source']} morning briefing", f"{f['url']}/0")) for f in feeds}   # backlog
    w = nw.NewsWire()
    w._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=body[str(req.url)])))
    s = {**nw.DEFAULTS, "enabled": True, "bursts": True, "custom_feeds": feeds, "sources": {k: False for k in nw.SOURCES}}

    async def go():
        q = event_bus.subscribe("news.alert")
        try:
            await w.poll(s)                                         # first read of each feed: backlog, no alert
            for k, f in enumerate(feeds):
                body[f["url"]] = _rss((["RIL shares jump 3%", "Reliance hits record high", "Reliance rally lifts Sensex"][k],
                                       f"{f['url']}/1"))
            w.poll_now()
            await w.poll(s)
            evt = await asyncio.wait_for(q.get(), 1)
            assert evt.payload["burst"] is True and evt.payload["symbol"] == REL
            assert evt.payload["keywords"] == ["RELIANCE: 3 outlets in 30 min"] and evt.payload["source"] == "Alpha, Beta, Gamma"
            body[feeds[0]["url"]] = _rss(("Reliance extends gains", f"{feeds[0]['url']}/2"))
            w.poll_now()
            await w.poll(s)
            assert q.empty()                                        # the same burst: no second alert for 2 h
            assert time.time() - w._burst_at[REL] < 5
        finally:
            event_bus.unsubscribe("news.alert", q)

    asyncio.run(go())


def test_burst_settings_validate(client, isolated_db):
    s = client.get("/api/news/settings").json()
    assert s["bursts"] is False and s["burst_sources"] == 3 and s["burst_minutes"] == 30
    assert client.put("/api/news/settings", json={**s, "burst_sources": 1}).status_code == 422
    r = client.put("/api/news/settings", json={**s, "bursts": True, "burst_sources": 4, "burst_minutes": 15}).json()
    assert (r["bursts"], r["burst_sources"], r["burst_minutes"]) == (True, 4, 15)
    st = client.get("/api/news/status").json()
    groups = dict(st["source_groups"])
    assert "NSE" in groups[nw.OFFICIAL] and "Moneycontrol" in groups[nw.MARKETS] and "CNBC" in groups[nw.GLOBAL]
    client.put("/api/news/settings", json=s)
