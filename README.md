# zolo-deals.com

Static site for the Zolo app, served by GitHub Pages from `main` (`CNAME`, `.nojekyll`).
No build step: edit the HTML/CSS/JS in place. Localized pages live under `en/` and `he/`
(`index.html`, `privacy/`, `terms/`, `delete/`, `accessibility/`); the root-level folders are
language redirects, `fb/`, `ig/`, `tt/` are social vanity redirects, `404.html` is the catch-all.

## Share links (`/place/`)

The app shares `https://zolo-deals.com/place/?id=<placeId>[&deal=<dealKey>]`. The contract is owned by
the app session (`~/project-runtimes/zolo-app-runtime/share-link-contract.md`): change it there first,
never here. With the app installed and App Links verified, Android opens the app directly; otherwise
`place/index.html` is the fallback. `assets/common/place.js` validates the query exactly like the app
and only selects static copy (EN/HE, place/deal/invalid); nothing from the URL is written into the page.
It is `noindex`, canonical `/place/`, not in the sitemap. Paths below `/place/` get `404.html`.

- `.well-known/assetlinks.json`: Android App Links for `com.zolo.deals`, Google Play **App Signing**
  certificate only (never the upload or debug key). The app's intent filter limits it to `/place[/]`.
- iOS: no live `apple-app-site-association` until the real Team ID exists. Fill
  `association/apple-app-site-association.template.json`, save it as `.well-known/apple-app-site-association`
  (no extension); `make check` rejects placeholder Team IDs and any path other than `/place[/]`.
- After deploying, verify on the live host: `curl -sI https://zolo-deals.com/.well-known/assetlinks.json`
  (200, `application/json`, no redirect), Google's checker
  `https://digitalassetlinks.googleapis.com/v1/statements:list?source.web.site=https://zolo-deals.com&relation=delegate_permission/common.handle_all_urls`,
  and that `https://zolo-deals.com/place?id=x` redirects to `/place/?id=x` with the query intact.

## Before deploying

```bash
make check          # or: python3 tests/check_site.py
```

Runs in about a minute with only Python 3.9+ and Chrome. It validates metadata, hreflang,
JSON-LD, sitemap, asset references and size budgets, accessibility basics, and then drives the
real pages in headless Chrome: JS/console errors, failed requests, horizontal overflow at
320/390/768/1280px, install-CTA tracking payloads, theme and language toggles. See the docstring
in `tests/check_site.py` for options (`--static`, `-v`, `VNU_JAR`, `CHROME`).
