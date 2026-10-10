"""Numeric surprise from a results filing's XBRL.

The evidence on where the tradable news in an earnings release sits points
the same way: it is in the text describing NUMBERS near the start of the
disclosure, and most of the revision lands within minutes. The bot declines
Q1_RESULTS filings that go on to move 56.7% of the time. That is the
category where a deterministic parser can read the numbers an LLM is asked
to guess.

NSE and BSE results come with an XBRL instance (Ind-AS financial-results
taxonomy). This module reads one with the standard library only:

  * contexts -> period (start/end or instant), skipping dimensional
    contexts (segment / scenario members such as business segments);
  * facts for a small set of concepts, matched by LOCAL name with aliases,
    because the namespace prefix differs between NSE and BSE files;
  * the current quarter is the latest ~3-month duration; the previous
    quarter ends ~3 months before it, the year-ago quarter ~12 months;
  * YoY / QoQ deltas for revenue, EBITDA (reported, or rebuilt as PBT +
    finance costs + D&A) and PAT, plus the PAT-margin change in percentage
    points.

These are features for the meta-labeling model and for research, not a
trade trigger. Consensus estimates exist for only ~900 Indian companies,
which leaves out most small caps, where drift is largest. So a "surprise"
here is measured against the company's own history.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

# Local names, first match wins. Lower-cased for matching.
CONCEPTS: dict[str, tuple[str, ...]] = {
    "revenue": ("revenuefromoperations", "revenuefromoperationsnet", "totalrevenuefromoperations",
                "incomefromoperations", "revenue"),
    "other_income": ("otherincome",),
    "finance_costs": ("financecosts", "financecost", "interestexpense"),
    "depreciation": ("depreciationdepletionandamortisationexpense", "depreciationandamortisationexpense",
                     "depreciationamortisationandimpairmentexpense"),
    "pbt": ("profitbeforetax", "profitlossbeforetax", "profitbeforeexceptionalitemsandtax"),
    "pat": ("profitlossforperiod", "profitlossforperiodfromcontinuingoperations", "profitforperiod",
            "netprofitlossforperiod"),
    "ebitda": ("ebitda", "earningsbeforeinteresttaxdepreciationandamortisation"),
    "eps_basic": ("basicearningslosspershare", "basicearningslosspersharefromcontinuingoperations",
                  "basicearningspershare"),
}


@dataclass(frozen=True)
class Period:
    start: Optional[date]
    end: Optional[date]

    @property
    def days(self) -> Optional[int]:
        return (self.end - self.start).days if self.start and self.end else None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag.split(":", 1)[-1]


def _date(text: Optional[str]) -> Optional[date]:
    if not text:
        return None
    try:
        return date.fromisoformat(text.strip()[:10])
    except ValueError:
        return None


def parse_instance(xml_bytes: bytes) -> dict[str, dict[Period, float]]:
    """{metric: {period: value}} for the concepts above, non-dimensional only."""
    root = ET.fromstring(xml_bytes)
    periods: dict[str, Period] = {}
    for ctx in root.iter():
        if _local(ctx.tag) != "context":
            continue
        if any(_local(e.tag) in ("segment", "scenario") for e in ctx.iter()):
            continue
        start = end = None
        for e in ctx.iter():
            name = _local(e.tag)
            if name == "startDate":
                start = _date(e.text)
            elif name in ("endDate", "instant"):
                end = _date(e.text)
        cid = ctx.get("id")
        if cid:
            periods[cid] = Period(start, end)

    alias_to_metric = {a: m for m, aliases in CONCEPTS.items() for a in aliases}
    priority = {a: i for aliases in CONCEPTS.values() for i, a in enumerate(aliases)}
    out: dict[str, dict[Period, float]] = {}
    chosen: dict[tuple[str, Period], int] = {}
    for el in root.iter():
        name = _local(el.tag).lower()
        metric = alias_to_metric.get(name)
        if metric is None:
            continue
        period = periods.get(el.get("contextRef") or "")
        if period is None or el.text is None:
            continue
        try:
            value = float(el.text.strip().replace(",", ""))
        except ValueError:
            continue
        key = (metric, period)
        if key in chosen and chosen[key] <= priority[name]:
            continue                    # a higher-priority alias already filled it
        chosen[key] = priority[name]
        out.setdefault(metric, {})[period] = value
    return out


def _quarters(series: dict[Period, float]) -> list[tuple[Period, float]]:
    """~3-month duration facts, newest first."""
    q = [(p, v) for p, v in series.items() if p.days is not None and 80 <= p.days <= 100]
    return sorted(q, key=lambda pv: pv[0].end, reverse=True)


def _find(quarters: list[tuple[Period, float]], target_end: date, tolerance_days: int = 20) -> Optional[float]:
    for p, v in quarters:
        if abs((p.end - target_end).days) <= tolerance_days:
            return v
    return None


def _shift_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    for day in (d.day, 30, 29, 28):
        try:
            return date(year, month, day)
        except ValueError:
            continue
    return date(year, month, 28)


def _pct(cur: Optional[float], prev: Optional[float]) -> Optional[float]:
    if cur is None or prev is None or prev == 0:
        return None
    return round((cur - prev) / abs(prev) * 100.0, 2)


def surprise_features(facts: dict[str, dict[Period, float]]) -> dict[str, Any]:
    """YoY / QoQ deltas for the latest quarter in the instance."""
    rev_q = _quarters(facts.get("revenue", {}))
    if not rev_q:
        return {"ok": False, "reason": "no quarterly revenue fact"}
    cur_period = rev_q[0][0]
    end = cur_period.end
    qoq_end, yoy_end = _shift_months(end, -3), _shift_months(end, -12)

    def series(metric: str) -> list[tuple[Period, float]]:
        return _quarters(facts.get(metric, {}))

    def at(metric: str, when: date) -> Optional[float]:
        return _find(series(metric), when)

    def ebitda(when: date) -> Optional[float]:
        rep = at("ebitda", when)
        if rep is not None:
            return rep
        pbt, fin, dep = at("pbt", when), at("finance_costs", when), at("depreciation", when)
        if pbt is None or fin is None or dep is None:
            return None
        return pbt + fin + dep

    out: dict[str, Any] = {"ok": True, "quarter_end": end.isoformat()}
    for metric, getter in (("revenue", lambda w: at("revenue", w)), ("ebitda", ebitda),
                           ("pat", lambda w: at("pat", w))):
        cur, qoq, yoy = getter(end), getter(qoq_end), getter(yoy_end)
        out[f"{metric}"] = cur
        out[f"{metric}_qoq_pct"] = _pct(cur, qoq)
        out[f"{metric}_yoy_pct"] = _pct(cur, yoy)

    def margin(when: date) -> Optional[float]:
        r, p = at("revenue", when), at("pat", when)
        return p / r * 100.0 if r and p is not None else None

    m_now, m_yoy = margin(end), margin(yoy_end)
    out["pat_margin_pct"] = None if m_now is None else round(m_now, 2)
    out["pat_margin_change_pp_yoy"] = None if m_now is None or m_yoy is None else round(m_now - m_yoy, 2)
    out["loss_to_profit"] = bool(at("pat", yoy_end) is not None and at("pat", yoy_end) < 0
                                 and (out["pat"] or 0) > 0)
    return out


ALLOWED_HOST_SUFFIXES = ("nseindia.com", "bseindia.com")
MAX_BYTES = 20 * 1024 * 1024


def allowed_url(url: str) -> bool:
    """Exchange-hosted XBRL only: the endpoint fetches a URL the caller
    supplies, so it must not become a way to make the server fetch anything."""
    from urllib.parse import urlparse

    u = urlparse(url)
    host = (u.hostname or "").lower()
    return u.scheme == "https" and any(host == h or host.endswith("." + h) for h in ALLOWED_HOST_SUFFIXES)


async def fetch_and_parse(url: str, *, timeout: float = 10.0) -> dict[str, Any]:
    """Download an exchange XBRL instance and return its surprise features."""
    import httpx

    if not allowed_url(url):
        return {"ok": False, "reason": "only https URLs on nseindia.com / bseindia.com are fetched"}
    headers = {"User-Agent": "Mozilla/5.0 (compatible; market-proof research)",
               "Accept": "application/xml,text/xml,*/*"}
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client:
        resp = await client.get(url)
        resp.raise_for_status()
    if len(resp.content) > MAX_BYTES:
        return {"ok": False, "reason": "XBRL instance larger than 20 MB"}
    try:
        facts = parse_instance(resp.content)
    except ET.ParseError as e:
        return {"ok": False, "reason": f"not an XBRL instance: {e}"}
    return surprise_features(facts)
