"""
Credential resolution: the one piece of this server with no equivalent in the
free one, and the one most likely to fail silently for a whole class of users.

Every host passes API keys differently. A bug here does not look like a bug --
it looks like "this server doesn't work in Cursor" -- so each accepted
mechanism gets an explicit test rather than being covered by inference.
"""

import base64
import json

from src.credentials import (
    Credential,
    key_looks_malformed,
    missing_key_message,
    redact,
    resolve_credential,
)

KEY = "2b3b32aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
OTHER = "9f9f9f9f9f9f9f9f9f9f9f9f9f9f9f9f9f9f9f9f9f9f9f9f9f"


class TestHeaders:
    def test_x_rapidapi_key(self):
        got = resolve_credential({"x-rapidapi-key": KEY}, {})
        assert got == Credential(key=KEY, source="header:x-rapidapi-key")

    def test_x_api_key(self):
        got = resolve_credential({"x-api-key": KEY}, {})
        assert got.key == KEY
        assert got.source == "header:x-api-key"

    def test_bearer_token(self):
        got = resolve_credential({"authorization": f"Bearer {KEY}"}, {})
        assert got.key == KEY
        assert got.source == "header:authorization"

    def test_bearer_prefix_is_case_insensitive(self):
        assert resolve_credential({"authorization": f"bearer {KEY}"}, {}).key == KEY

    def test_bare_authorization_value(self):
        """Some clients put the raw key in Authorization with no scheme."""
        assert resolve_credential({"authorization": KEY}, {}).key == KEY

    def test_surrounding_quotes_are_stripped(self):
        """A key pasted out of JSON or an .env keeps its quotes, and the
        upstream 401 that produces looks like a wrong key, not a paste bug."""
        assert resolve_credential({"x-rapidapi-key": f'"{KEY}"'}, {}).key == KEY

    def test_whitespace_is_stripped(self):
        assert resolve_credential({"x-rapidapi-key": f"  {KEY}\n"}, {}).key == KEY

    def test_empty_header_falls_through(self):
        got = resolve_credential({"x-rapidapi-key": "   "}, {"rapidapi_key": KEY})
        assert got.key == KEY
        assert got.source.startswith("query:")


class TestQueryParams:
    def test_snake_case(self):
        got = resolve_credential({}, {"rapidapi_key": KEY})
        assert got == Credential(key=KEY, source="query:rapidapi_key")

    def test_alternate_spellings(self):
        for name in ("rapidapi-key", "rapidapiKey", "api_key", "apikey", "key"):
            got = resolve_credential({}, {name: KEY})
            assert got.key == KEY, name

    def test_smithery_dot_notation(self):
        got = resolve_credential({}, {"config.rapidApiKey": KEY})
        assert got.key == KEY
        assert got.source == "query:config.rapidapikey"

    def test_smithery_base64_blob(self):
        blob = base64.b64encode(
            json.dumps({"rapidApiKey": KEY}).encode("utf-8")
        ).decode("ascii")
        got = resolve_credential({}, {"config": blob})
        assert got.key == KEY
        assert got.source == "query:config"

    def test_smithery_urlsafe_unpadded_base64(self):
        """URL-safe and stripped of padding is the common on-the-wire form."""
        raw = base64.urlsafe_b64encode(
            json.dumps({"apiKey": KEY}).encode("utf-8")
        ).decode("ascii")
        got = resolve_credential({}, {"config": raw.rstrip("=")})
        assert got.key == KEY

    def test_malformed_config_blob_is_ignored_not_fatal(self):
        """A blob that does not decode must fall through, never raise -- and a
        name that can only mean our key still works."""
        got = resolve_credential({}, {"config": "not base64!!", "rapidapi_key": KEY})
        assert got.key == KEY

    def test_malformed_config_still_suppresses_generic_names(self):
        """`config` present at all means a gateway is proxying us, so a
        generic `key`/`api_key` is its credential and not the caller's --
        even when the blob itself is unreadable. See
        TestGatewayOwnKeyIsNotMistakenForOurs for what this prevents."""
        got = resolve_credential({}, {"config": "not base64!!", "key": KEY})
        assert not got.present

    def test_config_blob_without_a_key_field_is_ignored(self):
        blob = base64.b64encode(json.dumps({"region": "eu"}).encode()).decode()
        assert not resolve_credential({}, {"config": blob}).present


class TestPrecedence:
    def test_header_beats_query(self):
        """Headers stay out of logs, so they win when both are supplied."""
        got = resolve_credential({"x-rapidapi-key": KEY}, {"rapidapi_key": OTHER})
        assert got.key == KEY

    def test_query_beats_env_fallback(self):
        got = resolve_credential({}, {"rapidapi_key": KEY}, fallback=OTHER)
        assert got.key == KEY

    def test_env_fallback_is_last(self):
        got = resolve_credential({}, {}, fallback=KEY)
        assert got == Credential(key=KEY, source="env:RAPIDAPI_KEY")

    def test_nothing_anywhere(self):
        got = resolve_credential({}, {})
        assert not got.present
        assert got.source == "none"


class TestRedaction:
    def test_never_reveals_the_key(self):
        out = redact(KEY)
        assert KEY not in out
        assert out.startswith("2b3b")
        assert "50" in out

    def test_short_values_reveal_nothing_at_all(self):
        assert redact("abc") == "<3 chars>"

    def test_empty(self):
        assert redact("") == "<none>"

    def test_two_keys_are_distinguishable(self):
        assert redact(KEY) != redact(OTHER)


class TestMalformedCheck:
    def test_short_value_is_flagged(self):
        assert key_looks_malformed("abc")

    def test_real_length_is_not_flagged(self):
        assert not key_looks_malformed(KEY)

    def test_empty_is_not_flagged(self):
        """Absent is a different condition from malformed, and gets a
        different message."""
        assert not key_looks_malformed("")


class TestMissingKeyMessage:
    def test_names_every_supported_mechanism(self):
        """The message is the only documentation a user in a chat window
        gets, so it has to cover the mechanism their client happens to use."""
        text = missing_key_message("https://example.test/api")
        assert "x-rapidapi-key" in text
        assert "?rapidapi_key=" in text
        assert "https://example.test/api" in text

    def test_warns_about_keys_in_urls(self):
        assert "logs" in missing_key_message("https://example.test/api")


class TestGatewayOwnKeyIsNotMistakenForOurs:
    """The bug that shipped and would have broken every Smithery install.

    Smithery proxies to us as `?api_key=<SMITHERY key>&config=<base64 of the
    user's config>`. `api_key` used to be accepted before the config blob was
    read, so the gateway's own key was forwarded to RapidAPI, failed auth, and
    surfaced to the user as "no RapidAPI key was supplied" -- no matter what
    they had typed into Smithery. It looked like a config problem on their end.
    """

    SMITHERY_KEY = "49c187f7-dc45-4ee7-af98-e8275854b5c8"

    def _smithery_query(self, user_key: str) -> dict[str, str]:
        blob = base64.b64encode(
            json.dumps({"rapidApiKey": user_key}).encode()
        ).decode()
        return {"api_key": self.SMITHERY_KEY, "config": blob}

    def test_user_key_wins_over_the_gateway_key(self):
        got = resolve_credential({}, self._smithery_query(KEY))
        assert got.key == KEY
        assert got.key != self.SMITHERY_KEY
        assert got.source == "query:config"

    def test_dot_notation_also_beats_the_gateway_key(self):
        params = {"api_key": self.SMITHERY_KEY, "config.rapidApiKey": KEY}
        assert resolve_credential({}, params).key == KEY

    def test_generic_name_is_ignored_whenever_config_is_present(self):
        """Even with nothing usable in the blob, a gateway's key must never be
        forwarded upstream as if it were the caller's."""
        blob = base64.b64encode(json.dumps({"region": "eu"}).encode()).decode()
        got = resolve_credential({}, {"api_key": self.SMITHERY_KEY, "config": blob})
        assert not got.present

    def test_generic_name_still_works_for_a_direct_caller(self):
        """No `config` means no gateway, so ?api_key= is the user's own key and
        must keep working -- this is the plain-URL path we document."""
        got = resolve_credential({}, {"api_key": KEY})
        assert got.key == KEY
        assert got.source == "query:api_key"

    def test_explicit_rapidapi_name_beats_everything(self):
        params = {"api_key": self.SMITHERY_KEY, "rapidapi_key": KEY}
        assert resolve_credential({}, params).key == KEY

    def test_header_still_beats_a_gateway_query_key(self):
        got = resolve_credential(
            {"x-rapidapi-key": KEY}, self._smithery_query(OTHER)
        )
        assert got.key == KEY
