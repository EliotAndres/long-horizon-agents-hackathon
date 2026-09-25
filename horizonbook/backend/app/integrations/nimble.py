"""Nimble adapter: "WHAT DOES THE WEB SAY RIGHT NOW?" for any everyday task.

The agent describes what it is watching as a task, e.g.
    {"kind": "doctor_appointment", "query": "dermatologist appointment San Francisco next week"}
    {"kind": "grocery", "query": "oat milk 64oz delivery"}
    {"kind": "flight", "query": "one way flights SFO to LAX on 2026-09-26"}
and gets back normalized Observation dicts:
    {"item_id", "kind", "title", "price", "currency", "time", "available",
     "source_url", "observed_at", "attributes": {...kind-specific fields...}}

Pages are read by our own headless browser (browser.py: Playwright + Chromium), with Nimble's cloud
headless browser as the helper: POST https://sdk.nimbleway.com/v2/extract with render=true (Bearer
NIMBLE_API_KEY). No Nimble Search API.
  - extract(url):   Nimble renders the page (JS, proxies, optional stealth driver) -> markdown
  - read_page(url): local browser first, extract(url) when the site blocks it (HORIZON_BROWSER=nimble: Nimble only)
  - search(query):  Nimble renders a DuckDuckGo results page -> result links
  - observe(task):  search (or a known start URL) -> read the best page -> Observations
  - verify(item):   re-read the item's page -> is it still there, at what price?

Kind-specific knowledge (preferred sites, start URL, browser driver, row parser) lives in KINDS;
unknown kinds use the generic parser. Every live response is recorded to NIMBLE_RECORD_DIR for replay. NIMBLE_MODE: "live",
"replay" (never hit the network), or "auto" (default: live, fall back to the last recording on failure).

Stdlib only, so it drops into the backend without adding dependencies.

    python -m app.integrations.nimble search  "dermatologist appointment San Francisco"
    python -m app.integrations.nimble observe doctor_appointment "dermatologist San Francisco next week"
    python -m app.integrations.nimble observe grocery "oat milk 64oz"
    python -m app.integrations.nimble observe flight "one way flights SFO to LAX on 2026-09-26"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

HORIZONBOOK_DIR = Path(__file__).resolve().parents[3]
API_BASE = os.environ.get("NIMBLE_API_BASE", "https://sdk.nimbleway.com")
RECORD_DIR = Path(os.environ.get("NIMBLE_RECORD_DIR", HORIZONBOOK_DIR / "data" / "nimble_recordings"))

PRICE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d{1,5})(?:\.(\d{2}))?")
CLOCK = re.compile(r"\b(\d{1,2}:\d{2})\s*([AP]M)\b", re.IGNORECASE)
TIME_RANGE = re.compile(
    r"(\d{1,2}:\d{2})\s*([AP]M)\s*(?:[–—\-]|to|on\s+\w+,?\s+\w+\s+\d+\s+[–—\-])\s*(\d{1,2}:\d{2})\s*([AP]M)(\+\d)?",
    re.IGNORECASE,
)
HEADING = re.compile(r"^\s*\[?#{1,4}\s+([^\]\n]+?)(?:\]\(([^)\s]+)[^)]*\))?\s*$", re.MULTILINE)
# "Sep 25" / "Sept 30" but not "Sep 10, 2025" (a review date, not a slot)
MONTH_DAY = re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2})\b(?!,?\s*\d{4})")
UNAVAILABLE = re.compile(r"\b(sold out|out of stock|unavailable|no availability|fully booked)\b", re.IGNORECASE)

stats = {"calls": 0, "live_calls": 0, "replayed": 0, "errors": 0, "last_call_at": None}


def _load_dotenv() -> None:
    env = HORIZONBOOK_DIR / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


_load_dotenv()


class NimbleError(RuntimeError):
    pass


@dataclass
class NimbleResult:
    """Envelope for everything this module returns, so the UI can show what Nimble actually did."""
    kind: str  # search | extract | observe | verify
    mode: str  # live | replay
    ok: bool
    observed_at: str
    latency_ms: int
    task_kind: str | None = None
    request_id: str | None = None
    source_url: str | None = None
    sources: list[dict] = field(default_factory=list)
    items: list[dict] = field(default_factory=list)
    content: str | None = None
    verified: bool | None = None
    matched_item: dict | None = None
    summary: str = ""
    error: str | None = None

    def to_dict(self, include_content: bool = False) -> dict:
        d = asdict(self)
        if not include_content:
            d.pop("content")
        return d


# ---------------------------------------------------------------- transport


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ms(t0: float) -> int:
    return int((time.time() - t0) * 1000)


def _mode() -> str:
    return os.environ.get("NIMBLE_MODE", "auto").lower()


def _failed_mode() -> str:
    return "replay" if _mode() == "replay" else "live"


def _ssl_context() -> ssl.SSLContext:
    # python.org builds on macOS ship without root certificates.
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
        if not ctx.get_ca_certs() and Path("/etc/ssl/cert.pem").exists():
            ctx.load_verify_locations("/etc/ssl/cert.pem")
        return ctx


SSL_CONTEXT = _ssl_context()


def _recording_path(endpoint: str, body: dict) -> Path:
    key = hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()[:12]
    return RECORD_DIR / f"{endpoint.strip('/').replace('/', '_')}_{key}.json"


def _post(endpoint: str, body: dict, timeout: float, attempts: int = 2,
          reject: Callable[[dict], str | None] | None = None) -> tuple[dict, str]:
    """POST to Nimble, recording the response. Returns (json, mode).
    `reject(data)` returns a reason when a 200 response is unusable (e.g. a captcha page)."""
    mode = _mode()
    path = _recording_path(endpoint, body)
    stats["calls"] += 1
    stats["last_call_at"] = _now()

    if mode == "replay":
        return _replay(path), "replay"

    key = os.environ.get("NIMBLE_API_KEY")
    try:
        if not key:
            raise NimbleError("NIMBLE_API_KEY is not set")
        req = urllib.request.Request(
            API_BASE + endpoint,
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="POST",
        )
        # Protected sites (DataDome etc.) block a share of Nimble sessions at random; a fresh
        # attempt gets a fresh browser identity. Rejected pages (captchas) are retried and never recorded.
        for attempt in range(attempts):
            last = attempt == attempts - 1
            try:
                with urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT) as resp:
                    data = json.loads(resp.read())
            except TimeoutError:
                if last:
                    raise
                continue
            except urllib.error.HTTPError as e:
                if last or e.code < 500:
                    raise
                continue
            reason = reject(data) if reject else None
            if not reason:
                break
            if last:
                raise NimbleError(reason)
    except (urllib.error.URLError, TimeoutError, NimbleError, json.JSONDecodeError) as e:
        stats["errors"] += 1
        detail = e.read().decode(errors="replace")[:300] if isinstance(e, urllib.error.HTTPError) else str(e)
        if mode == "auto" and path.exists():
            return _replay(path), "replay"
        raise NimbleError(f"Nimble {endpoint} failed: {detail}") from e

    stats["live_calls"] += 1
    RECORD_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"endpoint": endpoint, "request": body, "recorded_at": _now(), "response": data}, indent=2))
    return data, "live"


def _replay(path: Path) -> dict:
    if not path.exists():
        raise NimbleError(f"no Nimble recording at {path.name}; run once with NIMBLE_MODE=live")
    stats["replayed"] += 1
    return json.loads(path.read_text())["response"]


# ---------------------------------------------------------------- primitives


SEARCH_PAGE = "https://html.duckduckgo.com/html/?"
SEARCH_RESULT = re.compile(r"^#{1,4}\s*\[([^\]]+)\]\((https?://[^)\s]+)\)", re.MULTILINE)


def search(query: str, domains: list[str] | None = None, max_results: int = 8, timeout: float | None = None) -> NimbleResult:
    """Web search done by Nimble's headless browser: it renders a DuckDuckGo results page and we read
    the result links off it. Results on `domains` are ranked first."""
    t0 = time.time()
    page = extract(SEARCH_PAGE + urllib.parse.urlencode({"q": query}), timeout=timeout)
    if not page.ok:
        page.kind, page.summary = "search", "Nimble browser search failed"
        return page

    sources, seen = [], set()
    for m in SEARCH_RESULT.finditer(page.content):
        url = _unwrap(m[2])
        if "duckduckgo.com" in urllib.parse.urlparse(url).netloc or url in seen:
            continue
        seen.add(url)
        after = page.content[m.end(): m.end() + 1200]
        snippet = next((_clean(l) for l in after.splitlines() if len(_clean(l)) > 40 and not l.lstrip().startswith("#")), "")
        sources.append({"title": _clean(m[1]), "url": url, "snippet": re.sub(r"\(https?://\S+", "", snippet)[:240]})
    if domains:
        sources.sort(key=lambda s: not any(_on_domain(s["url"], d) for d in domains))
    sources = sources[:max_results]

    prices = [p for p in (_price(s["title"] + " " + s["snippet"]) for s in sources) if p]
    summary = f"{len(sources)} {'live' if page.mode == 'live' else 'recorded'} web results" + (f", prices seen from ${min(prices):g}" if prices else "")
    return NimbleResult(
        "search", page.mode, bool(sources), page.observed_at, _ms(t0),
        request_id=page.request_id, source_url=sources[0]["url"] if sources else None,
        sources=sources, summary=summary, error=None if sources else "no results on the search page",
    )


# Per-site Nimble browser settings, found by testing each site live. Applied to any URL on that
# domain; explicit options win. "attempts" is ours (retries), everything else goes to Nimble.
SITE_PROFILES: dict[str, dict] = {
    # DataDome. Only vx10-pro + mobile + Google referrer got past it (1 in ~3 sessions); results
    # load ~10s after the page. Longer waits and wait_for_element made Nimble fail more often.
    "expedia.com": {"driver": "vx10-pro", "device": "mobile", "referrer_type": "google",
                    "browser_actions": [{"wait": {"duration": "10s", "required": False}}], "attempts": 3, "timeout": 120},
    "zocdoc.com": {"driver": "vx10", "attempts": 1, "timeout": 60},
    "walmart.com": {"driver": "vx10"},
}
CAPTCHA = re.compile(r"captcha|press (?:&|and) hold|verify you are (?:a )?human|are you a (?:human|robot)|bot or not|access denied"
                     r"|/_dms/interstitial/|we're having a problem on our end", re.IGNORECASE)  # Expedia's soft block


def site_profile(url: str) -> dict:
    return next((dict(p) for d, p in SITE_PROFILES.items() if _on_domain(url, d)), {})


def _captcha(data: dict) -> str | None:
    md = (data.get("data") or {}).get("markdown") or ""
    return "blocked: captcha / bot check page" if len(md) < 20000 and CAPTCHA.search(md) else None


def extract(url: str, render: bool = True, timeout: float | None = None, **options) -> NimbleResult:
    """Fetch a page through Nimble's headless browser and return its markdown as `content`.
    Extra Nimble options (driver, browser_actions, device, ...) pass straight through and override
    the site's profile."""
    t0 = time.time()
    profile = {**site_profile(url), **options}
    attempts = profile.pop("attempts", 2)
    timeout = timeout or profile.pop("timeout", 60)
    profile.pop("timeout", None)
    body = {"url": url, "render": render, "formats": ["markdown"], "country": "US", **profile}
    try:
        data, mode = _post("/v2/extract", body, timeout, attempts=attempts, reject=_captcha)
    except NimbleError as e:
        return NimbleResult("extract", _failed_mode(), False, _now(), _ms(t0), source_url=url, error=str(e), summary="Nimble extract failed")
    markdown = (data.get("data") or {}).get("markdown") or ""
    status = data.get("status", "unknown")
    return NimbleResult(
        "extract", mode, bool(markdown) and status == "success", _now(), _ms(t0),
        request_id=data.get("task_id"), source_url=url, content=markdown,
        summary=f"page fetched ({status}, {len(markdown)} chars)", error=None if markdown else f"status={status}",
    )


# ---------------------------------------------------------------- task kinds


@dataclass
class Kind:
    domains: list[str] | None = None  # preferred sites when picking a search result
    driver: str | None = None  # Nimble browser; "vx10" = stealth, needed for sites that block the default
    url: Callable[[str], str] | None = None  # query -> page to open directly (e.g. the site's own search), skipping web search
    browser_actions: list[dict] | None = None  # Nimble browser steps before capture (wait, auto_scroll, click, fill, ...)
    parse: Callable[[str, str, str, str], list[dict]] | None = None  # (markdown, kind, url, observed_at)


def parse_generic(markdown: str, kind: str, source_url: str, observed_at: str) -> list[dict]:
    """Any listing page. If the page has headings (provider/product cards), each heading and the text
    under it is one option; otherwise each text block is. An option needs a price, a clock time, an
    upcoming date, or an availability phrase."""
    text = re.sub(r"[\u202f\u00a0]", " ", markdown)
    # Split price display glued with a unit price: "$5278.2 ¢/fl oz" = $5.27, 8.2¢/fl oz
    text = re.sub(r"\$(\d{1,4})(\d{2})(\d{1,3}(?:\.\d+)?)\s?¢", r"$\1.\2 (\3¢", text)
    headings = list(HEADING.finditer(text))
    if len(headings) >= 2:
        entries = [(m[1], m[2], text[m.end(): headings[i + 1].start() if i + 1 < len(headings) else len(text)])
                   for i, m in enumerate(headings)]
    else:
        entries = []
        for block in re.split(r"\n\s*\n|\n(?=[*\-] )", text):
            lines = [_clean(l) for l in block.splitlines() if len(_clean(l)) > 3 and not PRICE.fullmatch(_clean(l))]
            if lines:
                entries.append((lines[0], None, block))

    items, seen = [], set()
    for title, link, body in entries:
        title = _clean(title)[:120]
        price, clock = _price(body), CLOCK.search(body)
        dates = list(dict.fromkeys(f"{m[1][:3]} {m[2]}" for m in MONTH_DAY.finditer(body)))
        unavailable = UNAVAILABLE.search(body)
        signals = price is not None or clock or dates
        # A heading linking to its own page is a card (doctor, product) even if its slots/price haven't rendered.
        card = link and not link.startswith("#")
        if not title or not (signals or unavailable or card):
            continue
        item_id = _slug(title)
        if item_id in seen:
            continue
        seen.add(item_id)
        extra = {"dates": dates} if dates else {}
        if link:
            extra["link"] = _unwrap(urllib.parse.urljoin(source_url, link))
        available = False if unavailable else (True if signals else None)  # None = unknown
        items.append(_item(item_id, kind, title, price, _hhmm(clock) if clock else (dates[0] if dates else None),
                           available, source_url, observed_at, **extra))
    return items


AIRLINE_CODES = {
    "united": "UA", "american": "AA", "delta": "DL", "alaska": "AS", "southwest": "WN",
    "jetblue": "B6", "spirit": "NK", "frontier": "F9", "hawaiian": "HA", "sun country": "SY",
    "breeze": "MX", "allegiant": "G4",
}
ROUTE = re.compile(r"\b([A-Z]{3})\s*(?:to|–|—|-)\s*([A-Z]{3})\b")
DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


EXPEDIA_ROW = re.compile(
    r"Select (?:and show fare information for )?(?P<airline>[^,\n]+?) flight, departing at (?P<dep>\d{1,2}:\d{2}[ap]m)"
    r"(?: from [^,\n]+)?, arriving at (?P<arr>\d{1,2}:\d{2}[ap]m)(?: in [^,\n]+)?, [Pp]riced at \$(?P<price>[\d,]+) (?P<fare>[^.,\n]+)"
    r"(?P<rest>[^\n]*)"
)
STOP_WORDS = {"nonstop": 0, "one stop": 1, "two stops": 2, "three stops": 3}


def parse_flights(markdown: str, kind: str, source_url: str, observed_at: str) -> list[dict]:
    if _on_domain(source_url, "expedia.com"):
        return parse_expedia_flights(markdown, kind, source_url, observed_at)
    return parse_google_flights(markdown, kind, source_url, observed_at)


def parse_expedia_flights(markdown: str, kind: str, source_url: str, observed_at: str) -> list[dict]:
    """Expedia results: each flight card starts with an accessible one-line summary
    ('Select United flight, departing at 2:40pm, arriving at 10:25am, priced at $1,519 Roundtrip per traveler. Nonstop. ...')."""
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(source_url).query)
    d1 = (qs.get("d1") or [""])[0]
    day = datetime.strptime(d1, "%Y-%m-%d") if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", d1) else datetime.now()
    text = re.sub(r"[\u202f\u00a0]", " ", markdown)
    rows = list(EXPEDIA_ROW.finditer(text))
    items, seen = [], set()
    for i, m in enumerate(rows):
        card = text[m.end(): rows[i + 1].start() if i + 1 < len(rows) else m.end() + 1500]
        rest = m["rest"].lower()
        dep = datetime.strptime(f"{day:%Y-%m-%d} {m['dep'].upper()}", "%Y-%m-%d %I:%M%p")
        arr = datetime.strptime(f"{day:%Y-%m-%d} {m['arr'].upper()}", "%Y-%m-%d %I:%M%p")
        later = re.search(r"arrives (\d) days? later", rest)
        arr += timedelta(days=int(later[1]) if later else (1 if arr < dep else 0))
        name = "Multiple airlines" if m["airline"] == "multipleAirlines" else m["airline"]
        logo = re.search(r"/airlines/[^)]*?/([A-Z0-9]{2})_sq\.svg", card)
        airline = "MULTI" if name == "Multiple airlines" else (logo[1] if logo else _airline_code(name))
        route = re.search(r"\b([A-Z]{3}) - ([A-Z]{3})\b", card)
        left = re.search(r"(\d+) left at this price", rest)
        item_id = f"{airline}-{dep:%H%M}-{arr:%H%M}"
        if item_id in seen:
            continue
        seen.add(item_id)
        items.append(_item(
            item_id, kind, f"{name} {route[1] + '→' + route[2] if route else ''} {dep:%H:%M}".replace("  ", " "),
            int(m["price"].replace(",", "")), dep.isoformat(timespec="minutes"), True, source_url, observed_at,
            airline=name, origin=route[1] if route else None, destination=route[2] if route else None,
            arrival=arr.isoformat(timespec="minutes"), stops=next((n for w, n in STOP_WORDS.items() if w in rest), None),
            fare=m["fare"].strip(), seats_left=int(left[1]) if left else None, cheapest="Cheapest" in text[max(0, m.start() - 12): m.start()],
        ))
    return items


def _airline_code(name: str) -> str:
    low = name.lower()
    return next((c for n, c in AIRLINE_CODES.items() if n in low), re.sub(r"[^A-Z]", "", name.title())[:2] or "XX")


def parse_google_flights(markdown: str, kind: str, source_url: str, observed_at: str) -> list[dict]:
    """Google Flights rows: 'H:MM AM – H:MM PM', airline, duration, stops, '$price'."""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(source_url).query).get("q", [""])[0]
    route, date = ROUTE.search(q), DATE.search(q)
    origin, destination = (route[1], route[2]) if route else (None, None)
    day = datetime.strptime(date[1], "%Y-%m-%d") if date else datetime.now()

    text = re.sub(r"[\u202f\u00a0]", " ", markdown)
    matches = list(TIME_RANGE.finditer(text))
    items, seen = [], set()
    for i, m in enumerate(matches):
        chunk = text[m.end(): matches[i + 1].start() if i + 1 < len(matches) else m.end() + 600]
        price = _price(chunk)
        if price is None:
            continue
        dep, arr = _at(day, m[1], m[2]), _at(day, m[3], m[4])
        if m[5] or arr < dep:
            arr += timedelta(days=int(m[5][1:]) if m[5] else 1)
        low = chunk.lower()
        hits = [(low.find(n), c) for n, c in AIRLINE_CODES.items() if n in low]
        airline = min(hits)[1] if hits else "XX"
        stops = re.search(r"\b(nonstop|non-stop|(\d)\s+stops?)\b", chunk, re.IGNORECASE)
        item_id = f"{airline}-{dep:%H%M}"
        if item_id in seen:
            continue
        seen.add(item_id)
        items.append(_item(
            item_id, kind, f"{airline} {origin or ''}→{destination or ''} {dep:%H:%M}", price,
            dep.isoformat(timespec="minutes"), True, source_url, observed_at,
            origin=origin, destination=destination, arrival=arr.isoformat(timespec="minutes"),
            stops=0 if not stops or stops[1].lower().startswith("non") else int(stops[2]),
        ))
    return items


def _google_flights_url(query: str) -> str:
    return "https://www.google.com/travel/flights?" + urllib.parse.urlencode({"q": query, "hl": "en", "curr": "USD"})


def expedia_flights_url(origin: str, destination: str, depart: str, return_date: str | None = None, adults: int = 1,
                        cabin: str = "economy") -> str:
    """Expedia flight search URL. Airports as IATA codes (SFO, CDG), dates as YYYY-MM-DD."""
    def leg(frm, to, day):
        d = datetime.strptime(day, "%Y-%m-%d")
        return f"from:{frm},to:{to},departure:{d.month}/{d.day}/{d.year}TANYT,fromType:AIRPORT,toType:AIRPORT"
    params = [("flight-type", "on"), ("mode", "search"), ("trip", "roundtrip" if return_date else "oneway"),
              ("leg1", leg(origin, destination, depart))]
    if return_date:
        params.append(("leg2", leg(destination, origin, return_date)))
    params += [("options", f"cabinclass:{cabin}"), ("passengers", f"adults:{adults},infantinlap:N"),
               ("d1", f"{int(depart[:4])}-{int(depart[5:7])}-{int(depart[8:10])}")]
    if return_date:
        params.append(("d2", f"{int(return_date[:4])}-{int(return_date[5:7])}-{int(return_date[8:10])}"))
    return "https://www.expedia.com/Flights-Search?" + urllib.parse.urlencode(params, safe=":,/")


def _expedia_from_query(query: str) -> str:
    """'SFO to CDG on 2026-10-09 returning 2026-10-16' -> Expedia URL (return date optional)."""
    route, dates = ROUTE.search(query), DATE.findall(query)
    if not route or not dates:
        raise ValueError(f"need 'AAA to BBB' and a YYYY-MM-DD date in {query!r}")
    return expedia_flights_url(route[1], route[2], dates[0], dates[1] if len(dates) > 1 else None)


def _walmart_search_url(query: str) -> str:
    return "https://www.walmart.com/search?" + urllib.parse.urlencode({"q": query})


# Slot calendars / prices on listing pages load after the first paint.
LOAD_LAZY_CONTENT = [
    {"wait": {"duration": "3s", "required": False}},
    {"auto_scroll": {"max_duration": 8000, "idle_timeout": 2000, "required": False}},
]

KINDS: dict[str, Kind] = {
    "flight": Kind(domains=["google.com", "kayak.com", "expedia.com", "united.com", "aa.com", "delta.com", "alaskaair.com"],
                   url=_google_flights_url, parse=parse_flights),
    "flight_expedia": Kind(url=_expedia_from_query, parse=parse_flights),
    "doctor_appointment": Kind(domains=["zocdoc.com", "solvhealth.com", "onemedical.com", "healthgrades.com"], driver="vx10",
                               browser_actions=LOAD_LAZY_CONTENT),
    "grocery": Kind(domains=["walmart.com", "instacart.com", "target.com", "amazon.com"], driver="vx10", url=_walmart_search_url),
    "restaurant": Kind(domains=["opentable.com", "resy.com", "yelp.com", "exploretock.com"], driver="vx10"),
    "haircut": Kind(domains=["booksy.com", "vagaro.com", "yelp.com", "fresha.com"], driver="vx10"),
}


# ---------------------------------------------------------------- observe / verify (what the agent calls)


def read_page(url: str, timeout: float | None = None, **options) -> NimbleResult:
    """Render one page. HORIZON_BROWSER=local (default): our own Playwright browser first, Nimble's
    stealth browser when it is blocked (see browser.py). HORIZON_BROWSER=nimble: Nimble only."""
    if os.environ.get("HORIZON_BROWSER", "local") == "local":
        try:
            from app.integrations import browser
        except ImportError:  # playwright not installed: Nimble renders everything
            return extract(url, timeout=timeout, **options)
        return browser.read(url, **options, **({"timeout": timeout} if timeout else {}))
    return extract(url, timeout=timeout, **options)


def observe(task: dict, timeout: float | None = None) -> NimbleResult:
    """What does the web offer for this task right now?

    task: {"kind": str, "query": str, "url"?: str, "domains"?: [str], "driver"?: str, "browser_actions"?: [dict]}
    Every step runs in Nimble's headless browser: pick the page (task url > kind's url builder > top
    result of a browser web search), render it and normalize the options. If no options can be
    parsed, the search results are still returned."""
    t0 = time.time()
    kind_name = task.get("kind", "generic")
    kind = KINDS.get(kind_name, Kind())
    query = task["query"]

    found = None
    url = task.get("url") or (kind.url(query) if kind.url else None)
    if not url:
        found = search(query, domains=task.get("domains") or kind.domains, timeout=timeout)
        if not found.ok:
            found.kind, found.task_kind = "observe", kind_name
            return found
        url = found.source_url

    options = {"driver": task.get("driver") or kind.driver, "browser_actions": task.get("browser_actions") or kind.browser_actions}
    options = {k: v for k, v in options.items() if v}
    page = read_page(url, timeout=timeout, **options)
    fallbacks = [s["url"] for s in found.sources[1:3]] if found else []
    while not page.ok and fallbacks:  # site blocked or timed out: try the next search result
        url = fallbacks.pop(0)
        page = read_page(url, timeout=timeout, **options)
    items = (kind.parse or parse_generic)(page.content or "", kind_name, url, page.observed_at) if page.ok else []
    sources = found.sources if found else []
    if found and page.ok and not kind.parse and len(items) < 3:
        # Few options parsed means the search hit is a single product/provider page: the page itself is the option.
        top = found.sources[0]
        head = page.content[:4000]
        if _price(head) is not None:
            items.insert(0, _item(_slug(top["title"]), kind_name, top["title"], _price(head), None,
                                  not UNAVAILABLE.search(head), url, page.observed_at, page="search_hit"))
    if not items and not sources:
        found = search(query, domains=task.get("domains") or kind.domains, timeout=timeout)
        sources = found.sources

    ok = bool(items or sources)
    mode = page.mode if page.ok else (found.mode if found else page.mode)
    fresh = "recorded" if mode == "replay" else "live"
    summary = f"{len(items)} {fresh} options for '{query}'" if items else (
        f"{len(sources)} {fresh} web results for '{query}' (no options parsed from page)" if sources else "Nimble saw nothing")
    return NimbleResult(
        "observe", mode, ok, page.observed_at, _ms(t0),
        task_kind=kind_name, request_id=page.request_id or (found.request_id if found else None),
        source_url=url, sources=sources, items=items, content=page.content, summary=summary,
        error=None if ok else (page.error or (found.error if found else None)),
    )


def verify(item: dict, query: str | None = None, time_tolerance_min: int = 20, timeout: float | None = None) -> NimbleResult:
    """Is this option still on the web, at what price? Call right before the agent decides/books.

    Re-reads the item's source page and looks for the same option (same item_id, or same title with
    a nearby time). Falls back to a live search so the demo always shows a genuine Nimble observation."""
    t0 = time.time()
    kind = item.get("kind", "generic")
    query = query or item.get("title", "")
    url = item.get("source_url") or (KINDS[kind].url(query) if kind in KINDS and KINDS[kind].url else None)

    live = observe({"kind": kind, "query": query, "url": url} if url else {"kind": kind, "query": query}, timeout=timeout)
    if live.items:
        match = _match(item, live.items, time_tolerance_min)
        if match:
            summary = f"live option verified: {match['title']}" + (f" ${match['price']:g}" if match.get("price") else "")
            if item.get("price") is not None and match.get("price") is not None and match["price"] != item["price"]:
                summary += f" (was ${item['price']:g})"
        else:
            summary = f"'{item.get('title')}' not found live; {len(live.items)} other options"
        return NimbleResult(
            "verify", live.mode, True, live.observed_at, _ms(t0), task_kind=kind,
            request_id=live.request_id, source_url=live.source_url, sources=live.sources, items=live.items,
            verified=match is not None and match.get("available") is True, matched_item=match, summary=summary,
        )

    return NimbleResult(
        "verify", live.mode, live.ok, live.observed_at, _ms(t0), task_kind=kind,
        request_id=live.request_id, source_url=live.source_url, sources=live.sources,
        verified=bool(live.sources), summary=("seen on the live web: " + live.summary) if live.sources else "Nimble could not verify",
        error=live.error,
    )


def _match(item: dict, candidates: list[dict], tolerance_min: int) -> dict | None:
    for c in candidates:
        if c["item_id"] == item.get("item_id"):
            return c
    want_time, want_title = item.get("time"), _slug(item.get("title", ""))
    best, best_gap = None, None
    for c in candidates:
        same_title = want_title and (want_title in c["item_id"] or _slug(c["title"]) in want_title)
        gap = _minutes_apart(want_time, c.get("time"))
        if gap is not None and gap > tolerance_min:
            continue
        if not same_title and gap is None:
            continue
        score = (0 if same_title else 1000) + (gap or 0)
        if best_gap is None or score < best_gap:
            best, best_gap = c, score
    return best


# ---------------------------------------------------------------- helpers


def _item(item_id, kind, title, price, when, available, source_url, observed_at, **attributes) -> dict:
    return {
        "item_id": item_id, "kind": kind, "title": title, "price": price, "currency": "USD",
        "time": when, "available": available, "source_url": source_url, "observed_at": observed_at,
        "attributes": attributes,
    }


def _price(text: str) -> float | None:
    m = PRICE.search(text or "")
    if not m:
        return None
    value = float(m[1].replace(",", "") + (f".{m[2]}" if m[2] else ""))
    return int(value) if value.is_integer() else value


def _at(day: datetime, hm: str, ampm: str) -> datetime:
    return datetime.strptime(f"{day:%Y-%m-%d} {hm} {ampm.upper()}", "%Y-%m-%d %I:%M %p")


def _hhmm(clock: re.Match) -> str:
    return datetime.strptime(f"{clock[1]} {clock[2].upper()}", "%I:%M %p").strftime("%H:%M")


def _minutes_apart(a: str | None, b: str | None) -> float | None:
    if not a or not b:
        return None
    ta, tb = a[-5:], b[-5:]
    try:
        da, db = datetime.strptime(ta, "%H:%M"), datetime.strptime(tb, "%H:%M")
    except ValueError:
        return None
    return abs((da - db).total_seconds()) / 60


def _clean(line: str) -> str:
    return re.sub(r"[#*_`>\[\]]|\(http\S*\)", "", line).strip(" -|")


def _unwrap(url: str) -> str:
    """Tracking/redirect links (DuckDuckGo uddg=, Walmart rd=, Google url=) -> the real destination."""
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    for key in ("uddg", "rd", "url"):
        target = qs.get(key, [""])[0]
        if target.startswith("http"):
            return target.split("?")[0] if key == "rd" else target
    return url


def _on_domain(url: str, domain: str) -> bool:
    host = urllib.parse.urlparse(url).netloc.lower()
    return host == domain or host.endswith("." + domain)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48]


# ---------------------------------------------------------------- CLI


def main() -> None:
    p = argparse.ArgumentParser(description="Nimble smoke test for HorizonBook")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search")
    s.add_argument("query")
    e = sub.add_parser("extract")
    e.add_argument("url")
    o = sub.add_parser("observe")
    o.add_argument("kind", help=f"one of {sorted(KINDS)} or anything else for the generic parser")
    o.add_argument("query")
    o.add_argument("--url")
    a = p.parse_args()

    if a.cmd == "search":
        r = search(a.query)
    elif a.cmd == "extract":
        r = extract(a.url)
    else:
        r = observe({"kind": a.kind, "query": a.query, **({"url": a.url} if a.url else {})})
    out = r.to_dict(include_content=a.cmd == "extract")
    out["items"] = out["items"][:8]
    print(json.dumps(out, indent=2)[:6000])
    print(f"\n[{r.mode}] {r.summary}  ({r.latency_ms} ms)  stats={stats}")


if __name__ == "__main__":
    main()
