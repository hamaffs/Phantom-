"""Phantom LLM analyst — short, factual post-scan reasoning.

Reads the typed investigation graph and produces four artifacts via
the LLM (Anthropic or Groq, both via OpenAI-compatible chat API):

  - Dossier:       50-80 word factual paragraph
  - Contradictions: JSON list of factual conflicts in the graph
  - Pivots:        3 concrete next-steps for a human analyst
  - Adversarial:   only when real breach data is present
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from typing import Any, AsyncIterator, Optional

import aiohttp

import apis
from graph.model import Graph, Node


# Two backends, both via OpenAI-compatible /v1/chat/completions:
#   Anthropic — sk-ant-... via api.anthropic.com/v1 + claude-haiku-4-5
#   Groq     — gsk_... via api.groq.com/openai/v1 + llama-3.3-70b
# Configure: phantom --api add llm_{endpoint,model,api_key} VALUE
# Without a key, --analyze is silently skipped.
_DEFAULT_ENDPOINT = "https://api.anthropic.com/v1"
_DEFAULT_MODEL = "claude-haiku-4-5-20251001"
_REQUEST_TIMEOUT = 120.0


class LLMClient:
    """Async OpenAI-compatible chat client.

    `endpoint` is the base URL up to and including `/v1` (e.g.
    `http://localhost:11434/v1`). We POST to `{endpoint}/chat/completions`.
    `model` is whatever the backend recognizes (e.g. `llama3.1:8b`,
    `gpt-4o-mini`, `claude-3-5-sonnet-20241022`).
    """

    def __init__(
        self,
        endpoint: Optional[str] = None,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> None:
        self.endpoint = (endpoint or apis.get("llm_endpoint") or _DEFAULT_ENDPOINT).rstrip("/")
        self.model = model or apis.get("llm_model") or _DEFAULT_MODEL
        # Anthropic + Groq both require a real bearer token.
        self.api_key = api_key or apis.get("llm_api_key")

    @property
    def chat_url(self) -> str:
        return f"{self.endpoint}/chat/completions"

    @property
    def is_configured(self) -> bool:
        """True iff an API key is available. Used by callers (analyst,
        investigate, learn_site) to decide whether to skip LLM features
        entirely vs. propagate a hard error."""
        return bool(self.api_key)

    async def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.3,
        json_mode: bool = False,
        stream: bool = False,
        max_tokens: int = 1500,
    ) -> str:
        """One-shot completion. Returns the assistant's response text.

        When `json_mode=True`, asks the model for valid JSON and adds the
        `response_format` hint (most modern backends honor it; Ollama in
        2024+ does for many models).
        """
        if stream:
            chunks: list[str] = []
            async for piece in self.stream(system, user, temperature=temperature, max_tokens=max_tokens):
                chunks.append(piece)
            return "".join(chunks)

        body = self._build_body(system, user, temperature, max_tokens, json_mode, stream=False)
        headers = self._headers()
        timeout = aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT)
        # Retry on transient API errors (429 rate-limit, 529 overloaded,
        # 503 service unavailable - both Anthropic and Groq use these).
        # Schedule covers up to ~80s of backoff because Anthropic's
        # "overloaded" outages routinely last 30-90s; capping at 12s
        # (the old budget) lost the dossier on every minor wobble. The
        # backoffs grow to keep retries cheap when the API recovers
        # quickly, but stay patient if it doesn't.
        # Backoff schedule. Honors server retry-after when provided
        # (capped at 60s so a misbehaving header can't strand us). Total
        # max wait is ~80s, which covers the typical 30-90s Anthropic
        # "overloaded" outage window. Mutate `next_delay` rather than
        # the schedule list so the override applies to the *next*
        # sleep, not the snapshot enumerate took at loop start.
        backoffs = [3.0, 8.0, 20.0, 45.0]
        last_err: Optional[str] = None
        next_delay: Optional[float] = None
        for attempt in range(len(backoffs) + 1):
            if attempt > 0:
                delay = next_delay if next_delay is not None else backoffs[attempt - 1]
                next_delay = None
                # Surface retry waits to stderr so the user knows the
                # analyst isn't hung - a 45s silent sleep felt like a
                # freeze, multiple users reported it.
                print(
                    f"  llm: {last_err}; retrying in {delay:.0f}s "
                    f"({attempt}/{len(backoffs)})...",
                    file=sys.stderr,
                )
                await asyncio.sleep(delay)
            try:
                async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                    async with session.post(self.chat_url, json=body) as resp:
                        if resp.status in (429, 529, 503) and attempt < len(backoffs):
                            ra = resp.headers.get("retry-after", "")
                            try:
                                hinted = float(ra)
                                if 1.0 <= hinted <= 60.0:
                                    next_delay = hinted
                            except (TypeError, ValueError):
                                pass
                            last_err = f"HTTP {resp.status}"
                            continue
                        if resp.status != 200:
                            text = await resp.text(errors="replace")
                            raise RuntimeError(
                                f"LLM request failed: HTTP {resp.status} from "
                                f"{self.chat_url}: {text[:200]}"
                            )
                        data = await resp.json(content_type=None)
                        break
            except aiohttp.ClientError as e:
                if attempt < len(backoffs):
                    last_err = f"ClientError: {type(e).__name__}"
                    continue
                raise RuntimeError(
                    f"LLM connection failed at {self.endpoint}: {e}"
                ) from e
        else:
            raise RuntimeError(f"LLM request failed after retries: {last_err}")

        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise RuntimeError(f"unexpected response shape from LLM: {data}")

    async def stream(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.3,
        max_tokens: int = 1500,
    ) -> AsyncIterator[str]:
        """Yield response chunks as they arrive. Suitable for live dossier output."""
        body = self._build_body(system, user, temperature, max_tokens, json_mode=False, stream=True)
        headers = self._headers()
        timeout = aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT)
        # Same retry policy as complete() - see comments there.
        backoffs = [3.0, 8.0, 20.0, 45.0]
        last_err: Optional[str] = None
        next_delay: Optional[float] = None
        for attempt in range(len(backoffs) + 1):
            if attempt > 0:
                delay = next_delay if next_delay is not None else backoffs[attempt - 1]
                next_delay = None
                print(
                    f"  llm-stream: {last_err}; retrying in {delay:.0f}s "
                    f"({attempt}/{len(backoffs)})...",
                    file=sys.stderr,
                )
                await asyncio.sleep(delay)
            try:
                async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                    async with session.post(self.chat_url, json=body) as resp:
                        if resp.status in (429, 529, 503) and attempt < len(backoffs):
                            ra = resp.headers.get("retry-after", "")
                            try:
                                hinted = float(ra)
                                if 1.0 <= hinted <= 60.0:
                                    next_delay = hinted
                            except (TypeError, ValueError):
                                pass
                            last_err = f"HTTP {resp.status}"
                            continue
                        if resp.status != 200:
                            text = await resp.text(errors="replace")
                            raise RuntimeError(
                                f"LLM stream failed: HTTP {resp.status}: {text[:200]}"
                            )
                        async for raw in resp.content:
                            line = raw.decode("utf-8", errors="replace").strip()
                            if not line or not line.startswith("data:"):
                                continue
                            payload = line[5:].strip()
                            if payload == "[DONE]":
                                return   # generator done
                            try:
                                chunk = json.loads(payload)
                                piece = chunk["choices"][0]["delta"].get("content", "")
                                if piece:
                                    yield piece
                            except (json.JSONDecodeError, KeyError, IndexError):
                                continue
                        return   # consumed full stream
            except aiohttp.ClientError as e:
                if attempt < len(backoffs):
                    last_err = f"ClientError: {type(e).__name__}"
                    continue
                raise RuntimeError(f"LLM stream connection failed: {e}") from e

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _build_body(self, system, user, temperature, max_tokens, json_mode, stream) -> dict:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        # json_mode is enforced via prompt only - backends differ on the
        # `response_format` shape (Anthropic wants json_schema, OpenAI/Groq
        # want json_object). Dropping the hint and relying on the system
        # prompt + _safe_json's robust parser works across all of them.
        return body


def llm_available() -> bool:
    """Module-level shortcut: True iff Phantom can talk to an LLM.

    Returns False when no `llm_api_key` is configured. Lets cli.py
    skip LLM features cleanly so Phantom remains fully usable without
    any AI backend at all (scan + graph + export still work)."""
    return bool(apis.get("llm_api_key"))


# ---------------------------------------------------------------------------# Graph → compact text (the prompt input)# ---------------------------------------------------------------------------
def graph_to_compact_text(g: Graph, *, max_chars: int = 12_000) -> str:
    """Serialize the graph into a compact human-readable form for the LLM.

    Order: Identities first (clusters), then Accounts grouped by Identity,
    then loose Photos, Emails (with breach data inline), Bios, Locations,
    Urls. Truncated to `max_chars` so even tight context windows fit.
    """
    out: list[str] = []

    # Identities and what they own.
    identities = list(g.nodes("Identity"))
    if identities:
        out.append("== IDENTITY CLUSTERS ==")
        for i, ident in enumerate(identities, 1):
            display = ident.attrs.get("display_name") or "(unnamed)"
            owned: list[Node] = []
            for e in g.edges(kind="owns", src=ident.id):
                n = g.get(e.dst)
                if n:
                    owned.append(n)
            sites = ", ".join(
                f"{a.attrs.get('site','?')}/{a.attrs.get('handle','?')}"
                for a in owned if a.kind == "Account"
            )
            out.append(f"  [{i}] {display}  ({len(owned)} accounts: {sites})")
        out.append("")

    # Accounts.
    accounts = list(g.nodes("Account"))
    if accounts:
        out.append(f"== ACCOUNTS ({len(accounts)}) ==")
        for a in accounts:
            site = a.attrs.get("site") or "?"
            handle = a.attrs.get("handle") or "?"
            display = a.attrs.get("display_name")
            bits = [f"{site}/@{handle}"]
            if display and display != handle:
                bits.append(f"display={display!r}")
            for k in ("follower_count", "joined_date", "created_at",
                      "verified", "language_label", "tier", "score"):
                v = a.attrs.get(k)
                if v not in (None, "", []):
                    bits.append(f"{k}={v}")
            # GitHub-specific richness
            ssh = a.attrs.get("ssh_keys")
            if ssh:
                bits.append(f"ssh_keys={len(ssh)}")
            orgs = a.attrs.get("organizations")
            if orgs:
                bits.append(f"orgs={orgs}")
            starred = a.attrs.get("recent_starred")
            if starred:
                names = [r.get("name") for r in starred if isinstance(r, dict)]
                bits.append(f"recent_starred={names}")
            out.append("  - " + " | ".join(bits))
        out.append("")

    # Emails + breach attachments.
    emails = list(g.nodes("Email"))
    if emails:
        out.append(f"== EMAILS ({len(emails)}) ==")
        for em in emails:
            addr = em.attrs.get("address") or "?"
            line = f"  - {addr}"
            for k in ("gravatar_display_name", "gravatar_location",
                      "gravatar_description", "infostealer_victim",
                      "infostealer_log_count", "leaked_password_count",
                      "xposedornot_breach_count"):
                v = em.attrs.get(k)
                if v not in (None, "", [], False):
                    line += f"  {k}={v}"
            # Inline a sample of leaked passwords if present (user opted in)
            sample = em.attrs.get("leaked_password_sample")
            if sample:
                line += f"  leaked_passwords_sample={sample[:5]}"
            out.append(line)
            # Attached breaches
            for e in g.edges(kind="appeared_in", src=em.id):
                b = g.get(e.dst)
                if b:
                    bn = b.attrs.get("name") or "?"
                    bd = (b.attrs.get("breach_date") or "")[:10]
                    via = b.attrs.get("via") or ""
                    out.append(f"      └ breach: {bn}  date={bd}  via={via}")
        out.append("")

    # Photos.
    photos = list(g.nodes("Photo"))
    if photos:
        out.append(f"== PHOTOS ({len(photos)}) ==")
        cross_matches = sum(
            1 for e in g.edges(kind="same_as")
            if e.src.startswith("Photo:") and e.dst.startswith("Photo:")
        )
        out.append(f"  cross-platform pHash matches: {cross_matches}")
        for p in photos[:10]:
            via = p.attrs.get("via") or "scan"
            exif = p.attrs.get("exif") or {}
            line = f"  - via={via}"
            if exif:
                line += f"  exif_fields={list(exif.keys())[:6]}"
                if "gps_latitude" in exif:
                    line += f"  GPS=({exif['gps_latitude']},{exif['gps_longitude']})"
            out.append(line)
        out.append("")

    # Bios.
    bios = list(g.nodes("Bio"))
    if bios:
        out.append(f"== BIOS ({len(bios)}) ==")
        for b in bios:
            text = (b.attrs.get("text") or "").replace("\n", " ")
            if len(text) > 200:
                text = text[:200] + "…"
            lang = b.attrs.get("language") or ""
            out.append(f"  - [{lang or '?'}] {text!r}")
        out.append("")

    # Locations.
    locs = list(g.nodes("Location"))
    if locs:
        out.append(f"== LOCATIONS ({len(locs)}) ==")
        for l in locs:
            label = l.attrs.get("label")
            country = l.attrs.get("country")
            via = l.attrs.get("via") or ""
            line = f"  - {label!r}"
            if country: line += f" (country={country})"
            if via: line += f" via={via}"
            out.append(line)
        out.append("")

    # Urls (outbound links from bios / Gravatar verified accounts).
    urls = list(g.nodes("Url"))
    if urls:
        out.append(f"== LINKED URLS ({len(urls)}) ==")
        for u in urls[:30]:
            via = u.attrs.get("via") or ""
            out.append(f"  - {u.attrs.get('url')!r}  via={via}")
        if len(urls) > 30:
            out.append(f"  … and {len(urls)-30} more")
        out.append("")

    # Wallets.
    wallets = list(g.nodes("Wallet"))
    if wallets:
        out.append(f"== CRYPTO WALLETS ({len(wallets)}) ==")
        for w in wallets:
            out.append(f"  - {w.attrs.get('chain')}: {w.attrs.get('address')}  ens={w.attrs.get('ens_name')}")
        out.append("")

    # Domains with WHOIS data.
    domains_with_whois = [
        d for d in g.nodes("Domain")
        if any(k.startswith("whois_") for k in d.attrs)
    ]
    if domains_with_whois:
        out.append(f"== WHOIS ({len(domains_with_whois)}) ==")
        for d in domains_with_whois:
            host = d.attrs.get("host")
            reg = d.attrs.get("whois_registrar_name") or "?"
            created = (d.attrs.get("whois_registration") or "")[:10]
            out.append(f"  - {host}  registrar={reg}  created={created}")
        out.append("")

    text = "\n".join(out)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n… (truncated at {max_chars} chars)"
    return text


# ---------------------------------------------------------------------------# Prompt builders# ---------------------------------------------------------------------------
_DOSSIER_SYSTEM = """\
You are a senior OSINT analyst writing for another analyst. Output ONE
dense paragraph of factual findings about the target.

HARD CONSTRAINTS:
- Maximum 80 words. Aim for 50-70. SHORTER IS ALWAYS BETTER.
- One paragraph. No headings, no bullets, no markdown.
- Start with the strongest fact. No "The target appears to be...".
- No hedging: cut "likely", "may suggest", "could indicate", "appears to". State or omit.
- No meta-sentences. Don't describe what you're describing.
- Cite specific evidence inline (handle, date, count) — never generic.
- End with the single biggest evidence gap. One short clause.

FORBIDDEN phrases (auto-rejected style):
- "moderately strong", "deliberately privacy-conscious", "operates a"
- "without additional", "further investigation would require"
- "shows technical interest", "suggests engagement with"
- "indicating active", "consistent with", "presents a"

Style target: an OSINT-report INFOBOX written as one tight paragraph.
Every clause carries a verifiable fact. Empty areas get one word: "none".
"""


_CONTRADICTIONS_SYSTEM = """\
Identify FACTUAL contradictions between evidence items in the graph.

A contradiction is two pieces of evidence that CANNOT BOTH be true,
not stylistic variation.

IS a contradiction:
- claimed birth year vs snowflake account creation date
- bio location vs EXIF GPS coordinates on photos
- claimed employer vs verified-accounts domain
- "joined since 2015" in bio vs account created 2023

NOT a contradiction (do not report these):
- Different display names across platforms (people do this on purpose)
- Empty/placeholder bios vs filled ones
- Username variation like "user" vs "user_official"
- Cross-platform photo differences (people use different photos)
- Tier disagreements in our own scoring system

Output ONLY this JSON:
{"contradictions": [{"claim_a": "...", "claim_b": "...", "evidence": "graph node IDs or platform names"}]}

Max 4 entries. If none qualify, return {"contradictions": []}.
"""


_PIVOTS_SYSTEM = """\
Suggest concrete next investigation actions. ONE action per pivot.

Output ONLY this JSON:
{"pivots": [{"action": "...", "why": "1-sentence rationale"}]}

ACTION RULES:
- Max 25 words per action.
- Must name a specific URL, handle, fingerprint, or address. Never generic.
- Single operation, not a research thread.
- Must NOT duplicate work the graph already shows was done:

ALREADY DONE BY PHANTOM (do not re-suggest):
  - Handle correlation across all 100+ sites
  - Photo pHash/dHash/wHash cross-platform matching
  - Snowflake decoding for Twitter/Discord/Instagram IDs
  - HudsonRock infostealer lookup (every Email node)
  - ProxyNova combo-list password lookup (every Email node)
  - XposedOrNot public breach lookup (every Email node)
  - Gravatar v3 profile lookup (every Email node)
  - holehe Twitter registration probe (every Email node)
  - GitHub .keys, .gpg, orgs, starred, commit-email leak
  - Wayback Machine (if requested via --wayback)
  - ENS resolution from .eth strings in bios

BANNED phrases:
  - "Extract and analyze..." / "Investigate X for patterns"
  - "Cross-reference X with Y" (too vague — name the operation)
  - "Run HIBP" / "Run breach lookups" (already done)
  - "Search GitHub/GitLab/Gitea instances and SSH key databases" (be one specific query)
  - "Expected:" — do NOT include expected outcomes; rationale is enough

Max 4 pivots. Quality over quantity. If nothing new is genuinely useful,
return {"pivots": []}.

GOOD pivot examples (use these as the template — name a specific URL or fingerprint):
  {"action": "Visit instagram.com/<handle_variant> (URL listed in graph but not scanned).", "why": "Bio-link variant outside handle correlation's set."}
  {"action": "GitHub search for commits author-keyid:<sha256> matching the SSH key on the Account node.", "why": "Other repos using the same key would extend the technical footprint."}
"""


_ADVERSARIAL_SYSTEM = """\
Generate red-team intelligence ONLY when supported by real evidence in
the graph. Empty arrays are correct and preferred over fabrication.

Output ONLY this JSON:
{
  "passwords_from_leaks": [],
  "phishing_angles": [],
  "missing_for_red_team": "1-sentence summary of what's needed for real red-team work"
}

STRICT RULES:

passwords_from_leaks:
- ONLY populate when the graph contains a "leaked_password_sample" or
  similar field on an Email node (from ProxyNova).
- The candidates must be the EXACT leaked strings OR direct mutations
  (e.g. "abc123" → "Abc123!", "abc1234").
- DO NOT generate passwords from name + handle + birth-year guesses.
  Those are not real passwords. Return empty array if no leaks.

phishing_angles:
- Each angle MUST cite the specific evidence point that makes it work
  (a named starred repo, a verified social account, a domain).
- Max 3. NEVER speculate "they probably like X".
- Format: [{"angle": "specific pretext", "based_on": "exact evidence point"}]
- If no verified evidence supports a tailored phishing angle, return [].

missing_for_red_team:
- One factual sentence. Examples:
  - "No email recovered; password and breach posture cannot be assessed."
  - "No phone numbers; SMS-pretext angles unavailable."
  - "EXIF stripped on all platforms; no device fingerprint."

DO NOT generate "security question guesses". Those are fabricated guesses
with no basis in evidence. We removed that section.
"""


# ---------------------------------------------------------------------------# Analyst functions# ---------------------------------------------------------------------------
def _evidence_prompt(g: Graph) -> str:
    """Build the user-side prompt — the typed graph + an explicit
    'already done' notice so the LLM doesn't suggest re-running work
    that's already in the evidence pack."""
    return (
        "Graph evidence:\n\n"
        + graph_to_compact_text(g)
        + "\n\nNOTE: The following enrichments have ALREADY been performed "
          "and their results are encoded in the graph above. Do NOT "
          "suggest re-running them:\n"
          "- Cross-platform handle correlation (every site Phantom scans)\n"
          "- Photo pHash/dHash/wHash matching\n"
          "- Snowflake decoding for Twitter/Discord/Instagram IDs\n"
          "- HudsonRock infostealer-victim lookup on every Email node\n"
          "- ProxyNova combo-list password lookup on every Email node\n"
          "- XposedOrNot public-breach lookup on every Email node\n"
          "- Gravatar v3 profile lookup\n"
          "- GitHub .keys / .gpg / orgs / starred / commit-email harvest\n"
          "- ENS resolution from .eth strings in bios\n"
    )


async def dossier(
    client: LLMClient, g: Graph, *, stream: bool = True
) -> str:
    """Generate a narrative dossier. ≤150 words, single paragraph,
    factual. When stream=True, prints chunks to stdout as they arrive."""
    prompt = _evidence_prompt(g)
    # 600 tokens caps at roughly 400 words of safety margin while the
    # prompt asks for ≤150. Lower temperature than before - want
    # the model factual, not creative.
    if stream:
        chunks: list[str] = []
        async for piece in client.stream(
            _DOSSIER_SYSTEM, prompt, temperature=0.15, max_tokens=300,
        ):
            sys.stdout.write(piece)
            sys.stdout.flush()
            chunks.append(piece)
        sys.stdout.write("\n")
        return "".join(chunks)
    return await client.complete(
        _DOSSIER_SYSTEM, prompt, temperature=0.15, max_tokens=300,
    )


async def contradictions(client: LLMClient, g: Graph) -> dict:
    """Detect inconsistencies. Returns a dict matching the
    {"contradictions": [...]} shape, or {"contradictions": []} on parse failure."""
    raw = await client.complete(
        _CONTRADICTIONS_SYSTEM, _evidence_prompt(g),
        temperature=0.1, json_mode=True, max_tokens=500,
    )
    return _safe_json(raw, {"contradictions": []})


async def pivots(client: LLMClient, g: Graph) -> dict:
    raw = await client.complete(
        _PIVOTS_SYSTEM, _evidence_prompt(g),
        temperature=0.2, json_mode=True, max_tokens=500,
    )
    return _safe_json(raw, {"pivots": []})


async def adversarial_profile(client: LLMClient, g: Graph) -> dict:
    raw = await client.complete(
        _ADVERSARIAL_SYSTEM, _evidence_prompt(g),
        temperature=0.1, json_mode=True, max_tokens=500,
    )
    default = {
        "passwords_from_leaks": [],
        "phishing_angles": [],
        "missing_for_red_team": "(no analyst output)",
    }
    return _safe_json(raw, default)


# ---------------------------------------------------------------------------# Helpers# ---------------------------------------------------------------------------
def _safe_json(raw: str, default: dict) -> dict:
    """Try hard to parse a JSON object out of the model's reply.

    Models often wrap JSON in markdown fences or prefix with prose. We
    strip those and try again. Returns `default` if all attempts fail.
    """
    if not raw:
        return default
    # Strip common markdown fences.
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Try to extract the first {...} block.
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    print(
        f"analyst: could not parse JSON from model response "
        f"(got {len(raw)} chars). Returning default.",
        file=sys.stderr,
    )
    return default


# ---------------------------------------------------------------------------# Convenience wrapper - runs all four analyses# ---------------------------------------------------------------------------
async def analyze_all(
    g: Graph, *, client: Optional[LLMClient] = None, stream_dossier: bool = True,
) -> dict:
    """Run dossier + contradictions + pivots + adversarial concurrently.

    The four LLM calls are independent. Firing them in parallel via
    asyncio.gather cuts wall time ~3-4x. The dossier still streams to
    stdout as it arrives (it's a single call kicked off with the others);
    the three JSON results print together once everything finishes.

    Returns a dict with all four results.
    """
    client = client or LLMClient()
    out: dict[str, Any] = {
        "endpoint": client.endpoint,
        "model": client.model,
    }

    print(f"\n=== Dossier ({client.model}) ===", file=sys.stderr)
    if stream_dossier:
        print(
            "(dossier streams below; contradictions + pivots + adversarial "
            "run concurrently and print when complete)\n",
            file=sys.stderr,
        )

    dossier_task = asyncio.create_task(dossier(client, g, stream=stream_dossier))
    contradictions_task = asyncio.create_task(contradictions(client, g))
    pivots_task = asyncio.create_task(pivots(client, g))
    adversarial_task = asyncio.create_task(adversarial_profile(client, g))

    results = await asyncio.gather(
        dossier_task, contradictions_task, pivots_task, adversarial_task,
        return_exceptions=True,
    )
    dossier_r, contradictions_r, pivots_r, adversarial_r = results

    # Coerce any RuntimeError into a safe default so one slow / failing
    # call doesn't lose the rest of the output.
    out["dossier"] = dossier_r if isinstance(dossier_r, str) else f"[error: {dossier_r}]"
    out["contradictions"] = (
        contradictions_r if isinstance(contradictions_r, dict)
        else {"contradictions": [], "error": str(contradictions_r)}
    )
    out["pivots"] = (
        pivots_r if isinstance(pivots_r, dict)
        else {"pivots": [], "error": str(pivots_r)}
    )
    out["adversarial"] = (
        adversarial_r if isinstance(adversarial_r, dict)
        else {"likely_passwords": [], "error": str(adversarial_r)}
    )

    print(f"\n=== Contradictions ===", file=sys.stderr)
    print(json.dumps(out["contradictions"], indent=2))
    print(f"\n=== Suggested Pivots ===", file=sys.stderr)
    print(json.dumps(out["pivots"], indent=2))
    print(f"\n=== Adversarial Profile ===", file=sys.stderr)
    print(json.dumps(out["adversarial"], indent=2))

    return out
