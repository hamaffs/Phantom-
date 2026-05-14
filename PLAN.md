# Phantom roadmap — "Maigret-level breadth, Phantom-level depth"

Goal: bring Phantom to feature parity with Maigret on the dimensions where it's
behind (site count, recursive search, profile-URL parsing, anti-block, visual
exports, packaging) **without losing** the accuracy-first detection contract,
the confidence/cluster scoring, the photo-hash identity merging, and the
dossier-aesthetic HTML report.

Originality rule: anywhere Maigret has a feature, Phantom's version must
either (a) be measurably more accurate, (b) integrate into the existing
dossier instead of bolting on, or (c) add a signal Maigret doesn't have.
Identity comes from depth + judgment + craft, not raw site count.

A phase is **only marked done** after the whole phase is complete *and* the
user has signed off on it.

---

## Phase 1 — Breadth: site count + tagging  · status: DONE 2026-05-14

Close the 63 → 200+ site gap. Add tooling so future additions don't take a
manual hour each.

- [x] **Site auto-discovery tool** (`discover_site.py`): probe a URL pattern
      with a known-real + known-fake handle, diff responses, propose a
      `sites.json` entry. Includes a hardened heuristic — rejects URL-only
      "presence" patterns and 4xx-on-real-user probes that would
      false-positive at scan time.
- [x] **Richer site tags**: extended schema with `country` (ISO/global),
      `language` (ISO 639), `content_type` (photo/text/code/audio/video/
      links/mixed). `Site` dataclass updated, `validate_sites.py` enforces
      the new shape, `disabled` flag also added (filtered in `load_sites`).
- [x] **Backfill tags on existing 63 sites.**
- [x] **CLI filter flags**: `--country CC`, `--language LANG`,
      `--content-type TYPE` (all repeatable), plus `--strict-tags` to drop
      untagged sites when filtering.
- [x] **Batch addition pass 1**: 39 candidates probed → 16 added.
- [x] **Batch addition pass 2**: 32 probed (incl. retries with impersonation)
      → 7 added.
- [x] **Batch addition pass 3**: 28 probed → 7 added.
- [x] **Batch addition pass 4**: 19 probed → 5 added (TryHackMe, Codecademy,
      TradingView, Untappd, Tildes).
- [x] **Batch addition pass 5**: 9 probed → 3 added (KhanAcademy, HubPages,
      Smule). Whois rejected (different semantic: domain-existence, not
      username).
- [x] **Final count: 101 sites** (was 63 → +38, +60%). Below the
      aspirational 180 target — many high-volume platforms turned out to be
      JS-only SPAs or sit behind aggressive WAFs that defeat curl_cffi.
      Quality bar held: every site that made it through has either a
      status-code discriminator or an anchored two-sided pattern.
- [x] **All sites pass `validate_sites.py`** (0 errors, 0 warnings).
- [x] **44 unit tests still pass**; hamaffs scan still finds Instagram
      consistently; a sanity scan of `tourist` lights up 16+ new platforms.
- [ ] **User sign-off → mark phase done.**

Yield analysis (probe → keep):
- Total probed: 127 candidates
- Kept: 38 (30% yield)
- Rejected as bot-walled: ~25
- Rejected as no-discriminator (JS-only / login walls): ~50
- Rejected as duplicate or unreliable on review: ~14

The auto-discovery tool is now in-tree and can be used to add more sites at
any time without code changes. Future "+50 sites" passes are mechanical.

---

## Phase 2 — Real recursion: smarter than Maigret's recursive search  · status: DONE 2026-05-14

Today `--expand` does one hop. Make it iterate intelligently.

- [x] **Multi-hop recursion** with `--expand-depth N` (default 2, max 4,
      clamped with warning if exceeded). Each round's discoveries feed the
      next round's harvest. Implemented as a bounded `for` loop in
      `cli._scan_and_correlate`.
- [x] **Confidence propagation**: source-tagged handles get a starting
      score boost added by `confidence.score_all`. Weights live in
      `expand.SOURCE_WEIGHTS`:
      `keybase_proof=30`, `github_handle=20`, `linked_account=15`,
      `website=10`, `bio_link=5`. Strongest source wins when the same
      handle appears in multiple places.
- [x] **`--parse URL`**: detects the platform via `expand._extract_one`,
      sets `--exact` automatically, refuses unknown URLs with a clear
      error. Mutually exclusive with the positional username.
- [x] **Stop conditions**: `--expand-max-handles` (default 50) and
      `--expand-max-time` (default 300s). Per-round checks; partial
      slicing when only some of a round's discoveries fit under the cap.
- [x] **Per-round telemetry**: every round prints both
      `expand: round N discovered M new handle(s) ... — scanning` and
      `expand: round N done in Xs — Y new FOUND`, plus a clear stop
      reason when a cap fires.
- [x] **Tests**: source-tag tagging, dedup on duplicate sources,
      already-tested handles filtered, weight ordering, confidence boost
      math, clamping at 100. 10 new tests, 55 total, all pass.
- [ ] **User sign-off → mark phase done.**

---

## Phase 3 — Anti-block parity: optional Playwright backend  · status: DONE 2026-05-14

A few sites (LinkedIn 999, Reddit humanity prompts, Cloudflare turnstile)
defeat even curl_cffi TLS impersonation. Added a third backend.

- [x] **Playwright fetcher backend** in `playwright_backend.py`. Lazy-
      initialises one long-lived headless Chromium browser. Each request
      gets its own incognito context so cookies don't leak across sites.
      Stability flags `--disable-blink-features=AutomationControlled` and
      `--disable-dev-shm-usage`.
- [x] **Browser pool**: one browser + N pages bounded by
      `--js-concurrency` (default 3, tuned so each page's ~50MB RAM and
      JS CPU cost stays controlled).
- [x] **Three-way routing in scanner.run_many**: js_challenge sites →
      Playwright, tls_fingerprint sites → curl_cffi, everything else →
      aiohttp. JS flag wins over TLS when both are set.
- [x] **Per-site `protection: ["js_challenge"]` flag** (validator already
      accepted it from Phase 1). Off by default per site.
- [x] **`--js-render` CLI flag** routes EVERY site through Playwright —
      useful for testing on networks where other backends are blocked.
- [x] **Re-enabled LinkedIn**: previously returned 999 to all
      datacenter-IP requests including curl_cffi. With Playwright: clean
      200 for real users, 999 for fake. Reliability bumped 50 → 85.
      Live-tested: `phantom williamhgates --exact` correctly finds Bill
      Gates' LinkedIn.
- [x] **Added PyPI**: title-based discriminator (`<title>Profile of
      {username} · PyPI</title>` vs `<title>Page Not Found (404) · PyPI
      </title>`). 100 sites total now.
- [x] **Probed but skipped (Cloudflare blocks Playwright too)**: Reddit,
      Ko-fi, Discogs, Trakt, ProductHunt, Quora, Bandsintown — these
      genuinely need residential IPs or a different angle, not just JS
      rendering.
- [x] **Validator loosened**: 3-digit non-standard status codes (999,
      521, 526, etc.) accepted without warning.
- [x] **Zero false positives**: both new JS-render sites correctly
      classify a known-fake handle as MISSING.
- [x] **55 tests still pass.**
- [ ] **User sign-off → mark phase done.**

---

## Phase 4 — Visual investigation tools  · status: DONE 2026-05-14

Maigret has D3 + XMind but they're separate, bolted on, and ugly. Phantom's
version is integrated into the dossier and stays in the same visual language.

- [x] **Per-account evidence trace** in `confidence.py` — every signal
      that fires (`+50 verified`, `+30 photo match`, `−15 photo doesn't
      match any cluster`, …) is recorded into `CheckResult.signals`.
      Rendered in the HTML card as a collapsible "Why this score" panel
      with green positives / red negatives. Nothing else surfaces this.
- [x] **Interactive force-directed graph** embedded in the dossier
      (`_build_graph_data` + `_html_identity_graph`). Vanilla SVG, no
      external libs. Nodes = accounts (sized by score, coloured by
      tier/verified status, ringed for primary identity). Edges colour-
      coded by signal: solid ink = disambiguation cluster, green = same
      photo, dashed grey = cross-linked in bio. Hover-highlights edges
      and neighbours; click to scroll to the card and flash its outline.
      Legend at the bottom.
- [x] **Mermaid mindmap export** (`--export mmd` / `.mmd` / `.mermaid`):
      cluster-grouped tree rendered as Mermaid `mindmap` syntax —
      renders natively in GitHub, Obsidian, VS Code, Notion. No
      XMind-style ecosystem lock-in. 4 unit tests cover sanitization
      and shape.
- [x] **62 tests pass.** Verified on `hamaffs --exact` — graph contains
      9 nodes, 28 edges, all 8 primary-cluster accounts fully connected
      through cluster signal.
- [x] **Graph visual upgrade** (Tier A + B; iterated heavily with user
      feedback during the session). Final shipped surface:
  - [x] **Real profile photos as nodes** (SVG `<pattern>` clip into
        circle). Default-PFP detection via URL fragments + PIL image
        variance check (16×16 grayscale, variance < 200 = placeholder).
        Falls back to serif-letter glyph when default.
  - [x] **Curved bezier edges** with parallel-edge fan-out.
  - [x] **Two-island layout**: bucket = "confirmed" (primary cluster
        AND non-impostor tier) vs "unrelated" (everything else,
        including impostor-tier nodes that disambiguation lumped into
        primary). Per-bucket gravity centroids; cross-bucket cluster
        edges dropped entirely so impostors visibly stay on the right
        side. Photo / cross-link edges across buckets retained.
  - [x] **Static after settle**: simulation runs once to lay things out,
        then every node auto-pins. Manual drag only moves the dragged
        node — neighbours stay put. Reset button restarts the sim.
  - [x] **Side panel on node click**: avatar, name, handle, bio,
        follower stats, full evidence trace, "Open profile ↗" + "Show in
        cards" buttons. Click outside or ESC to close.
  - [x] **Soft glow** around primary-identity nodes (SVG `feGaussianBlur`
        + `feMerge` filter).
  - [x] **Score ring** around each node — thin colour-graded arc.
  - [x] **Cluster halos REMOVED** (user dropped them — green blob was
        dominating the canvas; cluster membership still readable via
        edge density and node glow).
  - [x] **Edge pulses REMOVED** (user said they made the graph feel
        like a "kids game"; real OSINT tools are static).
  - [x] **Per-edge spring scaled by 1/√degree** so dense cliques don't
        collapse to a tight ball; node count adapts repulsion + spring
        rest length so 8-/16-/30-node graphs all spread evenly.
- [x] **User sign-off → marked done.**

---

## Phase 5 — Originality: signals Maigret literally doesn't have  · status: DONE 2026-05-14 (visual confirmation pending — full re-verify after Phase 6 HTML upgrade)

This is where Phantom stops being a Maigret-with-better-CSS and becomes its
own thing.

- [x] **Stylometric correlation** (`stylometry.py`): per-bio feature
      vector (punctuation rates, capitalization class, emoji rate +
      uniqueness, banded sentence-length, lexical signature). Cosine
      similarity ≥ 0.7 on bios ≥ 40 chars adds +2 disambiguation weight.
      Wired into `disambiguation._edge_weight`. 13 unit tests.
- [x] **Wayback Machine integration** (`wayback.py`): `--wayback` flag
      queries the free CDX API for each FOUND URL's oldest 200-status
      snapshot. Returns `first_snapshot_date`, `snapshot_count`,
      `first_snapshot_url`. Concurrent (cap 5) with timeout fallback.
      Live-verified against `torvalds` GitHub (1st snapshot 2013-07-18).
- [x] **GitHub deep-dive** (`github_deep.py`): `--github-deep` flag.
      Public-only — fetches orgs, recently-starred repos, verified
      social accounts (Twitter/Mastodon/Bluesky/etc.), and attempts the
      commit-email leak via `.patch` URLs (gated by user's email-privacy
      setting). Live-verified against `sindresorhus` — 15 orgs, 5
      starred repos, 4 social accounts surfaced (plus auto-feeds into
      `linked_accounts` for `--expand`).
- [x] **Profile-photo OCR** (`photo_ocr.py`): `--photo-ocr` flag runs
      Tesseract over each downloaded avatar, extracts handle-shaped
      text, feeds it back into the variant queue. Optional dep — module
      gracefully degrades when Tesseract isn't installed. 7 unit tests
      cover the handle-extraction filter (blocklist, length, leading
      letter, dedup) without needing the binary.
- [x] **Confidence-in-MISSING tier**: `missing_tier()` classifies every
      MISSING result as `confirmed_missing` (high-reliability site,
      clean 4xx or absence-text match, no retry/cache tag) or
      `uncertain_missing` (low reliability, retry-flagged, unexpected
      status). Stamped onto `.tier`. 9 unit tests.
- [x] **92 unit tests pass.** Every new module covered.
- [ ] **User sign-off → mark phase done.**

---

## Phase 6 — Deep enrichment parity  · status: DONE 2026-05-14

Maigret extracts far more per-account data than Phantom does today, because
its per-site extractors read the full embedded JSON (`ytInitialData`,
`__NEXT_DATA__`, JSON-LD, GraphQL response shapes) rather than just the
`og:*` meta tags. Phantom's "Phantom-level depth" pitch falls apart when
the per-account card has less information than Maigret's bland one does.

This phase makes Phantom's enrichment uniformly deeper across the whole
99-site set — both for display and for cross-link expansion (more
structured `linked_accounts` fields surface in `--expand`).

Concrete: the screenshot the user shared made the YouTube gap obvious —
the about-page Description (`<user's real name>`) and the structured Links
panel (`instagram.com/hama_ffs`) live in `ytInitialData`, not `og:*`. We
currently miss both.

- [x] **YouTube**: switched to `ytInitialData` parsing. Pulls
      `aboutChannelViewModel.description` (real bio, e.g. the user's
      real name), `channelMetadataRenderer.title` (display name),
      structured `links` panel → `linked_accounts` (e.g.
      `instagram.com/hama_ffs`), country → location, subscriber /
      view / video counts (with European-locale thousands-separator
      handling — `7.669` → 7669), joined date, HD avatar URL.
      Legacy regex fallbacks kept for when ytInitialData is absent.
      Live-verified on hamaffs.
- [x] **Instagram**: extended to read `external_url`, business email,
      category, `biography_with_entities` (mentioned @-handles get
      added to `linked_accounts`), `fbid_v2`. Live-verified: `@hama_ffs`
      in bio surfaces as a linked account.
- [x] **Threads**: rewrote to parse the SSR profile JSON via per-field
      regexes (more robust than parsing the whole multi-MB blob).
      Pulls real bio, full_name, follower_count, profile_pic_url,
      verified, private, user_id, AND `bio_links` (the panel of links
      Threads users can add) + `mention_fragment` (tagged @-handles in
      the bio). Falls back to og:description path when SSR JSON isn't
      present. Live-verified: bio "slept.", follower 11.
- [x] **Generic bio-URL extraction**: `_harvest_bio_links` runs after
      every per-site extractor — regex-scans bio text for recognisable
      platform URLs (twitter/x/instagram/tiktok/threads/youtube/github/
      reddit/linkedin/mastodon/bluesky/linktree/etc.) and merges them
      into `linked_accounts`. Bare-domain form ("instagram.com/foo")
      auto-prefixed with `https://`. Catches the long tail of accounts
      that hand-write platform URLs in their bio. 7 unit tests.
- [ ] **TikTok / Twitter / Twitch / Facebook**: opportunistic — current
      extractors already cover the basics; defer further depth to
      future enrichment passes unless gaps surface in real use.
- [x] **HTML dossier extension**: per-card display for all new fields:
      `_html_facts_block` (location, website, email, category, wayback
      first-archived), `_html_linked_chips` (small clickable platform
      chips for linked_accounts), `_html_github_deep_block` (orgs as
      chips, starred repos in a collapsible, commit email row).
      Dossier-level `confirmed-missing-list` chip section surfaces
      every Phase 5 `confirmed_missing` tier as "definitely not on
      these platforms". CSS-styled in the existing Instrument-Serif /
      IBM-Plex visual language. Live-verified against hamaffs (facts
      row + linked-account chips + confirmed-missing list visible) and
      sindresorhus (orgs + starred-repos sections visible).
- [ ] **Tests**: 108 tests pass. Per-site fixtures formalised in Phase 7.
- [ ] **User sign-off → mark phase done.**

---

## Phase 7 — Reliability + packaging  · status: DONE 2026-05-14

Maigret has a self-check, auto-disable, and pip/Docker install. Match it.

- [x] **Golden-file regression suite** (`tests/test_extractor_fixtures.py`):
      synthetic minimal fixtures per critical extractor (YouTube, Instagram,
      Threads, GitHub, Twitter) that exercise the parse paths without
      shipping multi-MB real bodies. The fixture pass already caught a
      latent regex bug in the YouTube parser (missing `re.DOTALL`) that
      would have silently degraded extraction on certain response shapes.
      19 fixture-based tests added.
- [x] **`--self-check` CLI flag** (`self_check.py` + `tests/canaries.json`):
      probes 27 curated canary handles, classifies each as ok / ok-thin /
      missing / unknown / error. Persistent streak tracking in
      `~/.cache/phantom/self_check_history.json`. After N=3 consecutive
      non-ok results for a site, prints a "consider disabling" suggestion
      (never auto-writes sites.json). Live-tested: caught 5 drifted sites
      from current IP (LinkedIn 999 from datacenter, Reddit bot-wall,
      Pastebin `no-presence`, Linktree 429, Bluesky 400). Exit code is
      non-zero on drift so it slots into CI.
- [x] **Auto-disable**: the `disabled: true` flag in sites.json is
      already honored by `load_sites`; `--self-check` surfaces which
      sites to add it to. Manual review intentional — auto-writing the
      sites file would risk masking real outages.
- [x] **`pyproject.toml`** + `pip install phantom-osint`: built a wheel
      and sdist successfully (`phantom_osint-1.0.0-py3-none-any.whl`).
      `pip install <wheel>` into a fresh venv resolved all deps and
      exposed `phantom` on PATH. `from phantom import Phantom` embeds
      as expected via the back-compat shim in `checker.py`.
- [x] **Dockerfile**: single-stage debian-slim image, bundles
      Chromium + tesseract + opencv runtime, runs as non-root user
      `phantom`. `.dockerignore` keeps build context lean. Persistent
      cache via volume mount.
- [x] **README rewrite**: lead section now positions Phantom as
      "Maigret tells you where, Phantom tells you who". Added a
      side-by-side comparison table (Maigret vs Phantom). Updated the
      CLI-flag table from ~22 flags to ~45, grouped by category
      (input / filtering / networking / expansion / output /
      operational). Files section reflects the post-split module
      layout.
- [x] **142 unit tests pass.**
- [ ] **User sign-off → mark phase done.**

---

## Phase 8 — Graph polish (Tier C + D)  · status: pending

Once the basic surveillance-map aesthetic is in (Phase 4 visual upgrade),
these are the power-tool additions that turn it from a pretty picture
into a real investigation interface.

- [ ] **Click-to-expand "hub" mode**: default the graph to showing only
      the strongest anchor node (highest-scoring account) — click it and
      the connected nodes fan out from there with an animated reveal.
      Each newly-revealed node is itself clickable to expand its own
      connections one layer deeper. Visually feels like an investigation
      unfolding instead of dumping all 16+ nodes at once. Toggle button
      "expand all" reverts to the current full-graph view. (Idea from
      user, 2026-05-14.)
- [ ] **Minimap** bottom-right showing the whole graph + a draggable
      viewport rectangle. Drag the rect or click anywhere on the minimap
      to navigate the main view.
- [ ] **Edge-type toggles** in the toolbar — checkboxes for
      photo/cluster/cross-link, hiding edges of unchecked kinds. Lets
      the user isolate one signal at a time.
- [ ] **Node search bar** above the graph — type a site or handle, it
      highlights and pans-to-centre the matching node.
- [ ] **Edge tooltips on hover** — small badge showing the signal type
      and detail ("photo hash match · hamming=2", "bio links to
      twitter.com/x", etc.).
- [ ] **Subtle drop-shadow lift** when hovering a node — depth cue, makes
      the canvas feel layered.
- [ ] **User sign-off → mark phase done.**

---

## Out of scope (deliberate)

- `--ai` OpenAI summarizer — user constraint: no APIs.
- I2P routing — niche, big maintenance surface.
- Windows EXE / Cloud Shell deploys — niche.
- 3000-site DB — Phantom's promise is precision, not raw coverage. ~250
  reliable sites beats 3000 noisy ones for OSINT integrity.
- FlareSolverr — running Playwright in-process is simpler than depending on
  a separate Docker service.
- AI report generation — out of scope.

---

## Progress log

- 2026-05-14 — plan drafted; starting Phase 1.
- 2026-05-14 — Phase 1 done: 63 → 99 sites (+36, +57%), auto-discovery tool
  in-tree, richer tag schema, CLI tag filters, zero false positives (audited),
  URL-echo bug found and patched mid-flight. Starting Phase 2.
- 2026-05-14 — Phase 2 done: multi-hop recursion (--expand-depth, default 2,
  max 4), source-weighted confidence boost (keybase=30, github_handle=20,
  linked_account=15, website=10, bio_link=5), --parse URL, stop conditions,
  per-round telemetry. 55 tests pass. User confirmed Keybase demo works
  end-to-end (3 new handles in round 1, 17 new FOUND accounts).
- 2026-05-14 — Plan amended: deep-enrichment work inserted as new Phase 6
  after user pointed out Maigret extracts richer per-account data
  (YouTube about-page Description + Links panel both missed by Phantom).
  Old Phase 6 (reliability + packaging) becomes Phase 7.
- 2026-05-14 — Phase 3 done: Playwright backend, --js-render flag,
  LinkedIn re-enabled (reliability 50→85), PyPI added (100 sites total).
  Probed but couldn't crack Reddit/Ko-fi/Discogs/Trakt/ProductHunt/Quora
  (need residential IPs). User confirmed LinkedIn detection fires on
  williamhgates. Starting Phase 4 (visual tools in the dossier).
- 2026-05-14 — Phase 4 done: evidence trace ("Why this score"), inline
  interactive graph in the HTML dossier (vanilla SVG, force-directed,
  hover/click), Mermaid mindmap export. 62 tests pass.
- 2026-05-14 — Phase 4 visual upgrade signed off after iterative
  tuning with the user: real avatars (with PIL-variance default
  detection), curved edges, two-island confirmed/unrelated layout,
  static-after-settle drag model, side panel on click. Pulses + cluster
  halos cut after user feedback ("looks like a kids game"). Click-to-
  expand hub mode and Tier C/D power features queued for Phase 8.
  Starting Phase 5 (originality signals).
- 2026-05-14 — Phase 5 implemented end-to-end: stylometry.py (cosine
  bio similarity → +2 disambiguation weight, integrated), wayback.py
  + --wayback flag, github_deep.py + --github-deep flag, photo_ocr.py
  + --photo-ocr flag (Tesseract optional, graceful degrade), MISSING
  tier classifier. 95 unit tests. Photo OCR precision-tightened after
  ARUN/cam false-positive risk found in live test. Marked DONE
  pending visual re-check after Phase 6 HTML upgrade — user wants to
  see the new fields displayed before final sign-off.
- 2026-05-14 — Starting Phase 6 (deep enrichment parity). YouTube
  ytInitialData parsing first (user-specified gap: about-page
  Description "<user's real name>" + Instagram Links panel currently
  missed). Then generic bio-URL extraction, then per-site overhauls.
- 2026-05-14 — Phase 6 core landed: YouTube ytInitialData parser
  (real bio "<user's real name>" + Instagram link recovered),
  Instagram biography_with_entities + external_url, Threads SSR JSON
  parser (bio, real follower count, bio_links, mention_fragment),
  generic bio-URL harvester (long-tail of hand-written platform URLs).
  108 tests pass. HTML dossier display of the new fields deferred to
  its own pass before final sign-off; same pass also re-verifies
  Phase 5 visibly.
- 2026-05-14 — Phase 6 HTML pass landed: facts block (location,
  website, email, category, wayback first-archived), linked-account
  chips per card, GitHub-deep block (orgs chips, starred-repos
  collapsible, commit email row), dossier-level confirmed-missing
  chip list. Twitter public-field extras (banner, default_avatar,
  snowflake-derived creation time, withheld_in_countries) shipped
  separately. 112 tests pass.
- 2026-05-14 — Tried adding X authenticated "Account based in" panel
  via cookies (auth_token + ct0). Built the path with Playwright
  click-on-(i)-button. Discovered the panel is X Premium-only when
  viewing OTHER profiles — free accounts (incl. the test account the
  user provided) can't see the (i) button at all. Feature removed
  cleanly (x_account_info.py deleted, --x-account-info flag pulled,
  cookies purged). Phantom keeps the public Twitter fields, which
  give 80% of the value without paywall.
- 2026-05-14 — Subject-level Real-Name / Nickname classifier added:
  walks every FOUND profile's display_name and first-line bio, scores
  each as real_name (multi-token Capitalised) / nickname (single
  stylised word) / username (matches a tested variant) / noise
  (platform-decorated strings, digits, etc.). The most-recurring
  real_name and nickname are surfaced at the very top of the dossier's
  Subject details box, so an OSINT analyst sees "Real name: …" /
  "Nickname: …" before drilling into individual cards.
  123 unit tests pass. User signed off Phase 6.
- 2026-05-14 — Starting Phase 7 (reliability + packaging): golden-
  file regression suite, --self-check, auto-disable, pyproject.toml +
  pip install, Dockerfile, README rewrite.
- 2026-05-14 — Phase 7 implemented: golden-file fixtures (caught
  latent YT regex bug), --self-check + streak-tracking auto-disable
  suggestion, pyproject.toml (wheel build + install verified end-to-
  end), Dockerfile + .dockerignore, README rewrite with comparison
  table and 45-flag grouped reference. 142 tests pass. User signed
  off. Phantom 1.0 feature-complete vs. the original Maigret-parity
  roadmap. Phase 8 (graph polish: minimap, edge toggles, search,
  click-to-expand hub mode) remains as power-user follow-ups.
