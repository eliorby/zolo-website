#!/usr/bin/env python3
"""Zolo website quality gate.

    python3 tests/check_site.py            # static checks + real-browser checks
    python3 tests/check_site.py --static   # static checks only (no Chrome needed)
    python3 tests/check_site.py -v         # list every check that passed too

Static checks parse the HTML/XML in the repo (metadata, hreflang, JSON-LD, sitemap,
asset references, asset budgets, accessibility basics). Browser checks serve the repo
on 127.0.0.1, load the important pages in headless Chrome through a same-origin runner
page, and verify runtime behaviour: JS errors, failed requests, broken images,
horizontal overflow at several widths, install-CTA tracking, theme and language toggles.
The local server is tests/pages_server.py (also `make serve`); it is checked to answer every
missing path with the branded 404.html and HTTP 404, like GitHub Pages.
External hosts are unresolvable during the browser run, so nothing reaches GA.

Only the Python standard library and a Chrome/Chromium binary are needed
(set CHROME=/path/to/chrome if it is not found automatically). If the Nu HTML checker
is available (VNU_JAR=/path/to/vnu.jar), it is run as an extra static check.
Exit status is non-zero when any check fails.
"""
import argparse
import http.client
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from html.parser import HTMLParser
from pathlib import Path

from pages_server import PagesHandler, PagesServer   # tests/pages_server.py: the GitHub-Pages-like local server

ROOT = Path(__file__).resolve().parent.parent
SITE = "https://zolo-deals.com"
PLAY_URL = "https://play.google.com/store/apps/details?id=com.zolo.deals"
LANGS = ("en", "he")
SECTIONS = ("", "privacy/", "terms/", "delete/", "accessibility/")

# ---- Budgets (intentional, generous; the first optimisation pass landed at ~530 KB / ~470 KB) ----
LANDING_IMAGE_PAYLOAD_KB = 1000   # sum of light-theme images referenced by a landing page
MAX_IMAGE_FILE_KB = 250           # any single referenced raster image
MAX_SMALL_ICON_KB = 40            # images displayed at <= 64px (header logo, social icons)
SMALL_ICON_MAX_DISPLAY_PX = 64

STORE_URLS = {"google_play": PLAY_URL}          # add "app_store": "https://apps.apple.com/..." when it exists
STORE_HOSTS = {"google_play": "play.google.com", "app_store": "apps.apple.com"}

# ---- Share links: contract owned by the app session (~/project-runtimes/zolo-app-runtime/share-link-contract.md) ----
SHARE_PATH = "/place/"                          # https://zolo-deals.com/place/?id=<placeId>[&deal=<dealKey>]
SHARE_ID_RE = r"^[A-Za-z0-9_-]{1,64}$"
SHARE_DEAL_RE = r"^[0-9a-f]{8}$"
SHARE_MAX_URL = 2048
APP_PACKAGE = "com.zolo.deals"
# Google Play App Signing certificate of the Play-installed production app (contract section 6). Not the
# upload key (48:99:DD:E0:...:01:F8) and not a debug key. Changing it must be a deliberate, two-place edit.
PLAY_SIGNING_SHA256 = "A8:ED:91:81:D9:70:C6:E2:A9:52:99:83:F9:C5:AD:C9:61:A3:15:7C:71:FF:0F:E1:A8:B1:A5:7F:BF:8C:CC:EE"
KNOWN_BAD_FINGERPRINTS = {
    # Android documentation / codelab examples
    "14:6D:E9:83:C5:73:06:50:D8:EE:B9:95:2F:34:FC:64:16:A0:83:42:E6:1D:BE:A8:8A:04:96:B2:3F:CF:44:E5",
    "FA:C6:17:45:DC:09:03:78:6F:B9:ED:E6:2A:96:2B:39:9F:73:48:F0:BB:6F:89:9B:83:32:66:75:91:03:3B:9C",
}
UPLOAD_KEY_PREFIX, UPLOAD_KEY_SUFFIX = "48:99:DD:E0", "01:F8"
PLACEHOLDER_WORDS = re.compile(r"TODO|TBD|XXX|PLACEHOLDER|REPLACE|EXAMPLE|CHANGEME|<|>|\$\{", re.I)
PLACEHOLDER_TEAM_IDS = {"ABCDE12345", "ABCDEFGHIJ", "0123456789", "1234567890", "A1B2C3D4E5", "9JA89QQLNQ", "TEAMID1234"}

WIDTHS = (320, 390, 768, 1280)                   # conversion-critical pages (landing, 404)
WIDTHS_SECONDARY = (320, 1280)                   # legal pages

# ---- Timeouts (seconds) ----
CDP_TIMEOUT = 20          # one DevTools command round trip
LOAD_TIMEOUT = 15         # page load event
REDIRECT_TIMEOUT = 3      # script redirect on the helper pages (it is synchronous in practice)
BROWSER_DEADLINE = 300    # whole browser phase; a hang fails instead of stalling CI


# --------------------------------------------------------------------------- page table
def localized_pages():
    pages = []
    for section in SECTIONS:
        for lang in LANGS:
            pages.append({
                "path": "/%s/%s" % (lang, section),
                "file": "%s/%sindex.html" % (lang, section),
                "lang": lang,
                "indexable": True,
                "x_default": "/" + section,
                "landing": section == "",
                "counterpart": "/%s/%s" % ("he" if lang == "en" else "en", section),
            })
    return pages


def redirect_pages():
    pages = []
    for section in SECTIONS:
        pages.append({"path": "/" + section, "file": section + "index.html", "lang": "en",
                      "indexable": False, "redirect": True})
    for vanity in ("fb", "ig", "tt"):
        pages.append({"path": "/%s/" % vanity, "file": "%s/index.html" % vanity, "lang": "en",
                      "indexable": False, "redirect": True})
    return pages


NOT_FOUND_PAGE = {"path": "/404.html", "file": "404.html", "lang": "en", "indexable": False, "not_found": True}
# Language-neutral share-link fallback: static lang=en, Hebrew chosen at runtime (checked separately)
SHARE_PAGE = {"path": SHARE_PATH, "file": "place/index.html", "lang": "en", "indexable": False, "share": True}
ALL_PAGES = localized_pages() + redirect_pages() + [NOT_FOUND_PAGE, SHARE_PAGE]


# --------------------------------------------------------------------------- reporting
class Report:
    def __init__(self, verbose):
        self.verbose = verbose
        self.failures = []
        self.passed = 0

    def check(self, ok, message):
        if ok:
            self.passed += 1
            if self.verbose:
                print("  ok   " + message)
        else:
            self.failures.append(message)
            print("  FAIL " + message)
        return ok

    def section(self, name):
        print("\n== " + name)


# --------------------------------------------------------------------------- html model
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class Doc(HTMLParser):
    """Flat element list with attributes and (for non-void elements) their text content."""

    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self.stack = []
        self.feed(text)
        while self.stack:
            self.elements.append(self.stack.pop())

    def handle_starttag(self, tag, attrs):
        el = {"tag": tag, "attrs": dict(attrs), "text": "", "uid": len(self.elements) + len(self.stack),
              "parents": [e["tag"] for e in self.stack], "parent_uids": [e["uid"] for e in self.stack]}
        if tag in VOID:
            self.elements.append(el)
        else:
            self.stack.append(el)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i]["tag"] == tag:
                while len(self.stack) > i:
                    self.elements.append(self.stack.pop())
                return

    def handle_data(self, data):
        # text inside an aria-hidden subtree does not contribute to any ancestor's accessible name
        if any(el["attrs"].get("aria-hidden") == "true" for el in self.stack):
            return
        for el in self.stack:
            el["text"] += data

    def find(self, tag, **attrs):
        out = []
        for el in self.elements:
            if el["tag"] != tag:
                continue
            if all((k in el["attrs"]) if v is None else el["attrs"].get(k) == v for k, v in attrs.items()):
                out.append(el)
        return out

    def first(self, tag, **attrs):
        found = self.find(tag, **attrs)
        return found[0] if found else None

    def meta(self, **attrs):
        el = self.first("meta", **attrs)
        return el["attrs"].get("content") if el else None

    @property
    def html_attrs(self):
        el = self.first("html")
        return el["attrs"] if el else {}


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def local_file(url):
    """Repo file for a root-relative or site-absolute URL (directories map to index.html)."""
    if url.startswith(SITE):
        url = url[len(SITE):] or "/"
    if not url.startswith("/"):
        return None
    url = url.split("#", 1)[0].split("?", 1)[0]
    p = ROOT / url.lstrip("/")
    if url.endswith("/") or p.is_dir():
        p = p / "index.html"
    return p


def is_hebrew(text):
    return bool(re.search(u"[\u0590-\u05FF]", text or ""))


def accessible_name(el, doc):
    a = el["attrs"]
    if a.get("aria-label") or a.get("aria-labelledby") or a.get("title"):
        return True
    if el["text"].strip():
        return True
    # an <img alt> inside the control names it
    for img in doc.find("img"):
        if el["uid"] in img["parent_uids"] and img["attrs"].get("alt"):
            return True
    return False


# --------------------------------------------------------------------------- static checks
def check_page_metadata(r, page, doc):
    p = page["path"]
    canonical_expected = SITE + p
    canonicals = doc.find("link", rel="canonical")
    if page.get("not_found"):
        # served for any missing URL, so it must not claim a canonical or og:url
        r.check(not canonicals and not doc.meta(property="og:url"), "%s: no canonical/og:url on the catch-all 404" % p)
    else:
        r.check(len(canonicals) == 1, "%s: exactly one canonical" % p)
    canonical = canonicals[0]["attrs"].get("href") if canonicals else None
    if page.get("share"):
        # every shared link collapses onto the one generic page; no per-language URLs exist
        r.check(canonical == SITE + SHARE_PATH and doc.meta(property="og:url") == SITE + SHARE_PATH,
                "%s: canonical and og:url are %s%s (got %s)" % (p, SITE, SHARE_PATH, canonical))
        r.check(not doc.find("link", rel="alternate"), "%s: no hreflang alternates on the language-neutral share page" % p)
    if page["indexable"]:
        r.check(canonical == canonical_expected, "%s: canonical is %s (got %s)" % (p, canonical_expected, canonical))
        r.check(doc.meta(property="og:url") == canonical_expected, "%s: og:url matches canonical" % p)
    title = doc.first("title")
    r.check(bool(title and title["text"].strip()), "%s: has <title>" % p)
    r.check(bool(doc.meta(name="description")), "%s: has meta description" % p)
    r.check(bool(doc.first("meta", name="viewport")), "%s: has viewport meta" % p)

    robots = doc.meta(name="robots") or ""
    if page["indexable"]:
        r.check("noindex" not in robots, "%s: indexable page is not noindex" % p)
    else:
        r.check("noindex" in robots, "%s: helper page is noindex" % p)

    if page.get("not_found"):
        return

    for prop in ("og:title", "og:description", "og:image", "og:image:width", "og:image:height", "og:site_name", "og:type"):
        r.check(bool(doc.meta(property=prop)), "%s: has %s" % (p, prop))
    r.check(doc.meta(name="twitter:card") == "summary_large_image", "%s: twitter:card" % p)
    og_image = doc.meta(property="og:image") or ""
    f = local_file(og_image)
    r.check(bool(og_image.startswith(SITE) and f and f.is_file()), "%s: og:image is an absolute URL to an existing file" % p)
    if f and f.is_file():
        r.check(f.suffix.lower() in (".jpg", ".jpeg", ".png"), "%s: og:image is JPEG/PNG (not WebP) for social scrapers" % p)

    alternates = {el["attrs"].get("hreflang"): el["attrs"].get("href") for el in doc.find("link", rel="alternate")}
    if page["indexable"]:
        section = p.split("/", 2)[2]
        r.check(alternates.get("en") == SITE + "/en/" + section, "%s: hreflang en" % p)
        r.check(alternates.get("he") == SITE + "/he/" + section, "%s: hreflang he" % p)
        r.check(alternates.get("x-default") == SITE + page["x_default"], "%s: hreflang x-default -> %s" % (p, page["x_default"]))
        r.check(set(alternates) == {"en", "he", "x-default"}, "%s: hreflang set is exactly en/he/x-default" % p)
        expected_locale = "he_IL" if page["lang"] == "he" else "en_US"
        r.check(doc.meta(property="og:locale") == expected_locale, "%s: og:locale %s" % (p, expected_locale))


def check_page_language(r, page, doc):
    p = page["path"]
    attrs = doc.html_attrs
    if page["lang"] == "he":
        r.check(attrs.get("lang") == "he" and attrs.get("dir") == "rtl", "%s: <html lang=he dir=rtl>" % p)
        r.check(is_hebrew(doc.first("title")["text"] if doc.first("title") else ""), "%s: Hebrew title" % p)
        for btn in doc.find("button"):
            label = btn["attrs"].get("aria-label", "")
            r.check(not label or is_hebrew(label), "%s: button aria-label localized (%r)" % (p, label))
        lang_btn = doc.first("button", **{"data-lang-btn": None})
        if lang_btn:
            r.check(lang_btn["text"].strip() == "EN" and lang_btn["attrs"].get("lang") == "en",
                    "%s: language toggle offers EN with lang=en" % p)
    else:
        r.check(attrs.get("lang") == "en" and attrs.get("dir") in (None, "ltr"), "%s: <html lang=en> LTR" % p)
        lang_btn = doc.first("button", **{"data-lang-btn": None})
        if lang_btn:
            r.check(lang_btn["text"].strip() == "עב" and lang_btn["attrs"].get("lang") == "he",
                    "%s: language toggle offers Hebrew with lang=he" % p)
    if "counterpart" in page:
        r.check(local_file(page["counterpart"]).is_file(), "%s: counterpart page %s exists" % (p, page["counterpart"]))


def check_page_links_and_assets(r, page, doc, text):
    p = page["path"]
    refs = set()
    for el in doc.elements:
        a = el["attrs"]
        for key in ("href", "src", "data-light", "data-dark"):
            v = a.get(key)
            if v and (v.startswith("/") or v.startswith(SITE)) and not v.startswith("/#"):
                refs.add(v)
        if a.get("srcset"):
            for cand in a["srcset"].split(","):
                refs.add(cand.strip().split(" ")[0])
    for ref in sorted(refs):
        f = local_file(ref)
        r.check(bool(f and f.is_file()), "%s: local reference exists: %s" % (p, ref))
    if page.get("not_found") or page.get("share"):
        # GitHub Pages serves 404.html for any missing path, including nested ones (/place/x/y),
        # and the share page lives one level down: every URL must be root-relative or absolute.
        rel = [v for v in re.findall(r'(?:href|src)="([^"]+)"', text)
               if not v.startswith(("/", "http", "#", "mailto:"))]
        r.check(not rel, "%s: no relative URLs (would break on nested paths): %s" % (p, rel))

    # images: alt attribute, dimensions, loading strategy
    for img in doc.find("img"):
        a = img["attrs"]
        src = a.get("src", "?")
        r.check("alt" in a, "%s: <img %s> has an alt attribute" % (p, src))
        r.check(bool(a.get("width") and a.get("height")), "%s: <img %s> has width and height" % (p, src))
        try:
            w = int(a.get("width", "0"))
        except ValueError:
            w = 0
        f = local_file(src)
        if f and f.is_file() and 0 < w <= SMALL_ICON_MAX_DISPLAY_PX:
            kb = f.stat().st_size // 1024
            r.check(kb <= MAX_SMALL_ICON_KB, "%s: small icon %s is %d KB (limit %d KB)" % (p, src, kb, MAX_SMALL_ICON_KB))

    if page.get("landing"):
        imgs = doc.find("img")
        lcp = [i for i in imgs if i["attrs"].get("fetchpriority") == "high"]
        r.check(len(lcp) == 1 and lcp[0]["attrs"].get("loading") != "lazy",
                "%s: exactly one eager fetchpriority=high image (the LCP hero mockup), got %d" % (p, len(lcp)))
        lazy = [i for i in imgs if i["attrs"].get("loading") == "lazy"]
        r.check(len(lazy) >= 3, "%s: below-the-fold screenshots are lazy-loaded (got %d lazy images)" % (p, len(lazy)))
        r.check(len([el for el in doc.find("picture") if "data-theme-pic" in el["attrs"]]) >= 4,
                "%s: light/dark <picture> pairs present for mockup and screenshots" % p)

    # external links opening a new tab
    for a_el in doc.find("a"):
        a = a_el["attrs"]
        href = a.get("href", "")
        r.check(bool(href.strip()) and href != "#", "%s: <a> has a real href (%r)" % (p, href))
        if a.get("target") == "_blank":
            r.check("noopener" in a.get("rel", ""), "%s: target=_blank link %s has rel=noopener" % (p, href))

    # skip link target
    skip = next((el for el in doc.find("a") if "skip-link" in el["attrs"].get("class", "").split()), None)
    r.check(bool(skip) or page.get("redirect"), "%s: has a skip link" % p)
    if skip:
        target = skip["attrs"]["href"].lstrip("#")
        r.check(any(el["attrs"].get("id") == target for el in doc.elements), "%s: skip link target #%s exists" % (p, target))


def check_page_a11y(r, page, doc):
    p = page["path"]
    ids = [el["attrs"]["id"] for el in doc.elements if el["attrs"].get("id")]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    r.check(not dupes, "%s: no duplicate ids %s" % (p, dupes))
    h1 = doc.find("h1")
    if page["indexable"] or page.get("not_found"):
        r.check(len(h1) == 1, "%s: exactly one <h1> (got %d)" % (p, len(h1)))
    if page.get("share"):
        # one language block is displayed at a time; each has exactly one <h1>
        for lang in LANGS:
            blocks = [el for el in doc.find("section") if el["attrs"].get("data-l10n") == lang]
            inside = [h for h in h1 if blocks and blocks[0]["uid"] in h["parent_uids"]]
            r.check(len(blocks) == 1 and len(inside) == 1, "%s: the %s block has exactly one <h1>" % (p, lang))
        he = next((el for el in doc.find("section") if el["attrs"].get("data-l10n") == "he"), None)
        r.check(bool(he and he["attrs"].get("lang") == "he" and he["attrs"].get("dir") == "rtl"),
                "%s: Hebrew block declares lang=he dir=rtl" % p)
        for el in doc.elements:
            if el["attrs"].get("data-l10n") == "he" and el["tag"] != "button":   # the toggle's text is "EN" (lang=en)
                inherited = any(q["attrs"].get("lang") == "he" for q in doc.elements if q["uid"] in el["parent_uids"])
                r.check(el["attrs"].get("lang") == "he" or inherited, "%s: Hebrew-only <%s> declares lang=he" % (p, el["tag"]))
    for el in doc.elements:
        if el["tag"] in ("a", "button"):
            r.check(accessible_name(el, doc), "%s: <%s> has an accessible name (%s)" % (p, el["tag"], el["attrs"].get("href") or el["attrs"].get("class")))
            nested = [t for t in el["parents"] if t in ("a", "button")]
            r.check(not nested, "%s: <%s> nested inside interactive %s" % (p, el["tag"], nested))
        if el["tag"] == "button":
            r.check(el["attrs"].get("type") == "button", "%s: <button> declares type=button" % p)


def check_ctas(r, page, doc):
    p = page["path"]
    ctas = doc.find("a", **{"data-cta": None})
    placements = {c["attrs"]["data-cta"] for c in ctas}
    if page.get("landing"):
        r.check({"hero", "bottom"} <= placements, "%s: install CTAs at hero and bottom (got %s)" % (p, sorted(placements)))
    elif page.get("not_found"):
        r.check(placements == {"404"}, "%s: install CTA on 404 page" % p)
    elif page.get("share"):
        r.check(placements == {"share"} and len(ctas) == 2, "%s: one Google Play CTA per language (got %s)" % (p, sorted(placements)))
    else:
        r.check(not placements, "%s: no install CTAs expected (got %s)" % (p, sorted(placements)))
    for c in ctas:
        r.check(c["attrs"].get("href") in STORE_URLS.values(), "%s: CTA %s points to a known store listing (%s)" % (p, c["attrs"]["data-cta"], c["attrs"].get("href")))
        r.check(accessible_name(c, doc), "%s: CTA %s has an accessible name" % (p, c["attrs"]["data-cta"]))


def check_json_ld(r, page, doc):
    p = page["path"]
    scripts = [el for el in doc.find("script") if el["attrs"].get("type") == "application/ld+json"]
    if not page.get("landing"):
        return
    r.check(len(scripts) == 1, "%s: one JSON-LD block" % p)
    if not scripts:
        return
    try:
        data = json.loads(scripts[0]["text"])
    except ValueError as e:
        r.check(False, "%s: JSON-LD parses (%s)" % (p, e))
        return
    nodes = data.get("@graph", [data])
    types = {n.get("@type") for n in nodes}
    r.check({"Organization", "MobileApplication"} <= types, "%s: JSON-LD has Organization and MobileApplication" % p)
    app = next((n for n in nodes if n.get("@type") == "MobileApplication"), {})
    for key in ("name", "operatingSystem", "applicationCategory", "installUrl", "offers", "url", "description", "image"):
        r.check(key in app, "%s: MobileApplication has %s" % (p, key))
    r.check(app.get("installUrl") == PLAY_URL, "%s: MobileApplication.installUrl is the Play listing" % p)
    r.check(app.get("url") == SITE + p, "%s: MobileApplication.url is this page" % p)
    offers = app.get("offers") or {}
    r.check(str(offers.get("price")) == "0" and bool(offers.get("priceCurrency")), "%s: offer is free with a currency" % p)
    r.check("Android" in str(app.get("operatingSystem")), "%s: operatingSystem includes Android" % p)
    org = next((n for n in nodes if n.get("@type") == "Organization"), {})
    logo = local_file(org.get("logo", ""))
    r.check(bool(logo and logo.is_file()), "%s: Organization.logo exists" % p)


def check_landing_payload(r, page, doc):
    """Light-theme mobile payload: every referenced local image except dark variants."""
    p = page["path"]
    files = set()
    for el in doc.elements:
        a = el["attrs"]
        if el["tag"] == "img" and a.get("src"):
            files.add(a["src"])
        if el["tag"] == "source" and a.get("srcset") and "dark" not in a.get("media", ""):
            files.add(a["srcset"].split()[0])
        if a.get("data-light"):
            files.add(a["data-light"])
    total = 0
    for ref in files:
        f = local_file(ref)
        if f and f.is_file():
            kb = f.stat().st_size // 1024
            total += kb
            r.check(kb <= MAX_IMAGE_FILE_KB, "%s: image %s is %d KB (limit %d KB)" % (p, ref, kb, MAX_IMAGE_FILE_KB))
    r.check(total <= LANDING_IMAGE_PAYLOAD_KB, "%s: light-theme image payload %d KB within %d KB budget" % (p, total, LANDING_IMAGE_PAYLOAD_KB))
    # dark variants must exist for every light one that declares a dark source
    for el in doc.find("source"):
        if "dark" in el["attrs"].get("media", ""):
            f = local_file(el["attrs"]["srcset"].split()[0])
            r.check(bool(f and f.is_file()), "%s: dark variant exists: %s" % (p, el["attrs"]["srcset"]))
    for el in doc.elements:
        if "data-hero-bg" in el["attrs"]:
            for key in ("data-light", "data-dark"):
                f = local_file(el["attrs"].get(key, ""))
                r.check(bool(f and f.is_file()), "%s: hero background %s exists" % (p, key))


def check_sitemap_and_robots(r):
    r.section("sitemap and robots")
    tree = ET.parse(ROOT / "sitemap.xml")
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9", "x": "http://www.w3.org/1999/xhtml"}
    locs = {u.findtext("s:loc", namespaces=ns) for u in tree.getroot().findall("s:url", ns)}
    expected = {SITE + p["path"] for p in ALL_PAGES if p["indexable"]}
    r.check(locs == expected, "sitemap lists exactly the indexable pages (missing %s, extra %s)"
            % (sorted(expected - locs), sorted(locs - expected)))
    today = date.today() + timedelta(days=1)   # tolerate time-zone differences between author and CI
    for u in tree.getroot().findall("s:url", ns):
        loc = u.findtext("s:loc", namespaces=ns)
        lastmod = u.findtext("s:lastmod", namespaces=ns) or ""
        try:
            d = date.fromisoformat(lastmod)
            r.check(d <= today, "sitemap %s lastmod %s is not in the future" % (loc, lastmod))
        except ValueError:
            r.check(False, "sitemap %s has a valid lastmod (got %r)" % (loc, lastmod))
        alts = {l.get("hreflang"): l.get("href") for l in u.findall("x:link", ns)}
        r.check(set(alts) == {"en", "he", "x-default"}, "sitemap %s has en/he/x-default alternates" % loc)
        r.check(loc in alts.values(), "sitemap %s alternates include itself" % loc)
        for href in alts.values():
            r.check(local_file(href).is_file(), "sitemap alternate exists: %s" % href)
    robots = read("robots.txt")
    r.check("Sitemap: %s/sitemap.xml" % SITE in robots, "robots.txt points at the sitemap")
    r.check(re.search(r"^Allow: /\s*$", robots, re.M) is not None and "Disallow: /" not in robots, "robots.txt allows crawling")
    r.check((ROOT / "CNAME").read_text().strip() == SITE.replace("https://", ""), "CNAME matches SITE")


def check_repo_hygiene(r):
    r.section("repository hygiene")
    referenced = set()
    for page in ALL_PAGES:
        for m in re.finditer(r'(?:src|href|srcset|data-light|data-dark|content)="(/assets/[^"\s]+)', read(page["file"])):
            referenced.add(m.group(1))
        for m in re.finditer(r'%s(/assets/[^"\s]+)' % re.escape(SITE), read(page["file"])):
            referenced.add(m.group(1))
    for css in (ROOT / "assets/common").glob("*.css"):
        for m in re.finditer(r'url\("?(/assets/[^")]+)', css.read_text()):
            referenced.add(m.group(1))
    # Large unreferenced rasters are dead weight (the optimisation pass replaced the PNG screenshots).
    for f in sorted(ROOT.glob("assets/**/*")):
        if f.is_file() and f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
            rel = "/" + f.relative_to(ROOT).as_posix()
            if rel not in referenced and f.stat().st_size > 300 * 1024:
                r.check(False, "unreferenced large asset %s (%d KB)" % (rel, f.stat().st_size // 1024))
    r.check(not list(ROOT.glob("assets/**/*.psd")), "no design sources under assets/")


SITE_TEXT_SUFFIXES = (".html", ".js", ".css", ".json", ".xml", ".txt")


def site_text_files():
    """Everything GitHub Pages publishes that could carry a link (the quality gate itself excluded)."""
    for f in sorted(ROOT.rglob("*")):
        rel = f.relative_to(ROOT).as_posix()
        if f.is_file() and f.suffix in SITE_TEXT_SUFFIXES and not rel.startswith((".git/", ".idea/", "tests/")):
            yield rel, f.read_text(encoding="utf-8", errors="replace")


def check_share_link(r):
    r.section("share links (contract: /place/?id=<placeId>[&deal=<dealKey>])")
    js = read("assets/common/place.js")
    # The page must validate exactly as the app does; these mirror share-link-contract.md sections 1-4.
    r.check("var ID_RE = /%s/;" % SHARE_ID_RE.replace("/", "\\/") in js, "place.js validates id with %s" % SHARE_ID_RE)
    r.check("var DEAL_RE = /%s/;" % SHARE_DEAL_RE in js, "place.js validates deal with %s" % SHARE_DEAL_RE)
    r.check("var MAX_URL_LENGTH = %d;" % SHARE_MAX_URL in js, "place.js rejects URLs over %d characters" % SHARE_MAX_URL)
    r.check('var SHARE_PATH = "%s";' % SHARE_PATH in js and 'var PACKAGE = "%s";' % APP_PACKAGE in js and 'var PLAY_URL = "%s";' % PLAY_URL in js,
            "place.js uses the contract path, package and Play listing")
    # Nothing from the URL may reach markup, script evaluation or navigation.
    sinks = [w for w in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "Function(",
                         "location.href =", "location.assign", "location.replace", "window.open", "setTimeout(\"",
                         "firebase", "firestore") if w.lower() in js.lower()]
    r.check(not sinks, "place.js has no HTML/script/navigation sinks and no backend access (found %s)" % sinks)
    r.check(js.count("setAttribute(\"href\"") == 1 and "intentUrl(link)" in js,
            "place.js writes exactly one attribute from the link: the intent href built from the validated id/deal")

    # One scheme only: no competing routes, custom schemes, Dynamic Links or localized share paths.
    competing = re.compile(r"zolo://|\.page\.link|app\.goo\.gl|zolo-deals\.com/(?:p|share|en/place|he/place)\b|"
                           r"href=\"/(?:p|share|en/place|he/place)/")
    hits = ["%s: %s" % (rel, m.group(0)) for rel, text in site_text_files() for m in competing.finditer(text)]
    r.check(not hits, "no competing share-link scheme anywhere on the site %s" % hits)
    pages_with_share = sorted(rel for rel, text in site_text_files() if re.search(r'href="[^"]*/place/?\?', text))
    r.check(not pages_with_share, "no page hard-codes a shared-link URL (share URLs come from the app) %s" % pages_with_share)
    apple = sorted(rel for rel, text in site_text_files() if rel.endswith(".html") and "apps.apple.com" in text)
    r.check(not apple, "no App Store link anywhere until a real listing exists %s" % apple)

    robots = read("robots.txt")
    r.check(not re.search(r"^Disallow:\s*/(place|\.well-known)", robots, re.M),
            "robots.txt lets crawlers see /place/ (so noindex is honoured) and /.well-known/")
    r.check(SITE + SHARE_PATH not in read("sitemap.xml"), "sitemap does not list the share fallback")


def check_association_files(r):
    r.section(".well-known association files")
    wk = ROOT / ".well-known"
    r.check((ROOT / ".nojekyll").is_file(), ".nojekyll present (GitHub Pages would otherwise drop .well-known/)")
    present = sorted(f.name for f in wk.iterdir()) if wk.is_dir() else []
    r.check(set(present) <= {"assetlinks.json", "apple-app-site-association"},
            ".well-known holds only the association files (got %s)" % present)
    r.check(not (ROOT / "apple-app-site-association").exists() and not (wk / "apple-app-site-association.json").exists(),
            "no Apple association file at a legacy/alternate path")

    # ---- Android Digital Asset Links ----
    f = wk / "assetlinks.json"
    if not r.check(f.is_file(), "/.well-known/assetlinks.json exists"):
        return
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except ValueError as e:
        r.check(False, "assetlinks.json parses (%s)" % e)
        return
    r.check(isinstance(data, list) and len(data) == 1, "assetlinks.json is a list with one statement")
    st = data[0] if isinstance(data, list) and data else {}
    target = st.get("target") or {}
    r.check(st.get("relation") == ["delegate_permission/common.handle_all_urls"], "assetlinks relation is handle_all_urls only")
    r.check(target.get("namespace") == "android_app" and target.get("package_name") == APP_PACKAGE,
            "assetlinks targets android_app %s only (got %s)" % (APP_PACKAGE, target.get("package_name")))
    r.check(set(st) == {"relation", "target"} and set(target) == {"namespace", "package_name", "sha256_cert_fingerprints"},
            "assetlinks has no extra fields (got %s / %s)" % (sorted(st), sorted(target)))
    fps = target.get("sha256_cert_fingerprints") or []
    for fp in fps:
        r.check(re.fullmatch(r"(?:[0-9A-F]{2}:){31}[0-9A-F]{2}", fp or "") is not None, "fingerprint %s is 32 upper-case hex bytes" % fp)
        r.check(fp not in KNOWN_BAD_FINGERPRINTS and not PLACEHOLDER_WORDS.search(fp) and len(set(fp.replace(":", ""))) > 4,
                "fingerprint %s is not an example/placeholder value" % fp)
        r.check(not (fp.startswith(UPLOAD_KEY_PREFIX) and fp.endswith(UPLOAD_KEY_SUFFIX)), "fingerprint %s is not the upload key" % fp)
    r.check(fps == [PLAY_SIGNING_SHA256], "assetlinks lists exactly the Play App Signing certificate from the contract")

    # ---- Apple Universal Links: deploy only with real values; until then a template outside .well-known ----
    tpl = ROOT / "association" / "apple-app-site-association.template.json"
    aasa = wk / "apple-app-site-association"
    for label, file, live in (("template", tpl, False), ("live AASA", aasa, True)):
        if not file.is_file():
            # the live file stays absent until the real Team ID exists (contract section 7); the template must exist
            r.check(live, "live AASA absent (no Team ID yet)" if live else "Apple template exists")
            continue
        try:
            doc = json.loads(file.read_text(encoding="utf-8"))
        except ValueError as e:
            r.check(False, "Apple %s parses (%s)" % (label, e))
            continue
        details = (doc.get("applinks") or {}).get("details") or []
        r.check(set(doc) == {"applinks"} and len(details) >= 1, "Apple %s declares applinks only" % label)
        for d in details:
            for app_id in d.get("appIDs") or []:
                team, _, bundle = app_id.partition(".")
                r.check(bundle == APP_PACKAGE, "Apple %s appID bundle is %s (got %s)" % (label, APP_PACKAGE, app_id))
                if live:
                    r.check(re.fullmatch(r"[A-Z0-9]{10}", team) is not None and team not in PLACEHOLDER_TEAM_IDS
                            and len(set(team)) > 3, "live AASA Team ID %r is real-looking, not a placeholder" % team)
                else:
                    r.check(team == "<TEAM_ID>", "Apple template keeps the unmistakable <TEAM_ID> placeholder")
            comps = d.get("components") or []
            paths = sorted({c.get("/") for c in comps})
            r.check(paths == ["/place", "/place/"] and all(not c.get("exclude") for c in comps),
                    "Apple %s claims only /place and /place/ (got %s)" % (label, paths))
            r.check(all((c.get("?") or {}).get("id") == "?*" for c in comps), "Apple %s requires ?id= on every component" % label)
            r.check("paths" not in d, "Apple %s uses components, not legacy wildcard paths" % label)


def run_nu_validator(r):
    jar = os.environ.get("VNU_JAR") or str(ROOT / "tests" / "vnu.jar")
    if not os.path.isfile(jar) or not shutil.which("java"):
        print("  (Nu HTML checker skipped: set VNU_JAR=/path/to/vnu.jar and have java on PATH)")
        return
    r.section("Nu HTML checker")
    files = [str(ROOT / p["file"]) for p in ALL_PAGES]
    out = subprocess.run(["java", "-jar", jar, "--format", "gnu"] + files, capture_output=True, text=True)
    problems = [l for l in (out.stdout + out.stderr).splitlines() if ": error:" in l or ": warning:" in l or ": info " in l]
    r.check(not problems, "Nu HTML checker reports nothing:\n    " + "\n    ".join(problems[:20]))
    css = subprocess.run(["java", "-jar", jar, "--format", "gnu", "--css", str(ROOT / "assets/common/site.css")], capture_output=True, text=True)
    r.check(not (css.stdout + css.stderr).strip(), "site.css validates:\n    " + (css.stdout + css.stderr)[:500])


def static_checks(r):
    for page in ALL_PAGES:
        r.section("static: " + page["path"])
        text = read(page["file"])
        doc = Doc(text)
        check_page_metadata(r, page, doc)
        check_page_language(r, page, doc)
        check_page_links_and_assets(r, page, doc, text)
        check_page_a11y(r, page, doc)
        check_ctas(r, page, doc)
        check_json_ld(r, page, doc)
        if page.get("landing"):
            check_landing_payload(r, page, doc)
        if page.get("redirect"):
            r.check("<noscript>" in text and 'http-equiv="refresh"' in text, "%s: redirect page has a no-JS fallback" % page["path"])
    check_sitemap_and_robots(r)
    check_share_link(r)
    check_association_files(r)
    check_repo_hygiene(r)
    run_nu_validator(r)


# --------------------------------------------------------------------------- browser checks
# Headless Chrome is driven over the DevTools protocol through --remote-debugging-pipe
# (JSON messages on fds 3/4, NUL-terminated), so no third-party client is required.

PAGE_INFO_JS = r"""(() => {
  const q = s => document.querySelector(s);
  return {
    lang: document.documentElement.lang, dir: document.documentElement.dir, title: document.title,
    brokenImages: Array.from(document.images).filter(i => i.complete && i.naturalWidth === 0 && i.getAttribute("src")).map(i => i.getAttribute("src")),
    langBtnText: q("[data-lang-btn]") ? q("[data-lang-btn]").textContent.trim() : null,
    themeLabel: q("[data-theme-btn]") ? q("[data-theme-btn]").getAttribute("aria-label") : null,
    ctas: Array.from(document.querySelectorAll("a[data-cta]")).map(a => ({ placement: a.getAttribute("data-cta"), href: a.getAttribute("href") })),
  };
})()"""

OVERFLOW_JS = r"""(() => {
  const width = window.innerWidth;
  const scrollers = new Set();
  const all = Array.from(document.body.querySelectorAll("*"));
  for (const el of all) if (/(auto|scroll)/.test(getComputedStyle(el).overflowX)) scrollers.add(el);
  const offenders = [];
  for (const el of all) {
    let inScroller = false;
    for (const s of scrollers) if (s !== el && s.contains(el)) { inScroller = true; break; }
    if (inScroller) continue;
    const rect = el.getBoundingClientRect();
    if (rect.width > 0 && (rect.right > width + 1 || rect.left < -1) && offenders.length < 5)
      offenders.push(el.tagName.toLowerCase() + (typeof el.className === "string" && el.className ? "." + el.className.trim().split(/\s+/).join(".") : ""));
  }
  return { width, scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth, offenders };
})()"""

CLICK_JS = r"""(() => {
  const events = [];
  window.gtag = function () { events.push(Array.from(arguments)); };
  document.addEventListener("click", e => { if (e.target.closest("a")) e.preventDefault(); }, true);
  const clicks = [];
  for (const a of document.querySelectorAll("a[data-cta]")) {
    const before = events.length; a.click();
    clicks.push({ placement: a.getAttribute("data-cta"), fired: events.length - before, payload: events.length > before ? events[events.length - 1] : null });
  }
  window.gtag = undefined;
  const first = document.querySelector("a[data-cta]");
  let noGtag = null;
  if (first) { const before = events.length; first.click(); noGtag = { fired: events.length - before }; }
  return { clicks, noGtag };
})()"""

THEME_JS = r"""(async () => {
  const btn = document.querySelector("[data-theme-btn]");
  if (!btn) return null;
  try { localStorage.removeItem("zolo_theme"); } catch (e) {}
  const media = () => Array.from(new Set(Array.from(document.querySelectorAll("picture[data-theme-pic] > source")).map(s => s.getAttribute("media"))));
  const tc = () => Array.from(document.querySelectorAll('meta[name="theme-color"]')).map(m => m.content);
  const out = [];
  for (let i = 0; i < 3; i++) {
    btn.click(); await new Promise(r => setTimeout(r, 10));
    out.push({ mode: document.documentElement.getAttribute("data-theme"), media: media(), themeColor: tc(), label: btn.getAttribute("aria-label") });
  }
  return out;
})()"""


class Chrome:
    """Minimal DevTools client over --remote-debugging-pipe."""

    def __init__(self, binary, profile):
        r_in, w_in = os.pipe()    # Chrome reads commands on fd 3
        r_out, w_out = os.pipe()  # Chrome writes responses on fd 4
        cmd = [binary, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
               "--disable-extensions", "--disable-background-networking", "--disable-component-update",
               "--user-data-dir=" + profile, "--remote-debugging-pipe",
               "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",   # offline: nothing reaches GA
               "about:blank"]
        if sys.platform.startswith("linux"):
            cmd.insert(1, "--disable-dev-shm-usage")
        if os.environ.get("CI") or (hasattr(os, "geteuid") and os.geteuid() == 0):
            cmd.insert(1, "--no-sandbox")   # CI containers/root cannot use Chrome's user-namespace sandbox
        self.stderr = tempfile.TemporaryFile()
        self.deadline = time.monotonic() + BROWSER_DEADLINE
        self.proc = self._spawn(cmd, r_in, w_out)
        os.close(r_in)
        os.close(w_out)
        self.w = w_in
        self.rd = r_out
        self.buf = b""
        self.next_id = 0
        self.events = []
        self.session = None

    def _spawn(self, cmd, r_in, w_out):
        """Chrome expects the command pipe on fd 3 and the response pipe on fd 4. Arrange that in the
        parent (preserving whatever occupies those numbers) and hand them over with pass_fds. This
        works on every Python 3 and needs no preexec_fn, which Python 3.14 no longer lets juggle fds."""
        saved = {}
        for fd in (3, 4):
            try:
                saved[fd] = os.dup(fd)
            except OSError:
                saved[fd] = None
        tmp_in, tmp_out = os.dup(r_in), os.dup(w_out)   # copies, so a pipe end already at 3/4 is not clobbered
        os.dup2(tmp_in, 3)
        os.dup2(tmp_out, 4)
        os.close(tmp_in)
        os.close(tmp_out)
        try:
            return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=self.stderr, pass_fds=(3, 4))
        finally:
            for fd, old in saved.items():
                if old is None:
                    os.close(fd)
                else:
                    os.dup2(old, fd)
                    os.close(old)

    def _read_message(self, timeout):
        while b"\0" not in self.buf:
            if time.monotonic() > self.deadline:
                raise TimeoutError("browser phase exceeded %ss" % BROWSER_DEADLINE)
            ready, _, _ = select.select([self.rd], [], [], timeout)
            if not ready:
                if self.proc.poll() is not None:
                    raise RuntimeError("Chrome exited with status %s" % self.proc.returncode)
                raise TimeoutError("no DevTools message within %ss" % timeout)
            chunk = os.read(self.rd, 65536)
            if not chunk:
                raise RuntimeError("DevTools pipe closed")
            self.buf += chunk
        raw, self.buf = self.buf.split(b"\0", 1)
        return json.loads(raw.decode("utf-8"))

    def send(self, method, params=None, session=True, timeout=CDP_TIMEOUT):
        self.next_id += 1
        msg = {"id": self.next_id, "method": method, "params": params or {}}
        if session and self.session:
            msg["sessionId"] = self.session
        os.write(self.w, json.dumps(msg).encode("utf-8") + b"\0")
        while True:
            m = self._read_message(timeout)
            if m.get("id") == msg["id"]:
                if "error" in m:
                    raise RuntimeError("%s: %s" % (method, m["error"]))
                return m.get("result", {})
            if "method" in m:
                self.events.append(m)

    def wait_event(self, name, timeout=LOAD_TIMEOUT):
        for i, m in enumerate(self.events):
            if m["method"] == name:
                return self.events.pop(i)
        end = time.monotonic() + timeout
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("no %s within %ss" % (name, timeout))
            m = self._read_message(remaining)
            if m.get("method") == name:
                return m
            if "method" in m:
                self.events.append(m)

    def drain(self):
        self.send("Runtime.evaluate", {"expression": "0"})  # round trip flushes pending events
        taken, self.events = self.events, []
        return taken

    def evaluate(self, expression):
        res = self.send("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True})
        if "exceptionDetails" in res:
            raise RuntimeError(res["exceptionDetails"].get("text", "evaluate failed"))
        return res["result"].get("value")

    def open_page(self):
        target = self.send("Target.createTarget", {"url": "about:blank"}, session=False)
        self.session = self.send("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True}, session=False)["sessionId"]
        for domain in ("Page", "Runtime", "Log", "Network"):
            self.send(domain + ".enable")

    def set_width(self, width, height=900):
        self.send("Emulation.setDeviceMetricsOverride", {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": width < 700})

    def navigate(self, url):
        self.events = [m for m in self.events if m["method"] != "Page.loadEventFired"]
        self.send("Page.navigate", {"url": url})
        self.wait_event("Page.loadEventFired")

    def wait_for_location_change(self, initial_path):
        """Helper pages redirect with location.replace() from an inline script; poll the URL."""
        end = time.monotonic() + REDIRECT_TIMEOUT
        while time.monotonic() < end:
            loc = self.evaluate("location.pathname + location.search")
            if loc.split("?")[0] != initial_path:
                return loc
            time.sleep(0.05)
        return self.evaluate("location.pathname + location.search")

    def press_tab(self):
        for t in ("keyDown", "keyUp"):
            self.send("Input.dispatchKeyEvent", {"type": t, "key": "Tab", "code": "Tab", "windowsVirtualKeyCode": 9})

    def set_user_agent(self, user_agent, accept_language="en-US,en"):
        """Also drives navigator.language(s), which the share page uses to pick EN/HE."""
        self.send("Emulation.setUserAgentOverride", {"userAgent": user_agent, "acceptLanguage": accept_language})

    def set_javascript(self, enabled):
        self.send("Emulation.setScriptExecutionDisabled", {"value": not enabled})

    def close(self):
        try:
            self.send("Browser.close", session=False, timeout=5)
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()
            self.proc.wait()
        for fd in (self.w, self.rd):
            try:
                os.close(fd)
            except OSError:
                pass
        self.stderr.close()


ANDROID_UA = "Mozilla/5.0 (Linux; Android 14; Pixel 7 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Mobile Safari/537.36"
IPHONE_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
DESKTOP_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
SHARE_ID = "9qrxnqnI6TuYgvUfLTUK"      # contract example ids
SHARE_DEAL = "3f9a1c07"
MARKER_ID = "Zq7Mk_x-9"                 # valid id that must never appear in the rendered page
XSS = '"><img src=x onerror="window.__xss=1"><script>window.__xss=1</script>'


def intent_for(place_id, deal=None):
    query = "?id=" + place_id + ("&deal=" + deal if deal else "")
    return ("intent://zolo-deals.com%s%s#Intent;scheme=https;action=android.intent.action.VIEW;package=%s;S.browser_fallback_url=%s;end"
            % (SHARE_PATH, query, APP_PACKAGE, urllib.parse.quote(PLAY_URL, safe="-_.!~*'()")))


_q = urllib.parse.quote
SHARE_CASES = [
    # (what, query appended to /place/, expected data-share state, (id, deal) the Android intent must carry)
    ("place link", "?id=" + SHARE_ID, "place", (SHARE_ID, None)),
    ("deal link", "?id=%s&deal=%s" % (SHARE_ID, SHARE_DEAL), "deal", (SHARE_ID, SHARE_DEAL)),
    ("any order; tracking params and fragment ignored", "?utm_source=whatsapp&deal=%s&fbclid=AbC&id=%s#top" % (SHARE_DEAL, SHARE_ID), "deal", (SHARE_ID, SHARE_DEAL)),
    ("percent-encoded ASCII is decoded before validation", "?id=%61b%2Dc_9", "place", ("ab-c_9", None)),
    ("64-character id", "?id=" + "a" * 64, "place", ("a" * 64, None)),
    ("65-character id", "?id=" + "a" * 65, "invalid", None),
    ("no query", "", "invalid", None),
    ("empty id", "?id=", "invalid", None),
    ("id without a value", "?id", "invalid", None),
    ("parameter names are case-sensitive", "?ID=" + SHARE_ID, "invalid", None),
    ("repeated id", "?id=abc&id=abc", "invalid", None),
    ("empty deal", "?id=abc&deal=", "invalid", None),
    ("upper-case deal", "?id=abc&deal=3F9A1C07", "invalid", None),
    ("7-character deal", "?id=abc&deal=3f9a1c0", "invalid", None),
    ("repeated deal", "?id=abc&deal=%s&deal=%s" % (SHARE_DEAL, SHARE_DEAL), "invalid", None),
    ("malformed percent-encoding in id", "?id=%zz", "invalid", None),
    ("malformed percent-encoding in another parameter", "?id=abc&utm_source=%E0%A4%A", "invalid", None),
    ("encoded path separator", "?id=ab%2Fcd", "invalid", None),
    ("path traversal", "?id=..%2F..%2Fetc%2Fpasswd", "invalid", None),
    ("encoded query separators stay inside the value", "?id=abc%26deal%3D" + SHARE_DEAL, "invalid", None),
    ("plus decodes to a space", "?id=abc+def", "invalid", None),
    ("non-ASCII id", "?id=" + _q("קפה"), "invalid", None),
    ("full-width look-alike id", "?id=" + _q("ａｂｃ"), "invalid", None),
    ("HTML/script injection in id", "?id=" + _q(XSS, safe=""), "invalid", None),
    ("injection in an unknown parameter", "?id=abc&name=" + _q(XSS, safe=""), "place", ("abc", None)),
    ("redirect-style parameters", "?id=abc&redirect=https%3A%2F%2Fevil.example%2F&next=//evil.example&url=javascript:alert(1)&return_to=https://evil.example", "place", ("abc", None)),
    ("environment/collection parameters", "?id=abc&env=dev&flavor=devtest&project=zolo-dev&collection=places_devtest&host=evil.example", "place", ("abc", None)),
    ("injection in the fragment", "?id=abc#" + _q(XSS, safe=""), "place", ("abc", None)),
    ("URL longer than 2048 characters", "?id=abc&pad=" + "x" * 2100, "invalid", None),
    ("the id itself is never rendered", "?id=%s&deal=%s" % (MARKER_ID, SHARE_DEAL), "deal", (MARKER_ID, SHARE_DEAL)),
]

SHARE_INFO_JS = r"""(() => {
  const vis = el => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== "hidden";
  const all = s => Array.from(document.querySelectorAll(s)).filter(vis);
  const html = document.documentElement;
  const clone = html.cloneNode(true);
  clone.querySelectorAll("[data-open-app]").forEach(a => a.removeAttribute("href"));
  const markup = clone.outerHTML;
  return {
    state: html.getAttribute("data-share"), lang: html.lang, dir: html.dir, title: document.title, path: location.pathname,
    h1: all("h1").map(h => h.innerText.trim()),
    h1Variant: all("h1 [data-share-v]").map(e => e.getAttribute("data-share-v")),
    notice: all(".notice").length, hint: all(".hint").length, lead: all(".lead").length,
    open: all("[data-open-app]").map(a => a.getAttribute("href")),
    play: all("a[data-cta]").map(a => { const b = a.getBoundingClientRect(); return { href: a.getAttribute("href"), label: a.getAttribute("aria-label"),
      badge: a.querySelector("img").getAttribute("src"), inFirstScreen: b.width > 0 && b.height > 0 && b.bottom <= innerHeight }; }),
    home: all("a.home").map(a => a.getAttribute("href")),
    langToggles: all("[data-share-lang]").map(b => b.textContent.trim()),
    themeLabel: document.querySelector("[data-theme-btn]") ? document.querySelector("[data-theme-btn]").getAttribute("aria-label") : null,
    appStore: !!document.querySelector('a[href*="apps.apple.com"]'),
    brokenImages: Array.from(document.images).filter(i => vis(i) && i.complete && i.naturalWidth === 0).map(i => i.getAttribute("src")),
    injected: window.__xss === 1 || !!document.querySelector("[onerror]"),
    leaked: ["evil.example", "javascript:", "onerror", "__xss", "devtest", "zolo-dev", "%MARKER%"].filter(t => markup.includes(t)),
  };
})()""".replace("%MARKER%", MARKER_ID)

FOCUS_NAME_JS = r"""(a => a ? (a.dataset.cta ? "cta:" + a.dataset.cta : a.hasAttribute("data-theme-btn") ? "theme"
  : a.hasAttribute("data-share-lang") ? "lang" : a.hasAttribute("data-open-app") ? "open" : a.className || a.tagName.toLowerCase()) : null)(document.activeElement)"""


def check_branded_404(r, browser, origin, target):
    """A document navigation to a missing path shows the branded 404.html with HTTP 404, fully styled."""
    browser.navigate(origin + target)
    events = browser.drain()
    docs = [m["params"]["response"] for m in events
            if m["method"] == "Network.responseReceived" and m["params"].get("type") == "Document"]
    status = [d["status"] for d in docs]
    # the document's own 404 is the point; any other failed request (CSS, images, JS) still counts
    doc_urls = {d["url"] for d in docs}
    events = [m for m in events if not (m["method"] == "Network.responseReceived" and m["params"]["response"]["url"] in doc_urls)
              and not (m["method"] == "Log.entryAdded" and (m["params"]["entry"].get("url") or "").split("#")[0] in doc_urls)]
    page = browser.evaluate("({ h1: (document.querySelector('h1') || {}).textContent, css: getComputedStyle(document.querySelector('.btn.primary')).backgroundColor,"
                            " ctas: Array.from(document.querySelectorAll('a[data-cta]')).map(a => a.getAttribute('href')),"
                            " broken: Array.from(document.images).filter(i => i.complete && i.naturalWidth === 0).map(i => i.getAttribute('src')) })")
    problems = runtime_problems(events, origin)
    r.check(status[:1] == [404] and page["h1"] == "Page not found" and page["ctas"] and set(page["ctas"]) == {PLAY_URL},
            "%s: the branded 404 with Google Play (status %s, %s)" % (target, status, page["h1"]))
    r.check(page["css"] not in ("", "rgba(0, 0, 0, 0)") and not page["broken"] and not problems,
            "%s: styles and images load at this depth (%s %s %s)" % (target, page["css"], page["broken"], problems))


def share_checks(r, browser, origin):
    """The share-link fallback, driven like a recipient's phone: Android and iPhone, EN and HE, valid,
    malformed and hostile links, keyboard, no JavaScript, and nested paths through the GitHub-Pages-like 404."""
    r.section("browser: share-link fallback " + SHARE_PATH)
    base = origin + SHARE_PATH

    def load(query="", ua=ANDROID_UA, accept="en-US,en", storage=None):
        browser.set_user_agent(ua, accept)
        # prepare storage on another same-origin document (re-navigating to a URL with a #fragment
        # would be a same-document navigation without a load event)
        browser.navigate(origin + "/robots.txt")
        browser.evaluate("(() => { localStorage.clear(); %s return 0; })()"
                         % ("localStorage.setItem('preferredLang', %s);" % json.dumps(storage) if storage else ""))
        browser.drain()
        browser.navigate(base + query)
        return browser.evaluate(SHARE_INFO_JS)

    browser.set_width(390)
    for what, query, state, carried in SHARE_CASES:
        label = "%s %s" % (SHARE_PATH, what)
        info = load(query)
        problems = runtime_problems(browser.drain(), origin)
        r.check(info["state"] == state, "%s: link is treated as %s (got %s)" % (label, state, info["state"]))
        variant = "generic" if state == "invalid" else state
        r.check(len(info["h1"]) == 1 and info["h1Variant"] == [variant] and not is_hebrew(info["h1"][0]),
                "%s: one visible English <h1>, the %s message (got %s %s)" % (label, variant, info["h1"], info["h1Variant"]))
        r.check(info["notice"] == (1 if state == "invalid" else 0) and info["hint"] == (0 if state == "invalid" else 1) and info["lead"] == 1,
                "%s: explanatory copy matches the state (notice %d, hint %d, lead %d)" % (label, info["notice"], info["hint"], info["lead"]))
        r.check(len(info["play"]) == 1 and info["play"][0]["href"] == PLAY_URL and info["play"][0]["inFirstScreen"],
                "%s: the Google Play CTA is visible in the first screen (%s)" % (label, info["play"]))
        expected_open = [intent_for(*carried)] if carried else []
        r.check(info["open"] == expected_open, "%s: Android 'Open in Zolo' is %s (got %s)" % (label, expected_open or "hidden", info["open"]))
        r.check(info["home"] == ["/en/"], "%s: never a dead end: home link shown" % label)
        r.check(not info["injected"] and not info["leaked"], "%s: nothing from the URL reached the page (injected %s, leaked %s)" % (label, info["injected"], info["leaked"]))
        if "redirect" in what:
            time.sleep(0.3)
        r.check(browser.evaluate("location.pathname") == SHARE_PATH, "%s: the page did not navigate anywhere" % label)
        r.check(not info["brokenImages"] and not problems, "%s: loads cleanly %s %s" % (label, info["brokenImages"], problems))

    deal_q = "?id=%s&deal=%s" % (SHARE_ID, SHARE_DEAL)
    for name, ua in (("iPhone", IPHONE_UA), ("desktop", DESKTOP_UA)):
        info = load(deal_q, ua=ua)
        r.check(info["state"] == "deal" and not info["open"] and len(info["play"]) == 1 and not info["appStore"],
                "%s on %s: no 'Open in Zolo' (Android only), Google Play CTA, no App Store link (%s)" % (SHARE_PATH, name, info["open"]))

    # ---- language: saved preference, then browser language, then English (as the redirect pages) ----
    he = load(deal_q, accept="he-IL,he;q=0.9,en;q=0.8")
    r.check(he["lang"] == "he" and he["dir"] == "rtl" and is_hebrew(he["title"]), "%s: Hebrew browser gets lang=he dir=rtl and a Hebrew title (%s)" % (SHARE_PATH, he["title"]))
    r.check(len(he["h1"]) == 1 and is_hebrew(he["h1"][0]) and he["h1Variant"] == ["deal"], "%s: Hebrew browser sees one Hebrew <h1> (%s)" % (SHARE_PATH, he["h1"]))
    r.check(len(he["play"]) == 1 and is_hebrew(he["play"][0]["label"]) and "Hebrew" in he["play"][0]["badge"] and he["play"][0]["href"] == PLAY_URL,
            "%s: Hebrew Google Play badge and label (%s)" % (SHARE_PATH, he["play"]))
    r.check(he["open"] == [intent_for(SHARE_ID, SHARE_DEAL)] and he["home"] == ["/he/"] and he["langToggles"] == ["EN"],
            "%s: Hebrew page keeps the same intent, links home to /he/, offers EN (%s %s)" % (SHARE_PATH, he["home"], he["langToggles"]))
    r.check(bool(he["themeLabel"]) and is_hebrew(he["themeLabel"]), "%s: Hebrew theme label (%r)" % (SHARE_PATH, he["themeLabel"]))
    saved = load(deal_q, accept="he-IL,he", storage="en")
    r.check(saved["lang"] == "en" and not is_hebrew(saved["h1"][0]), "%s: a saved preferredLang=en beats a Hebrew browser" % SHARE_PATH)
    en = load(deal_q, accept="fr-FR,fr")
    r.check(en["lang"] == "en" and en["dir"] == "ltr" and en["langToggles"] == ["עב"], "%s: other languages fall back to English" % SHARE_PATH)

    # in-page toggle: same URL, preference saved, labels follow
    load(deal_q, accept="he-IL,he")
    href_before = browser.evaluate("location.href")
    browser.evaluate("document.querySelectorAll('[data-share-lang]').forEach(b => { if (b.getClientRects().length) b.click(); }); 0")
    after = browser.evaluate(SHARE_INFO_JS)
    state = browser.evaluate("({ href: location.href, pref: localStorage.getItem('preferredLang'), focus: %s })" % FOCUS_NAME_JS)
    r.check(after["lang"] == "en" and after["dir"] == "ltr" and not is_hebrew(after["h1"][0]) and not is_hebrew(after["title"]),
            "%s: language toggle switches the page to English in place" % SHARE_PATH)
    r.check(state["href"] == href_before and state["pref"] == "en", "%s: toggle keeps the exact shared URL and saves the preference (%s)" % (SHARE_PATH, state))
    r.check(after["themeLabel"] == "Theme: system" and state["focus"] == "lang", "%s: theme label follows the language; focus stays on the toggle (%r, %r)" % (SHARE_PATH, after["themeLabel"], state["focus"]))
    r.check(not runtime_problems(browser.drain(), origin), "%s: language toggle raises no errors" % SHARE_PATH)

    # ---- layout, tracking and theme in both languages ----
    for lang, accept in (("en", "en-US,en"), ("he", "he-IL,he")):
        for query in (deal_q, "?id=%zz"):
            load(query, accept=accept)
            for w in WIDTHS:
                browser.set_width(w)
                o = browser.evaluate(OVERFLOW_JS)
                r.check(o["scrollWidth"] <= o["clientWidth"] and not o["offenders"],
                        "%s%s [%s]: no horizontal overflow at %dpx (%s)" % (SHARE_PATH, query[:12], lang, w, o["offenders"]))
            browser.set_width(390)
        load(deal_q, accept=accept)
        clicks = browser.evaluate(CLICK_JS)
        r.check(len(clicks["clicks"]) == 2 and all(c["fired"] == 1 and (c["payload"] or [None, None])[:2] == ["event", "install_click"]
                and c["payload"][2].get("store") == "google_play" and c["payload"][2].get("placement") == "share"
                and c["payload"][2].get("language") == lang and c["payload"][2].get("transport_type") == "beacon" for c in clicks["clicks"]),
                "%s [%s]: Play CTA emits install_click store=google_play placement=share language=%s (%s)" % (SHARE_PATH, lang, lang, clicks["clicks"]))
        r.check(clicks["noGtag"] == {"fired": 0} and browser.evaluate("location.pathname") == SHARE_PATH, "%s [%s]: tracking is silent without gtag" % (SHARE_PATH, lang))
        theme = browser.evaluate(THEME_JS) or []
        r.check([t["mode"] for t in theme] == ["dark", "light", "system"] and all(is_hebrew(t["label"]) == (lang == "he") for t in theme)
                and [t["themeColor"] for t in theme][:2] == [["#000000", "#000000"], ["#FFFFFF", "#FFFFFF"]],
                "%s [%s]: theme cycles with localized labels and theme-color (%s)" % (SHARE_PATH, lang, theme))
        r.check(not runtime_problems(browser.drain(), origin), "%s [%s]: no runtime problems after interactions" % (SHARE_PATH, lang))

    # ---- keyboard ----
    load(deal_q)   # fresh document, focus at the start
    order = []
    for _ in range(8):
        browser.press_tab()
        order.append(browser.evaluate(FOCUS_NAME_JS))
    wanted = ["theme", "lang", "open", "cta:share"]
    idx = [order.index(x) if x in order else -1 for x in wanted]
    r.check(order[0] == "skip-link" and all(i >= 0 for i in idx) and idx == sorted(idx),
            "%s: Tab order is skip link, theme, language, Open in Zolo, Google Play (got %s)" % (SHARE_PATH, order))
    rings = browser.evaluate("['[data-open-app]:not([hidden])', 'a[data-cta=share]'].map(s => { const el = Array.from(document.querySelectorAll(s)).find(e => e.getClientRects().length); el.focus(); const cs = getComputedStyle(el); return cs.outlineStyle !== 'none' && parseFloat(cs.outlineWidth) >= 2; })")
    r.check(rings == [True, True], "%s: focused Open in Zolo and Google Play show an outline (%s)" % (SHARE_PATH, rings))

    # ---- without JavaScript: English, generic message, install link still works ----
    browser.set_javascript(False)
    browser.navigate(base + deal_q)
    nojs = browser.evaluate(SHARE_INFO_JS)
    browser.set_javascript(True)
    r.check(nojs["state"] is None and nojs["h1Variant"] == ["generic"] and len(nojs["h1"]) == 1 and not is_hebrew(nojs["h1"][0]),
            "%s: without JavaScript one English generic <h1> (%s)" % (SHARE_PATH, nojs["h1"]))
    r.check(len(nojs["play"]) == 1 and nojs["play"][0]["href"] == PLAY_URL and nojs["play"][0]["inFirstScreen"] and not nojs["open"] and not nojs["langToggles"],
            "%s: without JavaScript the Google Play link is in the first screen; no inert buttons (%s)" % (SHARE_PATH, nojs))
    browser.drain()

    # ---- other paths: never a dead end, assets resolve at any depth ----
    info = load("", ua=ANDROID_UA)
    browser.navigate(origin + "/place/index.html?id=" + SHARE_ID)
    alias = browser.evaluate(SHARE_INFO_JS)
    r.check(alias["state"] == "invalid" and not alias["open"] and len(alias["play"]) == 1,
            "/place/index.html?id=...: not the contract path, so the generic page with Google Play (%s)" % alias["state"])
    browser.navigate(origin + "/place" + deal_q)   # the local server, like GitHub Pages, adds the slash with a 301
    slash = browser.evaluate(SHARE_INFO_JS)
    r.check(slash["path"] == SHARE_PATH and slash["state"] == "deal" and slash["open"] == [intent_for(SHARE_ID, SHARE_DEAL)],
            "/place?id=...&deal=...: the slash redirect keeps the query (%s %s)" % (slash["path"], slash["state"]))
    browser.drain()
    for bad in ("/place/abc", "/place/abc/def/", "/place/%2e%2e/%2e%2e/etc/passwd", "/place/index.html/x", "/place/" + SHARE_ID + "/" + SHARE_DEAL):
        check_branded_404(r, browser, origin, bad + "?id=" + SHARE_ID)

    # ---- Digital Asset Links as the verifier fetches it ----
    with urllib.request.urlopen(origin + "/.well-known/assetlinks.json", timeout=5) as resp:
        body = resp.read()
        r.check(resp.status == 200 and resp.geturl() == origin + "/.well-known/assetlinks.json"
                and resp.headers.get_content_type() == "application/json" and json.loads(body.decode("utf-8")) == json.loads(read(".well-known/assetlinks.json")),
                "/.well-known/assetlinks.json is served as application/json with HTTP 200 and no redirect (locally)")


def find_chrome():
    candidates = [os.environ.get("CHROME"), os.environ.get("CHROME_BIN"), os.environ.get("CHROME_PATH"),
                  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                  "/Applications/Chromium.app/Contents/MacOS/Chromium"]
    candidates += [shutil.which(n) for n in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome")]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def runtime_problems(events, origin):
    """Uncaught exceptions, console errors, failed same-origin requests, and any external request
    that actually reached the network (the resolver rule is supposed to make that impossible)."""
    problems = []
    urls = {m["params"]["requestId"]: m["params"]["request"]["url"]
            for m in events if m["method"] == "Network.requestWillBeSent"}
    for m in events:
        p = m.get("params", {})
        if m["method"] == "Runtime.exceptionThrown":
            d = p.get("exceptionDetails", {})
            problems.append("exception: " + (d.get("exception", {}).get("description") or d.get("text", ""))[:200])
        elif m["method"] == "Log.entryAdded":
            e = p.get("entry", {})
            if e.get("level") == "error" and origin in (e.get("url") or "") + e.get("text", ""):
                problems.append("console: " + e.get("text", "")[:200])
        elif m["method"] == "Runtime.consoleAPICalled" and p.get("type") == "error":
            problems.append("console.error: " + " ".join(str(a.get("value", a.get("description", ""))) for a in p.get("args", []))[:200])
        elif m["method"] == "Network.responseReceived":
            resp = p.get("response", {})
            url = resp.get("url", "")
            if url.startswith(origin):
                if resp.get("status", 200) >= 400:
                    problems.append("request %s -> %s" % (url.replace(origin, ""), resp["status"]))
            elif url.startswith("http") and not resp.get("fromDiskCache"):
                problems.append("external request reached the network (isolation broken): " + url[:120])
        elif m["method"] == "Network.loadingFailed":
            url = urls.get(p.get("requestId"), "")
            if url.startswith(origin) and not p.get("canceled"):
                problems.append("request %s failed: %s" % (url.replace(origin, ""), p.get("errorText", "")))
    return problems


# Paths GitHub Pages answers with 404.html and status 404 (production verified with curl on 2026-09-26)
MISSING_PATHS = ("/does-not-exist", "/foo/bar/baz", "/en/does-not-exist", "/he/does-not-exist/", "/place/x",
                 "/place/abc/def", "/does-not-exist?utm_source=check&id=abc", "/missing.txt", "/en/privacy/missing.html",
                 "/assets/common/missing.webp", "/robots.txt/", "/assets/", "/.well-known/")


def http_get(origin, target, method="GET"):
    """One request, redirects not followed: (status, headers, body)."""
    host, port = origin.split("//")[1].split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=5)
    try:
        conn.request(method, target)
        resp = conn.getresponse()
        return resp.status, resp.headers, resp.read()
    finally:
        conn.close()


def server_checks(r):
    """tests/pages_server.py (make serve, and the browser checks) must behave like GitHub Pages:
    the branded 404 with status 404 for any missing path, real files and the slash redirect intact."""
    r.section("local server (GitHub-Pages-like, make serve)")
    not_found = read("404.html").encode("utf-8")

    class Quiet(PagesHandler):
        def log_message(self, *a):
            pass

    server = PagesServer(("127.0.0.1", 0), Quiet)
    origin = "http://127.0.0.1:%d" % server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        for target in MISSING_PATHS:
            status, headers, body = http_get(origin, target)
            r.check(status == 404 and body == not_found and headers.get_content_type() == "text/html" and not headers.get("Location"),
                    "%s: the branded 404.html with HTTP 404, no redirect (got %s, %d bytes)" % (target, status, len(body)))
        status, headers, body = http_get(origin, "/does-not-exist", "HEAD")
        r.check(status == 404 and not body, "HEAD /does-not-exist: 404 without a body (got %s)" % status)

        found = {"/": "index.html", "/en/": "en/index.html", "/he/privacy/": "he/privacy/index.html", "/404.html": "404.html",
                 SHARE_PATH: "place/index.html", SHARE_PATH + "?id=%s&deal=%s" % (SHARE_ID, SHARE_DEAL): "place/index.html",
                 "/robots.txt": "robots.txt", "/sitemap.xml": "sitemap.xml", "/.well-known/assetlinks.json": ".well-known/assetlinks.json",
                 "/assets/common/site.css": "assets/common/site.css", "/assets/common/icon_zolo_96.webp": "assets/common/icon_zolo_96.webp"}
        types = {".txt": "text/plain", ".xml": "application/xml", ".json": "application/json", ".css": "text/css", ".webp": "image/webp"}
        for target, rel in found.items():
            status, headers, body = http_get(origin, target)
            want = types.get(Path(rel).suffix, "text/html")
            r.check(status == 200 and body == (ROOT / rel).read_bytes() and headers.get_content_type() == want,
                    "%s: 200 %s with %s (got %s %s)" % (target, want, rel, status, headers.get_content_type()))

        for target, location in (("/place?id=%s&deal=%s" % (SHARE_ID, SHARE_DEAL), "/place/?id=%s&deal=%s" % (SHARE_ID, SHARE_DEAL)),
                                 ("/place", "/place/"), ("/en", "/en/"), ("/he/terms?x=1", "/he/terms/?x=1")):
            status, headers, _ = http_get(origin, target)
            r.check(status == 301 and headers.get("Location") == location,
                    "%s: 301 to %s, query intact (got %s %s)" % (target, location, status, headers.get("Location")))
    finally:
        server.shutdown()
        server.server_close()

    # `make serve` itself: starts, prints its URL, serves the branded 404, stops cleanly on Ctrl-C
    proc = subprocess.Popen([sys.executable, str(ROOT / "tests/pages_server.py"), "0"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        ready, _, _ = select.select([proc.stdout], [], [], 10)
        line = proc.stdout.readline().decode("utf-8", "replace") if ready else ""
        m = re.search(r"http://127\.0\.0\.1:(\d+)/", line)
        r.check(bool(m), "pages_server.py announces its URL on startup (%r)" % line.strip())
        if m:
            status, _, body = http_get("http://127.0.0.1:" + m.group(1), "/place/abc/def?id=x")
            r.check(status == 404 and body == not_found, "pages_server.py serves the branded 404 with status 404 (got %s)" % status)
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        r.check(proc.returncode == 0, "pages_server.py exits cleanly on Ctrl-C (status %s)" % proc.returncode)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()


def browser_checks(r):
    r.section("browser")
    chrome = find_chrome()
    if not chrome:
        r.check(False, "Chrome/Chromium found (set CHROME=/path/to/chrome)")
        return

    class Handler(PagesHandler):
        def log_message(self, *a):
            pass

    class QuietServer(PagesServer):
        def handle_error(self, request, client_address):
            pass  # Chrome drops connections mid-response when it navigates; not a site problem

    profile = tempfile.mkdtemp(prefix="zolo-site-check-")
    browser = None
    server = None
    current = "(startup)"
    try:
        # Launch Chrome before the server thread exists (preexec_fn forks a single-threaded process).
        browser = Chrome(chrome, profile)
        server = QuietServer(("127.0.0.1", 0), Handler)
        origin = "http://127.0.0.1:%d" % server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        with urllib.request.urlopen(origin + "/robots.txt", timeout=5) as resp:   # proves serve_forever is running
            r.check(resp.status == 200, "local server serves the repository")
        browser.open_page()
        pages = [p for p in ALL_PAGES if not p.get("not_found") and not p.get("share")] + [NOT_FOUND_PAGE]
        for page in pages:
            p = current = page["path"]
            browser.set_width(390)
            browser.navigate(origin + p)
            if page.get("redirect"):
                final = browser.wait_for_location_change(p)
                r.check(final.startswith(("/en/", "/he/")), "%s: redirects to a localized page (got %r)" % (p, final))
                if p == "/":
                    browser.navigate(origin + "/?utm_source=check&x=1")
                    final = browser.wait_for_location_change(p)
                    r.check("utm_source=check" in final and final.startswith(("/en/", "/he/")), "/: redirect preserves the query string (got %r)" % final)
                r.check(not runtime_problems(browser.drain(), origin), "%s: no runtime problems during redirect" % p)
                continue

            load_events = browser.drain()
            problems = runtime_problems(load_events, origin)
            r.check(not problems, "%s: no JS errors, console errors or failed requests on load: %s" % (p, "; ".join(problems)))
            if page.get("landing") and page["lang"] == "en":
                # Positive proof that the DNS blackhole is active: the GA tag request must be attempted and fail to resolve.
                ga = [m for m in load_events if m["method"] == "Network.loadingFailed" and "NAME_NOT_RESOLVED" in m["params"].get("errorText", "")]
                r.check(bool(ga), "%s: external requests are blackholed (GA attempt failed with ERR_NAME_NOT_RESOLVED)" % p)
            info = browser.evaluate(PAGE_INFO_JS)
            r.check(not info["brokenImages"], "%s: no broken images %s" % (p, info["brokenImages"]))
            r.check(info["lang"] == page["lang"], "%s: runtime lang %s" % (p, page["lang"]))
            localized = "counterpart" in page
            if page["lang"] == "he":
                r.check(info["dir"] == "rtl", "%s: runtime dir=rtl" % p)
                r.check(info["langBtnText"] == "EN", "%s: language toggle shows EN (got %r)" % (p, info["langBtnText"]))
                r.check(bool(info["themeLabel"]) and is_hebrew(info["themeLabel"]), "%s: theme label localized at runtime (%r)" % (p, info["themeLabel"]))
            elif localized:
                r.check(info["langBtnText"] == "עב", "%s: language toggle shows עב (got %r)" % (p, info["langBtnText"]))
            r.check(bool(info["themeLabel"]), "%s: theme control present with a label" % p)

            for w in (WIDTHS if page.get("landing") or page.get("not_found") else WIDTHS_SECONDARY):
                browser.set_width(w)
                o = browser.evaluate(OVERFLOW_JS)
                r.check(o["scrollWidth"] <= o["clientWidth"] and not o["offenders"],
                        "%s: no horizontal overflow at %dpx (scroll %d, client %d, offenders %s)" % (p, w, o["scrollWidth"], o["clientWidth"], o["offenders"]))
            browser.set_width(390)

            expected_ctas = {"hero", "bottom"} if page.get("landing") else ({"404"} if page.get("not_found") else set())
            got = {c["placement"] for c in info["ctas"]}
            r.check(got == expected_ctas, "%s: CTA placements at runtime %s" % (p, sorted(got)))
            if expected_ctas:
                clicks = browser.evaluate(CLICK_JS)
                click_problems = runtime_problems(browser.drain(), origin)
                r.check(not click_problems, "%s: CTA clicks raise no errors %s" % (p, click_problems))
                for c in clicks["clicks"]:
                    payload = c["payload"] or []
                    params = payload[2] if len(payload) > 2 else {}
                    r.check(c["fired"] == 1, "%s: CTA %s click emits exactly one event (got %d)" % (p, c["placement"], c["fired"]))
                    r.check(payload[:2] == ["event", "install_click"], "%s: CTA %s event is install_click" % (p, c["placement"]))
                    href = next((x["href"] for x in info["ctas"] if x["placement"] == c["placement"]), "")
                    store = next((k for k, host in STORE_HOSTS.items() if host in href), "other")
                    r.check(params.get("store") == store and params.get("placement") == c["placement"] and params.get("language") == page["lang"],
                            "%s: CTA %s payload store/placement/language (%s)" % (p, c["placement"], params))
                    r.check(params.get("transport_type") == "beacon", "%s: CTA %s event uses beacon transport (survives navigation)" % (p, c["placement"]))
                r.check(clicks["noGtag"] == {"fired": 0}, "%s: CTA click without gtag stays silent" % p)
                r.check(browser.evaluate("location.pathname") == p, "%s: CTA test did not navigate away" % p)

            theme = browser.evaluate(THEME_JS)
            r.check(bool(theme), "%s: theme toggle exists and cycles" % p)
            if theme:
                modes = [t["mode"] for t in theme]
                r.check(modes == ["dark", "light", "system"], "%s: theme cycles dark/light/system (got %s)" % (p, modes))
                medias = [t["media"] for t in theme]
                if localized:   # every localized page has at least the themed TikTok icon
                    r.check(medias[0] == ["all"] and medias[1] == ["not all"] and medias[2] == ["(prefers-color-scheme: dark)"],
                            "%s: picture sources follow the theme (%s)" % (p, medias))
                colors = [t["themeColor"] for t in theme]
                r.check(colors[0] in ([], ["#000000", "#000000"]) and colors[1] in ([], ["#FFFFFF", "#FFFFFF"]),
                        "%s: theme-color follows a forced theme (%s)" % (p, colors[:2]))
                if page["lang"] == "he":
                    r.check(all(is_hebrew(t["label"]) for t in theme), "%s: theme labels stay Hebrew while cycling" % p)
            r.check(not runtime_problems(browser.drain(), origin), "%s: no runtime problems after interactions" % p)

            if "counterpart" in page:
                browser.events = [m for m in browser.events if m["method"] != "Page.loadEventFired"]
                browser.evaluate("document.querySelector('[data-lang-btn]').click(); 0")
                browser.wait_event("Page.loadEventFired")
                landed = browser.evaluate("location.pathname")
                r.check(landed == page["counterpart"], "%s: language toggle navigates to %s (got %s)" % (p, page["counterpart"], landed))
                r.check(not runtime_problems(browser.drain(), origin), "%s: counterpart page loads cleanly" % page["counterpart"])
            if page.get("landing"):
                # Keyboard: Tab from the top must reach theme control, language toggle and hero CTA, in that order.
                browser.navigate(origin + p)   # fresh document, focus at the start
                order = []
                for _ in range(8):
                    browser.press_tab()
                    order.append(browser.evaluate("(a => a ? (a.dataset.cta ? 'cta:' + a.dataset.cta : a.hasAttribute('data-theme-btn') ? 'theme' : a.hasAttribute('data-lang-btn') ? 'lang' : a.className || a.tagName.toLowerCase()) : null)(document.activeElement)"))
                wanted = ["theme", "lang", "cta:hero"]
                idx = [order.index(x) if x in order else -1 for x in wanted]
                r.check(all(i >= 0 for i in idx) and idx == sorted(idx), "%s: keyboard focus order reaches theme, language, hero CTA (got %s)" % (p, order))
                r.check(order[0] == "skip-link", "%s: first Tab stop is the skip link (got %r)" % (p, order[0]))
                visible = browser.evaluate("(() => { const el = document.querySelector('a[data-cta=hero]'); el.focus(); const cs = getComputedStyle(el); return cs.outlineStyle !== 'none' && parseFloat(cs.outlineWidth) >= 2; })()")
                r.check(visible, "%s: focused hero CTA shows an outline" % p)
                # Without JavaScript the pitch and the install link must still work (last: leaves JS re-enabled).
                browser.set_javascript(False)
                browser.navigate(origin + p)
                nojs = browser.evaluate("({ h1: !!document.querySelector('h1'), cta: (() => { const a = document.querySelector('a[data-cta=hero]'); if (!a) return false; const b = a.getBoundingClientRect(); return b.width > 0 && b.height > 0 && b.top < innerHeight; })() })")
                browser.set_javascript(True)
                r.check(nojs == {"h1": True, "cta": True}, "%s: without JavaScript the headline and hero install link are visible in the first screen (%s)" % (p, nojs))
                browser.drain()

        # ---- missing paths: the branded 404 as GitHub Pages serves it, at any depth, language prefix, query or fragment ----
        current = "missing paths"
        browser.set_width(390)
        for target in ("/does-not-exist", "/foo/bar/baz", "/en/does-not-exist", "/he/does-not-exist/",
                       "/does-not-exist?utm_source=check&id=abc", "/does-not-exist#section", "/foo/bar.html?x=1#y"):
            check_branded_404(r, browser, origin, target)
        # a missing asset still counts as a failed request: the 404.html body must not mask it
        browser.navigate(origin + "/404.html")
        browser.drain()
        loaded = browser.evaluate("new Promise(ok => { const i = new Image(); i.onload = () => ok(true); i.onerror = () => ok(false);"
                                  " i.src = '/assets/common/missing-check.webp'; })")
        problems = runtime_problems(browser.drain(), origin)
        r.check(loaded is False and any("/assets/common/missing-check.webp -> 404" in x for x in problems),
                "a missing image fails to load and is reported as a failed request (%s %s)" % (loaded, problems))

        current = SHARE_PATH
        share_checks(r, browser, origin)

    except Exception as e:  # keep the report useful when the browser layer itself breaks
        r.check(False, "browser checks ran to completion (while testing %s: %s: %s)" % (current, type(e).__name__, e))
        if browser:
            browser.stderr.seek(0)
            tail = browser.stderr.read().decode("utf-8", "replace")[-1500:]
            print("    Chrome stderr tail:\n    " + tail.replace("\n", "\n    "))
    finally:
        if browser:
            browser.close()
        if server:
            server.shutdown()
            server.server_close()
        shutil.rmtree(profile, ignore_errors=True)


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--static", action="store_true", help="skip the headless-browser checks")
    ap.add_argument("-v", "--verbose", action="store_true", help="print passing checks too")
    args = ap.parse_args()
    # SIGTERM (CI cancellation, kill) behaves like Ctrl-C so the finally blocks close Chrome and remove the profile
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    r = Report(args.verbose)
    static_checks(r)
    server_checks(r)
    if not args.static:
        browser_checks(r)
    print("\n%d checks passed, %d failed" % (r.passed, len(r.failures)))
    if r.failures:
        print("\nFailures:")
        for f in r.failures:
            print("  - " + f.splitlines()[0])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
