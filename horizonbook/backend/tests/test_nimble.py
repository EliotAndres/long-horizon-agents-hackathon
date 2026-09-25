import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.integrations import nimble

GOOGLE_FLIGHTS_URL = nimble._google_flights_url("one way flights SFO to LAX on 2026-09-26")
GOOGLE_FLIGHTS_MD = """
6:20 AM – 8:12 AM
United
Nonstop
$211
6:40 AM – 8:05 AM
Alaska
Nonstop
$195
10:30 PM – 12:05 AM+1
Delta
Nonstop
$1,049
7:15 AM – 11:40 AM
Southwest
1 stop
$129
"""

DOCTOR_MD = """
## Dr. Maya Chen, MD — Dermatologist

Next available: Tue 9:30 AM

## Dr. Omar Ruiz, DO — Dermatologist

Fully booked this week

## Dr. Lena Park — Dermatologist

Next available: Wed 2:15 PM · $45 copay
"""

ZOCDOC_MD = """
All providers

[### Dr. Francis Hsiao, MD](https://www.zocdoc.com/doctor/francis-hsiao-md-617388)

Dermatologist

Great doctor - Sep 10, 2025 by Anderson H.

Fri

Sep 25

1

appt

Wed

Sep 30

1

appt

[### Danielle Davaros, PA-C](/doctor/danielle-davaros-pa-c-617359)

Physician Assistant (Dermatology)

Thu

Oct 1

2

appts
"""

GROCERY_MD = """
* Oatly Oat Milk Original 64 fl oz
  $5.49

* Califia Farms Oat Milk 48 fl oz
  $4.99 · Out of stock
"""

DDG_MD = """
## [Best Dermatologists in San Francisco, CA | WebMD](https://duckduckgo.com/l/?uddg=https%3A%2F%2Fdoctor.webmd.com%2Fsf&rut=1)

[Discover top Dermatologists in San Francisco - View 409 providers with 1,774 reviews.](https://duckduckgo.com/l/?uddg=https%3A%2F%2Fdoctor.webmd.com%2Fsf&rut=1)

## [Best Dermatologists Near Me in San Francisco, CA | Zocdoc](https://duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.zocdoc.com%2Fdermatologists%2Fsf&rut=2)

[Book online instantly. Appointments from $45 with verified patient reviews.](https://duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.zocdoc.com%2Fdermatologists%2Fsf&rut=2)
"""

WALMART_MD = """
## Results for "oatly"

[### Oatly Original Oatmilk, 64 fl oz](https://www.walmart.com/sp/track?pos=1&rd=https%3A%2F%2Fwww.walmart.com%2Fip%2FOatly%2F911697164%3FclassType%3DREGULAR)

Add

$5278.2 ¢/fl oz

[### Oatly Full Fat Oatmilk, 64 fl oz](https://www.walmart.com/ip/Oatly-Full-Fat/164081085)

$6488.3 ¢/fl oz

Out of stock
"""


EXPEDIA_URL = nimble.expedia_flights_url("SFO", "CDG", "2026-10-09", "2026-10-16")
EXPEDIA_MD = """
# Select your departure to Paris

*   Tue, Oct 6$862

*   Cheapest, Select Condor flight, departing at 4:30pm, arriving at 3:50pm, priced at $950 Roundtrip per traveler. One stop. Arrives 1 day later., Layover for 1 hour 50 minutes in Frankfurt.

    ![](https://images.trvl-media.com/media/content/expus/graphics/static_content/fusion/v0.1b/images/airlines/vector/s/DE_sq.svg)

    SFO - CDG

    $950

*   Select and show fare information for Air France flight, departing at 8:10pm from San Francisco, arriving at 3:50pm in Paris, Priced at $1,602 Roundtrip per traveler, 4 left at this price. Arrives 1 day later. 10 hours 40 minutes total travel time, Nonstop.

    ![](https://images.trvl-media.com/media/content/expus/graphics/static_content/fusion/v0.1b/images/airlines/vector/s/AF_sq.svg)

    SFO - CDG

*   [Select Bundle & Save deal for United flight departing at 2:40pm and arriving at 10:25am, Nonstop.](https://www.expedia.com/flexibleshopping)

*   Select multipleAirlines flight, departing at 2:40pm, arriving at 1:50pm, priced at $1,550 Roundtrip per traveler. One stop. Arrives 1 day later.

    SFO - CDG
"""


def fake_urlopen(*responses):
    it = iter(responses)
    calls = []

    def fake(req, timeout, **kw):
        calls.append({"endpoint": req.full_url, **json.loads(req.data)})
        return mock.MagicMock(__enter__=lambda s: io.BytesIO(json.dumps(next(it)).encode()), __exit__=lambda *a: None)
    fake.calls = calls
    return fake


def page(md):
    return {"task_id": "t-1", "status": "success", "data": {"markdown": md}}


SEARCH_PAGE = page(DDG_MD)


class Parsers(unittest.TestCase):
    def test_flights(self):
        items = nimble.parse_flights(GOOGLE_FLIGHTS_MD, "flight", GOOGLE_FLIGHTS_URL, "now")
        self.assertEqual([i["item_id"] for i in items], ["UA-0620", "AS-0640", "DL-2230", "WN-0715"])
        ua, dl = items[0], items[2]
        self.assertEqual((ua["time"], ua["price"], ua["attributes"]["origin"], ua["attributes"]["stops"]), ("2026-09-26T06:20", 211, "SFO", 0))
        self.assertEqual((dl["attributes"]["arrival"], dl["price"]), ("2026-09-27T00:05", 1049))

    def test_doctor_slots(self):
        items = nimble.parse_generic(DOCTOR_MD, "doctor_appointment", "u", "now")
        self.assertEqual([(i["title"].split(",")[0].split(" —")[0], i["time"], i["price"], i["available"]) for i in items],
                         [("Dr. Maya Chen", "09:30", None, True), ("Dr. Omar Ruiz", None, None, False), ("Dr. Lena Park", "14:15", 45, True)])

    def test_zocdoc_cards(self):
        md = ZOCDOC_MD
        items = nimble.parse_generic(md, "doctor_appointment", "https://www.zocdoc.com/dermatologists/sf", "now")
        self.assertEqual([i["title"] for i in items], ["Dr. Francis Hsiao, MD", "Danielle Davaros, PA-C"])
        hsiao = items[0]
        self.assertEqual((hsiao["time"], hsiao["attributes"]["dates"]), ("Sep 25", ["Sep 25", "Sep 30"]))
        self.assertEqual(hsiao["attributes"]["link"], "https://www.zocdoc.com/doctor/francis-hsiao-md-617388")

    def test_expedia_flights(self):
        items = nimble.parse_flights(EXPEDIA_MD, "flight", EXPEDIA_URL, "now")
        self.assertEqual([i["item_id"] for i in items], ["DE-1630-1550", "AF-2010-1550", "MULTI-1440-1350"])
        condor, af = items[0], items[1]
        self.assertEqual((condor["price"], condor["time"], condor["attributes"]["arrival"], condor["attributes"]["stops"],
                          condor["attributes"]["cheapest"]), (950, "2026-10-09T16:30", "2026-10-10T15:50", 1, True))
        self.assertEqual((af["price"], af["attributes"]["stops"], af["attributes"]["seats_left"], af["attributes"]["origin"]),
                         (1602, 0, 4, "SFO"))
        self.assertEqual(items[2]["attributes"]["airline"], "Multiple airlines")

    def test_expedia_url(self):
        self.assertIn("leg1=from:SFO,to:CDG,departure:10/9/2026TANYT", EXPEDIA_URL)
        self.assertIn("trip=roundtrip", EXPEDIA_URL)
        self.assertIn("d2=2026-10-16", EXPEDIA_URL)
        self.assertEqual(nimble._expedia_from_query("one way SFO to LAX on 2026-10-02"),
                         nimble.expedia_flights_url("SFO", "LAX", "2026-10-02"))

    def test_card_without_rendered_slots_has_unknown_availability(self):
        md = "[### Dr. Kumar Nadhan, MD](https://www.zocdoc.com/doctor/kumar-nadhan-md-617409)\n\nDermatologist\n\n" \
             "[### Melanie Choi, PA](https://www.zocdoc.com/doctor/melanie-choi-pa-661925)\n\nFri\n\nSep 25\n\n1\n\nappt\n\n" \
             "## How can I find a dermatologist?\n\nMost open 9:00 AM"
        items = nimble.parse_generic(md, "doctor_appointment", "https://www.zocdoc.com/x", "now")
        self.assertEqual([(i["title"], i["available"]) for i in items],
                         [("Dr. Kumar Nadhan, MD", None), ("Melanie Choi, PA", True), ("How can I find a dermatologist?", True)])

    def test_walmart_search_page(self):
        items = nimble.parse_generic(WALMART_MD, "grocery", "https://www.walmart.com/search?q=oatly", "now")
        self.assertEqual([(i["title"], i["price"], i["available"]) for i in items],
                         [("Oatly Original Oatmilk, 64 fl oz", 5.27, True), ("Oatly Full Fat Oatmilk, 64 fl oz", 6.48, False)])
        self.assertEqual(items[0]["attributes"]["link"], "https://www.walmart.com/ip/Oatly/911697164")

    def test_grocery(self):
        items = nimble.parse_generic(GROCERY_MD, "grocery", "u", "now")
        self.assertEqual([(i["price"], i["available"]) for i in items], [(5.49, True), (4.99, False)])
        self.assertTrue(items[0]["title"].startswith("Oatly"))


class Calls(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            mock.patch.object(nimble, "RECORD_DIR", Path(self.tmp.name)),
            mock.patch.dict(os.environ, {"NIMBLE_API_KEY": "k", "NIMBLE_MODE": "auto", "HORIZON_BROWSER": "nimble"}),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_search_uses_headless_browser_not_search_api(self):
        fake = fake_urlopen(SEARCH_PAGE)
        with mock.patch("urllib.request.urlopen", fake):
            r = nimble.search("dermatologist San Francisco", domains=["zocdoc.com"])
        call = fake.calls[0]
        self.assertTrue(call["endpoint"].endswith("/v2/extract"))
        self.assertTrue(call["url"].startswith("https://html.duckduckgo.com/html/?q=dermatologist"))
        self.assertTrue(call["render"])
        self.assertEqual([s["url"] for s in r.sources], ["https://www.zocdoc.com/dermatologists/sf", "https://doctor.webmd.com/sf"])
        self.assertEqual(r.sources[0]["snippet"][:12], "Book online ")
        self.assertIn("from $45", r.summary)

    def test_search_live_then_replay(self):
        with mock.patch("urllib.request.urlopen", fake_urlopen(SEARCH_PAGE)):
            r = nimble.search("dermatologist San Francisco")
        self.assertEqual((r.mode, r.ok, r.request_id), ("live", True, "t-1"))
        with mock.patch("urllib.request.urlopen", side_effect=nimble.urllib.error.URLError("down")):
            r2 = nimble.search("dermatologist San Francisco")
        self.assertEqual((r2.mode, r2.ok), ("replay", True))

    def test_failure_without_recording_is_reported(self):
        with mock.patch("urllib.request.urlopen", side_effect=nimble.urllib.error.URLError("down")):
            r = nimble.search("x")
        self.assertFalse(r.ok)
        self.assertIn("down", r.error)

    def test_retries_once_on_timeout(self):
        fake = fake_urlopen(SEARCH_PAGE)
        calls = iter([TimeoutError("slow"), None])

        def flaky(req, timeout, **kw):
            err = next(calls)
            if err:
                raise err
            return fake(req, timeout)
        with mock.patch("urllib.request.urlopen", flaky):
            self.assertTrue(nimble.search("x").ok)

    def test_expedia_gets_its_site_profile(self):
        fake = fake_urlopen(page(EXPEDIA_MD))
        with mock.patch("urllib.request.urlopen", fake):
            r = nimble.observe({"kind": "flight", "query": "SFO to CDG", "url": EXPEDIA_URL})
        call = fake.calls[0]
        self.assertEqual((call["driver"], call["device"], call["referrer_type"]), ("vx10-pro", "mobile", "google"))
        self.assertNotIn("attempts", call)
        self.assertEqual(len(r.items), 3)

    def test_captcha_page_is_retried_and_never_recorded(self):
        captcha = page("Please verify you are a human. Press & Hold")
        with mock.patch("urllib.request.urlopen", fake_urlopen(captcha, captcha, captcha)):
            r = nimble.extract(EXPEDIA_URL)
        self.assertFalse(r.ok)
        self.assertIn("captcha", r.error)
        self.assertEqual(list(Path(self.tmp.name).glob("*.json")), [])

        with mock.patch("urllib.request.urlopen", fake_urlopen(captcha, page(EXPEDIA_MD))):
            r = nimble.extract(EXPEDIA_URL)
        self.assertEqual((r.ok, r.mode), (True, "live"))

    def test_server_error_is_retried(self):
        err = nimble.urllib.error.HTTPError("u", 500, "boom", {}, io.BytesIO(b"can't download"))
        responses = iter([err, None])
        fake = fake_urlopen(page(EXPEDIA_MD))

        def flaky(req, timeout, **kw):
            e = next(responses)
            if e:
                raise e
            return fake(req, timeout)
        with mock.patch("urllib.request.urlopen", flaky):
            self.assertTrue(nimble.extract(EXPEDIA_URL).ok)

    def test_observe_unknown_kind_searches_then_renders_top_result(self):
        fake = fake_urlopen(SEARCH_PAGE, page(GROCERY_MD))
        with mock.patch("urllib.request.urlopen", fake):
            r = nimble.observe({"kind": "pharmacy_refill", "query": "oat milk"})
        self.assertEqual(fake.calls[1]["url"], "https://doctor.webmd.com/sf")
        self.assertNotIn("driver", fake.calls[1])
        self.assertEqual([i["title"][:12] for i in r.items], ["Best Dermato", "Oatly Oat Mi", "Califia Farm"])
        self.assertEqual(r.items[0]["attributes"], {"page": "search_hit"})

    def test_observe_known_kind_prefers_its_sites_and_driver(self):
        fake = fake_urlopen(SEARCH_PAGE, page("nothing useful"))
        with mock.patch("urllib.request.urlopen", fake):
            r = nimble.observe({"kind": "doctor_appointment", "query": "dermatologist"})
        self.assertEqual((fake.calls[1]["url"], fake.calls[1]["driver"]), ("https://www.zocdoc.com/dermatologists/sf", "vx10"))
        self.assertEqual([next(iter(a)) for a in fake.calls[1]["browser_actions"]], ["wait", "auto_scroll"])
        self.assertNotIn("browser_actions", fake.calls[0])
        self.assertTrue(r.ok)
        self.assertEqual(r.items, [])
        self.assertEqual(len(r.sources), 2)

    def test_observe_grocery_opens_walmart_search_directly(self):
        fake = fake_urlopen(page(WALMART_MD))
        with mock.patch("urllib.request.urlopen", fake):
            r = nimble.observe({"kind": "grocery", "query": "oatly oat milk"})
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0]["url"], "https://www.walmart.com/search?q=oatly+oat+milk")
        self.assertEqual(r.items[0]["price"], 5.27)

    def test_verify_flight_price_change(self):
        with mock.patch("urllib.request.urlopen", fake_urlopen(page(GOOGLE_FLIGHTS_MD))):
            r = nimble.verify({"item_id": "UA-0620", "kind": "flight", "title": "UA SFO→LAX 06:20", "price": 169,
                               "time": "2026-09-26T06:20", "source_url": GOOGLE_FLIGHTS_URL})
        self.assertTrue(r.verified)
        self.assertIn("$211 (was $169)", r.summary)

    def test_verify_sold_out_item_is_not_verified(self):
        with mock.patch("urllib.request.urlopen", fake_urlopen(page(GROCERY_MD))):
            r = nimble.verify({"item_id": "x", "kind": "grocery", "title": "Califia Farms Oat Milk 48 fl oz", "source_url": "https://i/c"})
        self.assertEqual(r.matched_item["price"], 4.99)
        self.assertFalse(r.verified)

    def test_verify_falls_back_to_browser_search(self):
        fake = fake_urlopen(page("nothing readable here"), SEARCH_PAGE)
        with mock.patch("urllib.request.urlopen", fake):
            r = nimble.verify({"item_id": "x", "kind": "doctor_appointment", "title": "Dr. Chen", "source_url": "https://z/c"})
        self.assertTrue(r.verified)
        self.assertTrue(r.summary.startswith("seen on the live web"))
        self.assertTrue(all(c["endpoint"].endswith("/v2/extract") for c in fake.calls))


class FakeBrowser:
    """Stands in for browser.Browser: serves one snapshot, no Chromium."""

    def __init__(self, markdown, blocked=None):
        self.snap = {"url": None, "title": "t", "status": 200, "markdown": markdown, "blocked": blocked}

    def __call__(self, headless=True):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def goto(self, url):
        self.snap["url"] = url

    def run_actions(self, actions):
        pass

    def snapshot(self):
        return self.snap


class LocalFirst(unittest.TestCase):
    tearDown = Calls.tearDown

    def setUp(self):
        from app.integrations import browser
        self.browser = browser
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            mock.patch.object(nimble, "RECORD_DIR", Path(self.tmp.name)),
            mock.patch.dict(os.environ, {"NIMBLE_API_KEY": "k", "NIMBLE_MODE": "auto", "HORIZON_BROWSER": "local"}),
        ]
        for p in self.patches:
            p.start()

    def test_local_browser_reads_page_without_nimble(self):
        fake = fake_urlopen()
        with mock.patch.object(self.browser, "Browser", FakeBrowser(WALMART_MD)), mock.patch("urllib.request.urlopen", fake):
            r = nimble.observe({"kind": "grocery", "query": "oatly oat milk"})
        self.assertEqual(fake.calls, [])
        self.assertEqual((r.mode, r.items[0]["price"]), ("local", 5.27))
        self.assertIn("live options", r.summary)

    def test_blocked_local_browser_falls_back_to_nimble(self):
        fake = fake_urlopen(page(EXPEDIA_MD))
        with mock.patch.object(self.browser, "Browser", FakeBrowser("Press & Hold", blocked="captcha frame on page")), \
                mock.patch("urllib.request.urlopen", fake):
            r = self.browser.read(EXPEDIA_URL)
        self.assertEqual((r.mode, r.ok), ("live", True))
        self.assertEqual(fake.calls[0]["driver"], "vx10-pro")
        self.assertTrue(r.summary.startswith("local browser blocked (captcha frame on page) -> Nimble"))

    def test_replay_mode_skips_local_browser(self):
        with mock.patch.dict(os.environ, {"NIMBLE_MODE": "replay"}), \
                mock.patch.object(self.browser, "Browser", side_effect=AssertionError("launched")):
            r = self.browser.read("https://www.walmart.com/search?q=x")
        self.assertIn("replay mode -> Nimble", r.summary)

    def test_proxy_from_nimble_pipeline_url(self):
        with mock.patch.dict(os.environ, {"NIMBLE_PROXY_URL": "http://account-a-pipeline-p-country-us:s%40cret@ip.nimbleway.com:7000"}):
            self.assertEqual(self.browser._proxy(), {"server": "http://ip.nimbleway.com:7000",
                                                     "username": "account-a-pipeline-p-country-us", "password": "s@cret"})


if __name__ == "__main__":
    unittest.main()
