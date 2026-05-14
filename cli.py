"""Phantom CLI entrypoint — argparse, --api subcommand, main()."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional

import apis
import confidence as _confidence
import disambiguation as _disambiguation
import photo_deep
from cache import ResponseCache
from dedupe import _dedupe_same_site_dicts, _flatten
from emails import _attach_emails_to_found, _print_emails_section, discover_emails
from exporters import export_report, resolve_export_path
from exporters.json_export import _build_json_payload
from hints import _filter_results_by_hint, _load_identity_hint
from identity import build_overall_and_clusters
from models import USERNAME_PATTERN, load_sites
from scanner import HAS_CURL_CFFI, Phantom
from terminal import _c, _print_identity_summary, print_compact
from variants import generate as generate_variants
from watch import (
    Snapshot, diff as compute_diff, load_history, render_diff_terminal,
    save_snapshot,
)


def _run_api_subcommand(argv: list[str]) -> int:
    """Handle `phantom --api <cmd> [args...]`. Two subcommands today:

    - add SERVICE KEY: store a key (overwrites any existing one).
    - list: print configured services without revealing the keys.
    """
    usage = "usage: phantom --api {add SERVICE KEY | list}"
    if not argv:
        print(usage, file=sys.stderr)
        return 2
    cmd = argv[0].lower()
    if cmd == "add":
        if len(argv) != 3:
            print("usage: phantom --api add SERVICE KEY", file=sys.stderr)
            return 2
        try:
            path = apis.add(argv[1], argv[2])
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        print(f"saved {argv[1].lower()} key to {path}")
        return 0
    if cmd == "list":
        services = apis.list_services()
        if not services:
            print("no API keys configured.")
            print("add one with: phantom --api add SERVICE KEY")
            return 0
        print(f"Configured API keys ({apis.config_path()}):")
        for s in services:
            print(f"  {s:<10}  [configured]")
        return 0
    print(f"unknown --api subcommand: {argv[0]}", file=sys.stderr)
    print(usage, file=sys.stderr)
    return 2
_HELP_DESCRIPTION = """\
Phantom — find a username across 60 hand-picked platforms with high accuracy
and pull whatever public profile data each platform exposes (display name,
bio, photo, follower / following / post counts, location, joined date,
verified flag).

A single input is automatically expanded to dozens of plausible variants
(separator insertion, smart word splits, number suffixes, prefix/suffix
combinations, first/last name permutations) and every variant is checked
against every site. Each FOUND result is tagged with the variant that
produced it.
"""

_HELP_EPILOG = """\
Examples:
  phantom <username>                       # full run with the variant engine
  phantom <username> --exact               # check the input verbatim, no variants
  phantom "<first> <last>"                 # name mode → firstlast, first.last, flast, ...
  phantom <username> --list-variants       # preview the variants and exit
  phantom <username> --max-variants 5      # cap at the first 5 variants
  phantom <username> --found-only          # hide the [ ? ] / [MISSING] sections
  phantom <username> --category social     # restrict to one category
  phantom <username> --min-reliability 85  # only the most trustworthy sites
  phantom <username> --export html         # auto-name to <username>_report.html
  phantom <username> --export json         # auto-name to <username>_report.json
  phantom <username> --export md           # auto-name to <username>_report.md
  phantom <username> --export reports/     # write to reports/<username>_report.json
  phantom <username> --export out.html     # write exactly to out.html
  phantom <username> --json                # JSON to stdout instead of file
  phantom <username> --no-cache            # ignore the on-disk cache for this run
  phantom <username> --no-retry            # don't retry transient failures
  phantom <username> --watch --quiet       # snapshot + diff (cron-friendly)
  phantom <username> --no-identity         # skip photo-hash cross-site merging

Output:
  [ FOUND ]  one row per hit, with the variant that found it
  [   ?   ]  count only — full detail is in the export
  [MISSING]  count only

Variants:
  - one word  → separators, smart splits, numbers, prefixes, suffixes
  - two words → firstlast, first.last, flast, lastfirst, ...

Categories:
  dev, social, gaming, media, forum, other

Notes:
  - The tool runs entirely on public data — no auth, no API keys, no cookies.
  - Bot-walled sites (LinkedIn, Reddit on some IPs) return [   ?   ].
  - For the most accurate results, use the default variant engine.
  - To make this command available everywhere, from inside the cloned dir:
      sudo ln -s "$PWD/phantom" /usr/local/bin/phantom
"""

def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="phantom",
        description=_HELP_DESCRIPTION,
        epilog=_HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "username", nargs="*",
        help="username (or first + last name) to look up. Multi-word inputs "
             "are joined with a space and treated as a name. Optional when "
             "using --api.",
    )
    p.add_argument(
        "--api", nargs="+", metavar="ARG",
        help="manage stored API keys instead of running a scan. "
             "Subcommands: 'add SERVICE KEY' to save a key, 'list' to show "
             "configured services. Keys live in ~/.config/phantom/apis.json.",
    )
    p.add_argument(
        "--email", action="store_true",
        help="for each FOUND profile, query Hunter.io email-finder using "
             "the profile's display name and the site's domain. Requires a "
             "Hunter.io key (set with: phantom --api add hunter YOUR_KEY).",
    )
    p.add_argument(
        "--sites", default=str(Path(__file__).with_name("sites.json")),
        help="path to sites.json (default: alongside this script)",
    )
    p.add_argument("--concurrency", type=int, default=25)
    p.add_argument(
        "--per-host-concurrency", type=int, default=3, metavar="N",
        help="cap on simultaneous in-flight requests to any single host "
             "(default: 3). Prevents rate-limit throttling on Instagram, "
             "TikTok, etc. when running many variants against few hosts.",
    )
    p.add_argument("--timeout", type=float, default=15.0)
    p.add_argument(
        "--min-reliability", type=int, default=0,
        help="skip sites with a reliability score below this threshold",
    )
    p.add_argument(
        "--category", action="append", default=None,
        help="restrict to a category (repeatable: dev, social, gaming, media, forum, other)",
    )
    p.add_argument(
        "--no-impersonate", action="store_true",
        help="disable curl_cffi browser impersonation, even if installed "
             "(sites flagged with tls_fingerprint will likely fail)",
    )
    p.add_argument(
        "--proxy", metavar="URL",
        help="route all HTTP requests through a proxy. Accepted schemes: "
             "http://, https://, socks5://, socks5h://. Example: "
             "--proxy socks5://127.0.0.1:9050 (Tor). The same URL is used "
             "for both the aiohttp and curl_cffi backends.",
    )
    p.add_argument(
        "--no-retry", action="store_true",
        help="disable the single-shot retry on transient failures (timeouts, "
             "5xx, transport errors). Off by default — retries on.",
    )
    p.add_argument(
        "--no-cache", action="store_true",
        help="disable the on-disk response cache "
             "(~/.cache/phantom/cache.json, 1h TTL). Cache is on by default.",
    )
    p.add_argument(
        "--resume", action="store_true",
        help="resume an interrupted scan: extend the cache TTL to 7 days and "
             "reuse every prior verdict, then refetch only the requests that "
             "didn't complete. Reports the reuse ratio at the end.",
    )
    p.add_argument(
        "--no-identity", action="store_true",
        help="skip the identity-correlation step (downloading + hashing "
             "profile photos to merge cross-platform accounts).",
    )
    p.add_argument(
        "--watch", action="store_true",
        help="snapshot mode: persist this scan's FOUND set and diff against "
             "the previous snapshot for the same input. Designed for cron "
             "(see --quiet). Snapshots live in ~/.cache/phantom/snapshots/.",
    )
    p.add_argument(
        "--quiet", action="store_true",
        help="suppress the normal scan output. Combined with --watch, only "
             "the diff is printed (or nothing if there are no changes).",
    )
    p.add_argument(
        "--exact", action="store_true",
        help="check only the exact input — skip the variant generator",
    )
    p.add_argument(
        "--max-variants", type=int, default=0,
        help="cap the number of variants checked (0 = no cap)",
    )
    p.add_argument(
        "--list-variants", action="store_true",
        help="print the generated variants and exit (no network calls)",
    )
    p.add_argument(
        "--photo-deep", dest="photo_deep", action="store_true", default=True,
        help="use deep photo matching (DINOv2 image embeddings + Face++ "
             "face compare + Yandex reverse image search) on top of "
             "perceptual-hash clustering. ON by default. Each provider is "
             "skipped silently when its credentials aren't configured "
             "(see --api add huggingface / facepp_key / facepp_secret). "
             "Yandex reverse search needs no key.",
    )
    p.add_argument(
        "--no-photo-deep", dest="photo_deep", action="store_false",
        help="disable deep photo matching (--photo-deep is on by default)",
    )
    p.add_argument(
        "--identity-hint", metavar="REPORT.json",
        help="path to a previous Phantom JSON report. In name mode, FOUND "
             "hits whose profile location or bio language clearly contradict "
             "the hint's country / language are reclassified as UNKNOWN — "
             "cuts down on collisions with strangers who happen to share a "
             "name-derived handle.",
    )
    p.add_argument(
        "--expand", action="store_true",
        help="after the first scan, harvest @handles from linked accounts "
             "(Keybase proofs, Dev.to / About.me sameAs, Linktree links, "
             "GitHub blog / x_handle, etc.) and run a second pass on every "
             "new handle. Goes one hop deep — found-account links are not "
             "themselves followed.",
    )
    p.add_argument("--found-only", action="store_true", help="only print hits")
    p.add_argument(
        "--show-all", action="store_true",
        help="include 'possible impostor' accounts in terminal output "
             "(default: show count only)",
    )
    p.add_argument(
        "--no-cluster", action="store_true",
        help="disable identity disambiguation clustering; show flat tier-based "
             "output instead of grouped-by-identity output",
    )
    p.add_argument("--json", dest="as_json", action="store_true", help="emit JSON results to stdout")
    p.add_argument(
        "--export", metavar="FILE_OR_FORMAT",
        help="write a structured report. Pass a format ('html', 'json', 'md', 'pdf') "
             "and the file is auto-named '<input>_report.<ext>' in the cwd. "
             "Pass a path with extension (e.g. 'reports/out.html') to write "
             "exactly there.",
    )
    _theme = p.add_mutually_exclusive_group()
    _theme.add_argument(
        "--dark", action="store_true",
        help="use dark theme for HTML/PDF exports (default is light)",
    )
    _theme.add_argument(
        "--light", action="store_true",
        help="use light theme for HTML/PDF exports (default)",
    )
    p.add_argument("--no-color", action="store_true")
    return p.parse_args(argv)
def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    if args.api:
        return _run_api_subcommand(args.api)

    raw = " ".join(args.username).strip()
    if not raw:
        print(
            "error: missing username. Pass a username (or first + last name), "
            "or use --api to manage stored keys.",
            file=sys.stderr,
        )
        return 2

    hunter_key: Optional[str] = None
    if args.email:
        hunter_key = apis.get("hunter")
        if not hunter_key:
            print(
                "warning: --email requires a Hunter.io API key. Add one with:\n"
                "  phantom --api add hunter YOUR_KEY\n"
                "Continuing the scan without email lookup.",
                file=sys.stderr,
            )

    color = sys.stdout.isatty() and not args.no_color

    if args.exact:
        if not USERNAME_PATTERN.match(raw):
            print(f"error: invalid username '{raw}' (use without --exact to enable variants)", file=sys.stderr)
            return 2
        variants = [raw]
    else:
        variants = generate_variants(raw)
        if not variants:
            print(f"error: no valid variants generated from '{raw}'", file=sys.stderr)
            return 2

    if args.max_variants > 0:
        variants = variants[: args.max_variants]

    if args.list_variants:
        for v in variants:
            print(v)
        return 0

    sites_path = Path(args.sites)
    if not sites_path.is_file():
        print(f"error: sites file not found: {sites_path}", file=sys.stderr)
        return 2

    sites = load_sites(sites_path)
    sites = [s for s in sites if s.reliability >= args.min_reliability]
    if args.category:
        wanted = {c.lower() for c in args.category}
        sites = [s for s in sites if s.category.lower() in wanted]
    if not sites:
        print("error: no sites match the given filters", file=sys.stderr)
        return 2

    impersonate = not args.no_impersonate
    if any(s.needs_impersonation for s in sites) and not HAS_CURL_CFFI:
        print(
            "warning: curl_cffi is not installed; sites flagged with "
            "tls_fingerprint will likely return [   ?   ]. Install with: "
            "pip install curl_cffi",
            file=sys.stderr,
        )

    if len(variants) == 1:
        print(
            f"Phantom: searching for '{variants[0]}' across {len(sites)} sites...",
            file=sys.stderr,
        )
    else:
        print(
            f"Phantom: trying {len(variants)} variants of '{raw}' across "
            f"{len(sites)} sites = {len(variants) * len(sites)} requests "
            f"(use --exact to skip variants)",
            file=sys.stderr,
        )

    is_name_mode = len(raw.split()) >= 2
    hint: Optional[dict] = None
    if args.identity_hint:
        if not is_name_mode:
            print(
                "warning: --identity-hint is ignored outside name mode "
                "(input must be two or more space-separated tokens).",
                file=sys.stderr,
            )
        else:
            hint_path = Path(args.identity_hint)
            if not hint_path.is_file():
                print(f"error: --identity-hint file not found: {hint_path}", file=sys.stderr)
                return 2
            hint = _load_identity_hint(hint_path)
            if hint:
                bits = []
                if hint.get("country"): bits.append(f"country={hint['country']}")
                if hint.get("language"): bits.append(f"language={hint['language']}")
                print(
                    f"identity hint: filtering FOUND hits against {', '.join(bits)} "
                    f"(from {hint['source']})",
                    file=sys.stderr,
                )

    from cache import _RESUME_TTL, _CACHE_TTL
    cache = ResponseCache(
        enabled=not args.no_cache,
        ttl_seconds=_RESUME_TTL if args.resume else _CACHE_TTL,
    )
    if args.resume and cache.enabled:
        prior_entries = len(cache._mem)
        print(
            f"resume: loaded {prior_entries} cached verdict"
            f"{'s' if prior_entries != 1 else ''} (7d TTL)",
            file=sys.stderr,
        )
    if args.proxy:
        print(f"proxy: routing through {args.proxy}", file=sys.stderr)
    phantom = Phantom(
        sites,
        concurrency=args.concurrency,
        per_host_concurrency=args.per_host_concurrency,
        timeout=args.timeout,
        impersonate=impersonate,
        retry_on_transient=not args.no_retry,
        cache=cache,
        proxy=args.proxy,
    )

    async def _scan_and_correlate():
        results = await phantom.run_many(variants)

        # --- Cross-link expansion ---------------------------------------
        # After the first scan completes, walk every FOUND profile's
        # linked-account fields and queue any handles we haven't tried
        # yet. One hop deep — second-pass results aren't themselves
        # expanded, to keep latency bounded.
        if args.expand:
            from expand import discover_new_handles
            found_now = [
                r for _, rs in results for r in rs if r.exists is True
            ]
            new_handles = discover_new_handles(found_now, set(variants))
            if new_handles:
                print(
                    f"expand: found {len(new_handles)} linked handle"
                    f"{'s' if len(new_handles) != 1 else ''} "
                    f"({', '.join(new_handles[:6])}"
                    f"{'…' if len(new_handles) > 6 else ''}) — rescanning",
                    file=sys.stderr,
                )
                expand_results = await phantom.run_many(new_handles)
                results.extend(expand_results)

        if hint:
            n_filtered = _filter_results_by_hint(results, hint)
            if n_filtered:
                print(
                    f"identity hint: discarded {n_filtered} FOUND hit"
                    f"{'s' if n_filtered != 1 else ''} (location or language "
                    f"mismatch).",
                    file=sys.stderr,
                )
        emails: dict[str, dict] = {}
        if hunter_key:
            found_for_email = [
                r for _, rs in results for r in rs if r.exists is True
            ]
            if found_for_email:
                emails = await discover_emails(found_for_email, hunter_key)
                _attach_emails_to_found(results, emails)
        if args.no_identity:
            return results, None, [], emails, None, {}, None
        found_dicts: list[dict] = []
        for _, rs in results:
            for r in rs:
                if r.exists is True:
                    found_dicts.append(asdict(r))
        # Dedupe before correlation so a single profile reached via
        # several URL aliases doesn't inflate the photo-match cluster.
        found_dicts = _dedupe_same_site_dicts(found_dicts)
        deep_options = photo_deep.options_from_apis(enabled=args.photo_deep)
        overall, clusters, deep_evidence, face_map, photo_bytes_map = await build_overall_and_clusters(
            found_dicts, deep_options=deep_options,
        )
        return results, overall, clusters, emails, deep_evidence, face_map, photo_bytes_map

    start = time.monotonic()
    grouped, overall, clusters, emails, deep_evidence, face_map, photo_bytes_map = asyncio.run(_scan_and_correlate())
    elapsed = time.monotonic() - start
    cache.save()
    if args.resume and cache.enabled:
        total = cache.disk_hits + cache.fresh_writes
        if total:
            pct = 100 * cache.disk_hits / total
            print(
                f"resume: reused {cache.disk_hits}/{total} request"
                f"{'s' if total != 1 else ''} from cache ({pct:.0f}%)",
                file=sys.stderr,
            )

    # Compute confidence scores and attach tier labels to every FOUND result.
    # Done once here so all output paths (terminal, HTML, JSON, Markdown) share
    # the same scores without recomputing.
    _found_for_scoring, _, _ = _flatten(grouped)
    if _found_for_scoring:
        subject_name = getattr(overall, "display_name", None) or ""
        _confidence.score_all(_found_for_scoring, clusters or [], subject_name, raw)

    # Identity disambiguation — cluster found accounts into distinct identity groups.
    # Skipped when --no-cluster, --no-identity, or there are no found results.
    _dis_clusters = None
    no_cluster = getattr(args, "no_cluster", False)
    if not no_cluster and not args.no_identity and _found_for_scoring:
        _dis_clusters = _disambiguation.disambiguate(
            _found_for_scoring, clusters or [], raw
        )
        _disambiguation.attach_identity_fields(_found_for_scoring, _dis_clusters)

    if args.as_json:
        payload = _build_json_payload(grouped, raw, elapsed, overall, clusters, emails,
                                      deep_evidence, dis_clusters=_dis_clusters)
        print(json.dumps(payload, indent=2))
    elif not args.quiet:
        print_compact(grouped, elapsed, color, args.found_only,
                      show_all=getattr(args, "show_all", False),
                      dis_clusters=_dis_clusters)
        if emails:
            found_for_print, _, _ = _flatten(grouped)
            _print_emails_section(found_for_print, emails, color)
        # Identity summary only when clustering is off (clustering replaces it).
        if not args.found_only and no_cluster:
            _print_identity_summary(overall, clusters, color)

    # --- Watch mode: snapshot + diff -------------------------------------
    if args.watch:
        found_for_snapshot, _, _ = _flatten(grouped)
        snap = Snapshot.from_results(
            raw, [asdict(r) for r in found_for_snapshot]
        )
        history = load_history(raw)
        prev = history[-1] if history else None
        d = compute_diff(prev, snap)
        save_snapshot(snap)
        # Quiet mode: only output the diff (or nothing). Cron-friendly.
        if args.quiet:
            if not d.is_empty():
                print(render_diff_terminal(d, color))
        else:
            print()
            print(render_diff_terminal(d, color))

    if args.export:
        export_path = resolve_export_path(args.export, raw)
        if export_path.parent and not export_path.parent.exists():
            export_path.parent.mkdir(parents=True, exist_ok=True)
        export_report(grouped, raw, elapsed, export_path, overall, clusters, emails, deep_evidence, face_map, dark=args.dark, dis_clusters=_dis_clusters, photo_bytes_map=photo_bytes_map)
        print(
            f"{_c(color,'dim')}Report written to {export_path}{_c(color,'reset')}",
            file=sys.stderr,
        )

    sys.stdout.flush()
    return 0
