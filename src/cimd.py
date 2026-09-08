"""
Client ID Metadata Documents: a client_id that is a URL you can fetch.

Why this exists
---------------
Dynamic client registration (RFC 7591) works, and it is what every MCP client
does today. It also means a row in our database for every install of every
client, forever, created by anyone who can POST -- which is the whole reason
`oauth.py` now caps and sweeps them.

A CIMD client_id is the other answer. The client_id IS an https URL, and the
document at that URL lists the client's metadata:

    {"client_id": "https://smithery.ai/oauth/client",
     "client_name": "Smithery",
     "redirect_uris": ["https://smithery.ai/oauth/callback"]}

We fetch it, check it, and use it for that one flow. Nothing is written down,
so there is nothing to cap and nothing to sweep, and the operator of that URL
controls their own metadata. Smithery asks for this before it will proxy a
remote OAuth server, and it is on the MCP spec's track for the same reason.

DCR is untouched: a `client_id` that is not an https URL still goes to the
database exactly as before. This is an additional shape, not a replacement.

What is checked, and why each check is here
-------------------------------------------
* **https only.** A client_id that is a URL is a claim we resolve over the
  network on behalf of a user mid-sign-in. Over http that claim is whatever
  the nearest network is willing to say it is.
* **A public ADDRESS, not just a public-looking name.** No IP literals, no
  `localhost`, no `.local`, no userinfo -- and then the hostname is RESOLVED
  and every address it answers with has to be a public one. The name check
  alone is not a control: `127.0.0.1.nip.io` is a perfectly ordinary
  hostname with a dot in it that resolves to loopback, and without the
  resolution step this endpoint is a request-forgery primitive that anyone
  can reach unauthenticated. A name that resolves to a private, loopback,
  link-local, multicast or reserved address is refused before a socket is
  opened.
* **No redirects followed.** A 302 to somewhere else is the same bypass as a
  private address, one hop later.
* **A size cap and a short timeout.** This fetch is inline in a page load a
  human is waiting on; a document that streams forever must not become a
  stuck worker.
* **The document's own `client_id`, if present, must equal the URL.** A
  document that names a different client_id is either misconfigured or is
  somebody else's document being pointed at.
* **The redirect_uri must be listed in the document.** This is the one that
  matters: it is what stops an attacker who found a CIMD URL from having its
  codes delivered somewhere else.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import socket
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

#: Bytes. A client metadata document is a few hundred; anything approaching
#: this is not one.
MAX_DOCUMENT_BYTES = 64 * 1024
FETCH_TIMEOUT_SECONDS = 5.0
#: How long a fetched document is reused. Long enough that authorize and the
#: token exchange that follows it seconds later cost one fetch; short enough
#: that a client that rotates its redirect URIs is not stuck with the old set
#: for an afternoon.
CACHE_TTL_SECONDS = 600
CACHE_MAX_ENTRIES = 512


class CimdError(Exception):
    """The document is missing, unreachable, or not usable as it stands."""


def is_cimd_client_id(client_id: str) -> bool:
    """Whether this client_id should be resolved as a URL rather than a row.

    Deliberately narrow: an https URL and nothing else. Our own registered
    ids start `fpcl_`, so the two shapes cannot be confused, and a client_id
    we do not recognise as a URL keeps the exact behaviour it has today.
    """
    value = (client_id or "").strip()
    if not value.lower().startswith("https://"):
        return False
    parts = urlsplit(value)
    return bool(parts.netloc) and not parts.fragment


def _check_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme.lower() != "https":
        raise CimdError("a client_id URL must be https")
    if parts.username or parts.password:
        raise CimdError("a client_id URL must not carry credentials")
    if parts.fragment:
        raise CimdError("a client_id URL must not have a fragment")
    host = (parts.hostname or "").lower()
    if not host:
        raise CimdError("a client_id URL must have a host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        raise CimdError("a client_id URL must name a hostname, not an IP address")
    if host in ("localhost",) or host.endswith((".localhost", ".local", ".internal")):
        raise CimdError("a client_id URL must name a public host")
    if "." not in host:
        raise CimdError("a client_id URL must name a public host")


def _is_public_address(value: str) -> bool:
    """Whether we are willing to open a connection to this address.

    Everything that is not ordinary public unicast is refused: loopback,
    RFC1918 and the rest of the private ranges, link-local (which is where
    169.254.169.254 lives), multicast, reserved and the unspecified address.
    IPv4-mapped IPv6 is unwrapped first, because `::ffff:127.0.0.1` is
    loopback wearing a different hat.
    """
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


async def resolve_host(host: str) -> tuple[str, ...]:
    """Every address `host` answers with. Replaced wholesale in tests.

    Separate from the check below so the check has something to check: the
    guard that matters is "what does this name actually point at", and that
    question can only be answered by resolving it.
    """
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError) as exc:
        raise CimdError("the client_id host does not resolve") from exc
    return tuple(str(info[4][0]) for info in infos)


async def check_host_addresses(host: str) -> None:
    """Refuse a hostname that points anywhere we should not be calling."""
    addresses = await resolve_host(host)
    if not addresses:
        raise CimdError("the client_id host does not resolve")
    if not all(_is_public_address(a) for a in addresses):
        # Deliberately does NOT say which address: the answer is the thing
        # the prober is asking for.
        raise CimdError("a client_id URL must resolve to a public address")


def build_http_client(timeout: float = FETCH_TIMEOUT_SECONDS) -> httpx.AsyncClient:
    """The client used for the fetch. Replaced wholesale in tests.

    `follow_redirects=False` is load-bearing, not a default: see the module
    docstring.
    """
    return httpx.AsyncClient(timeout=timeout, follow_redirects=False)


async def fetch_document(url: str) -> dict[str, Any]:
    """The parsed document at `url`, with every network guard applied."""
    _check_url(url)
    await check_host_addresses((urlsplit(url).hostname or "").lower())
    client = build_http_client()
    try:
        try:
            # STREAMED, not `client.get(...).content`. Buffering first and
            # truncating afterwards means the whole body is already in this
            # process's memory before the cap is consulted, and the read
            # timeout is per chunk, not per response -- so a server that
            # dribbles bytes could hand us gigabytes inside the "5 second"
            # budget. Reading with a running total aborts the connection at
            # the cap instead.
            async with client.stream(
                "GET", url, headers={"accept": "application/json"}
            ) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    raise CimdError(
                        "the client metadata document redirected; that is not followed"
                    )
                if response.status_code != 200:
                    raise CimdError(
                        f"the client metadata document answered {response.status_code}"
                    )
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_DOCUMENT_BYTES:
                        raise CimdError("the client metadata document is too large")
                    chunks.append(chunk)
                body = b"".join(chunks)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise CimdError(
                f"could not fetch the client metadata document: {type(exc).__name__}"
            ) from exc
    finally:
        await client.aclose()

    try:
        document = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise CimdError("the client metadata document is not JSON") from exc
    if not isinstance(document, dict):
        raise CimdError("the client metadata document is not a JSON object")
    declared = str(document.get("client_id") or "").strip()
    if declared and declared != url:
        raise CimdError(
            "the client metadata document declares a different client_id"
        )
    return document


_cache: dict[str, tuple[float, dict[str, Any]]] = {}


async def load(url: str, now: float | None = None) -> dict[str, Any]:
    """`fetch_document`, with a small per-process cache.

    Failures are NOT cached. A document that was unreachable for one second
    should not lock a client out for ten minutes, and the fetch is cheap.
    """
    now = now if now is not None else time.time()
    hit = _cache.get(url)
    if hit is not None and hit[0] > now:
        return hit[1]
    document = await fetch_document(url)
    if len(_cache) >= CACHE_MAX_ENTRIES:
        for stale in [k for k, (expiry, _) in _cache.items() if expiry <= now]:
            del _cache[stale]
        if len(_cache) >= CACHE_MAX_ENTRIES:
            _cache.clear()
    _cache[url] = (now + CACHE_TTL_SECONDS, document)
    return document


def clear_cache() -> None:
    _cache.clear()


def redirect_uris(document: dict[str, Any]) -> tuple[str, ...]:
    """The document's redirect_uris, or a CimdError saying why not."""
    raw = document.get("redirect_uris")
    if not isinstance(raw, list) or not raw:
        raise CimdError(
            "the client metadata document lists no redirect_uris"
        )
    uris = tuple(str(u).strip() for u in raw if isinstance(u, str) and str(u).strip())
    if not uris:
        raise CimdError("the client metadata document lists no usable redirect_uris")
    return uris


def client_name(document: dict[str, Any], url: str) -> str:
    name = document.get("client_name")
    if isinstance(name, str) and name.strip():
        return name.strip()[:120]
    return (urlsplit(url).hostname or url)[:120]
