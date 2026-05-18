"""The async scanner. Runs (variant × site) checks through aiohttp or
curl_cffi behind a single shared semaphore, with retry and caching.
"""
from __future__ import annotations

import asyncio
import itertools
import time
from typing import Callable, Optional
from urllib.parse import urlparse

import aiohttp
from aiohttp import ClientSession, ClientTimeout, TCPConnector


# Phase 5 OPSEC: TLS-fingerprint impersonation profiles curl_cffi exposes.
# Rotating across these prevents a single Cloudflare-protected site from
# fingerprinting + rate-limiting our exact TLS signature. stick to
# four mainstream profiles - older / niche ones (edge99, qq, etc.)
# trigger more anti-bot heuristics, not fewer.
_TLS_PROFILES = ("chrome", "chrome120", "safari17_0", "firefox120")


def _fmt_eta(seconds: float) -> str:
    """Compact ETA: 12s / 1m45s / 1h23m."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        m, s = divmod(int(seconds), 60)
        return f"{m}m{s:02d}s"
    h, rem = divmod(int(seconds), 3600)
    m, _ = divmod(rem, 60)
    return f"{h}h{m:02d}m"


def _write_progress(done: int, total: int, elapsed: float) -> None:
    """Render a single-line progress indicator that rewrites itself in
    place via \\r. ETA is a linear extrapolation; close enough."""
    import sys as _sys
    pct = done / total if total else 0.0
    bar_w = 24
    filled = int(pct * bar_w)
    bar = "█" * filled + "░" * (bar_w - filled)
    if done > 0 and pct > 0:
        eta = elapsed * (1.0 - pct) / pct
        eta_str = f"ETA {_fmt_eta(eta)}"
    else:
        eta_str = "ETA --"
    msg = f"\r  [scanning] {bar} {done:>4}/{total} · {_fmt_eta(elapsed)} · {eta_str}"
    # Truncate / pad to terminal width so don't accidentally wrap.
    _sys.stderr.write(msg)
    _sys.stderr.flush()


def _clear_progress() -> None:
    """Erase the progress line so the next stderr write starts clean."""
    import sys as _sys
    # \033[2K clears the entire current line; \r returns cursor to col 0.
    _sys.stderr.write("\r\033[2K")
    _sys.stderr.flush()

from cache import (
    _FLUSH_INTERVAL_SECONDS,
    ResponseCache,
    _is_transient,
    _result_from_cache,
    _result_to_cache,
)
from enrich import extract_profile
from models import CheckResult, DEFAULT_HEADERS, Site, _drain, evaluate

try:
    from curl_cffi.requests import AsyncSession as CurlSession  # type: ignore
    HAS_CURL_CFFI = True
except ImportError:
    CurlSession = None  # type: ignore
    HAS_CURL_CFFI = False


# ---------------------------------------------------------------------------
# Checker
# ---------------------------------------------------------------------------
class Phantom:
    def __init__(
        self,
        sites: list[Site],
        *,
        concurrency: int = 25,
        timeout: float = 15.0,
        max_body: int = 2 * 1024 * 1024,
        impersonate: bool = True,
        retry_on_transient: bool = True,
        retry_delay: float = 0.2,
        cache: Optional[ResponseCache] = None,
        proxy: Optional[str] = None,
        per_host_concurrency: int = 3,
        js_render: bool = False,
        js_concurrency: int = 3,
        tls_rotate: bool = False,
        proxy_pool: Optional[list[str]] = None,
        simulate_session: bool = False,
    ) -> None:
        self.sites = sites
        self.concurrency = concurrency
        self.timeout = timeout
        self.max_body = max_body
        # Caller can disable the curl_cffi path even when installed.
        self.impersonate = impersonate and HAS_CURL_CFFI
        self.retry_on_transient = retry_on_transient
        self.retry_delay = retry_delay
        self.cache = cache or ResponseCache(enabled=False)
        # Proxy URL. aiohttp accepts http/https/socks; curl_cffi accepts the
        # same as a `proxy` kwarg. None disables proxying entirely.
        self.proxy = proxy
        # Per-host concurrency cap. The global semaphore lets us run ~50
        # checks in parallel, but if 30 variants × 1 site (e.g. Instagram)
        # all fire at once, the site's API rate-limits and some come back
        # as UNKNOWN. This second, per-host semaphore staggers them. 3 is
        # conservative enough that real sites don't throttle while still
        # letting unrelated hosts run wide-open.
        self.per_host_concurrency = per_host_concurrency
        self._host_sems: dict[str, asyncio.Semaphore] = {}
        # JS-render backend (Playwright). When `js_render=True`, every
        # site routes through headless Chromium - useful for blanket
        # testing or networks that block other backends. Otherwise only
        # sites flagged `protection: ["js_challenge"]` use it. The
        # fetcher is lazy-instantiated so phantom doesn't pay browser
        # startup unless a JS-route site is actually scanned.
        self.js_render_all = js_render
        self.js_concurrency = js_concurrency
        self._js_fetcher = None  # PlaywrightFetcher | None

        # Phase 5 OPSEC features. All three default to off so existing
        # behaviour is unchanged unless the user explicitly opts in.
        self.tls_rotate = tls_rotate
        # round-robin iterator over TLS profiles; thread-safe enough for
        # our async use because reads happen one at a time per checker.
        self._tls_cycle = itertools.cycle(_TLS_PROFILES) if tls_rotate else None

        self.proxy_pool = list(proxy_pool) if proxy_pool else None
        self._proxy_cycle = itertools.cycle(self.proxy_pool) if self.proxy_pool else None

        # session simulation: GET the site homepage before hitting the
        # profile URL so cookies + Referer look organic. Doubles request
        # count; off by default.
        self.simulate_session = simulate_session
        # Per-host one-time flag - only need to warm up cookies once
        # per host per scan, not per profile lookup.
        self._warmed_hosts: set[str] = set()
        # Per-host gate so concurrent profile-hits for the same host
        # wait for the in-progress warmup instead of duplicating it.
        # Events not Locks: first task creates+sets the Event, others
        # await it. Never use a global lock — serializes warmups
        # behind the slowest host and stalls multi-host scans.
        self._warmup_events: dict[str, asyncio.Event] = {}
        # Warmup is best-effort and runs alongside the real scan; cap
        # it tightly so one slow homepage never blocks the host's
        # actual profile lookup for long.
        self._warmup_timeout: float = 5.0

    def _pick_proxy(self) -> Optional[str]:
        """Return the proxy URL for the next request. Priority: pool > single proxy."""
        if self._proxy_cycle is not None:
            return next(self._proxy_cycle)
        return self.proxy

    def _pick_tls_profile(self) -> str:
        """Return the TLS impersonation profile for the next curl_cffi call."""
        if self._tls_cycle is not None:
            return next(self._tls_cycle)
        return "chrome"

    async def _warmup_gate(self, host: str) -> bool:
        """Per-host warmup coordination. Returns True iff THIS caller
        should run the warmup; False means another task is doing it (or
        already finished) and the caller should just proceed to the
        real request.

        Implementation: a dict of asyncio.Events keyed by host. The
        first task to arrive for host H creates events[H] and gets
        True. Subsequent tasks for H find the event and await it (so
        their profile request runs *after* warmup finishes, not in
        parallel — which is the whole point of session simulation).

        asyncio is single-threaded, so `if host not in events: events[host] = Event()`
        is atomic — no race window.
        """
        if host in self._warmed_hosts:
            return False
        ev = self._warmup_events.get(host)
        if ev is None:
            self._warmup_events[host] = asyncio.Event()
            return True
        # Someone else is warming this host - wait for them, capped at
        # _warmup_timeout so never hang behind a stuck warmup.
        try:
            await asyncio.wait_for(ev.wait(), timeout=self._warmup_timeout)
        except asyncio.TimeoutError:
            pass
        return False

    def _warmup_done(self, host: str) -> None:
        """Mark a host as warmed and unblock any waiting tasks."""
        self._warmed_hosts.add(host)
        ev = self._warmup_events.get(host)
        if ev is not None:
            ev.set()

    async def _warmup_aiohttp(self, session: ClientSession, url: str) -> None:
        """Phase 5 OPSEC: GET the site homepage once before hitting profile URLs.

        Best-effort and bounded by `_warmup_timeout` (5s). One warmup
        per host per scan; concurrent profile requests for the same
        host wait for the in-progress warmup via per-host events.
        """
        host = urlparse(url).netloc.lower()
        if not host:
            return
        should_warm = await self._warmup_gate(host)
        if not should_warm:
            return
        try:
            home = f"https://{host}/"
            proxy = self._pick_proxy()
            kwargs: dict = {
                "headers": DEFAULT_HEADERS,
                "allow_redirects": True,
                "timeout": ClientTimeout(total=self._warmup_timeout),
            }
            if proxy:
                kwargs["proxy"] = proxy
            try:
                async with session.get(home, **kwargs) as resp:
                    # Drain a few KB so the response is actually consumed.                    await resp.
                    content.read(8192)
            except Exception:
                pass   # warmup is best-effort; never block the real check
        finally:
            self._warmup_done(host)

    async def _warmup_curl(self, session, url: str) -> None:
        """Same as _warmup_aiohttp but for the curl_cffi session."""
        host = urlparse(url).netloc.lower()
        if not host:
            return
        should_warm = await self._warmup_gate(host)
        if not should_warm:
            return
        try:
            home = f"https://{host}/"
            kwargs: dict = {
                "url": home,
                "allow_redirects": True,
                "timeout": self._warmup_timeout,
            }
            proxy = self._pick_proxy()
            if proxy:
                kwargs["proxy"] = proxy
            if self.tls_rotate:
                kwargs["impersonate"] = self._pick_tls_profile()
            try:
                await session.get(**kwargs)
            except Exception:
                pass
        finally:
            self._warmup_done(host)

    # Hosts that rate-limit hard on bursts. Capped at 3 concurrent so
    # variant detection stays reliable; without this, 30-variant Instagram
    # scans return UNKNOWN for half the variants.
    _STRICT_HOSTS = (
        "instagram.com", "i.instagram.com",
        "tiktok.com", "www.tiktok.com",
        "facebook.com", "www.facebook.com", "m.facebook.com",
        "threads.net", "www.threads.net",
        "x.com", "twitter.com",
    )

    def _host_sem(self, url: str) -> asyncio.Semaphore:
        host = urlparse(url).netloc.lower()
        sem = self._host_sems.get(host)
        if sem is None:
            cap = getattr(self, "_effective_per_host", None) or self.per_host_concurrency
            if any(host == h or host.endswith("." + h) for h in self._STRICT_HOSTS):
                cap = min(cap, 3)
            sem = asyncio.Semaphore(cap)
            self._host_sems[host] = sem
        return sem

    # ------- aiohttp path --------------------------------------------------
    async def _aiohttp_request(
        self,
        session: ClientSession,
        site: Site,
        username: str,
    ) -> CheckResult:
        """Single aiohttp attempt — no retry, no cache. Used by the wrapper."""
        url = site.url_for(username)
        display_url = site.display_url_for(username)
        has_custom_ua = any(k.lower() == "user-agent" for k in site.headers)
        headers = dict(site.headers) if has_custom_ua else {**DEFAULT_HEADERS, **site.headers}
        body_payload = site.body_for(username)
        start = time.monotonic()
        try:
            method = session.post if site.request_method.upper() == "POST" else session.get
            kwargs: dict = {
                "headers": headers,
                "allow_redirects": True,
                "timeout": ClientTimeout(total=self.timeout),
            }
            if body_payload is not None:
                kwargs["data"] = body_payload
            proxy = self._pick_proxy()
            if proxy:
                kwargs["proxy"] = proxy
            async with method(url, **kwargs) as resp:
                raw = await _drain(resp.content, self.max_body)
                body = raw.decode(resp.charset or "utf-8", errors="replace")
                exists, reason = evaluate(site, resp.status, body, username)
                final = str(resp.url)
                profile = (
                    extract_profile(site.name, body, final, username)
                    if exists is True else {}
                )
                blocked_by = (reason.split(":", 1)[1] if reason and (reason.startswith("bot-wall:") or reason.startswith("login-wall:")) else None)
                return CheckResult(
                    site=site.name, category=site.category, url=display_url, exists=exists,
                    reliability=site.reliability, status=resp.status,
                    elapsed_ms=int((time.monotonic() - start) * 1000),
                    reason=reason,
                    final_url=final if final != url else None,
                    backend="aiohttp", profile=profile, variant=username,
                    blocked_by=blocked_by,
                )
        except asyncio.TimeoutError:
            return CheckResult(
                site=site.name, category=site.category, url=display_url, exists=None,
                reliability=site.reliability, error="timeout", reason="timeout",
                elapsed_ms=int((time.monotonic() - start) * 1000),
                backend="aiohttp", variant=username,
            )
        except aiohttp.ClientError as e:
            return CheckResult(
                site=site.name, category=site.category, url=display_url, exists=None,
                reliability=site.reliability, error=type(e).__name__,
                reason=type(e).__name__,
                elapsed_ms=int((time.monotonic() - start) * 1000),
                backend="aiohttp", variant=username,
            )

    async def _check_aiohttp(
        self,
        session: ClientSession,
        site: Site,
        username: str,
        sem: asyncio.Semaphore,
    ) -> CheckResult:
        """aiohttp backend with cache + retry."""
        url = site.url_for(username)
        body_payload = site.body_for(username)
        method_name = site.request_method.upper()

        cached = self.cache.get(method_name, url, body_payload)
        if cached:
            return _result_from_cache(site, username, cached, "aiohttp")

        host_sem = self._host_sem(url)
        async with sem, host_sem:
            result = await self._aiohttp_request(session, site, username)
            if getattr(self, "_effective_retry", self.retry_on_transient) and _is_transient(result):
                await asyncio.sleep(self.retry_delay)
                retry = await self._aiohttp_request(session, site, username)
                # Prefer the retry only if it actually upgraded the verdict.
                # A second timeout shouldn't overwrite the first.
                if retry.exists is True or retry.exists is False:
                    retry.reason = (retry.reason or "") + "+retry"
                    result = retry

        # Cache only definitive verdicts (FOUND / MISSING). UNKNOWN means
        # couldn't decide - usually a transient SPA-shell, login wall,
        # or rate-limit response - and locking that in for an hour blocks
        # legitimate retries. Transient transport errors are also skipped.
        if not _is_transient(result) and result.exists is not None:
            self.cache.set(method_name, url, body_payload, _result_to_cache(result))
        return result

    # ------- curl_cffi path ------------------------------------------------
    async def _curl_request(
        self,
        session,  # CurlSession
        site: Site,
        username: str,
    ) -> CheckResult:
        """Single curl_cffi attempt — no retry, no cache."""
        url = site.url_for(username)
        display_url = site.display_url_for(username)
        # Strip `Connection` (curl_cffi manages it). Strip `User-Agent`
        # UNLESS the site explicitly set one - Reddit's API rejects all
        # browser UAs and requires its own format ("python:appname:v0.1
        # (by u/x)"). curl_cffi's impersonate-chrome default UA fails
        # there. Honoring an explicit site UA fixes Reddit without
        # breaking the common case (no site UA → use chrome).
        site_headers = {
            k: v for k, v in site.headers.items()
            if k.lower() != "connection"
        }
        body_payload = site.body_for(username)
        start = time.monotonic()
        try:
            kwargs: dict = {
                "url": url,
                "headers": site_headers or None,
                "allow_redirects": True,
                "timeout": self.timeout,
            }
            if body_payload is not None:
                kwargs["data"] = body_payload
            proxy = self._pick_proxy()
            if proxy:
                kwargs["proxy"] = proxy
            if self.tls_rotate:
                kwargs["impersonate"] = self._pick_tls_profile()
            # libcurl's own timeout misses TLS-handshake stalls - wrap
            # in asyncio.wait_for as a hard event-loop timeout.
            hard_timeout = self.timeout + 5
            if site.request_method.upper() == "POST":
                resp = await asyncio.wait_for(session.post(**kwargs), timeout=hard_timeout)
            else:
                resp = await asyncio.wait_for(session.get(**kwargs), timeout=hard_timeout)
            body = resp.text or ""
            if len(body) > self.max_body:
                body = body[: self.max_body]
            exists, reason = evaluate(site, resp.status_code, body, username)
            final = str(resp.url)
            profile = (
                extract_profile(site.name, body, final, username)
                if exists is True else {}
            )
            blocked_by = (reason.split(":", 1)[1] if reason and (reason.startswith("bot-wall:") or reason.startswith("login-wall:")) else None)
            return CheckResult(
                site=site.name, category=site.category, url=display_url, exists=exists,
                reliability=site.reliability, status=resp.status_code,
                elapsed_ms=int((time.monotonic() - start) * 1000),
                reason=reason,
                final_url=final if final != url else None,
                backend="curl_cffi", profile=profile, variant=username,
                blocked_by=blocked_by,
            )
        except asyncio.TimeoutError:
            return CheckResult(
                site=site.name, category=site.category, url=display_url, exists=None,
                reliability=site.reliability, error="timeout", reason="timeout",
                elapsed_ms=int((time.monotonic() - start) * 1000),
                backend="curl_cffi", variant=username,
            )
        except Exception as e:
            return CheckResult(
                site=site.name, category=site.category, url=display_url, exists=None,
                reliability=site.reliability, error=type(e).__name__,
                reason=type(e).__name__,
                elapsed_ms=int((time.monotonic() - start) * 1000),
                backend="curl_cffi", variant=username,
            )

    async def _check_curl(
        self,
        session,
        site: Site,
        username: str,
        sem: asyncio.Semaphore,
    ) -> CheckResult:
        """curl_cffi backend with cache + retry."""
        url = site.url_for(username)
        body_payload = site.body_for(username)
        method_name = site.request_method.upper()

        cached = self.cache.get(method_name, url, body_payload)
        if cached:
            return _result_from_cache(site, username, cached, "curl_cffi")

        # See _check_aiohttp - warmup is fire-and-forget, not awaited here.
        host_sem = self._host_sem(url)
        async with sem, host_sem:
            result = await self._curl_request(session, site, username)
            if getattr(self, "_effective_retry", self.retry_on_transient) and _is_transient(result):
                await asyncio.sleep(self.retry_delay)
                retry = await self._curl_request(session, site, username)
                if retry.exists is True or retry.exists is False:
                    retry.reason = (retry.reason or "") + "+retry"
                    result = retry

        # Cache only definitive verdicts (FOUND / MISSING). UNKNOWN means
        # couldn't decide - usually a transient SPA-shell, login wall,
        # or rate-limit response - and locking that in for an hour blocks
        # legitimate retries. Transient transport errors are also skipped.
        if not _is_transient(result) and result.exists is not None:
            self.cache.set(method_name, url, body_payload, _result_to_cache(result))
        return result

    # ------- Playwright path -----------------------------------------------
    async def _playwright_request(self, site: Site, username: str) -> CheckResult:
        """Single Playwright attempt — no retry, no cache."""
        url = site.url_for(username)
        display_url = site.display_url_for(username)
        start = time.monotonic()
        # Reuse the per-site User-Agent if it set one - site authors
        # sometimes pin a UA to bypass UA-based gating.
        ua = None
        extra_headers: dict = {}
        for k, v in (site.headers or {}).items():
            if k.lower() == "user-agent":
                ua = v
            else:
                extra_headers[k] = v
        try:
            assert self._js_fetcher is not None
            status, body, final = await self._js_fetcher.fetch(
                url, headers=extra_headers or None, user_agent=ua,
            )
            if len(body) > self.max_body:
                body = body[: self.max_body]
            exists, reason = evaluate(site, status, body, username)
            profile = (
                extract_profile(site.name, body, final, username)
                if exists is True else {}
            )
            blocked_by = (reason.split(":", 1)[1] if reason and (reason.startswith("bot-wall:") or reason.startswith("login-wall:")) else None)
            return CheckResult(
                site=site.name, category=site.category, url=display_url, exists=exists,
                reliability=site.reliability, status=status,
                elapsed_ms=int((time.monotonic() - start) * 1000),
                reason=reason,
                final_url=final if final != url else None,
                backend="playwright", profile=profile, variant=username,
                blocked_by=blocked_by,
            )
        except asyncio.TimeoutError:
            return CheckResult(
                site=site.name, category=site.category, url=display_url, exists=None,
                reliability=site.reliability, error="timeout", reason="timeout",
                elapsed_ms=int((time.monotonic() - start) * 1000),
                backend="playwright", variant=username,
            )
        except Exception as e:
            return CheckResult(
                site=site.name, category=site.category, url=display_url, exists=None,
                reliability=site.reliability, error=type(e).__name__,
                reason=type(e).__name__,
                elapsed_ms=int((time.monotonic() - start) * 1000),
                backend="playwright", variant=username,
            )

    async def _check_playwright(
        self,
        site: Site,
        username: str,
        sem: asyncio.Semaphore,
    ) -> CheckResult:
        """Playwright backend with cache + retry."""
        url = site.url_for(username)
        body_payload = site.body_for(username)
        method_name = site.request_method.upper()

        cached = self.cache.get(method_name, url, body_payload)
        if cached:
            return _result_from_cache(site, username, cached, "playwright")

        host_sem = self._host_sem(url)
        async with sem, host_sem:
            result = await self._playwright_request(site, username)
            if getattr(self, "_effective_retry", self.retry_on_transient) and _is_transient(result):
                await asyncio.sleep(self.retry_delay)
                retry = await self._playwright_request(site, username)
                if retry.exists is True or retry.exists is False:
                    retry.reason = (retry.reason or "") + "+retry"
                    result = retry

        if not _is_transient(result) and result.exists is not None:
            self.cache.set(method_name, url, body_payload, _result_to_cache(result))
        return result

    # ------- driver --------------------------------------------------------
    async def run_many(
        self,
        variants: list[str],
        on_result: Optional[Callable[[CheckResult], None]] = None,
    ) -> list[tuple[str, list[CheckResult]]]:
        """Scan a list of variants against all configured sites in one pool.

        Previous behaviour ran variants sequentially: variant 1 finishes
        all 60 sites, then variant 2 starts. With a small number of slow
        sites that meant lots of idle bandwidth — variant 2 was waiting
        for variant 1's stragglers. This version pools every (variant,
        site) pair behind one semaphore, so the queue stays full and any
        one slow site doesn't block the next variant from starting.

        `on_result`, when provided, is fired synchronously for each
        CheckResult as it lands — used by the TUI to stream rows into
        the FOUND ACCOUNTS table while the scan is still running.
        Callback exceptions are swallowed so a buggy UI hook can't
        abort the scan.
        """
        # Three-way routing:
        #   - js-render sites (or `--js-render` forcing every site through it)
        #     → Playwright
        #   - tls-fingerprint sites with curl_cffi available → curl
        #   - everything else → aiohttp
        # Routing is done up front so the per-backend semaphores can be
        # sized correctly. JS routing wins over TLS routing when both
        # flags are set on a site.
        if self.js_render_all:
            js_sites = list(self.sites)
            curl_sites: list[Site] = []
            aio_sites: list[Site] = []
        else:
            js_sites = [s for s in self.sites if s.needs_js_render]
            remaining = [s for s in self.sites if not s.needs_js_render]
            if self.impersonate:
                curl_sites = [s for s in remaining if s.needs_impersonation]
                aio_sites = [s for s in remaining if not s.needs_impersonation]
            else:
                curl_sites = []
                aio_sites = list(remaining)

        # Multi-variant runs: SKIP Playwright entirely. Sites that need
        # headless Chromium (LinkedIn, PyPI) are slow (~5-15s per page)
        # and bottlenecked at js_concurrency=3. With 30 variants × 2
        # js-sites = 60 page loads queued through 3 slots, that's 4-8
        # minutes of pure Playwright stall at the end of every scan -
        # all returning the same login-wall for every variant anyway.
        # Single-variant scans keep Playwright on so the user still gets
        # those results when they matter. --js-render forces it on.
        if len(variants) > 5 and not self.js_render_all and js_sites:
            import sys as _sys
            print(
                f"  skipping Playwright for {len(js_sites)} JS-challenge site"
                f"{'s' if len(js_sites) != 1 else ''} "
                f"({', '.join(s.name for s in js_sites)}) — "
                f"too slow for {len(variants)}-variant runs. "
                f"Use --exact or --js-render to override.",
                file=_sys.stderr,
            )
            js_sites = []

        # Bigger semaphore for multi-variant runs - most of the work is
        # network-bound and per-host already throttled by TCPConnector,
        # so can afford to be aggressive with the global cap.
        cap = self.concurrency * 2 if len(variants) > 1 else self.concurrency
        sem = asyncio.Semaphore(cap)

        # Per-host concurrency scales with variant count to avoid
        # variant-induced serialization. STRICT_HOSTS override this in
        # _host_sem with a hard cap of 3.
        n_variants = max(1, len(variants))
        if n_variants > 1:
            self._effective_per_host = min(
                10, self.per_host_concurrency * max(1, n_variants // 3)
            )
        else:
            self._effective_per_host = self.per_host_concurrency

        # Retries off on multi-variant runs - doubling wall time on the
        # bot-walled tail dominates the scan otherwise.
        self._effective_retry = self.retry_on_transient and n_variants <= 5

        # curl_cffi gets its own semaphore - libcurl's async wrapper
        # deadlocks past ~30 concurrent on one AsyncSession.
        curl_sem = asyncio.Semaphore(20)

        connector = TCPConnector(
            limit=cap,
            limit_per_host=max(8, self._effective_per_host),
            ttl_dns_cache=300,
            enable_cleanup_closed=True,
        )
        timeout = ClientTimeout(total=self.timeout + 5)

        aio_session_cm = ClientSession(connector=connector, timeout=timeout)
        curl_session_cm = CurlSession(impersonate="chrome") if curl_sites else None

        # Lazy-init the Playwright fetcher only if any site actually
        # needs it - pays the ~1s browser-startup cost only on demand.
        if js_sites:
            from playwright_backend import PlaywrightFetcher
            self._js_fetcher = PlaywrightFetcher(
                concurrency=self.js_concurrency,
                timeout_seconds=self.timeout + 10,
            )
            await self._js_fetcher.start()

        # Bucket so can rebuild per-variant ordering at the end.
        all_tasks: list[tuple[str, asyncio.Task]] = []

        try:
            aio_session = await aio_session_cm.__aenter__()
            curl_session = (
                await curl_session_cm.__aenter__() if curl_session_cm else None
            )

            # Fire-and-forget warmup: one homepage GET per unique host,
            # kicked off NOW so they run in parallel with the real scan
            # rather than blocking 2,970 tasks behind asyncio.Events.
            # Only warm up _STRICT_HOSTS - for the other 90+ sites,
            # firing a homepage GET adds noise without benefit, and in
            # multi-variant runs that 90-warmup burst can itself trip
            # anti-bot detection. The strict hosts are the ones that
            # actually benefit from a cookie+Referer seed before the
            # profile lookup.
            if self.simulate_session:
                seen_hosts: set[str] = set()
                for s in aio_sites + curl_sites:
                    sample = s.url.replace("{username}", "x")
                    host = urlparse(sample).netloc.lower()
                    if host in seen_hosts:
                        continue
                    if not any(host == h or host.endswith("." + h)
                               for h in self._STRICT_HOSTS):
                        continue
                    seen_hosts.add(host)
                    if s in aio_sites:
                        asyncio.create_task(
                            self._warmup_aiohttp(aio_session, sample),
                            name=f"warmup_{host}",
                        )
                    elif curl_session:
                        asyncio.create_task(
                            self._warmup_curl(curl_session, sample),
                            name=f"warmup_{host}",
                        )

            for v in variants:
                for s in aio_sites:
                    t = asyncio.create_task(
                        self._check_aiohttp(aio_session, s, v, sem)
                    )
                    all_tasks.append((v, t))
                for s in curl_sites:
                    t = asyncio.create_task(
                        self._check_curl(curl_session, s, v, curl_sem)
                    )
                    all_tasks.append((v, t))
                for s in js_sites:
                    t = asyncio.create_task(
                        self._check_playwright(s, v, sem)
                    )
                    all_tasks.append((v, t))

            # Drain completions as they come in so can periodically
            # flush the cache to disk. If the user hits Ctrl-C mid-scan,
            # the work completed up to the last flush is preserved.
            # When `on_result` is set, fire it for each task that just
            # completed so callers (the TUI) can stream rows into the
            # UI without waiting for every variant × site to finish.
            pending = {t for _, t in all_tasks}
            total_tasks = len(pending)
            completed = 0
            scan_started = time.monotonic()
            # Live progress to stderr - but only when stderr is a real
            # TTY (so piping / non-interactive shells don't get a wall
            # of `\r`-overwrites). Avoids the "scan looks frozen" UX on
            # long multi-variant runs where the CLI doesn't print
            # per-result rows. Suppressed when `on_result` is set
            # because the caller (TUI) is already streaming.
            import sys as _sys
            show_progress = (
                on_result is None
                and total_tasks > 20
                and getattr(_sys.stderr, "isatty", lambda: False)()
            )
            last_progress_update = 0.0

            while pending:
                done, pending = await asyncio.wait(
                    pending,
                    timeout=_FLUSH_INTERVAL_SECONDS,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if on_result is not None:
                    for t in done:
                        try:
                            on_result(t.result())
                        except Exception:
                            pass
                completed += len(done)
                self.cache.maybe_flush()
                # Throttle progress writes - at most ~2 per second.
                now = time.monotonic()
                if show_progress and (now - last_progress_update) >= 0.5:
                    _write_progress(completed, total_tasks, now - scan_started)
                    last_progress_update = now
            # Clear the progress line before returning so the next thing
            # written to stderr (scan summary, expand-round message, …)
            # starts on a clean line.
            if show_progress:
                _clear_progress()
        finally:
            await aio_session_cm.__aexit__(None, None, None)
            if curl_session_cm:
                await curl_session_cm.__aexit__(None, None, None)
            if self._js_fetcher is not None:
                await self._js_fetcher.close()
                self._js_fetcher = None

        grouped: dict[str, list[CheckResult]] = {v: [] for v in variants}
        for v, t in all_tasks:
            grouped[v].append(t.result())
        return [(v, grouped[v]) for v in variants]

    async def run(self, username: str) -> list[CheckResult]:
        """Backwards-compatible single-variant scan."""
        out = await self.run_many([username])
        return out[0][1] if out else []
