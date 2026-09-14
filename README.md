# zolo-deals.com

Static site for the Zolo app, served by GitHub Pages from `main` (`CNAME`, `.nojekyll`).
No build step: edit the HTML/CSS/JS in place. Localized pages live under `en/` and `he/`
(`index.html`, `privacy/`, `terms/`, `delete/`, `accessibility/`); the root-level folders are
language redirects, `fb/`, `ig/`, `tt/` are social vanity redirects, `404.html` is the catch-all.

## Before deploying

```bash
make check          # or: python3 tests/check_site.py
```

Runs in about a minute with only Python 3.9+ and Chrome. It validates metadata, hreflang,
JSON-LD, sitemap, asset references and size budgets, accessibility basics, and then drives the
real pages in headless Chrome: JS/console errors, failed requests, horizontal overflow at
320/390/768/1280px, install-CTA tracking payloads, theme and language toggles. See the docstring
in `tests/check_site.py` for options (`--static`, `-v`, `VNU_JAR`, `CHROME`).
