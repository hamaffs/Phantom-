"""ENS resolver: ENS names mentioned in bios/display_names → Wallet nodes.

Most crypto-aware users put their `.eth` name somewhere visible —
display name, bio, Twitter handle, etc. This transform regexes for
`*.eth` strings on Bio + Account nodes, then resolves each via a public
Ethereum RPC (Cloudflare's, no key needed).

ENS resolution via raw JSON-RPC:
  1. namehash(name) — keccak256-based recursive label hash.
  2. ENS Registry.resolver(node) → resolver contract address.
  3. Resolver.addr(node) → 20-byte Ethereum address.

We deliberately use HTTP JSON-RPC rather than `web3.py` to avoid adding
a heavy dep tree. Namehash is implemented locally — it's small.

When resolution succeeds we emit a Wallet(chain="ethereum") node with a
`linked` edge (role="ens_owner") from the source Bio/Account.

Public RPCs are best-effort. Cloudflare is the default; on failure we
fall back to ankr's public endpoint, then llamarpc.
"""
from __future__ import annotations

import asyncio
import re
import sys
from typing import Optional

import aiohttp

from graph.model import Graph, Node
from graph.transforms import transform


_USER_AGENT = "Phantom-OSINT"
_TIMEOUT = 12.0

# Public RPCs in priority order. All free, no auth.
_RPCS = (
    "https://ethereum-rpc.publicnode.com",
    "https://rpc.eth.gateway.fm",
    "https://eth.drpc.org",
)

# ENS Registry contract address (mainnet, immutable).
_ENS_REGISTRY = "0x00000000000C2E074eC69A0dFb2997BA6C7d2e1e"

# Match `something.eth` (allow subdomains: vitalik.eth, sub.vitalik.eth).
# Disallow trailing chars that would suggest a file extension (e.g. .ether).
_ENS_RE = re.compile(
    r"(?<![A-Za-z0-9._-])"
    r"([a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?){0,2}\.eth)"
    r"(?![A-Za-z0-9])",
    re.IGNORECASE,
)


@transform(input="Bio", produces=("Wallet",))
async def ens_from_bio(node: Node, g: Graph) -> None:
    text = (node.attrs.get("text") or "").lower()
    for name in _extract_ens_names(text):
        await _resolve_and_emit(name, node, g, source_role="ens_in_bio")


@transform(input="Account", produces=("Wallet",))
async def ens_from_account(node: Node, g: Graph) -> None:
    dn = (node.attrs.get("display_name") or "").lower()
    handle = (node.attrs.get("handle") or "").lower()
    candidates: set[str] = set()
    for src in (dn, handle):
        for name in _extract_ens_names(src):
            candidates.add(name)
    for name in candidates:
        await _resolve_and_emit(name, node, g, source_role="ens_on_account")


def _extract_ens_names(text: str) -> list[str]:
    if not text:
        return []
    out: list[str] = []
    for m in _ENS_RE.finditer(text):
        name = m.group(1).lower().strip()
        # Reject obvious noise.
        if name.startswith("-") or name.startswith(".") or ".." in name:
            continue
        if name == ".eth":
            continue
        # Skip when the part before .eth is suspiciously short (single
        # char) - those don't exist on ENS anyway (3-char min for new
        # registrations, though some legacy 1/2-char names exist).
        head = name[:-4]
        if not head or head.startswith("-") or head.endswith("-"):
            continue
        out.append(name)
    # Dedup preserving order.
    seen = set()
    return [n for n in out if not (n in seen or seen.add(n))]


async def _resolve_and_emit(
    name: str, source_node: Node, g: Graph, *, source_role: str,
) -> None:
    addr = await resolve_ens(name)
    if not addr:
        return
    wallet = g.add_node(
        "Wallet",
        source="ens",
        chain="ethereum",
        address=addr.lower(),
        ens_name=name,
    )
    g.add_edge(source_node.id, wallet.id, "linked", role=source_role)


# ---------------------------------------------------------------------------# Namehash + RPC plumbing# ---------------------------------------------------------------------------
def _keccak256(data: bytes) -> bytes:
    """Keccak-256 (Ethereum / pre-NIST-SHA3). Tries fast backends first,
    falls back to a tiny pure-Python implementation so we never need an
    optional dep just for ENS namehash."""
    # 1. Fast backends if present.
    try:
        import sha3  # type: ignore
        return sha3.keccak_256(data).digest()
    except ImportError:
        pass
    try:
        from Crypto.Hash import keccak  # type: ignore
        k = keccak.new(digest_bits=256)
        k.update(data)
        return k.digest()
    except ImportError:
        pass
    try:
        from eth_hash.auto import keccak as _k  # type: ignore
        return _k(data)
    except ImportError:
        pass
    # 2. Pure-Python fallback. Slow but correct.
    return _keccak256_pure(data)


# ---------------------------------------------------------------------------# Pure-Python Keccak-256 (~80 lines).# Implements Keccak-f[1600] with rate=1088 bits, capacity=512 bits, output=256.# Critical: padding byte is 0x01 (Keccak), NOT 0x06 (NIST SHA-3).# ---------------------------------------------------------------------------
_KECCAK_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A,
    0x8000000080008000, 0x000000000000808B, 0x0000000080000001,
    0x8000000080008081, 0x8000000000008009, 0x000000000000008A,
    0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089,
    0x8000000000008003, 0x8000000000008002, 0x8000000000000080,
    0x000000000000800A, 0x800000008000000A, 0x8000000080008081,
    0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]
_KECCAK_R = [
    [0, 36, 3, 41, 18],
    [1, 44, 10, 45, 2],
    [62, 6, 43, 15, 61],
    [28, 55, 25, 21, 56],
    [27, 20, 39, 8, 14],
]


def _rotl64(x: int, n: int) -> int:
    n %= 64
    return ((x << n) & 0xFFFFFFFFFFFFFFFF) | (x >> (64 - n))


def _keccak_f1600(state: list[list[int]]) -> None:
    for rnd in range(24):
        # Theta
        C = [state[x][0] ^ state[x][1] ^ state[x][2] ^ state[x][3] ^ state[x][4] for x in range(5)]
        D = [C[(x - 1) % 5] ^ _rotl64(C[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                state[x][y] ^= D[x]
        # Rho + Pi
        B = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                B[y][(2 * x + 3 * y) % 5] = _rotl64(state[x][y], _KECCAK_R[x][y])
        # Chi
        for x in range(5):
            for y in range(5):
                state[x][y] = B[x][y] ^ ((~B[(x + 1) % 5][y]) & 0xFFFFFFFFFFFFFFFF & B[(x + 2) % 5][y])
        # Iota
        state[0][0] ^= _KECCAK_RC[rnd]


def _keccak256_pure(data: bytes) -> bytes:
    rate_bytes = 136  # 1088 / 8
    # Padding: append 0x01, then zero-fill, then OR 0x80 into the final byte.
    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % rate_bytes != 0:
        padded.append(0x00)
    padded[-1] |= 0x80

    state = [[0] * 5 for _ in range(5)]
    for offset in range(0, len(padded), rate_bytes):
        block = padded[offset:offset + rate_bytes]
        for i in range(rate_bytes // 8):
            lane = int.from_bytes(block[i * 8:(i + 1) * 8], "little")
            x, y = i % 5, i // 5
            state[x][y] ^= lane
        _keccak_f1600(state)

    out = bytearray()
    for y in range(5):
        for x in range(5):
            out += state[x][y].to_bytes(8, "little")
            if len(out) >= 32:
                return bytes(out[:32])
    return bytes(out[:32])


def namehash(name: str) -> Optional[bytes]:
    """Compute the ENS namehash. Returns None if keccak isn't available."""
    node = b"\x00" * 32
    if not name:
        return node
    labels = name.split(".")
    for label in reversed(labels):
        lh = _keccak256(label.encode("utf-8"))
        if not lh:
            return None
        node = _keccak256(node + lh)
        if not node:
            return None
    return node


def _eth_call(to_addr: str, data_hex: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [
            {"to": to_addr, "data": data_hex},
            "latest",
        ],
    }


async def resolve_ens(name: str) -> Optional[str]:
    nh = namehash(name)
    if not nh:
        return None
    # ENS Registry.resolver(node) selector = 0x0178b8bf
    sel_resolver = "0x0178b8bf"
    payload_resolver = sel_resolver + nh.hex()
    resolver_addr = await _rpc_call_first_working(_ENS_REGISTRY, payload_resolver)
    if not resolver_addr or resolver_addr == "0x" + "00" * 20:
        return None
    # Resolver.addr(node) selector = 0x3b3b57de
    sel_addr = "0x3b3b57de"
    payload_addr = sel_addr + nh.hex()
    addr_hex = await _rpc_call_first_working(resolver_addr, payload_addr)
    if not addr_hex or addr_hex == "0x" + "00" * 20:
        return None
    return addr_hex


async def _rpc_call_first_working(to_addr: str, data: str) -> Optional[str]:
    """Try each public RPC in order; return the first 20-byte address result."""
    for url in _RPCS:
        try:
            timeout = aiohttp.ClientTimeout(total=_TIMEOUT)
            async with aiohttp.ClientSession(
                timeout=timeout,
                headers={"User-Agent": _USER_AGENT, "Content-Type": "application/json"},
            ) as session:
                async with session.post(url, json=_eth_call(to_addr, data)) as resp:
                    if resp.status != 200:
                        continue
                    body = await resp.json(content_type=None)
                    raw = (body or {}).get("result")
                    if not isinstance(raw, str) or not raw.startswith("0x"):
                        continue
                    # eth_call returns 32 bytes; address is the last 20.
                    if len(raw) < 2 + 64:
                        continue
                    addr = "0x" + raw[-40:]
                    if int(raw[-64:], 16) == 0:
                        return None
                    return addr
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            continue
        except Exception as e:
            print(f"ens: RPC {url} failed: {e}", file=sys.stderr)
            continue
    return None
