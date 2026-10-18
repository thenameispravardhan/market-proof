"""News page tags: the listed stocks a headline names, and what kind of news it is.

Keyword rules over the scrip master, never a model: the News page is monitoring only, and nothing here reaches
the analyzer or the AI. Precision first, because a wrong tag sends the operator to the wrong chart:

- Two or more words must spell a company name from the scrip master without its "Ltd" ("State Bank of India"),
  its ticker cut along that name ("Sun Pharma" -> SUNPHARMA, "Tata Steel" -> TATASTEEL), or a media name in
  ALIASES ("Larsen & Toubro"). Each word starts with a capital: "life insurance premiums" names no company.
- One word alone counts only as an ALIASES name (RIL, Infosys) or as an ALL-CAPS ticker in a normally cased
  headline (TCS, ONGC). Every other ticker that is also a word stays untagged: "Oil prices" is not Oil India,
  "Supreme Court" is not Supreme Industries, "A Bright Idea" is not Vodafone Idea.
- A name several companies share goes to the largest only when it is 10x the next (data/mcap.csv). Otherwise it
  tags nothing, and still stops a shorter, wronger match: "Reliance Group" never tags RELIANCE.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional

from app.logging_config import get_logger
from app.services import mcap
from app.services.instrument_master import Instrument, get_master, search_filter

log = get_logger(__name__)

MAX_STOCKS = 4
_MAX_WORDS = 6                                    # the longest run of words tried as one name
_WORD = re.compile(r"[A-Za-z0-9&]+(?:['’][A-Za-z]+)?")
_POSSESSIVE = re.compile(r"['’]S$")
_NONKEY = re.compile(r"[^A-Z0-9&]")
# Fyers cuts names at 30 characters, so "Ltd" can end as a lone "L" ("RAILTEL CORPORATION OF INDIA L")
_LEGAL = re.compile(r"[\s.,-]*\b(?:LIMITED|LTD|PVT|PRIVATE|L|LT|LI|LIM|LIMI|LIMIT|LIMITE)\b[\s.]*$")
_TAIL = re.compile(r"\s*(?:\(INDIA\)|\bOF INDIA|\bINDIA)$")
_FIRM = re.compile(r"\s*\b(?:COMPANY|CORPORATION|CORP|CO)\.?$")
_SERIES = {"EQ", "BE", "BZ", "SM", "ST", "A", "B", "T", "X", "XT", "M", "MT", "Z", "ZP"}
_CONNECT = {"&", "OF", "THE", "FOR", "IN"}        # a name never ends on one; lower case is fine inside one
# Tickers that are, written in capitals, mostly something else: exchanges quoted as places ("BSE Sensex"),
# rating agencies quoted for someone else's rating, the cricket league, the US football league.
_NOT_CAPS = {"BSE", "MCX", "NSDL", "CRISIL", "ICRA", "IPL", "NFL", "CARE", "INDIA"}
# Everyday words that are also tickers, for the watchlist's lenient match ("oil" stays a keyword).
COMMON = {"apex", "bright", "clean", "crest", "crown", "data", "deep", "delta", "dollar", "earth", "fact", "focus",
          "global", "horizon", "idea", "natural", "oil", "pace", "par", "premier", "race", "rain", "route",
          "royal", "safe", "shree", "star", "super", "supreme", "take", "total", "unity", "vital", "wealth",
          "worth", "gold", "silver", "power", "energy", "bank", "india", "market", "auto", "metal", "steel"}
# Words a name's start needs more than: "Indian Energy" is no company until "Exchange" follows it.
_PLAIN = {w.upper() for w in COMMON} | {"INDIAN", "NATIONAL", "NEW", "FIRST", "UNITED", "GENERAL", "STATE",
                                        "CENTRAL", "ASIAN", "INTERNATIONAL", "BHARAT", "HINDUSTAN", "TAMIL", "UTTAR",
                                        "WEST", "SOUTH", "NORTH", "EAST", "WORLD", "DIGITAL", "SHRI", "SRI", "JAI"}
# Phrases that are a company's name with "of India" cut, or its start, but mean something else in a headline.
_GENERIC = ("Central Bank", "New India", "Life Insurance", "General Insurance", "Indian Railway", "Indian Railways",
            "Indian Bank Association", "State Government", "Union Government", "National Stock", "Dalal Street")
# Aliases that are everyday words, read in prose (a summary, an article) only past a sentence's first word:
# "Persistent rain is forecast" names no company.
_OPENER = re.compile(r"(^|[.!?][\"'’”]?\s+)(Persistent|Eternal|Mankind)\b")
# A broker call names the broker last: "Buy CEAT; target of Rs 4500: ICICI Securities" is CEAT's news, not ISEC's.
_BROKER = re.compile(r"^((?:buy|sell|hold|add|reduce|accumulate|neutral)\b[^:;]*[:;]\s*target[^:;]*|subscribe\b[^:;]*)"
                     r"[:;]\s*[^:;]+$", re.I)

# Media names that are neither the ticker nor the company name, each to its tickers in order of preference: the
# first one the scrip master lists wins (TATAMOTORS before the 2025 demerger, TMPV after). A name whose tickers are
# all missing still claims its words ("Reliance Capital" never falls back to RELIANCE). Empty = a group, not a stock.
# "Tata Motors" is left to the master: since the 2025 demerger it names the CV company (TMCV), and the seed's
# TATAMOTORS before the master loads.
ALIASES: dict[str, tuple[str, ...]] = {
    # conglomerates and their listed arms; the group itself is no stock
    "RIL": ("RELIANCE",), "Reliance": ("RELIANCE",), "Reliance Industries": ("RELIANCE",),
    "Reliance Power": ("RPOWER",), "Reliance Infra": ("RELINFRA",), "Reliance Infrastructure": ("RELINFRA",),
    "Reliance Capital": ("RELCAPITAL",), "Reliance Communications": ("RCOM",), "Reliance Home Finance": ("RHFL",),
    "Reliance Group": (), "Reliance ADAG": (), "Anil Ambani Group": (),
    "Jio Financial": ("JIOFIN",), "Jio Financial Services": ("JIOFIN",),
    "Jio": ("RELIANCE",), "Jio Platforms": ("RELIANCE",), "Reliance Jio": ("RELIANCE",),   # unlisted: its parent
    "Tata Group": (), "Tata Sons": (), "Adani Group": (), "Bajaj Group": (), "Birla Group": (), "Mahindra Group": (),
    "Tata Motors PV": ("TMPV",), "Tata Motors CV": ("TMCV",), "Tata Consumer": ("TATACONSUM",),
    "Tata Chemicals": ("TATACHEM",), "Tata Communications": ("TATACOMM",), "Tata Elxsi": ("TATAELXSI",),
    "Tata Technologies": ("TATATECH",), "Tata Capital": ("TATACAP",), "Tata Investment": ("TATAINVEST",), "Trent": ("TRENT",), "Voltas": ("VOLTAS",),
    "Titan": ("TITAN",), "Indian Hotels": ("INDHOTEL",), "IHCL": ("INDHOTEL",),
    "Adani Enterprises": ("ADANIENT",), "Adani Ports": ("ADANIPORTS",), "Adani Green": ("ADANIGREEN",),
    "Adani Power": ("ADANIPOWER",), "Adani Energy Solutions": ("ADANIENSOL",), "Adani Total Gas": ("ATGL",),
    "Adani Wilmar": ("AWL",), "AWL Agri": ("AWL",), "Ambuja Cements": ("AMBUJACEM",), "Ambuja": ("AMBUJACEM",),
    "Bajaj Finance": ("BAJFINANCE",), "Bajaj Finserv": ("BAJAJFINSV",), "Bajaj Auto": ("BAJAJ-AUTO",),
    "Bajaj Housing Finance": ("BAJAJHFL",), "Mahindra": ("M&M",), "Mahindra & Mahindra": ("M&M",), "M&M": ("M&M",),
    "M&M Financial": ("M&MFIN",), "Tech Mahindra": ("TECHM",), "Kotak": ("KOTAKBANK",), "Kotak Bank": ("KOTAKBANK",),
    "Kotak Mahindra Bank": ("KOTAKBANK",), "UltraTech": ("ULTRACEMCO",), "UltraTech Cement": ("ULTRACEMCO",),
    "Grasim": ("GRASIM",), "Hindalco": ("HINDALCO",), "Aditya Birla Capital": ("ABCAPITAL",),
    "Aditya Birla Fashion": ("ABFRL",), "Vodafone Idea": ("IDEA",), "Vi": (),
    # banks and financials
    "SBI": ("SBIN",), "State Bank": ("SBIN",), "SBI Card": ("SBICARD",), "SBI Cards": ("SBICARD",),
    "SBI Life": ("SBILIFE",), "HDFC": (), "HDFC Bank": ("HDFCBANK",), "HDFC Life": ("HDFCLIFE",),
    "HDFC AMC": ("HDFCAMC",), "ICICI": (), "ICICI Bank": ("ICICIBANK",), "ICICI Lombard": ("ICICIGI",),
    "ICICI Prudential": ("ICICIPRULI",), "ICICI Pru Life": ("ICICIPRULI",), "ICICI Securities": ("ISEC",),
    "Axis Bank": ("AXISBANK",), "IndusInd": ("INDUSINDBK",), "IndusInd Bank": ("INDUSINDBK",),
    "Yes Bank": ("YESBANK",), "IDFC First": ("IDFCFIRSTB",), "IDFC First Bank": ("IDFCFIRSTB",),
    "Bandhan Bank": ("BANDHANBNK",), "Federal Bank": ("FEDERALBNK",), "AU Small Finance": ("AUBANK",),
    "AU Bank": ("AUBANK",), "RBL Bank": ("RBLBANK",), "PNB": ("PNB",), "Punjab National Bank": ("PNB",),
    "Bank of Baroda": ("BANKBARODA",), "BoB": ("BANKBARODA",), "Canara Bank": ("CANBK",),
    "Union Bank": ("UNIONBANK",), "Bank of India": ("BANKINDIA",), "Indian Bank": ("INDIANB",),
    "IOB": ("IOB",), "UCO Bank": ("UCOBANK",), "Bank of Maharashtra": ("MAHABANK",), "IDBI Bank": ("IDBI",),
    "LIC": ("LICI",), "New India Assurance": ("NIACL",), "LIC Housing": ("LICHSGFIN",),
    "LIC Housing Finance": ("LICHSGFIN",),
    "Muthoot": ("MUTHOOTFIN",), "Muthoot Finance": ("MUTHOOTFIN",), "Manappuram": ("MANAPPURAM",),
    "Chola": ("CHOLAFIN",), "Cholamandalam": ("CHOLAFIN",), "Shriram Finance": ("SHRIRAMFIN",),
    "Paytm": ("PAYTM",), "One 97": ("PAYTM",), "PB Fintech": ("POLICYBZR",), "Policybazaar": ("POLICYBZR",),
    "REC": ("RECLTD",), "PFC": ("PFC",), "Power Finance": ("PFC",), "IRFC": ("IRFC",), "IREDA": ("IREDA",),
    "HUDCO": ("HUDCO",), "Angel One": ("ANGELONE",), "CDSL": ("CDSL",), "Motilal Oswal": ("MOTILALOFS",),
    # IT, telecom, consumer, health
    "Infosys": ("INFY",), "Infy": ("INFY",), "Wipro": ("WIPRO",), "HCL Tech": ("HCLTECH",),
    "HCLTech": ("HCLTECH",), "HCL Technologies": ("HCLTECH",), "LTIMindtree": ("LTIM",), "LTI Mindtree": ("LTIM",),
    "LTTS": ("LTTS",), "L&T Technology": ("LTTS",), "Mphasis": ("MPHASIS",), "Coforge": ("COFORGE",),
    "Persistent": ("PERSISTENT",), "KPIT": ("KPITTECH",), "Oracle Financial": ("OFSS",),
    "Airtel": ("BHARTIARTL",), "Bharti Airtel": ("BHARTIARTL",), "Indus Towers": ("INDUSTOWER",),
    "HUL": ("HINDUNILVR",), "Hindustan Unilever": ("HINDUNILVR",), "Nestle": ("NESTLEIND",),
    "Nestle India": ("NESTLEIND",), "Britannia": ("BRITANNIA",), "Dabur": ("DABUR",), "Marico": ("MARICO",),
    "Colgate": ("COLPAL",), "Godrej Consumer": ("GODREJCP",), "Godrej Properties": ("GODREJPROP",),
    "Asian Paints": ("ASIANPAINT",), "Berger Paints": ("BERGEPAINT",), "Pidilite": ("PIDILITIND",),
    "Havells": ("HAVELLS",), "Polycab": ("POLYCAB",), "Dixon": ("DIXON",), "Siemens": ("SIEMENS",),
    "United Spirits": ("UNITDSPR", "MCDOWELL-N"), "United Breweries": ("UBL",), "Jubilant FoodWorks": ("JUBLFOOD",),
    "DMart": ("DMART",), "Avenue Supermarts": ("DMART",), "Zomato": ("ETERNAL", "ZOMATO"), "Eternal": ("ETERNAL",),
    "Swiggy": ("SWIGGY",), "Nykaa": ("NYKAA",), "Naukri": ("NAUKRI",), "Info Edge": ("NAUKRI",),
    "Groww": ("GROWW",), "Meesho": ("MEESHO",), "Lenskart": ("LENSKART",), "PhysicsWallah": ("PWL",),
    "Ixigo": ("IXIGO",), "CEAT": ("CEATLTD",), "Urban Company": ("URBANCO",),
    "IndiGo": ("INDIGO",), "InterGlobe": ("INDIGO",), "Page Industries": ("PAGEIND",), "PVR": ("PVRINOX",),
    "PVR Inox": ("PVRINOX",), "Zee": ("ZEEL",), "Zee Entertainment": ("ZEEL",),
    "Sun Pharma": ("SUNPHARMA",), "Cipla": ("CIPLA",), "Lupin": ("LUPIN",), "Biocon": ("BIOCON",),
    "Dr Reddy's": ("DRREDDY",), "Dr. Reddy's": ("DRREDDY",), "Dr Reddy": ("DRREDDY",), "Divi's": ("DIVISLAB",),
    "Divis": ("DIVISLAB",), "Divi's Labs": ("DIVISLAB",), "Aurobindo": ("AUROPHARMA",), "Zydus": ("ZYDUSLIFE",),
    "Glenmark": ("GLENMARK",), "Torrent Pharma": ("TORNTPHARM",), "Alkem": ("ALKEM",), "Mankind": ("MANKIND",),
    "Apollo Hospitals": ("APOLLOHOSP",), "Max Healthcare": ("MAXHEALTH",), "Fortis": ("FORTIS",),
    # autos, industrials, energy, metals
    "Maruti": ("MARUTI",), "Maruti Suzuki": ("MARUTI",), "Hyundai Motor India": ("HYUNDAI",),
    "Hyundai India": ("HYUNDAI",), "Eicher": ("EICHERMOT",), "Eicher Motors": ("EICHERMOT",),
    "Hero MotoCorp": ("HEROMOTOCO",), "TVS Motor": ("TVSMOTOR",), "Ashok Leyland": ("ASHOKLEY",),
    "Ola Electric": ("OLAELEC",), "Bosch": ("BOSCHLTD",), "Motherson": ("MOTHERSON",), "Bharat Forge": ("BHARATFORG",),
    "Apollo Tyres": ("APOLLOTYRE",), "L&T": ("LT",), "Larsen & Toubro": ("LT",), "Larsen": ("LT",),
    "L&T Finance": ("LTF",), "BHEL": ("BHEL",), "Bharat Electronics": ("BEL",), "HAL": ("HAL",),
    "Hindustan Aeronautics": ("HAL",), "Bharat Dynamics": ("BDL",), "Mazagon Dock": ("MAZDOCK",),
    "Cochin Shipyard": ("COCHINSHIP",), "Garden Reach": ("GRSE",), "RVNL": ("RVNL",), "Rail Vikas": ("RVNL",),
    "IRCTC": ("IRCTC",), "IRCON": ("IRCON",), "Concor": ("CONCOR",), "Container Corporation": ("CONCOR",),
    "ONGC": ("ONGC",), "Oil India": ("OIL",), "Indian Oil": ("IOC",), "IOCL": ("IOC",), "BPCL": ("BPCL",),
    "HPCL": ("HINDPETRO",), "Hindustan Petroleum": ("HINDPETRO",), "GAIL": ("GAIL",), "Petronet": ("PETRONET",),
    "Coal India": ("COALINDIA",), "NTPC": ("NTPC",), "NTPC Green": ("NTPCGREEN",), "Power Grid": ("POWERGRID",),
    "Tata Power": ("TATAPOWER",), "JSW Energy": ("JSWENERGY",), "NHPC": ("NHPC",), "SJVN": ("SJVN",),
    "Suzlon": ("SUZLON",), "Waaree Energies": ("WAAREEENER",), "Premier Energies": ("PREMIERENE",),
    "Inox Wind": ("INOXWIND",), "Tata Steel": ("TATASTEEL",), "JSW Steel": ("JSWSTEEL",), "SAIL": ("SAIL",),
    "Jindal Steel": ("JINDALSTEL",), "Vedanta": ("VEDL",), "Hindustan Zinc": ("HINDZINC",),
    "Hindustan Copper": ("HINDCOPPER",), "Nalco": ("NATIONALUM",), "NALCO": ("NATIONALUM",), "NMDC": ("NMDC",),
    "NLC": ("NLCINDIA",), "NLC India": ("NLCINDIA",), "GMR Airports": ("GMRAIRPORT",), "DLF": ("DLF",),
}


def _key(word: str) -> str:
    """One word as the index spells it: capitals, no possessive, letters/digits/& only; "and" is "&"."""
    w = _NONKEY.sub("", _POSSESSIVE.sub("", word.upper()))
    return "&" if w == "AND" else w


def _words(text: str) -> list[str]:
    return [k for k in map(_key, _WORD.findall(text)) if k]


def _cuts(ticker: str, words: list[str]) -> bool:
    """True when the ticker is 2+ leading name words, each whole or cut short (2+ letters):
    SUNPHARMA = SUN + PHARMA(CEUTICAL), HCLTECH = HCL + TECH(NOLOGIES). GOODYEAR is one word, so "Good Year" never
    tags Goodyear."""
    def go(rest: str, i: int, pieces: int) -> bool:
        if not rest:
            return pieces >= 2
        if i >= len(words):
            return False
        w = words[i]
        return any(w[:n] == rest[:n] and go(rest[n:], i + 1, pieces + 1) for n in range(min(len(w), len(rest)), 1, -1))
    return go(ticker, 0, 0)


def _names(name: str) -> list[list[str]]:
    """A master name as the words a headline would use: without "Ltd" and a leading "The", then also without a
    trailing "(India)" / "of India", then without "Company" / "Corporation" ("Maruti Suzuki India Ltd" -> also
    "Maruti Suzuki"; "The New India Assurance Company Ltd" -> also "New India Assurance")."""
    full = name.upper().strip()
    for _ in range(2):                               # "XYZ PVT LTD"
        full = _LEGAL.sub("", full).strip()
    out = [re.sub(r"^THE\s+", "", full)]
    for tail in (_TAIL, _FIRM):
        short = tail.sub("", out[-1]).strip()
        if short and short != out[-1]:
            out.append(short)
    return [_words(v) for v in out]


@dataclass
class _Index:
    sig: tuple
    multi: dict[str, Optional[str]] = field(default_factory=dict)    # "TATASTEEL" -> symbol; None claims the words
    single: dict[str, Optional[str]] = field(default_factory=dict)   # one-word ALIASES
    caps: dict[str, str] = field(default_factory=dict)               # one-word tickers, matched when in ALL CAPS
    prefixes: set[str] = field(default_factory=set)                  # every start of a multi key: when to stop


_cache: dict[str, _Index] = {}


def _pick(shorts: set[str]) -> Optional[str]:
    """The one company a shared name means: the only one, or the largest when it is 10x the next."""
    if len(shorts) == 1:
        return next(iter(shorts))
    ranked = sorted(shorts, key=lambda s: mcap.lookup(s) or 0.0, reverse=True)
    top, nxt = mcap.lookup(ranked[0]) or 0.0, mcap.lookup(ranked[1]) or 0.0
    return ranked[0] if top > 0 and top >= 10 * nxt else None


def _rank(inst: Instrument) -> tuple[bool, bool]:
    return inst.exchange != "NSE", not inst.symbol.endswith("-EQ")


def _index() -> _Index:
    master = get_master()
    sig = (id(master), master.count())
    ix = _cache.get("ix")
    if ix is not None and ix.sig == sig:
        return ix
    for _ in range(3):                               # the daily master reload may swap the dicts mid-read: read again
        try:
            eq = [i for i in master.search("", limit=10**7, segments=["EQ"], where=search_filter(types=["EQ"]))
                  if i.exchange in ("NSE", "BSE") and i.short_name]
            break
        except RuntimeError:
            continue
    else:
        eq = []
    # Once the Fyers master is loaded every live listing carries its name: a seed scrip without one has been
    # renamed or delisted since the seed was written (TATAMOTORS -> TMPV, ZOMATO -> ETERNAL), so it is left out.
    if any(i.name for i in eq):
        eq = [i for i in eq if i.name]
    listings: dict[str, Instrument] = {}             # short name -> its best listing: NSE first, then the -EQ series
    for inst in eq:
        cur = listings.get(inst.short_name)
        if cur is None or _rank(inst) < _rank(cur):
            listings[inst.short_name] = inst
    ix = _Index(sig=sig)
    cand: dict[str, set[str]] = {}
    pref: dict[str, set[str]] = {}                   # the first 2+ words of a name: a cut name's start still matches
    for short, inst in listings.items():
        tk = "".join(_words(short))                  # BAJAJ-AUTO -> BAJAJAUTO, M&M stays
        if not tk:
            continue
        if re.fullmatch(r"[A-Z0-9&]+", short) and len(short) >= 2 and short not in _NOT_CAPS:
            ix.caps[tk] = inst.symbol
        variants = [w for w in _names(inst.name) if w]
        full = variants[0] if variants else []
        if len(full) == 1:                           # "Kamdhenu Ltd": the suffix makes a one-word name a company
            for sfx in ("LTD", "LIMITED"):
                cand.setdefault(full[0] + sfx, set()).add(short)
        named = len(full) >= 2 and not (full[-1] in _SERIES and "".join(full[:-1]) == tk)
        if named:
            for w in variants:
                if len(w) >= 2 and w[-1] not in _CONNECT:
                    cand.setdefault("".join(w), set()).add(short)
            if _cuts(tk, full):
                cand.setdefault(tk, set()).add(short)
            # "Himadri Speciality" for "HIMADRI SPECIALITY CHEM": a start led by a real word ("8.58% HUDCO", "P B M
            # Polytex", "Shri Krishna ..." are no company's start in a headline)
            if full[0].isalpha() and full[0] not in _PLAIN and all(len(w) > 1 for w in full[:2]):
                for n in range(2, len(full)):
                    if full[n - 1] not in _CONNECT:
                        pref.setdefault("".join(full[:n]), set()).add(short)
        else:                                        # the seed and old CSVs have no names: any 2+ words that spell it
            cand.setdefault(tk, set()).add(short)
    for k, shorts in [*pref.items(), *cand.items()]:  # a whole name wins over another name's start
        best = _pick(shorts)
        ix.multi[k] = listings[best].symbol if best else None
    for g in _GENERIC:
        ix.multi["".join(_words(g))] = None
    for alias, tickers in ALIASES.items():
        sym = next((listings[t].symbol for t in tickers if t in listings), None)
        ws = _words(alias)
        (ix.multi if len(ws) > 1 else ix.single)["".join(ws)] = sym
    ix.prefixes = {k[:n] for k in ix.multi for n in range(1, len(k) + 1)}
    _cache["ix"] = ix
    _tag.cache_clear()
    return ix


def _scan(text: str, ix: _Index, lenient: bool = False) -> list[str]:
    raw = _WORD.findall(text)
    keys = [_key(w) for w in raw]
    letters = [c for c in text if c.isalpha()]
    shouting = len(letters) > 12 and sum(c.isupper() for c in letters) > 0.7 * len(letters)
    cap = lambda w: w[0].isupper() or w[0].isdigit() or _key(w) in _CONNECT or w.lower() == "and"   # noqa: E731
    out: list[str] = []
    i = 0
    while i < len(raw):
        n, hit, k = 1, None, ""
        for j in range(i, min(i + _MAX_WORDS, len(raw))):      # the longest run of words that spells a name wins
            k += keys[j]
            if k not in ix.prefixes:
                break
            if j > i and k in ix.multi and (lenient or all(map(cap, raw[i:j + 1]))):
                n, hit = j - i + 1, ix.multi[k]
        if n == 1:
            w, k = raw[i], keys[i]
            if k in ix.single and (lenient or w[0].isupper()):
                hit = ix.single[k]
            elif k in ix.caps and ((w.isupper() and not shouting) or (lenient and w.lower() not in COMMON)):
                hit = ix.caps[k]
        if hit and hit not in out:
            out.append(hit)
        i += n
    return out


# ---------------------------------------------------------------- kinds

def _any(*alts: str) -> re.Pattern:
    return re.compile("|".join(alts), re.I)


_V = (r"(?:rises?|rose|jumps?|jumped|falls?|fell|drops?|dropped|declines?|declined|surges?|surged|soars?|soared|"
      r"grows?|grew|dips?|dipped|slumps?|slumped|doubles?|doubled|halves|plunges?|plunged|climbs?|climbed|zooms?|"
      r"increases?|increased|decreases?|decreased|up|down|widens?|narrows?|slips?|slipped)")
_AMT = r"(?:rs\.?\s?|₹\s?|\$\s?|inr\s?|usd\s?|us\$\s?)?\d"
# What kind of news a headline is: keyword rules on the title, in this order (a headline can be several).
KINDS: list[tuple[str, str, re.Pattern]] = [
    ("results", "Results", _any(
        r"\b(?:q[1-4]|h[12]|9m|quarterly|annual|full[- ]year)\b[^.;|]{0,40}?\b(?:results?|earnings|numbers|profit|loss|"
        r"revenues?|sales|ebitda|pat|income|margins?|performance|preview|update|show)\b",
        r"(?<!election )(?<!exam )(?<!poll )\bresults?\b", r"\b(?:earnings|net profit|net loss|pat|ebitda|topline)\b",
        rf"\b(?:profits?|loss(?:es)?|revenues?|sales|income)\s+{_V}\b",
        r"\b(?:beats?|miss(?:es)?|tops?)\s+(?:estimates?|expectations|forecasts?|street|consensus)\b")),
    ("orders", "Orders", _any(
        r"\b(?:bags?|bagged|bagging|wins?|won|winning|secures?|secured|securing|receives?|received|lands?|landed|"
        r"clinch(?:es|ed)?|grabs?)\b[^.;|]{0,50}?\b(?:orders?|contracts?|projects?|deals?|loa|tenders?|mandates?|"
        r"letters? of (?:award|intent))\b",
        r"\borders?\s+(?:book|inflows?|win|wins|worth|pipeline|backlog|intake)\b",
        r"\b(?:new|fresh|big|major|large|mega|multiple|repeat|export)\s+orders?\b")),
    ("deals", "Stake / M&A", _any(
        r"\b(?:acquires?|acquired|acquiring|acquisitions?|takeovers?|take over|buyouts?|mergers?|merges?|merged|"
        r"amalgamation|demergers?|demerged?|open offer|joint venture|jv|divest(?:s|ed|ment|iture)?|spin[- ]?off|"
        r"hive[- ]?off|stakes?|block deals?|bulk deals?|block trades?)\b",
        r"\bpromoters?\s+(?:sells?|sold|buys?|bought|pledges?|offloads?|trims?|raises?|hikes?)\b",
        r"\bsells?\s+(?:its\s+)?(?:business|unit|division|arm|subsidiary)\b")),
    ("actions", "Corp action", _any(
        r"\b(?:dividends?|buy[- ]?backs?|bonus (?:issue|shares?|ratio)|stock split|share split|splits?|sub-?division|"
        r"record date|ex[- ]?date|ex[- ]?dividend|rights issue|rights entitlement|face value)\b",
        r"\b\d+\s*:\s*\d+\s+bonus\b")),                  # not a shop's "exchange bonus" or a Diwali bonus
    ("ipo", "IPO", _any(
        r"\b(?:ipos?|listings?|debuts?|debuted|gmp|grey market|price band|subscribed|oversubscribed|subscription|"
        r"allotment status|listing gains?)\b", r"\banchor\s+(?:book|investors?|round)\b", r"\blists?\s+(?:at|on)\b")),
    ("funding", "Fund-raise", _any(
        r"\b(?:qips?|fpos?|ncds?|debentures?|preferential (?:issue|allotment|basis)|fund[- ]?rais(?:e|es|ing)|"
        r"offer for sale|ofs|warrants|capital raise|equity infusion|funding round)\b", r"\bbonds?\b(?!\s+yields?)",
        rf"\braise[sd]?\s+{_AMT}")),
    ("ratings", "Broker call", _any(
        r"\b(?:upgrades?|upgraded|downgrades?|downgraded|target price|price targets?|targets?|tp|buy call|sell call|"
        r"overweight|underweight|equal[- ]weight|outperform|underperform|initiates? coverage|top picks?|"
        r"stocks? to buy|stock picks?|brokerages?|moody'?s|fitch|crisil|icra|care ratings|india ratings)\b",
        r"\b(?:buy|sell|hold|add|reduce|accumulate|neutral)\s+rating\b",
        r"\bratings?\s+(?:upgrade|downgrade|action|outlook|reaffirmed)\b",
        r"\boutlook\s+(?:(?:revised\s+)?to\s+)?(?:positive|negative|stable|developing)\b")),   # not "inflation outlook cut"
    ("mgmt", "Management", _any(
        r"\b(?:ceo|cfo|coo|cto|cmd|md|chairman|chairperson|chairwoman|managing director|executive director|"
        r"whole[- ]time director|independent directors?|resigns?|resigned|resignation|steps? down|stepped down|quits?|"
        r"appoints?|appointed|appointment|re-?appoint(?:s|ed|ment)?|succession|elevated|ousted|sacked|top deck|"
        r"leadership change|key managerial)\b")),
    ("legal", "Regulatory", _any(
        r"\b(?:sebi|nclt|nclat|cci|ed|cbi|dri|sfio|income[- ]tax|show[- ]cause|penalty|penalties|penalised|penalized|"
        r"fined|probes?|probed|investigation|investigates?|raids?|raided|court|supreme court|high court|hc|sc|"
        r"tribunal|lawsuit|sues?|sued|litigation|arbitration|insolvency|bankruptcy|ibc|defaults?|defaulted|fraud|scam|"
        r"violations?|non-compliance|warning letter|usfda|fda|form 483|import alert|bans?|banned|suspension|suspended|"
        r"delisting|delisted|regulator|regulatory)\b",
        r"\b(?:gst|tax)\s+(?:notice|demand|evasion|raid)s?\b")),
    ("macro", "Macro", _any(
        r"\b(?:rbi|repo rate|reverse repo|crr|slr|mpc|monetary policy|inflation|cpi|wpi|gdp|iip|pmi|fiscal deficit|"
        r"trade deficit|current account|forex reserves?|rupee|dollar index|bond yields?|g-?secs?|treasury yields?|fed|"
        r"fomc|powell|ecb|boj|crude|brent|oil prices|opec|gold prices|silver prices|gst council|"
        r"gst collections?|tax collections?|monsoon|unemployment|jobs data|payrolls|recession|economy|"
        r"interest rates?)\b", r"\brate\s+(?:cut|hike)s?\b",
        # the Union Budget, not a film's or a household's
        r"\b(?:union|interim|annual|state|federal)\s+budget\b|\bbudget\s+(?:20\d\d|session|deficit|speech|estimates?)\b",
        # trade tariffs; a telecom or power "tariff hike" is a price, not macro
        r"(?<!raises )(?<!raised )(?<!hikes )(?<!hiked )(?<!mobile )(?<!telecom )(?<!power )(?<!electricity )"
        r"(?<!data )\btariffs?\b(?!\s+(?:hikes?|plans?|revisions?|cuts?))")),
    ("market", "Markets", _any(
        r"\b(?:sensex|nifty|nifty ?50|bank ?nifty|nifty ?bank|midcaps?|smallcaps?|small[- ]caps?|mid[- ]caps?|"
        r"stock market|share market|closing bell|opening bell|gift nifty|fiis?|diis?|fpis?|f&o expiry|expiry day|"
        r"india vix|vix|top gainers|top losers|record high|all[- ]time high|sell-?off|dalal street|d-street|m-cap|"
        r"bull run|bear market)\b",
        r"\bmarket\s+(?:wrap|close|closing|open|opening|today|live|outlook)\b", r"\b52[- ]week\s+(?:high|low)\b",
        rf"\bmarkets?\s+(?:{_V}|rall(?:y|ies|ied)|crash(?:es|ed)?|tumbles?|tumbled|recovers?|recovered|gains?)\b")),
]
KIND_LABELS = {k: label for k, label, _ in KINDS}


def kinds(title: str) -> list[str]:
    """What kind of news a headline is (KINDS ids, in KINDS order): "Q2 profit rises 10%" -> ["results"]."""
    return [k for k, _, rx in KINDS if rx.search(title)]


# ---------------------------------------------------------------- public

@lru_cache(maxsize=20000)
def _tag(title: str, summary: str, sig: tuple) -> tuple[tuple[str, ...], tuple[str, ...]]:
    ix = _cache["ix"]
    m = _BROKER.match(title)
    found = _scan(m[1] if m else title, ix)[:MAX_STOCKS] or _scan(_prose(summary), ix)[:2]
    return tuple(found), tuple(kinds(title))


def _prose(text: str) -> str:
    return _OPENER.sub(lambda m: m[1] + m[2].lower(), text)


def tag(title: str, summary: Optional[str] = None) -> dict[str, list[str]]:
    """{"tickers": Fyers symbols of the stocks the headline names, in order named (at most 4), "tags": its kinds}.
    The summary counts only when the title names no stock (it also names peers), and for at most two. Never raises:
    tags enrich the News page and must not take it down."""
    try:
        stocks, ks = _tag(title, summary or "", _index().sig)
    except Exception:  # noqa: BLE001
        log.exception("news_tags.failed", title=title[:120])
        return {"tickers": [], "tags": []}
    return {"tickers": list(stocks), "tags": list(ks)}


def stocks(title: str, summary: Optional[str] = None) -> list[str]:
    return tag(title, summary)["tickers"]


def find(title: str, summary: Optional[str] = None, text: Optional[str] = None) -> list[str]:
    """The chart button's look again: the headline's stocks, then its summary's, then the article lead's (the
    first 2,000 characters: later paragraphs name peers and brokers), at most six, by the same rules as tag()."""
    try:
        ix = _index()
        m = _BROKER.match(title)
        out: list[str] = []
        for part in (m[1] if m else title, _prose(summary or ""), _prose((text or "")[:2000])):
            out += [s for s in _scan(part, ix) if s not in out]
        return out[:6]
    except Exception:  # noqa: BLE001
        log.exception("news_tags.find_failed", title=title[:120])
        return []


def watch_stocks(words: list[str]) -> dict[str, list[str]]:
    """Watchlist words that name a stock, to its symbols: "Reliance" -> ["NSE:RELIANCE-EQ"], so a hit follows the
    stock whatever the headline calls it ("RIL shares jump"). Typed on purpose, so any case counts ("tcs"), except
    an everyday word ("oil" stays a keyword)."""
    ix = _index()
    return {w: hit for w in words if (hit := _scan(w, ix, lenient=True))}
