"""
Model-facing text states facts, never directives (2026-10-22).

The Claude Connectors Directory submission carries the acknowledgment "Tool
descriptions contain no instructions about model behavior, other tools, or
external instruction sources, and no hidden or encoded text". Every string a
client reads before calling anything -- `instructions`, each tool's
description and title, every input and output schema description, every
prompt description and prompt argument description -- is therefore written
as a plain fact about the tool: what it does, what it takes, its limits,
what it costs and what a result means. "Tell the user ...", "say that number
...", "always ...", "never ..." aimed at the model were rewritten that way.

This file walks that whole surface in every shape a deployment can serve
(flights, hotels, both; with and without the free sign-in allowance; with the
multi-source compare tool on) and fails on any directive pattern. A factual
use of a flagged word goes in ALLOWED_PHRASES, verbatim, with the reason it
is a fact and not an instruction.

The refusal and result texts a tool returns are covered too, more narrowly:
they may state facts and next steps for the person, but none of them tells
the model what to say or not say.

Set TOOL_TEXT_DUMP=<path> to write the collected surface to a file (used to
produce the before/after texts for review).
"""

from __future__ import annotations

import json
import os
import re

import httpx
import pytest
from fastmcp import Client

from src import quota_gate, status_text
from src.server import _degraded_message, build_instructions
from tests.test_oauth import Session, _payload, mcp_request
from tests.test_server import build_with_upstream, make_settings
from tests.test_trial import _bearer, _build_trial

#: Directive patterns. Case-insensitive, matched per sentence.
DIRECTIVE_PATTERNS = [
    r"\btell(s|ing)? (the |a )?(user|them|me|people|person)\b",
    r"\bask (the |a )?(user|them)\b",
    r"\bsay\b",
    r"\bnever\b",
    r"\balways\b",
    r"\byou must\b",
    r"\bdo not\b",
    r"\bdon'?t\b",
    r"\bmake sure\b",
    r"\bremember\b",
    r"\bshould\b",
    r"\brelay",
    r"\bworth one sentence\b",
    r"\bto quote\b",
    r"\bquote (it|back|them)\b",
    r"\breport (it|that|them)\b",
    r"\breach for\b",
    r"\b(use|read) (it|this|them)\b",
    r"\b(a|the|no) model\b",
    r"\byou\b",
    r"\byour\b",
    r"\bwhen the user\b",
    r"\bsearch again\b",
    r"\bnarrow (it|a wide)\b",
    r"\bhold one\b",
    r"\bcall each\b",
    r"\bmake one call\b",
    r"\bcall it once\b",
    r"\binstead of implying\b",
    r"\brather than quoting\b",
    # A sentence that opens on a bare imperative verb.
    r"(?:^|[.:;!?]\s+|\n)(Read|Use|Say|Tell|Pass|Set|Call|Hold|Give|Leave|Omit"
    r"|Turn|Reach|Search|Compare|Narrow|Ask|Include|Report|Quote|Make|Keep"
    r"|Avoid|Prefer|Check|Do|Judge|Mention|State|Explain|Warn)\b",
]

#: Factual uses of a flagged word, verbatim. Each is a statement about what
#: the tool does, not an instruction to the reader.
ALLOWED_PHRASES = [
    # The row selection keeps the cheapest property: a guarantee of the output.
    "the cheapest always among them",
    "the cheapest always included",
    "and the cheapest is always included",
    # results_total is present on every response that bounds rows.
    "results_total always says how many there were",
    # The per-destination row selection: what the server does.
    "never all of one destination at the expense of another",
    # Coverage facts: a combination that was not searched.
    "was never searched",
    "were never sent or billed",
    "was never priced",
    "never searched --",
    # The filter validation: an outcome guarantee.
    "never quietly returns unfiltered results",
]


def _strip_allowed(text: str) -> str:
    for phrase in ALLOWED_PHRASES:
        text = text.replace(phrase, " ")
    return text


def directive_hits(text: str) -> list[str]:
    """Every directive pattern in `text`, as the matching sentence."""
    cleaned = _strip_allowed(text or "")
    hits = []
    for pattern in DIRECTIVE_PATTERNS:
        for match in re.finditer(pattern, cleaned, flags=re.IGNORECASE):
            # The imperative-opener pattern is case-sensitive on purpose: a
            # lower-case "use" mid-sentence is caught by the other patterns.
            if pattern.startswith("(?:^") and not re.match(
                pattern, cleaned[match.start():]
            ):
                continue
            start = max(0, match.start() - 60)
            hits.append(f"{match.group(0)!r} in ...{cleaned[start:match.end() + 60]}...")
    return hits


def _walk(node, where: str, out: list[tuple[str, str]]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("description", "title", "instructions") and isinstance(
                value, str
            ):
                out.append((f"{where}/{key}", value))
            else:
                _walk(value, f"{where}/{key}", out)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            _walk(value, f"{where}[{i}]", out)


def _surface(init: dict, tools: list, prompts: list, resources: list) -> list:
    out: list[tuple[str, str]] = [("instructions", init.get("instructions") or "")]
    for tool in tools:
        _walk(tool, f"tool:{tool['name']}", out)
    # A prompt's title and argument descriptions are the labels of a form the
    # PERSON fills in ("Compare destinations on price", "Optional price you
    # were quoted"), and its body is the person's own request; only the
    # prompt's description is read as a statement about the server.
    for prompt in prompts:
        out.append((f"prompt:{prompt['name']}/description", prompt.get("description") or ""))
    for resource in resources:
        _walk(resource, f"resource:{resource.get('name')}", out)
    return out


async def _plain_surface(products: str) -> list[tuple[str, str]]:
    mcp = build_with_upstream(
        lambda _r: httpx.Response(200, json=[]),
        products=products,
        hotel_compare_enabled=True,
    )
    async with Client(mcp) as client:
        init = client.initialize_result.model_dump(mode="json")
        tools = [t.model_dump(mode="json") for t in await client.list_tools()]
        prompts = [p.model_dump(mode="json") for p in await client.list_prompts()]
        resources = [r.model_dump(mode="json") for r in await client.list_resources()]
    return _surface(init, tools, prompts, resources)


async def _trial_surface(monkeypatch, products: str) -> list[tuple[str, str]]:
    deployment = _build_trial(monkeypatch, cap=10, products=products, compare=True)
    async with Session(deployment) as session:
        token = await _bearer(session.http)
        auth = {"authorization": f"Bearer {token}"}
        started = await mcp_request(session.http, "/mcp", {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "facts", "version": "1.0"},
            },
        }, auth)
        sid = started.headers.get("mcp-session-id")
        extra = dict(auth, **({"mcp-session-id": sid} if sid else {}))
        listed = {}
        for i, method in enumerate(("tools/list", "prompts/list", "resources/list")):
            reply = await mcp_request(session.http, "/mcp", {
                "jsonrpc": "2.0", "id": 2 + i, "method": method,
            }, extra)
            listed[method] = _payload(reply.text)["result"]
    return _surface(
        _payload(started.text)["result"],
        listed["tools/list"]["tools"],
        listed["prompts/list"]["prompts"],
        listed["resources/list"]["resources"],
    )


CONFIGS = [
    ("flights", False),
    ("hotels", False),
    ("both", False),
    ("flights", True),
    ("hotels", True),
    ("both", True),
]


async def _collect(monkeypatch, products: str, trial: bool):
    if trial:
        return await _trial_surface(monkeypatch, products)
    return await _plain_surface(products)


@pytest.mark.parametrize(
    "products,trial", CONFIGS, ids=[f"{p}-{'trial' if t else 'plain'}" for p, t in CONFIGS]
)
async def test_model_facing_text_has_no_directives(monkeypatch, products, trial):
    surface = await _collect(monkeypatch, products, trial)
    assert any(where.startswith("tool:") for where, _ in surface)
    dump = os.environ.get("TOOL_TEXT_DUMP")
    if dump:
        with open(dump, "a", encoding="utf-8") as handle:
            handle.write(f"\n===== {products} {'trial' if trial else 'plain'}\n")
            for where, text in surface:
                handle.write(f"## {where}\n{text}\n\n")
    failures = [
        f"{where}: {hit}" for where, text in surface for hit in directive_hits(text)
    ]
    assert not failures, "\n".join(failures)


@pytest.mark.parametrize("products", ["flights", "hotels", "both"])
@pytest.mark.parametrize("trial_tail", ["", "10 free searches a day."])
def test_instructions_have_no_directives(products, trial_tail):
    text = build_instructions(
        make_settings(products=products),
        trial_tail=trial_tail,
        connect_url="https://mcp.test/connect",
    )
    assert not directive_hits(text), directive_hits(text)


# ── result and refusal texts ─────────────────────────────────────────────

#: Narrower than the description check: a refusal may tell the PERSON what to
#: do next ("ask again for fewer dates", "subscribe there"), but nothing in it
#: tells the model what to say or not say.
RESULT_PATTERNS = [
    r"\btell (the |a )?user\b",
    r"\bdo not tell\b",
    r"\bsay so\b",
    r"\bdo not retry\b",
    r"\brather than reporting\b",
    r"\bdo not suggest\b",
    r"\bdo not treat\b",
    r"\bif you report\b",
    r"\brelay",
]


def result_hits(text: str) -> list[str]:
    return [
        p for p in RESULT_PATTERNS if re.search(p, text or "", flags=re.IGNORECASE)
    ]


def test_status_text_lines_are_facts():
    assert not result_hits(status_text.DEGRADED_FIRST_LINE)
    assert not result_hits(status_text._PARTIAL_TAIL)


def test_degraded_message_is_a_fact():
    text = _degraded_message(3, 3, "upstream_no_render")
    assert not result_hits(text), text
    assert "NOT a statement about flight availability" in text


def test_quota_refusals_are_facts():
    trial = quota_gate.trial_quota_refusal(
        requested=93, remaining_today=4, day_cap=10,
        connect_url="https://mcp.test/connect",
        signup_url="https://rapidapi.test/google-flights",
    )
    plan = quota_gate.plan_quota_refusal(
        requested=93, remaining_month=10, spent_probing=1,
        signup_url="https://rapidapi.test/google-flights",
        results=[{"price": "$1"}],
    )
    for reply in (trial, plan):
        assert not result_hits(reply["message"]), reply["message"]
        assert reply["retry"] is False


def test_trial_exhausted_message_is_a_fact():
    from src.trial import TrialState, trial_exhausted_result

    state = TrialState(user_sub="sub", email="a@b.test", used_today=10, day_cap=10)
    reply = trial_exhausted_result(
        state,
        signup_url="https://rapidapi.test/google-flights",
        api_name="Google Flights Live API",
        connect_url="https://mcp.test/connect",
    )
    assert not result_hits(reply["message"]), reply["message"]


def test_the_checker_catches_what_it_should():
    """A guard on the guard: each rewritten directive would fail it."""
    for bad in (
        "narrow it to fit and tell the user that the whole month needs their own key.",
        "Say that number before running a search that large.",
        "always express a flexible question as ONE call",
        "never as many single-date calls",
        "so read it before telling a user a destination has no flights.",
        "Worth one sentence to the user before running one.",
        "Read the caveats on every row before you say one source is cheaper.",
        "Leave unset.",
        "when the user asks for a current price, search again rather than reusing",
        "so hold one named property fixed, call each country a few times",
        "For a flexible search make ONE call with a date range",
    ):
        assert directive_hits(bad), bad
    assert not directive_hits(
        "A whole-month scan needs the caller's own key; the free allowance "
        "covers 10 searches a day."
    )
    assert json.dumps(ALLOWED_PHRASES)
