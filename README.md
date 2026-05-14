# Phantom

Async OSINT username checker. Given a username, Phantom queries ~99 curated sites in parallel and reports where that handle exists — *and who that handle belongs to*.

> **Position: Maigret tells you *where*; Phantom tells you *who*.**
> Phantom isn't trying to win on raw site count. It's an accuracy-first investigation tool with confidence scoring, identity disambiguation, deep public-data enrichment, an interactive Maltego-style dossier graph, and an explicit "why this score" trace on every result.

## Designed for accuracy first

A `[ FOUND ]` requires positive evidence (a presence marker in the response body or a clean status code), and any "page exists / user not found" page is correctly classified as MISSING. Bot walls and ambiguous responses become `[   ?   ]` rather than risk a false positive.

Zero-false-positives is a hard constraint, audited via:

- Two-sided detection rule: `presence_text` must match AND `absence_text` must NOT match
- URL-echo guard in `discover_site.py` (suggested patterns get tested against a fake handle before being accepted)
- Per-site `validate_sites.py` schema check
- `--self-check` canary system (probes 27+ curated handles weekly to catch site drift)

## At a glance

| Feature | Maigret | Phantom |
|---|---|---|
| Site count | 3000+ (top 500 default) | ~99 (every one detection-audited) |
| False-positive rate | permissive | zero by construction |
| Confidence scoring per result | — | 0–100 + tier + evidence trace |
| Identity disambiguation (cluster real-person vs squatters) | — | weighted graph + photo-hash + stylometry |
| Recursive cross-link discovery | iterative | iterative + source-weighted boost (Keybase +30, GitHub +20, …) |
| `--parse URL` (give a profile URL, scan its derived handle) | — | yes |
| Anti-block backends | curl_cffi via FlareSolverr | aiohttp + curl_cffi + Playwright (in-process) |
| HTML report | functional | editorial dossier (Instrument Serif + IBM Plex) with: |
| · Interactive force-directed graph | external D3 | inline, pan/zoom/drag, two-island confirmed/unrelated layout |
| · Profile photos as graph nodes | — | yes (with PIL-variance default-PFP suppression) |
| · Per-account "Why this score" trace | — | every signal that fired with its weight |
| · Linked-account chips per card | — | yes |
| · "Confirmed missing on …" chip list | — | yes |
| · Real-name / Nickname detector | — | classifies display_name strings + bio first-line |
| Export formats | HTML, PDF, XMind, JSON, CSV, TXT, D3 | HTML, PDF, JSON, Markdown, CSV, **Mermaid mindmap** |
| Wayback Machine historical lookup | — | `--wayback` |
| GitHub deep-dive (orgs, starred, commit-email leak) | — | `--github-deep` |
| Profile-photo OCR | — | `--photo-ocr` (Tesseract, optional) |
| Stylometric bio fingerprint (impostor detection) | — | always-on |
| MISSING tier (confirmed vs uncertain) | — | yes |
| Site auto-discovery tool | — | `discover_site.py` (probes a URL pattern, proposes a sites.json entry) |
| Canary-handle self-check + auto-disable suggestion | — | `--self-check` |
| Embeddable Python library | yes | `pip install phantom-osint` → `from phantom import Phantom` |
| AI summarizer | yes (OpenAI) | deliberately omitted — no APIs needed |

## What Phantom extracts

- ~99 hand-picked sites across dev, social, media, gaming, forum, and other (every site detection-audited; zero false positives in the latest audit)
- **Variant engine**: one input expands to dozens of plausible handles (separators, number/prefix/suffix variants, smart word splits, blind position-insertion for short tokens, first/last name permutations, leetspeak substitutions, email-to-handle). Use `--exact` to disable.
- **Confidence-ranked output**: every FOUND result is scored 0–100 with full **evidence trace** (`+50 verified badge`, `+30 photo matches another account`, `−15 photo doesn't match any cluster`, …) and grouped into three tiers — `[ VERIFIED IDENTITY ]`, `[ LIKELY MATCH ]`, and `[ POSSIBLE IMPOSTOR ]`. Impostors are collapsed by default; use `--show-all` to surface them.
- **Identity disambiguation**: weighted similarity graph clusters FOUND accounts by photo hash, cross-links in bios, fuzzy display name, follower-tier, location, stylometric bio fingerprint (cosine similarity of punctuation / capitalization / emoji habits), and source-weighted handle confidence (Keybase proof +30, GitHub `x_handle` +20, JSON-LD sameAs +15, website +10, Linktree +5).
- **Exportable reports**: `--export FILE` writes to **HTML** (the editorial dossier with the interactive graph), **PDF**, **JSON**, **Markdown**, **CSV**, or **Mermaid mindmap** (`.mmd` — opens in GitHub / Obsidian / VS Code).
- **Public profile enrichment**: every FOUND site is scanned for the public profile data the SSR'd page already exposes — display name, bio, photo, follower / following / post counts, location, joined date, verified / private flags, bio language, website, plus per-platform extras (Twitter lists, TikTok hearts, GitHub pinned repos, YouTube total views, Reddit karma split, …). No auth, no extra HTTP calls.
- **Reliability built-in**: every `(variant × site)` check runs through a shared task pool (so stragglers don't block subsequent variants), transient failures (timeouts, 5xx, transport errors) get one retry, and stable answers are cached on disk for an hour so re-runs are near-instant.
- **Identity aggregation**: every FOUND account contributes to one "Overall identity" summary — display name, all photos, vote-counted locations, geo-region inference, total followers across platforms, oldest joined date, verified-on-N-of-M. Photo correlation (perceptually-hashed profile pictures matching across sites) runs on top as a separate "Photo-matched accounts" view. Disable with `--no-identity`.
- **Watch mode**: `--watch` snapshots the FOUND set after each scan and diffs against the previous run. Combined with `--quiet` and a cron job, you get a daily "what changed" digest — new accounts, removed accounts, follower deltas, bio updates.
- Two HTTP backends:
  - `aiohttp` for sites with no bot protection (default, fast)
  - `curl_cffi` with **Chrome TLS impersonation** for sites flagged with `protection: ["tls_fingerprint"]` — defeats Cloudflare/Akamai/AWS WAF
- Two detection methods per site: HTTP **status** code or page **message** text
- Two-sided text matching: a hit must produce a `presence_text` match AND not match an `absence_text` pattern
- Per-site headers (custom User-Agent, Accept, etc.) — site headers replace the defaults when a `User-Agent` is present, so e.g. YouTube can be queried with `curl/8.6.0` without other browser headers leaking through
- Optional `request_method: POST` + `request_body` for GraphQL/JSON-API endpoints (e.g. AniList)
- Tri-state output: **found** / **missing** / **unknown**
- Typical full scan: **2–4 seconds**

## Quick start

Three install routes — pick one.

**A) pip install (preferred):**

```bash
pip install phantom-osint                    # if/when published to PyPI
playwright install chromium                  # one-time; needed for --js-render / PDF
phantom <username> --found-only
```

**B) Docker (for proxy/Tor isolation):**

```bash
docker build -t phantom .
docker run --rm phantom <username>
# With persistent cache:
docker run -v $PWD/.phantom-cache:/home/phantom/.cache/phantom \
           --rm phantom <username>
```

**C) Git clone (development):**

```bash
git clone git@github.com:hamaffs/Phantom-.git phantom
cd phantom
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
sudo ln -s "$PWD/phantom" /usr/local/bin/phantom
phantom <username> --found-only
```

Optional: `apt install tesseract-ocr && pip install pytesseract` enables `--photo-ocr`.

## Example output

```
$ phantom <username>
Phantom: trying 33 variants of '<username>' across 63 sites = 2079 requests

[ FOUND ] 9
  GitHub         https://github.com/<username> (http=200)  [<username>]
  Pastebin       https://pastebin.com/u/<username> (http=200, presence)  [<username>]
  YouTube        https://www.youtube.com/@<username>/about (http=200, presence)  [<username>]
  TikTok         https://www.tiktok.com/@<user.name> (http=200, presence)  [<user.name>]
  Twitter        https://x.com/<username> (http=200, presence)  [<username>]
  Twitch         https://m.twitch.tv/<username> (http=200, presence)  [<username>]
  Threads        https://www.threads.com/@<username> (http=200, presence)  [<username>]
  Instagram      https://www.instagram.com/<username>/ (http=200, presence)  [<username>]
  Facebook       https://www.facebook.com/<user.name>/ (http=200, presence)  [<username>]

[   ?   ] 165  (use --export to see details)
[MISSING] 1806

1980 checks across 33 variants in 84.0s
```

`[ FOUND ]` is the only section that lists rows. `[ ? ]` and `[MISSING]` are counts only — they can hit the hundreds on a multi-variant run, so the per-row detail is kept for the export reports (HTML/JSON/Markdown). Pipe `--found-only` if you don't want either count printed. The variant tag in square brackets (e.g. `[user.name]`) is omitted when only one variant was checked (`--exact`).

## Files

```
phantom/
├── phantom              # bash wrapper (the CLI entry point)
├── checker.py           # 48-line shim (back-compat); calls cli.main()
├── cli.py               # argparse + main() orchestration
├── models.py            # Site, CheckResult, evaluate(), bot-wall detection
├── scanner.py           # async scan driver (aiohttp + curl_cffi + Playwright)
├── cache.py             # ResponseCache with periodic mid-scan flush
├── playwright_backend.py# headless Chromium fetcher for js_challenge sites
├── terminal.py          # ANSI rendering, three-tier + clustered output
├── dedupe.py            # same-site profile deduplication
├── emails.py            # Hunter.io email discovery
├── hints.py             # --identity-hint name-mode filtering
├── expand.py            # cross-link expansion (--expand)
├── stylometry.py        # bio fingerprint similarity (impostor signal)
├── wayback.py           # Wayback Machine CDX integration (--wayback)
├── github_deep.py       # GitHub orgs/starred/commit-email (--github-deep)
├── photo_ocr.py         # avatar OCR (--photo-ocr)
├── self_check.py        # canary-handle drift detector (--self-check)
├── discover_site.py     # auto-propose sites.json entries
├── validate_sites.py    # sites.json schema linter
├── variants.py          # username variation engine + leetspeak + email-to-handle
├── enrich.py            # per-site profile extractors (YouTube ytInitialData, Instagram bio_with_entities, …)
├── identity.py          # cross-platform identity correlation (pHash + name overlap)
├── disambiguation.py    # cluster found accounts by similarity graph
├── confidence.py        # 0–100 scoring with evidence trace
├── watch.py             # snapshot + diff for --watch mode
├── exporters/           # html, pdf, json, markdown, csv, mermaid
├── tests/               # 142+ unit tests + golden-file fixtures
├── sites.json           # 99 site definitions (data, no code)
├── pyproject.toml       # pip install phantom-osint
├── Dockerfile           # docker run phantom <username>
└── requirements.txt
```

## Install

```bash
git clone git@github.com:hamaffs/Phantom-.git phantom
cd phantom
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`curl_cffi` is required for the ~20 sites flagged with `tls_fingerprint` (Twitter, Instagram, Threads, Reddit, TikTok, …). Without it those sites will return `[   ?   ]`.

`playwright` is required only for `--export pdf`. After `pip install -r requirements.txt`, run `playwright install chromium` once to download the browser binary.

### Make `phantom` callable from anywhere

Run these from inside the cloned project directory. `$PWD` resolves to wherever you put it, so the symlink stays correct no matter where you cloned.

**System-wide (recommended)** — one `sudo` command:
```bash
sudo ln -s "$PWD/phantom" /usr/local/bin/phantom
```

**Per-user** — no sudo, edits `~/.bashrc` once:
```bash
mkdir -p ~/.local/bin
ln -s "$PWD/phantom" ~/.local/bin/phantom
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
# then re-source: source ~/.bashrc
```

**Project-local only** — no install, just call `./phantom <username>` from the project directory.

> The `phantom` wrapper uses `readlink -f` to find its own location, so it works from any clone path — no need to pin the project to a specific directory.

## Usage

```bash
# Basic search — runs the variant engine, checks each variant on every site
phantom <username>

# Skip variants and check the input verbatim
phantom <username> --exact

# Two-or-more words → name mode (firstlast, first.last, flast, lastfirst, …)
phantom "John Smith"

# Just print the variants the engine would produce, then exit
phantom <username> --list-variants

# Cap variants when you want a quick scan
phantom <username> --max-variants 5

# Only print hits (with no color, good for piping)
phantom <username> --found-only --no-color

# Filter to one or more categories
phantom <username> --category dev --category gaming

# Only show high-reliability sites
phantom <username> --min-reliability 85

# JSON output to stdout
phantom <username> --json > results.json

# Write a structured report to disk (format inferred from extension)
phantom <username> --export html             # → <username>_report.html in cwd
phantom <username> --export json             # → <username>_report.json in cwd
phantom <username> --export md               # → <username>_report.md in cwd
phantom <username> --export pdf              # → <username>_report.pdf in cwd
phantom <username> --export reports/         # → reports/<username>_report.json
phantom <username> --export out.html         # exact path; written as-is

# Theme flags for HTML and PDF exports (light is the default)
phantom <username> --export html --dark      # dark-themed HTML report
phantom <username> --export pdf --light      # light-themed PDF (explicit, same as default)
phantom <username> --export html             # light HTML with in-browser toggle button

# Disable curl_cffi impersonation (sites flagged tls_fingerprint will likely fail)
phantom <username> --no-impersonate
```

### Export formats

| Extension | Output |
| --- | --- |
| `.html`, `.htm` | Self-contained HTML report with dossier aesthetic (cream `#f5efe2` background, Instrument Serif + IBM Plex Mono + IBM Plex Sans). Stats header, portrait, confirmed account cards with photos. Includes a **☀/☾ theme toggle button** (top-right) that swaps between light and dark themes instantly — no reload, persisted in `localStorage`. Inconclusive results are not shown in the file (they are shown in the terminal). |
| `.pdf` | Pixel-faithful rendering of the HTML report via Playwright (Chromium). Same fonts, colors, layout, and photos. Respects `--dark`/`--light` flags. Requires `playwright` (`pip install playwright && playwright install chromium`). |
| `.json` | Single object with `input`, `summary` (includes inconclusive count), `found` (each with a nested `profile` object), and the list of generated variants. Inconclusive results are not enumerated (count only in summary). |
| `.md`, `.markdown`, `.txt` | Markdown report with FOUND section and a missing-count footer. Inconclusive results are not listed (count only in summary). |

`.json` is the default if the extension isn't recognised. All enrichment data (photos, names, bios, counts) is extracted from the SSR'd HTML during the scan, so no extra HTTP requests are made for the report.

### What gets extracted, per platform

| Site | Photo | Display name | Bio | Followers / Following / Posts | Extras |
| --- | --- | --- | --- | --- | --- |
| **Twitter / X** | ✓ | ✓ | ✓ | ✓ (followers, friends, statuses) | location, joined date, verified, **lists** (listed_count), **website** (real URL behind t.co) |
| **TikTok** | ✓ | ✓ | ✓ | ✓ | hearts, region, private, verified, **website** (bioLink) |
| **Instagram** | depends | depends | depends | ✓ (parsed from og:description) | — |
| **GitHub** | ✓ | ✓ | ✓ (real bio, not the og fallback) | followers / following / public-repo count | company, location, blog, X handle (clickable), pinned-repo names |
| **YouTube** | ✓ (channel art) | ✓ | ✓ | subscriber count → `followers`, video count → `posts` | country → `location`, total views, joined date |
| **Reddit** (old.reddit profile) | ✓ | ✓ | ✓ | — | **post karma + comment karma split**, total karma, cake-day text |
| **Steam** | ✓ | ✓ | — | — | profile level, country flag, game count |
| **Lichess** | ✓ | ✓ | ✓ | total games played → `posts` | best rating across blitz/rapid/classical, per-perf rating map |
| **Threads** | ✓ | ✓ (cleaned of `… • Threads, Say more` decoration) | ✓ | followers / posts (parsed from og:description) | — |
| **Linktree** | ✓ (avatar, when set) | ✓ | ✓ | — | **links list** (title + URL for every link on the profile), social icons, link count |
| **Beacons** | ✓ | ✓ | — | — | **linked_platforms** (list of connected platforms parsed from the page title) |
| **Bio.link** | ✓ | ✓ | — | — | (OpenGraph; generic bio description stripped) |
| **Carrd** | ✓ | ✓ | ✓ | — | (OpenGraph) |
| **Dev.to** | ✓ | ✓ | ✓ | — | **linked_accounts** (sameAs from JSON-LD: Twitter, GitHub, website), joined date, location |
| **Medium** | ✓ | ✓ | ✓ | followers / following | **recent_articles** (up to 5 article titles from page JSON) |
| **About.me** | ✓ | ✓ | ✓ (bio + jobTitle) | — | **linked_accounts** (sameAs from JSON-LD), job title, address |
| **Keybase** | ✓ | ✓ | ✓ | — | **proofs** (cryptographic identity proofs — verified Twitter, GitHub, Reddit, HN, DNS/websites), proof_count |
| **Vimeo** | ✓ | ✓ | ✓ | followers / posts (video count) | joined date |
| **SoundCloud** | ✓ | ✓ | ✓ | followers / following / track count | location, verified flag |
| **Bandcamp** | ✓ (artist name) | — | — | — | genre (from JSON-LD) |
| **Mixcloud** | ✓ | ✓ | ✓ (mixes = posts) | followers / following / views | city, country |
| **Spotify** | — | — | — | — | Removed — see note below |
| **Dribbble** | ✓ | — | — | — | (OpenGraph title only) |
| **Pastebin / Twitch / Letterboxd / Mastodon / …** | ✓ | ✓ | ✓ | — | (whatever's in `og:*`) |

Twitter/TikTok use a per-site extractor because they ship no `og:*` tags and embed all profile data in JSON instead. Instagram serves `og:description` of the form *"49 Followers, 176 Following, 1 Posts — …"* which is parsed for counts. GitHub has a fully-SSR'd profile so we read repo names, follower counts, company, location, and blog directly from the HTML. Everything else falls through to a generic OpenGraph reader.

Every extracted bio also runs through a **language detector** (script-based for Arabic/CJK/Cyrillic/Hangul, common-word matching for French/Spanish/Portuguese/Italian/Dutch/German/Turkish). The detected language is rendered as a 🌐 chip on each profile card and feeds into the cluster-level region inference.

### CLI flags

**Input + scope:**

| Flag | Default | Description |
| --- | --- | --- |
| `username` | required | One word → username; two+ words → name mode. Validated against `[A-Za-z0-9_.\-]{1,64}`. Accepts an email — local-part used. |
| `--exact` | off | Skip the variant engine; check input verbatim. |
| `--parse URL` | off | Extract the handle from a profile URL and scan that (implies `--exact`). |
| `--max-variants N` | 0 | Cap variant count. 0 = uncapped. |
| `--list-variants` | off | Print variants and exit (no network). |
| `--sites PATH` | `sites.json` | Custom sites file. |

**Site filtering:**

| Flag | Default | Description |
| --- | --- | --- |
| `--category NAME` | all | Repeatable: `dev`, `social`, `gaming`, `media`, `forum`, `other`. |
| `--country CC` | all | Repeatable ISO 3166-1 alpha-2 (e.g. `--country us`) or `global`. |
| `--language LANG` | all | Repeatable ISO 639-1 (e.g. `--language en`). |
| `--content-type TYPE` | all | Repeatable: `photo`, `text`, `code`, `audio`, `video`, `links`, `mixed`. |
| `--strict-tags` | off | When tag filters are set, drop sites with no tag set on that field. |
| `--min-reliability N` | 0 | Skip sites scoring below `N`. |

**Networking + backends:**

| Flag | Default | Description |
| --- | --- | --- |
| `--concurrency N` | 25 | Parallel in-flight requests. |
| `--per-host-concurrency N` | 3 | Cap per-host in-flight requests (prevents rate-limit self-DoS). |
| `--timeout SEC` | 15 | Per-request timeout. |
| `--no-impersonate` | off | Disable curl_cffi Chrome TLS impersonation. |
| `--js-render` | off | Route every site through Playwright (slow, useful for restricted networks). |
| `--js-concurrency N` | 3 | Max in-flight Playwright pages. |
| `--proxy URL` | none | Route via HTTP/SOCKS proxy (e.g. `socks5://127.0.0.1:9050`). |
| `--no-retry` | off | Disable single-shot retry on transient failures. |
| `--no-cache` | off | Disable on-disk cache. |
| `--resume` | off | Extend cache TTL to 7d and print reuse-ratio summary. |

**Expansion / enrichment:**

| Flag | Default | Description |
| --- | --- | --- |
| `--expand` | off | After scan, harvest @handles from linked-account fields and run another pass. |
| `--expand-depth N` | 2 | Max recursion rounds (clamped to 4). |
| `--expand-max-handles N` | 50 | Cap on total new handles scanned. |
| `--expand-max-time S` | 300 | Wall-clock cap on expansion. |
| `--wayback` | off | Query Wayback Machine for each FOUND URL's oldest snapshot. |
| `--github-deep` | off | Fetch GitHub orgs, starred repos, verified social accounts, commit-email leak. |
| `--photo-ocr` | off | Tesseract OCR on avatars; feeds handle-shaped text into the variant queue. |
| `--email` | off | Hunter.io email-finder per FOUND profile (requires `phantom --api add hunter <key>`). |
| `--identity-hint REPORT.json` | none | Filter FOUND hits in name mode against a previous report's country/language. |

**Output + reports:**

| Flag | Default | Description |
| --- | --- | --- |
| `--found-only` | off | Only print hits. |
| `--show-all` | off | Include possible-impostor accounts in terminal output. |
| `--no-cluster` | off | Disable disambiguation clustering; show three-tier output. |
| `--no-identity` | off | Skip identity-correlation step. |
| `--json` | off | Emit JSON to stdout. |
| `--export FILE` | off | Write structured report. Format from extension: `.html` `.pdf` `.json` `.md` `.csv` `.mmd`. |
| `--dark` / `--light` | light | Theme for HTML/PDF exports. |
| `--no-color` | off | Disable ANSI colors. |

**Operational:**

| Flag | Default | Description |
| --- | --- | --- |
| `--self-check` | off | Probe canary handles, report drifted sites (suggests auto-disable after 3 consecutive failures). |
| `--self-check-verbose` | off | Print every canary result, not just drifted ones. |
| `--watch` | off | Snapshot FOUND set and diff against previous run. |
| `--quiet` | off | Suppress regular output (cron-friendly with `--watch`). |
| `--api ARG ...` | — | Manage stored API keys (`--api add SERVICE KEY` / `--api list`). |

## Confidence ranking

After the scan, every FOUND account receives a confidence score (0–100) computed from cross-platform signals in `confidence.py`. Results are grouped into three tiers:

| Tier | Score | Terminal label | Meaning |
| --- | --- | --- | --- |
| **Verified identity** | 55+ | `[ VERIFIED IDENTITY ]` | High confidence this is the real person — verified badge, photo cluster, or strong cross-platform consistency |
| **Likely match** | 20–54 | `[ LIKELY MATCH ]` | Probably the same person but lacking the strongest signals |
| **Possible impostor** | 0–19 | `[ POSSIBLE IMPOSTOR ]` | Low confidence — likely a squatter, fan account, or unrelated person with the same handle |

Possible impostors are **collapsed by default** in the terminal. Use `--show-all` to print them. All three tiers appear in every export format (HTML collapsible section, JSON fields, Markdown section headers).

### Scoring signals

| Signal | Points |
| --- | --- |
| Verified badge on the platform | +50 |
| Profile photo perceptually matches another FOUND account (photo cluster) | +30 |
| Bio or linked website references another confirmed account's domain | +25 |
| Follower count within two orders of magnitude of the median across FOUND | +20 |
| **Exact username match** (no separators, no affixes, no numbers) | +20 |
| Display name matches the inferred subject name (fuzzy, case-insensitive) | +15 |
| Account shows activity (posts > 0, or joined > 6 months ago) | +10 |
| Zero posts AND zero followers (parked / placeholder account) | −25 |
| Default or placeholder profile photo | −20 |
| Has a real photo that doesn't match any photo cluster (isolated photo) | −15 |
| Variant has an impostor affix (`real`, `official`, `the`, …) AND the plain variant was also found on the same platform | −15 |
| Fewer than 10 followers while the subject has > 100k elsewhere | −10 |

Scores are clamped to [0, 100].

### Calibration notes

For **unique usernames** (hamaffs, jozzzof, etc.) with no impostors: every account matching the exact handle gets +20 from the exact-username signal. Combined with display-name match (+15) and activity (+10), genuine accounts naturally land in `likely_match` (35–45) or `verified_identity` when cross-platform photo or follower signals fire too.

For **famous usernames** (pewdiepie, zuck, etc.) with hundreds of variants: the real account gets boosted by verified badge (+50), exact-handle match (+20), and photo cluster (+30 when photos are fetched), reaching `verified_identity`. Impostors using modified variants (pewdiepie99, realpewdiepie) get 0 from the exact-handle signal and typically land below 20 → `possible_impostor`.

## Output legend

```
[ VERIFIED IDENTITY ]   score ≥ 55 — high confidence real account
[ LIKELY MATCH ]        score 20–54 — probably real, some uncertainty
[ POSSIBLE IMPOSTOR ]   score 0–19 — likely squatter (use --show-all to display)
[MISSING]   site cleanly says the user does not exist (4xx, or absence pattern matched)
[   ?   ]   inconclusive — bot-wall, captcha, 5xx, timeout, or auth wall
```

Each line includes a short `reason` tag:

| reason | meaning |
| --- | --- |
| `200`, `404`, … | decided by status code |
| `presence` | a `presence_text` pattern matched the body |
| `absence` | an `absence_text` pattern matched (the site told us the user is gone) |
| `no-presence` | site returned 200 but the expected presence pattern wasn't found (likely a SPA or junk response) |
| `bot-wall` | response title indicates a Cloudflare/captcha challenge — body is unreliable |
| `unexpected-NNN` | status code wasn't in `valid_status` or `invalid_status` (e.g. LinkedIn 999, Cloudflare 403) |
| `timeout`, `ClientError…` | network/transport error |

When the request was redirected, the line shows `→ https://final.example/...`. When `curl_cffi` was used (instead of `aiohttp`), the line is tagged `curl_cffi`.

The summary is printed to stderr; results go to stdout — so `--json` works in pipes. With multiple variants, each variant prints a header (`=== variant ===`) and the trailing summary lists per-variant counts plus a grand total.

## Identity disambiguation

After the scan and confidence scoring, `disambiguation.py` clusters the FOUND results into distinct identity groups — answering *"is this one person or several different people who share the username?"*

This is the most practically useful step in a multi-variant scan. Searching `pewdiepie` returns 40+ accounts; disambiguation separates Felix Kjellberg's verified accounts from the hundreds of squatters who registered the same handle.

### How it works

Each found account becomes a node in a weighted similarity graph. Two accounts get an edge when their signal-weight sum is ≥ 3 ("same person" threshold). [Connected components](https://en.wikipedia.org/wiki/Connected_component_(graph_theory)) of the graph become clusters.

| Signal | Weight | Notes |
| --- | --- | --- |
| Photo perceptual hash match | +3 | Computed by identity.py; reused here |
| Cross-link (bio/website of A contains B's domain) | +3 | Bidirectional |
| Both verified + display names match | +3 | Only fires when both platforms expose verified badges |
| Fuzzy display-name match (ratio > 0.85) | +2 | |
| Same location string | +2 | Normalised; substring overlap counts |
| Same website URL | +2 | |
| Same follower-count tier | +2 | Tiers: <100 / 100-1K / 1K-10K / 10K-100K / 100K-1M / 1M+ |
| Same exact username variant | +1 | |
| Same bio language | +1 | |
| Account creation dates within 12 months | +1 | |
| Same variant AND scores both ≥ 25 AND within 45 pts | +2 | Bridges data-sparse accounts that are clearly the same person |
| Different verified status (both on verifiable platforms) | −2 | |
| Follower counts 6+ orders of magnitude apart | −2 | e.g. 110M vs 50 |
| Contradicting location strings | −2 | |
| One default avatar, other has real photo | −2 | |

### Cluster labels

After building components, each cluster gets a label based on its highest-scoring member:

| Label | Threshold | Meaning |
| --- | --- | --- |
| `primary_identity` | max score ≥ 55 | Highly likely the real person |
| `secondary_cluster` | max score 40–54 | Credible but uncertain identity group |
| `low_confidence_cluster` | max score < 40 | Likely squatters, fans, or unrelated people |

The cluster with the highest `max_score` is always preferred for primary. If only one cluster exists (unique username like `hamaffs`), it is always primary.

### Terminal output

The default output groups by identity cluster rather than by confidence tier:

```
[ PRIMARY IDENTITY ] — PewDiePie
  10 accounts · region: Japan · max confidence: 100
  ✓ Verified on TikTok, YouTube
  Sites: YouTube, TikTok, Twitter, Instagram, ...
  ▸ youtube.com/@pewdiepie  (score 100, verified)
  ▸ tiktok.com/@pewdiepie   (score 100, verified)
  … and 8 more — see --export for full report

[ UNRELATED MATCHES ] 30  (use --show-all to display)
```

Use `--show-all` to list each unrelated cluster individually. Use `--no-cluster` to fall back to the three-tier `[ VERIFIED IDENTITY ]` / `[ LIKELY MATCH ]` / `[ POSSIBLE IMPOSTOR ]` display.

### Why this differs from flat OSINT tools

Most OSINT username checkers dump every match in a single list sorted by reliability, leaving you to manually decide which accounts belong to the same real person. Phantom's disambiguation:

1. **Groups accounts that provably belong together** (same profile photo across three platforms = definitely same person).
2. **Separates accounts that look suspicious** (110M followers vs 50 followers on similar platforms = different person).
3. **Propagates cluster membership to exports** — every found result in JSON/Markdown/HTML carries `identity_id` and `is_primary_identity` so downstream tools can filter on primary vs unrelated.
4. **Filters subject-overview stats to the primary cluster** — the HTML report's "110M followers" comes only from primary-cluster accounts, not averaged across squatter accounts.

### JSON fields

Every item in the `found` array has two new fields:
- `identity_id` — integer cluster ID (all accounts with the same ID belong to one identity group)
- `is_primary_identity` — `true` only for accounts in the primary cluster

The top-level `identity_clusters` array contains one object per cluster with `cluster_id`, `members`, `display_name`, `location`, `max_score`, `label`, and `size`.

## Variant engine

When you pass a single token (no spaces), `variants.py` produces:

- **The token itself** plus its lowercased form.
- **Smart-split separator variants** — if the input has `_`, `.`, or `-`, those are removed and other separators are tried (`word1_word2` → `word1.word2`, `word1-word2`, `word1word2`). Otherwise, `camelCase` and digit/letter boundaries are detected (`JohnDoe` → `john.doe`, `johndoe2024` → `johndoe.2024`).
- **Blind position-insertion** — for inputs ≤ 14 chars with no obvious split, every internal position gets each separator tried (`word1word2` → `word1.word2`, `wo.rd1word2`, `word1wo.rd2`, …). This is the only way to recover handles where the user condensed two words.
- **Number suffixes** — `1`, `2`, `99`, `123`.
- **Prefixes** — `the`, `its`, `real`, `official`, each tried both joined and underscore-separated.
- **Suffixes** — `_`, `official`, `_official`.

When you pass **two or more words**, the input is treated as `first … last` and the engine produces:

```
firstlast   first.last   first_last   first-last
flast       firstl
lastfirst   last.first   last_first   lastf
```

Middle names are dropped (`first` is `parts[0]`, `last` is `parts[-1]`) so a three-word name doesn't combinatorially explode.

Variants are **deduplicated and validated** against `^[A-Za-z0-9_.\-]{1,64}$` before any HTTP call. Use `--list-variants` to preview, `--max-variants` to cap, or `--exact` to disable the engine entirely.

`--json` returns a single flat object:

```json
{
  "input": "<username>",
  "generated_at": "2026-05-05T09:04:00+00:00",
  "elapsed_seconds": 7.0,
  "variants": ["<username>", "<user.name>", "<user_name>", ...],
  "summary": {"found": 9, "unknown": 5, "missing": 1812},
  "found": [
    {
      "site": "TikTok",
      "url": "https://www.tiktok.com/@<user.name>",
      "exists": true,
      "variant": "<user.name>",
      "profile": {
        "display_name": "<Display Name>",
        "photo": "https://p16-common-sign.tiktokcdn-eu.com/...",
        "followers": 1234,
        "following": 56,
        "posts": 0,
        "hearts": 78900,
        "verified": false,
        "private": false
      },
      ...
    }
  ],
  "unknown": [...]
}
```

The `MISSING` rows are intentionally not enumerated — only the count is kept (~thousands of them in a multi-variant run is just noise). Use `--export FILE.json` to write this same structure to disk.

## Identity: overall + photo-matched

The scanner says *"some account named X exists at site Y"*. Identity aggregation answers the harder question: *"what do we know about the person whose accounts these are?"*

There are two views, both built from the same FOUND set:

### Overall identity (always shown)

Aggregated from **every** FOUND account, regardless of clustering:

- **Display name** — most common normalized name across results.
- **Photos** — the union of every profile picture URL we extracted.
- **Locations** — vote-counted union of every `location` field. The most-mentioned region wins.
- **Geo region** — inferred from location strings, then bio language detection (Unicode-block + common-word matching for French/Spanish/Arabic/CJK/etc.), then joined-date timezone offsets. Lower-confidence fallbacks fire only if no explicit location was found.
- **Followers / Following / Posts** — summed across platforms (best-effort total reach).
- **Verified on / Private on** — list of sites where the account is verified or private.
- **Oldest joined date** — earliest `joined` value across results.

This view exists specifically for users whose photos don't happen to match across platforms — it pulls the geo region from anywhere it can find it, instead of needing two photos to agree first. If someone else owns one of the matched accounts (false positive), the aggregate is still mostly accurate because it's the union of many signals; one outlier doesn't dominate.

### Photo-matched accounts (when applicable)

A secondary "definitely the same person" view, only shown when 2+ accounts share a profile photo:

1. For each FOUND result, fetch the profile photo and compute a perceptual hash (`imagehash.phash`). A 48px Twitter avatar and a 400px Instagram upload of the same selfie produce hashes within Hamming-distance 2.
2. Pairs within Hamming-distance ≤ 8 merge into the same group.
3. As a secondary signal, results with identical normalized display names *and* high bio-token overlap also merge. Either alone is too weak (common names collide, short bios share filler words).
4. Each group ships with a confidence score and a rationale (`"matching profile photo (hamming=2)"`, `"identical display name + bio overlap 0.61"`).

**Privacy note:** identity correlation downloads the public profile photo URLs that are *already in* the SSR'd HTML you scraped during the scan. No auth, no API keys, no cookies. The hashes stay local and aren't sent anywhere. Disable the whole step with `--no-identity` if you don't want photos fetched.

### Hero portrait selection (face-aware)

The big subject portrait at the top of the HTML report is picked with face awareness so a real selfie wins over a logo when both exist. Logic:

1. Run OpenCV's frontal-face Haar cascade against every fetched profile photo (cached per URL for the rest of the run). If any photo contains a detected face, prefer the one whose photo cluster covers the most sites; ties break in favour of selfie-leaning platforms (Behance, Instagram, Twitter, Threads, Facebook, LinkedIn) over logo-leaning ones (GitHub, Pastebin, Disqus, Pinterest).
2. If no photo has a face, fall back to the largest photo-matched cluster's representative photo — the user's chosen self-representation, even if it's a logo, artwork, or a non-self picture (e.g. an album cover, a rapper photo).
3. If there are no clusters at all, take the first FOUND profile that has any photo.
4. If nothing exists, render a serif letter placeholder.

This logic only drives the hero. The 64×64 per-account cards keep showing whatever each platform exposed.

### Reverse image search (removed)

A previous version included a Yandex-scrape reverse image step under `--photo-deep`. It was removed because the matches were too often visually-similar but unrelated images (logos, default avatars, font samples) rather than identity matches. DINOv2 embeddings and Face++ comparison still run when their API keys are configured; only the reverse image step is gone.

### Canonical URLs for click links

The clickable URLs in the HTML/Markdown report (and the URL printed on the terminal line) are the **original, canonical** URLs we requested — `https://www.instagram.com/<user>/` rather than whatever Instagram redirected to mid-scan. Some platforms drop the `www` subdomain on redirect, then their own bot detection bounces cold visits to the redirected form. Using the canonical URL avoids that. The redirected `final_url` is still recorded in the JSON output for inspection.

### Geolocation hints (no API)

Each identity cluster gets a `geo_hint` with a best-guess region. Pure local heuristics:

1. **Explicit location strings** from profiles ("Paris, France", "Tokyo") — strongest signal, used as the cluster's region directly.
2. **Bio language** detected via Unicode-block presence (Arabic, CJK, Cyrillic, Hangul) and Latin-script common-word matching (French, Spanish, Portuguese, …) — lower-confidence fallback when no explicit location.
3. **Joined-date timezone** — last-resort. ISO offsets like `+02:00` map to coarse regions (Western Europe, Central Asia, etc.).

Each fired signal is recorded in `signals` so you can defend the inference. Confidence is `0.35`–`0.95`, capped low because regional inference is intentionally rough — we surface "France" or "Brazil" with confidence, never a city or address.

## Watch mode (cron-friendly)

Pass `--watch` and Phantom will snapshot every FOUND result (URL, variant, profile dict) to `~/.cache/phantom/snapshots/<input>.json` after the scan, then diff against the previous snapshot. Output is grouped into:

- `+ new account(s)` — sites that didn't exist last time but do now.
- `- removed account(s)` — sites that disappeared since last run.
- `~ changed account(s)` — same site, but a follower count, bio, location, photo URL, pinned-repo list, or other tracked stat moved.

Each snapshot file keeps the last 10 runs so the diff still works if you delete an in-progress one.

For automation:

```bash
# daily 9am check, only emails when something changes
0 9 * * *  /usr/local/bin/phantom <username> --watch --quiet --export json --no-color
```

`--quiet` suppresses the normal `[ FOUND ]` listing entirely so cron only sees output when there's something worth flagging.

## Reliability: retry + cache + parallel pool

Phantom does three things to make scans deterministic and fast:

1. **Single-shot retry on transient failures.** Timeouts, transport errors (`ServerDisconnected`, `ClientConnector`, `RemoteProtocol`, …) and 5xx responses get *one* retry with a 200ms backoff before the verdict is recorded. Definitive answers (a real 200 or 404) are never retried. Retried results are tagged in the `reason` field with `+retry`.
2. **On-disk response cache** at `~/.cache/phantom/cache.json` with a 1-hour TTL. Anything that wasn't a transient failure is cached by `(method, url, body)`, including bot-walled and auth-walled responses (they don't change in 5 minutes). Cache hits are tagged `+cached` and skip the network entirely. Disable with `--no-cache`. Override the path with `PHANTOM_CACHE_PATH=/some/path`.
3. **Pooled (variant × site) task graph.** Older versions ran variants sequentially — slow when one variant had a stragger. Now a 28-variant × 60-site scan creates 1680 tasks behind one shared semaphore so the queue is never idle and any stragglers don't block the next variant from starting.

The cache TTL exists so a re-run picks up if you create a new account between scans. If you want to force a fresh scan today, pass `--no-cache` once.

## Detection model

Decision order (hardest signals first, soft signals last):

1. **`status` is in `invalid_status`** → MISSING. Status codes are the most reliable signal when a site returns clean ones.
2. **Body matches an `absence_text` pattern** → MISSING. A page saying *"user not found"* beats a misleading 200 (Bandcamp's signup redirect, HackerRank's homepage redirect, etc.).
3. **Title looks like a bot-wall** (Cloudflare "Just a moment…", "Verify you are human", etc.) → UNKNOWN. The body is untrustworthy.
4. **Site-specific positive check:**
   - `method = "status"` — `status` must be in `valid_status`. If `presence_text` is also defined, at least one must match.
   - `method = "message"` — at least one `presence_text` pattern must match (with `{username}` substitution).
5. Otherwise → UNKNOWN.

This two-sided rule kills the false positives that a single check would let through (e.g. a 200 OK on a generic landing page, or a JSON blob that happens to contain the username because it's echoed back from the URL).

## sites.json schema

Each entry is a JSON object:

```json
{
  "name": "GitHub",
  "category": "dev",
  "url": "https://github.com/{username}",
  "method": "status",
  "valid_status": [200],
  "invalid_status": [404],
  "presence_text": ["<title>{username} ·"],
  "absence_text": ["<title>Page not found"],
  "reliability": 95
}
```

Required fields: `name`, `category`, `url`, `method`, `reliability`.

| Field | Default | Description |
| --- | --- | --- |
| `name` | required | Display name. |
| `category` | required | One of `dev`, `social`, `gaming`, `media`, `forum`, `other`. |
| `url` | required | URL template; `{username}` is substituted at request time. |
| `method` | required | `"status"` (decide by HTTP code) or `"message"` (decide by body content). |
| `reliability` | required | 0–100. Higher = the site's signal is more trustworthy. |
| `valid_status` | `[]` | Status codes that count as "user exists". |
| `invalid_status` | `[]` | Status codes that count as "user does not exist". Always wins, even on `method=message`. |
| `presence_text` | `[]` | Substring patterns (with `{username}`) that, if any matches the body, count as a positive hit. Required for `method=message` to FOUND. |
| `absence_text` | `[]` | Substring patterns (with `{username}`) that, if any matches the body, mean MISSING. Always wins over `valid_status`. |
| `protection` | `[]` | Bot-protection flags. `["tls_fingerprint"]` routes through `curl_cffi`; `["js_challenge"]` routes through Playwright (headless Chromium). |
| `country` / `language` / `content_type` | none | Tags for `--country` / `--language` / `--content-type` filters. |
| `disabled` | `false` | When `true`, the site is skipped entirely. Used by the `--self-check` auto-disable suggestion path. |
| `headers` | `{}` | Per-site headers. If `User-Agent` is set here, default headers are *replaced* (not merged) — important so e.g. a `curl/8.6.0` UA doesn't get paired with browser-shaped Accept-Encoding. |
| `request_method` | `"GET"` | `"GET"` or `"POST"`. |
| `request_body` | `null` | Raw POST body (with `{username}` substitution). Used for GraphQL/JSON-API sites like AniList. |

## How `curl_cffi` impersonation works

For sites with `protection: ["tls_fingerprint"]`, Phantom routes the request through `curl_cffi` with `impersonate="chrome"`. This makes the TLS handshake (JA3 fingerprint), HTTP/2 frame ordering, and SETTINGS frame look byte-identical to a real Chrome browser. WAFs that fingerprint TLS (Cloudflare bot fight, Akamai, AWS WAF) can't distinguish it from a logged-out Chrome user.

When `curl_cffi` is used, Phantom strips any `User-Agent` and `Connection` from the site's custom headers — `curl_cffi` provides matching Chrome equivalents, and mixing breaks the composite fingerprint.

This is what makes Twitter/X work without auth, plus Threads, Instagram, Speedrun, SoundCloud, Letterboxd, TikTok, GitLab API, CodePen, Patreon, Wikipedia API, Last.fm, Roblox, VSCO, and others.

### What still won't work

A handful of sites have layered protection that not even TLS impersonation defeats from a datacenter IP:

- **LinkedIn** — returns 999 (auth wall). Needs a logged-in cookie.
- **Reddit** — IP-reputation block on most public IPs. The `old.reddit.com` endpoint cleanly returns 404 for missing users, but real users may show as `[   ?   ]` when Reddit forces a "Prove your humanity" page.
- **Ko-fi** — Cloudflare client-challenge for non-existing handles (real users still resolve cleanly).

Sites that were always returning `[   ?   ]` from datacenter IPs (Quora, itch.io, Newgrounds, ProductHunt) and the SSR-shell-only Imgur web page have been replaced with reliable alternatives: **Disqus, Roblox, VSCO, Telegram**, plus an Imgur API endpoint that returns clean 200/404 JSON.

## Reliability tiers (current)

| Score | Examples | What it means |
| --- | --- | --- |
| 90–95 | GitHub, AniList API, Codewars API, Bluesky, Lichess, Last.fm, npm, Mojang, Hashnode, Patreon, YouTube, Roblox, VSCO, Disqus, Imgur API, Linktree, Beacons, Carrd, Dev.to, Keybase, Medium, About.me, Dribbble | Clean status code or strict presence/absence — trust. |
| 80–89 | Pastebin, Steam, Pinterest, Twitch (mobile), Bandcamp, Kaggle, HackerRank, TikTok, Reddit (old.reddit.com), Telegram, Bio.link, Vimeo, SoundCloud, Mixcloud | Generally clean but occasionally flaky. |
| 70–79 | Threads, VK | Heavy SPA / WAF; correct most of the time. |
| 50–69 | LinkedIn, Facebook, Ko-fi | Bot walls or SPAs we can't reliably penetrate. Filter out with `--min-reliability 70` if you want only confident hits. |

## Adding a new site

1. Pick a URL pattern that places `{username}` directly in the path or subdomain (or a JSON API endpoint, even better).
2. Probe it with `curl -i` and inspect the body for both a known-existing and known-missing handle. Look for substrings unique to one or the other.
3. Add an entry to `sites.json` matching the schema above. Use `presence_text` + `absence_text` together for the strongest signal.
4. If the site sits behind Cloudflare/Akamai, add `"protection": ["tls_fingerprint"]`.

No code changes are needed — `checker.py` reads everything from `sites.json`.

## Limitations

- Anti-bot walls behind WAF + IP-reputation will return `[   ?   ]` from datacenter IPs. A residential proxy fixes most; that's intentionally out of scope here.
- Auth-walled sites (LinkedIn, Reddit, sometimes Pinterest) need a logged-in cookie. Out of scope.
- No retries on failures — Phantom reports the first response it gets. Re-run if the network was flaky.

### Removed sites

**Spotify** (`open.spotify.com/user/{username}`) was removed in May 2026. Spotify's user-profile URL endpoint stopped reliably returning HTTP 200 for real users — real accounts like the official `spotify` user now get 404, while some usernames return 200 with a "Page not found" body. The `canonical` link tag always echoes back the requested username, making the previous presence-text detection produce false positives on any variant that happens to receive a 200 response. Since there is no reliable signal to distinguish "user exists" from "user does not exist" at this endpoint, Spotify was removed to protect Phantom's zero-false-positive guarantee. If Spotify restores a stable public profile API, it can be re-added.

## Troubleshooting

| Symptom | Likely cause / fix |
| --- | --- |
| `phantom: command not found` | Symlink missing. Re-run the install step under "Make `phantom` callable from anywhere". |
| `phantom: venv missing at …` | Run the install step. The wrapper expects `<project>/.venv/bin/python` to exist. |
| One particular site flips between FOUND/UNKNOWN across runs | The site is rate-limiting or its SSR varies. Re-run; if persistent, raise the issue and the site's `presence_text` may need a stronger pattern. |
| Almost every site times out | Network or DNS issue. Try `phantom <user> --concurrency 5 --timeout 30`. |
| All `tls_fingerprint` sites show `[   ?   ]` | `curl_cffi` not installed in the venv. Re-run `pip install -r requirements.txt`. |

## Dependencies

- [`aiohttp`](https://docs.aiohttp.org/) — async HTTP for non-protected sites
- [`aiodns`](https://github.com/saghul/aiodns) — async DNS, removes the `getaddrinfo` bottleneck
- [`brotli`](https://github.com/google/brotli) — needed to decode `Content-Encoding: br` responses
- [`curl_cffi`](https://github.com/lexiforest/curl_cffi) — libcurl with patched TLS to impersonate real browsers (defeats Cloudflare/WAFs)
- [`Pillow`](https://python-pillow.github.io/) — image decoding for the identity correlation step
- [`imagehash`](https://github.com/JohannesBuchner/imagehash) — perceptual hashes for cross-platform photo matching
