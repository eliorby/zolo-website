# Roadmap

Plans and status for the zolo-deals.com website. This file is public: it holds no private plans, account details or personal notes.

## Conversion to the standard project layout

The project is moving to one handoff kept outside the repo, this roadmap, and a repo `CLAUDE.md` built from a template. Each step is its own round; exact wording and every move or deletion are approved before they happen.

| # | Step | Status |
|---|---|---|
| 1 | Paths and names | done: no rename; no data folder exists; every share-link contract reference points to `docs/share-link-contract.md` in the zolo-flutter repo |
| 2 | Consolidation and handoff install | done: one consolidated handoff installed outside the repo; the ordered backlog below added |
| 3 | Minimal consistency edits to the repo `CLAUDE.md` | not applicable: the repo has no `CLAUDE.md`; step 8 creates one |
| 4 | Archive round | not started |
| 5 | Runtime layout | not started (planned together with step 4) |
| 6 | Runtime README | not started |
| 7 | Backups | not started |
| 8 | Repo `CLAUDE.md` from the template, with the auto-memory harvest and the repo settings | not started |
| 9 | SPEC round | not started |
| 10 | Close-out | not started |

## Backlog

In order of work; the conversion above comes first. Each item gives its order and why now, the desired outcome, dependencies, acceptance criteria, status and references. Status is one of: not started, in progress, blocked, awaiting verification, awaiting decision, done.

### 1. Re-establish the repository and live state (P0)
- **Order and why now:** first, because every later item starts from a known state.
- **Desired outcome:** the checkout, the tags and the live site are known before any change.
- **Dependencies:** none.
- **Acceptance criteria:**
  - The branch, HEAD, working tree and tags are inspected.
  - The quality gate is re-run.
  - After a push, the live site is checked against the repo.
- **Status:** done (2026-11-09).
  - The site serves `4651133`, and the GitHub Pages and `site-check` runs for it succeeded.
  - The five tags on GitHub match the local ones.
  - `make check-static`: 1678 passed. `make check`: 2261 passed, 0 failed.
- **References:** tags `v1.0.0` to `v2.1.0`; commits `709fb14` and `4651133`; the session reports of 2026-11-09 (kept outside the repo).

### 2. Finish the read-only asset and metadata audit (P1)
- **Order and why now:** second, because the asset system (items 3 to 7) is designed from it.
- **Desired outcome:** an accurate inventory of every page's social metadata and of every image the site uses, with no change to the site.
- **Dependencies:** none for the remaining checks.
- **Acceptance criteria:**
  - **Per route:** `og:title`, `og:description`, `og:url` and `og:image` (with its dimensions and type), the Twitter metadata, canonical and hreflang, and the JSON-LD image references.
  - **Images:** every image reference in HTML, CSS and JS, with each asset's dimensions, size, variants and duplicates.
  - **Asset hygiene:** imagery that shows the previous app version, unreferenced files, and the gate's limits.
  - **`/place/`:** its static link preview, compared with the language of the message that shares it.
  - **Roles:** which files are live dependencies, source masters, store or social-only assets, or unused.
- **Status:** in progress.
  - **Done** (2026-10-12, at `ccb3177`):
    - image references and roles; dimensions, sizes, formats and variants; lineage;
    - imagery of the previous app version; unreferenced files; the gate's limits;
    - the `og:image`, `twitter:image` and JSON-LD image references.
  - **Remaining:** the text metadata per route, and the `/place/` link-preview review.
- **References:** the asset audit report of 2026-10-12 (kept outside the repo); the budgets in `tests/check_site.py` (lines 50–54).

### 3. Propose the smallest coherent asset system, then stop for approval (P2)
- **Order and why now:** third. Nothing is built before the direction is approved.
- **Desired outcome:** an approved proposal for the site's images and link previews.
- **Dependencies:** item 2. Other inputs are tracked outside this file.
- **Acceptance criteria:** the proposal covers:
  - the asset families that live pages need;
  - the source and master structure, templates, a deterministic local generator, export formats and dimensions, a manifest, file names and the regeneration command;
  - copy and visual options for the home page, and for a neutral `/place/` link preview;
  - whether link-preview images stay at 1024×500 or get a dedicated 1200×630 card, including the cache effects of keeping or versioning their URLs;
  - an exact screenshot checklist: screen, app state, language, theme, map and sheet state, orientation and status bar.

  No final design, metadata change or broad asset replacement happens before approval.
- **Status:** blocked.
- **References:** item 2; `place/index.html` lines 33–42 (the share page's static preview).

### 4. Build the generator and templates (P3)
- **Order and why now:** after item 3 is approved.
- **Desired outcome:** local, reproducible static outputs that work on GitHub Pages:
  - no paid service and no backend;
  - source images replaceable without rebuilding every export by hand;
  - existing file paths kept where practical.
- **Dependencies:** item 3, approved.
- **Acceptance criteria:**
  - One documented command generates the outputs from the masters.
  - Exports are converted to sRGB.
  - Link-preview images are JPEG or PNG (the gate rejects WebP).
  - The tooling is chosen after inspecting what the repository and machine already have, for example local HTML and CSS rendered by headless Chrome, or SVG.
  - `make check` passes.
- **Status:** not started.
- **References:** `tests/check_site.py` line 281.

### 5. Capture current app screenshots (P4)
- **Order and why now:** after item 3, which supplies the checklist.
- **Desired outcome:** authentic screenshots of the current app for every state the checklist names, with no fabricated UI.
- **Dependencies:** item 3's checklist. Other inputs are tracked outside this file.
- **Acceptance criteria:** every checklist entry is captured in each language and theme it names.
- **Status:** blocked.
- **References:** item 3.

### 6. Generate and integrate the final assets (P5)
- **Order and why now:** after items 4 and 5.
- **Desired outcome:** the approved images are live on the site, with matching captions, alt text and metadata.
- **Dependencies:** item 3 (approved), item 4 and item 5.
- **Acceptance criteria:**
  - The outputs are optimised within the gate's budgets: at most 250 KB per landing image, 1000 KB of light-theme payload per landing page, and 40 KB for icons shown at 64 px or less.
  - Captions, alt text and metadata are updated in EN and HE, and the manifest and docs are updated.
  - Semantic tests cover dimensions, route metadata, asset references and outdated content, without pixel-perfect assertions.
  - `make check` passes.
- **Status:** blocked.
- **References:** `tests/check_site.py` lines 50–54; the screenshot captions in `en/index.html` and `he/index.html`.

### 7. Verify, review and deploy (P6)
- **Order and why now:** after item 6.
- **Desired outcome:** the new assets are reviewed, approved and live.
- **Dependencies:** item 6, and approval of the outputs.
- **Acceptance criteria:**
  - `make check-static`, `make check` and `git diff --check` pass.
  - A visual inspection at desktop and narrow mobile widths, in EN and HE, in light and dark.
  - The outputs are approved before the manual push.
  - After the push, the live site, the `/place/` metadata and the rendered link previews are verified, allowing for WhatsApp, Facebook and LinkedIn caches, and the results are recorded.
- **Status:** not started.
- **References:** `README.md` ("Before deploying").

### 8. Circular manual carousel navigation (P7)
- **Order and why now:** independent of items 2 to 7, and only if approved.
- **Desired outcome:** on manual navigation, the screenshot carousel wraps from last to first and from first to last.
- **Dependencies:** a decision to do it.
- **Acceptance criteria:**
  - The current carousel is inspected first: a CSS scroll-snap scroller on narrow screens (`assets/common/site.css`).
  - No autoplay.
  - Swipe, keyboard, focus and reduced-motion behaviour stay correct.
  - `make check` passes.
- **Status:** awaiting decision (not approved).
- **References:** `assets/common/site.css` (the carousel rules).

### 9. iOS links, once an iOS app exists
- **Order and why now:** last, because it waits for an iOS app.
- **Desired outcome:** iPhone users get an App Store link, and `/place/` links open the app through Universal Links.
- **Dependencies:** a real Apple Team ID and a published iOS app. Neither exists.
- **Acceptance criteria:**
  - `.well-known/apple-app-site-association` is made from `association/apple-app-site-association.template.json` with the real Team ID, covering only `/place` and `/place/`.
  - App Store links are added to the landing pages' hero and bottom CTAs.
  - The URL is added to `STORE_URLS` in `tests/check_site.py`, and the JSON-LD `operatingSystem` is updated.
  - After deploying, the file is checked through Apple's CDN (`https://app-site-association.cdn-apple.com/a/v1/zolo-deals.com`).
  - `make check` passes.
- **Status:** blocked.
- **References:** `README.md` lines 19–21; `tests/check_site.py` line 56.
