"""Local headless browser we control step by step (Playwright + Chromium over the Chrome DevTools
Protocol), with Nimble as the helper:

  - blocked?  If the local browser hits a captcha / bot wall (DataDome, Cloudflare, ...), the same URL
              is handed to Nimble's cloud stealth browser (nimble.extract).
  - proxy     Set NIMBLE_PROXY_URL=http://USER:PASS@ip.nimbleway.com:7000 (Nimble dashboard > Pipelines)
              to route the local browser through Nimble's residential IPs.
  - discover  nimble.search() still finds which page to open for a task.

    with Browser() as b:                         # interactive control, for agents
        b.goto("https://www.walmart.com/search?q=oat+milk")
        b.fill("input[type=search]", "bananas"); b.press("Enter")
        page = b.snapshot()                       # {"url", "title", "markdown", "blocked"}

    read(url)                                    # one-shot: local first, Nimble if blocked -> NimbleResult

    python -m app.integrations.browser read "https://www.walmart.com/search?q=bananas"
    python -m app.integrations.browser read "https://www.expedia.com/Flights-Search?..." --show
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.parse
from pathlib import Path

from app.integrations import nimble

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/153.0.0.0 Safari/537.36")
BLOCK_TITLES = re.compile(r"just a moment|access denied|attention required|pardon our interruption|are you a robot|bot or not",
                          re.IGNORECASE)
BLOCK_FRAMES = ("captcha-delivery.com", "hcaptcha.com", "recaptcha", "challenges.cloudflare.com", "px-captcha")

# DOM -> markdown in the page, shaped like Nimble's output so the same parsers work on both.
TO_MARKDOWN = r"""
() => {
  const SKIP = new Set(["SCRIPT","STYLE","NOSCRIPT","SVG","TEMPLATE","IFRAME","HEAD"]);
  const BLOCK = new Set(["P","DIV","SECTION","ARTICLE","MAIN","HEADER","FOOTER","NAV","ASIDE","UL","OL","TABLE","TR","FORM","FIELDSET"]);
  const walk = (n) => {
    if (n.nodeType === 3) return n.textContent.replace(/\s+/g, " ");
    if (n.nodeType !== 1 || SKIP.has(n.tagName)) return "";
    if (n.checkVisibility && !n.checkVisibility()) return "";
    // aria-hidden parts are visual duplicates (e.g. Walmart's "$ 5 27" digits next to "current price $5.27")
    if (n.getAttribute("aria-hidden") === "true") return "";
    // superscript-cents prices drawn as three spans: "$" "5" "27" -> "$5.27" (Walmart, Amazon, Target)
    const kids = [...n.children].map(c => c.textContent.trim());
    if (kids.length === 3 && n.children.length === n.childNodes.length && /^[$€£]$/.test(kids[0])
        && /^\d{1,3}(,\d{3})*$/.test(kids[1]) && /^\d{2}$/.test(kids[2])) return " " + kids[0] + kids[1] + "." + kids[2] + " ";
    // items of a flex row are visually separate: keep them apart in the text
    const sep = n.children.length > 1 && getComputedStyle(n).display.includes("flex") ? " " : "";
    let inner = "";
    for (const c of n.childNodes) inner += walk(c) + sep;
    const t = n.tagName;
    const h = /^H([1-6])$/.exec(t);
    if (h) return "\n\n" + "#".repeat(+h[1]) + " " + inner.trim() + "\n\n";
    if (t === "A" && n.href && !n.href.startsWith("javascript:")) {
      const label = inner.trim() || n.getAttribute("aria-label") || "";
      return label ? "[" + label.replace(/\n\n+/g, " ").trim() + "](" + n.href + ")" : "";
    }
    if (t === "BUTTON" || n.getAttribute("role") === "button") {
      const aria = n.getAttribute("aria-label");
      return "\n" + (aria && aria.length > inner.trim().length ? aria : inner.trim()) + "\n";
    }
    if (t === "LI") return "\n*   " + inner.trim() + "\n";
    if (t === "BR") return "\n";
    if (BLOCK.has(t) || t === "TD" || t === "TH") return "\n" + inner + "\n";
    return inner;
  };
  return walk(document.body).replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
}
"""


def _proxy() -> dict | None:
    url = os.environ.get("NIMBLE_PROXY_URL")
    if not url:
        return None
    p = urllib.parse.urlparse(url)
    return {"server": f"{p.scheme}://{p.hostname}:{p.port}", "username": urllib.parse.unquote(p.username or ""),
            "password": urllib.parse.unquote(p.password or "")}


class Browser:
    """One local Chromium tab we drive step by step."""

    def __init__(self, headless: bool = True, proxy: dict | None = None):
        self.headless, self.proxy = headless, proxy if proxy is not None else _proxy()
        self._pw = self._browser = self.context = self.page = None
        self.status: int | None = None

    def __enter__(self) -> "Browser":
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        # channel="chromium": full Chromium in new headless mode (looks like real Chrome to bot checks,
        # unlike the separate headless-shell build).
        self._browser = self._pw.chromium.launch(
            channel="chromium", headless=self.headless, proxy=self.proxy,
            args=["--disable-blink-features=AutomationControlled"])
        ctx = self._browser.new_context(user_agent=UA, locale="en-US", timezone_id="America/Los_Angeles",
                                        viewport={"width": 1366, "height": 900})
        ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        self.context = ctx
        self.page = ctx.new_page()
        return self

    def __exit__(self, *exc) -> None:
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()

    # -- control
    def goto(self, url: str, wait_ms: int = 2500, timeout_ms: int = 45000) -> "Browser":
        resp = self.page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        self.status = resp.status if resp else None
        try:
            self.page.wait_for_load_state("networkidle", timeout=min(wait_ms * 4, 15000))
        except Exception:
            pass  # busy pages never go idle; the fixed wait below is enough
        self.page.wait_for_timeout(wait_ms)
        return self

    def click(self, selector: str | None = None, text: str | None = None) -> "Browser":
        (self.page.get_by_text(text, exact=False).first if text else self.page.locator(selector).first).click(timeout=10000)
        self.page.wait_for_timeout(800)
        return self

    def fill(self, selector: str, value: str) -> "Browser":
        self.page.locator(selector).first.fill(value, timeout=10000)
        return self

    def press(self, key: str) -> "Browser":
        self.page.keyboard.press(key)
        self.page.wait_for_timeout(800)
        return self

    def scroll(self, steps: int = 6, pause_ms: int = 600) -> "Browser":
        for _ in range(steps):
            self.page.mouse.wheel(0, 1200)
            self.page.wait_for_timeout(pause_ms)
        return self

    def screenshot(self, path: str) -> str:
        self.page.screenshot(path=path, full_page=False)
        return path

    # -- read
    def snapshot(self) -> dict:
        markdown = self.page.evaluate(TO_MARKDOWN)
        return {"url": self.page.url, "title": self.page.title(), "status": self.status,
                "markdown": markdown, "blocked": self.blocked(markdown)}

    def blocked(self, markdown: str | None = None) -> str | None:
        if self.status in (403, 429):
            return f"HTTP {self.status}"
        title = self.page.title()
        if BLOCK_TITLES.search(title):
            return f"bot wall: {title!r}"
        if any(any(f in (fr.url or "") for f in BLOCK_FRAMES) for fr in self.page.frames):
            return "captcha frame on page"
        md = markdown if markdown is not None else self.page.evaluate(TO_MARKDOWN)
        if len(md) < 20000 and nimble.CAPTCHA.search(md):
            return "captcha text on page"
        if len(md) < 200:
            return "page is (almost) empty"
        return None

    def run_actions(self, actions: list[dict] | None) -> None:
        """Local equivalents of the Nimble browser_actions we use (wait, auto_scroll, click, fill, press)."""
        for a in actions or []:
            (name, arg), = a.items()
            arg = arg if not isinstance(arg, dict) else {k: v for k, v in arg.items() if k != "required"}
            if name == "wait":
                d = arg["duration"] if isinstance(arg, dict) else arg
                self.page.wait_for_timeout(int(float(str(d).rstrip("ms").rstrip("s")) * (1 if str(d).endswith("ms") else 1000)))
            elif name == "auto_scroll":
                self.scroll(steps=6)
            elif name == "click":
                self.click(arg if isinstance(arg, str) else arg.get("selector"))
            elif name == "fill":
                self.fill(arg["selector"], arg["value"])
            elif name == "press":
                self.press(arg if isinstance(arg, str) else arg.get("key"))
            elif name == "wait_for_element":
                sel = arg if isinstance(arg, str) else arg.get("selector")
                try:
                    self.page.wait_for_selector(sel, timeout=(arg.get("timeout", 15000) if isinstance(arg, dict) else 15000))
                except Exception:
                    pass


def read(url: str, headless: bool = True, local: bool | None = None, **nimble_options) -> nimble.NimbleResult:
    """Read one page. Local browser first; if it's blocked (or fails), Nimble's stealth browser reads it.
    The result says which one did: mode "local", or Nimble's "live"/"replay" with a note in `summary`."""
    t0 = time.time()
    profile = nimble.site_profile(url)
    local = profile.get("local", True) if local is None else local
    if nimble._mode() == "replay" or not local:
        why = "replay mode" if nimble._mode() == "replay" else "site known to block local browsers"
        return _via_nimble(url, t0, why, nimble_options)

    try:
        with Browser(headless=headless) as b:
            b.goto(url)
            b.run_actions(nimble_options.get("browser_actions"))
            snap = b.snapshot()
    except Exception as e:  # playwright missing, navigation timeout, crash...
        return _via_nimble(url, t0, f"local browser failed: {type(e).__name__}: {str(e)[:120]}", nimble_options)

    if snap["blocked"]:
        return _via_nimble(url, t0, f"local browser blocked ({snap['blocked']})", nimble_options)
    return nimble.NimbleResult(
        "browse", "local", True, nimble._now(), nimble._ms(t0), source_url=snap["url"], content=snap["markdown"],
        summary=f"local browser read page ({len(snap['markdown'])} chars)",
    )


def _via_nimble(url: str, t0: float, why: str, options: dict) -> nimble.NimbleResult:
    r = nimble.extract(url, **options)
    r.kind = "browse"
    r.summary = f"{why} -> Nimble stealth browser: {r.summary}"
    r.latency_ms = nimble._ms(t0)
    stats["nimble_fallbacks"] += 1
    return r


stats = {"nimble_fallbacks": 0}


def main() -> None:
    p = argparse.ArgumentParser(description="Local headless browser with Nimble fallback")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("read")
    r.add_argument("url")
    r.add_argument("--show", action="store_true", help="show the browser window")
    r.add_argument("--local-only", action="store_true", help="never fall back to Nimble")
    r.add_argument("--screenshot", help="save a screenshot of the local page to this path")
    a = p.parse_args()

    if a.local_only or a.screenshot:
        with Browser(headless=not a.show) as b:
            b.goto(a.url)
            snap = b.snapshot()
            if a.screenshot:
                print("screenshot:", b.screenshot(a.screenshot))
        print(json.dumps({k: v for k, v in snap.items() if k != "markdown"}, indent=2))
        print(snap["markdown"][:3000])
        return
    res = read(a.url, headless=not a.show)
    print((res.content or "")[:3000])
    print(f"\n[{res.mode}] ok={res.ok} {res.summary} ({res.latency_ms} ms) {res.error or ''}")


if __name__ == "__main__":
    main()
