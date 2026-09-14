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
External hosts are unresolvable during the browser run, so nothing reaches GA.

Only the Python standard library and a Chrome/Chromium binary are needed
(set CHROME=/path/to/chrome if it is not found automatically). If the Nu HTML checker
is available (VNU_JAR=/path/to/vnu.jar), it is run as an extra static check.
Exit status is non-zero when any check fails.
"""
import argparse
import json
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from html.parser import HTMLParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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
ALL_PAGES = localized_pages() + redirect_pages() + [NOT_FOUND_PAGE]


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
        # text inside aria-hidden subtrees does not contribute to accessible names
        hidden = False
        for el in self.stack:
            hidden = hidden or el["attrs"].get("aria-hidden") == "true"
            if not hidden:
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
    if page.get("not_found"):
        # GitHub Pages serves 404.html for any missing path, including nested ones,
        # so every URL in it must be root-relative or absolute.
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
        # close_fds=False keeps the dup2'd fds 3/4 alive in the child (Python would otherwise close them
        # after preexec_fn); the child inherits a few harmless extra descriptors.
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=self.stderr,
                                     close_fds=False, preexec_fn=lambda: (os.dup2(r_in, 3), os.dup2(w_out, 4)))
        os.close(r_in)
        os.close(w_out)
        self.w = w_in
        self.rd = r_out
        self.buf = b""
        self.next_id = 0
        self.events = []
        self.session = None

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


def browser_checks(r):
    r.section("browser")
    chrome = find_chrome()
    if not chrome:
        r.check(False, "Chrome/Chromium found (set CHROME=/path/to/chrome)")
        return

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(ROOT), **k)

        def log_message(self, *a):
            pass

    class QuietServer(ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            pass  # Chrome drops connections mid-response when it navigates; not a site problem

    # Python 3.9's mimetypes lacks a few types; Chrome sniffs images but keep it explicit.
    Handler.extensions_map.update({".webp": "image/webp", ".svg": "image/svg+xml", ".js": "text/javascript",
                                   ".css": "text/css", ".xml": "application/xml", ".txt": "text/plain"})

    profile = tempfile.mkdtemp(prefix="zolo-site-check-")
    browser = None
    server = None
    try:
        # Launch Chrome before the server thread exists (preexec_fn forks a single-threaded process).
        browser = Chrome(chrome, profile)
        server = QuietServer(("127.0.0.1", 0), Handler)
        origin = "http://127.0.0.1:%d" % server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        with urllib.request.urlopen(origin + "/robots.txt", timeout=5) as resp:   # proves serve_forever is running
            r.check(resp.status == 200, "local server serves the repository")
        browser.open_page()
        pages = [p for p in ALL_PAGES if not p.get("not_found")] + [NOT_FOUND_PAGE]
        current = "(startup)"
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

            problems = runtime_problems(browser.drain(), origin)
            r.check(not problems, "%s: no JS errors, console errors or failed requests on load:\n    %s" % (p, "\n    ".join(problems)))
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
    r = Report(args.verbose)
    static_checks(r)
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
