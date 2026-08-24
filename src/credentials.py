"""
Resolving the caller's own RapidAPI key.

This server proxies searches that are billed to whoever made them, so the key
is per request, not per deployment. The awkward part is that MCP clients have
no agreed way to pass one: every host invented its own, and a server that
supports only the mechanism its author tested is a server that mysteriously
fails for half its users.

So all of them are accepted, in this order:

1. `x-rapidapi-key` header -- the same header name RapidAPI itself uses, so a
   user who already has a working curl can paste it unchanged. Preferred:
   headers stay out of URLs, and therefore out of proxy and access logs.
2. `authorization: Bearer <key>` / `x-api-key` -- what hosts with a generic
   "API key" field usually emit.
3. A query parameter on the connector URL (`rapidapi_key`, `api_key`, `key`).
   Universal fallback: every host lets a user paste a URL, and not every host
   lets them add a header. claude.ai's custom-connector dialog is the case
   that matters.
4. Smithery's config convention -- `config.rapidApiKey=` dot-notation, or a
   base64-encoded JSON blob in `config=`. Smithery's gateway injects the
   user's saved configuration this way; without this branch every Smithery
   install of this server would arrive keyless.
5. `RAPIDAPI_KEY` in the server environment. Deliberately last, normally
   unset, and reported by /health so it cannot be on by accident: whenever it
   is set, every keyless caller bills the deployment owner's subscription.

A key is never logged, never echoed into an error message, and never returned
in a tool response. `redact` exists so diagnostics can say which source won
without saying what the value was.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from typing import Any

# Checked in order. The first non-empty match wins.
HEADER_NAMES = ("x-rapidapi-key", "x-api-key")
BEARER_HEADER = "authorization"
# Split deliberately, and the order between these two groups is load-bearing.
#
# A gateway that proxies to us puts ITS OWN key on the query string under a
# generic name. Smithery sends `?api_key=<smithery key>&config=<base64 of the
# user's config>` -- so accepting `api_key` before reading `config` means
# forwarding Smithery's key to RapidAPI, which fails auth on every single
# call. That shipped, and every Smithery install of this server would have
# told the user "no RapidAPI key was supplied" no matter what they entered.
#
# So: names that can only mean us first, then the config blob, then the
# generic names as a last resort.
SPECIFIC_QUERY_NAMES = (
    "rapidapi_key",
    "rapidapi-key",
    "rapidapikey",
)
GENERIC_QUERY_NAMES = (
    "api_key",
    "apikey",
    "key",
)
QUERY_NAMES = SPECIFIC_QUERY_NAMES + GENERIC_QUERY_NAMES
# Smithery lowercases nothing, so both casings of the same config field are
# checked. `config.` prefixed params are its dot-notation form.
CONFIG_QUERY_PREFIX = "config."
CONFIG_FIELD_NAMES = (
    "rapidapikey",
    "rapidapi_key",
    "rapidapi-key",
    "apikey",
    "api_key",
    "key",
)

# RapidAPI application keys are fixed-length opaque strings (50 characters at
# time of writing). The check is deliberately loose -- a length floor only --
# because rejecting a real key on a format guess is far worse than passing a
# malformed one upstream and letting RapidAPI return its own clear 401.
MIN_KEY_LENGTH = 20


@dataclass(frozen=True)
class Credential:
    """A resolved key plus where it came from, for diagnostics only."""

    key: str
    source: str

    @property
    def present(self) -> bool:
        return bool(self.key)


NO_CREDENTIAL = Credential(key="", source="none")


def redact(key: str) -> str:
    """A stable, non-reversible label for a key, safe to log.

    Shows only the leading four characters and the length. Enough to tell two
    keys apart in a log, useless to anyone who reads the log.
    """
    if not key:
        return "<none>"
    if len(key) <= 8:
        return f"<{len(key)} chars>"
    return f"{key[:4]}...<{len(key)} chars>"


def _clean(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    # A key pasted out of a JSON snippet or an .env file often keeps its
    # quotes; that would otherwise become an upstream 401 that looks like a
    # wrong key rather than a paste artefact.
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1].strip()
    return value


def _from_headers(headers: dict[str, str]) -> Credential:
    for name in HEADER_NAMES:
        value = _clean(headers.get(name))
        if value:
            return Credential(key=value, source=f"header:{name}")

    raw = _clean(headers.get(BEARER_HEADER))
    if raw:
        # Tolerate a bare key in the Authorization header as well as a proper
        # Bearer prefix; both are common in the wild.
        if raw.lower().startswith("bearer "):
            token = _clean(raw[7:])
        else:
            token = raw
        if token:
            return Credential(key=token, source="header:authorization")
    return NO_CREDENTIAL


def _decode_config_blob(raw: str) -> dict[str, Any]:
    """Smithery's `config=` parameter: base64-encoded JSON, possibly URL-safe
    and possibly unpadded. Returns {} for anything that does not decode --
    a malformed blob must fall through to the other sources, not raise."""
    if not raw:
        return {}
    candidate = raw.strip()
    # Base64 without padding is common in URLs; add it back rather than fail.
    padding = "=" * (-len(candidate) % 4)
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            decoded = decoder(candidate + padding)
        except (binascii.Error, ValueError):
            continue
        try:
            parsed = json.loads(decoded.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _from_query(params: dict[str, str]) -> Credential:
    lowered = {k.lower(): v for k, v in params.items()}

    # 1. Names that can only mean our key.
    for name in SPECIFIC_QUERY_NAMES:
        value = _clean(lowered.get(name))
        if value:
            return Credential(key=value, source=f"query:{name}")

    # Smithery dot-notation, e.g. ?config.rapidApiKey=...
    for name, value in lowered.items():
        if not name.startswith(CONFIG_QUERY_PREFIX):
            continue
        field = name[len(CONFIG_QUERY_PREFIX) :]
        if field in CONFIG_FIELD_NAMES:
            cleaned = _clean(value)
            if cleaned:
                return Credential(key=cleaned, source=f"query:{name}")

    # Smithery base64 blob, e.g. ?config=eyJyYXBpZEFwaUtleSI6Ii4uLiJ9
    blob = _decode_config_blob(lowered.get("config", ""))
    for field, value in blob.items():
        if field.lower() in CONFIG_FIELD_NAMES:
            cleaned = _clean(value)
            if cleaned:
                return Credential(key=cleaned, source="query:config")

    # 3. Generic names last. Skipped entirely when a `config` parameter is
    #    present, because that combination is a proxying gateway and the
    #    generic name is then its key, not ours -- forwarding it upstream
    #    would fail auth and read to the user as "no key supplied".
    if "config" not in lowered:
        for name in GENERIC_QUERY_NAMES:
            value = _clean(lowered.get(name))
            if value:
                return Credential(key=value, source=f"query:{name}")

    return NO_CREDENTIAL


def resolve_credential(
    headers: dict[str, str],
    query_params: dict[str, str],
    fallback: str = "",
) -> Credential:
    """Find the caller's RapidAPI key. See the module docstring for order.

    `headers` keys must already be lowercased; `_request_context` in server.py
    does that. Returns NO_CREDENTIAL rather than raising -- the caller decides
    how to phrase the failure, because the right phrasing depends on which
    tool was invoked.
    """
    for candidate in (_from_headers(headers), _from_query(query_params)):
        if candidate.present:
            return candidate

    fallback = _clean(fallback)
    if fallback:
        return Credential(key=fallback, source="env:RAPIDAPI_KEY")
    return NO_CREDENTIAL


def missing_key_message(signup_url: str) -> str:
    """What the model is told when no key arrived.

    Written to be read aloud to a human by an assistant, because that is
    exactly what will happen to it. It names the three ways to supply a key
    because we cannot know which of them the caller's host supports.
    """
    return (
        "No RapidAPI key was supplied, so this search cannot run. This server "
        "is free to use but each search is billed to the caller's own Google "
        "Flights API subscription.\n\n"
        f"1. Get a key (free tier available): {signup_url}\n"
        "2. Supply it in any ONE of these ways:\n"
        "   - add the header `x-rapidapi-key: <your key>` to the connection, or\n"
        "   - append `?rapidapi_key=<your key>` to the server URL, or\n"
        "   - fill in the API key field if your client shows one.\n\n"
        "Prefer the header where your client supports it: a key in a URL ends "
        "up in server and proxy logs."
    )


def key_looks_malformed(key: str) -> bool:
    """True for values that cannot be a RapidAPI key.

    Only used to produce a better error than a bare upstream 401. It never
    blocks a request on its own.
    """
    return bool(key) and len(key) < MIN_KEY_LENGTH
