"""The async scanner. Runs (variant × site) checks through aiohttp or
curl_cffi behind a single shared semaphore, with retry and caching.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional
from urllib.parse import urlparse

import aiohttp
from aiohttp import ClientSession, ClientTimeout, TCPConnector

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

    def _host_sem(self, url: str) -> asyncio.Semaphore:
        """Return a (lazily created) per-host semaphore for `url`."""
        host = urlparse(url).netloc.lower()
        sem = self._host_sems.get(host)
        if sem is None:
            sem = asyncio.Semaphore(self.per_host_concurrency)
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
            if self.proxy:
                kwargs["proxy"] = self.proxy
            async with method(url, **kwargs) as resp:
                raw = await _drain(resp.content, self.max_body)
                body = raw.decode(resp.charset or "utf-8", errors="replace")
                exists, reason = evaluate(site, resp.status, body, username)
                final = str(resp.url)
                profile = (
                    extract_profile(site.name, body, final, username)
                    if exists is True else {}
                )
                return CheckResult(
                    site=site.name, category=site.category, url=display_url, exists=exists,
                    reliability=site.reliability, status=resp.status,
                    elapsed_ms=int((time.monotonic() - start) * 1000),
                    reason=reason,
                    final_url=final if final != url else None,
                    backend="aiohttp", profile=profile, variant=username,
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
            if self.retry_on_transient and _is_transient(result):
                await asyncio.sleep(self.retry_delay)
                retry = await self._aiohttp_request(session, site, username)
                # Prefer the retry only if it actually upgraded the verdict.
                # A second timeout shouldn't overwrite the first.
                if retry.exists is True or retry.exists is False:
                    retry.reason = (retry.reason or "") + "+retry"
                    result = retry

        # Cache only definitive verdicts (FOUND / MISSING). UNKNOWN means
        # we couldn't decide — usually a transient SPA-shell, login wall,
        # or rate-limit response — and locking that in for an hour blocks
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
        site_headers = {
            k: v for k, v in site.headers.items()
            if k.lower() not in ("user-agent", "connection")
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
            if self.proxy:
                kwargs["proxy"] = self.proxy
            if site.request_method.upper() == "POST":
                resp = await session.post(**kwargs)
            else:
                resp = await session.get(**kwargs)
            body = resp.text or ""
            if len(body) > self.max_body:
                body = body[: self.max_body]
            exists, reason = evaluate(site, resp.status_code, body, username)
            final = str(resp.url)
            profile = (
                extract_profile(site.name, body, final, username)
                if exists is True else {}
            )
            return CheckResult(
                site=site.name, category=site.category, url=display_url, exists=exists,
                reliability=site.reliability, status=resp.status_code,
                elapsed_ms=int((time.monotonic() - start) * 1000),
                reason=reason,
                final_url=final if final != url else None,
                backend="curl_cffi", profile=profile, variant=username,
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

        host_sem = self._host_sem(url)
        async with sem, host_sem:
            result = await self._curl_request(session, site, username)
            if self.retry_on_transient and _is_transient(result):
                await asyncio.sleep(self.retry_delay)
                retry = await self._curl_request(session, site, username)
                if retry.exists is True or retry.exists is False:
                    retry.reason = (retry.reason or "") + "+retry"
                    result = retry

        # Cache only definitive verdicts (FOUND / MISSING). UNKNOWN means
        # we couldn't decide — usually a transient SPA-shell, login wall,
        # or rate-limit response — and locking that in for an hour blocks
        # legitimate retries. Transient transport errors are also skipped.
        if not _is_transient(result) and result.exists is not None:
            self.cache.set(method_name, url, body_payload, _result_to_cache(result))
        return result

    # ------- driver --------------------------------------------------------

    async def run_many(self, variants: list[str]) -> list[tuple[str, list[CheckResult]]]:
        """Scan a list of variants against all configured sites in one pool.

        Previous behaviour ran variants sequentially: variant 1 finishes
        all 60 sites, then variant 2 starts. With a small number of slow
        sites that meant lots of idle bandwidth — variant 2 was waiting
        for variant 1's stragglers. This version pools every (variant,
        site) pair behind one semaphore, so the queue stays full and any
        one slow site doesn't block the next variant from starting.
        """
        if self.impersonate:
            curl_sites = [s for s in self.sites if s.needs_impersonation]
            aio_sites = [s for s in self.sites if not s.needs_impersonation]
        else:
            curl_sites = []
            aio_sites = list(self.sites)

        # Bigger semaphore for multi-variant runs — most of the work is
        # network-bound and per-host already throttled by TCPConnector,
        # so we can afford to be aggressive with the global cap.
        cap = self.concurrency * 2 if len(variants) > 1 else self.concurrency
        sem = asyncio.Semaphore(cap)

        connector = TCPConnector(
            limit=cap,
            limit_per_host=8,
            ttl_dns_cache=300,
            enable_cleanup_closed=True,
        )
        timeout = ClientTimeout(total=self.timeout + 5)

        aio_session_cm = ClientSession(connector=connector, timeout=timeout)
        curl_session_cm = CurlSession(impersonate="chrome") if curl_sites else None

        # Bucket so we can rebuild per-variant ordering at the end.
        all_tasks: list[tuple[str, asyncio.Task]] = []

        try:
            aio_session = await aio_session_cm.__aenter__()
            curl_session = (
                await curl_session_cm.__aenter__() if curl_session_cm else None
            )

            for v in variants:
                for s in aio_sites:
                    t = asyncio.create_task(
                        self._check_aiohttp(aio_session, s, v, sem)
                    )
                    all_tasks.append((v, t))
                for s in curl_sites:
                    t = asyncio.create_task(
                        self._check_curl(curl_session, s, v, sem)
                    )
                    all_tasks.append((v, t))

            # Drain completions as they come in so we can periodically
            # flush the cache to disk. If the user hits Ctrl-C mid-scan,
            # the work completed up to the last flush is preserved.
            pending = {t for _, t in all_tasks}
            while pending:
                done, pending = await asyncio.wait(
                    pending,
                    timeout=_FLUSH_INTERVAL_SECONDS,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                self.cache.maybe_flush()
        finally:
            await aio_session_cm.__aexit__(None, None, None)
            if curl_session_cm:
                await curl_session_cm.__aexit__(None, None, None)

        grouped: dict[str, list[CheckResult]] = {v: [] for v in variants}
        for v, t in all_tasks:
            grouped[v].append(t.result())
        return [(v, grouped[v]) for v in variants]

    async def run(self, username: str) -> list[CheckResult]:
        """Backwards-compatible single-variant scan."""
        out = await self.run_many([username])
        return out[0][1] if out else []
