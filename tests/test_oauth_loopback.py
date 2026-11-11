"""RFC 8252 §7.3: a loopback redirect_uri matches on any port.

Claude Code's client metadata document lists `http://localhost/callback` and
`http://127.0.0.1/callback`, then signs in on an ephemeral port
(`http://localhost:53682/callback`). An exact string match refused that, so
Claude Code could not sign in (github.com/mtnrabi/google-flights-mcp/issues/15).
"""

from __future__ import annotations

import pytest

import src.cimd as cimd
from src.oauth import OAuthError, OAuthSupport, _redirect_uri_registered

CLAUDE_CODE = ("http://localhost/callback", "http://127.0.0.1/callback")


@pytest.mark.parametrize(
    "requested",
    [
        "http://localhost/callback",
        "http://localhost:53682/callback",
        "http://localhost:80/callback",
        "http://127.0.0.1:53682/callback",
        "http://LOCALHOST:1/callback",
    ],
)
def test_a_loopback_uri_matches_on_any_port(requested):
    assert _redirect_uri_registered(requested, CLAUDE_CODE)


def test_ipv6_loopback_matches_on_any_port():
    assert _redirect_uri_registered("http://[::1]:5000/cb", ("http://[::1]/cb",))


@pytest.mark.parametrize(
    "requested",
    [
        "http://localhost:53682/evil",  # different path
        "http://localhost:53682/callback?x=1",  # different query
        "http://localhost:53682/callback#frag",  # fragment
        "https://localhost:53682/callback",  # different scheme
        "http://[::1]:53682/callback",  # a host that was not registered
        "http://evil.test:53682/callback",  # not loopback
        "http://localhost.evil.test:53682/callback",
        "http://localhost:53682@evil.test/callback",  # userinfo trick
        "http://evil@localhost:53682/callback",
        "http://localhost:notaport/callback",
        "http://localhost:99999/callback",
        "http://localhost:53682/callback/",
    ],
)
def test_everything_but_the_port_must_still_match(requested):
    assert not _redirect_uri_registered(requested, CLAUDE_CODE)


def test_a_non_loopback_uri_is_still_an_exact_match():
    registered = ("https://client.test/cb", "cursor://anysphere.cursor/cb")
    assert _redirect_uri_registered("https://client.test/cb", registered)
    assert not _redirect_uri_registered("https://client.test:8443/cb", registered)
    assert not _redirect_uri_registered("https://client.test/cb2", registered)
    assert not _redirect_uri_registered("cursor://anysphere.cursor/cb2", registered)


async def test_a_cimd_client_signs_in_on_an_ephemeral_port(monkeypatch):
    client_id = "https://claude.ai/oauth/claude-code-client-metadata"
    document = {"client_id": client_id, "client_name": "Claude Code",
                "redirect_uris": list(CLAUDE_CODE)}

    async def _load(url, now=None):
        assert url == client_id
        return document

    monkeypatch.setattr(cimd, "load", _load)
    # lookup_client's CIMD branch never touches `self`'s stores.
    support = OAuthSupport.__new__(OAuthSupport)
    client = await support.lookup_client(client_id, "http://localhost:53682/callback")
    assert client is not None and client.client_id == client_id
    with pytest.raises(OAuthError, match="not listed"):
        await support.lookup_client(client_id, "http://localhost:53682/evil")
