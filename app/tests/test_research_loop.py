"""B1 research loop: split + walk-forward + DSR + PBO on one set of backtests, a verdict, a card."""
from __future__ import annotations

import random

from app.algo import engine
from app.research import loop
from app.tests.test_algo import DAY0, _cross_spec, bars


def _walk(n_days: int, drift: float, seed: int = 1):
    """n sessions of 75 x 5m bars: a random walk, with `drift` per bar."""
    rng = random.Random(seed)
    t, c, px = [], [], 100.0
    for k in range(n_days):
        for i in range(75):
            t.append(DAY0 + k * 86400 + i * 300)
            px += rng.gauss(drift, 0.15)
            c.append(px)
    d = bars(c, spread=0.05)
    d["t"] = t
    return d


def _ema(**kw):
    spec = _cross_spec(exit_long=None, target={"type": "pct", "value": 0.4}, stop_loss={"type": "pct", "value": 0.4}, **kw)
    spec["entry_long"]["conditions"][0]["right"] = {"ind": "EMA", "params": {"period": 20}}
    return engine.normalize(spec)


GRID = [{"path": "target.value", "values": [0.3, 0.4, 0.6]}]


def _oos(d, frac=0.7):
    return d["t"][int(len(d["t"]) * frac)]


def test_noise_is_killed_and_a_real_drift_passes():
    noise, drift = _walk(60, 0.0), _walk(60, 0.02)
    for grid in (None, GRID):
        dead = loop.check(_ema(), {"X": noise}, grid, oos_from=_oos(noise), min_trades=5)
        assert dead["verdict"] == "kill"
        assert next(c for c in dead["checks"] if c["key"] == "dsr")["pass"] is False
        live = loop.check(_ema(), {"X": drift}, grid, oos_from=_oos(drift), min_trades=5)
        assert live["verdict"] == "pass", live["checks"]
        assert all(c["pass"] is not False for c in live["checks"])


def test_every_try_counts_and_more_tries_deflate():
    d = _walk(60, 0.01, seed=3)
    few = loop.check(_ema(), {"X": d}, GRID, oos_from=_oos(d), min_trades=5)
    many = loop.check(_ema(), {"X": d}, GRID, oos_from=_oos(d), min_trades=5, prior_trials=500)
    assert few["trials"] == 3 and many["trials"] == 503
    assert few["overfitting"]["deflated_sharpe"]["n_trials"] == 3
    assert many["overfitting"]["deflated_sharpe"]["dsr"] < few["overfitting"]["deflated_sharpe"]["dsr"]
    one = loop.check(_ema(), {"X": d}, None, oos_from=_oos(d), min_trades=5, prior_trials=7)
    assert one["trials"] == 8 and one["fixed"] and one["combos"] == 1
    pbo = next(c for c in one["checks"] if c["key"] == "pbo")
    assert pbo["pass"] is None and "fixed rules" in pbo["note"]     # n/a, not a failure


def test_the_choice_never_sees_the_held_out_period():
    """The chosen combo is the in-sample winner; its held-out trades all
    entered after the split, and the walk-forward's unseen trades after the
    first window opened."""
    d = _walk(60, 0.01, seed=5)
    oos = _oos(d)
    res = loop.check(_ema(), {"X": d}, GRID, oos_from=oos, min_trades=5)
    by_hand = []
    for v in (0.3, 0.4, 0.6):
        s = _ema()
        s["target"]["value"] = v
        st = engine.stats([x for x in engine.run(engine.normalize(s), {"X": d})["trades"] if x["entry_t"] < oos], 100000)
        by_hand.append((st["net_pnl"], v))
    assert res["split"]["params"] == {"target.value": max(by_hand)[1]}
    assert res["split"]["oos_from"] == oos and res["split"]["oos"]["trades"] > 0
    wf = res["walk_forward"]
    assert wf["oos"]["equity"][0][0] >= wf["folds"][0]["oos_from"]
    assert wf["oos"]["trades"] == sum(f["oos"]["trades"] for f in wf["folds"] if f["oos"])


def test_too_few_trades_fail_instead_of_passing_on_luck():
    d = _walk(60, 0.02)
    res = loop.check(_ema(), {"X": d}, None, oos_from=_oos(d), min_trades=10_000)
    wf = next(c for c in res["checks"] if c["key"] == "walk_forward")
    held = next(c for c in res["checks"] if c["key"] == "held_out")
    assert res["verdict"] == "kill" and wf["pass"] is False and held["pass"] is False
    assert "trades" in wf["note"] and "trades" in held["note"]
    # a grid where no combo reaches min_trades in-sample: nothing chosen, killed
    res = loop.check(_ema(), {"X": d}, GRID, oos_from=_oos(d), min_trades=10_000)
    assert res["split"] is None and res["verdict"] == "kill"
    assert "in-sample trades" in next(c for c in res["checks"] if c["key"] == "held_out")["note"]


def test_verdict_bar():
    base = {"min_trades": 5, "trials": 10, "fixed": False,
            "walk_forward": {"oos": {"trades": 20, "net_pnl": 100.0}},
            "split": {"oos": {"trades": 9, "net_pnl": 50.0}},
            "overfitting": {"deflated_sharpe": {"dsr": 0.96}, "pbo": {"pbo": 0.2}}}
    assert loop.verdict(base)[1] == "pass"
    assert loop.verdict({**base, "overfitting": {"deflated_sharpe": {"dsr": 0.94}, "pbo": {"pbo": 0.2}}})[1] == "kill"
    assert loop.verdict({**base, "overfitting": {"deflated_sharpe": {"dsr": 0.99}, "pbo": {"pbo": 0.6}}})[1] == "kill"
    assert loop.verdict({**base, "walk_forward": {"oos": {"trades": 20, "net_pnl": -1.0}}})[1] == "kill"
    assert loop.verdict({**base, "split": {"oos": {"trades": 9, "net_pnl": -1.0}}})[1] == "kill"
    nodata = loop.verdict({**base, "overfitting": {"note": "fewer than 10 trading days"}})
    assert nodata[1] == "kill" and "10 trading days" in nodata[0][0]["note"]
    assert loop.verdict(base, {"dsr": 0.99})[1] == "kill"            # the bar can be raised


def test_rules_text_reads_the_spec():
    spec = {**_ema(), "direction": "both"}
    spec["entry_short"] = {"logic": "OR", "conditions": [
        {"left": {"ind": "RSI", "params": {"period": 14}}, "op": "above", "right": {"value": 70}},
        {"logic": "AND", "conditions": [{"left": {"ind": "PRICE"}, "op": "below", "right": {"value": 99}, "negate": True}]}]}
    txt = loop.rules_text(spec)
    assert txt[0].startswith("NSE:TEST-EQ · 5m · both")
    assert txt[1] == "Long when PRICE crosses above EMA(20)"
    assert txt[2] == "Short when RSI(14) above 70 OR (NOT PRICE below 99)"
    assert "Stop 0.4 pct · target 0.4 pct" in txt[3]


# ---- API: run, save, list, read, edit ---------------------------------------

def _fake_assemble(monkeypatch, d):
    from app.api import algo as api

    async def fake(spec, start, end):
        return {s: d for s in spec["symbols"]}, [], DAY0

    monkeypatch.setattr(api.data, "assemble", fake)


def test_api_runs_the_check_counts_tries_and_keeps_the_card(client, monkeypatch, isolated_db):
    from app.db import session as dbs
    from app.db.models import AlgoResearchCard

    _fake_assemble(monkeypatch, _walk(60, 0.02))
    spec = _ema()
    rng = {"start": "2025-01-01", "end": "2025-03-15"}
    s = client.post("/api/algo/strategies", json={"name": "ema-drift", "spec": spec, "trials": 4}).json()
    r = client.post("/api/algo/research/check", json={
        "spec": spec, **rng, "grid": GRID, "min_trades": 5, "strategy_id": s["id"],
        "hypothesis": "momentum after an EMA reclaim"}).json()
    assert r["verdict"] == "pass" and r["trials"] == 7 and r["name"] == "ema-drift" and r["version"] == 1
    assert r["setup"]["oos_pct"] == 30 and r["setup"]["walk_forward"] == {"windows": 4, "is_mult": 3.0, "anchored": False}
    assert r["rules"][1] == "Long when PRICE crosses above EMA(20)"
    assert client.get("/api/algo/strategies").json()["strategies"][0]["trials"] == 7      # the check's combos count
    # a draft: its own count, its own name
    d = client.post("/api/algo/research/check", json={"spec": spec, **rng, "trials": 2, "min_trades": 10_000}).json()
    assert d["trials"] == 3 and d["strategy_id"] is None and d["name"] == "Unsaved draft" and d["verdict"] == "kill"

    cards = client.get("/api/algo/research/cards").json()
    assert [c["id"] for c in cards["cards"]] == [d["id"], r["id"]] and cards["bar"] == {"dsr": 0.95, "pbo": 0.5}
    assert "result" not in cards["cards"][0] and cards["cards"][1]["headline"]["dsr"] >= 0.95
    assert [c["id"] for c in client.get("/api/algo/research/cards", params={"verdict": "kill"}).json()["cards"]] == [d["id"]]
    assert [c["id"] for c in client.get("/api/algo/research/cards", params={"strategy_id": s["id"]}).json()["cards"]] == [r["id"]]

    full = client.get(f"/api/algo/research/cards/{r['id']}").json()
    assert full["result"]["split"]["params"] and full["spec"]["symbols"] == ["NSE:TEST-EQ"]
    md = client.get(f"/api/algo/research/cards/{r['id']}/markdown").text
    assert md.startswith("# ema-drift — PASS") and "momentum after an EMA reclaim" in md and "| PBO ≤ 50% |" in md

    e = client.patch(f"/api/algo/research/cards/{r['id']}", json={"note": "paper from Monday", "verdict": "kill"}).json()
    assert e["note"] == "paper from Monday" and e["verdict"] == "pass"           # the verdict is the record
    assert client.get("/api/algo/research/cards/99999").status_code == 404

    # deleting the strategy keeps its card
    assert client.delete(f"/api/algo/strategies/{s['id']}").status_code == 200
    with dbs.SessionLocal() as db:
        c = db.get(AlgoResearchCard, r["id"])
        assert c.strategy_id is None and c.name == "ema-drift"


def test_api_rejects_a_bad_check(client, monkeypatch, isolated_db):
    _fake_assemble(monkeypatch, _walk(30, 0.0))
    rng = {"start": "2025-01-01", "end": "2025-02-15"}
    bad = client.post("/api/algo/research/check", json={"spec": _ema(), **rng, "walk_forward": {"windows": 20}})
    assert bad.status_code == 422 and "2-12" in bad.json()["detail"]
    bad = client.post("/api/algo/research/check", json={"spec": _ema(), **rng, "oos_pct": 95})
    assert bad.status_code == 422 and "between 5 and 80" in bad.json()["detail"]
    bad = client.post("/api/algo/research/check", json={"spec": _ema(), **rng, "metric": "luck"})
    assert bad.status_code == 422 and "metric" in bad.json()["detail"]
